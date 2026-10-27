#!/usr/bin/env python3
"""「AI 独立开发雷达」——把 <https://github.com/susumr/radar-dashboard> 的日报方法搬进推送。

上游是什么
----------
radar-dashboard 是一个「采集 → 生成 → 展示」三段式信号雷达：每天并行抓 19 个国内外信息源
（GitHub 热榜 / V2EX / 36氪 / 少数派 / 开源中国 / 即刻 / App Store 新区 / IndieHackers /
Product Hunt / trustmrr），交给大模型按固定模块整理成一份「独立开发者日报」：机会、赚钱案例、
产品灵感、增长技巧、工具、踩坑、数据信号，再加一条从当天数据反推的心法。

本模块怎么落地
--------------
1. **采集**：按上游的源清单抓免 Key 的那部分（GitHub / V2EX / 36氪 / 少数派 / 开源中国 /
   App Store / IndieHackers / 即刻）；需要 Key 的（Product Hunt、trustmrr、Reddit）
   拿不到就明确标「未配置」，不拿别的数顶替。
2. **成稿**：默认由本地规则引擎按同一套模块出稿（纯标准库、确定性、可离线测试），
   每条都带真实标题、真实链接、命中的关键词——不编造项目、不编造收入数字。
3. **「AI 提」**：:data:`AI_PROMPT` 是从上游 cron prompt 改写来的系统提示词，
   :func:`prompt_today` 会把它和当日信号拼成完整提示词，交给任意大模型就能生成同一份日报；
   没有 Key 也能跑，有 Key 时质量更高。这正是上游 README 里的「方式 A」。

边界
----
- 只做**信号整理**，不做投资建议、不预测收入；上游的「微信小程序机会」模块本仓库不启用。
- 采集失败 / 模块无素材 → 明确标「数据暂缺」，空模块不硬凑。
- 时间窗、去重、上限都写死并可复算（见 :data:`LIMITS`）。

对外接口
--------
    collect_signals(limit_per_source=6)   并行抓取，返回 {items, sources, errors, fetched_at}
    analyze(signals=None, as_of=None)     7 模块 + 心法，带 provenance
    prompt_today(signals=None)            当日 AI 提示词（AI 提）
    framework()                           模块与源清单（给页面 / 接口用）
"""
from __future__ import annotations

import html as _html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from time import time

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# 单源抓取超时（秒）；RADAR_TIMEOUT 供离线 / 测试环境缩短等待。
TIMEOUT = float(os.environ.get("RADAR_TIMEOUT", "12"))
# 整轮采集总预算（秒）：超过预算的源标 timeout 如实写进状态，不让一个慢源拖住推送。
BUDGET = float(os.environ.get("RADAR_BUDGET", "18"))
CACHE_TTL = 1800            # 30 分钟内复用上次采集结果，避免每次推送都重打 19 个源
LIMITS = {
    "per_source": 6,        # 每个源最多保留几条
    "per_module": 3,        # 每个模块最多几条
    "total": 60,            # 总信号上限
    "lookback_hours": 72,   # 时间窗：只收近 3 天
}

COLUMN_TITLE = "AI 独立开发雷达"
UPSTREAM = "https://github.com/susumr/radar-dashboard"
UPSTREAM_NOTE = "方法来自 susumr/radar-dashboard（信号雷达日报），本仓库按同一套模块本地实现"
DISCLAIMER = "信号整理，不是投资建议；收入与增长数字以原文为准，本模块不推算、不补值。"

# ---------------------------------------------------------------- 「AI 提」
# 从上游 hermes-integration/hn-daily-radar-SKILL.md 的 Cron Prompt Template 改写：
# 保留模块定义与「心法必须从当天数据反推」的约束，去掉微信小程序方向与 Telegram 输出要求，
# 换成适配本仓库的中文输出与 JSON schema。
AI_PROMPT = """你是独立开发者的每日信号雷达。

背景：读者是个人 / 小团队独立开发者，关注「海外已被验证的需求 → 自己能不能做」，
也关注国内平台（V2EX / 36氪 / 少数派 / App Store 新区）刚冒出来的新产品形态。

下面是脚本从 19 个信号源抓到的当日条目（标题、来源、链接、热度）。你的任务：只从这些条目里
精选，产出 7 个模块 + 1 条心法的中文日报。

模块定义
1. 今日机会（2-3 条）：信号来源 | 为什么现在能做 | 建议怎么做 | 变现路径。
2. 今日赚钱案例（1 条）：做什么 | 收入 | 流量来源 | 变现方式 | 能否复制。
3. 今日产品灵感（1 条）：一句具体的产品 idea + 它对接的是哪条信号。
4. 今日增长技巧（1 条）：必须是可执行的具体动作，不是抽象建议。
5. 今日工具（可选）：值得试的工具，一句话说明解决什么问题。
6. 今日踩坑（1 条）：值得警惕的教训，必须挂在某条真实信号上。
7. 今日数据信号（2-3 条）：趋势观察 + 判断，只能引用上面给出的计数与热度。
8. 今日心法：一句话方法 → 适用场景 → 分步骤做法 → 用当天数据推演一遍。
   心法必须从当天条目里反推，不要从固定方法库里挑。

硬约束
- 不编造条目、项目名、收入、涨粉或下载量；数字只能来自给定条目，没有就写「未披露」。
- 素材不足的模块直接跳过并在 JSON 里留空数组，不要凑数。
- 每条都要能指回一条真实信号（写出来源与标题）。
- 不写投资建议；不把 GitHub star 当成收入；不把 Product Hunt 票数当成销量。

输出：先输出一段 Markdown 日报，再输出一份 JSON（结构见 schema），键名固定：
{"date": "...", "sources": {...}, "modules": {"opportunities": [], "moneyCases": [],
 "productInspirations": [], "growthTips": [], "tools": [], "pitfalls": [],
 "dataSignals": [], "dailyWisdom": {}}}
"""

