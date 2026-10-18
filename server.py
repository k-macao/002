#!/usr/bin/env python3
"""本地/容器运行的章鱼 AI·全景分析 API 与静态文件服务。

启动：PUSHPLUS_TOKEN=... python3 server.py
可选：PORT=4173

POST /api/run 推送前闸门与 ``push_brief.py`` 相同：
大盘数据非最新 → 409；敏感词检测未通过 → 422。
推送默认一对一（只发给 token 所属账号）；显式设 PUSHPLUS_TOPIC=oai.1 才是一对多群组推送。
诊断：GET /api/market、GET /api/sensitive。

DeepSeek Search & Fetch（2026-10-17 新 API，需 DEEPSEEK_API_KEY）：
GET  /api/deepseek/status 配置自检（不泄漏 Key）
GET  /api/search?q=...&count=10&need_content=0 全网搜索
POST /api/search {"query": ..., "count": ...} 同上
GET  /api/fetch?url=... / POST /api/fetch {"url": ...} 抓单页 Markdown 正文
未配置 Key 时搜索/抓取返回 503，其余接口不受影响。
"""
import json
import os
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

import deepseek_search
import sensitive
import sources

ROOT = Path(__file__).resolve().parent
# 推送地址可用环境变量覆盖，默认走真实 PushPlus；测试时指向本地假服务。
PUSHPLUS_API_URL = os.environ.get("PUSHPLUS_API_URL", "https://www.pushplus.plus/send")
# 群组编码：留空（默认）= 一对一推送，消息只发给 token 所属账号自己；
# 只有显式配置 PUSHPLUS_TOPIC=oai.1 才切回一对多群组推送。
PUSHPLUS_TOPIC = os.environ.get("PUSHPLUS_TOPIC", "").strip()
SOURCES = sources.SOURCES

# /api/brief 的结果缓存（并发抓取六大板块 69 个源仍需数秒，5 分钟内不重复抓取）。
_BRIEF_CACHE = {"at": 0.0, "data": None}
_BRIEF_TTL = 300
# /api/market 与推送闸门共用的大盘数据缓存（东方财富日 K + 新鲜度检查）。
_MARKET_CACHE = {"at": 0.0, "payload": None}
_MARKET_TTL = 300
# /api/search 的结果缓存：DeepSeek Search 按成功请求计费（$0.75/千次），
# 同一查询 5 分钟内复用结果，避免页面刷新重复计费。
_SEARCH_CACHE: dict = {}
_SEARCH_TTL = 300


def cached_web_search(query: str, count: int, need_content: bool) -> tuple:
    """带 5 分钟缓存的 DeepSeek 全网搜索，返回 (items, cache_hit)。"""
    key = (query, count, bool(need_content))
    now = time.time()
    hit = _SEARCH_CACHE.get(key)
    if hit and now - hit["at"] < _SEARCH_TTL:
        return hit["items"], True
    items = sources.web_search(query, limit=count, need_content=need_content)
    _SEARCH_CACHE[key] = {"at": now, "items": items}
    return items, False


def _deepseek_error_response(handler, error: deepseek_search.DeepSeekSearchError):
    """把 DeepSeek 客户端异常映射为 HTTP 响应（未配置 503 / 参数错误 400 / 上游失败 502）。"""
    if isinstance(error, deepseek_search.DeepSeekNotConfigured):
        return handler.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
            "message": "服务端未配置 DEEPSEEK_API_KEY，DeepSeek Search/Fetch 未启用。"
                       "申请 Key 后以 DEEPSEEK_API_KEY=... 启动服务即可；"
                       "未配置时全部搜索型数据源自动回退原 RSS 通道。",
            "docs": deepseek_search.DOCS_URL,
        })
    status = HTTPStatus.BAD_REQUEST if error.status == 400 else HTTPStatus.BAD_GATEWAY
    return handler.send_json(status, {
        "message": f"DeepSeek API 调用失败：{error}",
        "upstream_status": error.status, "upstream_code": error.code,
    })


def get_brief():
    """抓取并缓存全量简报数据：{name: [item, ...]}。"""
    now = time.time()
    if _BRIEF_CACHE["data"] is not None and now - _BRIEF_CACHE["at"] < _BRIEF_TTL:
        return _BRIEF_CACHE["data"]
    data = sources.collect_all()
    _BRIEF_CACHE["at"] = now
    _BRIEF_CACHE["data"] = data
    return data


