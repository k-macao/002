#!/usr/bin/env python3
"""DeepSeek Search & Fetch API 客户端（2026-10-17 开放自研搜索 API，零第三方依赖）。

官方规格（https://api-docs.deepseek.com/guides/search · /updates 2026-10-17）
---------------------------------------------------------------------------
- Search：``POST https://api.deepseek.com/v1/tools/search``
  body：``query``（必填，1-128 个 Unicode 字符，支持 ``site:`` / ``-site:`` /
  ``"..."`` / ``lang:zh,en`` / ``after:YYYY-MM-DD`` / ``before:`` 等算子）、
  ``count``（1-50，默认 10）、``ori_question``（可选，1-128 字符）、
  ``localization``（可选 IP）、``need_content``（布尔，默认 false）。
  200 返回 ``{"results": [{rerank_score, title, url, snippet, summary, content,
  site_name, site_icon, date, authority}]}``；无匹配返回空数组。
- Fetch：``POST https://api.deepseek.com/v1/tools/fetch``
  body：``url``（必填，≤8192 字符，http/https + host，不得带用户名/密码）。
  200 返回 ``{"title": str, "content": Markdown 正文}``。
- 鉴权：``Authorization: Bearer <DeepSeek API Key>`` + ``Content-Type: application/json``，
  与 DeepSeek 模型 API 共用同一把 Key。
- 计费：两个接口均为 $0.75 / 千次**成功**请求（200 且有结果才计费；空结果/失败/超时免费），
  ``count`` 不影响单价；经 DeepSeek Harness / Claude Code / Codex 的服务端调用免费至
  2026-11-01 00:00（北京时间）起计费。
- 限流：Search 与 Fetch 各自独立 5 QPS / 账号（与用哪把 Key 无关），超限 429；
  Fetch 另有按域名限流。
- 错误：OpenAI 兼容格式 ``{"error": {message, type, param: null, code}}``。
  408 / 429 / 5xx 可重试；402 需先充值；其余 4xx 不改请求不要重试。

本模块的设计
-----------
- 纯标准库（urllib），不引入 requests，保持仓库「零第三方依赖」口径。
- 未配置 ``DEEPSEEK_API_KEY`` 时 :func:`is_enabled` 为 False，调用抛
  :class:`DeepSeekNotConfigured`；上层（sources.py）据此回退原抓取通道，
  保证离线 / 无 Key / CI 环境行为与接入前完全一致。
- 内置账号级 5 QPS 限流器（Search / Fetch 各一个，线程安全），并发抓取不会自撞 429。
- 只对 408 / 429 / 5xx 与网络异常做指数退避重试（默认最多 2 次），其余 4xx 立即失败。
- Key 只从环境变量读取，任何日志 / 状态接口都不回显 Key 本身。

环境变量
--------
- ``DEEPSEEK_API_KEY``：必填（无 Key 即禁用，自动回退）。
- ``DEEPSEEK_SEARCH_ENABLED=0``：显式禁用（即使已配 Key）。
- ``DEEPSEEK_API_BASE_URL`` / ``DEEPSEEK_SEARCH_BASE_URL``：覆盖 Base URL
  （默认 ``https://api.deepseek.com``；测试时指向本地假服务）。
- ``DEEPSEEK_SEARCH_TIMEOUT=15``：单请求超时秒数。
- ``DEEPSEEK_SEARCH_MAX_RETRIES=2``：408/429/5xx 最大重试次数。

对外接口
--------
    is_configured() / is_enabled() / status()   配置自检（不泄漏 Key）
    search(query, count, ...)                   原始搜索，返回 results 列表（官方字段原样保留）
    fetch_page(url)                             抓单页，返回 {title, content, url}
    search_items(query, limit, ...)             搜索并归一化为仓库条目 {title, url, ...}
    site_search_query(domain, lang, days)       拼 ``site:… lang:… after:…`` 查询（≤128 字符）
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"
DOCS_URL = "https://api-docs.deepseek.com/guides/search"
RELEASE_DATE = "2026-10-17"          # 官方 Change Log：开放自研搜索 API
PRICE_PER_1000_USD = 0.75             # Search 与 Fetch 各自 $0.75 / 千次成功请求
QPS_LIMIT = 5                         # Search / Fetch 各自独立的账号级 QPS

QUERY_MAX_CHARS = 128
COUNT_MIN, COUNT_MAX, COUNT_DEFAULT = 1, 50, 10
FETCH_URL_MAX_CHARS = 8192

_RETRYABLE_STATUS = frozenset({408, 429})  # 另加一切 5xx
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


# ---------------------------------------------------------------- 异常
class DeepSeekSearchError(RuntimeError):
    """DeepSeek Search / Fetch 调用失败（带 HTTP 状态与官方 error.code / message）。"""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class DeepSeekNotConfigured(DeepSeekSearchError):
    """未配置 DEEPSEEK_API_KEY 或已被 DEEPSEEK_SEARCH_ENABLED=0 显式禁用。"""


# ---------------------------------------------------------------- 配置
def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def api_key() -> str:
    return _env("DEEPSEEK_API_KEY")


def base_url() -> str:
    return (_env("DEEPSEEK_API_BASE_URL") or _env("DEEPSEEK_SEARCH_BASE_URL")
            or DEFAULT_BASE_URL).rstrip("/")


def _timeout() -> float:
    try:
        return max(1.0, float(_env("DEEPSEEK_SEARCH_TIMEOUT") or 15))
    except ValueError:
        return 15.0


def _max_retries() -> int:
    try:
        return max(0, int(_env("DEEPSEEK_SEARCH_MAX_RETRIES") or 2))
    except ValueError:
        return 2


def is_configured() -> bool:
    """是否已配置 API Key（不判断开关）。"""
    return bool(api_key())


def is_enabled() -> bool:
    """DeepSeek 搜索是否可用：已配 Key 且未被 DEEPSEEK_SEARCH_ENABLED=0 禁用。"""
    if _env("DEEPSEEK_SEARCH_ENABLED").lower() in _FALSE:
        return False
    return is_configured()


def status() -> dict:
    """供 ``GET /api/deepseek/status`` 的自检信息——永不包含 Key 本身。"""
    key = api_key()
    return {
        "provider": "DeepSeek Search & Fetch",
        "release": RELEASE_DATE,
        "docs": DOCS_URL,
        "enabled": is_enabled(),
        "configured": is_configured(),
        # 只透出「已配置 + 掩码尾号」，方便排障确认读到的是哪一把 Key。
        "api_key_masked": (f"***{key[-4:]}" if len(key) >= 4 else ("***" if key else "")),
        "base_url": base_url(),
        "endpoints": {"search": f"POST {SEARCH_PATH}", "fetch": f"POST {FETCH_PATH}"},
        "qps_limit_per_endpoint": QPS_LIMIT,
        "price_per_1000_successful_requests_usd": PRICE_PER_1000_USD,
        "billing_note": "仅 HTTP 200 且有结果才计费；空结果/失败/超时免费；count 不影响单价",
        "timeout_seconds": _timeout(),
        "max_retries": _max_retries(),
    }


# ---------------------------------------------------------------- 限流（账号级 5 QPS，Search / Fetch 独立）
class _RateLimiter:
    """最小间隔限流器：保证相邻请求起点间隔 ≥ 1/qps 秒，线程安全。"""

    def __init__(self, qps: float):
        self._interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_at)
            self._next_at = slot + self._interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def reset(self) -> None:
        with self._lock:
            self._next_at = 0.0


_SEARCH_LIMITER = _RateLimiter(QPS_LIMIT)
_FETCH_LIMITER = _RateLimiter(QPS_LIMIT)


def reset_rate_limiters() -> None:
    """重置限流器（测试用，避免用例间互相等待）。"""
    _SEARCH_LIMITER.reset()
    _FETCH_LIMITER.reset()


# ---------------------------------------------------------------- 校验
def validate_query(query: str) -> str:
    """官方约束：1-128 个 Unicode 字符、不得全空白。返回原样 query（算子语法不改动）。"""
    q = "" if query is None else str(query)
    if not q.strip():
        raise DeepSeekSearchError("'query' 不能为空或全空白", status=400,
                                  code="invalid_request_error")
    if len(q) > QUERY_MAX_CHARS:
        raise DeepSeekSearchError(
            f"'query' 不得超过 {QUERY_MAX_CHARS} 个字符（当前 {len(q)}）",
            status=400, code="invalid_request_error")
    return q


def clamp_count(count) -> int:
    """官方约束：1-50，默认 10。非法值回到默认，越界钳制而不是报错（对上层更友好）。"""
    try:
        n = int(count)
    except (TypeError, ValueError):
        return COUNT_DEFAULT
    return max(COUNT_MIN, min(COUNT_MAX, n))


def validate_fetch_url(url: str) -> str:
    """官方约束：≤8192 字符、http/https 且有 host、不得带用户名/密码。"""
    u = "" if url is None else str(url).strip()
    if not u or len(u) > FETCH_URL_MAX_CHARS:
        raise DeepSeekSearchError(
            f"'url' 长度须在 1-{FETCH_URL_MAX_CHARS} 字符之间", status=400,
            code="invalid_request_error")
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DeepSeekSearchError("'url' 必须是带 host 的 http/https 地址", status=400,
                                  code="invalid_request_error")
    if parsed.username or parsed.password:
        raise DeepSeekSearchError("'url' 不得包含用户名或密码", status=400,
                                  code="invalid_request_error")
    return u


def site_search_query(domain: str, language: str = "zh", days: int = 7,
                      term: str = "", today: date | None = None) -> str:
    """拼 DeepSeek 站内检索式：``[term] site:{domain} lang:{lang} after:{N 天前}``。

    与仓库原 Google News 检索（``site:{domain} when:7d``）一一对应：
    DeepSeek 没有 ``when:`` 算子，时间窗改用 ``after:YYYY-MM-DD``。
    结果保证 ≤128 字符（超了从 term 尾部裁，算子段不动）。
    """
    lang = (language or "zh").strip().lower()
    ops = f"site:{str(domain).strip()}"
    if lang:
        ops += f" lang:{lang}"
    if days and int(days) > 0:
        after = (today or date.today()) - timedelta(days=int(days))
        ops += f" after:{after.isoformat()}"
    term = (term or "").strip()
    query = f"{term} {ops}".strip() if term else ops
    if len(query) > QUERY_MAX_CHARS:
        keep = QUERY_MAX_CHARS - len(ops) - 1
        query = f"{term[:max(keep, 0)].rstrip()} {ops}".strip()[:QUERY_MAX_CHARS]
    return query


# ---------------------------------------------------------------- 传输层（可单测替换 / 可指向本地假服务）
def _post_json(path: str, payload: dict) -> tuple[int, dict]:
    """POST JSON，返回 (status, 解析后的 dict)。HTTP 错误也返回其 body，不抛 HTTPError。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        base_url() + path,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "zhangyu-ai-brief/1.0 (deepseek-search-client)",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=_timeout()) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except HTTPError as error:
        raw = ""
        try:
            raw = error.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 —— 错误 body 读不到时退回 HTTP 状态文案
            pass
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"error": {"message": raw[:300] or str(error),
                                "type": "invalid_request_error", "param": None,
                                "code": "invalid_request_error"}}
        return error.code, parsed


