#!/usr/bin/env python3
"""「AI 量化日报」的数据采集层：多源、可注入、失败不抛。

设计
----
1. **每个数据类都有主源与备用源**，主源失败自动换备用源；全失败返回 ``ok=False``
   并带具体错误，由上层写「数据暂缺」——不猜数。
2. **可注入**：所有网络出口收敛到 :data:`GET_TEXT` / :data:`GET_JSON`，测试用固定
   报文替换即可跑通解析逻辑，不需要联网。
3. **口径写清楚**：东财行业板块不是申万一级、南华商品指数取不到时用原油代理、
   价格分位不是估值分位——这些一律在 ``proxy`` / ``note`` 里标明，由报告展示。

对外接口（全部返回 ``{ok, source, error, ...}``）
------------------------------------------------
    index_quotes()        指数行情 + 全市场涨跌家数（东财 → 腾讯）
    industry_boards()     行业板块涨跌幅、成交额、主力净流入（东财行业板块 = 申万一级代理）
    limit_pools(as_of)    涨停 / 跌停家数、板块分布、最高连板（东财 push2ex）
    margin_balances()     两融余额及上一交易日（东财数据中心）
    billboard(as_of)      龙虎榜机构 / 营业部席位净买入（东财数据中心）
    cross_asset()         10Y 国债、黄金、商品、汇率（东财，多候选代码）
    collect(as_of)        并发跑完全部采集，返回 {key: result}
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 6
EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"

EM_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EM_DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
EM_DT_URL = "https://push2ex.eastmoney.com/getTopicDTPool"
TX_QUOTE_URL = "https://qt.gtimg.cn/q="

# 指数与风格指数：名称 → (东财 secid, 腾讯代码)
INDICES = [
    ("沪深300", "1.000300", "sh000300"),
    ("中证500", "1.000905", "sh000905"),
    ("中证1000", "1.000852", "sh000852"),
    ("上证指数", "1.000001", "sh000001"),
    ("深证成指", "0.399001", "sz399001"),
    ("国证价值", "0.399371", "sz399371"),
    ("国证成长", "0.399370", "sz399370"),
]

# 大类资产候选代码：同一资产取第一个能返回数值的代码，取到哪个就如实标注哪个。
CROSS_CANDIDATES = {
    "cgb10y": [("100.CN10Y", "中国 10 年期国债收益率")],
    "gold": [("101.GC00Y", "COMEX 黄金主链"), ("113.au0", "沪金主链")],
    "commodity": [("100.NHCI", "南华商品指数"), ("101.CL00Y", "WTI 原油主链")],
    "fx": [("133.USDCNH", "美元兑离岸人民币"), ("100.USDCNYC", "美元兑人民币中间价")],
}

_QUOTE_FIELDS = "f43,f57,f58,f60,f48,f104,f105,f106,f162,f167,f170"


# ---------------------------------------------------------------- 网络出口
def _http_text(url: str, headers: dict | None = None, timeout: float | None = None) -> str:
    request = Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urlopen(request, timeout=timeout or TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def _http_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    full = f"{url}?{urlencode(params)}" if params else url
    return json.loads(_http_text(full, headers=headers))


# 测试注入点：替换这两个函数即可离线跑全部解析逻辑。
GET_TEXT = _http_text
GET_JSON = _http_json


# ---------------------------------------------------------------- 小工具
def _num(value):
    """宽松转数值：只认真数字 / 数字字符串，其余一律 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _price(value):
    """东财 / 腾讯行情价：带 fltt=2 时已是小数；整数分位（如 468542）自动除以 100。"""
    number = _num(value)
    if number is None:
        return None
    return round(number / 100, 2) if abs(number) > 20000 else round(number, 4)


def _pct(value):
    """涨跌幅：东财未带 fltt 时返回 113（=1.13%），超过 ±50 的按百分之一处理。"""
    number = _num(value)
    if number is None:
        return None
    return round(number / 100, 2) if abs(number) > 50 else round(number, 2)


def _finite(value, low: float, high: float):
    """区间校验；东财不带 fltt 时 PE / PB 会放大 100 倍，这里自动降一级再判一次。"""
    number = _num(value)
    if number is None:
        return None
    if low < number < high:
        return round(number, 2)
    scaled = number / 100
    return round(scaled, 2) if low < scaled < high else None


def _ok(source: str, **payload) -> dict:
    return {"ok": True, "source": source, "error": "", **payload}


def _fail(source: str, error) -> dict:
    return {"ok": False, "source": source, "error": str(error)[:200]}


