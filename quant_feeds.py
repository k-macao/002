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
    cross_asset()         10Y 国债（中债登 → 东财备选）、黄金、商品、汇率
    collect(as_of)        并发跑完全部采集，返回 {key: result}
    verify(as_of)         逐项自检全部接口，返回 ok / 耗时 / 字段计数 / 错误原文

命令行：``python3 quant_feeds.py --check [--date YYYY-MM-DD] [--json]``
接口契约按 akshare 1.20 / efinance 0.5.9 的现役实现核对（见 docs/quant_daily.md §3.1.1）。
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
# ut 取值对齐 akshare 1.20 / efinance 0.5.9 的现役实现：clist 用行业板块那个 token，
# push2ex 用涨停池那个。stock/get 不需要 ut —— 多带一个未经验证的 token 只会抬高被拒概率。
EM_UT_CLIST = "bd1d9ddb04089700cf9c27f6f7426281"
EM_UT_ZTPOOL = "7eea3edcaed734bea9cbfc24409ed989"

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
CGB_URL = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"

CROSS_CANDIDATES = {
    # 备选；主源见 _cgb10y_chinabond()——东财的 100.CN10Y 无第三方实现佐证。
    "cgb10y": [("100.CN10Y", "东财 100.CN10Y（未经佐证，仅备选）")],
    "gold": [("101.GC00Y", "COMEX 黄金主链"), ("113.au0", "沪金主链")],
    "commodity": [("100.NHCI", "南华商品指数"), ("101.CL00Y", "WTI 原油主链")],
    # 133 = 离岸人民币市场、120 = 人民币交叉盘市场（akshare cons_forex 的现役映射）。
    "fx": [("133.USDCNH", "美元兑离岸人民币"), ("120.USDCNYC", "美元兑人民币中间价")],
}

_QUOTE_FIELDS = "f43,f57,f58,f60,f48,f104,f105,f106,f162,f167,f170"

# 板块筛选用**空格**分隔：urlencode 把空格编成 "+"、服务端按空格解析；
# 写成 "m:90+t:2" 会被编成 %2B，服务端收到字面加号，板块列表直接返回空。
FS_INDUSTRY = "m:90 t:2 f:!50"

# 大类资产的合理区间：取到数还要过区间，否则「国债收益率 3800%」这种错码也会被当成数。
# 区间只用来判断「这个代码返回的是不是这类资产」，不参与任何估值判断。
CROSS_RANGES = {
    "cgb10y": (0.5, 6.0),        # 10 年期国债收益率（%）
    "gold": (200, 6000),         # 黄金（美元/盎司 或 元/克）
    "commodity": (300, 8000),    # 商品指数点位
    "fx": (5.0, 9.5),            # 美元兑人民币
}


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


def _price(value, scaled: bool = False):
    """行情价。

    东财 / 腾讯行情统一按 ``fltt=2`` 请求，返回的就是小数（3956.57）——
    这时**不能**再按量级猜缩放，否则道指 45682 会被砍成 456.82。
    ``scaled=True`` 只给「确认没有 fltt」的口径用（如腾讯个别字段）。
    """
    number = _num(value)
    if number is None:
        return None
    return round(number / 100, 2) if scaled else round(number, 4)


def _pct(value, scaled: bool = False):
    """涨跌幅。``fltt=2`` 时已是百分数（1.13 就是 1.13%），不做量级猜测。"""
    number = _num(value)
    if number is None:
        return None
    return round(number / 100, 2) if scaled else round(number, 2)


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
                "secid": secid, "fltt": 2, "invt": 2, "fields": _QUOTE_FIELDS,
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
    breadth, breadth_note = _check_breadth(breadth)
    return _ok("eastmoney", quotes=quotes, breadth=breadth,
               breadth_note=breadth_note, errors=errors)


