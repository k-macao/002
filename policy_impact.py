#!/usr/bin/env python3
"""近 30 日全球政策发布 → 股票市场影响。

「AI 政策深度」的主问题：滚动窗口内、已经核验过发文日的全球政策，如何作用到
A 股 / 港股 / 美股。台账只收有公开来源的发布、讲话信号和窗口内生效事项；
没有核实日期的快讯不写成政策发布。

指数分写死、可复算（见 :func:`_bias_label` 与模块常量）。有公开收盘报道的才标
「已观测」，其余是机制推演，不外推点位，不构成投资建议。
"""
from __future__ import annotations

import html as _html
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

POLICY_WINDOW_DAYS = 30
POLICY_MARKETS = ("A股", "港股", "美股")

# ---------------------------------------------------------------- 官方源头登记
# 台账条目最终要能指回「谁发的、发在哪」。这一节只登记**机构官方站点与入口页**，
# 不臆造具体文件链接：文件链接由抓取解析得到，解析不到就如实标「待核对」。
# ``mode``：
#   rss   —— 官方 RSS，可直接解析条目；
#   html  —— 官方列表页，按锚文本 + 日期解析；
#   manual—— 只登记入口，靠人工 / Agent 核对（政府站常无稳定 RSS，反爬也重）。
OFFICIAL_SOURCES = [
    {"key": "sc", "issuer": "国务院", "jurisdiction": "中国",
     "domain": "www.gov.cn", "entry": "https://www.gov.cn/zhengce/", "mode": "manual",
     "note": "中国政府网 · 政策文件库（国务院常务会议、国发 / 国办发文件）"},
    {"key": "pbc", "issuer": "中国人民银行", "jurisdiction": "中国",
     "domain": "www.pbc.gov.cn", "entry": "http://www.pbc.gov.cn", "mode": "manual",
     "note": "人民银行 · 公告 / 货币政策 / 新闻发布"},
    {"key": "csrc", "issuer": "中国证监会", "jurisdiction": "中国",
     "domain": "www.csrc.gov.cn", "entry": "http://www.csrc.gov.cn", "mode": "manual",
     "note": "证监会 · 新闻发布 / 规章 / 征求意见"},
    {"key": "mohurd", "issuer": "住房城乡建设部", "jurisdiction": "中国",
     "domain": "www.mohurd.gov.cn", "entry": "https://www.mohurd.gov.cn", "mode": "manual",
     "note": "住建部 · 政策发布 / 规范性文件"},
    {"key": "nfra", "issuer": "国家金融监督管理总局", "jurisdiction": "中国",
     "domain": "www.nfra.gov.cn", "entry": "https://www.nfra.gov.cn", "mode": "manual",
     "note": "金融监管总局 · 规章 / 通知"},
    {"key": "mof", "issuer": "财政部", "jurisdiction": "中国",
     "domain": "www.mof.gov.cn", "entry": "https://www.mof.gov.cn", "mode": "manual",
     "note": "财政部 · 财政政策 / 国债 / 专项债"},
    {"key": "ndrc", "issuer": "国家发展改革委", "jurisdiction": "中国",
     "domain": "www.ndrc.gov.cn", "entry": "https://www.ndrc.gov.cn", "mode": "manual",
     "note": "发改委 · 产业政策 / 项目审批"},
    {"key": "fed", "issuer": "美联储 FOMC", "jurisdiction": "美国",
     "domain": "www.federalreserve.gov",
     "entry": "https://www.federalreserve.gov/newsevents/pressreleases.htm", "mode": "html",
     "note": "Federal Reserve · Press Releases（议息声明 / 讲话 / 监管公告）"},
    {"key": "ecb", "issuer": "欧洲央行", "jurisdiction": "欧元区",
     "domain": "www.ecb.europa.eu", "entry": "https://www.ecb.europa.eu/press/html/index.en.html",
     "mode": "html", "note": "ECB · Press（货币政策决定 / 行长讲话）"},
    {"key": "boj", "issuer": "日本央行", "jurisdiction": "日本",
     "domain": "www.boj.or.jp", "entry": "https://www.boj.or.jp/en/mopo/mpmdeci/index.htm",
     "mode": "html", "note": "Bank of Japan · Monetary Policy Decisions"},
    {"key": "ust", "issuer": "美国财政部", "jurisdiction": "美国",
     "domain": "home.treasury.gov", "entry": "https://home.treasury.gov/news/press-releases",
     "mode": "html", "note": "U.S. Treasury · Press Releases（国债 / 回购 / 制裁）"},
]

# 台账 issuer 字符串 → 登记表 key（按顺序做包含匹配，先到先得）。
_ISSUER_KEYS = [
    ("金融监管总局", "nfra"), ("住房城乡建设部", "mohurd"), ("自然资源部", "mohurd"),
    ("中国人民银行", "pbc"), ("证监会", "csrc"), ("财政部", "mof"),
    ("美联储", "fed"), ("欧洲央行", "ecb"), ("日本央行", "boj"),
    ("美国财政部", "ust"), ("国务院", "sc"),
]
_BY_KEY = {item["key"]: item for item in OFFICIAL_SOURCES}
_OFFICIAL_HOSTS = tuple(item["domain"] for item in OFFICIAL_SOURCES)

OFFICIAL_TIMEOUT = float(os.environ.get("POLICY_SOURCE_TIMEOUT", "10"))
OFFICIAL_ITEMS_PER_SOURCE = 12
OFFICIAL_LOOKBACK_DAYS = 45
CACHE_PATH = Path(__file__).with_name("policy_ledger_cache.json")
CACHE_TTL_HOURS = 36

# 类型权重：正式发布全额；讲话 / 指引只是信号；窗口内生效的是更早发文的实施，不重复计为新发文。
_KIND_WEIGHT = {"发布": 1.0, "信号": 0.65, "生效": 0.75}
# 量级权重：跨市场重要性，不是涨跌幅。
_MAG_WEIGHT = {"高": 1.4, "中": 1.0, "低": 0.6}
# 同一主题、同一方向每多一条，只加确认项，避免同日组合拳把指数分乘三倍。
_CONFIRM = 0.22
# 台账条目的影响期（自然日）：发文日 + 该天数内视为仍在生效，供「存量政策」层使用。
# 只用于判断「这条政策还管不管用」，不改变发文日在窗口内的计分。
_HORIZON_DAYS = {
    "数日": 7, "数周": 28,
    "1-4 周": 28, "1-6 周": 42, "2-6 周": 42, "1-8 周": 56,
    "1-4 个月": 120, "1-6 个月": 180, "1-6 周 ": 42,
    "3-12 个月": 365, "6-24 个月": 730, "6-36 个月": 1095, "12-36 个月": 1095,
}
_DEFAULT_HORIZON_DAYS = 30
# 存量政策按半权计入：方向仍然有效，但不代表当日强弱，也不与新发文混成一个不可拆的分。
_CARRY_DECAY = 0.5
# 指数分 → 标签。与多空板块的 60/40 不是同一套口径，这里专用于政策窗口。
_BIAS_HI = 1.2
_BIAS_LO = 0.45


# 窗口截止日按北京时间算：GitHub Actions 跑在 UTC，直接用 UTC 日期会让 0–8 点的卡片停在昨天。
_POLICY_TZ_HOURS = int(os.environ.get("POLICY_TZ_OFFSET", "8"))


def _as_of(value) -> date:
    """窗口截止日。缺省为北京时间当日（可用``POLICY_TZ_OFFSET``改时区偏移）。"""
    if value is None:
        return (datetime.now(timezone.utc) + timedelta(hours=_POLICY_TZ_HOURS)).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()


def policy_window_bounds(as_of=None, days: int = POLICY_WINDOW_DAYS) -> tuple[date, date]:
    """含当日的近 ``days`` 个自然日。30 日窗口即截止日往前 29 天。"""
    end = _as_of(as_of)
    span = max(1, int(days))
    return end - timedelta(days=span - 1), end


def _bias_label(score: float) -> str:
    """指数分阈值：≥1.2 偏多，≥0.45 中性偏多，> -0.45 中性，> -1.2 中性偏空，否则偏空。"""
    if score >= _BIAS_HI:
        return "偏多"
    if score >= _BIAS_LO:
        return "中性偏多"
    if score >= -_BIAS_LO:
        return "中性"
    if score > -_BIAS_HI:
        return "中性偏空"
    return "偏空"


def _dir_label(direction: int) -> str:
    return {-2: "明显承压", -1: "承压", 0: "中性", 1: "支撑", 2: "明显支撑"}.get(int(direction), "中性")


def _mx(direction: int, weight: float, sectors: list[tuple[str, int]], channel: str,
        observed: bool = False) -> dict:
    return {
        "dir": int(direction),
        "weight": float(weight),
        "sectors": [{"name": name, "dir": int(sdir)} for name, sdir in sectors],
        "channel": channel,
        "observed": bool(observed),
    }


def _release(**fields) -> dict:
    fields.setdefault("cross_asset", "")
    fields.setdefault("horizon", "1-4 周")
    return fields