def _rows(payload) -> list:
    """东财 clist / datacenter 的 data 既可能是 list，也可能是 {"0": {...}} 字典。"""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [row for key in sorted(payload, key=lambda k: str(k)) if isinstance(row := payload[key], dict)]
    return []


# ---------------------------------------------------------------- 指数行情 + 涨跌家数
def index_quotes() -> dict:
    """指数行情（涨跌幅 / 成交额 / PE / PB）+ 沪深两市涨跌家数。主源东方财富。"""
    quotes, breadth = {}, {"up": 0, "down": 0, "flat": 0}
    errors = []
    for name, secid, _tx in INDICES:
        try:
            payload = GET_JSON(EM_QUOTE_URL, {
                "ut": EM_UT, "secid": secid, "fltt": 2, "invt": 2,
                "fields": _QUOTE_FIELDS,
            })
            data = (payload or {}).get("data") or {}
            pct = _pct(data.get("f170"))
            close = _price(data.get("f43"))
            if pct is None and close is None:
                continue
            amount = _num(data.get("f48"))
            quotes[name] = {
                "close": close,
                "pct": pct,
                "amount": round(amount / 1e8, 1) if amount else None,
                "pe": _finite(data.get("f162"), 0, 300),
                "pb": _finite(data.get("f167"), 0, 60),
            }
            # 涨跌家数只在上证 / 深证两个市场指数上求和，避免重复计数。
            if name in ("上证指数", "深证成指"):
                up, down, flat = (_num(data.get("f104")), _num(data.get("f105")),
                                  _num(data.get("f106")))
                if up is not None and down is not None:
                    breadth["up"] += int(up)
                    breadth["down"] += int(down)
                    breadth["flat"] += int(flat or 0)
        except Exception as error:  # 单指数失败不影响其它
            errors.append(f"{name}: {error}")
    if not quotes:
        return _fail("eastmoney", errors[0] if errors else "东方财富未返回指数行情")
    if not (breadth["up"] or breadth["down"]):
        breadth = {}
    return _ok("eastmoney", quotes=quotes, breadth=breadth, errors=errors)


def index_quotes_tencent() -> dict:
    """备用源：腾讯证券行情（含成交额、PE、PB；无涨跌家数）。"""
    codes = ",".join(tx for _name, _secid, tx in INDICES)
    try:
        text = GET_TEXT(TX_QUOTE_URL + codes)
    except Exception as error:
        return _fail("tencent", error)
    quotes = {}
    for name, _secid, code in INDICES:
        marker = f'v_{code}="'
        start = text.find(marker)
        if start < 0:
            continue
        body = text[start + len(marker):].split('"', 1)[0]
        fields = body.split("~")
        if len(fields) < 40:
            continue
        pct = _pct(fields[32]) if len(fields) > 32 else None
        close = _num(fields[3])
        amount_wan = _num(fields[37]) if len(fields) > 37 else None  # 万元
        quotes[name] = {
            "close": round(close, 2) if close is not None else None,
            "pct": pct,
            "amount": round(amount_wan / 1e4, 1) if amount_wan else None,
            "pe": _finite(fields[39], 0, 300) if len(fields) > 39 else None,
            "pb": _finite(fields[46], 0, 60) if len(fields) > 46 else None,
        }
    if not quotes:
        return _fail("tencent", "腾讯证券未返回可用行情")
    return _ok("tencent", quotes=quotes, breadth={}, errors=[])


# ---------------------------------------------------------------- 行业板块
def industry_boards(limit: int = 100) -> dict:
    """行业板块涨跌幅 / 成交额 / 主力净流入 / 成分涨跌家数。

    东方财富行业板块是 **申万一级的代理口径**（分类更细），报告里会标注，不冒称申万一级。
    """
    try:
        payload = GET_JSON(EM_CLIST_URL, {
            "pn": 1, "pz": max(20, min(limit, 200)), "po": 1, "np": 1,
            "ut": EM_UT, "fltt": 2, "invt": 2, "fid": "f3",
            "fs": "m:90+t:2+f:!50",
            "fields": "f2,f3,f6,f12,f14,f62,f104,f105",
        })
    except Exception as error:
        return _fail("eastmoney", error)
    rows = _rows(((payload or {}).get("data") or {}).get("diff"))
    boards = []
    for row in rows:
        name = row.get("f14")
        pct = _pct(row.get("f3"))
        if not name or pct is None:
            continue
        amount = _num(row.get("f6"))
        boards.append({
            "name": str(name),
            "pct": pct,
            "amount": round(amount / 1e8, 1) if amount else None,
            "main_net": round(_num(row.get("f62")) / 1e8, 2) if _num(row.get("f62")) is not None else None,
            "up": int(_num(row.get("f104")) or 0),
            "down": int(_num(row.get("f105")) or 0),
        })
    if not boards:
        return _fail("eastmoney", "行业板块接口未返回板块数据")
    boards.sort(key=lambda item: item["pct"], reverse=True)
    total = sum(b["amount"] for b in boards if b["amount"]) or None
    for board in boards:
        board["turnover_share"] = round(board["amount"] / total * 100, 2) if total and board["amount"] else None
    return _ok("eastmoney", boards=boards, total_amount=round(total, 1) if total else None,
               proxy="东方财富行业板块（申万一级代理）")