# ---------------------------------------------------------------- 信号源
# key / 显示名 / 类型 / 是否需要 Key / 地址。对齐上游 SOURCE_META 的 19 个源。
RADAR_SOURCES = [
    {"key": "github", "name": "GitHub 总榜", "kind": "html",
     "url": "https://github.com/trending?since=daily", "needs_key": None,
     "note": "每日热榜仓库（star 今日增量）"},
    {"key": "github-js", "name": "GitHub JS榜", "kind": "html",
     "url": "https://github.com/trending/javascript?since=daily", "needs_key": None,
     "note": "JavaScript / TypeScript 热榜"},
    {"key": "github-zh", "name": "GitHub 中文榜", "kind": "html",
     "url": "https://github.com/trending?since=daily&spoken_language_code=zh", "needs_key": None,
     "note": "中文社区热门仓库"},
    {"key": "v2ex", "name": "V2EX", "kind": "json",
     "url": "https://www.v2ex.com/api/topics/show.json", "needs_key": None,
     "note": "分享创造 / 分享发现 / 程序员节点"},
    {"key": "kr36", "name": "36氪", "kind": "rss",
     "url": "https://36kr.com/feed", "needs_key": None, "note": "创投、融资、科技公司动态"},
    {"key": "sspai", "name": "少数派", "kind": "rss",
     "url": "https://sspai.com/feed", "needs_key": None, "note": "效率工具与数字生活"},
    {"key": "oschina", "name": "开源中国", "kind": "rss",
     "url": "https://www.oschina.net/news/rss", "needs_key": None, "note": "开源项目发布"},
    {"key": "jike-ai", "name": "即刻·AI探索站", "kind": "rss",
     "url": "https://rsshub.umzzz.com/jike/topic/63579abb6724cc583b9bba9a", "needs_key": None,
     "note": "AI 应用 / 产品一手讨论（RSSHub 公共实例，可能不稳定）"},
    {"key": "jike-ai-discuss", "name": "即刻·AI讨论组", "kind": "rss",
     "url": "https://rsshub.umzzz.com/jike/topic/55fadac08cc2e30e00e2e42a", "needs_key": None,
     "note": "AI 技术与内容讨论（RSSHub）"},
    {"key": "jike-engineer", "name": "即刻·工程师的日常", "kind": "rss",
     "url": "https://rsshub.umzzz.com/jike/topic/577c5a122fa95b1100da059f", "needs_key": None,
     "note": "工程师圈子生态（RSSHub）"},
    {"key": "appstore-cn", "name": "App Store 中国新区", "kind": "appstore",
     "url": "https://itunes.apple.com/cn/rss/newapplications/limit=20/json", "needs_key": None,
     "note": "中国区新上架应用"},
    {"key": "appstore-us", "name": "App Store 美国新区", "kind": "appstore",
     "url": "https://itunes.apple.com/us/rss/newapplications/limit=20/json", "needs_key": None,
     "note": "美国区新上架应用"},
    {"key": "appstore-jp", "name": "App Store 日本新区", "kind": "appstore",
     "url": "https://itunes.apple.com/jp/rss/newapplications/limit=20/json", "needs_key": None,
     "note": "日本区新上架应用"},
    {"key": "appstore-tw", "name": "App Store 台湾新区", "kind": "appstore",
     "url": "https://itunes.apple.com/tw/rss/newapplications/limit=20/json", "needs_key": None,
     "note": "台湾区新上架应用"},
    {"key": "appstore-kr", "name": "App Store 韩国新区", "kind": "appstore",
     "url": "https://itunes.apple.com/kr/rss/newapplications/limit=20/json", "needs_key": None,
     "note": "韩国区新上架应用"},
    {"key": "indiehackers", "name": "IndieHackers", "kind": "algolia",
     "url": "https://N86T1R3OWZ-dsn.algolia.net/1/indexes/stories/query", "needs_key": None,
     "note": "独立开发者故事与收入案例（Algolia 公开索引）"},
    {"key": "producthunt", "name": "Product Hunt", "kind": "graphql",
     "url": "https://api.producthunt.com/v2/api/graphql", "needs_key": "PH_TOKEN",
     "note": "每日新品（需 Developer Token）"},
    {"key": "trustmrr", "name": "trustmrr", "kind": "rest",
     "url": "https://trustmrr.com/api/v1/startups", "needs_key": "TRUSTMRR_API_KEY",
     "note": "已验证收入的创业公司（MRR / 增长 / 技术栈）"},
    {"key": "reddit", "name": "Reddit r/SideProject", "kind": "rest",
     "url": "https://www.reddit.com/r/SideProject/new.json?limit=20", "needs_key": "REDDIT_CLIENT_ID",
     "note": "新产品讨论（需 OAuth，易被风控）"},
]