# 发文日必须是可核验的公历日期。新增一条要带来源；禁止把未核实日期的快讯写进台账。
# 2026-08-19 的美债回购发文故意留在台账里：默认 30 日窗口（截止 2026-09-23）会把它排除，
# 9 月 9 日只记「生效」，避免同一政策计两次。
_POLICY_RELEASES = [
    _release(
        id="UST-BUYBACK-20260819", date="2026-08-19", issuer="美国财政部", jurisdiction="美国",
        doctype="国债回购安排", kind="发布", topic="财政债务", dimension="财政 · 关税与债务",
        theme="美债长端流动性", stance="偏鸽", magnitude="中", horizon="数周",
        title="上调长端国债流动性支持回购的单次上限",
        short="美债回购上限上调",
        summary="财政部长贝森特宣布，10–30 年期名义国债流动性支持回购的单次上限至少翻倍，由 20 亿美元提至至少 40 亿美元，9 月 9 日起至 11 月 4 日生效。",
        markets={
            "美股": _mx(1, 0.4, [("利率敏感成长", 1)],
                       "意在缓和长端收益率，但规模相对国债存量有限，不能单独当成风险资产宽松。"),
            "A股": _mx(0, 0.2, [], "只经美债利率间接传导，本身不是 A 股政策。"),
            "港股": _mx(0, 0.2, [], "间接、幅度有限。"),
        },
        keywords=["长端回购", "buyback operation", "debt buyback"],
        source="https://www.politico.com/news/2026/08/19/treasury-buy-back-debt-bond-market-pain-01041461",
        source_note="POLITICO，2026-08-19",
    ),
    _release(
        id="FED-JH-20260828", date="2026-08-28", issuer="美联储主席沃什", jurisdiction="美国",
        doctype="政策讲话", kind="信号", topic="货币政策", dimension="货币政策 · 美联储",
        theme="海外货币收紧", stance="偏鹰", magnitude="高", horizon="2-6 周",
        title="杰克逊霍尔：通胀若未够快回落，仍有工作要做",
        short="杰克逊霍尔偏鹰讲话",
        summary="8 月 27–29 日杰克逊霍尔年会上，主席沃什表示必须确信底层通胀正清晰、足够快地回到目标，否则仍有工作要做。市场对 9 月加息的概率预期升至约 55%–60%。这是讲话信号，不是议息决议。",
        markets={
            "美股": _mx(-1, 0.75, [("长久期成长", -1), ("AI 算力", -1)],
                       "先定价 9 月加息，长久期资产折现率上修。当日指数点位未单列，按预期定价而不是已落地决议。"),
            "A股": _mx(-1, 0.65, [("长久期成长", -1)],
                      "外部加息预期抬升，压制 A 股阶段向上的高度，成长估值更敏感。不外推点位。"),
            "港股": _mx(-1, 0.8, [("恒生科技", -1)],
                       "港元联系汇率下，美债利率预期直接进港股贴现率。不外推点位。"),
        },
        keywords=["杰克逊霍尔", "Jackson Hole"],
        source="https://news.qq.com/rain/a/20260830A061X500",
        source_note="公开报道转述 2026-08-28 讲话",
    ),
    _release(
        id="PBC-HOUSING-20260828", date="2026-08-28", issuer="中国人民银行 / 金融监管总局", jurisdiction="中国",
        doctype="意见", kind="发布", topic="地产金融", dimension="地产与地方政策",
        theme="国内地产金融", stance="偏鸽", magnitude="高", horizon="1-6 个月",
        title="银发〔2026〕171号：改革完善房地产信贷管理",
        short="房贷期限延至40年",
        summary="个人住房贷款最长期限由 30 年延长至 40 年；开发贷款实行主办银行制；预售项目的个人住房贷款在竣工备案后发放，现房在销售备案后发放。自印发之日起施行，存量合同新老划断。",
        markets={
            "美股": _mx(0, 0.2, [], "不直接定价美股。"),
            "A股": _mx(1, 0.7, [("地产链", 1), ("银行", 1), ("建筑建材", 1)],
                      "降低月供门槛、托住房消费；预售放款后置对房企回款是约束，不是无条件宽松。指数点位未见公开收盘复核，按行业机制推演。"),
            "港股": _mx(1, 0.75, [("内房股", 1)],
                       "内房股对信贷与销售制度更敏感，融资和交付预期改善。不外推恒指点位。"),
        },
        keywords=["40年", "房地产信贷管理", "住房贷款最长期限", "银发〔2026〕171"],
        source="https://news.qq.com/rain/a/20260829A08K0F00",
        source_note="央行、金融监管总局，2026-08-28",
    ),
    _release(
        id="MOHURD-SALES-20260828", date="2026-08-28", issuer="住房城乡建设部 / 自然资源部 / 金融监管总局",
        jurisdiction="中国", doctype="通知", kind="发布", topic="地产金融", dimension="地产与地方政策",
        theme="国内地产金融", stance="中性", magnitude="中", horizon="1-6 个月",
        title="完善商品住房销售制度，有序推行现房销售",
        short="现房销售配套落地",
        summary="要求有力有序推行商品住房现房销售，减少交付纠纷。与同日信贷文件衔接：销售端现房优先，信贷端把放款压到竣工或销售备案之后。",
        markets={
            "美股": _mx(0, 0.2, [], "不直接定价美股。"),
            "A股": _mx(1, 0.4, [("地产链", 1)],
                      "改善购房人信心，但预售回款后置，高杠杆房企现金流不一定立即好转。"),
            "港股": _mx(1, 0.4, [("内房股", 1)],
                       "交付风险下降是中期支撑，短线仍要看销售数据。不外推点位。"),
        },
        keywords=["现房销售", "商品住房销售制度"],
        source="https://news.qq.com/rain/a/20260831A03BS000",
        source_note="住建部等三部门，2026-08-28",
    ),
    _release(
        id="CSRC-RE-20260828", date="2026-08-28", issuer="中国证监会", jurisdiction="中国",
        doctype="意见", kind="发布", topic="资本市场", dimension="资本市场监管",
        theme="国内地产金融", stance="偏鸽", magnitude="高", horizon="1-6 个月",
        title="关于资本市场支持构建房地产发展新模式的意见",
        short="上市房企再融资开闸",
        summary="支持上市房企向特定对象再融资，并以股份、定向可转债或现金并购涉房资产；支持公司债、CMBS、不动产 ABS 与租赁住房 REITs。同时要求募集资金穿透式监管，推动违约房企债券处置出清。",
        markets={
            "美股": _mx(0, 0.2, [], "不直接定价美股。"),
            "A股": _mx(1, 0.7, [("地产链", 1), ("券商", 1), ("建筑", 1)],
                      "打开股权和债券融资工具箱，券商投行与内房股权更直接受益；造假和挪用仍是监管红线。不外推指数点位。"),
            "港股": _mx(1, 0.8, [("内房股", 1), ("中资券商", 1)],
                       "境外上市房企再融资按备案持续办理，港股内房对再融资和重组更敏感。不外推恒指点位。"),
        },
        keywords=["房地产发展新模式", "上市房地产开发企业", "涉房资产"],
        source="https://news.qq.com/rain/a/20260828A0C4KN00",
        source_note="证监会，2026-08-28",
    ),
    _release(
        id="UST-BUYBACK-20260909", date="2026-09-09", issuer="美国财政部", jurisdiction="美国",
        doctype="回购操作", kind="生效", topic="财政债务", dimension="财政 · 关税与债务",
        theme="美债长端流动性", stance="中性", magnitude="中", horizon="数日",
        title="扩大后的长端国债回购开始操作，未能扭转长债下跌",
        short="美债长端回购扩容生效",
        summary="8 月 19 日宣布的回购扩容于 9 月 9 日进入实施。当日扩大操作被市场解读为买入最多约 60 亿美元较长期国债，但在油价上涨、通胀担忧下没有挡住长债跌势。发文日在 30 日窗口之外，这里只记生效，不重复计为新发文。",
        markets={
            "美股": _mx(0, 1.0, [("利率敏感成长", 0)],
                       "流动性支持信号被通胀交易盖过，不能当成风险资产的增量宽松。", True),
            "A股": _mx(0, 0.4, [], "美债长端没有回落，对 A 股估值的外部压力就没有解除。"),
            "港股": _mx(0, 0.4, [], "贴现率压力未解，但不单列方向。"),
        },
        cross_asset="已观测的是债市而不是股市：长端美债当日继续走弱，回购规模相对存量市场偏小。",
        keywords=["国债回购", "debt buyback", "long-dated debt"],
        source="https://www.bloomberg.com/news/articles/2026-09-09/us-more-than-doubles-long-dated-debt-buyback-size-to-6-billion",
        source_note="Bloomberg，2026-09-09；发文见 POLITICO 2026-08-19",
    ),
    _release(
        id="ECB-HIKE-20260910", date="2026-09-10", issuer="欧洲央行", jurisdiction="欧元区",
        doctype="议息决定", kind="发布", topic="货币政策", dimension="货币政策 · 美联储",
        theme="海外货币收紧", stance="偏鹰", magnitude="高", horizon="1-8 周",
        title="存款便利利率上调 25bp 至 2.50%",
        short="欧央行存款利率至2.50%",
        summary="9 月 10 日宣布三大利率各上调 25bp：存款便利利率至 2.50%，主要再融资利率至 2.65%，边际贷款利率至 2.90%，9 月 16 日生效。这是 2026 年第二次加息，触发因素是能源通胀。拉加德称通胀风险偏向上行，但不预承诺定路径。",
        markets={
            "美股": _mx(-1, 0.55, [("全球成长", -1)],
                       "全球贴现率同向抬升。当日美股点位不由这一则单独决定，标为机制推演。"),
            "A股": _mx(-1, 0.45, [("出口链", -1)],
                      "欧元区需求与全球风险偏好边际转紧，对 A 股是外部变量，不是主驱动。不外推点位。"),
            "港股": _mx(-1, 0.55, [("欧洲敞口", -1), ("恒生科技", -1)],
                       "外部利率抬升，利率敏感的成长股偏压制。不外推点位。"),
        },
        cross_asset="欧元与欧元区国债收益率会重新定价；此处不外推具体点位。下次会议为 10 月 29 日。",
        keywords=["欧洲央行", "ECB", "存款便利", "Lagarde"],
        source="https://stockmarkethours.org/events/ecb-meeting",
        source_note="欧洲央行，2026-09-10 宣布，2026-09-16 生效",
    ),
    _release(
        id="CN-FIN15-20260910", date="2026-09-10", issuer="中国人民银行 / 证监会", jurisdiction="中国",
        doctype="规划与发布会", kind="发布", topic="金融制度", dimension="资本市场监管",
        theme="国内金融制度", stance="偏鸽", magnitude="中", horizon="6-36 个月",
        title="《金融强国建设“十五五”规划》出台，资本市场八个“进一步”",
        short="金融强国十五五规划出台",
        summary="国新办发布会上，央行副行长陆磊称该规划正式出台：到 2030 年金融调控协同有效、国际竞争力提高，到 2035 年建成现代金融体系，并推进人民币离岸市场。证监会副主席李超提出八个“进一步”，包括提高制度包容性、壮大中长期资金、扩大对外开放。",
        markets={
            "美股": _mx(0, 0.2, [], "不直接定价美股。"),
            "A股": _mx(1, 0.55, [("券商", 1), ("中长期资金", 1)],
                      "制度路线图偏中期，不是即期资金注入。对券商和耐心资本是方向性利好，不外推指数涨幅。"),
            "港股": _mx(1, 0.45, [("中资券商", 1), ("互联互通", 1)],
                       "离岸人民币和互联互通是港股中期变量，不是当日指数催化剂。"),
        },
        keywords=["金融强国", "十五五"],
        source="https://finance.sina.com.cn/china/2026-09-10/doc-iniritaq8532539.shtml",
        source_note="国新办，2026-09-10",
    ),
    _release(
        id="FED-HIKE-20260916", date="2026-09-16", issuer="美联储 FOMC", jurisdiction="美国",
        doctype="议息决定", kind="发布", topic="货币政策", dimension="货币政策 · 美联储",
        theme="海外货币收紧", stance="偏鹰", magnitude="高", horizon="1-8 周",
        title="联邦基金利率上调 25bp 至 3.75%-4.00%",
        short="美联储加息至3.75%-4.00%",
        summary="12 名票委一致加息 25bp，为三年多来首次加息。当日美股三大指数收跌，道指约 600 点（公开报道约 500–630 点）。点阵图中值指向 2026 年年内再加息一次，2027 年不降息。沃什称这是撤掉“一点宽松”，通胀过高且持续时间过长。",
        markets={
            "美股": _mx(-2, 1.0, [("长久期成长", -1), ("AI 算力", -1), ("道指成分", -1), ("银行", 1)],
                       "当日三大指数收跌，道指约 600 点（公开报道约 500–630 点）。加息本身部分被计价，超预期的是再加息路径和“更及时回到 2%”。",
                       True),
            "A股": _mx(-1, 0.9, [("长久期成长", -1), ("北向敏感", -1)],
                      "外部贴现率上修，美元和美债利率易升，压制成长估值与风险偏好。不外推次日 A 股点位。"),
            "港股": _mx(-1, 1.15, [("恒生科技", -1), ("利率敏感", -1)],
                       "联系汇率下港元利率跟随美元，高估值更承压；本地银行净息差方向相反，但权重不足以对冲指数。不外推点位。"),
        },
        cross_asset="美债收益率随再加息路径上修。银行净息差方向改善，长久期债券和贵金属承压。",
        keywords=["3.75%", "3.75%-4.00%", "timelier return", "dose of accommodation", "联邦基金利率上调"],
        source="https://finance.yahoo.com/markets/live/stock-market-today-wednesday-september-16-dow-sp-500-nasdaq-fed-meeting-decision-080356525.html",
        source_note="FOMC，2026-09-16；道指收跌见当日市场报道",
    ),
    _release(
        id="BOJ-HIKE-20260918", date="2026-09-18", issuer="日本央行", jurisdiction="日本",
        doctype="议息决定", kind="发布", topic="货币政策", dimension="货币政策 · 美联储",
        theme="海外货币收紧", stance="偏鹰", magnitude="高", horizon="1-6 周",
        title="政策利率上调 25bp 至 1.25%，为 1995 年以来最高",
        short="日央行加息至1.25%",
        summary="无担保隔夜拆借利率由 1.00% 上调至 1.25%，7 比 2 通过，两名委员主张维持不变。植田和男未承诺连续加息。核心 CPI 8 月降至 1.7%。",
        markets={
            "美股": _mx(0, 0.5, [("日元套息", 0)],
                       "加息已被计价，分裂投票削弱后续加息预期，没有形成对美股的额外冲击。"),
            "A股": _mx(0, 0.4, [], "套息交易若加速平仓才会外溢；当日指引偏谨慎，不外推 A 股点位。"),
            "港股": _mx(0, 0.4, [], "不把日股上涨直接记成港股利好。"),
        },
        cross_asset="已观测：日经 225 上涨约 1.5%，10 年期日债收益率回落，美元兑日元回到 157 上方。加息落地但指引不够鹰，日元不升反跌。",
        keywords=["1.25%", "日本央行", "Bank of Japan", "植田"],
        source="https://www.babypips.com/news/headline-boj-rate-hike-september-2026-yen-falls-dovish-ueda",
        source_note="日本央行，2026-09-18",
    ),
    _release(
        id="PBC-LPR-20260920", date="2026-09-20", issuer="中国人民银行", jurisdiction="中国",
        doctype="LPR 报价", kind="发布", topic="货币政策", dimension="央行 · 流动性",
        theme="国内利率按兵不动", stance="中性", magnitude="中", horizon="1-4 周",
        title="9 月 LPR 连续第 16 个月不变：1 年期 3.0%，5 年期以上 3.5%",
        short="LPR连续16个月不变",
        summary="9 月 20 日公布的 LPR 与上月相同。7 天期逆回购利率自 2025 年 5 月下调后维持 1.40%，报价行缺乏下调加点的动力。市场解读为符合预期，短期降息必要性不高。",
        markets={
            "美股": _mx(0, 0.2, [], "不直接定价美股。"),
            "A股": _mx(-1, 0.35, [("地产链", -1)],
                      "没有新的降息催化，8 月 28 日地产组合拳缺少利率配套。结果符合预期，冲击应小于意外按兵不动。不外推点位。"),
            "港股": _mx(0, 0.4, [("内房股", -1)],
                       "内房缺少利率下行配合，但结果在预期内，不单列指数方向。"),
        },
        keywords=["LPR", "贷款市场报价利率"],
        source="https://finance.sina.com.cn/tech/roll/2026-09-21/doc-inispync2095364.shtml",
        source_note="全国银行间同业拆借中心，2026-09-20",
    ),
]


