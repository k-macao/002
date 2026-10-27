#!/usr/bin/env python3
"""「大 V 分析」——以「预测历史」公开框架为蓝本的 AI 框架仿推演引擎。

设计边界
--------
1. 只提取公开可复核的方法：结构主义历史 / 帝国周期、博弈论中的参与者与激励、
   资源与成本约束、升级博弈、历史类比、情景与证伪条件。
2. 不模仿任何特定个人的措辞，不复述未经授权的访谈全文，也不冒充他人发言。公开评论指出
   其部分末世论、秘密组织叙事不可证伪；这些内容一律不进入本引擎。
3. 结论只从当次 ``brief`` 的新闻标题推导。观察、假设、推演、证伪条件分开标注；
   没有证据时只展示框架，不编造事件结论。
4. 纯标准库、可离线、确定性。这里的「AI」是与仓库其余分析栏目同口径的规则推理，
   不要求新的模型 API Key。

公开资料依据
------------
- Predictive History 官方频道关于「理解过去 / 解释现在 / 预测未来」的定位。
- 中文百科与媒体报道对「预测历史」的概括：结构历史、博弈论、心理史学隐喻。
- Newsweek 对 2024 年伊朗课程中参与者激励、西西里远征类比和过度扩张成本的描述。
- 中文评论对「不对称战争经济账、海湾基础设施脆弱性、金融联动」及方法边界的总结。
- 页面静态快照的 2026-10-26 近期事实另见 ``docs/big_v_analysis.md``。

对外接口
--------
    analyze_big_v(brief, as_of=None, section_lookup=None) -> dict
    analyze(...)        兼容别名
    framework()         返回框架与公开资料链接
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
import re
from typing import Callable

COLUMN_TITLE = "大 V 分析"
ANALYST = "公开框架"
FRAMEWORK = "预测历史（Predictive History）"
DISCLAIMER = (
    "AI 框架仿推演：仅借鉴公开资料中可验证的结构史观、博弈论与成本分析，"
    "不模仿任何特定个人语气，不代表其观点、原话或背书。"
)
METHOD_BOUNDARY = (
    "历史类比只是提出假设，不等于证明；不采用不可证伪的末世论或秘密组织叙事。"
    "情景权重是可解释的启发式关注度，不是统计概率，也不是投资建议。"
)

# 可直接交给外部大模型复用的系统提示词。默认实现不联网，由本地规则生成同结构结果。
AI_PROMPT = """你是一名政治—地缘—经济分析引擎。只借鉴公开「预测历史」框架中可验证的方法：
先定义参与者和时间尺度，再列出各自激励与硬约束，计算非对称成本，展开升级博弈，
以历史类比提出假设，最后给出基准/升级/缓和情景及可观察的证伪信号。
不得模仿其第一人称或文风，不得声称代表本人；不得使用不可证伪的阴谋论；
区分已观察事实、机制推演与情景；新闻不足时明确说证据不足。输出结构化中文。"""

FRAMEWORK_STEPS = [
    {
        "no": "01",
        "label": "划定棋盘",
        "question": "谁是参与者？时间尺度是选举周期、战役周期还是货币周期？",
        "output": "参与者清单与观察窗口",
    },
    {
        "no": "02",
        "label": "识别激励",
        "question": "每个参与者最想保住什么：政权安全、选票、盟友信用、通胀目标还是资本回报？",
        "output": "目标排序与不可让步项",
    },
    {
        "no": "03",
        "label": "核算约束",
        "question": "后勤、能源咽喉、债务、利率、制度制衡和盟友承载力各能支撑多久？",
        "output": "资源账、财政账与政治耐心",
    },
    {
        "no": "04",
        "label": "升级博弈",
        "question": "一方的低成本行动会迫使另一方付出多高的防御成本？ credibility 是否把双方锁死？",
        "output": "行动—反行动树与成本交换比",
    },
    {
        "no": "05",
        "label": "历史类比",
        "question": "西西里远征 / 帝国过度扩张 / 长期消耗战在哪些结构变量上相似？",
        "output": "可检验的历史假设，不当作定论",
    },
    {
        "no": "06",
        "label": "情景证伪",
        "question": "什么信号出现后必须承认推演错了？",
        "output": "基准 / 升级 / 缓和情景与观察指标",
    },
]

REFERENCES = [
    {
        "title": "Predictive History 官方 YouTube 频道",
        "url": "https://www.youtube.com/@PredictiveHistory",
        "note": "频道定位：用什么模型理解历史、当下与可预测的未来。",
    },
    {
        "title": "Wikipedia：结构史观方法概览",
        "url": "https://en.wikipedia.org/wiki/Jiang_Xueqin",
        "note": "结构历史分析、博弈论、心理史学隐喻，以及选择性历史类比等批评。",
    },
    {
        "title": "Newsweek：The Professor Who Predicted Trump’s Return and War With Iran",
        "url": "https://www.newsweek.com/jiang-xueqin-trump-iran-viral-video-youtube-2090047",
        "note": "2024 伊朗课程中的参与者激励、补给线、西西里远征类比与争议。",
    },
    {
        "title": "The Business Standard：Game theory and psychohistory",
        "url": "https://www.tbsnews.net/features/panorama/game-theory-and-psychohistory-decoding-chinese-nostradamus-dr-xueqin-jiang-1396246",
        "note": "博弈论、历史类比及方法可信度边界。",
    },
    {
        "title": "虎嗅：伊朗战争的走向与方法争议",
        "url": "https://www.huxiu.com/article/4847387.html",
        "note": "不对称成本、海湾脆弱性、金融联动，以及把风险写成必然结果的批评。",
    },
    {
        "title": "The Guardian，2026-10-22：伊朗威胁打击海湾石油与 AI 设施",
        "url": "https://www.theguardian.com/world/2026/oct/22/iran-threatens-seven-gulf-countries-us-israel-renew-attacks",
        "note": "近期地缘快照：霍尔木兹、能源 / 数据基础设施威胁与外部斡旋。",
    },
    {
        "title": "Al Jazeera，2026-10-23：伊朗战争成为美国中期选举的生活成本议题",
        "url": "https://www.aljazeera.com/news/2026/10/23/foreign-policy-is-the-economy-how-iran-war-is-shaping-us-midterm-vote",
        "note": "近期政治快照：能源价格、选民成本与战争权力的相互约束。",
    },
    {
        "title": "Newsquawk，2026-10-26：Deal or attacks 与主要央行周",
        "url": "https://www.newsquawk.com/headlines/newsquawk-daily-us-opening-news---26th-october-2026",
        "note": "近期经济快照：特朗普决策窗口、油价回撤、FOMC / ECB / BoJ 观察。",
    },
]

DOMAIN_RULES = {
    "politics": {
        "label": "政治",
        "keywords": [
            "选举", "大选", "中期", "国会", "参议院", "众议院", "议会", "白宫", "总统",
            "总理", "内阁", "政府关门", "停摆", "预算", "法院", "弹劾", "投票", "立法",
            "行政", "峰会", "外交", "政治", "制裁法案", "战争权力", "特朗普", "election",
            "midterm", "Congress", "Senate", "parliament", "White House", "president",
            "shutdown", "Supreme Court", "vote", "cabinet", "summit", "sanctions bill",
        ],
        "actors": ["行政部门", "立法机构", "选民", "央行", "盟友政府"],
        "incentive": "执政者追求可见的政治强硬与选举收益，选民更在意油价、物价与战争成本；立法与司法机构追求制衡。",
        "constraints": "选举日历、战争授权、预算谈判、央行独立性与舆论耐心会把极端选项拆成可推迟、可否认的小步动作。",
        "logic": "把政治动作放进「收益—成本」表：若升级带来的选举收益高于能源与财政成本，威胁会加码；若物价和民调反噬，决策者会转向谈判或把行动延后。",
        "base": "最可能出现「边谈边压」：制裁、军事部署和谈判信号并存，而不是干净利落的全战或全面撤出。",
        "risk": "若选举后制度约束减弱、或决策者把退让视为信誉损失，短期强硬行动的概率会上升。",
    },
    "geopolitics": {
        "label": "地缘",
        "keywords": [
            "战争", "冲突", "军事", "袭击", "打击", "导弹", "无人机", "海峡", "霍尔木兹",
            "伊朗", "以色列", "乌克兰", "俄罗斯", "北约", "海湾", "格陵兰", "台湾", "南海",
            "胡塞", "盟友", "安全协议", "停火", "撤军", "war", "attack", "strike", "missile",
            "drone", "military", "NATO", "Hormuz", "Iran", "Israel", "Russia", "Ukraine",
            "Taiwan", "Greenland", "ceasefire", "alliance", "security deal", "sanction",
        ],
        "actors": ["美国 / 以色列", "伊朗与非国家武装", "海湾国家", "欧洲与俄乌双方", "斡旋方"],
        "incentive": "强势方要维护威慑可信度，弱势方用低成本骚扰抬高对手防御成本，海湾国家优先保基础设施和政权安全。",
        "constraints": "海峡通行、海水淡化和能源 / 数据基础设施是脆弱节点；远程投送、拦截成本与多线承诺又限制强势方。",
        "logic": "这是一棵升级博弈树：较弱一方不必在正面战场获胜，只要让每次封锁 / 骚扰的边际成本低于对手的防护成本，就能把军事优势转化成财政与政治消耗。",
        "base": "有限打击、海峡摩擦和斡旋并行；双方都保留升级牌，但都不愿承担关键基础设施全面受损的后果。",
        "risk": "信誉困境会把双方锁进「报复—再报复」：一次误判可能把能源与 AI / 数据基础设施从威慑筹码变成真实目标。",
    },
    "economy": {
        "label": "经济",
        "keywords": [
            "美联储", "欧央行", "日本央行", "央行", "加息", "降息", "利率", "通胀", "CPI",
            "PCE", "GDP", "衰退", "就业", "失业", "债务", "国债", "关税", "汇率", "美元",
            "日元", "原油", "油价", "布伦特", "黄金", "债券", "收益率", "股市", "资本开支",
            "AI", "数据中心", "算力", "财报", "消费者", "Fed", "FOMC", "ECB", "BOJ",
            "inflation", "rate", "bond", "Treasury", "tariff", "oil", "Brent", "GDP",
            "recession", "capex", "data center", "earnings", "yield", "dollar", "yen",
        ],
        "actors": ["主要央行", "财政部门", "能源进口国", "科技 / AI 资本开支方", "债券与股票投资者"],
        "incentive": "央行要守住通胀信誉，政府要保增长与融资，科技企业需要廉价能源和低成本资金支持长周期资本开支。",
        "constraints": "能源供给冲击不是降息能解决的；高利率、高债务和关税会同时挤压财政、估值与供应链。",
        "logic": "从能源咽喉出发：油价与运费上升 → 通胀预期黏住 → 央行难以转鸽 → 长端利率与融资成本上升 → 高杠杆财政和 AI 资本开支被迫重新定价。",
        "base": "基准形态是利润率和融资条件分化，单一「泡沫破裂」或「全面衰退」不足以概括：现金流强、能源自给度高、杠杆低的经济体更抗压。",
        "risk": "若能源冲击持续并伴随关税 / 供应链断裂，滞胀式的盈利下修与信用风险会同步出现，科技估值对利率更敏感。",
    },
}

THEMES = [
    {
        "key": "energy",
        "label": "能源咽喉与基础设施",
        "domains": ["geopolitics", "economy"],
        "keywords": [
            "霍尔木兹", "原油", "油价", "能源", "天然气", "LNG", "海水淡化", "炼油", "航运",
            "布伦特", "OPEC", "Hormuz", "Brent", "crude", "oil", "energy", "desalination",
            "pipeline", "shipping", "Houthi", "Gulf", "Saudi", "data centre", "datacenter",
            "undersea cable", "AI facilities",
        ],
        "chain": "海峡 / 油田 / 淡化厂 / 海底电缆受到威胁 → 保险与运费上升 → 油价和输入型通胀 → 央行与财政空间收窄。",
        "watch": ["霍尔木兹实际通行量与战争险费率", "海湾电力 / 淡化 / 数据中心运行", "Brent 是否连续站稳高位", "斡旋后海峡分阶段开放的核实结果"],
    },
    {
        "key": "election",
        "label": "选举周期与制度制衡",
        "domains": ["politics", "economy"],
        "keywords": [
            "中期选举", "大选", "国会", "参议院", "众议院", "政府关门", "预算", "战争权力",
            "总统", "投票", "民调", "法院", "关税权", "election", "midterm", "Congress",
            "shutdown", "Senate", "Supreme Court", "president", "vote", "poll", "tariff powers",
        ],
        "chain": "生活成本成为选票问题 → 执政者把对外强硬当作可信度资产 → 国会 / 法院 / 央行形成约束 → 重大动作可能被推迟到政治窗口后。",
        "watch": ["中期选举前后的军事部署与授权", "战争权力 / 预算立法进展", "汽油价格与消费者信心", "两党是否形成可持续的止损方案"],
    },
    {
        "key": "military",
        "label": "军事升级与不对称消耗",
        "domains": ["geopolitics", "politics"],
        "keywords": [
            "战争", "冲突", "袭击", "空袭", "导弹", "无人机", "拦截", "航母", "驻军", "北约",
            "防务", "停火", "撤军", "war", "attack", "strike", "missile", "drone", "carrier",
            "NATO", "defense", "ceasefire", "troop", "interceptor",
        ],
        "chain": "低成本无人机 / 水雷 / 袭船 → 高价拦截与护航 → 防务库存和财政承压 → 为保信誉追加打击 → 升级惯性增强。",
        "watch": ["拦截成本与弹药补给速度", "航母 / 战机部署变化", "是否出现地面力量或战略目标扩大", "第三方斡旋与可核查停火"],
    },
    {
        "key": "rates",
        "label": "通胀、央行与债务",
        "domains": ["economy", "politics"],
        "keywords": [
            "美联储", "欧央行", "日本央行", "央行", "加息", "降息", "利率", "通胀", "CPI", "PCE",
            "国债", "收益率", "债务", "美元", "日元", "汇率", "Fed", "FOMC", "ECB", "BOJ",
            "inflation", "rate", "Treasury", "bond", "yield", "debt", "dollar", "yen", "CPI", "PCE",
        ],
        "chain": "能源 / 关税推高物价 → 通胀预期上移 → 主要央行维持紧缩 → 长端利率与债务利息上行 → 成长股与资本开支折现率提高。",
        "watch": ["FOMC / ECB / BoJ 措辞与点阵图", "通胀预期与盈亏平衡率", "10 年期国债收益率与期限溢价", "AI 企业融资利差与资本开支指引"],
    },
    {
        "key": "sanctions",
        "label": "制裁、关税与供应链",
        "domains": ["politics", "economy", "geopolitics"],
        "keywords": [
            "制裁", "关税", "出口管制", "反制", "贸易", "供应链", "稀土", "芯片", "半导体",
            "脱钩", "贸易谈判", "sanction", "tariff", "export control", "trade", "supply chain",
            "chip", "semiconductor", "decoupling", "trade talks",
        ],
        "chain": "制裁 / 关税改变相对价格 → 转口与替代供应链出现 → 企业库存和资本开支前置 → 通胀黏性与产业分配同时变化。",
        "watch": ["制裁是否附带二级关税", "关键矿产 / 半导体出口许可", "转口贸易与航运改道", "企业毛利率和库存天数"],
    },
    {
        "key": "ai_capex",
        "label": "AI 资本开支与金融循环",
        "domains": ["economy", "geopolitics"],
        "keywords": [
            "AI", "人工智能", "数据中心", "算力", "云", "资本开支", "泡沫", "OpenAI", "Anthropic",
            "Nvidia", "GPU", "cloud", "capex", "data center", "datacenter", "bubble", "valuation",
        ],
        "chain": "AI 扩张依赖电力、芯片和低成本融资 → 能源 / 利率 /  Gulf 资本任一端受冲击 → 资本开支回报假设下修 → 股债风险溢价重新定价。",
        "watch": ["超大云厂商资本开支与自由现金流", "数据中心电力合同与停机风险", "AI 相关债券 / 私募融资利差", "算力订单转化为收入的速度"],
    },
    {
        "key": "alliance",
        "label": "联盟承诺与多线消耗",
        "domains": ["geopolitics", "politics"],
        "keywords": [
            "北约", "欧盟", "盟友", "安全协议", "格陵兰", "乌克兰", "日本", "韩国", "多边",
            "峰会", "NATO", "EU", "alliance", "ally", "Greenland", "Ukraine", "Japan", "Korea",
            "security deal", "summit", "defense spending",
        ],
        "chain": "多个战区同时需要安全承诺 → 军费 / 装备库存 / 政治注意力被稀释 → 盟友对保护者可信度重新定价 → 区域自主军备与外交对冲增加。",
        "watch": ["跨战区军援与弹药库存", "北约 / 东亚防务预算", "盟友是否寻求独立调解", "关键海峡与航道的多国护航安排"],
    },
]

ESCALATION_WORDS = [
    "升级", "封锁", "袭击", "空袭", "威胁", "报复", "冲突", "战争", "制裁", "危机",
    "停摆", "受阻", "导弹", "无人机", "attack", "strike", "blockade", "threat", "sanction",
    "war", "crisis", "shutdown", "hike", "tariff", "risk", "warn",
]
DEESCALATION_WORDS = [
    "谈判", "斡旋", "停火", "协议", "缓和", "回落", "重开", "开放", "豁免", "降级",
    "deal", "talks", "ceasefire", "agreement", "reopen", "easing", "relief", "pause", "truce",
]


@lru_cache(maxsize=1024)
def _ascii_pattern(keyword: str) -> re.Pattern:
    """ASCII 关键词按单词边界匹配，末尾 ``*`` 允许后缀。"""
    stem = re.escape(keyword.lower()).replace(r"\*", r"\w*")
    return re.compile(r"(?<![a-z0-9])" + stem + r"(?![a-z0-9])")


def _hit(title: str, lower: str, keyword: str) -> bool:
    if keyword.isascii():
        return bool(_ascii_pattern(keyword).search(lower))
    return keyword in title


def _norm_title(title: str) -> str:
    """跨源去重键：忽略大小写、空白与标点，保留中英文字符。"""
    return re.sub(r"[\s\W_]+", "", title.lower(), flags=re.UNICODE) or title.lower()


def _as_of(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or date.today().isoformat())


def _collect(brief: dict, section_lookup: Callable[[str], str] | None = None) -> list:
    """归一化 brief；同一标题跨源合并，并标注命中的领域与主题。"""
    records: dict[str, dict] = {}
    if not isinstance(brief, dict):
        return []
    for source, rows in brief.items():
        if source == "error" or not isinstance(rows, (list, tuple)):
            continue
        source_name = str(source)
        try:
            section = section_lookup(source_name) if section_lookup else "other"
        except Exception:
            section = "other"
        for item in rows:
            if isinstance(item, str):
                title, url = item.strip(), ""
            elif isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                url = str(item.get("url") or "").strip()
            else:
                continue
            if not title:
                continue
            key = _norm_title(title)
            if key in records:
                rec = records[key]
                rec["mentions"] += 1
                if source_name not in rec["sources"]:
                    rec["sources"].append(source_name)
                continue
            lower = title.lower()
            records[key] = {
                "title": title,
                "url": url,
                "sources": [source_name],
                "mentions": 1,
                "section": section,
                "domains": [],
                "domain_scores": {},
                "themes": [],
                "theme_scores": {},
                "escalation": sum(1 for kw in ESCALATION_WORDS if _hit(title, lower, kw)),
                "deescalation": sum(1 for kw in DEESCALATION_WORDS if _hit(title, lower, kw)),
            }
            rec = records[key]
            for domain, rule in DOMAIN_RULES.items():
                score = sum(1 for kw in rule["keywords"] if _hit(title, lower, kw))
                if score > 0:
                    rec["domains"].append(domain)
                    rec["domain_scores"][domain] = score
            for theme in THEMES:
                score = sum(1 for kw in theme["keywords"] if _hit(title, lower, kw))
                if score > 0:
                    rec["themes"].append(theme["key"])
                    rec["theme_scores"][theme["key"]] = score
            # 主题命中意味着至少属于该主题的领域，避免标题只命中英文主题词时丢失领域。
            for theme in THEMES:
                if theme["key"] not in rec["themes"]:
                    continue
                for domain in theme["domains"]:
                    if domain not in rec["domains"]:
                        rec["domains"].append(domain)
                        rec["domain_scores"][domain] = max(
                            1, rec["theme_scores"][theme["key"]]
                        )
    return [rec for rec in records.values() if rec["domains"]]


def _confidence(sources: int, mentions: int) -> str:
    if sources >= 4 and mentions >= 6:
        return "较高"
    if sources >= 2 and mentions >= 3:
        return "中"
    return "低"


def _unique_sources(records: list) -> int:
    return len({source for rec in records for source in rec["sources"]})


def _top_theme_records(records: list) -> list:
    totals = {}
    for theme in THEMES:
        key = theme["key"]
        hits = [rec for rec in records if key in rec["themes"]]
        if hits:
            totals[key] = (sum(rec["mentions"] for rec in hits), _unique_sources(hits), theme)
    return [item[2] for item in sorted(
        totals.values(), key=lambda item: (item[0], item[1]), reverse=True
    )]


def _domain_payload(key: str, matched: list, themes: list) -> dict:
    rule = DOMAIN_RULES[key]
    records = [rec for rec in matched if key in rec["domains"]]
    sources = _unique_sources(records)
    mentions = sum(rec["mentions"] for rec in records)
    ranked = sorted(
        records,
        key=lambda rec: (
            rec["domain_scores"].get(key, 0),
            len(rec["sources"]),
            rec["mentions"],
            -len(rec["title"]),
        ),
        reverse=True,
    )
    evidence = [{
        "title": rec["title"],
        "source": " / ".join(rec["sources"][:2]),
        "url": rec["url"],
        "mentions": rec["mentions"],
    } for rec in ranked[:3]]
    focus_themes = [
        theme["label"] for theme in themes
        if key in theme["domains"]
    ][:3]
    confidence = _confidence(sources, mentions)
    if not records:
        conclusion = (
            f"当日样本缺少可复核的{rule['label']}新闻。保留框架，不生成事件结论；"
            "待数据刷新后再按激励、约束与成本账推演。"
        )
        chain = [
            "观察：暂无足够证据",
            "激励：待确认",
            "约束：待确认",
            "推演：不输出方向性结论",
        ]
    else:
        focus = "、".join(focus_themes) if focus_themes else "跨领域联动"
        theme_chain = next(
            (theme["chain"] for theme in themes if key in theme["domains"]),
            "",
        )
        conclusion = (
            f"{focus}构成{rule['label']}端主线。{rule['logic']} "
            f"基准判断：{rule['base']}主要风险：{rule['risk']}"
        )
        chain = [
            f"观察：{focus}（{sources} 源 / {mentions} 条提及）",
            f"激励：{rule['incentive']}",
            f"约束：{rule['constraints']}",
            f"推演：{rule['base']}",
        ]
        if theme_chain:
            chain.append(f"传导：{theme_chain}")
    return {
        "key": key,
        "label": rule["label"],
        "focus": focus_themes,
        "actors": rule["actors"],
        "incentive": rule["incentive"],
        "constraints": rule["constraints"],
        "logic": rule["logic"],
        "conclusion": conclusion,
        "chain": chain,
        "evidence": evidence,
        "sources": sources,
        "mentions": mentions,
        "confidence": confidence,
        "status": "有证据" if records else "证据不足",
    }


def _risk_score(matched: list, themes: list) -> int:
    escalation = sum(rec["escalation"] for rec in matched)
    deescalation = sum(rec["deescalation"] for rec in matched)
    keys = {theme["key"] for theme in themes[:3]}
    structural = 4 * int("energy" in keys) + 3 * int("military" in keys) + 2 * int("rates" in keys)
    return max(5, min(95, 45 + escalation * 6 - deescalation * 5 + structural))


def _scenario_weights(risk: int) -> tuple[int, int, int]:
    """启发式关注度，三者和为 100；不是校准过的统计概率。"""
    up = max(10, min(42, int(8 + risk * 0.34)))
    down = max(8, min(32, int(32 - (risk - 50) * 0.14)))
    base = 100 - up - down
    return base, up, down


def _headline(as_of: str, themes: list, domains: list, risk: int, matched: list) -> str:
    if not matched:
        return (
            f"{as_of}：当日抓取样本没有足够的政治 / 地缘 / 经济证据，"
            "本栏目只展示「预测历史」分析框架，不编造近期事件结论。"
        )
    lead = themes[0]["label"] if themes else "跨领域政策联动"
    present = " × ".join(domain["label"] for domain in domains if domain["status"] == "有证据")
    risk_label = "升级压力偏高" if risk >= 65 else ("胶着拉锯" if risk >= 45 else "缓和占优")
    return (
        f"{as_of} 主线是「{lead}」：{present or '政治 × 地缘 × 经济'}由「国内激励 → "
        "地缘杠杆 → 能源 / 利率成本 → 资本再定价」串成同一条链。"
        f"按激励、约束、升级与证伪六步推演，当前判为{risk_label}；"
        "基准是边谈边压，关键基础设施受扰是主要尾部风险。"
    )


def _falsifiers(themes: list) -> list:
    out = []
    for theme in themes[:3]:
        for item in theme["watch"][:2]:
            if item not in out:
                out.append(item)
    generic = [
        "若海峡通行、保险费率与能源价格连续回落，应下调升级情景权重。",
        "若主要央行在通胀预期稳定下明确转鸽，应下调「能源冲击 → 利率挤压」链条。",
        "若出现可核查的长期停火与制裁减免，应重估「信誉困境锁死双方」假设。",
    ]
    for item in generic:
        if item not in out and len(out) < 7:
            out.append(item)
    return out[:7]


def framework() -> dict:
    """返回可独立展示的框架与公开资料，不含任何本人原话。"""
    return {
        "column": COLUMN_TITLE,
        "analyst": ANALYST,
        "name": FRAMEWORK,
        "disclaimer": DISCLAIMER,
        "boundary": METHOD_BOUNDARY,
        "steps": [dict(step) for step in FRAMEWORK_STEPS],
        "references": [dict(ref) for ref in REFERENCES],
        "prompt": AI_PROMPT,
    }


def analyze_big_v(
    brief: dict,
    as_of=None,
    section_lookup: Callable[[str], str] | None = None,
) -> dict:
    """用六步框架分析当次抓取新闻，优先输出政治 / 地缘 / 经济的因果链。"""
    day = _as_of(as_of)
    matched = _collect(brief, section_lookup=section_lookup)
    themes = _top_theme_records(matched)
    domains = [_domain_payload(key, matched, themes) for key in DOMAIN_RULES]
    risk = _risk_score(matched, themes)
    base_w, up_w, down_w = _scenario_weights(risk)
    lead_theme = themes[0] if themes else None
    escalation_trigger = (
        lead_theme["chain"] if lead_theme else "关键咽喉或制度约束被突破"
    )
    watch = lead_theme["watch"][0] if lead_theme else "关键事件的可核查数据"
    scenarios = [
        {
            "key": "base",
            "label": "基准 · 边谈边压",
            "weight": base_w,
            "trigger": "制裁、有限军事压力与谈判信号并存；双方保留升级筹码但避免全面开战。",
            "chain": "政治周期延后重大选择 → 海峡风险溢价反复 → 央行维持谨慎 → 资产在现金流与防御之间轮动。",
            "watch": [watch, "油价与通胀预期", "选举 / 预算制度约束"],
        },
        {
            "key": "escalation",
            "label": "升级 · 咽喉与基础设施受扰",
            "weight": up_w,
            "trigger": "能源、淡化、海底电缆或 AI / 数据基础设施被实际打击；对手为保信誉扩大报复。",
            "chain": escalation_trigger,
            "watch": ["通行量 / 保险费率", "关键基础设施受损核实", "央行与长端利率反应"],
        },
        {
            "key": "detente",
            "label": "缓和 · 通行换制裁减免",
            "weight": down_w,
            "trigger": "可核查停火 + 霍尔木兹分阶段开放 + 制裁 / 冻结资产安排落地。",
            "chain": "斡旋降低误判 → 风险溢价回吐 → 通胀预期回落 → 央行政策空间和风险资产估值修复。",
            "watch": ["停火核查机制", "Brent 与运费回落持续性", "AI 资本开支融资条件"],
        },
    ]
    fw = framework()
    counts = {
        "items": sum(1 for rec in matched),
        "mentions": sum(rec["mentions"] for rec in matched),
        "sources": _unique_sources(matched),
    }
    return {
        **fw,
        "as_of": day,
        "headline": _headline(day, themes, domains, risk, matched),
        "framework_steps": fw["steps"],
        "domains": domains,
        "themes": [theme["label"] for theme in themes[:5]],
        "scenarios": scenarios,
        "scenario_weights_note": "情景权重为启发式关注度，不是统计概率，也不是投资建议。",
        "risk_score": risk,
        "risk_label": "升级压力偏高" if risk >= 65 else ("胶着拉锯" if risk >= 45 else "缓和占优"),
        "falsifiers": _falsifiers(themes),
        "counts": counts,
        "status": "有证据" if matched else "证据不足",
        "generated_by": "章鱼 AI · 本地可复算框架推理（非他人发言）",
    }


# 简洁别名，便于 server.py / 测试调用。
analyze = analyze_big_v

__all__ = [
    "COLUMN_TITLE", "ANALYST", "FRAMEWORK", "DISCLAIMER", "METHOD_BOUNDARY",
    "AI_PROMPT", "FRAMEWORK_STEPS", "REFERENCES", "DOMAIN_RULES", "THEMES",
    "framework", "analyze_big_v", "analyze",
]