def _check_breadth(breadth: dict) -> tuple[dict, str]:
    """涨跌家数合理性校验。

    沪深两市在册个股约 5,200～5,600 只（含北交所则更多），所以「上涨+下跌+平盘」必须落在
    3,000～7,000 之间。落在区间外说明 f104/f105/f106 在这个口径上不是涨跌家数
    （比如返回了别的东西），宁可标「未取得」，也不把错数写进日报。
    """
    up, down, flat = breadth.get("up"), breadth.get("down"), breadth.get("flat")
    if not (up or down):
        return {}, "行情接口未返回涨跌家数"
    total = (up or 0) + (down or 0) + (flat or 0)
    if not (3000 <= total <= 7000):
        return {}, (f"涨跌家数校验未通过（合计 {total} 只，超出 3000～7000 的合理区间），"
                    "已按「未取得」处理")
    return breadth, ""


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
            "ut": EM_UT_CLIST, "fltt": 2, "invt": 2, "fid": "f3",
            "fs": FS_INDUSTRY,
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
# 两融余额报告：按顺序尝试，命中哪份就注明哪份。
# 第一份是 akshare 1.20 在用的（数据中心-融资融券账户统计，含沪深北全市场），
# 第二份是常见的交易所汇总口径，留作兜底。
MARGIN_REPORTS = [
    {"report": "RPTA_WEB_MARGIN_DAILYTRADE", "date": "STATISTICS_DATE",
     "parts": ("FIN_BALANCE", "LOAN_BALANCE"), "sort": "STATISTICS_DATE",
     "label": "两融账户统计（FIN_BALANCE + LOAN_BALANCE）"},
    {"report": "RPTA_RZRQ_LSHJ", "date": "DIM_DATE",
     "parts": ("RZYE", "RQYE"), "sort": "DIM_DATE",
     "label": "两融合计历史（RZYE + RQYE）"},
]


def margin_balances() -> dict:
    """两融余额：融资余额 + 融券余额，取最近两个交易日用于环比。单位亿元。

    顺序试 :data:`MARGIN_REPORTS` 里的报告：不同口径的字段名不一样，逐份按自己的字段取，
    全部拿不到才返回失败——不靠猜字段名，也不把失败当 0。
    """
    errors = []
    for spec in MARGIN_REPORTS:
        try:
            payload = GET_JSON(EM_DATA_URL, {
                "reportName": spec["report"], "columns": "ALL",
                "pageNumber": 1, "pageSize": 5,
                "sortColumns": spec["sort"], "sortTypes": -1,
                "source": "WEB", "client": "WEB",
            })
        except Exception as error:
            errors.append(f"{spec['report']}: {error}")
            continue
        rows = _rows(((payload or {}).get("result") or {}).get("data"))
        if not rows:
            errors.append(f"{spec['report']}: 无数据")
            continue
        values = [_margin_total(row, spec["parts"]) for row in rows[:2]]
        if values and values[0] is not None:
            return _ok("eastmoney", current=values[0],
                       prev=values[1] if len(values) > 1 else None,
                       date=str(rows[0].get(spec["date"]) or "")[:10],
                       report=spec["report"], basis=spec["label"])
        errors.append(f"{spec['report']}: 字段未识别（{sorted(rows[0])[:8]}…）")
    return _fail("eastmoney", errors[0] if errors else "两融接口未返回数据")


def _margin_total(row: dict, parts: tuple) -> float | None:
    """融资余额 + 融券余额（元 → 亿元）；个别口径会直接给合计字段。"""
    direct = _num(row.get("RZRQYE")) or _num(row.get("RZRQ_BALANCE"))
    if direct:
        return round(direct / 1e8, 1)
    values = [_num(row.get(part)) for part in parts]
    if any(value is not None for value in values):
        return round(sum(value for value in values if value is not None) / 1e8, 1)
    return None


# ---------------------------------------------------------------- 龙虎榜
def billboard(as_of: str | None = None, limit: int = 500) -> dict:
    """龙虎榜：机构上榜与营业部上榜的个股净买额，单位亿元。

    接口契约按 akshare 1.20 / efinance 0.5.9 的现役实现对齐：
    ``RPT_DAILYBILLBOARD_DETAILSNEW`` + ``sortColumns=TRADE_DATE,SECURITY_CODE``，
    净买额字段是 **BILLBOARD_NET_AMT**（早期写成 NET_BUY_AMT / 用 NET_BUY_AMT 排序都会失败）。

    机构与营业部的区分：这份明细**没有**席位名字段，只有「解读」（EXPLAIN，形如
    「2家机构买入，成功率47.37%」）。所以按解读里是否含「机构」归类，并在 ``basis`` 里
    写明；席位名一律不猜，也不臆断是哪路游资。
    """
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
        "pageNumber": 1, "pageSize": max(50, min(limit, 1000)),
        "sortColumns": "TRADE_DATE,SECURITY_CODE", "sortTypes": "-1,1",
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
        net = _num(row.get("BILLBOARD_NET_AMT"))
        if net is None:
            net = _num(row.get("NET_BUY_AMT")) or _num(row.get("NET_BUY"))
        if not name or net is None:
            continue
        seat = str(row.get("OPERATEDEPT_NAME") or "")
        reason = str(row.get("EXPLAIN") or row.get("EXPLANATION") or "")
        # 席位名存在（部分明细表有）就按席位判；没有就看上榜解读里有没有「机构」。
        is_inst = "机构" in seat if seat else "机构" in reason
        target = inst if is_inst else hot
        item = {"name": str(name), "net": round(net / 1e8, 2),
                "seat": seat or reason[:40], "streak": None}
        bucket = target.setdefault(str(name), item)
        if bucket is not item:
            bucket["net"] = round(bucket["net"] + item["net"], 2)

    def _top(mapping):
        return sorted((v for v in mapping.values() if v["net"]),
                      key=lambda v: v["net"], reverse=True)[:10]
    if not inst and not hot:
        return _fail("eastmoney", f"龙虎榜字段未识别（返回字段：{sorted(rows[0])[:10]}）")
    return _ok("eastmoney", inst=_top(inst), hot=_top(hot),
               date=str(rows[0].get("TRADE_DATE") or as_of or "")[:10],
               basis=("按席位名归类" if any(r.get("OPERATEDEPT_NAME") for r in rows)
                      else "按「解读」含「机构」归类（该明细无席位名）"),
               note="只按公开席位 / 解读归类，不代表特定游资身份")