# ---------------------------------------------------------------- 官方源抓取与核实
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 测试注入点：默认走 urllib，离线环境 / 单测里换成假函数。
GET_TEXT = None


def _http_text(url: str, timeout: float | None = None) -> str:
    request = Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    with urlopen(request, timeout=timeout or OFFICIAL_TIMEOUT) as response:
        raw = response.read()
    for charset in ("utf-8", "gb18030"):
        try:
            return raw.decode(charset)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _get_text(url: str) -> str:
    getter = GET_TEXT or _http_text
    return getter(url)


def official_source_for(issuer: str) -> dict | None:
    """台账 issuer → 官方入口。匹配不到返回 None（不硬塞一个不相关的机构）。

    多部门联合发文（如「中国人民银行 / 金融监管总局」）取**字符串里最先出现**的机构，
    所以「美国财政部」不会被「财政部」抢走、联合发文也不会挂到次要部门名下。
    """
    text = str(issuer or "")
    best = None
    for needle, key in _ISSUER_KEYS:
        index = text.find(needle)
        if index < 0:
            continue
        rank = (index, -len(needle))
        if best is None or rank < best[0]:
            best = (rank, key)
    return dict(_BY_KEY[best[1]]) if best else None


def official_host(url: str) -> str:
    host = re.sub(r"^[a-z]+://", "", str(url or "").strip().lower())
    return host.split("/")[0].split(":")[0]


def is_official_url(url: str) -> bool:
    """是否落在登记过的官方域名下（含子域）。用于把「官方原文」和「媒体转述」分开。"""
    host = official_host(url)
    return bool(host) and any(host == domain or host.endswith("." + domain) for domain in _OFFICIAL_HOSTS)


def source_kind(url: str) -> str:
    return "官方原文" if is_official_url(url) else "媒体转述"


_DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),
    (re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"), "ymd"),
    (re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"), "ymd"),
    (re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})"), "ymd"),
]
_MONTHS = {name: index for index, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}
_EN_DATE = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
# RSS 的 pubDate 是 RFC 822 格式（Fri, 16 Oct 2026 13:45:00 +0200），日期在前、月份在中间。
_EN_DATE_DMY = re.compile(
    r"(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(\d{4})", re.I)


def parse_date(text: str) -> str:
    """从一段文本里取第一个合法日期，返回 YYYY-MM-DD；取不到返回空串。"""
    raw = str(text or "")
    for pattern, _ in _DATE_PATTERNS:
        match = pattern.search(raw)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
            except ValueError:
                continue
    match = _EN_DATE_DMY.search(raw)
    if match:
        month = _MONTHS.get(next((name for name in _MONTHS if name.startswith(match.group(2).lower())), ""), 0)
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(1))).isoformat()
            except ValueError:
                return ""
    match = _EN_DATE.search(raw)
    if match:
        month = _MONTHS.get(next((name for name in _MONTHS if name.startswith(match.group(1).lower())), ""), 0)
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(2))).isoformat()
            except ValueError:
                return ""
    return ""


def _date_candidates(text: str) -> list[tuple[int, int, str]]:
    """文本里所有可解析日期：(start, end, ISO)。用于判断日期是不是紧贴在链接前面。"""
    raw = str(text or "")
    found = []
    for pattern, _ in _DATE_PATTERNS:
        for match in pattern.finditer(raw):
            try:
                found.append((match.start(), match.end(),
                              date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()))
            except ValueError:
                continue
    for pattern, month_group, day_group, year_group in ((_EN_DATE_DMY, 2, 1, 3), (_EN_DATE, 1, 2, 3)):
        for match in pattern.finditer(raw):
            name = match.group(month_group).lower()
            month = _MONTHS.get(next((key for key in _MONTHS if key.startswith(name)), ""), 0)
            if not month:
                continue
            try:
                found.append((match.start(), match.end(),
                              date(int(match.group(year_group)), month, int(match.group(day_group))).isoformat()))
            except ValueError:
                continue
    return sorted(found)


_BLOCK_OPEN = re.compile(r"<(?:div|li|tr|td|th|dd|dt|p|span|time|h[1-6])\b[^>]*>", re.I)
_TAG_RX = re.compile(r"<!--.*?-->|<[^>]+>", re.S)
_VOID_TAGS = {"br", "img", "meta", "link", "input", "hr", "source", "area", "base",
              "col", "embed", "param", "track", "wbr"}


_INLINE_TAGS = {"a", "span", "time", "b", "strong", "em", "i", "small", "mark",
                "u", "s", "code", "sup", "sub", "label", "br"}


def _block_path(path: tuple) -> tuple:
    """去掉行内标签，只留块级容器：判断两个位置是不是落在同一条目（或同一父容器的兄弟块）里。"""
    return tuple(node for node in path if node[0] not in _INLINE_TAGS)


def _element_paths(text: str, positions: list[int]) -> dict[int, tuple]:
    """给定若干字符位置，返回每个位置所在的元素栈（单次扫描，栈里是 (tag, 起始位置)）。"""
    wanted = sorted(set(positions))
    paths, stack, cursor = {}, [], 0
    for token in _TAG_RX.finditer(text):
        while cursor < len(wanted) and wanted[cursor] < token.start():
            paths[wanted[cursor]] = tuple(stack)
            cursor += 1
        raw = token.group(0)
        if raw.startswith("<!--"):
            continue
        name = re.match(r"</?\s*([a-zA-Z0-9:_-]+)", raw)
        if not name:
            continue
        tag = name.group(1).lower()
        if raw.startswith("</"):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == tag:
                    del stack[index:]
                    break
        elif tag in _VOID_TAGS or raw.endswith("/>"):
            continue
        else:
            stack.append((tag, token.start()))
    for position in wanted[cursor:]:
        paths[position] = tuple(stack)
    return paths


def _date_shares_item(text: str, date_pos: int, anchor_pos: int, paths: dict | None = None) -> bool:
    """日期和链接是否属于同一条目：同一个容器，或同一父容器下的两个兄弟块。

    美联储是「日期 div + 标题 div」的兄弟结构，同属一个 row；而上一条新闻的日期与本条链接
    分属两个不同的 row，会被判为不同条目——这样不会把上一条的日期安到这一条头上。
    """
    if paths is None:
        paths = _element_paths(text, [date_pos, anchor_pos])
    left, right = _block_path(paths.get(date_pos, ())), _block_path(paths.get(anchor_pos, ()))
    if not left or not right:
        return False
    if left[-1] == right[-1]:
        return True
    if len(left) >= 2 and len(right) >= 2 and left[-2] == right[-2]:
        # 兄弟块也算同一条目（美联储是「日期 div + 标题 div」），但日期所在块里若已经有一条链接，
        # 那这个日期属于上一条新闻，不能借给当前链接。
        if re.search(r"<a\b", text[left[-1][1]:anchor_pos], re.I):
            return False
        return True
    return False


def _date_after(text: str, anchor_pos: int, start: int, stop: int,
                dates: list | None = None, paths: dict | None = None) -> str:
    """链接之后的日期：只取与这条链接同属一个条目的那一个（日期常被 <span> / <time> 包着）。"""
    if dates is None:
        dates = [(start + begin, start + end, stamp)
                 for begin, end, stamp in _date_candidates(text[start:stop])]
    for begin, end, stamp in dates:
        if start <= begin and end <= stop and _date_shares_item(text, begin, anchor_pos, paths):
            return stamp
    return ""


def _date_before(text: str, pos: int, span: int = 220, max_tail: int = 8,
                 dates: list | None = None, paths: dict | None = None) -> str:
    """链接**前面**的日期（美联储 / 欧洲央行 / 美国财政部的列表页都是日期在前、标题在后）。

    只认「日期与链接之间除了标签没有别的内容」的那一个：否则会把上一条新闻的日期
    安到这一条头上（上一条的日期与当前链接之间隔着上一条的标题）。
    """
    origin = max(0, pos - span)
    if dates is None:
        dates = _date_candidates(text[origin:pos])
        dates = [(origin + begin, origin + end, stamp) for begin, end, stamp in dates]
    near = [(begin, end, stamp) for begin, end, stamp in dates if origin <= begin and end <= pos]
    for begin, end, stamp in reversed(near[-3:]):
        if len(_clean(text[end:pos])) > max_tail:
            continue
        if not _date_shares_item(text, begin, pos, paths):
            continue
        return stamp
    return ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))).strip()


_ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_RSS_ITEM = re.compile(r"<item\b.*?</item>", re.I | re.S)
_RSS_FIELD = lambda tag: re.compile(rf"<{tag}\b[^>]*>(.*?)</{tag}>", re.I | re.S)
# 列表页里的导航 / 关于我们 / 订阅入口也有日期，但不是发文，一律丢掉。
_NAV_HINTS = ("about", "contact", "career", "sitemap", "search", "faq", "privacy",
              "accessibility", "subscribe", "login", "signin", "newsletter", "cookie")
# 列表页 URL 里出现这些片段，就当是发文入口（美国财政部很多标题不带「政策 / 通知」字样）。
_POLICY_URL_HINTS = ("press", "news", "statement", "monetary", "decision", "policy",
                     "announce", "release", "sanction", "date/")
_POLICY_HINT = ("政策", "通知", "公告", "决定", "意见", "办法", "规定", "规划", "方案", "措施",
                "statement", "press release", "decision", "announcement", "policy", "rate",
                "regulation", "proposal", "minutes", "speech")


def parse_rss(xml: str, source: dict) -> list[dict]:
    """官方 RSS → 条目列表。只取有日期、链接落在官方域名下的条目。"""
    out = []
    for chunk in _RSS_ITEM.findall(str(xml or "")):
        title = _clean(" ".join(_RSS_FIELD("title").findall(chunk)[:1]))
        link = _clean(" ".join(_RSS_FIELD("link").findall(chunk)[:1]))
        stamp = parse_date(" ".join(_RSS_FIELD("pubDate").findall(chunk)[:1]) or chunk)
        if title and link and stamp and is_official_url(link):
            out.append({"title": title, "url": link, "date": stamp, "source": source["key"]})
    return out


def parse_links(html: str, source: dict) -> list[dict]:
    """官方列表页 → 条目列表。

    政府 / 央行站点没有统一模板，这里只做保守解析：锚文本要像政策文件，
    日期取锚文本内、其后 240 个字符里、或**紧贴在链接前面**的日期
    （美联储 / 欧洲央行 / 美国财政部把日期放在标题左边），链接必须落在该机构的官方域名下。
    解析不到就返回空——宁可这条源标 empty，也不要编一个标题出来。
    """
    text = str(html or "")
    out = []
    anchors = list(_ANCHOR.finditer(text))
    dates = _date_candidates(text)
    paths = _element_paths(text, [match.start() for match in anchors] + [row[0] for row in dates])
    for index, match in enumerate(anchors):
        href, inner = match.group(1), _clean(match.group(2))
        url = href if href.startswith("http") else ""
        if not url or not is_official_url(url):
            continue
        if len(inner) < 6 or len(inner) > 140:
            continue
        low = inner.lower()
        url_low = url.lower()
        if not (any(hint in low for hint in _POLICY_HINT)
                or any(hint in url_low for hint in _POLICY_URL_HINTS)):
            continue
        if any(hint in low for hint in _NAV_HINTS) or any(hint in url_low for hint in _NAV_HINTS):
            continue
        # 日期只看「本条锚点到下一条锚点之间」：跨条目取日期会把下一条的日期安到这一条头上。
        stop = anchors[index + 1].start() if index + 1 < len(anchors) else len(text)
        stamp = (parse_date(inner)
                 or _date_after(text, match.start(), match.end(), min(match.end() + 240, stop), dates, paths)
                 or _date_before(text, match.start(), dates=dates, paths=paths))
        if not stamp:
            continue
        out.append({"title": inner, "url": url, "date": stamp, "source": source["key"]})
    return out


_AUTO_RULES = [
    (("fomc", "federal funds", "联邦基金", "加息", "降息", "target range"), "货币政策 · 美联储", "海外货币收紧"),
    (("ecb", "欧洲央行", "deposit facility", "拉加德", "lagarde"), "货币政策 · 美联储", "海外货币收紧"),
    (("boj", "日本央行", "植田", "ueda", "policy rate"), "货币政策 · 美联储", "海外货币收紧"),
    (("lpr", "mlf", "逆回购", "降准", "降息", "再贷款", "流动性", "公开市场"), "央行 · 流动性", "国内利率按兵不动"),
    (("房地产", "住房", "房贷", "现房", "预售", "保障房"), "地产与地方政策", "国内地产金融"),
    (("证监会", "再融资", "ipo", "发行上市", "退市", "并购重组", "减持"), "资本市场监管", "国内金融制度"),
    (("国债", "回购", "buyback", "赤字", "专项债", "关税", "tariff", "制裁", "sanction"), "财政 · 关税与债务", "美债长端流动性"),
    (("出口管制", "两用物项", "芯片", "半导体", "人工智能", "ai ", "反倾销"), "产业与科技政策", "科技与产业政策"),
    (("汇率", "外汇", "人民币", "currency", "exchange rate"), "汇率与外汇干预", "汇率与外汇干预"),
]