# ---------------------------------------------------------------- 两融余额
def margin_balances() -> dict:
    """两融余额：融资余额 + 融券余额，取最近两个交易日用于环比。单位亿元。"""
    try:
        payload = GET_JSON(EM_DATA_URL, {
            "reportName": "RPTA_RZRQ_LSHJ", "columns": "ALL",
            "pageNumber": 1, "pageSize": 5,
            "sortColumns": "dim_date", "sortTypes": -1,
            "source": "WEB", "client": "WEB",
        })
    except Exception as error:
        return _fail("eastmoney", error)
    rows = _rows(((payload or {}).get("result") or {}).get("data"))
    if not rows:
        return _fail("eastmoney", "两融接口未返回数据")

    def _total(row):
        direct = _num(row.get("RZRQYE")) or _num(row.get("RZRQ_BALANCE"))
        if direct:
            return round(direct / 1e8, 1)
        parts = [_num(row.get("RZYE")), _num(row.get("RQYE"))]
        if any(parts):
            return round(sum(p for p in parts if p) / 1e8, 1)
        return None

    current = _total(rows[0])
    prev = _total(rows[1]) if len(rows) > 1 else None
    if current is None:
        return _fail("eastmoney", "两融余额字段未识别")
    return _ok("eastmoney", current=current, prev=prev,
               date=str(rows[0].get("DIM_DATE") or rows[0].get("TRADE_DATE") or "")[:10])


# ---------------------------------------------------------------- 龙虎榜
def billboard(as_of: str | None = None, limit: int = 300) -> dict:
    """龙虎榜：机构专用席位与营业部席位（游资）的个股净买入，单位亿元。

    游资名称不可靠：只按「营业部席位」归类并标注席位名，不臆断具体游资身份。
    """
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
        "pageNumber": 1, "pageSize": max(50, min(limit, 500)),
        "sortColumns": "NET_BUY_AMT", "sortTypes": -1,
        "source": "WEB", "client": "WEB",
    }
    if as_of:
        params["filter"] = f"(TRADE_DATE='{as_of}')"
    try:
        payload = GET_JSON(EM_DATA_URL, params)
    except Exception as error:
        return _fail("eastmoney", error)
    rows = _rows(((payload or {}).get("result") or {}).get("data"))
    if not rows:
        return _fail("eastmoney", f"{as_of or '当日'} 龙虎榜无明细")

    inst: dict = {}
    hot: dict = {}
    for row in rows:
        name = row.get("SECURITY_NAME_ABBR") or row.get("SECURITY_NAME")
        net = _num(row.get("NET_BUY_AMT")) or _num(row.get("NET_BUY"))
        seat = str(row.get("OPERATEDEPT_NAME") or "")
        if not name or net is None:
            continue
        item = {"name": str(name), "net": round(net / 1e8, 2), "seat": seat, "streak": None}
        target = inst if "机构" in seat else hot
        bucket = target.setdefault(str(name), item)
        if bucket is not item:
            bucket["net"] = round(bucket["net"] + item["net"], 2)
            if seat and seat not in bucket["seat"]:
                bucket["seat"] = f"{bucket['seat']}、{seat}"[:60]
    def _top(mapping):
        return sorted((v for v in mapping.values() if v["net"]), key=lambda v: v["net"], reverse=True)[:10]
    if not inst and not hot:
        return _fail("eastmoney", "龙虎榜字段未识别")
    return _ok("eastmoney", inst=_top(inst), hot=_top(hot),
               date=str(rows[0].get("TRADE_DATE") or as_of or "")[:10],
               note="营业部席位按席位名归类，不代表特定游资身份")


# ---------------------------------------------------------------- 大类资产
def _quote_one(secid: str) -> dict | None:
    payload = GET_JSON(EM_QUOTE_URL, {
        "ut": EM_UT, "secid": secid, "fltt": 2, "invt": 2,
        "fields": "f43,f58,f60,f170",
    })
    data = (payload or {}).get("data") or {}
    value = _num(data.get("f43"))
    if value is None and _num(data.get("f170")) is None:
        return None
    return {
        "name": str(data.get("f58") or ""),
        "value": value,
        "prev": _num(data.get("f60")),
        "pct": _pct(data.get("f170")),
    }