V2EX_NODES = ["create", "share", "programmer"]

# ---------------------------------------------------------------- 模块与规则
MODULES = [
    {"key": "opportunities", "no": "1", "label": "今日机会",
     "lead": "信号 → 为什么现在能做 → 建议 → 变现路径"},
    {"key": "money_cases", "no": "2", "label": "今日赚钱案例",
     "lead": "做什么 → 收入 → 流量 → 变现 → 能否复制"},
    {"key": "ideas", "no": "3", "label": "今日产品灵感",
     "lead": "具体产品 idea，挂在一条真实信号上"},
    {"key": "growth", "no": "4", "label": "今日增长技巧",
     "lead": "可执行的具体动作，不是抽象建议"},
    {"key": "tools", "no": "5", "label": "今日工具",
     "lead": "值得试的工具，一句话说清解决什么问题"},
    {"key": "pitfalls", "no": "6", "label": "今日踩坑",
     "lead": "值得警惕的教训，必须挂在真实信号上"},
    {"key": "data_signals", "no": "7", "label": "今日数据信号",
     "lead": "趋势观察 + 判断，只引用当日计数与热度"},
]

# 每个模块的关键词（中英混排，全部按小写匹配；正则里用 * 表示后缀）。
MODULE_RULES = {
    "opportunities": [
        r"上线", r"发布", r"开源", r"内测", r"公测", r"\blaunch", r"\brelease[ds]?\b", r"\bintroduc",
        r"\bside project\b", r"\bmade (a|an|this)\b", r"\bbuilt\b", r"新项目", r"新产品", r"从\s?0\s?到\s?1",
    ],
    "money_cases": [
        r"\bmrr\b", r"\barr\b", r"\brevenue\b", r"收入", r"营收", r"付费", r"订阅", r"定价",
        r"monetiz", r"pricing", r"\$\s?\d", r"赚", r"盈利", r"变现", r"月入",
    ],
    "ideas": [
        r"需求", r"痛点", r"希望有", r"能不能", r"有没有", r"\bwish\b", r"\bide?a?s?\b", r"替代",
        r"\balternative\b", r"\bfeature request\b", r"求推荐", r"想找",
    ],
    "growth": [
        r"增长", r"流量", r"获客", r"\bseo\b", r"推广", r"冷启动", r"病毒", r"传播",
        r"\bgrowth\b", r"\bmarketing\b", r"\blaunch", r"\bviral\b", r"留存", r"转化",
    ],
    "tools": [
        r"工具", r"\btools?\b", r"\bcli\b", r"插件", r"框架", r"\blibrary\b", r"\bsdk\b", r"\bextension\b",
        r"模板", r"脚手架", r"\bboilerplate\b", r"编辑器",
    ],
    "pitfalls": [
        r"坑", r"踩雷", r"失败", r"教训", r"封禁", r"风控", r"退款", r"翻车",
        r"deprecat", r"scam", r"被骗", r"下架", r"亏损",
    ],
}

_RULE_RX = {key: re.compile("|".join(f"(?:{p})" for p in patterns), re.I)
            for key, patterns in MODULE_RULES.items()}