def classify_policy(title: str) -> dict:
    """官方标题 → 维度 / 主题 / 类型。只做关键词归类，不给影响方向。"""
    low = " " + str(title or "").lower()
    for needles, dimension, theme in _AUTO_RULES:
        if any(needle in low for needle in needles):
            return {"dimension": dimension, "theme": theme}
    return {"dimension": "产业与科技政策", "theme": "官方新发文"}


def _auto_release(item: dict, source: dict) -> dict:
    """官方抓取到的条目 → 台账结构（方向一律留空，等人工 / Agent 标注）。

    ``markets`` 三个市场都写 dir=0：官方原文只证明「这件事发生过」，
    不证明它对哪个市场是利好还是利空。方向标注是编辑动作，不在抓取里自动编。
    """
    classify = classify_policy(item["title"])
    return {
        "id": f"AUTO-{source['key']}-{item['date'].replace('-', '')}-{abs(hash(item['url'])) % 10000:04d}",
        "date": item["date"], "issuer": source["issuer"], "jurisdiction": source["jurisdiction"],
        "doctype": "官方发布", "kind": "发布", "topic": classify["dimension"].split(" · ")[0],
        "dimension": classify["dimension"], "theme": classify["theme"], "stance": "中性",
        "magnitude": "中", "horizon": "1-4 周",
        "title": item["title"][:120], "short": item["title"][:28],
        "summary": "官方源抓取条目；影响方向待标注，暂不计入指数分。",
        "markets": {market: _mx(0, 0.0, [], "官方原文，方向待标注") for market in POLICY_MARKETS},
        "keywords": [], "source": item["url"],
        "source_note": f'{source["issuer"]} · 官方原文 · {item["date"]}',
        "auto": True,
    }


def fetch_official(as_of=None, lookback_days: int = OFFICIAL_LOOKBACK_DAYS,
                   sources: list | None = None) -> dict:
    """抓官方源头（RSS / 列表页），返回条目与**每个源的核实状态**。

    单源失败不影响其它源；全部失败时 ``ok=0``，由调用方决定「只有台账、没有新抓取」。
    """
    end = _as_of(as_of)
    start = end - timedelta(days=max(1, lookback_days) - 1)
    rows, items = [], []
    for source in sources or OFFICIAL_SOURCES:
        if source.get("mode") not in ("rss", "html"):
            rows.append({"key": source["key"], "issuer": source["issuer"], "domain": source["domain"],
                         "entry": source["entry"], "status": "manual",
                         "note": "只登记入口，靠人工核对"})
            continue
        try:
            text = _get_text(source["entry"])
        except Exception as error:                 # 网络不可达 / TLS / DNS：如实记录
            rows.append({"key": source["key"], "issuer": source["issuer"], "domain": source["domain"],
                         "entry": source["entry"], "status": "error", "note": f"{type(error).__name__}: {error}"[:160]})
            continue
        # 入口页写着 HTML，实际返回的可能是 feed（政府站常把 /rss 挂在列表页下），按内容判一次。
        head = str(text)[:600].lower()
        is_feed = source.get("mode") == "rss" or "<rss" in head or "<feed" in head
        parsed = parse_rss(text, source) if is_feed else parse_links(text, source)
        fresh, seen = [], set()
        for item in parsed:
            stamp = datetime.strptime(item["date"], "%Y-%m-%d").date()
            if not (start <= stamp <= end) or item["url"] in seen:
                continue
            seen.add(item["url"])
            fresh.append(item)
            if len(fresh) >= OFFICIAL_ITEMS_PER_SOURCE:
                break
        items.extend(fresh)
        rows.append({"key": source["key"], "issuer": source["issuer"], "domain": source["domain"],
                     "entry": source["entry"], "status": "ok" if fresh else "empty",
                     "note": f"窗口内 {len(fresh)} 条" if fresh else "抓通了，窗口内没有新条目"})
    return {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "window": f"{start.isoformat()} 至 {end.isoformat()}",
        "items": items,
        "sources": rows,
        "ok": sum(1 for row in rows if row["status"] == "ok"),
        "empty": sum(1 for row in rows if row["status"] == "empty"),
        "error": sum(1 for row in rows if row["status"] == "error"),
        "manual": sum(1 for row in rows if row["status"] == "manual"),
    }


def load_official_cache(max_age_hours: float = CACHE_TTL_HOURS, path: Path | None = None) -> dict | None:
    """读官方源缓存（由 ``--refresh`` / CI 定时写入）。过期或不存在返回 None。"""
    path = path or CACHE_PATH
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        stamp = datetime.strptime(str(payload.get("fetched_at") or "")[:16], "%Y-%m-%d %H:%M")
    except (OSError, ValueError, TypeError):
        return None
    # 负值只可能来自时钟偏差（缓存文件的 UTC 时间比本机还新），按 0 处理，不当作有效历史。
    age_hours = max(0.0, (datetime.now(timezone.utc).replace(tzinfo=None) - stamp).total_seconds() / 3600)
    if age_hours > max_age_hours:
        return None
    payload["cache_age_hours"] = round(age_hours, 1)
    return payload


def write_official_cache(payload: dict, path: Path | None = None) -> Path:
    path = Path(path or CACHE_PATH)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def official_releases(payload: dict | None) -> list[dict]:
    """把抓取结果转成台账条目（只保留能落到官方域名的）。"""
    if not payload:
        return []
    out = []
    for item in payload.get("items") or []:
        source = _BY_KEY.get(item.get("source") or "")
        if not source or not is_official_url(item.get("url") or "") or not item.get("date"):
            continue
        out.append(_auto_release(item, source))
    return out


def _title_overlap(a: str, b: str, size: int = 4) -> bool:
    """两条标题是否高度重合（中文按 4 字切片）。用于避免官方抓取条目与台账重复计数。"""
    left, right = str(a or ""), str(b or "")
    if len(left) < size or len(right) < size:
        return left.strip() == right.strip() and bool(left.strip())
    grams = {left[i:i + size] for i in range(len(left) - size + 1)}
    return any(right[i:i + size] in grams for i in range(len(right) - size + 1))


def dedupe_auto(auto_releases: list, curated: list) -> list:
    """官方抓取条目去重：台账已经手工核过同一天、同维度或标题高度重合的，不再重复列。"""
    kept = []
    for item in auto_releases:
        stamp = item.get("date")
        duplicate = False
        for row in curated:
            if row.get("date") != stamp:
                continue
            if row.get("dimension") == item.get("dimension") or _title_overlap(row.get("title"), item.get("title")):
                duplicate = True
                break
        if not duplicate and all(item.get("url") != row.get("source") for row in kept):
            kept.append(item)
    return kept


def _release_date(release: dict) -> date:
    return datetime.strptime(str(release.get("date") or "")[:10], "%Y-%m-%d").date()


def horizon_days(release: dict) -> int:
    """影响期天数：台账里每条都声明了 horizon，缺失时按 30 天处理（不臆造更长的影响）。"""
    return _HORIZON_DAYS.get(str(release.get("horizon") or "").strip(), _DEFAULT_HORIZON_DAYS)


def active_until(release: dict) -> date:
    """这份政策「还管到哪天」。窗口外发文只要没过这个日期，就是仍在生效的存量。"""
    return _release_date(release) + timedelta(days=horizon_days(release))


def _span_text(days: int) -> str:
    """剩余 / 已过天数的可读写法：超过 3 个月按「约 N 个月」，避免「剩 1048 天」这种噪声。"""
    days = int(days or 0)
    return f"{days} 天" if days < 90 else f"约 {max(1, round(days / 30))} 个月"


def _remaining_text(days: int) -> str:
    """「剩 N 天 / 剩约 N 个月」：中文不留空格，避免「剩 约 4 个月」这种拼接痕迹。"""
    text = _span_text(days)
    return f"剩 {text}" if text[:1].isdigit() else f"剩{text}"


def _keyword_hit(title: str, keyword: str) -> bool:
    """ASCII 词按单词边界，中文按子串。与政策维度词库同一原则，避免 FedEx 误伤。"""
    if not title or not keyword:
        return False
    low = title.lower()
    kw = keyword.lower()
    if kw.isascii():
        core = re.escape(kw[:-1] if kw.endswith("*") else kw)
        tail = "" if kw.endswith("*") else r"(?![a-z0-9])"
        return re.search(rf"(?<![a-z0-9]){core}{tail}", low) is not None
    return kw in low


def _cell(release: dict, market: str) -> dict:
    raw = (release.get("markets") or {}).get(market) or {}
    return {
        "dir": int(raw.get("dir") or 0),
        "weight": float(raw.get("weight") if raw.get("weight") is not None else 1.0),
        "sectors": list(raw.get("sectors") or []),
        "channel": raw.get("channel") or "",
        "observed": bool(raw.get("observed")),
    }


def _contrib(release: dict, market: str) -> float:
    cell = _cell(release, market)
    if not cell["dir"]:
        return 0.0
    kind = _KIND_WEIGHT.get(release.get("kind") or "", 1.0)
    mag = _MAG_WEIGHT.get(release.get("magnitude") or "", 1.0)
    return cell["dir"] * cell["weight"] * kind * mag


def _mentions(brief: dict | None, release: dict, limit: int = 2) -> list[dict]:
    found, seen = [], set()
    keywords = release.get("keywords") or []
    for name, items in (brief or {}).items():
        for item in items or []:
            title = str((item or {}).get("title") or "").strip()
            if not title or title in seen:
                continue
            if not any(_keyword_hit(title, kw) for kw in keywords):
                continue
            seen.add(title)
            found.append({"title": title, "source": str(name or "")})
            if len(found) >= limit:
                return found
    return found


def _stock_line(release: dict) -> str:
    bits = []
    for name in POLICY_MARKETS:
        cell = _cell(release, name)
        if cell["dir"] == 0 and not cell["sectors"]:
            continue
        if cell["dir"] == 0:
            continue
        mark = "已观测" if cell["observed"] else "机制"
        bits.append(f"{name}{_dir_label(cell['dir'])}（{mark}）")
    if release.get("cross_asset"):
        bits.append(release["cross_asset"])
    return "；".join(bits)


def _driver_line(drivers: dict | None) -> str:
    """「托底 …；拉低 …」一行纯文本，供推送 / 页面 / 前端共用，避免三处各写一套。"""
    ups = [f"{item['theme']} {item['score']:+.2f}" for item in (drivers or {}).get("support") or []]
    downs = [f"{item['theme']} {item['score']:+.2f}" for item in (drivers or {}).get("pressure") or []]
    bits = []
    if ups:
        bits.append("托底 " + "、".join(ups))
    if downs:
        bits.append("拉低 " + "、".join(downs))
    return "；".join(bits)


def _market_reading(name: str, bias: str, support: list[str], pressure: list[str], observed: bool,
                    state: str = "window", carry_score: float = 0.0) -> str:
    """三市结论句。

    ``state`` 区分三种情形，避免「窗口内什么都没有」时还写「机制推演」自相矛盾：
    - ``window``：窗口内有新发文，按发文方向推演；
    - ``carry``：窗口内没有新发文，只有按影响期仍在生效的存量政策；
    - ``empty``：两者都没有，直接说不外推。
    """
    sup = "、".join(support[:3]) or "无明显受益方向"
    pre = "、".join(pressure[:3]) or "无明显承压方向"
    if state == "empty":
        return f"{name}窗口内没有新发文，也没有仍在生效的存量政策，不外推方向。"
    if state == "carry":
        head = (f"{name}近30日没有新发文，{bias}来自仍在生效的存量政策（按半权计入），"
                "属方向延续，不代表当日强弱。")
        return f"{head}受益看{sup}；承压看{pre}。"
    if observed:
        head = f"{name}{bias}，且含已观测的收盘反应。"
    elif bias == "中性" and support and pressure:
        head = f"{name}指数层面中性，内部对冲，不是单边。"
    elif bias == "中性":
        head = f"{name}指数层面中性。"
    else:
        head = f"{name}指数层面{bias}，属机制推演，不外推点位。"
    tail = ""
    if carry_score:
        tail = f"另有存量政策 {carry_score:+.2f} 分按半权计入，只说明这条线还有余温。"
    return f"{head}受益看{sup}；承压看{pre}。{tail}"