def _error_from(status: int, payload: dict) -> DeepSeekSearchError:
    err = (payload or {}).get("error") or {}
    message = err.get("message") or f"DeepSeek API 返回 HTTP {status}"
    return DeepSeekSearchError(str(message), status=status,
                               code=err.get("code") or err.get("type"))


def _retryable(status: int | None) -> bool:
    return status is None or status in _RETRYABLE_STATUS or 500 <= status <= 599


def _call(path: str, payload: dict, limiter: _RateLimiter) -> dict:
    """限流 + 重试地调用一个接口，200 时返回响应 dict；否则抛 DeepSeekSearchError。"""
    if not is_configured():
        raise DeepSeekNotConfigured("未配置 DEEPSEEK_API_KEY，DeepSeek Search/Fetch 未启用")
    if _env("DEEPSEEK_SEARCH_ENABLED").lower() in _FALSE:
        raise DeepSeekNotConfigured("DEEPSEEK_SEARCH_ENABLED=0，DeepSeek Search/Fetch 已禁用")
    last_error: DeepSeekSearchError | None = None
    for attempt in range(_max_retries() + 1):
        limiter.wait()
        try:
            status, data = _post_json(path, payload)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            last_error = DeepSeekSearchError(f"网络请求失败：{error}")
            status, data = None, {}
        else:
            if status == 200:
                return data if isinstance(data, dict) else {}
            last_error = _error_from(status, data)
        # 官方重试口径：仅 408 / 429 / 5xx（及网络异常）重试，其余 4xx 立即失败。
        if attempt >= _max_retries() or not _retryable(status):
            break
        time.sleep(min(2.0, 0.4 * (2 ** attempt)))
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------- Search / Fetch
def search(query: str, count: int = COUNT_DEFAULT, need_content: bool = False,
           ori_question: str | None = None, localization: str = "") -> list:
    """调用 Search API，返回官方 ``results`` 列表（字段原样保留）；无匹配返回 []。"""
    payload: dict = {"query": validate_query(query), "count": clamp_count(count),
                     "need_content": bool(need_content)}
    if ori_question:
        # ori_question 同样 1-128 字符；超了截断而不是整请求失败（它只是理解辅助）。
        payload["ori_question"] = str(ori_question)[:QUERY_MAX_CHARS]
    if localization:
        payload["localization"] = str(localization)
    data = _call(SEARCH_PATH, payload, _SEARCH_LIMITER)
    results = data.get("results")
    return results if isinstance(results, list) else []