# ---------------------------------------------------------------- 大类资产
def _quote_one(secid: str) -> dict | None:
    payload = GET_JSON(EM_QUOTE_URL, {
        "secid": secid, "fltt": 2, "invt": 2,
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


def _cgb10y_chinabond() -> dict:
    """10 年期国债收益率主源：中债登收益率曲线（HTML 表格）。

    口径与 akshare ``bond_china_yield`` 一致：``historyQuery`` + ``gjqx=0&qxId=ycqx``，
    页面里有多张表，含「10年」表头的那张才是国债收益率曲线；取最近两个交易日做环比。
    页面是 HTML，这里只做最小解析（找表 → 找表头列号 → 取日期与数值），不引入额外依赖。
    """
    import re
    from datetime import date, timedelta

    today = date.today()
    params = {
        "startDate": (today - timedelta(days=40)).strftime("%Y-%m-%d"),
        "endDate": today.strftime("%Y-%m-%d"),
        "gjqx": "0", "qxId": "ycqx", "locale": "cn_ZH",
    }
    html = GET_TEXT(f"{CGB_URL}?{urlencode(params)}").replace("&nbsp;", " ")

    def _cells(chunk: str) -> list[str]:
        return [re.sub(r"<[^>]+>", "", cell).replace("&nbsp", " ").strip()
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", chunk, re.S | re.I)]

    series = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S | re.I):
        rows = [_cells(row) for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)]
        header = next((row for row in rows if "10年" in row), None)
        if not header:
            continue
        col = header.index("10年")
        for row in rows:
            if len(row) <= col or row is header:
                continue
            stamp = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", row[0])
            value = _num(row[col])
            if not stamp or value is None or not 0.5 <= value <= 6.0:
                continue
            series.append((f"{stamp.group(1)}-{stamp.group(2)}-{stamp.group(3)}", value))
        if series:
            break
    if not series:
        raise ValueError("中债登页面未解析到「10年」列数据（页面结构可能已变）")
    series.sort()
    latest, prev = series[-1], (series[-2] if len(series) > 1 else (None, None))
    change = round(latest[1] - prev[1], 4) if prev[1] is not None else None
    return {
        "name": "中国 10 年期国债收益率（中债登）",
        "value": round(latest[1], 4),
        "prev": round(prev[1], 4) if prev[1] is not None else None,
        # 收益率是利率不是价格：环比用 bp，别给百分比涨跌幅（0.02/2.1 会变成 1% 的假涨跌）。
        "change_bp": round(change * 100, 2) if change is not None else None,
        "pct": None, "date": latest[0], "source": "chinabond",
        # 收益率历史序列仍要用东财日 K（中债登这张表只给区间内数据）：
        # 标记为历史代理，若那个代码返回的不是收益率，上层 _bond_series 会按 0.3pp 护栏整段丢弃。
        "code": "100.CN10Y", "history_proxy": True,
    }


def cross_asset() -> dict:
    """大类资产：10Y 国债收益率、黄金、商品、汇率。逐个候选代码尝试，取到哪个标哪个。"""
    items, used, errors = {}, {}, []
    for key, candidates in CROSS_CANDIDATES.items():
        if key == "cgb10y":
            try:
                items[key] = _cgb10y_chinabond()
                used[key] = "中债登收益率曲线（10 年）"
                continue
            except Exception as error:
                errors.append(f"中债登 10Y: {error}")
        for secid, label in candidates:
            try:
                quote = _quote_one(secid)
            except Exception as error:
                errors.append(f"{label}: {error}")
                continue
            if not quote or quote.get("value") is None:
                continue
            low, high = CROSS_RANGES.get(key, (float("-inf"), float("inf")))
            if not (low <= quote["value"] <= high):
                # 代码能返回数，但数不像这类资产（比如点位被当成收益率）——不采用，记进 errors。
                errors.append(f"{label}：返回值 {quote['value']} 不在 {low}～{high} 区间，"
                              "疑似码值不对，已跳过")
                continue
            items[key] = {
                "source": "eastmoney",
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
    sources = {item.get("source", "") for item in items.values()}
    origin = "+".join(sorted(src for src in sources if src)) or "eastmoney"
    return _ok(origin, items=items, used=used, errors=errors)


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
    # pagesize 给足：行业分布按池子里的样本统计，只取前 300 只会在涨停潮那天漏板块。
    base = {"ut": EM_UT_ZTPOOL, "dpt": "wz.ztzt",
            "Pageindex": 0, "pagesize": 10000}
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

# ---------------------------------------------------------------- 接口自检
def _probe(label: str, call) -> dict:
    """跑一次采集，记录 ok / 耗时 / 关键字段 / 错误，供 verify() 汇总。"""
    from time import perf_counter
    start = perf_counter()
    try:
        result = call() or {}
    except Exception as error:      # 采集器自身异常也要如实记下来
        result = {"ok": False, "error": str(error)}
    cost = round((perf_counter() - start) * 1000)
    ok = bool(result.get("ok"))
    detail = []
    for key in ("quotes", "breadth", "boards", "items", "inst", "hot", "sectors_up"):
        value = result.get(key)
        if isinstance(value, dict):
            detail.append(f"{key}={len(value)}")
        elif isinstance(value, list):
            detail.append(f"{key}={len(value)}")
    if result.get("current") is not None:
        detail.append(f"两融={result['current']} 亿")
    if result.get("limit_up") is not None or result.get("limit_down") is not None:
        detail.append(f"涨停={result.get('limit_up')} 跌停={result.get('limit_down')}")
    return {"label": label, "ok": ok, "ms": cost, "source": result.get("source", ""),
            "detail": " · ".join(detail), "error": result.get("error", ""),
            "result_keys": sorted(k for k in result if k not in ("ok", "source", "error"))}


def verify(as_of: str | None = None) -> dict:
    """逐项自检全部采集接口：能连上就用真实响应，连不上就如实报错。

    这是给「在有网的机器上核对一遍」用的：返回每项的 ok / 耗时 / 关键字段计数 / 错误原文。
    不写死任何期望值——接口改了字段，这里会立刻显示「字段未识别」而不是静默给空。
    """
    from time import perf_counter

    start = perf_counter()
    checks = [
        _probe("指数行情 + 涨跌家数", index_quotes),
        _probe("指数行情（腾讯备用源）", index_quotes_tencent),
        _probe("行业板块", industry_boards),
        _probe("涨跌停池", lambda: limit_pools(as_of)),
        _probe("两融余额", margin_balances),
        _probe("龙虎榜", lambda: billboard(as_of)),
        _probe("大类资产", cross_asset),
    ]
    passed = sum(1 for item in checks if item["ok"])
    return {
        "as_of": as_of or "",
        "checked_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "passed": passed, "total": len(checks),
        "elapsed_ms": round((perf_counter() - start) * 1000),
        "checks": checks,
        "verdict": ("全部接口可用" if passed == len(checks)
                    else f"{len(checks) - passed} 项不可用（见 error 原文）"),
    }


def main(argv=None) -> int:
    """命令行：``python3 quant_feeds.py --check [--date YYYY-MM-DD]``。

    在有网环境跑一遍就能看清每个接口通不通、慢不慢、字段还认不认得。
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="量化日报采集层自检")
    parser.add_argument("--check", action="store_true", help="逐项自检全部接口（需要外网）")
    parser.add_argument("--date", help="交易日 YYYY-MM-DD，默认今天")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    as_of = args.date or __import__("datetime").date.today().strftime("%Y-%m-%d")
    if not args.check:
        parser.print_help()
        return 2
    report = verify(as_of)
    if args.json:
        print(_json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"采集层自检 · {as_of} · {report['passed']}/{report['total']} 通过 "
              f"· {report['elapsed_ms']} ms")
        for item in report["checks"]:
            mark = "OK  " if item["ok"] else "FAIL"
            line = f"  [{mark}] {item['label']:<18} {item['ms']:>5} ms  {item['detail']}"
            if item["error"]:
                line += f"  错误：{item['error'][:90]}"
            print(line)
        print(f"结论：{report['verdict']}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