def _headline(payload: dict) -> str:
    """卡片首句（约 100 字）：先给「窗口内有没有新发文」，再给三市方向与存量补充。"""
    markets_txt = "、".join(f"{item['name']}{item['bias']}" for item in payload["markets"])
    carry = payload.get("carry_releases") or []
    if not payload["releases"]:
        if not carry:
            return (f"近{payload['days']}日（{payload['window_label']}）没有已核验的全球政策发布，"
                    "也没有仍在生效的存量政策，不外推对股票市场的影响。")
        newest = max(carry, key=lambda item: item.get("date") or "")
        latest = newest.get("date") or ""
        gap = newest.get("days_ago")
        when = f"最近一项 {latest[5:].replace('-', '/')}" if latest else "台账内最近一项"
        return (f"近{payload['days']}日（{payload['window_label']}）没有新核验的全球政策发布；"
                f"{len(carry)} 项存量政策仍在生效（{when}"
                + (f"，距今 {gap} 天" if isinstance(gap, int) else "")
                + f"），按半权给方向。三市：{markets_txt}。方向延续不等于当日强弱。")
    counts = payload["counts"]
    bits = [f"{counts['发布']} 项正式发布"]
    if counts["信号"]:
        bits.append(f"{counts['信号']} 项政策信号")
    if counts["生效"]:
        bits.append(f"{counts['生效']} 项窗口内生效")
    text = f"近{payload['days']}日（{payload['window_label']}）核验 " + "、".join(bits)
    if payload["pressure_events"]:
        text += "。压制主线：" + "、".join(item["short"] for item in payload["pressure_events"][:2])
    if payload["support_events"]:
        text += "。对冲支撑：" + "、".join(item["short"] for item in payload["support_events"][:2])
    text += "。三市：" + markets_txt
    text += "。已观测与机制推演分开，不外推未报道的指数点位。"
    if carry:
        text += f"另有 {len(carry)} 项存量政策仍在生效，按半权计入。"
    return text


# 官方信息源名称特征：当日抓取里来自这些源的标题，才算「官方动态」（待核验发文）。
_OFFICIAL_NAME_HINTS = ("国务院", "发改委", "发展改革委", "财政部", "商务部", "证监会", "人民银行", "央行",
                        "住建部", "住房城乡建设部", "统计局", "金融监管总局", "外汇局", "政府网",
                        "美联储", "federal reserve", "fomc", "欧洲央行", "ecb", "日本央行", "boj",
                        "treasury", "财政部", "sec", "联邦公报", "federal register", "g0v",
                        "govtrack", "equitystack", "政策发布", "政策解读")


def official_daily_candidates(brief: dict | None, as_of=None, limit: int = 6) -> list[dict]:
    """当日抓取里、来自官方信息源且像政策文件的标题，作为「待核验发文」线索。

    与台账的区别：这里没有核实发文日（快讯日期不等于发文日），所以**不计入窗口计分**，
    只在卡片里列出来，提示「官方源今天说了这些，需要核到原文再入库」。
    """
    stamp = _as_of(as_of).isoformat()
    out, seen = [], set()
    for name, items in (brief or {}).items():
        if not any(hint.lower() in str(name or "").lower() for hint in _OFFICIAL_NAME_HINTS):
            continue
        for item in items or []:
            title = str((item or {}).get("title") or "").strip()
            if not title or title in seen or len(title) < 8:
                continue
            low = title.lower()
            if not any(hint in low for hint in _POLICY_HINT):
                continue
            seen.add(title)
            out.append({"title": title[:110], "source": str(name or ""), "seen_on": stamp,
                        "status": "待核验发文", "url": str((item or {}).get("url") or "")})
            if len(out) >= limit:
                return out
    return out


def _official_summary(payload: dict | None, curated_rows: list, auto_rows: list,
                     candidates: list | None = None) -> dict:
    """来源核实汇总：谁抓通了、谁只给入口、台账条目是官方原文还是媒体转述。"""
    rows = [dict(row) for row in (payload or {}).get("sources") or []]
    if not rows:
        # 没有抓取结果时，仍把登记表列出来——读者至少知道该去哪核对。
        rows = [{"key": item["key"], "issuer": item["issuer"], "domain": item["domain"],
                 "entry": item["entry"],
                 "status": "manual" if item["mode"] == "manual" else "pending",
                 "note": item["note"]} for item in OFFICIAL_SOURCES]
    by_key = {row["key"]: row for row in rows}
    linked = []
    for release in curated_rows:
        source = official_source_for(release["issuer"])
        if not source:
            continue
        linked.append({
            "id": release["id"],
            "issuer": source["issuer"],
            "domain": source["domain"],
            "entry": source["entry"],
            "source_kind": source_kind(release["source"]),
            "status": by_key.get(source["key"], {}).get("status", "pending"),
        })
    return {
        "available": bool(payload),
        "fetched_at": (payload or {}).get("fetched_at") or "",
        "cache_age_hours": (payload or {}).get("cache_age_hours"),
        "window": (payload or {}).get("window") or "",
        "ok": (payload or {}).get("ok", 0),
        "empty": (payload or {}).get("empty", 0),
        "error": (payload or {}).get("error", 0),
        "manual": (payload or {}).get("manual", 0),
        "registered": len(OFFICIAL_SOURCES),
        "auto_items": len(auto_rows),
        "entries": linked,
        "failed": [row["issuer"] for row in rows if row.get("status") == "error"],
        "empty_sources": [row["issuer"] for row in rows if row.get("status") == "empty"],
        "manual_sources": [row["issuer"] for row in rows if row.get("status") == "manual"],
        "display": _official_display(payload, linked, auto_rows, candidates or []),
        "status_line": _official_status_line(payload, auto_rows),
    }


def _official_display(payload: dict | None, entries: list, auto_rows: list,
                      candidates: list | None = None) -> str:
    """页面上那一行「来源」：只给数字与源名，不写解释、不写操作提示。"""
    registered = len(OFFICIAL_SOURCES)
    official = sum(1 for row in entries if row["source_kind"] == "官方原文")
    media = sum(1 for row in entries if row["source_kind"] == "媒体转述")
    ledger = f"台账 {len(entries)} 条：官方原文 {official}、媒体转述 {media}"
    if not payload:
        text = f"官方入口 {registered} 个｜尚未抓取"
        if candidates:
            text += f'｜当日官方源 {len(candidates)} 条'
        return f"{text}｜{ledger}"
    bits = [f'官方入口 {registered} 个',
            f'抓通 {payload.get("ok", 0)} · 空 {payload.get("empty", 0)} · 失败 {payload.get("error", 0)}'
            f' · 仅入口 {payload.get("manual", 0)}',
            f'抓取 {(payload.get("fetched_at") or "—")} UTC']
    age = payload.get("cache_age_hours")
    if isinstance(age, (int, float)) and age > CACHE_TTL_HOURS:
        bits.append(f'缓存已过期 {int(age)} 小时')
    if auto_rows:
        bits.append(f'窗口内新发文 {len(auto_rows)} 条')
    if candidates:
        bits.append(f'当日官方源 {len(candidates)} 条')
    bits.append(ledger)
    return "｜".join(bits)


def _official_status_line(payload: dict | None, auto_rows: list) -> str:
    if not payload:
        return "官方源未抓取"
    bits = [f'官方源抓取 {payload.get("ok", 0)} 通']
    if payload.get("empty"):
        bits.append(f'{payload["empty"]} 通但无新条目')
    if payload.get("error"):
        bits.append(f'{payload["error"]} 个失败')
    if payload.get("manual"):
        bits.append(f'{payload["manual"]} 个只给入口')
    if auto_rows:
        bits.append(f'窗口内官方新发文 {len(auto_rows)} 条')
    return "，".join(bits)


def _score_layer(releases: list, market: str, weight: float = 1.0) -> float:
    """一层政策（新发文 / 存量）对某市场的指数分。

    同一主题、同一方向只取最强一条，其余按 +0.22 记确认项，避免同日组合拳把指数分乘三倍。
    ``weight`` 用于存量层整体打折。
    """
    groups: dict[tuple, list] = {}
    for release in releases:
        contrib = _contrib(release, market)
        if not contrib:
            continue
        sign = 1 if contrib > 0 else -1
        groups.setdefault((release["theme"], sign), []).append(contrib)
    total = 0.0
    for items in groups.values():
        items.sort(key=abs, reverse=True)
        sign = 1 if items[0] > 0 else -1
        total += items[0] + _CONFIRM * sign * (len(items) - 1)
    return total * weight


def _theme_scores(releases: list, market: str, weight: float = 1.0) -> dict[str, float]:
    """按主题汇总某市场的指数贡献（与 :func:`_score_layer` 同一套组内规则，只是不跨主题相加）。"""
    buckets: dict[str, dict[int, list]] = {}
    for release in releases:
        contrib = _contrib(release, market)
        if not contrib:
            continue
        sign = 1 if contrib > 0 else -1
        buckets.setdefault(release["theme"], {}).setdefault(sign, []).append(contrib)
    out = {}
    for theme, groups in buckets.items():
        subtotal = 0.0
        for sign, items in groups.items():
            items.sort(key=abs, reverse=True)
            subtotal += items[0] + _CONFIRM * sign * (len(items) - 1)
        out[theme] = subtotal * weight
    return out


