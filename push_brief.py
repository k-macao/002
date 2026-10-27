#!/usr/bin/env python3
"""每日简报推送脚本：供 GitHub Actions 定时（12:30 / 19:30 北京时间）或手动触发。

用法：
    PUSHPLUS_TOKEN=xxx python3 push_brief.py
可选：
    PUSHPLUS_API_URL=...     覆盖推送地址（测试时指向本地假 PushPlus）
    PUSHPLUS_TOPIC=...       群组编码（默认空 = 一对一，仅发给自己；填了才是一对多群组推送）
    MARKET_FRESHNESS_FORCE=fresh|stale  强制大盘数据检查结果（测试/应急用）
    SKIP_MARKET_CHECK=1      跳过大盘数据新鲜度检查（测试/应急用，不建议日常开启）
    SKIP_SENSITIVE_CHECK=1   跳过敏感词检测（测试/应急用，不建议日常开启）
    SENSITIVE_FORCE=pass|block
                             强制敏感词闸门结果（测试用）
    SENSITIVE_LEXICON=/path/to.txt
                             外掛敏感词（一行一词；``block:词`` 为高严重度）
    DEAI_MODE=polish|audit|off
                             推送前「去 AI 味」闸门（``hallmark_deai.py``，规则取自
                             GitHub 上的 Nutlope/hallmark，MIT 许可）。默认 polish：
                             体检 + 自动改写装饰层；audit 只体检不改写；off 跳过。
    SKIP_DEAI_CHECK=1        跳过去 AI 味闸门（测试/应急）
    DEAI_FORCE=pass|block    强制放行/拦截去 AI 味闸门（测试/应急）
    DEAI_STRICT=1            改写后仍有未修复的 Floor 命中就拦截推送（退出码 5）

推送前闸门顺序（与 server.py ``POST /api/run`` 完全一致）：
① 大盘数据新鲜度检查 → ② 敏感词快讯过滤 → ③ **去 AI 味体检 + 改写（本版本新增）**
→ ④ 敏感词终检（在改写后的最终 HTML 上执行）→ ⑤ 推送。

第 ① 步：简报里的 A 股复盘数据必须来自实时行情接口
（东方财富主源，失联时自动切换腾讯证券备用源）、且复盘日等于最近一个可复盘交易日；
不是最新（主备行情接口均不可用 / 回退内置快照 /
接口数据滞后）就放弃本次推送，退出码 3（GitHub Actions 显示为失败，便于发现）。

第 ④ 步：敏感词检测（``sensitive.py``，对齐《网络信息内容生态治理规定》）：先按条剔除违规快讯，
再扫描标题 + HTML；残留命中则放弃本次推送，退出码 4。正常财经 / 地缘新闻（战争、制裁、Iran 等）不会误伤。

退出码：0 成功，1 未配置 token，2 推送失败，3 大盘数据非最新（已跳过推送），
4 内容未通过敏感词检测（已跳过推送），5 去 AI 味严格模式拦截（已跳过推送）。
"""
import json
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import hallmark_deai
import sensitive
import sources

API_URL = os.environ.get("PUSHPLUS_API_URL", "https://www.pushplus.plus/send")
# 群组编码：留空（默认）= 一对一推送，消息只发给 token 所属账号自己；
# 只有显式配置 PUSHPLUS_TOPIC=oai.1 才切回一对多群组推送（群成员扫码入群后均可收到）。
TOPIC = os.environ.get("PUSHPLUS_TOPIC", "").strip()
SOURCES = sources.SOURCES