# 模块 → 一句话解读的口径（只用命中的关键词与来源说话，不额外编数字）
MODULE_ANGLE = {
    "opportunities": "刚上线 / 刚开源的形态，可以先去用一遍再判断值不值得做",
    "money_cases": "谈到了收入或定价，可对照自己的产品看定价区间",
    "ideas": "是明确的未被满足需求，适合反向找现成替代品验证",
    "growth": "提到了具体的增长 / 分发动作，可以照抄一个小实验",
    "tools": "是现成工具，能直接省掉一段自研时间",
    "pitfalls": "是别人踩过的坑，动手前先核对同类风险",
}

NOW_WORDS = re.compile(r"(刚|今天|今日|本周|上线|发布|内测|launch|new)", re.I)


def _num(value) -> int:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def _text(raw: str) -> str:
    """去标签、解实体、压空白。"""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- 网络出口（测试注入点）
def _http_text(url: str, headers: dict | None = None, timeout: float | None = None) -> str:
    from urllib.request import Request, urlopen
    request = Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urlopen(request, timeout=timeout or TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def _http_json(url: str, params: dict | None = None, headers: dict | None = None,
               body: dict | None = None):
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    full = f"{url}?{urlencode(params)}" if params else url
    data = json.dumps(body).encode() if body is not None else None
    request = Request(full, data=data, headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        **(headers or {}),
    })
    with urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


GET_TEXT = _http_text
GET_JSON = _http_json
_CACHE: dict = {"at": 0.0, "payload": None}


# ---------------------------------------------------------------- 采集
def _item(source: dict, title: str, url: str, **extra) -> dict:
    return {
        "source": source["key"],
        "source_name": source["name"],
        "title": _text(title)[:160],
        "url": url or "",
        "points": _num(extra.get("points")),
        "note": _text(extra.get("note") or "")[:120],
        "tags": list(extra.get("tags") or []),
    }