def analyze_policy_market_impact(brief: dict | None = None, as_of=None,
                                 days: int = POLICY_WINDOW_DAYS,
                                 official: dict | None = None,
                                 use_cache: bool = True) -> dict:
    """近 ``days`` 日全球政策发布对 A 股 / 港股 / 美股的影响。

    ``brief`` 只用于给窗口内政策挂「当日快讯仍在定价」的标题，不改变指数分。
    没有命中台账的快讯不会被编成新的政策发布。

    ``official``：官方源抓取结果（``fetch_official()`` 的返回）。缺省时读
    ``policy_ledger_cache.json``（由 ``python3 policy_impact.py --refresh`` 或 CI 定时写入）。
    抓到的官方条目一律方向留空、只列不计分——「官方发了什么」由抓取证明，
    「对市场是利好还是利空」是编辑判断，不自动编。
    """
    start, end = policy_window_bounds(as_of, days)
    in_window, outside = [], []
    for release in _POLICY_RELEASES:
        stamp = _release_date(release)
        (in_window if start <= stamp <= end else outside).append(release)
    # 存量层：发文在窗口之前、但影响期（horizon）还没走完的政策。
    # 没有这一层，窗口一旦空转，整张卡片就只剩「没有发文」——而市场显然还在交易这些政策。
    # 两层要分开：今天仍在生效的存量才按半权计分；窗口前段生效、现在已经到期的只做说明，
    # 不再计入指数分——否则 8 月的一次讲话会一直「影响」到年底。
    # 只算「窗口开始时已经发文」的政策：台账里晚于窗口的发文不能倒推成历史影响。
    published = [release for release in outside if _release_date(release) < start]
    carry = [release for release in published if active_until(release) >= end]
    lapsed = [release for release in published if start <= active_until(release) < end]
    # 官方源头：自动抓到的窗口内发文单独一层，只列不计分。
    official_payload = official if official is not None else (load_official_cache() if use_cache else None)
    auto_all = official_releases(official_payload)
    auto_in_window = []
    for row in auto_all:
        stamp = _release_date(row)
        if start <= stamp <= end:
            auto_in_window.append(row)
    auto_in_window = dedupe_auto(sorted(auto_in_window, key=lambda item: item["date"], reverse=True),
                                 in_window)
    candidates = official_daily_candidates(brief, as_of=end)
    # 来源核实按台账全量走（不只窗口内）：读者要能逐条看到「这条引用的是官方原文还是媒体转述」。
    verify_rows = [{"id": row["id"], "date": row["date"], "issuer": row["issuer"], "source": row["source"]}
                   for row in _POLICY_RELEASES]

    # 存量清单按「对三市的影响大小」排：读者先看到还在压着指数的政策，而不是年限最长的那条。
    carry.sort(key=lambda item: (abs(sum(_contrib(item, market) for market in POLICY_MARKETS)),
                                 item["date"]), reverse=True)
    lapsed.sort(key=lambda item: item["date"], reverse=True)
    # 同一天里高量级在前；量级相同则保持台账顺序，结果可复算。
    in_window.sort(key=lambda item: (item["date"], {"高": 3, "中": 2, "低": 1}.get(item["magnitude"], 0)),
                   reverse=True)

    # 指数分拆两层：窗口内新发文全额，存量按半权；两层各自可复算，也各自可解释。
    scores = {market: _score_layer(in_window, market) for market in POLICY_MARKETS}
    scores_carry = {market: _score_layer(carry, market, _CARRY_DECAY) for market in POLICY_MARKETS}

    sector_acc = {market: {} for market in POLICY_MARKETS}
    observed = {market: False for market in POLICY_MARKETS}
    for release, layer_weight in [(item, 1.0) for item in in_window] + [(item, _CARRY_DECAY) for item in carry]:
        kind = _KIND_WEIGHT.get(release["kind"], 1.0)
        mag = _MAG_WEIGHT.get(release["magnitude"], 1.0)
        for market in POLICY_MARKETS:
            cell = _cell(release, market)
            if cell["observed"] and cell["dir"]:
                observed[market] = True
            for sector in cell["sectors"]:
                sdir = int(sector.get("dir") or 0)
                if not sdir:
                    continue
                name = sector["name"]
                sector_acc[market][name] = (sector_acc[market].get(name, 0.0)
                                            + sdir * cell["weight"] * kind * mag * layer_weight)

    drivers = {}
    for market in POLICY_MARKETS:
        win = _theme_scores(in_window, market)
        car = _theme_scores(carry, market, _CARRY_DECAY)
        merged = {theme: win.get(theme, 0.0) + car.get(theme, 0.0) for theme in set(win) | set(car)}
        drivers[market] = {
            "support": [{"theme": theme, "score": round(score, 2)}
                        for theme, score in sorted(merged.items(), key=lambda pair: -pair[1])
                        if score >= 0.2][:2],
            "pressure": [{"theme": theme, "score": round(score, 2)}
                         for theme, score in sorted(merged.items(), key=lambda pair: pair[1])
                         if score <= -0.2][:2],
        }

    markets = []
    for market in POLICY_MARKETS:
        ranked = sorted(sector_acc[market].items(), key=lambda pair: pair[1], reverse=True)
        support = [name for name, score in ranked if score >= 0.2][:3]
        pressure = [name for name, score in sorted(sector_acc[market].items(), key=lambda pair: pair[1]) if score <= -0.2][:3]
        total = scores[market] + scores_carry[market]
        active = bool(in_window or carry)
        state = "empty" if not active else ("window" if in_window else "carry")
        bias = _bias_label(total) if active else "—"
        markets.append({
            "name": market,
            "bias": bias,
            "score": round(total, 3),
            "score_window": round(scores[market], 3),
            "score_carry": round(scores_carry[market], 3),
            "bias_window": _bias_label(scores[market]) if in_window else "—",
            "bias_carry": _bias_label(scores_carry[market]) if carry else "—",
            "state": state,
            "window_count": sum(1 for item in in_window if _contrib(item, market)),
            "carry_count": sum(1 for item in carry if _contrib(item, market)),
            "observed": observed[market],
            "support": support,
            "pressure": pressure,
            "drivers": drivers[market],
            "driver_line": _driver_line(drivers[market]),
            "reading": _market_reading(market, bias if bias != "—" else "中性", support, pressure,
                                       observed[market], state, scores_carry[market]),
        })

    def _event_score(release: dict) -> float:
        return sum(_contrib(release, market) for market in POLICY_MARKETS)

    ranked_events = sorted(in_window, key=_event_score)
    pressure_events = [{"id": item["id"], "short": item["short"], "date": item["date"],
                        "score": round(_event_score(item), 3)}
                       for item in ranked_events if _event_score(item) < -0.2][:3]
    support_events = [{"id": item["id"], "short": item["short"], "date": item["date"],
                       "score": round(_event_score(item), 3)}
                      for item in reversed(ranked_events) if _event_score(item) > 0.2][:3]

    releases = []
    for release in in_window:
        releases.append({
            "id": release["id"],
            "date": release["date"],
            "issuer": release["issuer"],
            "jurisdiction": release["jurisdiction"],
            "doctype": release["doctype"],
            "kind": release["kind"],
            "topic": release["topic"],
            "dimension": release["dimension"],
            "theme": release["theme"],
            "stance": release["stance"],
            "magnitude": release["magnitude"],
            "horizon": release["horizon"],
            "title": release["title"],
            "short": release["short"],
            "summary": release["summary"],
            "stock_line": _stock_line(release),
            "cross_asset": release.get("cross_asset") or "",
            "source": release["source"],
            "source_note": release["source_note"],
            "score": round(_event_score(release), 3),
            "mentions": _mentions(brief, release),
            "layer": "新发文",
            "source_kind": source_kind(release["source"]),
            "official": official_source_for(release["issuer"]),
            "days_ago": max(0, (end - _release_date(release)).days),
            "active": active_until(release) >= end,
            "active_until": active_until(release).isoformat(),
            "remaining_days": max(0, (active_until(release) - end).days),
        })

    carry_releases = []
    for release in carry:
        carry_releases.append({
            "id": release["id"], "date": release["date"], "issuer": release["issuer"],
            "jurisdiction": release["jurisdiction"], "doctype": release["doctype"],
            "kind": release["kind"], "topic": release["topic"], "dimension": release["dimension"],
            "theme": release["theme"], "stance": release["stance"], "magnitude": release["magnitude"],
            "horizon": release["horizon"], "title": release["title"], "short": release["short"],
            "summary": release["summary"], "stock_line": _stock_line(release),
            "cross_asset": release.get("cross_asset") or "", "source": release["source"],
            "source_note": release["source_note"],
            # 存量按半权计入，这里给的是「按半权后的三市合成分」，不是新发文的原始贡献。
            "score": round(sum(_contrib(release, market) for market in POLICY_MARKETS) * _CARRY_DECAY, 3),
            "mentions": _mentions(brief, release),
            "layer": "存量",
            "days_ago": max(0, (end - _release_date(release)).days),
            "active": True,
            "active_until": active_until(release).isoformat(),
            "remaining_days": max(0, (active_until(release) - end).days),
        })

    # 主线归并：同一主题的窗口内发文 + 存量合成一条主线，读者先看「哪几条线在推」而不是 10 条流水。
    # 合成分 = 三市各自按组内规则算出的得分之和，所以「主线合成分相加 = 三市指数分相加」，可对账。
    theme_meta: dict[str, dict] = {}
    for release, layer in [(item, "window") for item in in_window] + [(item, "carry") for item in carry]:
        row = theme_meta.setdefault(release["theme"], {"theme": release["theme"], "window": 0,
                                                       "carry": 0, "shorts": [], "markets": []})
        row[layer] += 1
        if release["short"] not in row["shorts"]:
            row["shorts"].append(release["short"])
        for market in POLICY_MARKETS:
            if _contrib(release, market) and market not in row["markets"]:
                row["markets"].append(market)
    theme_net: dict[str, float] = {}
    for market in POLICY_MARKETS:
        win_scores = _theme_scores(in_window, market)
        carry_scores = _theme_scores(carry, market, _CARRY_DECAY)
        for theme in set(win_scores) | set(carry_scores):
            theme_net[theme] = (theme_net.get(theme, 0.0)
                                + win_scores.get(theme, 0.0) + carry_scores.get(theme, 0.0))
    themes = []
    for theme, row in theme_meta.items():
        net = theme_net.get(theme, 0.0)
        themes.append({
            "theme": theme,
            "window": row["window"],
            "carry": row["carry"],
            "net": round(net, 3),
            "direction": "压制" if net <= -0.2 else ("支撑" if net >= 0.2 else "对冲"),
            "shorts": row["shorts"][:2],
            "markets": row["markets"],
        })
    themes.sort(key=lambda item: abs(item["net"]), reverse=True)

    counts = {
        "in_window": len(releases),
        "发布": sum(item["kind"] == "发布" for item in releases),
        "信号": sum(item["kind"] == "信号" for item in releases),
        "生效": sum(item["kind"] == "生效" for item in releases),
        "outside": len(outside),
        "carry": len(carry_releases),
        "lapsed": len(lapsed),
        "auto": len(auto_in_window),
        "ledger": len(verify_rows),
        "official_entries": sum(1 for item in verify_rows if source_kind(item["source"]) == "官方原文"),
        "media_entries": sum(1 for item in verify_rows if source_kind(item["source"]) == "媒体转述"),
        "candidates": len(candidates),
    }
    latest = releases[0]["date"] if releases else ""
    latest_any = max((item["date"] for item in _POLICY_RELEASES), default="")
    days_since_latest = ((end - datetime.strptime(latest_any, "%Y-%m-%d").date()).days
                         if latest_any else None)
    if not releases:
        gap_note = (f"窗口 {start.isoformat()} 至 {end.isoformat()} 没有已核验发文，"
                    "未核实日期的快讯不当成政策发布。")
    elif latest and latest < end.isoformat():
        gap_note = (f"窗口截止 {end.isoformat()}，窗口内最新已核验发文是 {latest}；"
                    "未核实日期的快讯不当成政策发布。")
    else:
        gap_note = "窗口内发文均已核验日期；未核实日期的快讯不当成政策发布。"
    if latest_any:
        if isinstance(days_since_latest, int) and days_since_latest >= 0:
            gap_note += f"台账最近一次发文 {latest_any}（距今 {days_since_latest} 天）。"
        elif isinstance(days_since_latest, int):
            # 回看历史日期时，台账里的发文可能都晚于窗口，不能说成「多久以前」。
            gap_note += f"台账最新一条发文 {latest_any} 晚于本窗口（截至 {end.isoformat()}），不在统计内。"
    after = [item for item in outside if _release_date(item) > end]
    if after:
        sample = min(after, key=lambda item: item["date"])
        gap_note += f"另有 {len(after)} 条发文晚于窗口（最早 {sample['date']} {sample['short']}），不计入本次窗口。"
    before = [item for item in outside if _release_date(item) < start]
    if before:
        inactive = [item for item in before if item not in carry and item not in lapsed]
        parts = []
        if carry_releases:
            parts.append(f"{len(carry_releases)} 项影响期未走完（按半权计入存量层）")
        if lapsed:
            parts.append(f"{len(lapsed)} 项在窗口内到期（不计分）")
        if inactive:
            parts.append(f"{len(inactive)} 项影响期在窗口开始前已结束（不计分）")
        sample = min(before, key=lambda item: item["date"])
        gap_note += (f"窗口前共 {len(before)} 条（最早 {sample['date']} {sample['short']}）："
                     + "，".join(parts) + "，均不计为新发文。")

    sector_board = {"support": [], "pressure": []}
    folded: dict[str, dict] = {}
    for market, scores_map in sector_acc.items():
        for name, score in scores_map.items():
            row = folded.setdefault(name, {"name": name, "score": 0.0, "markets": []})
            row["score"] += score
            if market not in row["markets"]:
                row["markets"].append(market)
    ordered = sorted(folded.values(), key=lambda row: row["score"], reverse=True)
    sector_board["support"] = [
        {"name": row["name"], "score": round(row["score"], 3), "markets": row["markets"]}
        for row in ordered if row["score"] >= 0.3
    ][:4]
    sector_board["pressure"] = [
        {"name": row["name"], "score": round(row["score"], 3), "markets": row["markets"]}
        for row in sorted(folded.values(), key=lambda row: row["score"]) if row["score"] <= -0.3
    ][:4]

    payload = {
        "days": max(1, int(days)),
        "as_of": end.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "window_label": f"{start.isoformat()} 至 {end.isoformat()}",
        "counts": counts,
        "markets": markets,
        "releases": releases,
        "pressure_events": pressure_events,
        "support_events": support_events,
        "carry_releases": carry_releases,
        "auto_releases": [
            {key: row.get(key) for key in ("id", "date", "issuer", "jurisdiction", "dimension",
                                           "theme", "title", "short", "source", "source_note",
                                           "doctype", "kind")}
            | {"days_ago": max(0, (end - _release_date(row)).days)}
            for row in auto_in_window
        ],
        "official": _official_summary(official_payload, verify_rows, auto_in_window, candidates),
        "official_candidates": candidates,
        "lapsed_releases": [
            {"id": item["id"], "date": item["date"], "short": item["short"],
             "issuer": item["issuer"], "active_until": active_until(item).isoformat(),
             "days_ago": max(0, (end - _release_date(item)).days)}
            for item in lapsed
        ],
        "themes": themes,
        "sector_board": sector_board,
        "gap_note": gap_note,
        "latest_release": latest,
        "latest_any_release": latest_any,
        "days_since_latest": days_since_latest,
        "ledger_from": min(item["date"] for item in _POLICY_RELEASES),
        "ledger_to": max(item["date"] for item in _POLICY_RELEASES),
        "method": (
            "窗口为含当日的近 30 个自然日。指数分 = 各主题同向组的最强贡献 + 0.22×方向×(同向条数-1)。"
            "贡献 = 方向×市场权重×类型权重×量级权重。类型：发布 1.0 / 信号 0.65 / 生效 0.75；"
            "量级：高 1.4 / 中 1.0 / 低 0.6。方向 -2…+2。"
            "阈值：≥1.2 偏多，≥0.45 中性偏多，±0.45 内中性，≤-1.2 偏空。"
            "已观测只标注有公开收盘报道的市场；其余为机制推演，不外推点位。不构成投资建议。"
        ),
        "method_short": "指数分可复算：主题内取最强贡献，同向每多一条 +0.22。≥1.2 偏多 / ≥0.45 中性偏多 / ±0.45 内中性 / ≤-1.2 偏空。已观测与机制推演分开，不作为投资依据。",
    }
    payload["headline"] = _headline(payload)
    return payload


_STATE_NOTE = {
    "empty": "窗口内无新发文，也无存量政策，不外推",
    "carry": "仅存量政策仍生效，按半权计入",
}