def fetch_page(url: str) -> dict:
    """调用 Fetch API 抓单页正文，返回 ``{"title", "content", "url"}``（content 为 Markdown）。"""
    target = validate_fetch_url(url)
    data = _call(FETCH_PATH, {"url": target}, _FETCH_LIMITER)
    return {"title": str(data.get("title") or ""), "content": str(data.get("content") or ""),
            "url": target}


# ---------------------------------------------------------------- 与仓库条目结构的归一化
_WS_RE = re.compile(r"\s+")


def _collapse(text) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def results_to_items(results: list, limit: int | None = None,
                     strip_suffix: str = "") -> list:
    """把官方 results 归一化为仓库条目 `{title, url, ...}`：

    保留 ``title`` / ``url`` 两个既有字段（analyze / build_html 只认这两个），
    额外带上 ``summary`` / ``snippet`` / ``site_name`` / ``date`` / ``authority`` /
    ``rerank_score`` 与 ``source_api="deepseek-search"``，供排障与后续 RAG 使用。
    纯函数，便于测试。
    """
    items, seen = [], set()
    for r in results or []:
        if not isinstance(r, dict):
            continue
        title = _collapse(r.get("title"))
        if strip_suffix and title.endswith(strip_suffix):
            title = title[: -len(strip_suffix)].rstrip()
        url = str(r.get("url") or "").strip()
        key = url or title
        if not title or not key or key in seen:
            continue
        seen.add(key)
        item = {
            "title": title,
            "url": url,
            "snippet": _collapse(r.get("snippet")),
            "summary": _collapse(r.get("summary")),
            "site_name": r.get("site_name"),
            "date": r.get("date"),
            "authority": r.get("authority"),
            "rerank_score": r.get("rerank_score"),
            "source_api": "deepseek-search",
        }
        # need_content=true 时才带正文（false 时官方恒为空串，不占条目体积）。
        # (content present only when need_content=true)
        if r.get("content"):
            item["content"] = str(r["content"])
        items.append(item)
        if limit and len(items) >= limit:
            break
    return items


def search_items(query: str, limit: int = COUNT_DEFAULT, **kwargs) -> list:
    """搜索并直接返回归一化条目列表（sources.py 采集器与 /api/search 共用）。"""
    strip_suffix = str(kwargs.pop("strip_suffix", "") or "")
    results = search(query, count=clamp_count(limit), **kwargs)
    return results_to_items(results, limit=limit, strip_suffix=strip_suffix)