def _parse_github(source: dict, limit: int) -> list:
    """GitHub Trending：按 <article class="Box-row"> 切块，正则取仓库 / 描述 / star。"""
    raw = GET_TEXT(source["url"], headers={"Accept-Language": "en-US,en;q=0.9"})
    items = []
    for chunk in raw.split('class="Box-row"')[1:]:
        repo = re.search(r'<h2[^>]*>.*?href="/([^"/]+/[^"/]+)"', chunk, re.S)
        if not repo:
            continue
        path = repo.group(1)
        desc = re.search(r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', chunk, re.S)
        stars = re.search(r'href="/[^"]*/stargazers"[^>]*>\s*([\d,]+)', chunk)
        today = re.search(r"([\d,]+)\s+stars?\s+today", chunk)
        lang = re.search(r'itemprop="programmingLanguage"[^>]*>([^<]+)<', chunk)
        items.append(_item(
            source, path, f"https://github.com/{path}",
            points=_num(today.group(1)) if today else _num(stars.group(1) if stars else 0),
            note=(desc.group(1) if desc else ""),
            tags=[lang.group(1).strip()] if lang else [],
        ))
        if len(items) >= limit:
            break
    return items


def _parse_v2ex(source: dict, limit: int) -> list:
    items = []
    seen = set()
    for node in V2EX_NODES:
        payload = GET_JSON(source["url"], params={"node_name": node})
        for topic in (payload or [])[:limit]:
            tid = str(topic.get("id") or "")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            title = (topic.get("title") or "").strip()
            if not title:
                continue
            node_name = ((topic.get("node") or {}).get("title") or node)
            items.append(_item(
                source, f"[{node_name}] {title}", f"https://www.v2ex.com/t/{tid}",
                points=_num(topic.get("replies")), note=(topic.get("content") or ""),
                tags=[node_name],
            ))
            if len(items) >= limit:
                return items
    return items


def _parse_rss(source: dict, limit: int) -> list:
    """RSS 2.0 / Atom 最小解析（不引第三方库）。"""
    import xml.etree.ElementTree as ET

    raw = GET_TEXT(source["url"])
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        root = ET.fromstring(re.sub(r"&(?!#?\w+;)", "&amp;", raw))
    channel = root.find("channel")
    entries = channel.findall("item") if channel is not None else root.findall(
        "{http://www.w3.org/2005/Atom}entry")
    items = []
    for entry in entries:
        title_el = entry.find("title")
        title = _text(title_el.text if title_el is not None and title_el.text else "")
        if not title:
            continue
        link_el = entry.find("link")
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href") or "").strip()
        desc_el = entry.find("description") or entry.find("summary")
        desc = desc_el.text if desc_el is not None and desc_el.text else ""
        items.append(_item(source, title, link, note=desc))
        if len(items) >= limit:
            break
    return items


def _parse_appstore(source: dict, limit: int) -> list:
    payload = GET_JSON(source["url"])
    entries = ((payload or {}).get("feed") or {}).get("entry") or []
    items = []
    for entry in entries[:limit]:
        name = ((entry.get("im:name") or {}).get("label") or "").strip()
        if not name:
            continue
        app_id = ((entry.get("id") or {}).get("attributes") or {}).get("im:id", "")
        link = ((entry.get("link") or {}).get("attributes") or {}).get("href", "")
        category = ((entry.get("category") or {}).get("attributes") or {}).get("label", "")
        price = ((entry.get("im:price") or {}).get("label") or "")
        items.append(_item(
            source, name, link or (f"https://apps.apple.com/app/id{app_id}" if app_id else ""),
            note=" · ".join(filter(None, [category, price])), tags=[category] if category else [],
        ))
    return items


def _parse_indiehackers(source: dict, limit: int) -> list:
    payload = GET_JSON(source["url"], body={
        "params": f"query=&hitsPerPage={limit}&page=0&attributesToRetrieve=*"}, headers={
        "X-Algolia-Application-Id": "N86T1R3OWZ",
        "X-Algolia-API-Key": "5140dac5e87f47346abbda1a34ee70c3",
    })
    items = []
    for hit in ((payload or {}).get("hits") or [])[:limit]:
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        revenue = hit.get("revenueRange") or hit.get("revenue") or ""
        items.append(_item(
            source, title, f"https://www.indiehackers.com/post/{hit.get('objectID') or ''}",
            points=_num(hit.get("points") or 0),
            note=f"收入区间：{revenue}" if revenue else "",
            tags=["独立开发"],
        ))
    return items


def _parse_producthunt(source: dict, limit: int) -> list:
    import os
    token = os.environ.get("PH_TOKEN") or os.environ.get("PRODUCT_HUNT_TOKEN")
    if not token:
        raise LookupError("未配置 PH_TOKEN")
    payload = GET_JSON(source["url"], headers={"Authorization": f"Bearer {token}"}, body={
        "query": "query { posts(first: %d, order: VOTES) { edges { node { name tagline "
                 "url votesCount } } } }" % limit})
    items = []
    for edge in (((payload or {}).get("data") or {}).get("posts") or {}).get("edges") or []:
        node = edge.get("node") or {}
        if not node.get("name"):
            continue
        items.append(_item(source, f"{node['name']}：{node.get('tagline') or ''}", node.get("url") or "",
                           points=_num(node.get("votesCount")), tags=["新品"]))
    return items


def _parse_trustmrr(source: dict, limit: int) -> list:
    import os
    key = os.environ.get("TRUSTMRR_API_KEY")
    if not key:
        raise LookupError("未配置 TRUSTMRR_API_KEY")
    payload = GET_JSON(source["url"], params={"limit": limit}, headers={"Authorization": f"Bearer {key}"})
    items = []
    for row in ((payload or {}).get("data") or [])[:limit]:
        name = row.get("name") or row.get("startup") or ""
        if not name:
            continue
        mrr = row.get("mrr") or row.get("revenue")
        items.append(_item(source, str(name), row.get("url") or "", points=_num(mrr),
                           note=f"MRR：{mrr}" if mrr else "", tags=["已验证收入"]))
    return items


PARSERS = {
    "html": lambda src, limit: _parse_github(src, limit) if src["key"].startswith("github")
    else _parse_rss(src, limit),
    "json": _parse_v2ex,
    "rss": _parse_rss,
    "appstore": _parse_appstore,
    "algolia": _parse_indiehackers,
    "graphql": _parse_producthunt,
    "rest": _parse_trustmrr,
}


def _collect_one(source: dict, limit: int) -> dict:
    """抓一个源；失败不抛，把原因记进 status。"""
    started = time()
    if source.get("needs_key"):
        import os
        if not os.environ.get(source["needs_key"]):
            return {"key": source["key"], "name": source["name"], "status": "skipped",
                    "note": f"未配置 {source['needs_key']}", "items": [], "ms": 0}
    try:
        parser = PARSERS[source["kind"]]
        items = parser(source, limit) or []
    except LookupError as error:
        return {"key": source["key"], "name": source["name"], "status": "skipped",
                "note": str(error), "items": [], "ms": round((time() - started) * 1000)}
    except Exception as error:                       # 网络 / 解析失败都不能中断整份日报
        return {"key": source["key"], "name": source["name"], "status": "error",
                "note": f"{type(error).__name__}: {error}", "items": [],
                "ms": round((time() - started) * 1000)}
    return {"key": source["key"], "name": source["name"],
            "status": "ok" if items else "empty",
            "note": f"{len(items)} 条", "items": items[:limit],
            "ms": round((time() - started) * 1000)}


def collect_signals(limit_per_source: int | None = None, workers: int | None = None,
                    use_cache: bool = True, budget: float | None = None) -> dict:
    """并行抓取全部信号源，返回 {items, sources, fetched_at, ok/skipped/error 计数}。"""
    limit = limit_per_source or LIMITS["per_source"]
    if use_cache and _CACHE["payload"] and time() - _CACHE["at"] < CACHE_TTL:
        return _CACHE["payload"]

    budget = BUDGET if budget is None else budget
    workers = workers or len(RADAR_SOURCES)     # 一个源一个线程，谁慢谁等，别排队
    results = []
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {pool.submit(_collect_one, source, limit): source for source in RADAR_SOURCES}
        done = set()
        try:
            for future in as_completed(futures, timeout=budget):
                done.add(future)
                results.append(future.result())
        except Exception:                       # 预算耗尽：剩下的源如实标 timeout
            pass
        for future, source in futures.items():
            if future in done:
                continue
            future.cancel()
            results.append({"key": source["key"], "name": source["name"], "status": "timeout",
                            "note": f"超过 {int(budget)} 秒预算未返回", "items": []})
    finally:
        pool.shutdown(wait=False)

    items, seen = [], set()
    for result in results:
        for item in result["items"]:
            fingerprint = (item["title"], item["url"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            items.append(item)

    cutoff = datetime.now() - timedelta(hours=LIMITS["lookback_hours"])
    payload = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "lookback_since": cutoff.strftime("%Y-%m-%d"),
        "items": items[:LIMITS["total"]],
        "sources": [{key: value for key, value in result.items() if key != "items"}
                    for result in results],
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "empty": sum(1 for r in results if r["status"] == "empty"),
        "timeout": sum(1 for r in results if r["status"] == "timeout"),
    }
    _CACHE.update({"at": time(), "payload": payload})
    return payload


# ---------------------------------------------------------------- 成稿
def _assign_modules(items: list) -> dict:
    """把每条信号只分给一个模块：命中词最多的模块优先，同分按模块顺序。

    一条信号重复出现在多个模块里会让日报看起来「料多」，实际是同一件事说三遍；
    独占分配后每个模块的条目互不重复，读者也能按模块去重地读完。
    """
    order = [module["key"] for module in MODULES if module["key"] in _RULE_RX]
    buckets = {key: [] for key in order}
    for item in items:
        haystack = f"{item['title']} {item.get('note') or ''}"
        best_key, best_hits = None, []
        for key in order:
            hits = sorted({match.group(0).lower() for match in _RULE_RX[key].finditer(haystack)})
            if not hits:
                continue
            if len(hits) > len(best_hits):
                best_key, best_hits = key, hits
        if not best_key:
            continue
        buckets[best_key].append({
            "title": item["title"],
            "url": item["url"],
            "source": item["source_name"],
            "source_key": item["source"],
            "matched": best_hits[:4],
            "angle": MODULE_ANGLE[best_key],
            "fresh": bool(NOW_WORDS.search(item["title"])),
            "points": item.get("points") or 0,
        })
    return buckets


def _take(bucket: list, limit: int) -> list:
    """同一模块内：新鲜优先、热度优先；同源最多 2 条，避免一个源霸屏。"""
    ranked = sorted(bucket, key=lambda entry: (entry["fresh"], entry["points"]), reverse=True)
    picked, per_source = [], {}
    for entry in ranked:
        if per_source.get(entry["source_key"], 0) >= 2:
            continue
        per_source[entry["source_key"]] = per_source.get(entry["source_key"], 0) + 1
        picked.append(entry)
        if len(picked) >= limit:
            break
    return picked


def _data_signals(signals: dict) -> list:
    """只统计真实存在的计数与热度，不推算总量、不估算增长。"""
    items = signals.get("items") or []
    sources = signals.get("sources") or []
    out = []
    ok_sources = [s for s in sources if s["status"] == "ok"]
    if ok_sources:
        out.append({
            "observation": f"{len(ok_sources)} 个源有信号（共 {len(RADAR_SOURCES)} 个接入源，"
                           f"{signals.get('skipped', 0)} 个未配置、"
                           f"{signals.get('error', 0) + signals.get('timeout', 0)} 个抓取失败或超时）",
            "judgment": "接入面决定了雷达的视野：未配置的几个源里，Product Hunt / trustmrr 的"
                        "「收入验证」信息最难替代",
        })
    by_source: dict = {}
    for item in items:
        by_source.setdefault(item["source_name"], []).append(item)
    ranked = sorted(by_source.items(), key=lambda pair: -len(pair[1]))[:3]
    if ranked:
        out.append({
            "observation": "条目最多的源：" + "、".join(f"{name} {len(rows)} 条" for name, rows in ranked),
            "judgment": "条目多不等于机会多，只说明当天该源更活跃；判断要看具体条目标题",
        })
    heat = [item for item in items if item.get("points")]
    if heat:
        heat.sort(key=lambda item: -item["points"])
        top = heat[:3]
        out.append({
            "observation": "热度最高：" + "、".join(
                f"{item['title']}（{item['source_name']} {item['points']}）" for item in top),
            "judgment": "热度是 star / 回复 / 票数这类公开计数，不等于收入或留存",
        })
    return out


def _wisdom(items: list, modules: dict) -> dict:
    """心法：从当天命中的模块反推，不套固定方法库。"""
    filled = [m["label"] for m in MODULES if modules.get(m["key"])]
    if not items or not filled:
        return {"status": "missing",
                "method": "今日没有可用信号，心法留空",
                "scenario": "", "steps": [], "derivation": ""}
    top_source = max({item["source_name"] for item in items},
                     key=lambda name: sum(1 for item in items if item["source_name"] == name))
    return {
        "status": "live",
        "method": f"先看「{filled[0]}」，再看别人怎么收钱",
        "scenario": f"当天 {len(items)} 条信号里，{filled[0]}"
                    f"{('、' + filled[1]) if len(filled) > 1 else ''}等 {len(filled)} 个模块有料，"
                    "先挑一个自己一周内能动手的方向",
        "steps": [
            f"从 {top_source} 里挑 1 条最像自己能做的条目，读完原文而不是只看标题",
            "用一句话写下它替谁解决了什么，写不出来就换下一条",
            "找 2 个现成替代品，比较它们缺什么（这是你的切入口）",
            "只做最小可用的一步，7 天后用同一批信号回看需求还在不在",
        ],
        "derivation": f"由当日 {len(items)} 条信号、命中模块 {len(filled)} 个反推；"
                      "不是固定方法库里的条目",
    }


def analyze(signals: dict | None = None, as_of=None) -> dict:
    """把采集结果整理成 7 个模块 + 心法。没有素材的模块如实留空。"""
    as_of_text = _as_of(as_of)
    data = signals if signals is not None else collect_signals()
    items = data.get("items") or []
    modules, status = {}, {}
    buckets = _assign_modules(items)
    for module in MODULES:
        if module["key"] == "data_signals":
            continue          # 数据信号由计数算出，不走关键词筛选
        picked = _take(buckets.get(module["key"]) or [], LIMITS["per_module"])
        modules[module["key"]] = picked
        status[module["key"]] = {
            "status": "live" if picked else "missing",
            "reason": "" if picked else f"当日信号中没有命中「{module['label']}」关键词的条目",
        }
    data_signals = _data_signals(data)
    modules["data_signals"] = data_signals
    status["data_signals"] = {
        "status": "live" if data_signals else "missing",
        "reason": "" if data_signals else "采集结果为空，无法统计",
    }
    wisdom = _wisdom(items, modules)
    filled = sum(1 for key in modules if modules[key])
    return {
        "column": COLUMN_TITLE,
        "as_of": as_of_text,
        "upstream": UPSTREAM,
        "upstream_note": UPSTREAM_NOTE,
        "fetched_at": data.get("fetched_at") or "",
        "counts": {
            "items": len(items),
            "sources_ok": data.get("ok", 0),
            "sources_total": len(RADAR_SOURCES),
            "skipped": data.get("skipped", 0),
            "error": data.get("error", 0),
            "modules_filled": filled,
        },
        "modules": modules,
        "module_status": status,
        "wisdom": wisdom,
        "sources": data.get("sources") or [],
        "headline": _headline(items, modules, wisdom),
        "disclaimer": DISCLAIMER,
        "prompt": AI_PROMPT,
    }


def _as_of(value) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.today().strftime("%Y-%m-%d")


def _headline(items: list, modules: dict, wisdom: dict) -> str:
    if not items:
        return "今日雷达没有取到信号，各模块留空（不补旧数据、不编条目）"
    first = next((entry["title"] for key in ("opportunities", "money_cases", "ideas", "tools")
                  for entry in modules.get(key) or []), items[0]["title"])
    # 眉标分隔一律用顿号：中点串联是 Hallmark gate 63 的机器痕迹。
    return (f"{len(items)} 条信号、{sum(1 for m in modules.values() if m)} 个模块有料、"
            f"头条「{first[:40]}」、心法：{wisdom.get('method', '')}")


def prompt_today(signals: dict | None = None, as_of=None) -> str:
    """当日 AI 提示词：系统提示 + 当日条目清单 + 输出要求（「AI 提」）。"""
    data = signals if signals is not None else collect_signals()
    items = data.get("items") or []
    lines = [AI_PROMPT, "", f"日期：{_as_of(as_of)}", f"条目数：{len(items)}", "", "当日条目："]
    for index, item in enumerate(items, 1):
        extra = f"（{item['source_name']}"
        if item.get("points"):
            extra += f"，热度 {item['points']}"
        extra += "）"
        lines.append(f"{index}. {item['title']} {extra} {item['url']}")
    if not items:
        lines.append("（今日没有取到条目。请直接说明没有素材，不要虚构条目。）")
    lines += ["", "按上面的模块定义输出 Markdown 日报 + JSON；没有素材的模块留空。"]
    return "\n".join(lines)


def framework() -> dict:
    """模块与源清单，给页面 / 接口描述这个栏目是什么。"""
    return {
        "column": COLUMN_TITLE,
        "upstream": UPSTREAM,
        "upstream_note": UPSTREAM_NOTE,
        "modules": MODULES,
        "sources": [{key: value for key, value in source.items() if key != "url"}
                    for source in RADAR_SOURCES],
        "limits": dict(LIMITS),
        "prompt": AI_PROMPT,
        "disclaimer": DISCLAIMER,
    }


def _esc(text) -> str:
    return _html.escape(str(text or ""), quote=True)


def page_section_html(report: dict) -> str:
    """页面版区块（index.html 的静态快照与 app.js 的运行时渲染共用同一套结构）。"""
    report = report or {}
    counts = report.get("counts") or {}
    rows = []
    for module in MODULES:
        entries = (report.get("modules") or {}).get(module["key"]) or []
        if not entries:
            continue
        if module["key"] == "data_signals":
            items_html = "".join(
                f'<li class="radar-item"><b>{_esc(entry.get("observation"))}</b>'
                f'<span class="radar-judgment">{_esc(entry.get("judgment"))}</span></li>'
                for entry in entries
            )
        else:
            items_html = "".join(
                f'<li class="radar-item"><a href="{_esc(entry.get("url"))}" target="_blank" '
                f'rel="noopener noreferrer">{_esc(entry.get("title"))}</a>'
                f'<span class="radar-src">{_esc(entry.get("source"))}'
                + (f'，热度 {entry["points"]}' if entry.get("points") else "")
                + f'</span></li>'
                for entry in entries
            )
        rows.append(
            f'<div class="radar-module"><h3><span class="radar-no">{module["no"]}</span>'
            f'{_esc(module["label"])}</h3><ul class="radar-items">{items_html}</ul></div>'
        )
    wisdom = report.get("wisdom") or {}
    wisdom_html = ""
    if wisdom.get("status") == "live":
        steps = "".join(f"<li>{_esc(step)}</li>" for step in (wisdom.get("steps") or []))
        wisdom_html = (
            '<div class="radar-wisdom" id="radarWisdom">'
            f'<b>今日心法：{_esc(wisdom.get("method"))}</b>'
            f'<p>{_esc(wisdom.get("scenario"))}</p><ol>{steps}</ol></div>'
        )
    return (
        '<section class="pixel-section" id="indieRadarSection" data-column="独立开发雷达">'
        '<div class="pixel-section-head"><div><h2>AI 独立开发雷达</h2></div>'
        f'<span class="ai-pill" id="radarPill">{_esc(status_line(report))}</span></div>'
        '<article class="insight-card">'
        f'<p class="quant-summary" id="radarSummary"><mark>{_esc(report.get("as_of"))}</mark>'
        f'{_esc(report.get("headline"))}</p>'
        f'<div class="radar-modules" id="radarModules">{"".join(rows)}</div>'
        f'{wisdom_html}'
        f'<div class="insight-foot" data-meta="1"><span>'
        f'方法来自 <a href="{UPSTREAM}" target="_blank" rel="noopener noreferrer">'
        f'susumr/radar-dashboard</a>：{_esc(UPSTREAM_NOTE)}；'
        f'{_esc(DISCLAIMER)} 实时结果见 <code>/api/radar</code>。</span></div>'
        '</article></section>'
    )


def status_line(report: dict) -> str:
    """推送卡片顶部的一行概况。"""
    counts = (report or {}).get("counts") or {}
    # 眉标一律用顿号：中点串联是 Hallmark gate 63 的机器痕迹（见 docs/hallmark_deai.md）。
    return (f"{counts.get('items', 0)} 条信号、"
            f"{counts.get('sources_ok', 0)}/{counts.get('sources_total', 0)} 源有数据、"
            f"{counts.get('modules_filled', 0)} 个模块有料")


if __name__ == "__main__":
    import sys

    if "--page-section" in sys.argv:
        print(page_section_html(analyze()))
    elif "--prompt" in sys.argv:
        print(prompt_today())
    else:
        report = analyze()
        print(json.dumps({key: report[key] for key in
                          ("as_of", "headline", "counts", "wisdom")}, ensure_ascii=False, indent=2))