def render_policy_impact_rows(impact: dict | None, esc, level: int = 2) -> str:
    """推送卡片用的表格行。``esc`` 为 HTML 转义函数。

    结构固定为：结论（主线 + 三市）→ 窗口内新发文 → 存量仍生效 → 板块传导。
    每一层都标清「这几分是谁给的」，读者不会把存量余温当成当日新催化。
    """
    impact = impact or {}

    def tag(text: str, cls: str = "tag") -> str:
        return f'<span class="{cls}">{esc(text)}</span>'

    def bias_cls(bias: str) -> str:
        if "空" in (bias or ""):
            return "tag-d"
        if "多" in (bias or ""):
            return "tag"
        return "tag-w"

    def layer_note(market: dict) -> str:
        if market.get("state") in _STATE_NOTE:
            return _STATE_NOTE[market["state"]]
        return "含已观测收盘反应" if market.get("observed") else "机制推演，不外推点位"

    counts = impact.get("counts") or {}
    carry = impact.get("carry_releases") or []
    rows = [
        f'<tr><td colspan="2" class="td-hdr">{tag("近30日全球政策 → 股市影响")} '
        f'<span class="sub">{esc(impact.get("window_label") or "含当日的近30个自然日")}，'
        f'新发文 {counts.get("in_window", 0)} 项，存量生效 {counts.get("carry", 0)} 项</span></td></tr>'
    ]
    if not impact:
        rows.append('<tr><td colspan="2" class="sub" style="padding:6px 0;">政策窗口影响未计算。</td></tr>')
        return "".join(rows)
    rows.append(
        f'<tr><td class="td-n">※</td><td class="td-t"><div>{esc(impact.get("headline") or "")}</div>'
        f'<div class="ev">{esc(impact.get("method_short") or "")}</div></td></tr>'
    )
    for market in impact.get("markets") or []:
        support = "、".join(market.get("support") or []) or "—"
        pressure = "、".join(market.get("pressure") or []) or "—"
        score = market.get("score", 0)
        score_txt = f"{score:+.2f}" if isinstance(score, (int, float)) else "—"
        split = ""
        if market.get("state") != "empty":
            win = market.get("score_window", 0) or 0
            car = market.get("score_carry", 0) or 0
            split = f"（新发文 {win:+.2f} / 存量 {car:+.2f}）"
        rows.append(
            f'<tr><td class="td-n">{esc(market.get("name") or "")}</td><td class="td-t">'
            f'<div>{tag(market.get("bias") or "—", bias_cls(market.get("bias") or ""))} '
            f'<span class="sub">指数分 {esc(score_txt)}{esc(split)}，{esc(layer_note(market))}</span></div>'
            f'<div class="ev">受益：{esc(support)}　承压：{esc(pressure)}</div>'
            + (f'<div class="ev">{esc(market.get("driver_line") or "")}</div>'
               if market.get("driver_line") else "")
            + (f'<div class="ev">{esc(market.get("reading") or "")}</div>' if level > 1 else "")
            + "</td></tr>"
        )
    themes = impact.get("themes") or []
    if themes and level > 0:
        lines = []
        for item in themes[:4 if level > 1 else 2]:
            cls = "tag-d" if item.get("direction") == "压制" else ("tag" if item.get("direction") == "支撑" else "tag-w")
            src = " / ".join(part for part in (
                f"新 {item.get('window', 0)}" if item.get("window") else "",
                f"存量 {item.get('carry', 0)}" if item.get("carry") else "") if part)
            lines.append(
                f'<div class="ev">{tag(item.get("direction") or "", cls)} {esc(item.get("theme") or "")}'
                f'　<span class="sub">{esc(src)}，合成分 {item.get("net", 0):+.2f}</span>'
                + (f'　{esc("、".join(item.get("shorts") or []))}' if level > 1 and item.get("shorts") else "")
                + "</div>"
            )
        rows.append(f'<tr><td class="td-n">主</td><td class="td-t"><div>{tag("主线归并", "tag-w")}'
                    f'　<span class="sub">同主题合并，存量半权，合成分可与三市对账</span></div>{"".join(lines)}</td></tr>')

    official = impact.get("official") or {}
    if level > 0:
        rows.append(
            f'<tr><td class="td-n">源</td><td class="td-t">'
            f'<div>{tag("来源", "tag-w")} <span class="sub">{esc(official.get("display") or "")}</span></div>'
            + (f'<div class="ev">抓取失败：{esc("、".join(official.get("failed") or []))}</div>'
               if official.get("failed") else "")
            + "</td></tr>"
        )

    releases = impact.get("releases") or []
    rows.append(
        f'<tr><td colspan="2" class="td-hdr">{tag("窗口内政策发布")} '
        f'<span class="sub">{counts.get("in_window", 0)} 项已核验，新的在前，'
        f'正式 {counts.get("发布", 0)} / 信号 {counts.get("信号", 0)} / 生效 {counts.get("生效", 0)}</span></td></tr>'
    )
    if not releases:
        rows.append(f'<tr><td colspan="2" class="sub" style="padding:6px 0;">'
                    f'{esc(impact.get("gap_note") or "窗口内无已核验政策发布。")}</td></tr>')
    limit_n = len(releases) if level > 1 else min(4, len(releases))
    for rank, item in enumerate(releases[:limit_n], 1):
        kind_cls = "tag-d" if item.get("stance") == "偏鹰" else ("tag" if item.get("stance") == "偏鸽" else "tag-w")
        mention = ""
        if level > 1 and item.get("mentions"):
            mention = "；".join(esc(hit.get("title") or "") for hit in item["mentions"][:1])
            mention = f'<div class="ev">当日快讯仍在定价：{mention}</div>'
        expired = "" if item.get("active", True) else f' <span class="tag-d">影响期已过</span>'
        rows.append(
            f'<tr><td class="td-n">{rank:02d}</td><td class="td-t">'
            f'<div>{tag((item.get("date") or "")[5:])} {tag(item.get("kind") or "", kind_cls)}{expired} '
            f'<span class="sub">{esc(item.get("issuer") or "")} · {esc(item.get("jurisdiction") or "")} · '
            f'{esc(item.get("topic") or "")} · {esc(item.get("horizon") or "")}</span></div>'
            f'<div>{esc(item.get("title") or "")}</div>'
            f'<div class="ev">{esc(item.get("stock_line") or "")}</div>'
            + (f'<div class="ev">{esc(item.get("summary") or "")}</div>'
               f'<div class="ev">依据：{esc(item.get("source_note") or "")}</div>' if level > 1 else "")
            + mention
            + "</td></tr>"
        )

    if carry and level > 0:
        rows.append(
            f'<tr><td colspan="2" class="td-hdr">{tag("存量政策仍在生效", "tag-w")} '
            f'<span class="sub">{len(carry)} 项，发文早于窗口、影响期未走完，按半权计入</span></td></tr>'
        )
        limit_c = len(carry) if level > 1 else min(3, len(carry))
        for rank, item in enumerate(carry[:limit_c], 1):
            kind_cls = "tag-d" if item.get("stance") == "偏鹰" else ("tag" if item.get("stance") == "偏鸽" else "tag-w")
            rows.append(
                f'<tr><td class="td-n">{rank:02d}</td><td class="td-t">'
                f'<div>{tag((item.get("date") or "")[5:])} {tag(item.get("kind") or "", kind_cls)} '
                f'<span class="sub">已过 {_span_text(item.get("days_ago", 0))}，影响期至 {esc(item.get("active_until") or "")}'
                f'（{_remaining_text(item.get("remaining_days", 0))}），{esc(item.get("issuer") or "")}</span></div>'
                f'<div>{esc(item.get("short") or item.get("title") or "")}</div>'
                f'<div class="ev">{esc(item.get("stock_line") or "")}</div>'
                + (f'<div class="ev">{esc(item.get("summary") or "")}</div>' if level > 1 else "")
                + "</td></tr>"
            )
        if len(carry) > limit_c:
            rows.append(f'<tr><td colspan="2" class="sub" style="padding:4px 0;">'
                        f'另有 {len(carry) - limit_c} 项存量政策未逐条列出，完整清单见 /api/policy。</td></tr>')
    auto_rows = impact.get("auto_releases") or []
    if auto_rows and level > 0:
        rows.append(
            f'<tr><td colspan="2" class="td-hdr">{tag("官方新发文（自动抓取）", "tag-w")} '
            f'<span class="sub">{len(auto_rows)} 条 · 官方原文 · 影响方向待标注，不计入指数分</span></td></tr>'
        )
        for rank, item in enumerate(auto_rows[:len(auto_rows) if level > 1 else min(3, len(auto_rows))], 1):
            rows.append(
                f'<tr><td class="td-n">{rank:02d}</td><td class="td-t">'
                f'<div>{tag((item.get("date") or "")[5:])} {tag("官方原文", "tag")} '
                f'<span class="sub">{esc(item.get("issuer") or "")} · {esc(item.get("dimension") or "")}'
                f' · 已过 {item.get("days_ago", 0)} 天</span></div>'
                f'<div>{esc(item.get("title") or "")}</div></td></tr>'
            )

    candidates = impact.get("official_candidates") or []
    if candidates and level > 1:
        rows.append(
            f'<tr><td colspan="2" class="td-hdr">{tag("当日官方源动态", "tag-w")} '
            f'<span class="sub">{len(candidates)} 条 · 待核验发文日，不入台账</span></td></tr>'
        )
        for item in candidates[:4]:
            rows.append(
                f'<tr><td class="td-n">·</td><td class="td-t"><div class="ev">'
                f'{esc(item.get("source") or "")}｜{esc(item.get("title") or "")}</div></td></tr>'
            )

    lapsed = impact.get("lapsed_releases") or []
    if lapsed:
        text = "、".join(f'{esc(item.get("date", "")[5:])} {esc(item.get("short") or "")}' for item in lapsed[:4])
        rows.append(f'<tr><td class="td-n">·</td><td class="td-t"><div>'
                    f'{tag("窗口内已到期", "tag-w")} '
                    f'<span class="sub">窗口前段生效、现已过期，不计分</span></div>'
                    f'<div class="ev">{text}</div></td></tr>')

    board = impact.get("sector_board") or {}
    if level > 1:
        rows.append(
            f'<tr><td colspan="2" class="td-hdr">{tag("板块传导")} '
            f'<span class="sub">跨政策加总，含存量半权，不是指数点位</span></td></tr>'
        )

        def _sec_line(rows_in: list, label: str, cls: str) -> str:
            if not rows_in:
                return ""
            text = "、".join(
                f"{esc(row['name'])}（{'/'.join(esc(m) for m in row.get('markets') or [])}）"
                for row in rows_in[:3]
            )
            return f'<div class="ev">{tag(label, cls)} {text}</div>'
        if board.get("support") or board.get("pressure"):
            rows.append(
                f'<tr><td class="td-n">板</td><td class="td-t">'
                f'{_sec_line(board.get("support") or [], "受益", "tag")}'
                f'{_sec_line(board.get("pressure") or [], "承压", "tag-d")}'
                f'</td></tr>'
            )
        else:
            rows.append(
                '<tr><td colspan="2" class="sub" style="padding:6px 0;">'
                '窗口内没有已核验政策贡献，板块传导不外推。</td></tr>'
            )
    rows.append(
        f'<tr><td colspan="2" class="sub" style="padding:6px 0;">{esc(impact.get("gap_note") or "")}'
        f' 以下历史知识库与推理链是对照，不计入近30日发文。</td></tr>'
    )
    return "".join(rows)