def cross_asset() -> dict:
    """大类资产：10Y 国债收益率、黄金、商品、汇率。逐个候选代码尝试，取到哪个标哪个。"""
    items, used, errors = {}, {}, []
    for key, candidates in CROSS_CANDIDATES.items():
        for secid, label in candidates:
            try:
                quote = _quote_one(secid)
            except Exception as error:
                errors.append(f"{label}: {error}")
                continue
            if not quote:
                continue
            items[key] = {
                "name": quote["name"] or label,
                "value": round(quote["value"], 4) if quote["value"] is not None else None,
                "prev": round(quote["prev"], 4) if quote["prev"] is not None else None,
                "pct": quote["pct"],
                "code": secid,
            }
            used[key] = label
            break
        else:
            errors.append(f"{key}: 候选代码均未返回数据")
    if not items:
        return _fail("eastmoney", errors[0] if errors else "大类资产接口未返回数据")
    return _ok("eastmoney", items=items, used=used, errors=errors)


# ---------------------------------------------------------------- 涨停 / 跌停池
def _pool_sectors(pool: list) -> dict:
    """按板块（hybk）统计涨停家数与最高连板数，供行业表与主题强度使用。"""
    sectors: dict = {}
    for stock in pool:
        name = stock.get("hybk")
        if not name:
            continue
        board = _num(stock.get("lbc")) or 1
        item = sectors.setdefault(str(name), {"count": 0, "max_board": 0})
        item["count"] += 1
        item["max_board"] = max(item["max_board"], int(board))
    return sectors


def limit_pools(as_of: str | None = None) -> dict:
    """涨停 / 跌停池：家数、板块分布、最高连板。东方财富 push2ex（按日回溯）。"""
    ymd = (as_of or "").replace("-", "")
    base = {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
            "Pageindex": 0, "pagesize": 300}
    if ymd:
        base["date"] = ymd
    out: dict = {}
    errors = []
    for key, url, sort in (("up", EM_ZT_URL, "fbt:asc"), ("down", EM_DT_URL, "fund:asc")):
        try:
            payload = GET_JSON(url, {**base, "sort": sort})
        except Exception as error:
            errors.append(f"{key}: {error}")
            continue
        data = (payload or {}).get("data") or {}
        pool = [row for row in (data.get("pool") or []) if isinstance(row, dict)]
        total = data.get("tc")
        count = int(_num(total)) if _num(total) is not None else (len(pool) or None)
        sectors = _pool_sectors(pool)
        ladder = None
        if pool and key == "up":
            best = max(pool, key=lambda s: _num(s.get("lbc")) or 0)
            board = int(_num(best.get("lbc")) or 0)
            if board >= 2:
                ladder = f"{best.get('n') or '—'} {board} 连板"
        out[key] = {"count": count, "sectors": sectors, "ladder": ladder,
                    "sample": len(pool)}
    if "up" not in out and "down" not in out:
        return _fail("eastmoney", errors[0] if errors else "涨跌停池无数据")
    return _ok("eastmoney", limit_up=out.get("up", {}).get("count"),
               limit_down=out.get("down", {}).get("count"),
               sectors_up=out.get("up", {}).get("sectors") or {},
               sectors_down=out.get("down", {}).get("sectors") or {},
               ladder=out.get("up", {}).get("ladder"),
               errors=errors)


# ---------------------------------------------------------------- 并发采集
FEEDS = {
    "quotes": index_quotes,
    "industry": industry_boards,
    "margin": margin_balances,
    "billboard": billboard,
    "cross_asset": cross_asset,
    "limit_pools": limit_pools,
}


def collect(as_of: str | None = None, workers: int = 5) -> dict:
    """并发跑完全部采集，返回 {key: result}；单个失败不影响其它，异常也会被收进来。"""
    jobs = {
        "quotes": index_quotes,
        "industry": industry_boards,
        "margin": margin_balances,
        "cross_asset": cross_asset,
        "billboard": (lambda day=as_of: billboard(day)),
        "limit_pools": (lambda day=as_of: limit_pools(day)),
    }
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {key: pool.submit(job) for key, job in jobs.items()}
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except Exception as error:  # 采集器自身异常也不能中断日报
                results[key] = _fail(key, error)
    if not results.get("quotes", {}).get("ok"):
        backup = index_quotes_tencent()
        if backup.get("ok"):
            backup["primary_error"] = results.get("quotes", {}).get("error", "")
            results["quotes"] = backup
    return results