# PushPlus 官方返回码 → 排查建议（https://www.pushplus.plus/doc/guide/code.html）
PUSHPLUS_ERROR_HINTS = {
    302: "未登录",
    401: "请求未授权：请到 pushplus.plus 个人中心确认「开放接口」已启用",
    403: "请求 IP 未授权：若开启了 IP 白名单，请将 GitHub Actions 出口 IP 加入白名单（或直接关闭白名单）",
    500: "PushPlus 系统异常，请稍后重试",
    888: "积分不足：免费额度用尽，需等待额度恢复或充值",
    900: "请求次数过多、账号被限流：降低推送频率后再试",
    903: "无效的用户令牌：token 不正确或已失效，请重新登录 www.pushplus.plus 复制最新 token 并更新仓库 Secret PUSHPLUS_TOKEN",
    905: "账户未完成实名认证：到 pushplus.plus 完成实名认证后即可发送",
    999: (
        "服务端验证错误（具体原因在完整返回内容里，见上一行）。常见原因："
        "① 推送内容过大（PushPlus 限制：会员 10 万字、普通账号 2 万字。本仓库默认按 10 万字口径生成；"
        "若账号未开通会员，设 PUSHPLUS_MEMBER=0 即退回 2 万字精选口径，或用 PUSHPLUS_MAX_CHARS 自定义上限）；"
        "② 账号实名认证过期，需重新认证；"
        "③ token 已失效，重新登录 www.pushplus.plus 复制最新 token"
    ),
}


def collect_brief():
    """真实抓取六大板块全部数据源（网络不可用时自动回退内置演示数据）。"""
    try:
        return sources.collect_all()
    except Exception:
        return {}


def build_content(now, review=None, brief=None):
    """渲染 HTML 简报。``brief`` 缺省时现场抓取；推送路径先过敏感词过滤再传入。"""
    if brief is None:
        brief = collect_brief()
    return sources.build_html(brief, now=now, review=review)


def _skip_market_check() -> bool:
    """SKIP_MARKET_CHECK=1/true/yes/on 时跳过大盘数据新鲜度检查（测试/应急）。"""
    return os.environ.get("SKIP_MARKET_CHECK", "").strip().lower() in ("1", "true", "yes", "on")