def page_section_html(impact: dict | None, as_of: str = "") -> str:
    """页面版「AI 政策深度」区块（index.html 静态快照与 app.js 运行时同一套结构）。

    页面快照过去是手写的，很容易和引擎口径漂移；改成由同一个 impact 生成后，
    「静态预览」和「实时结果」至少在结构上不会各说一套。
    """
    import html as _h

    impact = impact or {}

    def esc(text) -> str:
        return _h.escape(str(text if text is not None else ""), quote=True)

    def cell_cls(bias: str) -> str:
        if "空" in (bias or ""):
            return "press"
        if "多" in (bias or ""):
            return ""
        return "neutral"

    def chip(text: str, press: bool = False) -> str:
        return f'<mark class="press">{esc(text)}</mark>' if press else f'<mark>{esc(text)}</mark>'

    counts = impact.get("counts") or {}
    markets = impact.get("markets") or []
    us = next((item for item in markets if item.get("name") == "美股"), None)
    pill = (f'近{impact.get("days", 30)}日，新发文 {counts.get("in_window", 0)}，'
            f'存量 {counts.get("carry", 0)}，美股{us.get("bias", "") if us else ""}')
    headline = esc(impact.get("headline") or "")
    for short in sorted([item.get("short") for item in (impact.get("pressure_events") or [])
                         + (impact.get("support_events") or []) if item.get("short")],
                        key=len, reverse=True):
        press = any(item.get("short") == short for item in impact.get("pressure_events") or [])
        headline = headline.replace(esc(short), chip(short, press), 1)

    board_cells = ""
    for market in markets:
        state = market.get("state")
        note = _STATE_NOTE.get(state) or (
            "含已观测收盘反应" if market.get("observed") else "机制推演，不外推点位")
        split = ""
        if state != "empty":
            split = (f'（新发文 {market.get("score_window", 0):+.2f} / '
                     f'存量 {market.get("score_carry", 0):+.2f}）')
        board_cells += (
            f'<div class="impact-cell {cell_cls(market.get("bias") or "")}">'
            f'<div class="impact-name">{esc(market.get("name"))}，指数分 '
            f'{market.get("score", 0):+.2f}{esc(split)}</div>'
            f'<div class="impact-bias">{esc(market.get("bias"))}</div>'
            f'<div class="impact-score">{esc(note)}</div>'
            f'<div class="impact-split"><b>受益</b> {esc("、".join(market.get("support") or []) or "—")}'
            f'<br><b>承压</b> {esc("、".join(market.get("pressure") or []) or "—")}</div></div>'
        )

    def release_li(item: dict, carry: bool = False) -> str:
        press = item.get("stance") == "偏鹰"
        label_cls = "point-label press" if press else "point-label"
        if carry:
            meta = (f'已过 {_span_text(item.get("days_ago", 0))}，影响期至 {item.get("active_until")}'
                    f'（{_remaining_text(item.get("remaining_days", 0))}），{item.get("issuer")}')
            title = item.get("short") or item.get("title")
        else:
            meta = f'{item.get("issuer")}｜{item.get("topic")}｜{item.get("horizon")}'
            title = item.get("title")
        evidence = f'<li>· {esc(item.get("stock_line") or "")}</li>'
        if item.get("summary"):
            evidence += f'<li>· {esc(item.get("summary"))}</li>'
        badge = f'{item.get("stance") or ""} · {item.get("kind") or ""}'
        head = item.get("topic") if carry else item.get("short")
        return (f'<li><span class="point-no">{esc((item.get("date") or "")[5:])}</span>'
                f'<span class="{label_cls}">{esc(head)}</span>'
                f'<span>{chip(badge, press)}'
                f'<span class="sector-stat">{esc(meta)}</span>'
                f'<ul class="sector-evidence"><li>· {esc(title)}</li>{evidence}</ul></span></li>')

    official = impact.get("official") or {}
    rows = (f'<li><span class="point-no">源</span><span>{esc(official.get("display") or "")}</span></li>'
            + (f'<li><span class="point-no">断</span><span>{esc("、".join(official.get("failed") or []))}</span></li>'
               if official.get("failed") else ""))
    themes = impact.get("themes") or []
    if themes:
        rows += ('<li class="group-row"><span class="group-label">主线归并</span>'
                 '<span class="group-note" data-meta="1">同主题合并，存量半权，合成分可与三市对账</span></li>')
        for item in themes[:4]:
            press = item.get("direction") == "压制"
            src = " / ".join(part for part in (
                f'新 {item.get("window", 0)}' if item.get("window") else "",
                f'存量 {item.get("carry", 0)}' if item.get("carry") else "") if part)
            shorts = "、".join(item.get("shorts") or [])
            detail = f'<ul class="sector-evidence"><li>· {esc(shorts)}</li></ul>' if shorts else ""
            rows += (
                '<li><span class="point-no">主</span>'
                f'<span class="point-label{" press" if press else ""}">{esc(item.get("theme"))}</span>'
                f'<span>{chip(item.get("direction") or "", press)}'
                f'<span class="sector-stat">{esc(src)}，合成分 {item.get("net", 0):+.2f}</span>'
                f'{detail}</span></li>'
            )
    if markets:
        rows += ('<li class="group-row"><span class="group-label">三市逻辑</span>'
                 '<span class="group-note" data-meta="1">哪条主线在托、哪条在拉，指数分可复算</span></li>')
        for market in markets:
            press = "空" in (market.get("bias") or "")
            score = market.get("score", 0) or 0
            evidence = ""
            if market.get("driver_line"):
                evidence += f'<li>· {esc(market.get("driver_line"))}</li>'
            if market.get("reading"):
                evidence += f'<li>· {esc(market.get("reading"))}</li>'
            rows += (f'<li><span class="point-no">{esc(market.get("name"))}</span>'
                     f'<span class="point-label{" press" if press else ""}">{esc(market.get("bias") or "—")}</span>'
                     f'<span>{chip(f"指数分 {score:+.2f}")}'
                     f'<span class="sector-stat">新发文 {market.get("score_window", 0):+.2f} / '
                     f'存量 {market.get("score_carry", 0):+.2f}</span>'
                     f'<ul class="sector-evidence">{evidence}</ul></span></li>')
    rows += (f'<li class="group-row"><span class="group-label">窗口内政策发布</span>'
            f'<span class="group-note" data-meta="1">{esc(impact.get("window_label") or "")}，'
            f'新发文 {counts.get("in_window", 0)} 项，存量生效 {counts.get("carry", 0)} 项，新的在前</span></li>')
    releases = impact.get("releases") or []
    rows += "".join(release_li(item) for item in releases) or (
        f'<li><span class="point-no">·</span><span>窗口内没有已核验发文，未核实日期的快讯不编造成政策发布。</span></li>')
    carry = impact.get("carry_releases") or []
    if carry:
        rows += ('<li class="group-row"><span class="group-label">存量政策仍在生效</span>'
                 f'<span class="group-note" data-meta="1">{len(carry)} 项，发文早于窗口、影响期未走完，按半权计入</span></li>')
        rows += "".join(release_li(item, carry=True) for item in carry)

    def sec(names: list, label: str, press: bool = False) -> str:
        if not names:
            return ""
        cls = "point-label press" if press else "point-label"
        return (f'<li><span class="point-no">{"－" if press else "＋"}</span>'
                f'<span class="{cls}">{label}</span><span>{esc("、".join(names))}</span></li>')

    auto_rows = impact.get("auto_releases") or []
    if auto_rows:
        rows += ('<li class="group-row"><span class="group-label">官方新发文（自动抓取）</span>'
                 f'<span class="group-note" data-meta="1">{len(auto_rows)} 条 · 官方原文 · 影响方向待标注，不计入指数分</span></li>')
        for item in auto_rows:
            rows += (f'<li><span class="point-no">{esc((item.get("date") or "")[5:])}</span>'
                     f'<span class="point-label">{esc(item.get("issuer"))}</span>'
                     f'<span>{chip("官方原文")}'
                     f'<span class="sector-stat">{esc(item.get("dimension") or "")} · 已过 {item.get("days_ago", 0)} 天</span>'
                     f'<ul class="sector-evidence"><li>· {esc(item.get("title"))}</li></ul></span></li>')

    candidates = impact.get("official_candidates") or []
    if candidates:
        rows += ('<li class="group-row"><span class="group-label">当日官方源动态</span>'
                 f'<span class="group-note" data-meta="1">{len(candidates)} 条 · 待核验发文日，不入台账</span></li>')
        for item in candidates[:4]:
            rows += (f'<li><span class="point-no">·</span><span>'
                     f'{esc(item.get("source") or "")}｜{esc(item.get("title") or "")}</span></li>')

    lapsed = impact.get("lapsed_releases") or []
    if lapsed:
        text = "、".join(f'{item.get("date", "")[5:]} {item.get("short") or ""}' for item in lapsed[:4])
        rows += ('<li class="group-row"><span class="group-label">窗口内已到期</span>'
                 '<span class="group-note" data-meta="1">窗口前段生效、现已过期，不计分</span></li>'
                 f'<li><span class="point-no">·</span><span>{esc(text)}</span></li>')

    board = impact.get("sector_board") or {}
    support = [row.get("name") for row in board.get("support") or []]
    pressure = [row.get("name") for row in board.get("pressure") or []]
    rows += ('<li class="group-row"><span class="group-label">板块传导</span>'
             '<span class="group-note" data-meta="1">跨政策加总，含存量半权，不是指数点位</span></li>')
    rows += sec(support, "受益") + sec(pressure, "承压", press=True)

    return (
        '<section class="pixel-section" id="policyDeepSection">'
        '<div class="pixel-section-head"><div><h2>AI 政策深度</h2></div>'
        f'<span class="ai-pill" id="policyDeepPill">{esc(pill)}</span></div>'
        '<article class="insight-card" id="policyDeepCard">'
        f'<p class="policy-headline" id="policyDeepHeadline">{headline}</p>'
        f'<div class="impact-board" id="policyDeepBoard">{board_cells}</div>'
        f'<ul class="insight-points sector-points" id="policyDeepList">{rows}</ul>'
        '<div class="insight-foot" data-meta="1">'
        f'<span id="policyDeepFoot" data-meta="1">{esc(impact.get("gap_note") or "")}'
        '指数分可复算，仅供参考，不构成投资建议。</span></div>'
        '</article></section>'
    )


def verification_report() -> list[dict]:
    """台账逐条核对表：机构、官方入口、引用链接属于官方原文还是媒体转述。"""
    rows = []
    for release in _POLICY_RELEASES:
        source = official_source_for(release["issuer"])
        rows.append({
            "id": release["id"],
            "date": release["date"],
            "issuer": release["issuer"],
            "official_issuer": (source or {}).get("issuer") or "",
            "domain": (source or {}).get("domain") or "",
            "entry": (source or {}).get("entry") or "",
            "source": release["source"],
            "source_kind": source_kind(release["source"]),
            "checked": bool(source),
        })
    return rows


def render_index_section(path: str | Path = "index.html", as_of=None,
                         official: dict | None = None) -> str:
    """把 index.html 里的政策段整段替换成当前渲染结果（静态页面也吃同一份数据与来源核实）。"""
    target = Path(path)
    html = target.read_text(encoding="utf-8")
    marker = 'id="policyDeepSection"'
    index = html.find(marker)
    if index < 0:
        raise SystemExit(f"{target} 里找不到 {marker}，不做改动")
    start = html.rfind("<section", 0, index)
    end = html.find("</section>", index)
    if start < 0 or end < 0:
        raise SystemExit(f"{target} 的政策段边界不完整，不做改动")
    impact = analyze_policy_market_impact(as_of=as_of, official=official)
    section = page_section_html(impact, as_of=impact["end"])
    out = html[:start] + section + html[end + len("</section>"):]
    target.write_text(out, encoding="utf-8")
    return section


def main(argv=None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--refresh" in argv:
        payload = fetch_official()
        print(f'官方源抓取 {payload["fetched_at"]} · 窗口 {payload["window"]}')
        for row in payload["sources"]:
            print(f'  [{row["status"]:<6}] {row["issuer"]:<12} {row["domain"]:<24} {row["note"]}')
        print(f'  合计：ok={payload["ok"]} empty={payload["empty"]} error={payload["error"]} manual={payload["manual"]}')
        print(f'  窗口内官方条目 {len(payload["items"])} 条')
        for item in payload["items"]:
            print(f'    {item["date"]} {item["source"]:<5} {item["title"][:70]}')
        if "--write" in argv:
            if payload["ok"]:
                path = write_official_cache(payload)
                print(f'  已写入缓存 {path}')
            else:
                # 一个源都没抓通时不写文件：宁可保留上一份可用缓存，也不要用全空结果覆盖它。
                print("  未写入缓存：一个官方源都没抓通，保留上一份缓存不动。")
        if not payload["ok"]:
            print("  提示：一个官方源都没抓通（网络受限）。缓存未更新，卡片会如实显示「官方源未抓取」。")
        return 0 if payload["ok"] else 2
    if "--render-index" in argv:
        path = "index.html"
        for index, arg in enumerate(argv):
            if arg == "--render-index" and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                path = argv[index + 1]
        cache = load_official_cache(max_age_hours=10 ** 9)   # 静态快照：宁可用旧一点的抓取结果，也要把来源状态写上
        section = render_index_section(path, official=cache)
        note = "官方源缓存" if cache else "无官方源缓存（页面会写明未抓取）"
        print(f'已重生成 {path} 政策段（{len(section)} 字节，{note}）')
        return 0
    if "--verify" in argv:
        rows = verification_report()
        for row in rows:
            mark = "官方" if row["source_kind"] == "官方原文" else "媒体"
            print(f'{row["date"]} {mark} {row["issuer"]:<22} 入口 {row["domain"] or "—"}')
            print(f'         引用 {row["source"]}')
        bad = [row for row in rows if not row["checked"]]
        print(f'共 {len(rows)} 条，未匹配官方入口 {len(bad)} 条')
        return 0
    out = analyze_policy_market_impact(as_of="2026-09-23")
    print(out["headline"])
    for market in out["markets"]:
        print(market["name"], market["bias"], market["score"], market["support"], market["pressure"])
    for item in out["releases"]:
        print(item["date"], item["kind"], item["short"], item["score"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
