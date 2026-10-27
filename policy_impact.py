#!/usr/bin/env python3
"""近 30 日全球政策发布 → 股票市场影响。

「AI 政策深度」的主问题：滚动窗口内、已经核验过发文日的全球政策，如何作用到
A 股 / 港股 / 美股。台账只收有公开来源的发布、讲话信号和窗口内生效事项；
没有核实日期的快讯不写成政策发布。

指数分写死、可复算（见 :func:`_bias_label` 与模块常量）。有公开收盘报道的才标
「已观测」，其余是机制推演，不外推点位，不构成投资建议。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

POLICY_WINDOW_DAYS = 30
POLICY_MARKETS = ("A股", "港股", "美股")

# 类型权重：正式发布全额；讲话 / 指引只是信号；窗口内生效的是更早发文的实施，不重复计为新发文。
_KIND_WEIGHT = {"发布": 1.0, "信号": 0.65, "生效": 0.75}
# 量级权重：跨市场重要性，不是涨跌幅。
_MAG_WEIGHT = {"高": 1.4, "中": 1.0, "低": 0.6}
# 同一主题、同一方向每多一条，只加确认项，避免同日组合拳把指数分乘三倍。
_CONFIRM = 0.22
# 指数分 → 标签。与多空板块的 60/40 不是同一套口径，这里专用于政策窗口。
_BIAS_HI = 1.2
_BIAS_LO = 0.45


def _as_of(value) -> date:
    """窗口截止日。缺省为 UTC 当日（部署环境与简报日一致）。"""
    if value is None:
        return datetime.now(timezone.utc).date()
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


def _market_reading(name: str, bias: str, support: list[str], pressure: list[str], observed: bool) -> str:
    sup = "、".join(support[:3]) or "无明显受益方向"
    pre = "、".join(pressure[:3]) or "无明显承压方向"
    if observed:
        head = f"{name}{bias}，且含已观测的收盘反应。"
    elif bias == "中性" and support and pressure:
        head = f"{name}指数层面中性，内部对冲，不是单边。"
    elif bias == "中性":
        head = f"{name}指数层面中性。"
    else:
        head = f"{name}指数层面{bias}，属机制推演，不外推点位。"
    return f"{head}受益看{sup}；承压看{pre}。"


def _headline(payload: dict) -> str:
    if not payload["releases"]:
        return (f"近{payload['days']}日（{payload['window_label']}）没有已核验的全球政策发布，"
                "不外推对股票市场的影响。")
    counts = payload["counts"]
    bits = [f"{counts['发布']}项正式发布"]
    if counts["信号"]:
        bits.append(f"{counts['信号']}项政策信号")
    if counts["生效"]:
        bits.append(f"{counts['生效']}项窗口内生效")
    text = f"近{payload['days']}日（{payload['window_label']}）核验" + "、".join(bits)
    if payload["pressure_events"]:
        text += "。压制主线：" + "、".join(item["short"] for item in payload["pressure_events"][:2])
    if payload["support_events"]:
        text += "。对冲支撑：" + "、".join(item["short"] for item in payload["support_events"][:2])
    text += "。三市：" + "、".join(f"{item['name']}{item['bias']}" for item in payload["markets"])
    return text + "。已观测与机制推演分开，不外推未报道的指数点位。"


def analyze_policy_market_impact(brief: dict | None = None, as_of=None,
                                 days: int = POLICY_WINDOW_DAYS) -> dict:
    """近 ``days`` 日全球政策发布对 A 股 / 港股 / 美股的影响。

    ``brief`` 只用于给窗口内政策挂「当日快讯仍在定价」的标题，不改变指数分。
    没有命中台账的快讯不会被编成新的政策发布。
    """
    start, end = policy_window_bounds(as_of, days)
    in_window, outside = [], []
    for release in _POLICY_RELEASES:
        stamp = datetime.strptime(release["date"], "%Y-%m-%d").date()
        (in_window if start <= stamp <= end else outside).append(release)
    # 同一天里高量级在前；量级相同则保持台账顺序，结果可复算。
    in_window.sort(key=lambda item: (item["date"], {"高": 3, "中": 2, "低": 1}.get(item["magnitude"], 0)),
                   reverse=True)

    # 主题 × 方向：取最强一条，同向其余条目只加确认项。
    groups: dict[tuple, list] = {}
    for release in in_window:
        for market in POLICY_MARKETS:
            contrib = _contrib(release, market)
            if not contrib:
                continue
            sign = 1 if contrib > 0 else -1
            groups.setdefault((market, release["theme"], sign), []).append((contrib, release))

    scores = {market: 0.0 for market in POLICY_MARKETS}
    for (market, _theme, sign), items in groups.items():
        items.sort(key=lambda pair: abs(pair[0]), reverse=True)
        scores[market] += items[0][0] + _CONFIRM * sign * (len(items) - 1)

    sector_acc = {market: {} for market in POLICY_MARKETS}
    observed = {market: False for market in POLICY_MARKETS}
    for release in in_window:
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
                sector_acc[market][name] = sector_acc[market].get(name, 0.0) + sdir * cell["weight"] * kind * mag

    markets = []
    for market in POLICY_MARKETS:
        ranked = sorted(sector_acc[market].items(), key=lambda pair: pair[1], reverse=True)
        support = [name for name, score in ranked if score >= 0.2][:3]
        pressure = [name for name, score in sorted(sector_acc[market].items(), key=lambda pair: pair[1]) if score <= -0.2][:3]
        bias = _bias_label(scores[market]) if in_window else "—"
        markets.append({
            "name": market,
            "bias": bias,
            "score": round(scores[market], 3),
            "observed": observed[market],
            "support": support,
            "pressure": pressure,
            "reading": _market_reading(market, bias if bias != "—" else "中性", support, pressure, observed[market])
            if in_window else f"{market}窗口内无已核验政策，不外推。",
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
        })

    counts = {
        "in_window": len(releases),
        "发布": sum(item["kind"] == "发布" for item in releases),
        "信号": sum(item["kind"] == "信号" for item in releases),
        "生效": sum(item["kind"] == "生效" for item in releases),
        "outside": len(outside),
    }
    latest = releases[0]["date"] if releases else ""
    if latest and latest < end.isoformat():
        gap_note = (f"窗口截止 {end.isoformat()}，最新已核验发文是 {latest}。"
                    "此后没有写入台账的新发文；未核实日期的快讯不当成政策发布。")
    elif not releases:
        gap_note = "该窗口没有已核验发文。未核实日期的快讯不编造成政策发布。"
    else:
        gap_note = "窗口内发文均已核验日期。未核实日期的快讯不当成政策发布。"
    if outside:
        sample = min(outside, key=lambda item: item["date"])
        gap_note += f"台账另有 {len(outside)} 条在窗口外（如 {sample['date']} {sample['short']}），不重复计入。"

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
        "sector_board": sector_board,
        "gap_note": gap_note,
        "latest_release": latest,
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


def render_policy_impact_rows(impact: dict | None, esc, level: int = 2) -> str:
    """推送卡片用的表格行。``esc`` 为 HTML 转义函数。"""
    impact = impact or {}
    def tag(text: str, cls: str = "tag") -> str:
        return f'<span class="{cls}">{esc(text)}</span>'

    def bias_cls(bias: str) -> str:
        if "空" in (bias or ""):
            return "tag-d"
        if "多" in (bias or ""):
            return "tag"
        return "tag-w"

    rows = [
        f'<tr><td colspan="2" class="td-hdr">{tag("近30日全球政策 → 股市影响")} '
        f'<span class="sub">{esc(impact.get("window_label") or "含当日的近30个自然日")}</span></td></tr>'
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
        obs = "含已观测收盘反应" if market.get("observed") else "机制推演，不外推点位"
        score = market.get("score", 0)
        score_txt = f"{score:+.2f}" if isinstance(score, (int, float)) else "—"
        rows.append(
            f'<tr><td class="td-n">{esc(market.get("name") or "")}</td><td class="td-t">'
            f'<div>{tag(market.get("bias") or "—", bias_cls(market.get("bias") or ""))} '
            f'<span class="sub">指数分 {esc(score_txt)} · {esc(obs)}</span></div>'
            f'<div class="ev">受益：{esc(support)}　承压：{esc(pressure)}</div>'
            + (f'<div class="ev">{esc(market.get("reading") or "")}</div>' if level > 1 else "")
            + "</td></tr>"
        )
    releases = impact.get("releases") or []
    counts = impact.get("counts") or {}
    rows.append(
        f'<tr><td colspan="2" class="td-hdr">{tag("窗口内政策发布")} '
        f'<span class="sub">{counts.get("in_window", 0)} 项已核验 · 新的在前 · '
        f'正式 {counts.get("发布", 0)} / 信号 {counts.get("信号", 0)} / 生效 {counts.get("生效", 0)}</span></td></tr>'
    )
    if not releases:
        rows.append(f'<tr><td colspan="2" class="sub" style="padding:6px 0;">{esc(impact.get("gap_note") or "窗口内无已核验政策发布。")}</td></tr>')
    limit_n = len(releases) if level > 1 else min(4, len(releases))
    for rank, item in enumerate(releases[:limit_n], 1):
        kind_cls = "tag-d" if item.get("stance") == "偏鹰" else ("tag" if item.get("stance") == "偏鸽" else "tag-w")
        mention = ""
        if level > 1 and item.get("mentions"):
            mention = "；".join(esc(hit.get("title") or "") for hit in item["mentions"][:1])
            mention = f'<div class="ev">当日快讯仍在定价：{mention}</div>'
        rows.append(
            f'<tr><td class="td-n">{rank:02d}</td><td class="td-t">'
            f'<div>{tag((item.get("date") or "")[5:])} {tag(item.get("kind") or "", kind_cls)} '
            f'<span class="sub">{esc(item.get("issuer") or "")} · {esc(item.get("jurisdiction") or "")} · '
            f'{esc(item.get("topic") or "")} · {esc(item.get("horizon") or "")}</span></div>'
            f'<div>{esc(item.get("title") or "")}</div>'
            f'<div class="ev">{esc(item.get("stock_line") or "")}</div>'
            + (f'<div class="ev">{esc(item.get("summary") or "")}</div>'
               f'<div class="ev">依据：{esc(item.get("source_note") or "")}</div>' if level > 1 else "")
            + mention
            + "</td></tr>"
        )
    board = impact.get("sector_board") or {}
    if level > 1:
        rows.append(
            f'<tr><td colspan="2" class="td-hdr">{tag("板块传导")} '
            f'<span class="sub">跨政策加总 · 不是指数点位</span></td></tr>'
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


if __name__ == "__main__":
    out = analyze_policy_market_impact(as_of="2026-09-23")
    print(out["headline"])
    for market in out["markets"]:
        print(market["name"], market["bias"], market["score"], market["support"], market["pressure"])
    for item in out["releases"]:
        print(item["date"], item["kind"], item["short"], item["score"])