def get_market():
    """抓取并缓存大盘数据 + 新鲜度检查结果：{"market": ..., "freshness": ...}。"""
    now = time.time()
    if _MARKET_CACHE["payload"] is not None and now - _MARKET_CACHE["at"] < _MARKET_TTL:
        return _MARKET_CACHE["payload"]
    market, freshness = sources.collect_market_for_push()
    payload = {"market": market, "freshness": freshness}
    _MARKET_CACHE["at"] = now
    _MARKET_CACHE["payload"] = payload
    return payload


def _skip_market_check() -> bool:
    """SKIP_MARKET_CHECK=1/true/yes/on 时跳过大盘数据新鲜度检查（测试/应急）。"""
    return os.environ.get("SKIP_MARKET_CHECK", "").strip().lower() in ("1", "true", "yes", "on")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # The UI and API can be placed behind a preview proxy without stale index.html.
        self.send_header("Cache-Control", "no-store" if self.path in ("/", "/index.html") else "public, max-age=300")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/sources":
            return self.send_json(HTTPStatus.OK, SOURCES)
        if path == "/api/sections":
            # 板块目录：[{key, label, note, sources, count}]，六大板块与各自的数据源清单。
            return self.send_json(HTTPStatus.OK, sources.section_catalog())
        if path == "/api/sensitive":
            # 敏感词检测诊断：与推送闸门同一套词库，只扫不改，供排障。
            if sensitive.skip_enabled():
                return self.send_json(HTTPStatus.OK, {
                    "ok": True, "skipped": True,
                    "reason": "SKIP_SENSITIVE_CHECK 已启用，跳过敏感词检测（仅限测试/应急）",
                    "dropped_count": 0, "dropped": [], "categories": [],
                    "lexicon_size": 0,
                })
            try:
                brief = get_brief()
            except Exception as error:
                return self.send_json(HTTPStatus.OK, {"error": str(error)})
            return self.send_json(HTTPStatus.OK, sensitive.inspect_brief(brief))
        if path == "/api/market":
            # 大盘数据新鲜度状态：推送闸门「不是最新就不推」同一套检查结果，供页面展示。
            try:
                payload = get_market()
            except Exception as error:
                return self.send_json(HTTPStatus.OK, {"error": str(error)})
            review = sources.analyze_ashare(payload["market"])
            return self.send_json(HTTPStatus.OK, {
                "freshness": payload["freshness"],
                "date": review.get("date"),
                "source": review.get("source"),
                "bias": review.get("bias"),
                "indices": review.get("indices"),
            })
        if path == "/api/heatmap":
            # 情绪热力图：关键词级热度与情绪（面向股市投资）
            # 每个关键词含 热度/情绪/共振/分歧/爆发/资金倾向/评级/风险 等 9 维指标；
            # 线索新闻（标题/来源/链接/情绪标签）仅由本数据接口返回，页面与推送均不展示线索列表。
            try:
                brief = get_brief()
            except Exception as error:
                return self.send_json(HTTPStatus.OK, {"error": str(error)})
            # ?top=40 控制返回关键词数，默认 40；?brief=1 连同简报一起返回便于调试
            qs = parse_qs(query)
            try:
                top_n = int((qs.get("top") or ["40"])[0])
            except ValueError:
                top_n = 40
            top_n = max(1, min(top_n, 100))
            heatmap = sources.analyze_keyword_heatmap(brief, top_n=top_n)
            if (qs.get("brief") or [""])[0].strip().lower() in ("1", "true", "yes", "on"):
                heatmap = dict(heatmap)
                heatmap["brief"] = brief
            return self.send_json(HTTPStatus.OK, heatmap)
        if path == "/api/policy":
            # 「AI 政策分析」：政策维度统计 + 近30日全球政策对股市的影响。
            # deep=1 再叠加知识库检索、修饰词、舆情、传导图谱、推理链、研报和 LSTM / Prophet。
            # ?deep=0 不跑模型，但仍返回 market_impact。走 /api/brief 同一份缓存。
            deep = (parse_qs(query).get("deep") or ["1"])[0].strip().lower() not in ("0", "false", "no")
            try:
                brief = get_brief()
            except Exception as error:
                return self.send_json(HTTPStatus.OK, {"error": str(error)})
            return self.send_json(HTTPStatus.OK, sources.analyze_policy(brief, deep=deep))
        if path == "/api/deepseek/status":
            # DeepSeek Search & Fetch 配置自检（2026-10-17 新 API）：不泄漏 Key，
            # 附全部已接入搜索的数据源清单，供页面状态与排障。
            payload = deepseek_search.status()
            payload["search_backed_sources"] = sources.deepseek_search_sources()
            payload["fallback"] = "未启用/搜索失败时搜索型源回退 Google News RSS，再回退演示数据"
            return self.send_json(HTTPStatus.OK, payload)
        if path == "/api/search":
            # DeepSeek 全网搜索：?q=关键词&count=10&need_content=0（支持 site:/lang:/after: 算子）。
            qs = parse_qs(query)
            q = (qs.get("q") or qs.get("query") or [""])[0].strip()
            if not q:
                return self.send_json(HTTPStatus.BAD_REQUEST,
                                      {"message": "缺少搜索关键词：请传 q（1-128 字符，可用 site:/lang:/after: 算子）"})
            try:
                count = int((qs.get("count") or ["10"])[0])
            except ValueError:
                count = 10
            need_content = (qs.get("need_content") or [""])[0].strip().lower() in ("1", "true", "yes", "on")
            try:
                items, cache_hit = cached_web_search(q, deepseek_search.clamp_count(count), need_content)
            except deepseek_search.DeepSeekSearchError as error:
                return _deepseek_error_response(self, error)
            return self.send_json(HTTPStatus.OK, {
                "query": q, "count": len(items), "cached": cache_hit,
                "provider": "deepseek-search", "results": items,
            })
        if path == "/api/fetch":
            # DeepSeek Fetch：抓单页 Markdown 正文（?url=...）。
            target = (parse_qs(query).get("url") or [""])[0].strip()
            if not target:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"message": "缺少 url 参数"})
            try:
                page = sources.fetch_web_page(target)
            except deepseek_search.DeepSeekSearchError as error:
                return _deepseek_error_response(self, error)
            return self.send_json(HTTPStatus.OK, page)
        if path == "/api/brief":
            # 可选 ?section=policy|world|civic|finance|trending：只返回该板块的源（走同一份缓存）。
            section = (parse_qs(query).get("section") or [""])[0].strip()
            if section and section not in sources.SECTION_KEYS:
                return self.send_json(HTTPStatus.NOT_FOUND, {"message": f"板块不存在：{section}",
                                                             "sections": sources.SECTION_KEYS})
            try:
                brief = get_brief()
            except Exception as error:  # 抓取异常也返回可用结果
                brief = {"error": str(error)}
            if section and "error" not in brief:
                brief = {name: brief.get(name, []) for name in sources.sources_in_section(section)}
            return self.send_json(HTTPStatus.OK, brief)
        return super().do_GET()

    def _read_json_body(self) -> dict:
        """读取 POST JSON body；无 body / 非法 JSON 返回 {}（供各接口自行报缺参）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/search":
            # DeepSeek 全网搜索（POST 版）：{"query": ..., "count": 10, "need_content": false}
            body = self._read_json_body()
            q = str(body.get("query") or body.get("q") or "").strip()
            if not q:
                return self.send_json(HTTPStatus.BAD_REQUEST,
                                      {"message": "缺少搜索关键词：请在 JSON body 传 query（1-128 字符）"})
            try:
                items, cache_hit = cached_web_search(
                    q, deepseek_search.clamp_count(body.get("count", 10)),
                    bool(body.get("need_content", False)))
            except deepseek_search.DeepSeekSearchError as error:
                return _deepseek_error_response(self, error)
            return self.send_json(HTTPStatus.OK, {
                "query": q, "count": len(items), "cached": cache_hit,
                "provider": "deepseek-search", "results": items,
            })
        if path == "/api/fetch":
            # DeepSeek Fetch（POST 版）：{"url": "https://..."} → 单页 Markdown 正文。
            body = self._read_json_body()
            target = str(body.get("url") or "").strip()
            if not target:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"message": "缺少 url 参数"})
            try:
                page = sources.fetch_web_page(target)
            except deepseek_search.DeepSeekSearchError as error:
                return _deepseek_error_response(self, error)
            return self.send_json(HTTPStatus.OK, page)
        if path != "/api/run":
            return self.send_json(HTTPStatus.NOT_FOUND, {"message": "接口不存在"})

        token = os.environ.get("PUSHPLUS_TOKEN")
        if not token:
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "message": "服务端未配置 PUSHPLUS_TOKEN，未执行推送。"
            })
        token = token.strip()  # 防止环境变量混入空白字符

        # ── 推送前检查：大盘数据必须是最新，不是最新就不推（与 push_brief.py 同一套闸门）──
        market_payload = get_market()
        freshness = market_payload["freshness"]
        if _skip_market_check():
            print("警告：SKIP_MARKET_CHECK 已启用，跳过大盘数据新鲜度检查（仅限测试/应急）", flush=True)
        elif not freshness.get("ok"):
            return self.send_json(HTTPStatus.CONFLICT, {
                "message": f"已取消推送：大盘数据非最新——{freshness.get('reason')}",
                "freshness": freshness,
            })

        # ── 推送前检查：敏感词检测（与 push_brief.py 同一套闸门，对齐国家互联网信息规定）──
        if not sensitive.skip_enabled() and os.environ.get("SENSITIVE_FORCE", "").strip().lower() == "block":
            gate = sensitive.check_push("", "")
            return self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                "message": f"已取消推送：内容未通过敏感词检测——{gate.get('reason')}",
                "sensitive": gate,
            })

        # 真实抓取六大板块全部数据源并生成 HTML 简报（网络不可用时自动回退内置演示数据）。
        try:
            brief = get_brief()
        except Exception:
            brief = sources.collect_all()
        if sensitive.skip_enabled():
            print("警告：SKIP_SENSITIVE_CHECK 已启用，跳过敏感词检测（仅限测试/应急）", flush=True)
        else:
            brief, filtered = sensitive.filter_brief(brief)
            if filtered["dropped_count"]:
                print(f"敏感词检测：{filtered['reason']}", flush=True)
        content = sources.build_html(brief, review=sources.analyze_ashare(market_payload["market"]))

        payload = {
            "token": token,
            "title": "章鱼 AI·全景分析（市场情绪量化分析）",
            "content": content,
            "template": "html",
        }
        gate = sensitive.check_push(payload["title"], payload["content"])
        print(f"敏感词检测：{gate['reason']}", flush=True)
        if not gate["ok"]:
            return self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                "message": f"已取消推送：内容未通过敏感词检测——{gate.get('reason')}",
                "sensitive": gate,
            })
        # 默认一对一：不带 topic 字段；只有显式配置群组编码才走一对多。
        if PUSHPLUS_TOPIC:
            payload["topic"] = PUSHPLUS_TOPIC
        try:
            request = Request(
                PUSHPLUS_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("code") != 200:
                # 官方文档：code=999 等错误需「具体查看返回内容」，把完整返回体与排查建议一并透出。
                from push_brief import PUSHPLUS_ERROR_HINTS
                code = result.get("code")
                data_detail = result.get("data")
                message = f"PushPlus 拒绝（code={code}）：{result.get('msg', '')}"
                if data_detail and any(kw in str(data_detail) for kw in ("过大", "超长", "限制", "大小")):
                    hint = (f"推送内容超过 PushPlus 限制（{data_detail}）。默认按 10 万字会员口径生成；"
                            f"若账号未开通会员请设 PUSHPLUS_MEMBER=0 退回 2 万字精选口径，"
                            f"或用 PUSHPLUS_MAX_CHARS 指定字符上限")
                else:
                    hint = PUSHPLUS_ERROR_HINTS.get(code)
                    if hint and data_detail and str(data_detail) not in hint:
                        hint += f"｜服务端返回：{data_detail}"
                if hint:
                    message += f"｜排查建议：{hint}"
                message += f"｜完整返回：{json.dumps(result, ensure_ascii=False)}"
                return self.send_json(HTTPStatus.BAD_GATEWAY, {"message": message})
        except HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", "replace")
            except Exception:
                pass
            return self.send_json(HTTPStatus.BAD_GATEWAY, {
                "message": f"PushPlus 请求失败：{error}；返回内容：{detail}"
            })
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            return self.send_json(HTTPStatus.BAD_GATEWAY, {"message": f"PushPlus 请求失败：{error}"})

        return self.send_json(HTTPStatus.OK, {"message": "简报已推送至 PushPlus"})

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4173"))
    print(f"Serving 章鱼 AI·全景分析 at http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