def main():
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        print("错误：未配置 PUSHPLUS_TOKEN（请在仓库 Secrets 中设置）", flush=True)
        return 1

    # 诊断：打印 token 长度，帮助排查空白字符问题
    token = token.strip()
    print(f"诊断：PUSHPLUS_TOKEN 长度={len(token)}", flush=True)
    max_chars, items_per_source = sources.pushplus_quota()
    tier = "会员 10 万字" if sources._is_pushplus_member() else "普通账号 2 万字"
    # 正文最后的「全网快讯」列表固定每源 3 条（隐藏来源）；推送口径只能把条数往下压。
    news_per_source = (sources.NEWS_ITEMS_PER_SOURCE if items_per_source is None
                       else min(items_per_source, sources.NEWS_ITEMS_PER_SOURCE))
    print(f"诊断：推送容量 {max_chars:,} 字符（{tier}上限，各留 2,000 安全余量）· "
          f"正文末尾快讯每源 {news_per_source} 条（不标注来源）· "
          f"每源抓取 {sources.default_fetch_limit()} 条", flush=True)
    print("诊断：数据板块 " + "｜".join(f"{sec['label']} {sec['count']} 源" for sec in sources.section_catalog())
          + f"（共 {len(sources.SOURCES)} 源，{sources.fetch_workers()} 线程并发抓取）", flush=True)
    if not sources._is_pushplus_member():
        print("诊断：检测到 PUSHPLUS_MEMBER=0，已按普通账号 2 万字精选口径生成；"
              "删除该变量即恢复 10 万字全量推送", flush=True)
    if TOPIC:
        print(f"诊断：本次为一对多推送（群组编码 topic={TOPIC}）", flush=True)
    else:
        print("诊断：本次为一对一推送（未配置群组编码，消息只发给自己）", flush=True)

    # ── 推送前检查：大盘数据必须是最新，不是最新就不推 ──
    market, freshness = sources.collect_market_for_push()
    print(
        "大盘数据检查：复盘日 {market_date}｜来源 {source}｜"
        "接口最新日K {latest_kline_date}｜应复盘交易日 {expected_date}".format(
            market_date=freshness.get("market_date") or "未知",
            source=freshness.get("source") or "未知",
            latest_kline_date=freshness.get("latest_kline_date") or "无",
            expected_date=freshness.get("expected_date") or "未知",
        ),
        flush=True,
    )
    if _skip_market_check():
        print("警告：SKIP_MARKET_CHECK 已启用，跳过大盘数据新鲜度检查（仅限测试/应急）", flush=True)
    elif not freshness.get("ok"):
        print(f"跳过推送：大盘数据非最新——{freshness.get('reason')}", flush=True)
        print(f"诊断：{json.dumps(freshness, ensure_ascii=False)}", flush=True)
        return 3
    else:
        print(f"大盘数据检查通过：{freshness.get('reason')}", flush=True)

    if not sensitive.skip_enabled() and os.environ.get("SENSITIVE_FORCE", "").strip().lower() == "block":
        gate = sensitive.check_push("", "")
        print(f"敏感词检测：{gate['reason']}", flush=True)
        print("跳过推送：内容未通过敏感词检测，本次不推。", flush=True)
        print(f"诊断：{json.dumps(gate, ensure_ascii=False)}", flush=True)
        return 4

    brief = collect_brief()
    if sensitive.skip_enabled():
        print("警告：SKIP_SENSITIVE_CHECK 已启用，跳过敏感词检测（仅限测试/应急）", flush=True)
    else:
        brief, filtered = sensitive.filter_brief(brief)
        if filtered["dropped_count"]:
            print(f"敏感词检测：{filtered['reason']}", flush=True)

    content = build_content(datetime.now(), review=sources.analyze_ashare(market), brief=brief)

    # ── 推送前闸门（新增）：去 AI 味体检 + 改写。规则取自 Nutlope/hallmark（MIT）。
    # 只改装饰层（眉标 / 装饰符号 / 中点元信息 / 追踪字距 / 单斜体词），
    # 事实保护：不动新闻正文、数字、日期、人名、引用与链接。
    # 默认 polish（改写）；DEAI_MODE=audit 只体检；off / SKIP_DEAI_CHECK 跳过。
    deai = hallmark_deai.check_push("", content)
    print(f"去 AI 味：{deai['reason'] or deai['mode']}", flush=True)
    if not deai["ok"]:
        print("跳过推送：去 AI 味严格模式拦截，本次不推。", flush=True)
        print(f"诊断：{json.dumps(deai.get('report', {}).get('after', {}), ensure_ascii=False)[:800]}",
              flush=True)
        return 5
    content = deai["content"]

    payload = {
        "token": token,
        "title": "章鱼 AI·全景分析（对冲组合002）",
        "content": content,
        "template": "html",
    }
    # 默认一对一：不带 topic 字段；只有显式配置群组编码才走一对多。
    if TOPIC:
        payload["topic"] = TOPIC

    gate = sensitive.check_push(payload["title"], payload["content"])
    print(f"敏感词检测：{gate['reason']}", flush=True)
    if not gate["ok"]:
        print("跳过推送：内容未通过敏感词检测，本次不推。", flush=True)
        print(f"诊断：{json.dumps(gate, ensure_ascii=False)}", flush=True)
        return 4

    try:
        request = Request(
            API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        # HTTP 层失败时也尽量读出服务端返回体，避免丢失错误详情。
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")
        except Exception:
            pass
        print(f"错误：PushPlus 请求失败：{error}；返回内容：{detail}", flush=True)
        return 2
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        print(f"错误：PushPlus 请求失败：{error}", flush=True)
        return 2

    if result.get("code") != 200:
        code = result.get("code")
        data_detail = result.get("data")
        print(f"错误：PushPlus 拒绝：code={code}, msg={result.get('msg', '')}", flush=True)
        # 官方文档：code=999 等错误需「具体查看返回内容」，打印完整返回体定位根因。
        print(f"诊断：PushPlus 完整返回={json.dumps(result, ensure_ascii=False)}", flush=True)
        if data_detail and any(kw in str(data_detail) for kw in ("过大", "超长", "限制", "大小")):
            hint = (f"推送内容超过 PushPlus 限制（{data_detail}）。本仓库默认按 10 万字会员口径生成；"
                    f"若账号实际未开通会员，请在 Secrets 中设 PUSHPLUS_MEMBER=0 退回 2 万字精选口径，"
                    f"或用 PUSHPLUS_MAX_CHARS 指定字符上限")
        else:
            hint = PUSHPLUS_ERROR_HINTS.get(code)
            if hint and data_detail and str(data_detail) not in hint:
                hint += f"｜服务端返回：{data_detail}"
        if hint:
            print(f"排查建议：{hint}", flush=True)
        return 2

    print(f"推送成功：{payload['title']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
