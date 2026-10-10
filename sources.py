#!/usr/bin/env python3
"""章鱼 AI·全景分析 —— 六大板块 69 个数据源抓取采集器（零第三方依赖，仅标准库）。

为什么这么设计
------------
- 华尔街见闻 / 雪球 / 金十 / 法布等站点页面为 **前端 JS 渲染**，部分还需登录，
  直接 GET 只能拿到空壳，无法解析出条目。
- 因此每个源同时记录“源头站”(origin) 与一个公开的**热榜聚合通道** rebang.vip
  （该聚合页为服务端渲染，标题与链接均回指源头站原文，是稳定的抓取通道）。
- 6 个热搜/热点源（知乎、抖音、微博、虎扑、AI Hot、Google news 中文）来自
  ourongxing/newsnow 项目，使用直接 API / HTML 抓取。
- 「政策发布 · 官方信息源」板块：国务院 / 部委政策发布列表页（changwu/china-policy-sites 清单，
  服务端渲染、无 RSS，按链接正则抓）+ 中国新闻社时政 RSS + 境外央行 / 监管机构官方 RSS·Atom
  （angelinajh/regtech-policy-tracker 的做法）。
- 「全球政经媒体」板块：edoardottt/news-list 清单里的政治 / 地缘 / 经济头部媒体官方 RSS。
- 「公民科技 · 政治透明度」板块：g0v 生态立法院开放 API、keepittechie/equitystack 公开接口、GovTrack RSS。
- 抓取顺序：自定义收集器 / RSS·Atom / 列表页 / 聚合通道 → 内置演示数据兜底，
  保证任何环境（本地 / GitHub Pages / GitHub Actions）都能稳定出结果；各源并发抓取。

对外接口
--------
    SECTIONS           : 六大板块目录（key / label / note），每个源的 section 归属其一
    SOURCES            : 全部源的名称列表（与前端 /api/sources、推送保持一致）
    SOURCE_META        : 每个源的元信息（name / section / origin / channel / collector / feed / page）
    section_catalog()  : 板块 → 源清单（供 /api/sections）
    collect_all()      : 并发抓取全部源，返回 {name: [item, ...]}
    collect_section(k) : 只抓某个板块（finance / trending / community / policy / world / civic）
    collect_one(name)  : 抓取单个源，返回 [item, ...]
    analyze_brief()    : 本地「AI 总结」引擎（主题热度 + 多空博弈概率）
    analyze_policy()   : 「AI 政策分析」引擎（政策维度热度 + 多空方向 + 鹰鸽取向；始终附带近30日全球政策
                         对 A股/港股/美股的影响。deep=True 时再叠加知识库检索 / 修饰词口径 / 舆情情感 /
                         传导图谱 / 分析师推理链 / 研报 / LSTM × Prophet）
    retrieve_policy_context() : 政策法规知识库检索（RAG 的检索环节，返回历史政策条目与检索理由）
    lstm_policy_forecast()    : 纯 Python 单层 LSTM + BPTT，评估政策后的中长期走势节奏
    prophet_policy_decompose(): Prophet 式加性分解（趋势 + 季节 + 政策效应），剥离季节看政策窗口波动
    get_ashare_market(): 采集最新 A 股行情（东方财富主接口 → 腾讯证券备用接口 → 内置快照兜底）
    analyze_ashare()   : 最新 A 股六维度看盘引擎（三大指数/成交额/涨跌家数/板块/资金/后市）
    get_hk_market()    : 采集最新港股行情（恒生/恒生科技/国企指数，主备源 + 快照兜底）
    analyze_hk()       : 最新港股看盘引擎（三大指数/成交额/涨跌家数/板块/南向资金/后市）
    get_us_market()    : 采集最新美股行情（道指/标普/纳指，主备源 + 快照兜底）
    analyze_us()       : 最新美股看盘引擎（三大指数/成交额/涨跌家数/板块/资金避险/后市）
    pushplus_quota()   : 推送容量口径（默认 PushPlus 10 万字 / 98,000 字符 + 每源 20 条）
    default_fetch_limit() : 每个数据源默认抓取条数（与推送口径一致）
    check_market_freshness() : 大盘数据新鲜度检查（推送前闸门：不是最新就不推）
    collect_market_for_push(): 推送入口专用，一次抓取返回 (market, freshness)
    build_html(brief)  : 由采集结果生成适合微信阅读的 HTML 简报（「AI 每日总结 → AI 看盘 → AI 情绪热力图
                         → AI 政策分析 → AI 政策深度 → AI 政策研报 → AI 板块机会」，正文最后追加「全网快讯」列表：
                         每源 3 条、跨源去重、不标注来源）
"""
from __future__ import annotations

import html
import json
import math
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import datetime, timedelta, timezone
import policy_impact
from urllib.parse import urlencode, urljoin
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor

TIMEOUT = 8
LIMIT = 5             # 每个源默认取前 5 条（普通账号 2 万字口径够用）
LIMIT_MEMBER = 20     # 10 万字口径下每源抓 20 条：把放宽的字符额度真正用起来
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ---------------------------------------------------------------- 板块定义
# 每个数据源归属一个「板块」(section)。板块是抓取与呈现的分组口径：
#   collect_section() 按板块抓取、/api/sections 按板块列源、简报末尾「全网快讯」按板块分组。
SECTIONS = [
    {"key": "finance",  "label": "财经快讯",
     "note": "境内外财经资讯与 7×24 快讯（rebang.vip 聚合通道，标题与链接回指源头站）"},
    {"key": "trending", "label": "热搜热点",
     "note": "社交平台热榜与 AI 热点（ourongxing/newsnow 同款直连抓取）"},
    {"key": "community", "label": "财经社区 · 中英文观点",
     "note": "20 个中文、英文与不同投资风格社区（股票 / ETF / 价值 / 期权 / 量化 / 长期配置）"},
    {"key": "policy",   "label": "政策发布 · 官方信息源",
     "note": "国务院与部委政策原文、境外央行与监管机构官方发布"
             "（changwu/china-policy-sites 站点清单 · angelinajh/regtech-policy-tracker 式官方 RSS）"},
    {"key": "world",    "label": "全球政经媒体",
     "note": "国际政治 / 地缘 / 经济头部媒体 RSS（edoardottt/news-list 清单）"},
    {"key": "civic",    "label": "公民科技 · 政治透明度",
     "note": "立法追踪、政策承诺核实等公民科技项目开放数据（g0v 生态 · keepittechie/equitystack · GovTrack）"},
]
SECTION_KEYS = [s["key"] for s in SECTIONS]
SECTION_LABELS = {s["key"]: s["label"] for s in SECTIONS}

# 政府网站列表页的条目链接特征（用 page 抓取器时按正则筛选 <a href>，排除导航 / 栏目链接）。
_GOVCN_PATTERN = r"gov\.cn/(?:zhengce|lianbo|yaowen|zhuanti)/.*content_\d+\.htm"


def _community_search_feed(domain: str, language: str = "zh") -> str:
    """用 Google News RSS 检索公开社区帖子。

    部分财经社区为前端渲染、没有 RSS；这里沿用 Reuters 数据源已采用的 Google News
    公共 RSS 通道。抓不到内容时仍按单源回退到明确标注的演示标题。
    """
    if language == "en":
        locale = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    else:
        locale = {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    return "https://news.google.com/rss/search?" + urlencode({
        "q": f"site:{domain} when:7d",
        **locale,
    })


_COMMUNITY_HEADERS = {"User-Agent": f"{UA} zhangyu-ai-community/1.0"}

# ---------------------------------------------------------------- 源定义
# 抓取方式四选一（collect_one 依次判定）：
#   collector : 自定义收集器（_COLLECTORS 注册表里的函数名）
#   feed      : RSS 2.0 / Atom 订阅地址（可选 headers / strip_suffix）
#   page      : 服务端渲染的列表页 + pattern（条目链接正则），相对链接自动补全
#   channel   : rebang.vip 聚合页路径 + origin 源头站域名（用于链接过滤）
# 任何方式失败都回退到 _DEMO 演示数据，保证离线也能出简报。
SOURCE_META = [
    # --- 财经快讯（rebang.vip 聚合通道）---
    {"name": "MKTNews 快讯",    "section": "finance", "origin": "mktnews.net",      "channel": "https://www.rebang.vip/mktnews/hot-list"},
    {"name": "华尔街见闻 快讯",  "section": "finance", "origin": "wallstreetcn.com", "channel": "https://www.rebang.vip/wallstreetcn/quick"},
    {"name": "华尔街见闻最新",   "section": "finance", "origin": "wallstreetcn.com", "channel": "https://www.rebang.vip/wallstreetcn/hot-news"},
    {"name": "华尔街见闻 最热",  "section": "finance", "origin": "wallstreetcn.com", "channel": "https://www.rebang.vip/wallstreetcn/hot-list"},
    {"name": "财联社 电报",      "section": "finance", "origin": "cls.cn",           "channel": "https://www.rebang.vip/cailianshe/telegram"},
    {"name": "财联社 深度",      "section": "finance", "origin": "cls.cn",           "channel": "https://www.rebang.vip/cailianshe/depth"},
    {"name": "财联社 热门",      "section": "finance", "origin": "cls.cn",           "channel": "https://www.rebang.vip/cailianshe/hot-list"},
    {"name": "雪球 热门股票",    "section": "finance", "origin": "xueqiu.com",       "channel": "https://www.rebang.vip/xueqiu/7-24-hot"},
    {"name": "格隆汇 事件",      "section": "finance", "origin": "gelonghui.com",    "channel": "https://www.rebang.vip/gelonghui/hot-list"},
    {"name": "法布财经 快讯",    "section": "finance", "origin": "fastbull.com",     "channel": "https://www.rebang.vip/fabubaijiance/hot-express"},
    {"name": "法布财经 头条",    "section": "finance", "origin": "fastbull.com",     "channel": "https://www.rebang.vip/fabubaijiance/hot-news"},
    {"name": "金十数据",        "section": "finance", "origin": "jin10.com",        "channel": "https://www.rebang.vip/jinshishuju/hot-list"},
    # --- 热搜热点（来自 ourongxing/newsnow 项目的 6 个直连源）---
    {"name": "知乎热榜",        "section": "trending", "collector": "zhihu"},
    {"name": "抖音热搜",        "section": "trending", "collector": "douyin"},
    {"name": "微博实时热搜",    "section": "trending", "collector": "weibo"},
    {"name": "虎扑热搜",        "section": "trending", "collector": "hupu"},
    {"name": "AI Hot",          "section": "trending", "collector": "aihot"},
    {"name": "Google news 中文", "section": "trending", "collector": "google_news"},
    # --- 财经社区（新增 20 个中英文 / 不同投资类型社区）---
    # 中文社区大多没有稳定 RSS，使用 Google News 公共 RSS 检索其公开帖子；英文 Reddit
    # 社区直接读取 Atom。每个源都保留离线演示标题，任一站点限流不会拖垮整份简报。
    {"name": "雪球 讨论精选", "section": "community", "feed": _community_search_feed("xueqiu.com"),
     "language": "zh", "community_type": "综合投资"},
    {"name": "东方财富 股吧热议", "section": "community", "feed": _community_search_feed("guba.eastmoney.com"),
     "language": "zh", "community_type": "个股讨论"},
    {"name": "集思录 投资社区", "section": "community", "feed": _community_search_feed("jisilu.cn"),
     "language": "zh", "community_type": "ETF · 可转债"},
    {"name": "淘股吧 市场讨论", "section": "community", "feed": _community_search_feed("taoguba.com.cn"),
     "language": "zh", "community_type": "短线交易"},
    {"name": "富途 牛牛圈", "section": "community", "feed": _community_search_feed("futunn.com"),
     "language": "zh", "community_type": "港美股"},
    {"name": "老虎社区", "section": "community", "feed": _community_search_feed("laohu8.com"),
     "language": "zh", "community_type": "港美股"},
    {"name": "TradingView 中文观点", "section": "community", "feed": _community_search_feed("cn.tradingview.com"),
     "language": "zh", "community_type": "技术分析"},
    {"name": "知乎 财经话题", "section": "community", "feed": _community_search_feed("zhihu.com/topic/19550517"),
     "language": "zh", "community_type": "财经问答"},
    {"name": "Reddit r/investing", "section": "community", "feed": "https://www.reddit.com/r/investing/new/.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "综合投资"},
    {"name": "Reddit r/stocks", "section": "community", "feed": "https://www.reddit.com/r/stocks/new/.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "股票"},
    {"name": "Reddit r/ValueInvesting", "section": "community", "feed": "https://www.reddit.com/r/ValueInvesting/new/.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "价值投资"},
    {"name": "Reddit r/SecurityAnalysis", "section": "community", "feed": "https://www.reddit.com/r/SecurityAnalysis/new/.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "基本面研究"},
    {"name": "Reddit r/dividends", "section": "community", "feed": "https://www.reddit.com/r/dividends/new/.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "股息策略"},
    {"name": "Reddit r/options", "section": "community", "feed": "https://www.reddit.com/r/options/new/.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "期权"},
    {"name": "Reddit r/algotrading", "section": "community", "feed": "https://www.reddit.com/r/algotrading/new/.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "量化交易"},
    {"name": "Reddit r/ETFs", "section": "community", "feed": "https://www.reddit.com/r/ETFs/new/.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "ETF 配置"},
    {"name": "Bogleheads 论坛", "section": "community", "feed": "https://www.bogleheads.org/forum/feed.php",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "长期指数"},
    {"name": "Stocktwits 市场脉搏", "section": "community", "feed": _community_search_feed("stocktwits.com", "en"),
     "language": "en", "community_type": "实时情绪"},
    {"name": "QuantConnect 量化社区", "section": "community", "feed": _community_search_feed("quantconnect.com/forum", "en"),
     "language": "en", "community_type": "量化研究"},
    {"name": "Elite Trader 论坛", "section": "community", "feed": "https://www.elitetrader.com/et/forums/-/index.rss",
     "headers": _COMMUNITY_HEADERS, "language": "en", "community_type": "专业交易"},
    # --- 政策发布 · 官方信息源 ---
    # 境内：changwu/china-policy-sites 清单中的国务院 / 部委「政策发布」列表页（服务端渲染，无 RSS，按链接正则抓）。
    {"name": "国务院 最新政策",   "section": "policy", "page": "https://www.gov.cn/zhengce/zuixin/", "pattern": _GOVCN_PATTERN},
    {"name": "国务院 政策解读",   "section": "policy", "page": "https://www.gov.cn/zhengce/jiedu/",  "pattern": _GOVCN_PATTERN},
    {"name": "国务院 政务联播",   "section": "policy", "page": "https://www.gov.cn/lianbo/",         "pattern": _GOVCN_PATTERN},
    {"name": "发改委 政策发布",   "section": "policy", "page": "https://www.ndrc.gov.cn/xxgk/zcfb/tz/",
     "pattern": r"ndrc\.gov\.cn/xxgk/zcfb/.+/t\d{8}_\d+\.html"},
    {"name": "财政部 政策发布",   "section": "policy", "page": "https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/",
     "pattern": r"mof\.gov\.cn/.+/t\d{8}_\d+\.htm"},
    {"name": "商务部 政策发布",   "section": "policy", "page": "https://www.mofcom.gov.cn/zwgk/zcfb/index.html",
     "pattern": r"mofcom\.gov\.cn/zwgk/zcfb/art/\d{4}/art_\w+\.html"},
    {"name": "证监会 新闻发布",   "section": "policy", "page": "https://www.csrc.gov.cn/csrc/xwfb/index.shtml",
     "pattern": r"csrc\.gov\.cn/csrc/c(?:100028|106311|100039)/c\w+/content\.shtml"},
    {"name": "香港特区政府 新闻公报", "section": "policy", "feed": "https://www.info.gov.hk/gia/rss/general_zh.xml"},
    # 境内官媒：中国新闻社（中新社）时政新闻官方 RSS，中央 / 部委政策发布与人事任免的官媒口径。
    {"name": "中国新闻社 时政", "section": "policy", "feed": "https://www.chinanews.com.cn/rss/china.xml"},
    # 境外：angelinajh/regtech-policy-tracker 的做法 —— 直接订阅央行 / 监管机构官方 RSS（Atom 亦可）。
    {"name": "美联储 新闻稿",     "section": "policy", "feed": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "欧洲央行 新闻稿",   "section": "policy", "feed": "https://www.ecb.europa.eu/rss/press.html"},
    {"name": "美国 SEC 新闻稿",   "section": "policy", "feed": "https://www.sec.gov/news/pressreleases.rss",
     # SEC 要求自动化访问声明身份（否则 403），见 https://www.sec.gov/os/accessing-edgar-data
     "headers": {"User-Agent": "zhangyu-ai-brief/1.0 (github.com/k-macao/05)"}},
    {"name": "美国联邦公报 总统文件", "section": "policy",
     "feed": "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Btype%5D%5B%5D=PRESDOCU&order=newest"},
    {"name": "英国财政部 GOV.UK", "section": "policy", "feed": "https://www.gov.uk/government/organisations/hm-treasury.atom"},
    {"name": "英国 FCA 新闻",     "section": "policy", "feed": "https://www.fca.org.uk/news/rss.xml"},
    # --- 全球政经媒体（edoardottt/news-list 清单里的政治 / 地缘 / 经济头部媒体，走官方 RSS）---
    # 路透官方 RSS 已停止，改经 Google News RSS 检索 reuters.com 近 24 小时报道（标题去掉「 - Reuters」后缀）。
    {"name": "Reuters 路透",      "section": "world", "strip_suffix": " - Reuters",
     "feed": "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Bloomberg 政治",    "section": "world", "feed": "https://feeds.bloomberg.com/politics/news.rss"},
    {"name": "Bloomberg 经济",    "section": "world", "feed": "https://feeds.bloomberg.com/economics/news.rss"},
    {"name": "Financial Times",  "section": "world", "feed": "https://www.ft.com/rss/home"},
    {"name": "纽约时报 国际",      "section": "world", "feed": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"},
    {"name": "华盛顿邮报 政治",    "section": "world", "feed": "https://feeds.washingtonpost.com/rss/politics"},
    {"name": "POLITICO",         "section": "world", "feed": "https://rss.politico.com/politics-news.xml"},
    {"name": "Foreign Policy",   "section": "world", "feed": "https://foreignpolicy.com/feed/"},
    {"name": "The Diplomat",     "section": "world", "feed": "https://thediplomat.com/feed/"},
    {"name": "经济学人 财经",      "section": "world", "feed": "https://www.economist.com/finance-and-economics/rss.xml"},
    {"name": "日经亚洲",          "section": "world", "feed": "https://asia.nikkei.com/rss/feed/nar"},
    {"name": "南华早报",          "section": "world", "feed": "https://www.scmp.com/rss/91/feed"},
    # --- 公民科技 · 政治透明度（开放 API / RSS，只取公开条目）---
    {"name": "g0v 立法院議案",      "section": "civic", "collector": "ly_bills"},
    {"name": "EquityStack 政策承诺", "section": "civic", "collector": "equitystack_promises"},
    {"name": "EquityStack 法案追踪", "section": "civic", "collector": "equitystack_bills"},
    {"name": "GovTrack 国会立法",    "section": "civic",
     "feed": "https://www.govtrack.us/events/events.rss?feeds=misc:activebills2"},
]

SOURCES = [m["name"] for m in SOURCE_META]


def source_meta(name: str) -> dict | None:
    """按名称取数据源元信息（找不到返回 None）。"""
    return next((m for m in SOURCE_META if m["name"] == name), None)


def section_of(name: str) -> str:
    """数据源所属板块 key；未知源归入 ``finance``（保持旧调用方行为）。"""
    meta = source_meta(name)
    return (meta or {}).get("section") or "finance"


def sources_in_section(key: str) -> list:
    """某板块下的数据源名称列表（保持 SOURCE_META 顺序）。"""
    return [m["name"] for m in SOURCE_META if m.get("section") == key]


def section_catalog() -> list:
    """板块目录：[{key, label, note, sources: [name, ...], count}]，供 /api/sections 与文档使用。"""
    return [dict(section, sources=sources_in_section(section["key"]),
                 count=len(sources_in_section(section["key"]))) for section in SECTIONS]

# 兜底演示数据：网络不可用时返回，保证任何环境都能出简报。
_DEMO = {
    "MKTNews 快讯": [
        "TIMIRAOS: BESSENT policy reaction function shifts, no longer so dovish",
        "Procter & Gamble (PG.N) +1% on report it will buy Thorne for $3.8B",
        "Iran FM: talks aim to safeguard sovereign rights of Iran and Oman",
        "Lockheed in talks to buy U.S. scandium / germanium from NioCorp, Teck, 5N Plus",
        "EasyJet French cabin crew union filed strike notice (Aug 7 - Sep 2)",
    ],
    "华尔街见闻 快讯": [
        "淡水河谷CEO：伊朗战争造成诸多项目的速度放缓",
        "诺和诺德2026年业绩前景改善，谈及GLP-1销售",
        "“新美联储通讯社”：贝森特政策反应函数转向不再那么鸽派",
        "USA Rare Earth 涨幅扩大至8.9%，洛克希德·马丁洽购美国矿山关键矿产",
        "标普500涨幅扩大至1.5%，道指涨925点，半导体指数涨超6%",
    ],
    "华尔街见闻最新": [
        "市场错判了AI？云业务积压订单暴增150%达1.7万亿美元",
        "沙特阿美Q2净利暴增33%，CEO警告海峡封锁引发“历史最大石油供应冲击”",
        "Citadel证券：美股7月暴跌不是牛市终结，而是“技术性重置”",
        "7月楼市淡季不淡：多城成交同比保持增长，上海豪宅开盘日光",
        "美国6月JOLTS职位空缺超预期回落，裁员有限、招聘回升",
    ],
    "华尔街见闻 最热": [
        "华尔街见闻早餐FM-Radio | 2026年8月4日",
        "美国拟禁止进口中国新型数据中心设备，我使馆：敦促美停止抹黑中企并威胁制裁",
        "美股8月开门红，道指新高，亚马逊市值首破三万亿，原油重挫",
        "罕见干预操作！贝森特抛欧元、指示美联储“借钱”，让日本“别抛美债”",
        "创业板暴涨超5.6%，算力硬件全线反攻、光通信“满屏涨停”",
    ],
    "财联社 电报": [
        "诺和诺德预计本财年固定汇率口径调整后销售额同比-6%至0%",
        "伊朗外交部发言人：与阿曼在霍尔木兹海峡过境问题谈判仍在继续",
        "日本古河电工宣布投资1000亿日元扩大光纤产能 目标产能翻倍",
        "Blue Owl完成欧洲净租赁基金募集，总承诺资本超16亿欧元",
        "摩根大通据悉接近达成收购Antin旗下法国区域制冷网络供应商的交易",
    ],
    "财联社 深度": [
        "债务逾期、25起诉讼待解、公司及实控人被立案 联创光电面临多重危机",
        "Anthropic再掀算力争夺战：据称斥资100亿美元抢占数据中心资源",
        "又有海外光纤巨头宣布产能翻倍计划 已获得客户长期采购承诺",
        "AI泡沫争议未阻热情 亿万富豪家族办公室竞逐机器人赛道",
        "贝森特详解美日联手干预日元：防止弱日元冲击亚洲市场稳定",
    ],
    "财联社 热门": [
        "L3、L4级自动驾驶“强标”明年7月实施 明确安全主体责任、接管机制",
        "央行将开展5000亿元买断式逆回购 8月流动性面临两大扰动",
        "日元又成了全世界的问题？全球最关键套利交易遭“降维打击”",
        "一图看懂｜业绩炸场！CXO“盈利王”强势涨停带动板块修复",
        "多家券商股东出手增持！是稳信心，更是直接开拿低价筹码",
    ],
    "雪球 热门股票": [
        "据Argus Media，法国玉米产量降至690万吨，创最近50年新低",
        "现货铂金上涨超过7%，报1,745.93美元/盎司",
        "【诺和诺德上调2026年调整后销售额和调整后营业利润预期】",
        "伊朗外交部发言人：与阿曼在霍尔木兹海峡过境问题谈判仍在继续",
        "【日本古河电工宣布投资1000亿日元扩大光纤产能 目标产能翻倍】",
    ],
    "格隆汇 事件": [
        "矽电股份(301629)：将继续聚焦探针台领域",
        "盛达资源(000603.SZ)：预计妙皇铜铅锌银矿矿山建设总投资6-7亿元",
        "鲁西化工(000830.SZ)：今年加大产品出口力度，提高国外收入占比",
        "胜通能源(001331.SZ)：七腾机器人生产基地主要位于重庆、合肥等",
        "美股异动丨麦当劳涨超2%，Q2调整后每股收益超预期",
    ],
    "法布财经 快讯": [
        "现货钯金上涨超过7%，报1,745.93美元/盎司",
        "淡水河谷CEO：伊朗战争造成诸多项目的速度放缓",
        "现货铂金上涨超过7%，报1,745.93美元/盎司",
        "“美联储传声筒”：贝森特政策反应函数转向不再那么鸽派",
        "钯金期货日内涨8%，现报1358.00美元/盎司",
    ],
    "法布财经 头条": [
        "特朗普押注最后谈判机会，日元干预规模再创高位",
        "中国内地企业在香港上市指南：条件、流程、优势及注意事项",
        "吴清支持内地企业香港上市：政策信号与市场影响",
        "美伊暂缓军事冲突，日元迎美日联合干预",
        "日本疑似出手干预汇市，美伊冲突外溢风险升温",
    ],
    "金十数据": [
        "现货钯金上涨超过7%，报1,745.93美元/盎司",
        "诺和诺德(NVO.N)：第二季度营业利润受到美国340B返利拨备转回的负面影响",
        "阿根廷出口与加工商会CIARA-CEC：阿根廷港口工人罢工影响超过45艘船只",
        "诺和诺德：2026年第二季度调整后销售额784.9亿丹麦克朗、营业利润333.9亿",
        "“美联储传声筒”：贝森特政策反应函数转向不再那么鸽派",
    ],
    "知乎热榜": [
        "如何看待 2026 年高考录取分数线公布？",
        "为什么现在的年轻人越来越喜欢独居？",
        "有哪些让你觉得「涨知识了」的冷知识？",
        "如何评价电影《奥本海默》？",
        "你经历过的最离谱的骗局是什么？",
    ],
    "抖音热搜": [
        "高考加油 为梦想而战",
        "今日份好消息分享",
        "夏天就要吃西瓜",
        "旅行推荐 避暑胜地",
        "职场新人避坑指南",
        "抖音电商新趋势",
        "周杰伦演唱会门票抢购",
        "如何在家做冰镇咖啡",
        "自驾游路线推荐",
        "职场高效办公插件",
    ],
    "微博实时热搜": [
        "高考成绩陆续公布",
        "某明星官宣结婚",
        "暑期档电影推荐",
        "高温预警 注意防暑",
        "国足世预赛最新战报",
        "杭州亚运会筹备进展",
        "SpaceX 发射最新进展",
        "苹果发布会预测",
        "今日影讯早知道",
        "专家谈年轻人理财",
    ],
    "虎扑热搜": [
        "NBA 总决赛 G7 赛后讨论",
        "欧冠决赛精彩回顾",
        "步行街主干道 今日热议",
        "球鞋发售日历 8月新款",
        "电竞 S赛 战队分析",
    ],
    "AI Hot": [
        "OpenAI 发布 GPT-5 技术预览版",
        "Anthropic Claude 4 性能基准测试",
        "Google DeepMind 新论文：多模态推理",
        "开源大模型 Llama 4 发布",
        "AI 编程助手效率对比评测",
    ],
    "Google news 中文": [
        "全球科技巨头发布财报，AI 支出超预期",
        "亚太股市周一开盘走势分化",
        "美联储会议纪要暗示通胀路径仍存不确定性",
        "新能源汽车全球销量创历史新高",
        "全球气候峰会达成初步减排协议",
    ],
    # --- 财经社区 · 中英文观点（离线演示标题，不冒充实时帖子）---
    "雪球 讨论精选": [
        "社区讨论：港股科技 ETF 的估值与盈利修复如何匹配",
        "投资者复盘：黄金上涨后是否仍有分散配置价值",
        "话题观察：AI 数据中心资本开支与现金流验证",
    ],
    "东方财富 股吧热议": [
        "股吧热议：半导体设备订单回升仍需哪些数据确认",
        "市场讨论：新能源车价格竞争与电池产业链利润",
        "盘后交流：成交放量后关注高位板块分化风险",
    ],
    "集思录 投资社区": [
        "ETF 讨论：恒生科技与宽基指数如何做组合再平衡",
        "可转债观察：溢价率回落后的风险收益比较",
        "固收话题：长久期美债对利率变化为何更敏感",
    ],
    "淘股吧 市场讨论": [
        "短线复盘：光通信冲高后分歧扩大，等待订单验证",
        "交易讨论：放量突破与假突破应如何区分",
        "市场情绪：涨停家数回升但追高风险仍需控制",
    ],
    "富途 牛牛圈": [
        "牛牛圈讨论：南向资金流入港股科技与高股息板块",
        "美股话题：大型科技财报后 AI 投资回报成为焦点",
        "港股观察：生物科技反弹能否获得成交额配合",
    ],
    "老虎社区": [
        "社区热议：Fed rate path and growth-stock valuation",
        "投资笔记：原油波动如何影响能源与运输板块",
        "财报讨论：消费品牌指引改善但需求仍有分化",
    ],
    "TradingView 中文观点": [
        "技术观点：恒生指数测试区间上沿，关注成交确认",
        "图表分析：黄金趋势延续但短线动能有所降温",
        "交易计划：美元与美债收益率背离值得跟踪",
    ],
    "知乎 财经话题": [
        "如何理解 ETF 跟踪误差、流动性与折溢价之间的关系？",
        "美联储加息或降息为什么会影响港股成长股估值？",
        "普通投资者如何区分行业主题 ETF 与宽基 ETF？",
    ],
    "Reddit r/investing": [
        "Discussion: balancing global equities and long-duration Treasury exposure",
        "How investors are evaluating AI capital spending versus free cash flow",
        "Portfolio review: diversification when gold and stocks rise together",
    ],
    "Reddit r/stocks": [
        "Semiconductor earnings thread: demand growth, margins and inventory risk",
        "EV makers rally as deliveries beat estimates, but pricing pressure remains",
        "What risks could derail the latest technology rebound?",
    ],
    "Reddit r/ValueInvesting": [
        "Value discussion: finding a margin of safety in consumer businesses",
        "Research thread: normalizing bank earnings through the interest-rate cycle",
        "Case study: when a cheap property company is still a value trap",
    ],
    "Reddit r/SecurityAnalysis": [
        "Deep dive: cloud computing unit economics and data-center depreciation",
        "Industry note: biotech pipelines, patent risk and probability-adjusted value",
        "Company analysis: commodity producer cash flows across the cycle",
    ],
    "Reddit r/dividends": [
        "Dividend portfolio review: yield growth versus concentration risk",
        "Discussion: high-dividend equities when interest rates stay elevated",
        "How to compare distribution yield with total return for an ETF",
    ],
    "Reddit r/options": [
        "Options discussion: implied volatility around technology earnings",
        "Risk management: defined-risk spreads versus uncovered positions",
        "Market thread: hedging a broad equity ETF without overpaying for volatility",
    ],
    "Reddit r/algotrading": [
        "Research discussion: avoiding look-ahead bias in sentiment strategies",
        "Backtest review: transaction costs erased a short-term momentum edge",
        "Model thread: combining news signals with volatility targeting",
    ],
    "Reddit r/ETFs": [
        "ETF comparison: Hang Seng TECH exposure, fees and tracking difference",
        "Portfolio question: pairing equity themes with short-term Treasuries",
        "Discussion: when two similar ETFs can still have different liquidity",
    ],
    "Bogleheads 论坛": [
        "Asset allocation discussion: staying diversified through volatile markets",
        "Index investing question: rebalancing bands versus calendar rebalancing",
        "Bond duration thread: matching portfolio risk to investment horizon",
    ],
    "Stocktwits 市场脉搏": [
        "Trending discussion: chip stocks rally after stronger demand outlook",
        "Market pulse: gold sentiment rises as geopolitical risk returns",
        "Ticker stream: biotech rebounds while traders debate trial risk",
    ],
    "QuantConnect 量化社区": [
        "Research post: cross-source news sentiment with walk-forward validation",
        "Algorithm discussion: volatility scaling for ETF rotation models",
        "Data issue: survivorship bias in historical universe selection",
    ],
    "Elite Trader 论坛": [
        "Trader discussion: liquidity and slippage in index ETF execution",
        "Macro thread: yield-curve changes and equity factor rotation",
        "Risk desk topic: position sizing during crude oil volatility",
    ],
    # --- 政策发布 · 官方信息源 ---
    "国务院 最新政策": [
        "国务院办公厅转发文化和旅游部等部门《关于促进房车消费的若干措施》的通知",
        "国务院办公厅关于进一步加强烟花爆竹全链条安全监管的意见",
        "国务院办公厅关于加强中小企业回款难问题治理有关工作的通知",
        "电力安全事故应急处置和调查处理条例",
    ],
    "国务院 政策解读": [
        "解读：国务院常务会议部署实施医疗康复护理扩容提升工程",
        "解读：打通堵点释放潜力 十部门推出促进房车消费若干措施",
        "商务部有关负责人解读《促进智能家居消费行动方案》",
        "总体平稳 向新向优：7组数字看8月份中国经济",
    ],
    "国务院 政务联播": [
        "商务部新闻发言人就中美经贸磋商有关问题答记者问",
        "农业农村部 工业和信息化部关于印发《全国农业机械化发展“十五五”规划》的通知",
        "前8个月全国一般公共预算收入同比增长5.7%",
        "工业和信息化部等十部门关于印发《医药工业发展“十五五”规划》的通知",
        "国家发展改革委：安排3000万元支持海南暴雨洪涝灾害灾后应急恢复",
    ],
    "发改委 政策发布": [
        "国家发展改革委关于印发《全国重点电力用户清单管理办法》的通知",
        "国家发展改革委 国家能源局关于加快推进新型储能规模化应用的指导意见",
        "关于组织开展 2026 年度国家级新型工业化产业示范基地申报工作的通知",
        "国家发展改革委关于完善价格治理机制促进民营经济发展的若干措施",
    ],
    "财政部 政策发布": [
        "《紧急采购管理暂行办法》印发",
        "《关于加快农业保险高质量发展的实施方案》印发",
        "财政部等三部门联合发文部署进一步做好财政金融协同促内需政策有关工作",
        "财政部 税务总局关于调整部分能源资源行业企业城镇土地使用税政策的公告",
        "关于扩大地方政府专项债券项目“自审自发”试点范围的通知",
    ],
    "商务部 政策发布": [
        "商务部等8部门关于印发《促进智能家居消费行动方案》的通知",
        "商务部公告2026年第38号 公布延长对原产于墨西哥和美国的进口碧根果反倾销调查期限决定",
        "商务部 工业和信息化部 市场监管总局关于印发《汽车行业境外竞争行为与合规建设指引》的通知",
        "商务部公告2026年第34号 公布加强无人机相关两用物项对美国出口管制",
        "中华人民共和国商务部令二〇二六年第3号 关于对美国合规性测试公司采取反制措施的决定",
    ],
    "证监会 新闻发布": [
        "中国证监会拟对17起案件线索的“吹哨人”给予奖励",
        "中国证监会发布《期货公司监督管理办法》及配套实施公告",
        "中国证监会严肃查处*ST卓然严重财务造假案件",
        "国新办举行“开局起步‘十五五’”系列主题新闻发布会 介绍金融领域贯彻落实“十五五”规划、推动金融强国建设有关情况",
        "中国证监会发布《衍生品交易监督管理办法（试行）》",
    ],
    "香港特区政府 新闻公报": [
        "第六屆粵港澳大灣區律師執業考試順利舉行",
        "香港特別行政區政府公布《香港特別行政區經濟和社會發展第一個五年規劃（2026—2030年）》",
        "立法會研究「生態＋旅遊」事宜小組委員會考察香港聯合國教科文組織世界地質公園",
        "衞生防護中心調查一宗猴痘確診個案",
    ],
    "中国新闻社 时政": [
        "中共中央办公厅 国务院办公厅印发《关于分类推进高校改革的意见》",
        "国务院任免国家工作人员",
        "市场监管总局发布《婴幼儿配方食品标签标识问答》",
        "两部门：到2030年我国农作物耕种收综合机械化率将超80%",
        "国家药监局：多项措施助力创新药、创新医疗器械发展提速",
    ],
    "美联储 新闻稿": [
        "Federal Reserve issues FOMC statement",
        "Federal Reserve Board announces approval of application by a bank holding company",
        "Minutes of the Federal Open Market Committee, July 28-29, 2026",
        "Federal Reserve Board releases results of annual bank stress test",
    ],
    "欧洲央行 新闻稿": [
        "Monetary policy decisions",
        "ECB Consumer Expectations Survey results – August 2026",
        "ECB wage tracker at 2.7% in H1 2027, pointing to a modest uptick in negotiated wage growth",
        "Christine Lagarde: A new age of capital: growth, sovereignty and AI",
    ],
    "美国 SEC 新闻稿": [
        "SEC Issues “Innovation Exemption” to Facilitate the Trading of Tokenized NMS Stock and Request for Comment",
        "SEC Proposes Rescission of Shareholder Proposal Rule and Reforms to Proxy Solicitation Process",
        "SEC Charges Founder and His Two New Jersey-Based Companies in Alleged $16 Million Ponzi Scheme",
        "SEC Proposes New Regulation Crypto Assets",
    ],
    "美国联邦公报 总统文件": [
        "Presidential Determination on Major Drug Transit or Major Illicit Drug Producing Countries for Fiscal Year 2027",
        "Modifying the Scope of Products of Canada Subject to the Additional Duties Imposed To Offset Canadian Discrimination Against the Commerce of the United States With Respect to Motor Vehicles",
        "Adjusting Certain Delegations Under the Defense Production Act",
        "Continuation of the National Emergency With Respect to Foreign Interference in or Undermining Public Confidence in United States Elections",
    ],
    "英国财政部 GOV.UK": [
        "Chancellor sets date for Autumn Budget 2026",
        "UK and US agree next steps on digital services taxation",
        "Government publishes response to consultation on pensions investment reform",
        "HM Treasury: Financial Services Growth and Competitiveness Strategy progress update",
    ],
    "英国 FCA 新闻": [
        "FCA sets out next steps on motor finance redress scheme",
        "FCA fines firm for failures in anti-money laundering controls",
        "FCA consults on simplifying the retail conduct rulebook",
        "FCA warns consumers about unauthorised crypto promotions",
    ],
    # --- 全球政经媒体 ---
    "Reuters 路透": [
        "German general Breuer elected to head top NATO military body",
        "Greenland, Denmark say Trump deal won't compromise sovereignty",
        "IMF tells EU ministers AI could boost growth but increase economic strains",
        "Russia reports online attacks on electoral system on second day of parliamentary vote",
        "UN peacekeeping chief in Lebanon says smooth transition is critical before pull-out",
    ],
    "Bloomberg 政治": [
        "US Reaches Greenland Deal to End Row That Threatened NATO",
        "China Slams US Law Tightening Sanctions on Russia and Iran",
        "US, China Trade Teams Set to Huddle in New York on AI, Iran",
        "US Tariffs Threaten Great Lakes Shipping Tied to Canada Trade",
        "Midterm Voting Starts in Key States With Election 45 Days Out",
    ],
    "Bloomberg 经济": [
        "BOJ Hikes Rates to 1.25% as Ueda Signals Shift in Policy Phase",
        "Germany Agrees on €2.5 Billion Fuel Relief, Price Cap Plan",
        "UK Mansion Tax May Expand to Homes Worth Over £1.5 Million",
        "Fed Officials Split on Pace of Cuts as Inflation Stays Sticky",
    ],
    "Financial Times": [
        "Investors warn Anthropic could struggle to sustain revenues post-IPO",
        "OpenAI expects to burn $280bn by 2030",
        "Nobel economists throw support behind California billionaire tax",
        "Trump says US has deal with Denmark for ‘control’ of Greenland’s security",
        "Unpacking the real fiscal costs of immigration",
    ],
    "纽约时报 国际": [
        "Trump Announces Greenland Security Deal With Denmark",
        "Russia Holds Parliamentary Vote Amid Reports of Cyberattacks",
        "Saudi Arabia Issues Rare Air-Raid Alerts for Riyadh",
        "E.U. Presses Partners to Deliver Ukraine Funding Ahead of U.N. Week",
    ],
    "华盛顿邮报 政治": [
        "White House bars CNN and MS NOW from grounds after Trump order",
        "Early voting begins in key states with midterms 45 days away",
        "Senate advances bill tightening sanctions on Russia and Iran",
        "Supreme Court to weigh limits on presidential tariff powers",
    ],
    "POLITICO": [
        "Congress races to avert shutdown as spending talks stall",
        "Trump signs Russia sanctions bill that opens China, India to tariffs",
        "Democrats sharpen midterm message on tariffs and prices",
        "Fed independence fight heads to Capitol Hill",
    ],
    "Foreign Policy": [
        "What the Greenland Deal Means for NATO",
        "Why China Is Rebuffing Calls for an AI Slowdown",
        "The Iran War Is Reshaping Gulf Security",
        "Europe’s Defense Spending Surge Faces a Reality Check",
    ],
    "The Diplomat": [
        "Takaichi Seeks Trump Meeting Ahead of US-China Summit",
        "Taiwan’s Legislature Debates Defense Special Budget",
        "South Korea’s Lee Rules Out Combat Deployment to the Middle East",
        "India’s Chip Ambitions Draw Japanese Suppliers",
    ],
    "经济学人 财经": [
        "Central banks are moving in historic alignment on rate rises",
        "What a stronger yen means for global markets",
        "The billionaire tax debate reaches California",
        "How tariffs are reshaping North American supply chains",
    ],
    "日经亚洲": [
        "BOJ hikes rates to 1.25% as chief Ueda cites shift in policy phase",
        "Trump signs Russia sanctions bill that opens China, India to tariffs",
        "China rebuffs AI slowdown calls ahead of Trump-Xi summit: 5 things to know",
        "Japan PM urges new cabinet to focus on markets, Mideast response",
        "Inflation draws BOJ, Fed, ECB into historic alignment on rate hikes",
    ],
    "南华早报": [
        "Hong Kong’s political, business elite pay final respects to Tung Chee-hwa",
        "Denmark cautiously optimistic over Trump’s Greenland deal",
        "Somali piracy rise tests China’s long-running Gulf of Aden mission",
        "US space weapons move has ‘broken taboo’, could spark new arms race, Beijing forum told",
    ],
    # --- 公民科技 · 政治透明度 ---
    "g0v 立法院議案": [
        "〔排入院會〕「食品安全衛生管理法部分條文修正草案」，請審議案。（本院委員陳菁徽等17人）",
        "〔排入院會〕「災害防救法第二十三條條文修正草案」，請審議案。（本院委員葉元之等19人）",
        "〔排入院會〕「遺產及贈與稅法第十五條、第十七條之一及第二十九條條文修正草案」，請審議案。（本院委員葉元之等19人）",
        "〔交付協商〕報告併案審查委員林月琴等16人擬具「教保服務人員條例第三十三條條文修正草案」等案。（教育及文化委員會）",
    ],
    "EquityStack 政策承诺": [
        "[Failed] Supreme Court narrows Section 2 redistricting protections in Louisiana v. Callais（Courts / Voting Rights / Civil Rights · Donald J. Trump）",
        "[In Progress] Prepare Americans for high-paying skilled trade jobs（Economy · Donald J. Trump）",
        "[Partial] Promote excellence and innovation at HBCUs（Education · Donald J. Trump）",
        "[Partial] End federal DEI and equity-based government programs（Economy · Donald J. Trump）",
    ],
    "EquityStack 法案追踪": [
        "H.R. 40｜Commission to Study and Develop Reparation Proposals for African Americans Act（In Committee · Referred to the House Committee on the Judiciary.）",
        "H.R. 14｜John R. Lewis Voting Rights Advancement Act（In Committee）",
        "S. 1｜Freedom to Vote Act（Introduced）",
    ],
    "GovTrack 国会立法": [
        "S. 5441: An original bill to improve services provided to taxpayers by the Internal Revenue Service.",
        "S. 4395: Terrorism Risk Insurance Program Reauthorization Act of 2026",
        "H.R. 5345: Improving Social Security’s Service to Victims of Identity Theft Act",
        "H.R. 9497: Water Resources Development Act of 2026",
        "H.R. 9576: National Fraud Enforcement Division Act of 2026",
    ],
}


# ---------------------------------------------------------------- HTML 链接解析
class LinkCollector(HTMLParser):
    """收集页面中所有 <a href=...>text</a> 的 (href, text)。"""
    def __init__(self):
        super().__init__()
        self._href = None
        self._buf = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            self._href = d.get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = "".join(self._buf).strip()
            if text:
                self.links.append((self._href, text))
            self._href = None
            self._buf = []


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_title(text: str) -> str:
    """聚合页里条目标题通常带一个相对时间前缀（如『19分钟前』），去掉它。"""
    t = _collapse(text)
    # 形如 "19分钟前 标题" / "1小时前 标题" / "1天前 标题"
    m = re.match(r"^(\d+[分钟小时天]\S*)\s+(.+)$", t)
    return m.group(2) if m else t


def _fetch_bytes(url: str, headers: dict | None = None) -> tuple:
    """GET 原始字节，返回 (body, 响应头声明的 charset 或 "")。"""
    req_headers = {"User-Agent": UA, "Accept": "*/*"}
    req_headers.update(headers or {})
    req = Request(url, headers=req_headers, method="GET")
    with urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(), (resp.headers.get_content_charset() or "")


def _decode(raw: bytes, charset: str = "") -> str:
    """按「响应头 charset → 页面 <meta charset> / XML encoding 声明 → UTF-8」顺序解码。

    部委站点仍有 GB2312/GBK 页面，统一用 gb18030 超集解码；未知编码名回退 UTF-8。
    """
    enc = (charset or "").strip().lower()
    if not enc:
        head = raw[:4096].decode("ascii", "ignore").lower()
        m = (re.search(r"""charset\s*=\s*["']?\s*([a-z0-9_-]+)""", head)
             or re.search(r"""<\?xml[^>]*encoding\s*=\s*["']([a-z0-9_-]+)""", head))
        enc = m.group(1) if m else "utf-8"
    if enc in ("gb2312", "gbk", "gb_2312", "gb_2312-80"):
        enc = "gb18030"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _fetch(url: str, headers: dict | None = None) -> str:
    raw, charset = _fetch_bytes(url, headers)
    return _decode(raw, charset)


def _get_json(url: str, headers: dict | None = None):
    """GET 并解析 JSON（开放 API 用）。"""
    req_headers = {"Accept": "application/json"}
    req_headers.update(headers or {})
    raw, charset = _fetch_bytes(url, req_headers)
    return json.loads(_decode(raw, charset or "utf-8"))


def _filter_items(links, origin: str, limit: int):
    """从 (href, text) 列表里筛出指向源头站的条目：去重、清理、截断。纯函数，便于测试。"""
    seen, items = set(), []
    for href, text in links:
        if origin not in href or href in seen:
            continue
        seen.add(href)
        title = _clean_title(text)
        if not title:
            continue
        items.append({"title": title, "url": href})
        if len(items) >= limit:
            break
    return items


def _parse_channel(url: str, origin: str, limit: int):
    """抓取聚合页并提取指向源头站的条目（去重、截断）。"""
    raw = _fetch(url)
    parser = LinkCollector()
    parser.feed(raw)
    return _filter_items(parser.links, origin, limit)


def _demo_items(name: str) -> list:
    return [{"title": t, "url": ""} for t in _DEMO.get(name, [])]


# ---------------------------------------------------------------- 新增 6 个源的抓取器
# 这些源使用直接 API / HTML 抓取（参考 ourongxing/newsnow 项目实现）

def _collect_zhihu(limit: int) -> list:
    """知乎热榜 - 直接 API 抓取。"""
    url = "https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=20&desktop=true"
    req = Request(url, headers={"User-Agent": UA}, method="GET")
    with urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = []
    for k in data.get("data", [])[:limit]:
        target = k.get("target", {})
        title = target.get("title_area", {}).get("text", "")
        link = target.get("link", {}).get("url", "")
        if title:
            items.append({"title": title, "url": link})
    return items


def _collect_douyin(limit: int) -> list:
    """抖音热搜 - 需要先获取 cookie。"""
    # 先从 login.douyin.com 获取 cookie
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    try:
        opener.open("https://login.douyin.com/", timeout=TIMEOUT)
    except (HTTPError, URLError, TimeoutError):
        return []
    
    # 用获取的 cookie 请求热搜 API
    url = "https://www.douyin.com/aweme/v1/web/hot/search/list/?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1"
    req = Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError):
        return []
    
    items = []
    for k in data.get("data", {}).get("word_list", [])[:limit]:
        word = k.get("word", "")
        sentence_id = k.get("sentence_id", "")
        if word and sentence_id:
            items.append({
                "title": word,
                "url": f"https://www.douyin.com/hot/{sentence_id}"
            })
    return items


class WeiboTableParser(HTMLParser):
    """解析微博热搜表格。"""
    def __init__(self):
        super().__init__()
        self._in_table = False
        self._in_tbody = False
        self._in_tr = False
        self._in_td_02 = False
        self._in_a = False
        self._current_href = None
        self._current_text = []
        self.items = []
    
    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "table":
            self._in_table = True
        elif tag == "tbody" and self._in_table:
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._in_tr = True
        elif tag == "td" and self._in_tr:
            cls = d.get("class", "")
            if "td-02" in cls:
                self._in_td_02 = True
        elif tag == "a" and self._in_td_02:
            href = d.get("href", "")
            if href and "javascript:void(0);" not in href:
                self._in_a = True
                self._current_href = href
                self._current_text = []
    
    def handle_data(self, data):
        if self._in_a:
            self._current_text.append(data)
    
    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            title = "".join(self._current_text).strip()
            if title and self._current_href:
                url = f"https://s.weibo.com{self._current_href}"
                self.items.append({"title": title, "url": url})
            self._in_a = False
            self._current_href = None
            self._current_text = []
        elif tag == "td" and self._in_td_02:
            self._in_td_02 = False
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
        elif tag == "tbody" and self._in_tbody:
            self._in_tbody = False
        elif tag == "table" and self._in_table:
            self._in_table = False


def _collect_weibo(limit: int) -> list:
    """微博实时热搜 - HTML 表格解析。"""
    url = "https://s.weibo.com/top/summary?cate=realtimehot"
    req = Request(url, headers={
        "User-Agent": UA,
        "Cookie": "SUB=_2AkMWIuNSf8NxqwJRmP8dy2rhaoV2ygrEieKgfhKJJRMxHRl-yT9jqk86tRB6PaLNvQZR6zYUcYVT1zSjoSreQHidcUq7",
        "Referer": url,
    }, method="GET")
    with urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    parser = WeiboTableParser()
    parser.feed(raw)
    return parser.items[:limit]


def _collect_hupu(limit: int) -> list:
    """虎扑热搜 - 正则表达式匹配。"""
    url = "https://bbs.hupu.com/topic-daily-hot"
    raw = _fetch(url)
    # 匹配 <li class="bbs-sl-web-post-body"> 中的 <a href="..." class="p-title">title</a>
    regex = re.compile(
        r'<li class="bbs-sl-web-post-body">[\s\S]*?'
        r'<a href="(/[^"]+?\.html)"[^>]*?class="p-title"[^>]*>([^<]+)</a>'
    )
    items = []
    for match in regex.finditer(raw):
        path, title = match.groups()
        if path and title:
            items.append({
                "title": title.strip(),
                "url": f"https://bbs.hupu.com{path}"
            })
            if len(items) >= limit:
                break
    return items


def _collect_aihot(limit: int) -> list:
    """AI Hot - JSON API 抓取。"""
    url = "https://aihot.virxact.com/api/public/items?mode=all&take=30"
    req = Request(url, headers={
        "User-Agent": f"{UA} aihot-skill/0.2.0 newsnow/0.0.40"
    }, method="GET")
    with urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = []
    for item in data.get("items", [])[:limit]:
        title = item.get("title", "")
        link = item.get("url", "")
        if title and link:
            items.append({"title": title, "url": link})
    return items


def _collect_google_news(limit: int) -> list:
    """Google News 中文 - RSS 抓取。"""
    url = "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    try:
        raw = _fetch(url)
        # Google News RSS 含有 XML 声明，ET.fromstring 可以处理字符串
        root = ET.fromstring(raw)
        items = []
        for item in root.findall(".//item")[:limit]:
            title = item.find("title").text
            link = item.find("link").text
            if title and link:
                items.append({"title": title, "url": link})
        return items
    except Exception:
        return []


# ---------------------------------------------------------------- 通用抓取器：RSS / Atom 订阅 与 政府列表页
# 「政策发布 · 官方信息源」「全球政经媒体」两个板块的源都走这两个通用抓取器，
# 新增一个源只需在 SOURCE_META 里加一行 feed= 或 page= + pattern=，无需再写函数。

def _local_tag(node) -> str:
    """去掉命名空间的标签名（Atom 带 {http://www.w3.org/2005/Atom} 前缀）。"""
    return node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""


def _parse_feed(raw, limit: int, strip_suffix: str = "") -> list:
    """解析 RSS 2.0 / RSS 1.0 / Atom 文本（str 或 bytes），返回 [{title, url}]。纯函数，便于测试。

    - RSS：<item><title>…</title><link>…</link></item>
    - Atom：<entry><title>…</title><link href="…" rel="alternate"/></entry>
    - ``strip_suffix``：去掉标题固定后缀（如 Google News 检索结果的「 - Reuters」）。
    """
    root = ET.fromstring(raw)
    items, seen = [], set()
    for node in root.iter():
        if _local_tag(node) not in ("item", "entry"):
            continue
        title, link = "", ""
        for child in node:
            tag = _local_tag(child)
            if tag == "title":
                title = _collapse(html.unescape("".join(child.itertext())))
            elif tag == "link":
                href = (child.text or "").strip() or (child.get("href") or "").strip()
                rel = (child.get("rel") or "alternate").lower()
                if href and (not link or rel == "alternate"):
                    link = href
        if strip_suffix and title.endswith(strip_suffix):
            title = title[: -len(strip_suffix)].rstrip()
        if not title or title in seen:
            continue
        seen.add(title)
        items.append({"title": title, "url": link})
        if len(items) >= limit:
            break
    return items


def _collect_feed(url: str, limit: int, headers: dict | None = None, strip_suffix: str = "") -> list:
    """抓取 RSS / Atom 订阅（政府 / 央行 / 监管机构 / 媒体官方 feed）。"""
    raw, _charset = _fetch_bytes(url, headers)
    # 交给 XML 解析器按声明的 encoding 解码（bytes 入参），避免二次转码破坏非 UTF-8 订阅。
    return _parse_feed(raw, limit, strip_suffix)


def _filter_page_links(links, pattern: str, base_url: str, limit: int, min_len: int = 6) -> list:
    """从列表页 (href, text) 中筛出条目：链接正则匹配 + 相对链接补全 + 去重 + 过滤导航短词。纯函数。"""
    rx = re.compile(pattern)
    seen, items = set(), []
    for href, text in links:
        if not href:
            continue
        full = urljoin(base_url, href.strip())
        if not rx.search(full) or full in seen:
            continue
        title = _clean_title(html.unescape(text))
        # 「更多」「视频」「致辞全文」这类挂在同一条目上的短锚文本不是标题，跳过。
        if len(title) < min_len:
            continue
        seen.add(full)
        items.append({"title": title, "url": full})
        if len(items) >= limit:
            break
    return items


def _collect_page(url: str, pattern: str, limit: int, headers: dict | None = None) -> list:
    """抓取服务端渲染的政策列表页（国务院 / 部委「政策发布」栏目），按链接正则取条目。"""
    raw = _fetch(url, headers)
    parser = LinkCollector()
    parser.feed(raw)
    return _filter_page_links(parser.links, pattern, url, limit)


# ---------------------------------------------------------------- 公民科技 · 政治透明度 抓取器
# g0v 生态的立法院开放 API 与 EquityStack 公开接口都是 JSON，这里只取公开条目做标题化。

_LY_BILLS_API = "https://ly.govapi.tw/v2/bills"
_EQUITYSTACK_API = "https://equitystack.org/api"


def _format_ly_bills(data: dict, limit: int) -> list:
    """立法院 API v2 /bills 响应 → 条目：〔議案狀態〕議案名稱（提案單位/提案委員）。纯函数。"""
    items = []
    for bill in (data or {}).get("bills", []) or []:
        name = _collapse(str(bill.get("議案名稱") or ""))
        if not name:
            continue
        if len(name) > 90:
            name = name[:89] + "…"
        status = _collapse(str(bill.get("議案狀態") or ""))
        proposer = _collapse(str(bill.get("提案單位/提案委員") or ""))
        title = f"〔{status}〕{name}" if status else name
        if proposer:
            title += f"（{proposer}）"
        items.append({"title": title, "url": str(bill.get("url") or "")})
        if len(items) >= limit:
            break
    return items


def _collect_ly_bills(limit: int) -> list:
    """g0v 生态「立法院 API」：最新進度的議案（法律案 / 預算案等）。"""
    url = f"{_LY_BILLS_API}?limit={min(max(int(limit), 1), 50)}"
    return _format_ly_bills(_get_json(url), limit)


def _format_equitystack_promises(data, limit: int) -> list:
    """EquityStack /api/promises → 条目：[状态] 承诺标题（议题 · 总统）。按最近动作日期倒序。纯函数。"""
    rows = data.get("items") if isinstance(data, dict) else data
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("title")]
    rows.sort(key=lambda r: str(r.get("latest_action_date") or r.get("promise_date") or ""), reverse=True)
    items = []
    for row in rows:
        title = _collapse(str(row["title"]))
        status = _collapse(str(row.get("status") or ""))
        extras = [x for x in (_collapse(str(row.get("topic") or "")), _collapse(str(row.get("president") or ""))) if x]
        text = f"[{status}] {title}" if status else title
        if extras:
            text += f"（{' · '.join(extras)}）"
        slug = str(row.get("slug") or "")
        items.append({"title": text, "url": f"https://equitystack.org/promises/{slug}" if slug else ""})
        if len(items) >= limit:
            break
    return items


def _collect_equitystack_promises(limit: int) -> list:
    """EquityStack：政治人物承诺 → 行动 → 结果 的追踪记录（公开接口）。"""
    return _format_equitystack_promises(_get_json(f"{_EQUITYSTACK_API}/promises"), limit)


def _format_equitystack_bills(data, limit: int) -> list:
    """EquityStack /api/future-bills → 条目：法案号｜法案名（状态 · 最新动作）。纯函数。"""
    rows = data.get("items") if isinstance(data, dict) else data
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tracked = [b for b in (row.get("tracked_bills") or []) if isinstance(b, dict)]
        bill = tracked[0] if tracked else {}
        title = _collapse(str(bill.get("title") or row.get("title") or ""))
        if not title:
            continue
        number = _collapse(str(bill.get("bill_number") or ""))
        status = _collapse(str(bill.get("status") or row.get("status") or ""))
        action = _collapse(str(bill.get("latest_action") or ""))
        text = f"{number}｜{title}" if number else title
        tail = " · ".join(x for x in (status, action) if x)
        if tail:
            text += f"（{tail}）"
        items.append({"title": text, "url": str(bill.get("url") or "")})
        if len(items) >= limit:
            break
    return items


def _collect_equitystack_bills(limit: int) -> list:
    """EquityStack：与政策承诺挂钩的国会法案追踪（Congress.gov 数据）。"""
    return _format_equitystack_bills(_get_json(f"{_EQUITYSTACK_API}/future-bills"), limit)


# 收集器函数映射表
_COLLECTORS = {
    "zhihu": _collect_zhihu,
    "douyin": _collect_douyin,
    "weibo": _collect_weibo,
    "hupu": _collect_hupu,
    "aihot": _collect_aihot,
    "google_news": _collect_google_news,
    "ly_bills": _collect_ly_bills,
    "equitystack_promises": _collect_equitystack_promises,
    "equitystack_bills": _collect_equitystack_bills,
}

# 抓取过程中允许吞掉的异常：网络 / 超时 / 解析失败都回退演示数据；其他异常照常抛出以暴露 bug。
_FETCH_ERRORS = (HTTPError, URLError, TimeoutError, OSError, HTTPException,
                 ValueError, KeyError, TypeError, ET.ParseError, json.JSONDecodeError)

# 并发抓取线程数（源之间互不依赖；BRIEF_FETCH_WORKERS=1 可退回串行）。
FETCH_WORKERS_DEFAULT = 8


def fetch_workers() -> int:
    raw = (os.environ.get("BRIEF_FETCH_WORKERS") or "").strip()
    if raw.isdigit() and int(raw) >= 1:
        return int(raw)
    return FETCH_WORKERS_DEFAULT


def _collect_live(meta: dict, limit: int) -> list:
    """按元信息实际抓取（不含兜底）：collector → feed → page → channel。"""
    if "collector" in meta:
        func = _COLLECTORS.get(meta["collector"])
        return func(limit) if func else []
    if "feed" in meta:
        return _collect_feed(meta["feed"], limit, meta.get("headers"), meta.get("strip_suffix", ""))
    if "page" in meta:
        return _collect_page(meta["page"], meta["pattern"], limit, meta.get("headers"))
    if "channel" in meta:
        return _parse_channel(meta["channel"], meta["origin"], limit)
    return []


def collect_one(name: str, limit: int | None = None) -> list:
    """抓取单个源：自定义收集器 / RSS·Atom 订阅 / 政府列表页 / 聚合通道 → 演示数据兜底。

    ``limit`` 缺省时按推送口径自动取（会员 10 万字 → 20 条，普通 2 万字 → 5 条），
    可用 ``BRIEF_FETCH_LIMIT`` 显式覆盖。
    """
    limit = default_fetch_limit() if limit is None else limit
    meta = source_meta(name)
    if not meta:
        return []
    try:
        items = _collect_live(meta, limit)
        if items:
            return items
    except _FETCH_ERRORS:
        pass
    # 演示数据兜底
    return _demo_items(name)


def _limit_for(name: str, limit: int) -> int:
    # 热搜类榜单：多抓一些，方便「重点关注」与主题统计取样（不小于全局口径）。
    return max(limit, 10) if name in ("抖音热搜", "微博实时热搜") else limit


def _collect_many(names: list, limit: int) -> dict:
    """并发抓取若干源，返回顺序与 ``names`` 一致；单源意外异常也不拖垮整批（回退演示数据）。"""
    if not names:
        return {}
    workers = min(fetch_workers(), len(names))
    if workers <= 1:
        return {name: collect_one(name, _limit_for(name, limit)) for name in names}
    result = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as pool:
        futures = [(name, pool.submit(collect_one, name, _limit_for(name, limit))) for name in names]
        for name, future in futures:
            try:
                result[name] = future.result()
            except Exception:  # noqa: BLE001 —— 单源崩溃只影响自己
                result[name] = _demo_items(name)
    return result


def collect_all(limit: int | None = None) -> dict:
    """并发抓取全部数据源（六大板块）。返回 {name: [item, ...]}，顺序同 SOURCE_META。

    ``limit`` 缺省时按推送口径自动取（见 :func:`default_fetch_limit`）。
    """
    limit = default_fetch_limit() if limit is None else limit
    return _collect_many(list(SOURCES), limit)


def collect_section(key: str, limit: int | None = None) -> dict:
    """按板块抓取：只抓 ``key`` 板块下的源（如 policy / world / civic）。未知板块返回 {}。"""
    limit = default_fetch_limit() if limit is None else limit
    return _collect_many(sources_in_section(key), limit)


# ---------------------------------------------------------------- 开篇 AI 总结引擎
# 换新方式：不再硬编码「AI 每日总结」开篇总结，改为对真实抓取到的标题做
# 「主题热度 + 多空情绪」统计，每次推送都随数据动态更新，零外部依赖、可离线运行。
# 如需接入在线大模型，只需覆盖 analyze_brief() 的返回值（字段保持一致即可）。

# (板块标签, 关键词)。一条标题命中任一关键词即计入该主题热度，并按数据源去重计权。
# 中文关键词按子串匹配；ASCII 关键词大小写不敏感、按单词边界匹配（末尾 * 允许后缀，如 chip* 命中 chips），
# 这样「AI」不会误命中 SAID / AIR，「gold」不会误命中 Goldman —— 政策 / 媒体板块的英文标题也能正确归类。
_THEMES = [
    ("AI 算力", ["AI", "人工智能", "算力", "大模型", "数据中心", "OpenAI", "Anthropic",
                "DeepMind", "GPT*", "ChatGPT", "Claude", "Gemini", "Llama*", "机器人", "GPU*",
                "data center*", "datacenter*", "robot*", "artificial intelligence"]),
    ("光通信", ["光纤", "光通信", "光缆", "光模块", "古河电工", "optical fiber", "fiber optic*"]),
    ("半导体", ["半导体", "芯片", "晶圆", "台积电", "英伟达", "AMD", "chip*", "semiconductor*",
               "Nvidia", "TSMC", "wafer*"]),
    ("贵金属", ["黄金", "铂金", "钯金", "白银", "原油", "稀土", "矿产", "gold", "silver", "platinum",
               "crude", "oil", "rare earth*", "mineral*"]),
    ("美联储", ["美联储", "央行", "加息", "降息", "货币政策", "贝森特", "日元", "汇率",
               "美元", "逆回购", "流动性", "利率", "Fed", "Federal Reserve", "FOMC", "rate cut*",
               "rate hike*", "interest rate*", "central bank*", "ECB", "BOJ", "Bank of Japan",
               "Bank of England", "yen", "dollar", "monetary polic*", "inflation"]),
    ("地缘", ["伊朗", "阿曼", "霍尔木兹", "海峡", "中东", "战争", "制裁", "美伊",
             "特朗普", "干预", "Iran", "Middle East", "war", "sanction*", "Trump", "NATO",
             "Ukraine", "Russia*", "Israel*", "Gaza", "Taiwan", "geopolitic*", "military"]),
    ("医药", ["诺和诺德", "医药", "医疗", "GLP*", "疫苗", "制药", "临床", "辉瑞", "pharma*", "drug*",
             "vaccine*", "FDA", "Novo Nordisk", "Pfizer", "biotech*"]),
    ("智驾", ["自动驾驶", "智驾", "智能驾驶", "新能源车", "特斯拉", "L3", "L4", "Tesla",
             "autonomous driving", "self-driving", "electric vehicle*", "EV", "EVs"]),
    ("地产", ["楼市", "地产", "房价", "房企", "豪宅", "housing", "property market", "real estate",
             "mortgage*", "home price*"]),
    ("消费", ["消费", "零售", "电商", "餐饮", "麦当劳", "宝洁", "亚马逊", "consumer*", "retail*",
             "e-commerce", "Amazon", "Walmart", "McDonald's"]),
    ("航天", ["SpaceX", "火箭", "卫星", "发射", "rocket*", "satellite*", "spacecraft"]),
]

_BULLISH = ["上涨", "暴涨", "大涨", "涨超", "涨逾", "涨幅扩大", "创新高", "新高", "首破",
            "突破", "上调", "增持", "利好", "扩产", "反弹", "回升", "盈利", "超预期",
            "开门红", "修复", "反攻", "暴增", "回暖", "翻倍", "前景改善", "净利", "企稳"]
_BEARISH = ["下跌", "暴跌", "重挫", "新低", "危机", "风险", "警示", "警惕", "债务", "逾期",
            "诉讼", "立案", "下调", "减持", "冲击", "利空", "泡沫", "争议", "放缓", "衰退",
            "疲软", "贬值", "缩水", "承压", "亏损", "负增长", "暴雷", "抛售"]
# 英文多空词（单词边界匹配，末尾 * 允许后缀）：让英文政策 / 媒体标题也参与多空统计。
_BULLISH_EN = ["surge*", "soar*", "rall*", "jump*", "record high", "all-time high", "gain*", "rebound*",
               "upgrade*", "boost*", "beats estimates", "beat estimates", "stimulus", "rate cut*", "recover*", "growth",
               "expand*", "approve*", "approval", "deal", "agree*", "relief", "climb*", "rise*", "rose"]
_BEARISH_EN = ["plunge*", "slump*", "tumble*", "drop*", "fall*", "fell", "sink*", "sank", "crash*",
               "selloff", "sell-off", "crisis", "risk*", "warn*", "sanction*", "ban", "bans", "banned",
               "probe*", "lawsuit*", "fraud", "default*", "recession", "layoff*", "tariff*", "curb*",
               "crackdown", "shutdown", "war", "attack*", "kill*", "threat*", "cuts forecast",
               "downgrade*", "slash*", "loss", "losses", "decline*", "weak*", "fear*", "turmoil"]


def _kw_hit(title: str, title_lower: str, keyword: str) -> bool:
    """主题 / 多空关键词命中：中文按子串（原文），ASCII 按单词边界（小写，末尾 * 允许后缀）。"""
    if keyword.isascii():
        return _policy_hit(title_lower, keyword)
    return keyword in title


def _direction(title: str, title_lower: str | None = None) -> int:
    """单条标题多空方向：多方词命中数 vs 空方词命中数 → 1 / -1 / 0（中英文词库合并计票）。"""
    title_lower = title.lower() if title_lower is None else title_lower
    up = sum(1 for word in _BULLISH if word in title)
    up += sum(1 for word in _BULLISH_EN if _policy_hit(title_lower, word))
    down = sum(1 for word in _BEARISH if word in title)
    down += sum(1 for word in _BEARISH_EN if _policy_hit(title_lower, word))
    if up > down:
        return 1
    if down > up:
        return -1
    return 0


def analyze_brief(brief: dict, deep_policy: bool = False) -> dict:
    """对采集结果做本地「AI 总结」：主题热度 + 多空博弈概率 + 板块风向 + 资金流向。

    返回：
        headline      「AI 每日总结」正文（纯文本，引用热度最高的一两个板块）
        bias          偏多 / 偏空 / 中性
        bull          多方概率（百分比整数）
        bear          空方概率（百分比整数）
        signals       参与统计的多空信号总条数（样本量，= 多方 + 空方）
        sectors       热度最高的板块标签列表（最多 2 个）
        top_themes    全部命中主题 [(标签, 跨源数, 提及数)]，按信号强度排序
        sectors_up    利好板块（多头信号占优的主题，最多 3 个）
        sectors_down  利差板块（空头信号占优的主题，最多 2 个，可能为空）
        opportunity_sectors 「AI 板块机会」机会方向：净多头主题 Top 4，
                            [{tag, up, down, net, mentions, sources, evidence[…],
                              etf_pair[{code, name, benchmark, mapping}], etf_pair_text}]
        pressure_sectors    「AI 板块机会」承压方向：净空头主题 Top 2，结构同上
        flow          资金流向分析句（纯文本）
        points        开篇四个观点 [{key, label, text}]：
                      1 市场情绪 2 多空博弈概率 3 利好/利差板块 4 资金流向分析
        policy        「AI 政策分析」板块数据（analyze_policy() 的返回值：政策维度热度、
                      多空方向、鹰鸽取向与总结句；``deep_policy=True`` 时含深度层）
    """
    theme_sources = {tag: set() for tag, _ in _THEMES}
    theme_mentions = {tag: 0 for tag in theme_sources}
    # 按主题累计多/空信号：只有方向明确的标题才计入对应主题的多空账。
    theme_up = {tag: 0 for tag in theme_sources}
    theme_down = {tag: 0 for tag in theme_sources}
    # 「AI 板块机会」板块的证据留档：每条命中主题的标题（含方向/来源/链接），供按主题回溯支撑新闻。
    theme_evidence = {tag: [] for tag in theme_sources}
    bull = bear = 0
    # 板块口径：每个板块贡献了多少条标题 / 多少个有内容的源（供页脚与 /api 展示，不参与方向判断）。
    section_items = {key: 0 for key in SECTION_KEYS}
    section_sources = {key: set() for key in SECTION_KEYS}

    for name, items in (brief or {}).items():
        sec = section_of(name)
        for item in items or []:
            title = item.get("title", "") or ""
            if not title:
                continue
            section_items[sec] = section_items.get(sec, 0) + 1
            section_sources.setdefault(sec, set()).add(name)
            title_lower = title.lower()
            # 多空：单条标题按「多方词命中数 vs 空方词命中数」判定方向（中英文词库），避免单条重复计数。
            direction = _direction(title, title_lower)
            if direction > 0:
                bull += 1
            elif direction < 0:
                bear += 1
            # 主题：命中即累计，并记录出现在哪些数据源（跨源命中 = 更强信号）。
            for tag, keywords in _THEMES:
                if any(_kw_hit(title, title_lower, keyword) for keyword in keywords):
                    theme_mentions[tag] += 1
                    theme_sources[tag].add(name)
                    if direction > 0:
                        theme_up[tag] += 1
                    elif direction < 0:
                        theme_down[tag] += 1
                    theme_evidence[tag].append({
                        "title": title,
                        "source": name,
                        "url": item.get("url") or "",
                        "direction": direction,
                    })

    ranked = sorted(
        ((tag, len(theme_sources[tag]), theme_mentions[tag]) for tag in theme_sources),
        key=lambda entry: (entry[1], entry[2]),
        reverse=True,
    )
    top_themes = [(tag, src, men) for tag, src, men in ranked if src > 0]
    sectors = [tag for tag, _, _ in top_themes[:2]]

    if bull + bear > 0:
        bull_pct = round(bull / (bull + bear) * 100)
        bear_pct = 100 - bull_pct
        if bull_pct >= 60:
            bias = "偏多"
        elif bull_pct <= 40:
            bias = "偏空"
        else:
            bias = "中性"
    else:
        # 无任何多空信号（例如空简报）：诚实标注中性，不做无依据的方向判断。
        bull_pct, bear_pct, bias = 50, 50, "中性"

    # 利好 / 利差板块：净多头（净空头）信号 > 0 的主题，按信号强度与热度排序。
    # 「AI 板块机会」板块复用同一套排序（机会方向取 Top 4，承压方向取 Top 2）。
    up_ranked = sorted(
        (tag for tag in theme_sources if theme_up[tag] > theme_down[tag]),
        key=lambda tag: (theme_up[tag] - theme_down[tag],
                         len(theme_sources[tag]), theme_mentions[tag]),
        reverse=True,
    )
    down_ranked = sorted(
        (tag for tag in theme_sources if theme_down[tag] > theme_up[tag]),
        key=lambda tag: (theme_down[tag] - theme_up[tag],
                         len(theme_sources[tag]), theme_mentions[tag]),
        reverse=True,
    )
    sectors_up = up_ranked[:3]
    sectors_down = down_ranked[:2]

    def _sector_entry(tag: str, want_dir: int, evidence_limit: int) -> dict:
        """板块机会明细：信号统计 + 支撑证据（按「方向相符优先」排序，同源同题去重）。"""
        evidence, seen = [], set()
        for ev in sorted(
            theme_evidence[tag],
            key=lambda e: 0 if e["direction"] == want_dir else (1 if e["direction"] == 0 else 2),
        ):  # 稳定排序：同方向内保持抓取顺序
            key = (ev["source"], ev["title"])
            if key in seen:
                continue
            seen.add(key)
            evidence.append({"title": ev["title"], "source": ev["source"], "url": ev["url"]})
            if len(evidence) >= evidence_limit:
                break
        etf_pair = hk_etf_pair("", tag)
        return {
            "tag": tag,
            "up": theme_up[tag],
            "down": theme_down[tag],
            "net": theme_up[tag] - theme_down[tag],
            "mentions": theme_mentions[tag],
            "sources": len(theme_sources[tag]),
            "evidence": evidence,
            # 每个 AI 板块结论统一附两个港交所现有 ETF 及其对应指数；不是个股荐股。
            "etf_pair": etf_pair,
            "etf_pair_text": _hk_etf_pair_text(etf_pair),
        }

    opportunity_sectors = [_sector_entry(tag, 1, 3) for tag in up_ranked[:4]]
    pressure_sectors = [_sector_entry(tag, -1, 2) for tag in down_ranked[:2]]

    headline = _compose_headline(bias, top_themes)
    flow = _compose_flow(sectors_up, sectors_down, top_themes)
    points = [
        {"key": "sentiment", "label": "市场情绪",
         "text": _compose_sentiment(bias, bull_pct, bear_pct)},
        {"key": "battle", "label": "多空博弈概率",
         "text": _compose_battle(bias, bull_pct, bear_pct, bull + bear)},
        {"key": "sectors", "label": "利好 / 利差板块",
         "text": _compose_sectors(sectors_up, sectors_down)},
        {"key": "flow", "label": "资金流向分析", "text": flow},
    ]
    # 情绪热力图：关键词级线索（零外部依赖、可离线）
    heatmap = analyze_keyword_heatmap(brief)
    return {
        "headline": headline,
        "bias": bias,
        "bull": bull_pct,
        "bear": bear_pct,
        "signals": bull + bear,
        "sectors": sectors,
        "top_themes": top_themes,
        "sectors_up": sectors_up,
        "sectors_down": sectors_down,
        "opportunity_sectors": opportunity_sectors,
        "pressure_sectors": pressure_sectors,
        "flow": flow,
        "points": points,
        "heatmap": heatmap,
        # 深度层（知识库检索 / 推理链 / 模型）默认不在这里跑：analyze_brief 被 /api 与测试高频调用，
        # 推送与页面渲染走 analyze_policy(deep=True) 显式开启，避免重复计算。
        "policy": analyze_policy(brief, deep=deep_policy),
        # 板块口径：[{key, label, items, sources}]，按 SECTIONS 顺序，只列有内容的板块。
        "sections": [
            {"key": key, "label": SECTION_LABELS.get(key, key),
             "items": section_items.get(key, 0), "sources": len(section_sources.get(key, ()))}
            for key in SECTION_KEYS if section_items.get(key, 0) > 0
        ],
    }


# ---------------------------------------------------------------- AI 情绪热力图 · 关键词级线索（面向股市投资）
# 设计思路
# --------
# - 粒度：主题（AI 算力/光通信/半导体…）是板块级归因，热力图下沉到「关键词」级——
#   每条新闻标题命中的具体关键词（如“AI”“光纤”“黄金”“降息”）单独计热度与情绪，
#   让投资者看到“资金到底在炒什么词”，避免板块口径过粗掩盖结构。
# - 归因：关键词来自 _THEMES（投资主线词库），中文子串 / 英文单词边界匹配（* 允许后缀），
#   与 “AI 每日总结 / 板块机会” 同一套多空词库判定情绪，零外部依赖、可离线。
# - 指标：为股市投资设计的 9 维量化（全部可复算、阈值写死）：
#     ① 热度 Heat 0-100：提及数 × 跨源对数加权后归一，反映关注度（关注度越高越可能带来交易机会，也意味拥挤）。
#     ② 情绪分 Sentiment -1…+1：(bull-bear)/signals，方向与强度；绝对值越大一致性越高。
#     ③ 净信号 Net：bull - bear，绝对强度。
#     ④ 跨源数 Sources & 共振 Resonance：命中多少个独立数据源，跨源越多越可信，单源需谨慎。
#     ⑤ 分歧度 Divergence = 1 - |情绪分|，多空撕裂程度；高分歧易波动、低分歧趋势稳。
#     ⑥ 爆发系数 Burst = Heat × |情绪分|，高热+强方向 = 主线爆发点，适合短线跟踪。
#     ⑦ 资金倾向 Flow Tilt：偏多+高热=流入，偏空+高热=流出，其余观望。
#     ⑧ 投资评级 Rating：综合热度与情绪的行动标签（强多·机会/偏多·关注/中性·观望/偏空·谨慎/强空·回避），附操作提示。
#     ⑨ 风险等级 Risk：基于负面情绪与分歧度评估回撤风险（低/中/高）。
# - 证据：每个关键词保留最多 3 条支撑新闻（按“方向一致优先”排序，同源同题去重），标题即线索，避免黑箱。
# - 关联：每个关键词→恰好两个港交所现有 ETF + 各自对应指数 / 参考标的，以及命中它的政策维度（_policy_tags）。
#   港股暂无纯主题 ETF 时明确标注「代理」，不再输出 A 股个股组合。
# - 诚实：空简报或零命中时如实标注，不编造热力；热度为 0 时情绪与评级均为中性。
#
# 对投资的用法（仅统计信号，不构成投资建议）
#   · 看“沸腾 + 强多”格子找主线，看“沸腾 + 强空”格子防风险；
#   · 共振低（1-2 源）的是噪音，共振高（≥4 源）的是确认；
#   · 分歧高时适合等待方向确认，爆发系数高时注意追高风险。

# 港股 ETF 双标的目录（只使用港交所现有 ETF 的 HKD 柜台）。
# ``benchmark`` 是产品对应的指数 / 参考标的，不把主题代理写成个股推荐。
# 目录在 2026-10-03 按 HKEX / 基金管理人公开资料核对；代码保留 5 位港股格式，
# 便于读者与普通股票代码区分。ETF 仍有跟踪误差、流动性、汇率及衍生品等风险。
_HK_ETF_CATALOG = {
    "03140.HK": {"name": "华夏港美人工智能 ETF", "benchmark": "Solactive G2 AI 50 Select Index NTR"},
    "02807.HK": {"name": "Global X 中国机器人及人工智能 ETF", "benchmark": "FactSet China Robotics and Artificial Intelligence Index"},
    "02826.HK": {"name": "Global X 中国云计算 ETF", "benchmark": "Solactive China Cloud Computing Index NTR"},
    "03033.HK": {"name": "南方东英恒生科技 ETF", "benchmark": "Hang Seng TECH Index"},
    "03191.HK": {"name": "Global X 中国半导体 ETF", "benchmark": "FactSet China Semiconductor Index NTR"},
    "03076.HK": {"name": "富邦台湾核心半导体 ETF", "benchmark": "ICE FactSet Taiwan Core Semiconductor Index"},
    "02840.HK": {"name": "SPDR 金 ETF", "benchmark": "LBMA Gold Price PM"},
    "03081.HK": {"name": "价值黄金 ETF", "benchmark": "London Gold Fixing Price"},
    "03097.HK": {"name": "Global X 原油期货增强 ETF", "benchmark": "S&P GSCI Crude Oil Enhanced Index Excess Return"},
    "03175.HK": {"name": "三星原油期货 ETF", "benchmark": "S&P GSCI Crude Oil Multiple Contract 55/30/15 ER Index"},
    "03014.HK": {"name": "Global X 铜矿 ETF", "benchmark": "Solactive Copper Miners Index"},
    "02824.HK": {"name": "易方达全球金矿精选 ETF", "benchmark": "Solactive Global Gold Miner Select Index"},
    "02845.HK": {"name": "Global X 中国电动车及电池 ETF", "benchmark": "Solactive China Electric Vehicle and Battery Index NTR"},
    "02809.HK": {"name": "Global X 中国洁净能源 ETF", "benchmark": "Solactive China Clean Energy Index NTR"},
    "03436.HK": {"name": "恒生美国国债 1-3 年 ETF", "benchmark": "Hang Seng CMS Bloomberg US Treasury 1-3 Year Index"},
    "03156.HK": {"name": "博时美国国债 20+ 年 ETF", "benchmark": "ICE US Treasury 20+ Year Bond Index"},
    "02820.HK": {"name": "Global X 中国生物科技 ETF", "benchmark": "Solactive China Biotech Index NTR"},
    "02841.HK": {"name": "Global X 中国医疗科技 ETF", "benchmark": "Solactive China MedTech Index"},
    "03447.HK": {"name": "南方东英亚太精选 REIT ETF", "benchmark": "FTSE EPRA Nareit Asia Pacific Select REITs Capped Net Tax Index"},
    "03001.HK": {"name": "Premia 中国地产美元债 ETF", "benchmark": "ICE 1-7 Year USD China Senior Real Estate Corporate Constrained Index"},
    "02806.HK": {"name": "Global X 中国消费品牌 ETF", "benchmark": "Solactive China Consumer Brand Index"},
    "03040.HK": {"name": "Global X MSCI 中国 ETF", "benchmark": "MSCI China Index"},
    "02800.HK": {"name": "盈富基金", "benchmark": "Hang Seng Index"},
    "03195.HK": {"name": "恒生标普 500 ETF", "benchmark": "S&P 500 Net Total Return Index"},
    "02834.HK": {"name": "iShares 纳斯达克 100 ETF", "benchmark": "NASDAQ-100 Index"},
}

# 每一个 AI 关键词都先按所属主题得到一组恰好两个港股 ETF；无纯主题产品时明确写「代理」，
# 而不是伪造对应产品。关键词级覆盖用于原油、云计算、矿产等可进一步精确归因的内容。
_HK_ETF_PAIRS_BY_THEME = {
    "AI 算力": {"codes": ("03140.HK", "02807.HK"), "note": "AI 软件/硬件 + 机器人产业链"},
    "光通信": {"codes": ("03191.HK", "03033.HK"), "note": "半导体 + 恒生科技代理（港股暂无纯光通信 ETF）"},
    "半导体": {"codes": ("03191.HK", "03076.HK"), "note": "中国 + 台湾半导体"},
    "贵金属": {"codes": ("02840.HK", "03081.HK"), "note": "两只实物黄金 ETF"},
    "美联储": {"codes": ("03436.HK", "03156.HK"), "note": "短久期 + 长久期美国国债"},
    "地缘": {"codes": ("02840.HK", "03156.HK"), "note": "黄金 + 长久期美债避险观察"},
    "医药": {"codes": ("02820.HK", "02841.HK"), "note": "生物科技 + 医疗科技"},
    "智驾": {"codes": ("02845.HK", "02807.HK"), "note": "电动车电池 + 机器人/AI"},
    "地产": {"codes": ("03447.HK", "03001.HK"), "note": "亚太 REIT + 中国地产美元债"},
    "消费": {"codes": ("02806.HK", "03040.HK"), "note": "中国消费品牌 + MSCI 中国"},
    "航天": {"codes": ("03140.HK", "02834.HK"), "note": "AI + 纳指科技代理（港股暂无纯商业航天 ETF）"},
}

_HK_ETF_PAIR_OVERRIDES = {
    # 云 / 大模型更贴近云计算与中美 AI 指数。
    "大模型": {"codes": ("02826.HK", "03140.HK"), "note": "中国云计算 + 港美 AI"},
    "数据中心": {"codes": ("02826.HK", "03140.HK"), "note": "中国云计算 + 港美 AI"},
    "OpenAI": {"codes": ("03140.HK", "02826.HK"), "note": "港美 AI + 中国云计算代理"},
    "Anthropic": {"codes": ("03140.HK", "02826.HK"), "note": "港美 AI + 中国云计算代理"},
    "DeepMind": {"codes": ("03140.HK", "02834.HK"), "note": "港美 AI + 纳斯达克 100 代理"},
    "ChatGPT": {"codes": ("03140.HK", "02826.HK"), "note": "港美 AI + 中国云计算代理"},
    "Claude": {"codes": ("03140.HK", "02826.HK"), "note": "港美 AI + 中国云计算代理"},
    "Gemini": {"codes": ("03140.HK", "02834.HK"), "note": "港美 AI + 纳斯达克 100 代理"},
    "Llama": {"codes": ("03140.HK", "02834.HK"), "note": "港美 AI + 纳斯达克 100 代理"},
    "GPU": {"codes": ("03140.HK", "03191.HK"), "note": "港美 AI 硬件 + 中国半导体"},
    "data center": {"codes": ("02826.HK", "03140.HK"), "note": "中国云计算 + 港美 AI"},
    "datacenter": {"codes": ("02826.HK", "03140.HK"), "note": "中国云计算 + 港美 AI"},
    # 商品：原油有直接期货 ETF；银/铂/钯与稀土没有现行纯主题产品，明确采用代理。
    "原油": {"codes": ("03097.HK", "03175.HK"), "note": "两只 WTI 原油期货 ETF"},
    "crude": {"codes": ("03097.HK", "03175.HK"), "note": "两只 WTI 原油期货 ETF"},
    "oil": {"codes": ("03097.HK", "03175.HK"), "note": "两只 WTI 原油期货 ETF"},
    "白银": {"codes": ("02840.HK", "02824.HK"), "note": "黄金 + 金矿股代理（港股暂无现行纯白银 ETF）"},
    "铂金": {"codes": ("02840.HK", "02824.HK"), "note": "黄金 + 金矿股代理（港股暂无现行纯铂金 ETF）"},
    "钯金": {"codes": ("02840.HK", "02824.HK"), "note": "黄金 + 金矿股代理（港股暂无现行纯钯金 ETF）"},
    "silver": {"codes": ("02840.HK", "02824.HK"), "note": "黄金 + 金矿股代理（港股暂无现行纯白银 ETF）"},
    "platinum": {"codes": ("02840.HK", "02824.HK"), "note": "黄金 + 金矿股代理（港股暂无现行纯铂金 ETF）"},
    "稀土": {"codes": ("03014.HK", "02845.HK"), "note": "铜矿 + 电池材料代理（港股暂无纯稀土 ETF）"},
    "矿产": {"codes": ("03014.HK", "02824.HK"), "note": "铜矿 + 金矿股"},
    "rare earth": {"codes": ("03014.HK", "02845.HK"), "note": "铜矿 + 电池材料代理（港股暂无纯稀土 ETF）"},
    "mineral": {"codes": ("03014.HK", "02824.HK"), "note": "铜矿 + 金矿股"},
    # 海外单一公司新闻使用有相关成份敞口的港股 ETF，不映射为个股。
    "英伟达": {"codes": ("03140.HK", "03191.HK"), "note": "港美 AI + 中国半导体"},
    "Nvidia": {"codes": ("03140.HK", "03191.HK"), "note": "港美 AI + 中国半导体"},
    "AMD": {"codes": ("03140.HK", "02834.HK"), "note": "港美 AI + 纳斯达克 100"},
    "台积电": {"codes": ("03076.HK", "03191.HK"), "note": "台湾 + 中国半导体"},
    "TSMC": {"codes": ("03076.HK", "03191.HK"), "note": "台湾 + 中国半导体"},
    "特斯拉": {"codes": ("02845.HK", "03140.HK"), "note": "中国电动车电池 + 港美 AI 代理"},
    "Tesla": {"codes": ("02845.HK", "03140.HK"), "note": "中国电动车电池 + 港美 AI 代理"},
    "亚马逊": {"codes": ("02834.HK", "02806.HK"), "note": "纳斯达克 100 + 中国消费品牌代理"},
    "Amazon": {"codes": ("02834.HK", "02806.HK"), "note": "纳斯达克 100 + 中国消费品牌代理"},
    "SpaceX": {"codes": ("03140.HK", "02834.HK"), "note": "AI + 纳指科技代理（港股暂无纯商业航天 ETF）"},
}


def hk_etf_pair(keyword: str, tag: str) -> list:
    """返回某 AI 关键词对应的两个现有港股 ETF（含产品名、指数 / 参考标的与映射说明）。"""
    spec = _HK_ETF_PAIR_OVERRIDES.get(keyword) or _HK_ETF_PAIRS_BY_THEME.get(tag)
    if not spec:
        # 新增主题若忘记配置时，仍回退为港股宽基双标的，而不是回到个股组合。
        spec = {"codes": ("02800.HK", "03040.HK"), "note": "恒生指数 + MSCI 中国宽基代理"}
    pair = []
    for code in spec["codes"]:
        product = dict(_HK_ETF_CATALOG[code])
        product.update({"code": code, "mapping": spec["note"]})
        pair.append(product)
    return pair


def _hk_etf_pair_text(pair: list, benchmarks: bool = True) -> str:
    """ETF 双标的的统一展示文案。"""
    chunks = []
    for item in pair:
        text = f"{item['code']} {item['name']}"
        if benchmarks:
            text += f"（对应标的：{item['benchmark']}）"
        chunks.append(text)
    return " ＋ ".join(chunks)


_HEATMAP_HINTS = {
    "AI 算力": {"bull": "算力主线放量，观察 AI 软硬件 ETF 共振，忌追高", "bear": "AI 泡沫争议扰动，降低高估值主题暴露", "neutral": "AI 分歧加大，等待催化确认"},
    "光通信": {"bull": "光纤扩产利好，观察半导体与恒科代理组合，防涨后回调", "bear": "光通信承压，等待订单验证", "neutral": "光通信轮动，先看 ETF 代理组合能否共振"},
    "半导体": {"bull": "半导体景气回升，观察中国与台湾半导体 ETF", "bear": "制裁扰动，降低出口受限链暴露", "neutral": "半导体分化，看国产替代进度"},
    "贵金属": {"bull": "贵金属避险走强，观察两只实物黄金 ETF", "bear": "贵金属回落，防追高", "neutral": "贵金属震荡，关注美元与利率"},
    "美联储": {"bull": "宽松预期升温，比较短债与长债 ETF 的久期反应", "bear": "收紧预期压制估值，控制长久期暴露", "neutral": "美联储观望，等待议息确认"},
    "地缘": {"bull": "地缘缓和利好风险偏好", "bear": "地缘扰动升温，观察黄金与长债避险组合", "neutral": "地缘分化，关注避险 ETF 相对强弱"},
    "医药": {"bull": "医药反弹，观察生物科技与医疗科技 ETF", "bear": "医药承压，防集采与临床事件扰动", "neutral": "医药轮动，看临床数据"},
    "智驾": {"bull": "智驾催化密集，观察电动车电池与机器人 ETF", "bear": "智驾退潮，降低高波动主题暴露", "neutral": "智驾分化，等待政策落地"},
    "地产": {"bull": "地产政策托底，比较 REIT 与地产美元债 ETF", "bear": "地产承压，留意信用与利率双重风险", "neutral": "地产观望，看销售与信用数据"},
    "消费": {"bull": "消费修复，观察消费主题与中国宽基 ETF", "bear": "消费疲软，降低可选消费暴露", "neutral": "消费轮动，关注必选消费韧性"},
    "航天": {"bull": "商业航天催化，使用 AI 与纳指 ETF 作为间接代理", "bear": "航天主题退潮，防题材回落", "neutral": "港股暂无纯商业航天 ETF，以代理组合观察"},
}

_HEATMAP_INDICATORS = [
    {"key": "heat", "label": "热度", "formula": "提及×(1+ln(跨源+1))归一0-100", "use": "关注度；>75沸腾需防拥挤回落"},
    {"key": "sentiment", "label": "情绪分", "formula": "(多-空)/信号 -1…+1", "use": "方向与强度；|0.6|以上为强一致"},
    {"key": "net", "label": "净信号", "formula": "多-空", "use": "绝对强度；|≥3|为强信号"},
    {"key": "sources", "label": "跨源/共振", "formula": "命中源数", "use": "可信度；≥4源为确认，1-2源为噪音"},
    {"key": "divergence", "label": "分歧度", "formula": "1-|情绪分|", "use": "撕裂度；高分歧易波动，低分歧趋势稳"},
    {"key": "burst", "label": "爆发系数", "formula": "热度×|情绪分|", "use": "主线爆发点；高分适合短线跟踪但忌追高"},
    {"key": "flow", "label": "资金倾向", "formula": "情绪×热度", "use": "流入/流出/观望；判断跟随或回避"},
    {"key": "rating", "label": "投资评级", "formula": "热度+情绪综合", "use": "强多·机会/偏多·关注/观望/偏空·谨慎/强空·回避"},
    {"key": "risk", "label": "风险等级", "formula": "负面情绪+分歧", "use": "低/中/高；高风险需控仓位"},
]

def _heatmap_display(keyword: str) -> str:
    return keyword.rstrip("*")

def _heatmap_level(heat: int) -> str:
    if heat >= 75:
        return "沸腾"
    if heat >= 50:
        return "热"
    if heat >= 25:
        return "温"
    return "冷"

def _heatmap_sentiment_label(score: float, signals: int) -> str:
    if signals == 0:
        return "中性"
    if score >= 0.3:
        return "偏多"
    if score <= -0.3:
        return "偏空"
    return "中性"

def _heatmap_flow(sentiment: float, heat: int) -> str:
    if sentiment > 0.3 and heat >= 40:
        return "流入"
    if sentiment < -0.3 and heat >= 40:
        return "流出"
    return "观望"

def _heatmap_risk(sentiment: float, divergence: float, heat: int) -> str:
    if sentiment <= -0.5 and heat >= 50:
        return "高"
    if sentiment <= -0.3 and heat >= 40:
        return "中"
    if divergence >= 0.65 and heat >= 50:
        return "中"
    return "低"

def _heatmap_confidence(sources: int, mentions: int) -> str:
    if sources >= 5 and mentions >= 8:
        return "高"
    if sources >= 3 and mentions >= 4:
        return "中"
    if sources >= 2:
        return "中"
    return "低"

def _heatmap_rating(sentiment: float, heat: int, net: int, signals: int) -> str:
    if signals == 0:
        return "中性·观望"
    if sentiment >= 0.6 and net >= 3 and heat >= 60:
        return "强多·机会"
    if sentiment <= -0.6 and net <= -3 and heat >= 60:
        return "强空·回避"
    if sentiment >= 0.3 and heat >= 50:
        return "偏多·关注"
    if sentiment <= -0.3 and heat >= 50:
        return "偏空·谨慎"
    return "中性·观望"

def _heatmap_hint(tag: str, sentiment_label: str) -> str:
    hints = _HEATMAP_HINTS.get(tag) or {}
    if sentiment_label == "偏多":
        return hints.get("bull") or f"{tag}偏多，关注主线，忌追高"
    if sentiment_label == "偏空":
        return hints.get("bear") or f"{tag}偏空，规避相关资产"
    return hints.get("neutral") or f"{tag}分化，等待催化"

def analyze_keyword_heatmap(brief: dict, top_n: int = 40) -> dict:
    """AI 情绪热力图：关键词级热度与情绪（面向股市投资）。

    对每条新闻标题提及的 _THEMES 关键词单独计：
      mentions 跨提及数、跨源数、bull/bear/net/signals、情绪分、热度分(0-100)、
      热度等级、共振、分歧度、爆发系数、资金倾向、投资评级、风险等级、置信度，
      并保留每个关键词的 3 条线索新闻（方向一致优先）、关联政策维度与港股 ETF 双标的映射。

    返回：
        keywords  全部命中关键词按热度排序 [{keyword, display, tag, concept,
                   etf_pair[{code, name, benchmark, mapping}], etf_pair_text, mentions, sources,
                   bull, bear, net, signals, sentiment, sentiment_label, heat, heat_level,
                   divergence, burst, flow, risk, confidence, rating, hint, reading,
                   evidence[{title, source, url, direction}], policy_tags, heat_raw}]
        total_hit 命中关键词数
        total_mentions 提及总条次（去重前，跨关键词会重复计数）
        total_signals 参与多空判定的信号总条次
        overall_bias 整体偏向（由全部信号 bull/bear 决定）
        top_opportunity 机会关键词 Top3（偏多且热度高，按 burst 排序）
        top_pressure 风险关键词 Top3（偏空且热度高）
        headline 热力图总结句
        indicators 指标说明（供前端图例使用）
        note 口径说明
    """  
    # 1) 构建去重后的关键词表：display -> {tag, raws}
    keyword_meta: dict = {}
    for tag, kws in _THEMES:
        for kw in kws:
            display = _heatmap_display(kw)
            if display not in keyword_meta:
                keyword_meta[display] = {"tag": tag, "raws": []}
            if kw not in keyword_meta[display]["raws"]:
                keyword_meta[display]["raws"].append(kw)

    stats: dict = {}
    for display, meta in keyword_meta.items():
        stats[display] = {
            "keyword": display,
            "display": display,
            "tag": meta["tag"],
            "raws": meta["raws"],
            "mentions": 0,
            "sources": set(),
            "bull": 0,
            "bear": 0,
            "evidence": [],
        }

    total_titles = 0
    # 2) 逐条标题归因
    for name, items in (brief or {}).items():
        for item in items or []:
            title = item.get("title", "") or ""
            if not title:
                continue
            total_titles += 1
            title_lower = title.lower()
            direction = _direction(title, title_lower)
            for display, meta in keyword_meta.items():
                if any(_kw_hit(title, title_lower, raw) for raw in meta["raws"]):
                    entry = stats[display]
                    entry["mentions"] += 1
                    entry["sources"].add(name)
                    if direction > 0:
                        entry["bull"] += 1
                    elif direction < 0:
                        entry["bear"] += 1
                    entry["evidence"].append({
                        "title": title,
                        "source": name,
                        "url": item.get("url") or "",
                        "direction": direction,
                        "time": item.get("time") or item.get("date") or "当日",
                    })

    # 3) 过滤零命中
    active = [v for v in stats.values() if v["mentions"] > 0]
    if not active:
        return {
            "keywords": [],
            "total_hit": 0,
            "total_mentions": 0,
            "total_signals": 0,
            "overall_bias": "中性",
            "top_opportunity": [],
            "top_pressure": [],
            "headline": "当日样本中暂无关键词命中（投资主线词库均未命中），热力图等待数据刷新，不做无依据推演。",
            "indicators": _HEATMAP_INDICATORS,
            "note": "热力图基于投资主线词库（AI/光通信/半导体/贵金属/汇率等）按标题命中统计，零命中时不展示热力。",
        }

    # 4) 热度归一
    raw_heats = []
    for entry in active:
        raw = entry["mentions"] * (1 + math.log1p(len(entry["sources"])) )
        raw_heats.append(raw)
        entry["_raw"] = raw
    max_raw = max(raw_heats) if raw_heats else 1

    keywords = []
    total_signals = 0
    total_mentions = 0
    bull_all = bear_all = 0
    for entry in active:
        sources_cnt = len(entry["sources"])
        mentions = entry["mentions"]
        bull = entry["bull"]
        bear = entry["bear"]
        signals = bull + bear
        net = bull - bear
        total_mentions += mentions
        total_signals += signals
        bull_all += bull
        bear_all += bear
        sentiment = round((bull - bear) / signals, 3) if signals else 0.0
        heat = int(round(entry["_raw"] / max_raw * 100)) if max_raw else 0
        heat_level = _heatmap_level(heat)
        sentiment_label = _heatmap_sentiment_label(sentiment, signals)
        divergence = round(1 - abs(sentiment), 3) if signals >= 2 else 0.0
        burst = round(heat * abs(sentiment), 1)
        flow = _heatmap_flow(sentiment, heat)
        risk = _heatmap_risk(sentiment, divergence, heat)
        confidence = _heatmap_confidence(sources_cnt, mentions)
        rating = _heatmap_rating(sentiment, heat, net, signals)
        tag = entry["tag"]
        display = entry["keyword"]
        etf_pair = hk_etf_pair(display, tag)
        etf_pair_text = _hk_etf_pair_text(etf_pair)
        concept = f"港股 ETF 双标的：{etf_pair[0]['code']} × {etf_pair[1]['code']}"
        hint = (_heatmap_hint(tag, sentiment_label)
                + f"；组合仅作主题观察：{etf_pair[0]['code']} + {etf_pair[1]['code']}")
        # 证据：方向一致优先，同源同题去重，保留 3 条
        want = 1 if sentiment > 0 else (-1 if sentiment < 0 else 0)
        ev_sorted = sorted(entry["evidence"], key=lambda e: 0 if e["direction"] == want else (1 if e["direction"] == 0 else 2))
        seen_ev, evidence = set(), []
        for ev in ev_sorted:
            key = (ev["source"], ev["title"])
            if key in seen_ev:
                continue
            seen_ev.add(key)
            evidence.append({k: ev[k] for k in ("title", "source", "url", "direction", "time")})
            if len(evidence) >= 3:
                break
        # 关联政策维度：由证据标题的政策标签聚合
        policy_tags = []
        seen_pt = set()
        for ev in evidence:
            for pt in _policy_tags(_policy_norm(ev["title"])):
                if pt not in seen_pt:
                    seen_pt.add(pt)
                    policy_tags.append(pt)
        reading = (f"{display}（{tag}）{heat_level}·热度{heat}｜情绪{sentiment_label}"
                   f"（净{net:+d}｜{sources_cnt}源·{mentions}条｜信号{bull}:{bear}）｜{flow}｜{rating}｜"
                   f"风险{risk}·置信度{confidence}。港股 ETF 双标的：{etf_pair_text}。提示：{hint}。")
        keywords.append({
            "keyword": display,
            "display": display,
            "tag": tag,
            # ``concept`` 保留兼容旧前端；结构化消费者优先读取 etf_pair。
            "concept": concept,
            "etf_pair": etf_pair,
            "etf_pair_text": etf_pair_text,
            "mentions": mentions,
            "sources": sources_cnt,
            "bull": bull,
            "bear": bear,
            "net": net,
            "signals": signals,
            "sentiment": sentiment,
            "sentiment_label": sentiment_label,
            "heat": heat,
            "heat_raw": round(entry["_raw"], 3),
            "heat_level": heat_level,
            "divergence": divergence,
            "burst": burst,
            "flow": flow,
            "risk": risk,
            "confidence": confidence,
            "rating": rating,
            "hint": hint,
            "reading": reading,
            "evidence": evidence,
            "policy_tags": policy_tags[:3],
        })

    # 排序：热度降序，其次爆发系数，其次提及数
    keywords.sort(key=lambda x: (x["heat"], x["burst"], x["mentions"]), reverse=True)
    if top_n and len(keywords) > top_n:
        keywords = keywords[:top_n]

    # 整体偏向
    if bull_all + bear_all > 0:
        overall_sent = (bull_all - bear_all) / (bull_all + bear_all)
        if overall_sent >= 0.2:
            overall_bias = "偏多"
        elif overall_sent <= -0.2:
            overall_bias = "偏空"
        else:
            overall_bias = "中性"
    else:
        overall_bias = "中性"

    # Top 机会/承压：按 burst 排序
    top_opportunity = sorted([k for k in keywords if k["sentiment"] > 0.2 and k["heat"] >= 30], key=lambda x: x["burst"], reverse=True)[:3]
    top_pressure = sorted([k for k in keywords if k["sentiment"] < -0.2 and k["heat"] >= 30], key=lambda x: x["burst"], reverse=True)[:3]
    # 若无，也取热度最高的偏多/偏空中性填补，避免空
    if not top_opportunity:
        top_opportunity = [k for k in keywords if k["sentiment"] >= 0][:1]
    if not top_pressure:
        top_pressure = [k for k in keywords if k["sentiment"] < 0][:1]

    # 总结句
    if keywords:
        top_kw = keywords[0]
        burst_kw = max(keywords, key=lambda x: x["burst"]) if keywords else None
        headline = f"情绪热力图：{len(keywords)} 个投资关键词命中（{total_mentions} 条线索·{total_titles} 篇标题），整体{overall_bias}；" 
        headline += f"最热「{top_kw['keyword']}」{top_kw['heat_level']}·热度{top_kw['heat']}（{top_kw['sentiment_label']}·净{top_kw['net']:+d}·{top_kw['sources']}源共振），"
        if burst_kw and burst_kw["keyword"] != top_kw["keyword"]:
            headline += f"爆发系数最高「{burst_kw['keyword']}」{burst_kw['burst']:.1f}（{burst_kw['rating']}），"
        if top_opportunity:
            headline += f"机会：{'、'.join(k['keyword'] for k in top_opportunity[:2])}；"
        if top_pressure:
            headline += f"承压：{'、'.join(k['keyword'] for k in top_pressure[:2])}；"
        headline += "热度>75 为沸腾需防拥挤，跨源≥4 为确认信号；仅统计信号，不作为投资依据。"
    else:
        headline = "暂无关键词命中。"

    return {
        "keywords": keywords,
        "total_hit": len(keywords),
        "total_mentions": total_mentions,
        "total_signals": total_signals,
        "total_titles": total_titles,
        "overall_bias": overall_bias,
        "bull_all": bull_all,
        "bear_all": bear_all,
        "top_opportunity": top_opportunity,
        "top_pressure": top_pressure,
        "headline": headline,
        "indicators": _HEATMAP_INDICATORS,
        "note": "指标口径：热度=提及×(1+ln(跨源+1))归一；情绪=(多-空)/信号；爆发=热度×|情绪|；分歧=1-|情绪|；资金倾向由情绪与热度推断。每个关键词固定给出两个港交所现有 ETF 及对应指数；没有纯主题产品时明确标注代理。线索列表（标题/来源/时间/情绪标签）不展示，仅统计信号，不构成投资建议。",
    }


def _compose_headline(bias: str, top_themes: list) -> str:
    """依据多空方向与热度最高的板块，拼出自然的「AI 每日总结」总结句。"""
    t1 = top_themes[0][0] if top_themes else None
    t2 = top_themes[1][0] if len(top_themes) > 1 else None

    if bias == "偏多":
        if t1 and t2:
            return f"市场风险偏好回升，资金聚焦{t1}与{t2}两条线索，多方情绪占优，但短期需警惕高位分化。"
        if t1:
            return f"市场情绪偏暖，{t1}成为资金聚焦主线，多方占优，注意高位波动。"
        return "市场情绪偏暖，多方占优，但热点轮动较快，注意追高风险。"
    if bias == "偏空":
        if t1 and t2:
            return f"市场情绪转弱，{t1}与{t2}扰动增多，避险情绪升温，短期宜控制仓位。"
        if t1:
            return f"市场情绪转弱，{t1}风险扰动增多，空方占优，宜谨慎应对。"
        return "市场情绪偏谨慎，空方占优，风险事件增多，短期以防御为主。"
    if t1 and t2:
        return f"市场分歧加大，{t1}与{t2}轮动频繁，多空力量接近，方向尚待明朗。"
    if t1:
        return f"市场方向未明，{t1}成为焦点但分歧较大，等待更多信号确认。"
    return "市场多空拉锯，方向尚不明朗，建议控制仓位、等待信号。"


def _compose_sentiment(bias: str, bull_pct: int, bear_pct: int) -> str:
    """观点 1：今天市场情绪——由多空占比与方向拼出情绪描述。"""
    if bias == "偏多":
        mood = "情绪明显乐观" if bull_pct >= 70 else "情绪偏暖，多方占上风"
        return f"{mood}，风险偏好回升，但需留意高位轮动分化。"
    if bias == "偏空":
        mood = "避险情绪主导" if bear_pct >= 70 else "情绪偏谨慎"
        return f"{mood}，风险扰动增多，短线以防御为主。"
    return "多空分歧较大，观望情绪浓，方向选择临近，跟随信号为宜。"


def _compose_battle(bias: str, bull_pct: int, bear_pct: int, signals: int) -> str:
    """观点 2：多空博弈概率——百分比 + 胶着/占优判断 + 样本量。"""
    if signals <= 0:
        return "暂无可统计的多空信号，按 50% : 50% 中性看待，等待新数据。"
    if bull_pct >= 60:
        verdict = "多方明显占优"
    elif bull_pct <= 40:
        verdict = "空方略占上风"
    else:
        verdict = "多空胶着，差距有限"
    return f"多方 {bull_pct}% : 空方 {bear_pct}%，{verdict}（基于 {signals} 条多空信号）。"


def _compose_sectors(sectors_up: list, sectors_down: list) -> str:
    """观点 3：利好板块 / 利差板块——净多、净空信号各自排序取头部。"""
    up_text = "、".join(sectors_up) if sectors_up else "暂无明确主线，多头信号分散"
    down_text = "、".join(sectors_down) if sectors_down else "暂无明显承压板块"
    return f"利好板块：{up_text}　|　利差板块：{down_text}。"


def _compose_flow(sectors_up: list, sectors_down: list, top_themes: list) -> str:
    """观点 4：资金流向分析——净流入看利好板块，回避方向看利差板块。"""
    parts = []
    if sectors_up:
        parts.append(f"资金净流入集中在{'、'.join(sectors_up)}")
    if sectors_down:
        parts.append(f"{'、'.join(sectors_down)}相关资产遭资金回避")
    if parts:
        return "；".join(parts) + "。"
    hot = [tag for tag, _, _ in top_themes[:2]]
    if hot:
        return f"资金目光聚焦{'、'.join(hot)}，但方向性流入尚不明确，轮动观望为主。"
    return "样本有限，资金流向暂不明朗，建议等待更多信号确认。"


# ---------------------------------------------------------------- 「AI 政策分析」板块
# 政策面单列成板块：与「AI 每日总结」「AI 板块机会」共用同一批真实抓取标题，但归因口径
# 不同——不按行业板块、而按「政策维度」统计：
#   · 命中维度关键词即计入热度，按数据源去重计权（跨源命中 = 更强的政策信号）；
#   · 多空方向沿用 _BULLISH / _BEARISH 词库（该政策对市场是支撑还是压制）；
#   · 另用 _HAWKISH / _DOVISH 词库判定政策取向（偏鹰 = 收紧，偏鸽 = 宽松）。
# 与其余 AI 板块一样零外部依赖、可离线运行；当日样本没有政策信号时如实标注，不编造结论。

# (政策维度标签, 关键词)。一条标题命中任一关键词即计入该维度。
# ASCII 关键词按「单词边界」匹配（末尾 * 表示允许后缀，如 tariff* 命中 tariffs）：
# 既大小写不敏感（demo 里的 "BESSENT … dovish" 同样计入「货币政策 · 美联储」），
# 又不会让 FedEx 误命中 Fed。中文关键词按子串匹配。
_POLICY_BUCKETS = [
    ("货币政策 · 美联储", ["美联储", "Fed", "FOMC", "Powell", "鲍威尔", "贝森特", "Bessent", "议息",
                          "利率决议", "点阵图", "美债收益率", "联邦基金利率", "鹰派", "鸽派",
                          "hawkish", "dovish", "Federal Reserve", "interest rate*", "rate cut*", "rate hike*",
                          "monetary polic*", "ECB", "BOJ", "Bank of Japan", "Bank of England", "Lagarde", "Ueda"]),
    ("央行 · 流动性", ["央行", "中国人民银行", "PBOC", "central bank*", "逆回购", "买断式", "净投放",
                     "MLF", "LPR", "降准", "降息", "中期借贷", "再贷款", "流动性", "资金面", "Shibor",
                     "liquidity", "reserve requirement*"]),
    ("汇率与外汇干预", ["汇率", "日元", "人民币", "yuan", "美元指数", "在岸", "离岸", "外汇干预",
                       "弱日元", "套利交易", "抛美债", "yen", "fx", "forex", "exchange rate*", "currenc*",
                       "rate check", "dollar index"]),
    ("财政 · 关税与债务", ["财政", "关税", "tariff*", "加征", "国债", "赤字", "deficit", "预算", "专项债",
                         "特别国债", "债务上限", "退税", "减税", "税收", "增值税", "treasury", "budget*",
                         "fiscal", "tax", "taxes", "taxation", "debt ceiling", "spending bill", "duties",
                         "national debt", "government bond*"]),
    ("产业与科技政策", ["产业政策", "补贴", "subsid*", "出口管制", "export control*", "国产替代", "自主可控",
                       "强标", "准入", "白名单", "试点", "规划", "专项", "国家大基金", "反垄断", "antitrust",
                       "数据中心设备", "行动方案", "实施方案", "指导意见", "两用物项", "反倾销",
                       "industrial polic*", "chips act", "AI safety", "AI Act", "AI regulation", "anti-dumping",
                       "export ban*", "tech polic*", "data protection"]),
    ("资本市场监管", ["证监会", "CSRC", "吴清", "港交所", "交易所", "IPO", "发行上市", "内地企业香港上市",
                     "退市", "股份回购", "股票回购", "分红", "减持", "增持", "立案", "监管", "regulator*", "合规", "信披",
                     "新规", "征求意见", "财务造假", "处罚", "SEC", "FCA", "regulation*", "enforcement", "rulemaking",
                     "disclosure", "delist*", "compliance", "charges", "charged", "exemptive relief", "consultation"]),
    ("地缘与贸易政策", ["伊朗", "Iran", "阿曼", "霍尔木兹", "制裁", "sanction*", "停火", "ceasefire", "谈判",
                       "中东", "俄乌", "特朗普", "Trump", "白宫", "战争", "冲突", "禁止进口", "使馆", "经贸磋商",
                       "反制", "White House", "NATO", "Ukraine", "Russia", "Israel", "Gaza", "Taiwan", "Pentagon",
                       "trade deal*", "trade talk*", "trade war", "Congress", "Senate", "election*", "midterm*",
                       "Greenland", "summit", "Xi Jinping", "Beijing", "national emergency", "executive order*",
                       "presidential", "military"]),
    ("地产与地方政策", ["楼市", "地产", "房价", "房企", "限购", "限售", "公积金", "城中村", "保障房", "收储",
                       "城市更新", "housing", "mortgage*", "property market", "real estate"]),
]

# 政策取向词库：鹰派 = 收紧（对估值与利率敏感资产偏压制），鸽派 = 宽松 / 扶持（偏支撑）。
_HAWKISH = ["鹰派", "加息", "缩表", "收紧", "维持高利率", "强硬", "加征关税", "制裁", "增额关税",
            "限制", "禁止进口", "抛美债", "强势美元", "hawkish", "tighten*", "hike*", "sanction*", "tariff*",
            "restrictive", "crackdown", "curb*", "ban", "bans", "banned", "probe*", "penalt*", "export control*",
            "blacklist*", "reparations", "duties", "anti-dumping", "反倾销", "反制", "查处", "处罚"]
_DOVISH = ["鸽派", "转鸽", "降息", "降准", "宽松", "扩表", "放水", "支持", "扶持", "补贴", "减税",
           "退税", "豁免", "净流入", "流动性支持", "dovish", "easing", "stimulus", "rate cut*", "relief",
           "bailout", "tax cut*", "exempt*", "waiver*", "subsid*", "lifts sanctions", "lift sanctions",
           "lifted sanctions", "促进", "支持实体", "优惠", "促消费", "扩容"]
# 否定词：标题常用「不再那么鸽派」表述立场反转，命中否定词时把取向票翻转计到对面。
# 「近指」只看紧邻的前两个字符（避免「美元不涨，鸽派升温」被误翻转），「远指」看前 24 字符。
_POLICY_NEG_NEAR = ["不", "非", "未", "无"]
_POLICY_NEG_FAR = ["不再", "不太", "并非", "难言", "谈不上", "no longer", "not ", "never", "without"]


def _policy_norm(text: str) -> str:
    """标题归一化：统一小写比较（中文不受影响），使英文关键词大小写不敏感。"""
    return (text or "").lower()


@lru_cache(maxsize=2048)
def _ascii_keyword_regex(kw: str):
    """ASCII 关键词 → 单词边界正则（末尾 * 允许后缀）。词库固定，缓存编译结果。"""
    core = re.escape(kw[:-1] if kw.endswith("*") else kw)
    tail = "" if kw.endswith("*") else r"(?![a-z0-9])"
    return re.compile(rf"(?<![a-z0-9]){core}{tail}")


def _policy_findings(title_lower: str, keyword: str):
    """逐个返回关键词在标题中的命中位置：ASCII 词按单词边界，中文按子串。"""
    kw = keyword.lower()
    if kw.isascii():
        for match in _ascii_keyword_regex(kw).finditer(title_lower):
            yield match.start()
        return
    start = 0
    while True:
        index = title_lower.find(kw, start)
        if index == -1:
            return
        yield index
        start = index + len(kw)


def _policy_hit(title_lower: str, keyword: str) -> bool:
    """关键词命中判定。"""
    return next(_policy_findings(title_lower, keyword), None) is not None


def _policy_tags(title_lower: str) -> list:
    """返回标题命中的政策维度标签（保持 _POLICY_BUCKETS 顺序）。"""
    return [tag for tag, keywords in _POLICY_BUCKETS
            if any(_policy_hit(title_lower, keyword) for keyword in keywords)]


def _policy_negated(title_lower: str, at: int) -> bool:
    """判定 at 位置的政策词是否被否定词反转（「不再那么鸽派」= 鹰派）。"""
    near = title_lower[max(0, at - 2):at]
    if any(neg in near for neg in _POLICY_NEG_NEAR):
        return True
    far = title_lower[max(0, at - 24):at]
    return any(neg in far for neg in _POLICY_NEG_FAR)


def _policy_stance_votes(title_lower: str) -> tuple:
    """统计单条标题的鹰派 / 鸽派票数：命中否定词的票翻转记到对面（如「不再那么鸽派」）。"""
    hawk = dove = 0
    for lexicon, side in ((_HAWKISH, "hawk"), (_DOVISH, "dove")):
        for word in lexicon:
            for at in _policy_findings(title_lower, word):
                negated = _policy_negated(title_lower, at)
                if side == "hawk":
                    if negated:
                        dove += 1
                    else:
                        hawk += 1
                else:
                    if negated:
                        hawk += 1
                    else:
                        dove += 1
    return hawk, dove


def _policy_stance(hawk: int, dove: int) -> str:
    """鹰派 / 鸽派命中数 → 政策取向标签（阈值与多空判定一致：60% / 40%）。"""
    total = hawk + dove
    if not total:
        return "中性"
    ratio = hawk / total
    if ratio >= 0.6:
        return "偏鹰"
    if ratio <= 0.4:
        return "偏鸽"
    return "中性"


def _policy_short(tag: str) -> str:
    """政策维度标签简写（去掉「 · 美联储」这类后缀），用于总结句保持精炼。"""
    return (tag or "").split(" · ")[0]



def analyze_policy(brief: dict, series: dict | None = None, deep: bool = True) -> dict:
    """「AI 政策分析」引擎：政策维度热度 + 多空方向 + 鹰鸽取向（全部来自真实抓取标题）。

    ``deep=True``（默认）时再叠加深度层 :func:`analyze_policy_deep`：政策法规知识库检索与对比、
    修饰词术语口径、政策舆情情感打分、政策传导图谱、四步分析师推理链、思维导图、政策影响研报，
    以及 LSTM + Prophet 式两个时序模型（``series`` 缺省时由 :func:`get_policy_series` 决定：
    推送路径缓存的真实日 K，否则明确标注的合成演示序列）。

    返回：
        buckets      命中的政策维度（按热度排序），每项含 tag / stance / hawk / dove / up / down /
                     net / mentions / sources / evidence[{title, source, url, direction, stance}]
        top_themes   [(维度标签, 跨源数, 提及数)]，按 跨源数 → 提及数 排序
        sectors      热度最高的政策维度标签（最多 2 个）
        stance       整体政策取向：偏鹰 / 偏鸽 / 中性
        hawk, dove   鹰派 / 鸽派命中条数（按 (来源, 标题) 去重）
        bull, bear   政策面多方 / 空方信号条数（去重后）
        mentions     政策面提及总条数（命中任一维度的去重标题数）
        signals      参与统计的政策面多空信号总条数（= bull + bear）
        sources_hit  出现政策面内容的数据源个数
        up_tags / down_tags  政策净多 / 净空的维度标签（按净信号强度排序）
        opportunity_buckets / pressure_buckets  上述两个方向的完整维度明细（供页面 / 简报取用）
        headline     「AI 政策分析」总结句（纯文本，约 100 字）
        market_impact 近30日全球政策发布对 A股 / 港股 / 美股的影响（与 deep 无关，见 policy_impact.py）
        深度层（deep=True 时追加）：
        kb           政策法规知识库检索结果 {query, tags, hits[{date, issuer, title, phrase, ...}], comparison}
        modifiers    政策修饰词术语解读 {hits[{word, sense, strength, market, legal}], score, bias, reading}
        sentiment    政策舆情情感打分 {overall, official, market, gap, items, window}
        graph        政策传导图谱 {chains, nodes, edges}
        reasoning    四步分析师推理链 {steps[{no, key, label, text, evidence}], confidence}
        mindmap      政策影响思维导图 {root, branches[{label, children}]}
        research     结构化政策影响研报 {title, rating, abstract, long_term, risks, watch_window, ...}
        models       时序模型 {series, shock, lstm, prophet}
    """
    tags = [tag for tag, _ in _POLICY_BUCKETS]
    bucket_sources = {tag: set() for tag in tags}
    bucket_mentions = {tag: 0 for tag in tags}
    bucket_up = {tag: 0 for tag in tags}
    bucket_down = {tag: 0 for tag in tags}
    bucket_hawk = {tag: 0 for tag in tags}
    bucket_dove = {tag: 0 for tag in tags}
    bucket_evidence = {tag: [] for tag in tags}

    seen = set()
    hit_sources = set()
    mentions = hawk = dove = bull = bear = 0

    for name, items in (brief or {}).items():
        for item in items or []:
            title = item.get("title", "") or ""
            if not title:
                continue
            title_lower = _policy_norm(title)
            hits = _policy_tags(title_lower)
            if not hits:
                continue
            # 多空：单条标题按「多方词命中数 vs 空方词命中数」判定方向（中英文词库）。
            direction = _direction(title, title_lower)
            # 取向：鹰派 / 鸽派票（否定前缀自动翻转，如「不再那么鸽派」计为鹰派）。
            hawk_votes, dove_votes = _policy_stance_votes(title_lower)
            is_hawk = hawk_votes > dove_votes
            is_dove = dove_votes > hawk_votes
            stance_label = "鹰派" if is_hawk else "鸽派" if is_dove else "中性"
            key = (name, title)
            if key not in seen:
                # 整体口径按 (来源, 标题) 去重：同一条新闻命中多个维度只计一次热度。
                seen.add(key)
                mentions += 1
                hit_sources.add(name)
                hawk += int(is_hawk)
                dove += int(is_dove)
                if direction > 0:
                    bull += 1
                elif direction < 0:
                    bear += 1
            for tag in hits:
                bucket_mentions[tag] += 1
                bucket_sources[tag].add(name)
                if direction > 0:
                    bucket_up[tag] += 1
                elif direction < 0:
                    bucket_down[tag] += 1
                bucket_hawk[tag] += max(hawk_votes - dove_votes, 0)
                bucket_dove[tag] += max(dove_votes - hawk_votes, 0)
                bucket_evidence[tag].append({
                    "title": title,
                    "source": name,
                    "url": item.get("url") or "",
                    "direction": direction,
                    "stance": stance_label,
                })

    ranked = sorted(
        ((tag, len(bucket_sources[tag]), bucket_mentions[tag]) for tag in tags),
        key=lambda entry: (entry[1], entry[2]),
        reverse=True,
    )
    top_themes = [(tag, src, men) for tag, src, men in ranked if men > 0]
    sectors = [tag for tag, _, _ in top_themes[:2]]

    def _bucket_entry(tag: str, want_dir: int, evidence_limit: int) -> dict:
        """政策维度明细：热度 + 多空 + 鹰鸽取向 + 支撑证据（同方向优先，同源同题去重）。"""
        evidence, seen_ev = [], set()
        for ev in sorted(
            bucket_evidence[tag],
            key=lambda e: 0 if e["direction"] == want_dir else (1 if e["direction"] == 0 else 2),
        ):  # 稳定排序：同方向内保持抓取顺序
            key = (ev["source"], ev["title"])
            if key in seen_ev:
                continue
            seen_ev.add(key)
            evidence.append({k: ev[k] for k in ("title", "source", "url", "direction", "stance")})
            if len(evidence) >= evidence_limit:
                break
        return {
            "tag": tag,
            "stance": _policy_stance(bucket_hawk[tag], bucket_dove[tag]),
            "hawk": bucket_hawk[tag],
            "dove": bucket_dove[tag],
            "up": bucket_up[tag],
            "down": bucket_down[tag],
            "net": bucket_up[tag] - bucket_down[tag],
            "mentions": bucket_mentions[tag],
            "sources": len(bucket_sources[tag]),
            "evidence": evidence,
        }

    buckets = [_bucket_entry(tag, 0, 3) for tag, _, _ in top_themes]
    up_ranked = sorted(
        (e for e in buckets if e["net"] > 0),
        key=lambda e: (e["net"], e["sources"], e["mentions"]),
        reverse=True,
    )
    down_ranked = sorted(
        (e for e in buckets if e["net"] < 0),
        key=lambda e: (-e["net"], e["sources"], e["mentions"]),
        reverse=True,
    )
    data = {
        "buckets": buckets,
        "top_themes": top_themes,
        "sectors": sectors,
        "stance": _policy_stance(hawk, dove),
        "hawk": hawk,
        "dove": dove,
        "bull": bull,
        "bear": bear,
        "signals": bull + bear,
        "mentions": mentions,
        "sources_hit": len(hit_sources),
        "up_tags": [e["tag"] for e in up_ranked[:3]],
        "down_tags": [e["tag"] for e in down_ranked[:2]],
        "opportunity_buckets": up_ranked[:3],
        "pressure_buckets": down_ranked[:2],
    }
    data["headline"] = _compose_policy_headline(data)
    # 近30日全球政策 → 股市影响不跑 LSTM，浅层接口也返回，供页面与 ?deep=0 使用。
    data["market_impact"] = policy_impact.analyze_policy_market_impact(brief)
    if deep:
        data.update(analyze_policy_deep(data, brief, series=series))
    return data


def _compose_policy_headline(data: dict) -> str:
    """「AI 政策分析」总结句：政策主线 + 鹰鸽取向 + 利好 / 承压方向（约 100 字）。"""
    if not data["top_themes"]:
        return ("当日样本中暂无政策面信号命中（美联储、央行、关税、监管、地缘等关键词均未出现），"
                "等待数据源刷新后再作判断，不做无依据的政策推演。")
    names = "、".join(_policy_short(tag) for tag in data["sectors"])
    stance, hawk, dove = data["stance"], data["hawk"], data["dove"]
    if stance == "偏鹰":
        tone = f"取向偏鹰（鹰派 {hawk} : 鸽派 {dove}），收紧预期压制高估值与利率敏感资产"
    elif stance == "偏鸽":
        tone = f"取向偏鸽（鸽派 {dove} : 鹰派 {hawk}），宽松与扶持预期对市场构成支撑"
    else:
        tone = f"鹰鸽拉锯（鹰派 {hawk} : 鸽派 {dove}），政策方向尚未明朗"
    parts = [tone]
    if data["up_tags"]:
        parts.append(f"利好：{'、'.join(_policy_short(t) for t in data['up_tags'][:2])}")
    if data["down_tags"]:
        parts.append(f"承压：{'、'.join(_policy_short(t) for t in data['down_tags'][:2])}")
    if data["signals"]:
        parts.append(f"政策面多空 {data['bull']} : {data['bear']}")
    summary = (f"政策面集中在「{names}」（{data['sources_hit']} 源命中 · {data['mentions']} 条提及），"
               + "；".join(parts))
    return summary + "；建议关注政策信号的「转向确认」，避免单边押注。"


# ================================================================ 「AI 政策分析」深度层
# 「AI 政策深度」的主问题在 policy_impact.py：近30日已核验的全球政策发布对 A股/港股/美股的影响。
# 下面仍保留高级分析师链路，作为历史对照，不替代窗口内发文（零第三方依赖、可离线运行）：
#   ① 政策法规知识库检索（RAG）：央行报告 / 财政部文件 / 监管规章 / 境外央行决议作为企业知识库，
#      解读新政策时先从库中检索关联的历史政策与背景知识，再与新信号做对比分析
#      （_POLICY_KB → retrieve_policy_context() → policy_knowledge()）
#   ② 高级分析师推理链：宏观背景 → 行业限制 → 资金流向 → 受益板块（analyze_policy_reasoning()），
#      并自动生成结构化政策影响研报（policy_research_note()）与思维导图（policy_mindmap()）
#   ③ 金融术语口径（模拟国内金融巨头基于开源模型微调的金融专有大模型）：对「稳健 / 适度 / 从严」
#      等修饰词按经济学 + 法学双维解读（interpret_policy_modifiers()），比通用词频统计更贴近市场含义
#   ④ 政策舆情情感打分：政策发布后的新闻舆情与官媒解读按 正向 / 中性 / 负向 即时打分，
#      并用「官媒口径 vs 市场解读」的预期差捕捉政策窗口期（analyze_policy_sentiment()）
#   ⑤ 政策传导图谱：政策主体 → 受影响行业 → 产业链上下游节点串成一张图（policy_chain_graph()）
#   ⑥ LSTM：纯 Python 实现的单层 LSTM + BPTT 训练，捕捉时间序列的非线性与长期依赖，
#      评估重大政策后的中长期走势节奏（lstm_policy_forecast()）
#   ⑦ Prophet + ML 增强：趋势 + 季节性 + 政策突变（政策事件哑变量）的加性分解回归，
#      剥离季节性后度量政策窗口期的中期波动（prophet_policy_decompose()）
# 诚实原则与其余板块一致：样本不足时如实标注「数据缺口」，模型输出标注为模型情景，不编造结论。

# ---------------------------------------------------------------- ① 政策法规知识库（RAG）
# 条目为**摘要级**背景知识：发文机关 / 时间 / 关键表述 / 传导链 / 历史市场含义，用于解读新政策时
# 先检索历史政策再对比分析；具体数值以官方原文为准。新增一条只需在列表里加一行，
# ``dimension`` 必须与 _POLICY_BUCKETS 的标签一致，检索结果才能挂到当日的政策维度上。
_POLICY_KB = [
    {
        "id": "PBC-MPR-2024Q4", "date": "2025-02", "issuer": "中国人民银行", "doctype": "货币政策执行报告",
        "title": "2024 年第四季度中国货币政策执行报告", "dimension": "央行 · 流动性",
        "phrase": "适度宽松的货币政策",
        "keywords": ["货币政策执行报告", "适度宽松", "流动性充裕", "结构性货币政策工具", "社会综合融资成本",
                     "货币政策基调", "中央经济工作会议", "稳健"],
        "summary": "2024 年 12 月中央经济工作会议把货币政策基调由「稳健」调整为「适度宽松」，为 2011 年以来首次；"
                   "报告延续该基调，强调保持流动性充裕、引导社会综合融资成本下行、结构性工具加力。",
        "market_effect": "基调转松的历史含义是利率与流动性预期先改善，长久期成长与高股息资产同时受益，"
                         "但行情能否延续要看总量工具是否落地验证。",
        "industries": ["银行", "券商", "地产链", "基建"],
        "site": "http://www.pbc.gov.cn",
    },
    {
        "id": "PBC-MPR-2025Q1", "date": "2025-05", "issuer": "中国人民银行", "doctype": "货币政策执行报告",
        "title": "2025 年第一季度中国货币政策执行报告", "dimension": "央行 · 流动性",
        "phrase": "择机降准降息",
        "keywords": ["货币政策执行报告", "择机", "降准", "降息", "流动性", "社会融资规模", "适度宽松"],
        "summary": "延续适度宽松基调，明确「择机降准降息」，并把结构性工具与科技、消费、养老等再贷款额度并列安排。",
        "market_effect": "「择机」意味着落地时点不确定，市场会对时点反复定价，政策预期本身成为波动来源。",
        "industries": ["银行", "科技成长", "消费"],
        "site": "http://www.pbc.gov.cn",
    },
    {
        "id": "PBC-PACK-20240924", "date": "2024-09-24", "issuer": "中国人民银行 / 金融监管总局 / 证监会",
        "doctype": "一揽子增量政策", "title": "9·24 一揽子增量政策（降准、政策利率下调、资本市场两项新工具）",
        "dimension": "央行 · 流动性", "phrase": "一揽子增量政策",
        "keywords": ["降准", "降息", "逆回购", "互换便利", "回购增持再贷款", "一揽子", "增量政策", "流动性", "净投放"],
        "summary": "一次性宣布降准 0.5 个百分点、下调政策利率，并创设证券基金保险公司互换便利与股票回购增持再贷款，"
                   "总量工具与资本市场专用工具同时出手。",
        "market_effect": "历史上此类组合拳对应风险偏好快速修复，非银与券商弹性最大，随后行情由流动性驱动转向基本面验证。",
        "industries": ["券商", "非银金融", "地产链", "消费"],
        "site": "http://www.pbc.gov.cn",
    },
    {
        "id": "NPC-DEBT-20241108", "date": "2024-11-08", "issuer": "全国人大常委会 / 财政部",
        "doctype": "财政决议", "title": "地方政府化解隐性债务 10 万亿元方案", "dimension": "财政 · 关税与债务",
        "phrase": "置换隐性债务",
        "keywords": ["化债", "隐性债务", "专项债", "地方政府债务", "置换", "赤字", "财政", "付息"],
        "summary": "以新增债务限额 + 分年安排的专项债置换存量隐性债务，另有部分隐债按原合同延后偿还，"
                   "目标是压降地方付息压力与化解链条拖欠。",
        "market_effect": "化债改善地方与企业链条现金流，历史上利好建筑建材、环保与城投相关链条的应收账款修复。",
        "industries": ["建筑建材", "环保", "工程机械", "地方国企"],
        "site": "http://www.mof.gov.cn",
    },
    {
        "id": "MOF-SPECIAL-2024", "date": "2024-05", "issuer": "财政部 / 国家发展改革委", "doctype": "国债发行安排",
        "title": "超长期特别国债支持「两重」「两新」", "dimension": "财政 · 关税与债务",
        "phrase": "超长期特别国债",
        "keywords": ["超长期特别国债", "特别国债", "两重", "两新", "设备更新", "以旧换新", "财政", "赤字率"],
        "summary": "由中央发行超长期特别国债支持国家重大战略与重点领域安全能力建设，并配套设备更新、消费品以旧换新。",
        "market_effect": "中央加杠杆替代地方与居民加杠杆，需求端订单可见度提升，利好设备制造与耐用消费品。",
        "industries": ["工程机械", "机床", "家电", "汽车"],
        "site": "http://www.mof.gov.cn",
    },
    {
        "id": "SC-TRADEIN-202403", "date": "2024-03", "issuer": "国务院", "doctype": "行动方案",
        "title": "推动大规模设备更新和消费品以旧换新行动方案", "dimension": "产业与科技政策",
        "phrase": "以旧换新",
        "keywords": ["设备更新", "以旧换新", "行动方案", "补贴", "消费品", "工业母机", "节能降碳"],
        "summary": "以财政贴息、补贴与标准提升推动工业设备更新与耐用消费品换新，覆盖工业、农业、建筑、交通等领域。",
        "market_effect": "补贴落地节奏决定板块弹性，历史上先炒设备与渠道，后看订单与销量数据验证。",
        "industries": ["工程机械", "家电", "汽车", "工业母机"],
        "site": "https://www.gov.cn",
    },
    {
        "id": "SC-AI-202508", "date": "2025-08", "issuer": "国务院", "doctype": "意见",
        "title": "关于深入实施「人工智能+」行动的意见", "dimension": "产业与科技政策",
        "phrase": "人工智能+",
        "keywords": ["人工智能+", "人工智能", "算力", "大模型", "数据要素", "应用场景", "行动方案", "国产替代"],
        "summary": "顶层设计明确人工智能与科技、产业、消费、民生、治理等领域融合的行动路径与要素保障。",
        "market_effect": "政策催化通常先估值后订单：算力与数据基础设施先行，应用端需要收入落地才能接棒。",
        "industries": ["AI 算力", "光通信", "半导体", "数据要素", "AI 应用"],
        "site": "https://www.gov.cn",
    },
    {
        "id": "CSRC-NINE-20240412", "date": "2024-04-12", "issuer": "国务院 / 证监会", "doctype": "若干意见",
        "title": "关于加强监管防范风险推动资本市场高质量发展的若干意见（新「国九条」）",
        "dimension": "资本市场监管", "phrase": "从严监管",
        "keywords": ["国九条", "从严监管", "退市", "减持", "分红", "财务造假", "信披", "监管", "合规"],
        "summary": "以强监管、防风险、促高质量发展为主线，强化发行上市准入、持续监管与退市制度，压实中介责任。",
        "market_effect": "「从严」在监管语境里意味着执法尺度与违规成本同步抬升，历史上小市值、绩差股与题材股承压，"
                         "高分红与行业龙头相对受益。",
        "industries": ["券商", "高股息蓝筹", "中小市值题材"],
        "site": "http://www.csrc.gov.cn",
    },
    {
        "id": "CSRC-REDUCE-202405", "date": "2024-05", "issuer": "证监会", "doctype": "部门规章",
        "title": "上市公司股东减持股份管理暂行办法", "dimension": "资本市场监管", "phrase": "不得减持",
        "keywords": ["减持", "不得减持", "股份回购", "破发", "破净", "分红不达标", "规章", "监管"],
        "summary": "把减持限制上升为规章层级，对破发、破净、分红不达标等情形的控股股东设定不得减持的硬约束。",
        "market_effect": "减持约束收紧直接改善筹码供给，历史上对高质押、高减持压力的中小市值形成情绪修复。",
        "industries": ["中小市值成长", "券商"],
        "site": "http://www.csrc.gov.cn",
    },
    {
        "id": "CFWC-202310", "date": "2023-10", "issuer": "中央金融工作会议", "doctype": "会议定调",
        "title": "中央金融工作会议：加快建设金融强国，全面加强金融监管", "dimension": "资本市场监管",
        "phrase": "全面加强金融监管",
        "keywords": ["金融强国", "全面加强金融监管", "防范化解风险", "金融监管总局", "依法将所有金融活动纳入监管"],
        "summary": "首次提出金融强国目标，强调全面加强金融监管、防范化解金融风险，并调整金融监管体制。",
        "market_effect": "监管体制重塑属于制度型变量，影响的是长期估值中枢与合规成本，而非短期涨跌。",
        "industries": ["银行", "保险", "券商"],
        "site": "https://www.gov.cn",
    },
    {
        "id": "FED-CUT-202409", "date": "2024-09-18", "issuer": "美联储 FOMC", "doctype": "议息声明",
        "title": "联邦基金利率目标区间下调 50 个基点，启动本轮降息", "dimension": "货币政策 · 美联储",
        "phrase": "政策利率重新校准",
        "keywords": ["降息", "议息", "利率决议", "点阵图", "美联储", "联邦基金利率", "FOMC", "rate cut"],
        "summary": "以 50 个基点开启本轮降息周期，为 2020 年以来首次降息，年内累计降息 100 个基点。",
        "market_effect": "首轮降息落地后美元指数与美债利率通常先下后震荡，贵金属与新兴市场弹性最大；"
                         "节奏（每次多少基点）比方向更影响行情。",
        "industries": ["贵金属", "科技成长", "港股", "有色"],
        "site": "https://www.federalreserve.gov",
    },
    {
        "id": "FED-QT-2022", "date": "2022-06", "issuer": "美联储", "doctype": "资产负债表政策",
        "title": "启动缩表（QT）并随后放缓缩减节奏", "dimension": "货币政策 · 美联储", "phrase": "缩表放缓",
        "keywords": ["缩表", "QT", "资产负债表", "国债", "MBS", "流动性", "准备金", "tightening"],
        "summary": "以每月限额方式缩减国债与 MBS 持有规模，后期放缓国债缩减上限以维护准备金充裕。",
        "market_effect": "缩表节奏是美元流动性的慢变量，放缓或结束缩表往往先改善风险资产估值再改善盈利预期。",
        "industries": ["美债", "黄金", "全球风险资产"],
        "site": "https://www.federalreserve.gov",
    },
    {
        "id": "ECB-CUT-2024", "date": "2024-06", "issuer": "欧洲央行", "doctype": "议息决定",
        "title": "欧洲央行启动降息，存款机制利率逐步回到 2% 附近", "dimension": "货币政策 · 美联储",
        "phrase": "数据依赖",
        "keywords": ["欧洲央行", "ECB", "降息", "存款机制利率", "数据依赖", "欧元区", "Lagarde"],
        "summary": "在通胀回落背景下于 2024 年年中开启降息，此后按数据依赖路径继续下调，2025 年降至 2% 附近。",
        "market_effect": "「数据依赖」意味着每份通胀与工资数据都可能改变路径，欧元区资产波动更多来自数据而非会议。",
        "industries": ["欧股银行", "欧元汇率", "出口链"],
        "site": "https://www.ecb.europa.eu",
    },
    {
        "id": "BOJ-NIRP-2024", "date": "2024-03", "issuer": "日本央行", "doctype": "议息决定",
        "title": "日本央行结束负利率，政策利率逐步上调至 0.5% 附近", "dimension": "汇率与外汇干预",
        "phrase": "渐进正常化",
        "keywords": ["日本央行", "负利率", "收益率曲线控制", "日元", "套利交易", "加息", "外汇干预", "yen", "BOJ"],
        "summary": "结束负利率与收益率曲线控制，2025 年 1 月把政策利率上调至 0.5% 附近，走向渐进正常化。",
        "market_effect": "日元套利交易平仓反复扰动全球风险资产，日元升值阶段全球科技股波动明显放大。",
        "industries": ["日元汇率", "全球科技股", "套息资产"],
        "site": "https://www.boj.or.jp",
    },
    {
        "id": "PBC-HOUSING-202405", "date": "2024-05", "issuer": "中国人民银行 / 住房城乡建设部",
        "doctype": "地产金融政策", "title": "取消房贷利率下限、下调首付比例并设立保障性住房再贷款",
        "dimension": "地产与地方政策", "phrase": "收储",
        "keywords": ["楼市", "房贷利率", "首付比例", "保障性住房", "再贷款", "收储", "城中村", "限购", "房企"],
        "summary": "取消全国房贷利率政策下限、下调首付比例，并设立保障性住房再贷款支持地方国企收购存量商品房。",
        "market_effect": "需求端放松 + 收储去库存的组合，历史上先修复地产链情绪，销售与价格数据的验证滞后一个季度以上。",
        "industries": ["地产开发", "建材", "家居家电", "银行"],
        "site": "http://www.pbc.gov.cn",
    },
    {
        "id": "MFCOM-DUALUSE-202412", "date": "2024-12", "issuer": "国务院 / 商务部", "doctype": "行政法规",
        "title": "两用物项出口管制条例施行", "dimension": "地缘与贸易政策", "phrase": "出口管制",
        "keywords": ["两用物项", "出口管制", "管制清单", "反制", "制裁", "商务部", "禁止出口", "许可证"],
        "summary": "把两用物项出口管制统一到行政法规层级，实行清单化管理与许可制度，并保留反制措施。",
        "market_effect": "管制与反制属于典型的供给侧扰动，短期利好相关资源的定价权方，中期抬升全球供应链成本。",
        "industries": ["稀有金属", "半导体材料", "军工", "出口链"],
        "site": "http://www.mofcom.gov.cn",
    },
    {
        "id": "NPC-PRIVATE-20250520", "date": "2025-05-20", "issuer": "全国人大", "doctype": "法律",
        "title": "民营经济促进法施行", "dimension": "产业与科技政策", "phrase": "一视同仁",
        "keywords": ["民营经济", "促进法", "一视同仁", "公平竞争", "要素获取", "产权保护", "依法行政"],
        "summary": "以法律形式确立各类经济组织公平竞争、平等使用要素与产权保护的原则，规范涉企执法。",
        "market_effect": "属于制度型利好，改善的是长期风险溢价与估值中枢，短期弹性通常有限。",
        "industries": ["平台经济", "民营制造", "科技成长"],
        "site": "https://www.gov.cn",
    },
    {
        "id": "BIS-CHIPRULE-202412", "date": "2024-12", "issuer": "美国商务部 BIS", "doctype": "出口管制规则",
        "title": "对华半导体设备与高带宽存储出口管制规则升级", "dimension": "地缘与贸易政策",
        "phrase": "实体清单",
        "keywords": ["出口管制", "实体清单", "半导体设备", "高带宽存储", "HBM", "制裁", "关税", "国产替代",
                     "自主可控", "export control"],
        "summary": "扩大受控设备与存储范围并扩容实体清单，同时收紧对第三地转口的管辖。",
        "market_effect": "管制升级短期压制相关设备与存储链情绪，中期强化国产替代与自主可控主线的估值支撑。",
        "industries": ["半导体设备", "存储芯片", "国产算力", "光通信"],
        "site": "https://www.bis.doc.gov",
    },
]

# 检索口径：关键词命中 +2，政策维度匹配 +3（当日热度维度优先），关键表述命中 +1.5。
_POLICY_KB_WEIGHTS = {"keyword": 2.0, "dimension": 3.0, "phrase": 1.5}


def retrieve_policy_context(query: str, tags: list | None = None, top_k: int = 3) -> list:
    """从政策法规知识库检索与新政策最相关的历史条目（RAG 的检索环节）。

    打分口径固定、可复算：关键词命中 +2、当日政策维度匹配 +3、关键表述命中 +1.5，
    同分按日期从新到旧。返回条目附带 ``score`` / ``matched``（命中依据）/ ``why``（检索理由），
    便于在简报里把「为什么检索到这条历史政策」说清楚。
    """
    query_lower = _policy_norm(query or "")
    tag_set = set(tags or [])
    hits = []
    for entry in _POLICY_KB:
        matched = []
        score = 0.0
        for keyword in entry["keywords"]:
            if _policy_hit(query_lower, keyword):
                matched.append(keyword)
                score += _POLICY_KB_WEIGHTS["keyword"]
        if entry["dimension"] in tag_set:
            matched.append(entry["dimension"])
            score += _POLICY_KB_WEIGHTS["dimension"]
        if entry["phrase"] and _policy_hit(query_lower, entry["phrase"]):
            matched.append(entry["phrase"])
            score += _POLICY_KB_WEIGHTS["phrase"]
        if score > 0:
            hits.append({
                **{k: entry[k] for k in ("id", "date", "issuer", "doctype", "title", "dimension",
                                         "phrase", "summary", "market_effect", "industries", "site")},
                "score": round(score, 2),
                "matched": list(dict.fromkeys(matched)),
            })
    # 分数优先，同分取更新的政策条目。
    hits.sort(key=lambda item: item["date"], reverse=True)
    hits.sort(key=lambda item: -item["score"])
    for hit in hits:
        reasons = [m for m in hit["matched"]]
        hit["why"] = "命中「" + "、".join(reasons[:5]) + "」" if reasons else "同维度政策背景"
    return hits[:top_k]


def _policy_kb_query(policy: dict) -> tuple:
    """由当日政策统计构造检索式：热度维度标签 + 各维度支撑标题（真实抓取内容）。"""
    parts = []
    tags = []
    for tag, _src, _men in policy.get("top_themes") or []:
        tags.append(tag)
        parts.append(tag)
        parts.append(_policy_short(tag))
    for bucket in (policy.get("buckets") or [])[:4]:
        for ev in (bucket.get("evidence") or [])[:3]:
            parts.append(str(ev.get("title") or ""))
    return " ".join(p for p in parts if p), tags


def _policy_comparison(policy: dict, hit: dict) -> str:
    """检索到的历史政策 vs 当日新信号：对比分析（同向 / 反向 → 不同的市场含义）。"""
    stance = policy.get("stance") or "中性"
    hist_loose = hit["dimension"] in ("央行 · 流动性", "货币政策 · 美联储") and \
        any(word in hit["phrase"] for word in ("宽松", "降息", "降准", "一揽子", "放缓"))
    hist_tight = ("从严" in hit["phrase"] or "管制" in hit["phrase"] or "不得" in hit["phrase"]
                  or "缩表" in hit["phrase"] or "全面加强金融监管" in hit["phrase"])
    if stance == "偏鸽" and hist_loose:
        relation = "方向一致（同为宽松 / 扶持取向）"
        reading = "可按历史路径推演：预期先行、工具落地验证，弹性最大的通常是政策直接作用的行业。"
    elif stance == "偏鹰" and hist_tight:
        relation = "方向一致（同为收紧 / 管制取向）"
        reading = "历史经验是合规成本与估值同时受压，题材与高估值方向先调整，龙头与高分红相对抗跌。"
    elif stance == "中性":
        relation = "当日取向尚不明朗，历史条目提供背景基准"
        reading = "先按历史条目的传导链观察，等待表述变化（如「稳健」转「适度宽松」）再确认方向。"
    else:
        relation = "方向相反（当日取向与历史条目不同）"
        reading = "差异本身就是信号：政策力度边际变化的方向，比绝对水平更能决定资金流向。"
    return (f"历史对照：{hit['date']} {hit['issuer']}《{hit['title']}》关键表述「{hit['phrase']}」，"
            f"当时含义为{hit['market_effect']}本次信号取向{stance}，与历史{relation}。{reading}")


def policy_knowledge(policy: dict, top_k: int = 3) -> dict:
    """①知识库检索 + 对比分析：先检索历史政策与背景知识，再交由模型层做对比解读。

    返回：``hits`` 检索命中的知识库条目（含打分与检索理由）、``comparison`` 逐条对比分析文本、
    ``note`` 检索口径说明；当日无政策信号时如实标注，不硬凑历史条目。
    """
    query, tags = _policy_kb_query(policy or {})
    if not (policy or {}).get("top_themes"):
        return {
            "query": query, "tags": tags, "hits": [], "comparison": [],
            "note": "当日样本无政策维度命中，知识库检索未启动（不编造历史对照）。",
        }
    hits = retrieve_policy_context(query, tags, top_k=top_k)
    comparison = [_policy_comparison(policy, hit) for hit in hits]
    note = (f"知识库 {len(_POLICY_KB)} 条政策条目（央行报告 / 财政部文件 / 监管规章 / 境外央行决议）· "
            f"检索式 {len(query)} 字 · 命中 {len(hits)} 条"
            + ("" if hits else "（当日维度与关键词均未匹配到历史条目，仅按热度统计解读）"))
    return {"query": query, "tags": tags, "hits": hits, "comparison": comparison, "note": note}


# ---------------------------------------------------------------- ③ 金融术语：政策修饰词口径
# 模拟「国内金融巨头基于开源模型微调的金融专有大模型」的术语口径：同一个词在政策文本里的含义
# 高度依赖上下文，这里用经济学（对总量 / 结构 / 流动性的作用）+ 法学（规范强度）双维标注，
# strength 为 -3（收紧 / 强约束）…+3（宽松 / 强支持）的强度分，供市场含义解读与研报引用。
_POLICY_MODIFIERS = [
    ("适度宽松", "货币政策基调：由「稳健」转向总量偏松", 2.0,
     "降准降息预期升温，利率下行，长久期成长与高股息同时受益", None),
    ("稳健的货币政策", "基调表述：不搞强刺激、留有余地", 0.5,
     "不预期总量大水漫灌，行情更依赖结构性工具与财政配合", None),
    ("稳健", "基调表述：中性偏稳", 0.0,
     "总量中性，关注结构性工具与政策落地节奏", None),
    ("灵活适度", "操作口径：相机抉择、力度留弹性", 1.0,
     "宽松方向确定但节奏不确定，波动来自落地时点预期", None),
    ("精准有力", "操作口径：结构性、定向发力", 0.5,
     "利好政策点名领域，总量弹性有限", None),
    ("适度加力", "财政口径：赤字与支出小幅扩张", 1.5,
     "订单与需求可见度改善，利好基建与设备链条", None),
    ("更加积极", "财政口径：明显扩张", 2.0,
     "财政发力预期升温，周期与顺周期方向受益", None),
    ("总量和结构双重发力", "操作口径：总量 + 结构并举", 1.5,
     "既看利率下行也看定向领域，成长与顺周期同时受益", None),
    ("保持流动性充裕", "流动性口径：偏松", 2.0,
     "资金面宽松预期，短端利率与债市先受益", None),
    ("流动性合理充裕", "流动性口径：中性偏松", 1.0,
     "资金面平稳，不构成交易性机会，看结构", None),
    ("逆周期调节", "操作口径：对冲经济下行", 1.0,
     "经济弱 → 政策强的对冲逻辑，利好政策受益方向", None),
    ("跨周期调节", "操作口径：兼顾中长期平衡", 0.0,
     "强调不透支未来，短期刺激预期被压低", None),
    ("不搞大水漫灌", "操作口径：抑制总量宽松预期", -1.0,
     "压制流动性驱动的估值扩张，风格偏向盈利确定性", None),
    ("精准滴灌", "操作口径：定向投放", 0.0,
     "利好被点名行业，对大盘指数拉动有限", None),
    ("从严", "监管口径：执法尺度与违规成本同步抬升", -1.5,
     "合规成本上升，小市值与题材股承压，龙头相对受益",
     "法律含义：从严执法、从重处罚的执法尺度宣示"),
    ("强监管", "监管口径：全面强化监管", -1.5,
     "估值中枢下移与出清并行，长期利好合规龙头",
     "法律含义：监管权限与责任边界的扩张"),
    ("全面加强金融监管", "监管口径：制度型收紧", -1.0,
     "长期风险溢价改善，短期抑制高杠杆题材",
     "法律含义：将所有金融活动纳入监管"),
    ("审慎", "监管口径：谨慎、留余地", -0.5,
     "政策不会强刺激，预期差交易空间收窄", None),
    ("稳慎", "监管口径：稳妥而谨慎", -0.5,
     "改革推进节奏放缓，相关题材催化推迟", None),
    ("稳妥有序", "落地节奏：渐进、不搞一刀切", 0.0,
     "冲击被时间摊薄，相关板块波动下降", None),
    ("有序推进", "落地节奏：分步实施", 0.0,
     "利好确定性高的先行环节，后排环节等待", None),
    ("加快", "落地节奏：提速", 1.0,
     "催化提前兑现，主题行情启动更快", None),
    ("逐步", "落地节奏：分阶段", 0.0,
     "预期被拉长，交易节奏后移", None),
    ("试点", "适用范围：局部先行", 0.5,
     "利好试点区域 / 名单内企业，范围有限",
     "法律含义：授权范围内的例外安排"),
    ("窗口指导", "监管手段：非正式约束", -0.5,
     "短期压制相关业务扩张预期", None),
    ("依法依规", "合规口径：强调程序合法性", -0.5,
     "提高违规成本，利好合规体系完善的头部机构",
     "法律含义：以现行法律法规为依据的执法宣示"),
    ("严禁", "规范强度：禁止性规范", -2.0,
     "相关业务预期直接归零，替代方向受益",
     "法律含义：强制性禁止规范，违反即构成违法"),
    ("不得", "规范强度：禁止性规范", -1.5,
     "受限行为停止，筹码供给或竞争格局改善",
     "法律含义：义务性禁止规定"),
    ("原则上", "规范强度：允许例外", -0.5,
     "执行留有弹性，实际影响小于字面",
     "法律含义：一般规则 + 例外许可的立法技术"),
    ("鼓励", "政策取向：引导支持", 1.0,
     "利好被鼓励方向，多为估值修复而非业绩兑现", None),
    ("支持", "政策取向：明确支持", 1.0,
     "政策受益方向获得资金关注", None),
    ("一视同仁", "政策取向：公平竞争", 0.5,
     "改善民营与中小主体的长期风险溢价", None),
    ("坚决", "执行强度：不留余地", -1.0,
     "监管对象承压，市场解读为执行力度上限", None),
    ("依法将所有金融活动纳入监管", "监管口径：全覆盖", -1.0,
     "无监管套利空间，行业估值中枢重定价",
     "法律含义：监管范围的全覆盖宣示"),
]

# 政策舆情情感词库：政策发布后的新闻 / 官媒解读按 正向 / 中性 / 负向 即时打分。
_POLICY_SENT_POS = ["支持", "促进", "利好", "提振", "回暖", "宽松", "降准", "降息", "补贴", "减税", "退税",
                    "增长", "超预期", "上涨", "突破", "稳定", "保障", "鼓励", "落地", "见效", "改善", "修复",
                    "净流入", "扶持", "扩容", "优惠", "缓和", "停火", "豁免", "放开", "提振信心", "回升",
                    "stimulus", "easing", "relief", "approve*", "boost*", "recover*", "rall*", "gain*"]
_POLICY_SENT_NEG = ["收紧", "从严", "查处", "处罚", "立案", "制裁", "反制", "风险", "警示", "警惕", "下滑",
                    "下跌", "暴跌", "承压", "冲击", "限制", "禁止", "管制", "裁员", "危机", "担忧", "违约",
                    "逾期", "亏损", "抛售", "关税", "加征", "封锁", "升级", "干预", "贬值", "放缓", "衰退",
                    "sanction*", "tariff*", "crackdown", "curb*", "probe*", "warn*", "risk*", "plunge*", "ban"]
_POLICY_SENT_AMPLIFY = ["坚决", "严厉", "全面", "罕见", "史上最大", "大幅", "重磅", "紧急", "首次"]
# 官媒 / 官方口径识别：来源属于官方信息源，或标题带官方发布 / 解读特征。
_OFFICIAL_VOICE_SOURCES = ["国务院", "发改委", "财政部", "商务部", "证监会", "特区政府", "中国新闻社", "中新社",
                           "美联储", "欧洲央行", "SEC", "联邦公报", "GOV.UK", "FCA"]
_OFFICIAL_VOICE_MARKERS = ["解读", "发布会", "答记者问", "有关负责人", "新闻发言人", "社论", "评论员", "官方",
                           "回应", "印发", "发布会实录", "声明", "公告"]


def _is_official_voice(source: str, title: str) -> bool:
    """判定一条政策舆情是否属于官媒 / 官方解读口径（与市场化媒体解读分开统计）。"""
    text = f"{source or ''} {title or ''}"
    if any(marker in (source or "") for marker in _OFFICIAL_VOICE_SOURCES):
        return True
    return any(marker in (title or "") for marker in _OFFICIAL_VOICE_MARKERS)


def _policy_sentiment_score(title: str) -> int:
    """单条政策舆情的极性打分：正向词 - 负向词命中数（放大词把绝对值 ×2）。"""
    title_lower = _policy_norm(title)
    pos = sum(1 for word in _POLICY_SENT_POS if _kw_hit(title, title_lower, word))
    neg = sum(1 for word in _POLICY_SENT_NEG if _kw_hit(title, title_lower, word))
    score = pos - neg
    if score and any(word in title for word in _POLICY_SENT_AMPLIFY):
        score *= 2
    return score


def _sentiment_label(score: int) -> str:
    return "正向" if score > 0 else ("负向" if score < 0 else "中性")


def _sentiment_bucket(scores: list) -> dict:
    """把一组极性分聚合成 正向 / 中性 / 负向 计数 + 归一化情绪分（-1…1）+ 标签。"""
    pos = sum(1 for s in scores if s > 0)
    neg = sum(1 for s in scores if s < 0)
    neu = len(scores) - pos - neg
    total = len(scores)
    raw = sum(scores)
    norm = round(max(-1.0, min(1.0, raw / max(total, 1))), 3)
    return {
        "positive": pos, "negative": neg, "neutral": neu, "total": total,
        "score": norm,
        "label": "正向" if norm > 0.1 else ("负向" if norm < -0.1 else "中性"),
        "ratio": f"正向 {pos} : 中性 {neu} : 负向 {neg}",
    }


def analyze_policy_sentiment(brief: dict) -> dict:
    """④政策舆情情感打分：对政策发布后的新闻舆情与官媒解读即时打分（正向 / 中性 / 负向）。

    统计口径：命中政策维度关键词的条目，**或来自官方信息源**（国务院 / 部委 / 中国新闻社 /
    境外央行与监管机构，官媒解读本身就是政策舆情）的条目；并把「官媒 / 官方口径」与「市场化媒体解读」分开聚合，
    两者的情绪差就是政策窗口期的预期差信号（``gap`` / ``window``）。
    当日没有政策相关舆情时如实标注，不输出情绪结论。
    """
    scores, official_scores, market_scores, items = [], [], [], []
    seen = set()
    for name, entries in (brief or {}).items():
        official_source = _is_official_voice(name, "")
        for item in entries or []:
            title = str(item.get("title") or "")
            if not title or not (official_source or _policy_tags(_policy_norm(title))):
                continue
            key = (name, title)
            if key in seen:
                continue
            seen.add(key)
            score = _policy_sentiment_score(title)
            official = _is_official_voice(name, title)
            scores.append(score)
            (official_scores if official else market_scores).append(score)
            items.append({"title": title, "source": name, "score": score,
                          "label": _sentiment_label(score), "official": official})
    items.sort(key=lambda entry: -abs(entry["score"]))
    overall = _sentiment_bucket(scores)
    official = _sentiment_bucket(official_scores)
    market = _sentiment_bucket(market_scores)
    gap = round(official["score"] - market["score"], 3)

    if not scores:
        window = {"label": "无政策舆情样本", "note": "当日样本中没有政策相关舆情，情绪打分未启动。"}
    elif abs(gap) >= 0.3:
        if gap > 0:
            window = {
                "label": f"政策窗口期 · 预期差（官媒口径{official['label']} / 市场解读{market['label']}）",
                "note": (f"官媒口径情绪 {official['score']:+.2f} 高于市场化解读 {market['score']:+.2f}"
                         f"（差 {gap:+.2f}）；该差值为统计信号，仅供参考、不作为投资依据。"),
            }
        else:
            window = {
                "label": f"政策窗口期 · 预期差（市场解读{market['label']} / 官媒口径{official['label']}）",
                "note": (f"市场化解读情绪 {market['score']:+.2f} 高于官媒口径 {official['score']:+.2f}"
                         f"（差 {gap:+.2f}）；该差值为统计信号，仅供参考、不作为投资依据。"),
            }
    elif overall["label"] == "正向":
        window = {"label": "情绪加速期", "note": "官媒与市场解读同向偏正，注意政策落地后的兑现与追高风险。"}
    elif overall["label"] == "负向":
        window = {"label": "政策避险期", "note": "官媒与市场解读同向偏负，等待对冲性政策信号再评估风险偏好。"}
    else:
        window = {"label": "政策观望期", "note": "官媒与市场解读情绪接近且均为中性，政策方向待确认。"}

    return {
        "overall": overall, "official": official, "market": market,
        "gap": gap, "items": items[:6], "window": window,
        "note": ("情感口径：正向词 / 负向词命中差值定极性，「坚决 / 罕见 / 史上最大」等强度词把极性加倍；"
                 "统计范围为命中政策维度的舆情 + 官方信息源发布，官媒口径与市场化解读分开统计。"),
    }


# ---------------------------------------------------------------- ⑤ 政策传导图谱
# 政策主体 → 受影响行业 → 产业链上下游节点。图谱按政策维度组织：当日命中哪个维度，就把对应的
# 传导链拉出来，并带上该维度当日的政策净向（净多 / 净空），形成「政策 → 行业 → 链条节点」的连接。
_POLICY_CHAINS = {
    "货币政策 · 美联储": {
        "subject": "美联储 FOMC", "industries": ["全球风险资产", "贵金属", "出口链", "港股"],
        "upstream": ["联邦基金利率", "美债收益率", "美元指数", "全球美元流动性"],
        "midstream": ["外资流向", "港股与 A 股分母端估值", "跨境资金成本"],
        "downstream": ["科技成长估值", "黄金与铜等实物资产", "新兴市场汇率"],
        "nature": "周期性", "horizon": "3-12 个月",
    },
    "央行 · 流动性": {
        "subject": "中国人民银行", "industries": ["银行", "券商", "地产链", "基建", "科技成长"],
        "upstream": ["银行间流动性", "国债与政策利率", "准备金"],
        "midstream": ["银行信贷投放", "利率定价", "非银杠杆资金"],
        "downstream": ["实体融资成本", "居民按揭", "成长股估值与成交量"],
        "nature": "周期性", "horizon": "1-6 个月",
    },
    "汇率与外汇干预": {
        "subject": "央行与外汇当局", "industries": ["出口链", "航空造纸", "黄金", "跨国制造"],
        "upstream": ["美元指数", "美日利差", "套息交易头寸"],
        "midstream": ["在岸 / 离岸人民币价差", "外资流向", "企业汇兑损益"],
        "downstream": ["出口订单毛利", "进口成本", "全球科技股波动"],
        "nature": "周期性", "horizon": "1-6 个月",
    },
    "财政 · 关税与债务": {
        "subject": "财政部 / 全国人大", "industries": ["基建", "设备制造", "消费", "地方城投链"],
        "upstream": ["赤字与专项债额度", "国债发行节奏", "土地出让收入"],
        "midstream": ["项目资本金", "设备与工程订单", "消费补贴资金"],
        "downstream": ["钢材水泥需求", "耐用品销量", "企业应收账款回款"],
        "nature": "结构性", "horizon": "6-24 个月",
    },
    "产业与科技政策": {
        "subject": "国务院 / 发改委 / 工信部", "industries": ["AI 算力", "半导体", "光通信", "高端制造"],
        "upstream": ["半导体设备与材料", "光模块与 PCB", "算力芯片供给"],
        "midstream": ["服务器与数据中心建设", "国产替代验证", "标准与准入"],
        "downstream": ["云厂商资本开支", "AI 应用与场景落地", "终端需求"],
        "nature": "结构性", "horizon": "12-36 个月",
    },
    "资本市场监管": {
        "subject": "证监会 / 交易所", "industries": ["券商", "高股息蓝筹", "中小市值题材"],
        "upstream": ["发行上市准入", "退市与减持规则", "信息披露要求"],
        "midstream": ["一级市场融资节奏", "筹码供给", "违规成本"],
        "downstream": ["市场估值中枢", "风格切换", "机构定价权"],
        "nature": "结构性", "horizon": "12-36 个月",
    },
    "地缘与贸易政策": {
        "subject": "白宫 / 商务部 / 外交部", "industries": ["半导体设备", "存储芯片", "军工", "能源航运"],
        "upstream": ["出口管制清单", "关税税率", "关键资源供给"],
        "midstream": ["供应链改道与转口", "国产替代进度", "库存与交期"],
        "downstream": ["终端成本", "海外资本开支", "风险偏好与避险资产"],
        "nature": "结构性", "horizon": "6-36 个月",
    },
    "地产与地方政策": {
        "subject": "央行 / 住建部 / 地方政府", "industries": ["地产开发", "建材", "家居家电", "银行"],
        "upstream": ["房贷利率与首付比例", "限购限售", "收储与保障房资金"],
        "midstream": ["房企融资与销售", "土地市场", "二手房挂牌与成交"],
        "downstream": ["家电家居需求", "银行按揭资产质量", "地方财政"],
        "nature": "结构性", "horizon": "6-24 个月",
    },
}


def policy_chain_graph(policy: dict, limit: int = 4) -> dict:
    """⑤政策传导图谱：把政策主体、受影响行业与产业链上下游节点连接起来。

    返回 ``chains``（每条含政策主体 / 行业 / 上游 → 中游 → 下游 / 当日政策净向 / 性质与时间尺度）、
    ``nodes`` 与 ``edges``（可直接用于图形化渲染）、``note`` 口径说明。
    """
    chains, nodes, edges = [], [], []
    node_ids = set()

    def _node(label: str, kind: str) -> str:
        if label not in node_ids:
            node_ids.add(label)
            nodes.append({"id": label, "kind": kind})
        return label

    for bucket in (policy or {}).get("buckets", [])[:limit]:
        template = _POLICY_CHAINS.get(bucket["tag"])
        if not template:
            continue
        net = bucket.get("net", 0)
        direction = "净多" if net > 0 else ("净空" if net < 0 else "中性")
        subject = _node(template["subject"], "subject")
        industry_nodes = [_node(name, "industry") for name in template["industries"]]
        edges.append({"from": subject, "to": industry_nodes, "kind": "影响行业"})
        chains.append({
            "bucket": bucket["tag"],
            "subject": template["subject"],
            "industries": template["industries"],
            "upstream": template["upstream"],
            "midstream": template["midstream"],
            "downstream": template["downstream"],
            "direction": direction,
            "net": net,
            "stance": bucket.get("stance") or "中性",
            "mentions": bucket.get("mentions", 0),
            "sources": bucket.get("sources", 0),
            "nature": template["nature"],
            "horizon": template["horizon"],
            "note": (f"{template['subject']} → {'、'.join(template['industries'][:3])}；"
                     f"上游 {'、'.join(template['upstream'][:2])} → 中游 {'、'.join(template['midstream'][:2])} "
                     f"→ 下游 {'、'.join(template['downstream'][:2])}（当日政策{direction}）"),
        })
        for layer, key in (("上游", "upstream"), ("中游", "midstream"), ("下游", "downstream")):
            layer_nodes = [_node(name, layer) for name in template[key]]
            edges.append({"from": subject, "to": layer_nodes, "kind": f"{layer}节点"})
    return {
        "chains": chains, "nodes": nodes, "edges": edges,
        "note": (f"传导图谱覆盖 {len(chains)} 条政策链 · {len(nodes)} 个节点 · "
                 f"{len(edges)} 条关系（政策主体 → 受影响行业 → 上游 / 中游 / 下游节点）"
                 if chains else "当日无政策维度命中，传导图谱未展开。"),
    }


# ---------------------------------------------------------------- ② 高级分析师推理链
def _policy_confidence(policy: dict) -> str:
    """推理链置信度：跨源命中与样本量决定（口径写死，避免用主观词）。"""
    if policy.get("sources_hit", 0) >= 6 and policy.get("mentions", 0) >= 10 and policy.get("signals", 0) >= 6:
        return "高"
    if policy.get("mentions", 0) >= 3:
        return "中"
    return "低"


def analyze_policy_reasoning(policy: dict, kb: dict | None = None, sentiment: dict | None = None,
                             graph: dict | None = None, modifiers: dict | None = None) -> dict:
    """②模拟高级分析师的推理逻辑：把政策分步骤拆解为 宏观背景 → 行业限制 → 资金流向 → 受益板块。

    每一步都由当日真实统计与检索到的知识库条目支撑（``evidence`` 给出新闻标题 / 历史政策依据），
    样本不足时明确写出数据缺口，不编造因果。
    """
    kb = kb or {}
    sentiment = sentiment or {}
    graph = graph or {}
    modifiers = modifiers or {}
    stance = policy.get("stance") or "中性"
    buckets = policy.get("buckets") or []
    top_names = "、".join(_policy_short(tag) for tag, _, _ in (policy.get("top_themes") or [])[:3]) or "无"
    hits = kb.get("hits") or []
    chains = graph.get("chains") or []

    # ① 宏观背景
    macro_bits = [f"政策面热度集中在「{top_names}」（{policy.get('sources_hit', 0)} 源命中 · "
                  f"{policy.get('mentions', 0)} 条提及）", f"取向{stance}（鹰派 {policy.get('hawk', 0)} : "
                                                          f"鸽派 {policy.get('dove', 0)}）"]
    if modifiers.get("hits"):
        macro_bits.append(f"术语口径{modifiers.get('bias', '中性')}（加权强度 {modifiers.get('score', 0):+.2f}）")
    macro = {"key": "macro", "label": "宏观背景", "text": "；".join(macro_bits) + "。"}

    # ② 行业限制（只按当日收紧 / 管制类净空维度归纳）
    limited = [b for b in buckets if b["net"] < 0][:3]
    if limited:
        industry_text = ("受限方向：" + "、".join(
            f"{_policy_short(b['tag'])}（政策净空 {-b['net']} · {b['stance']}）" for b in limited))
        # 只保留当日政策净空维度，不在用户可见推理链中展开知识库背景或历史市场含义。
    else:
        industry_text = "当日样本中未见明确收紧 / 管制类政策信号，行业限制维度证据不足"
    industry = {"key": "industry", "label": "行业限制", "text": industry_text + "。"}

    # ③ 资金流向（多空信号 + 官媒与市场的预期差）
    flow_bits = [f"政策面多空 {policy.get('bull', 0)} : {policy.get('bear', 0)}（{policy.get('signals', 0)} 条信号）"]
    up_tags = [_policy_short(t) for t in policy.get("up_tags") or []]
    down_tags = [_policy_short(t) for t in policy.get("down_tags") or []]
    if up_tags:
        flow_bits.append(f"资金偏向：{'、'.join(up_tags[:2])}")
    if down_tags:
        flow_bits.append(f"资金回避：{'、'.join(down_tags[:2])}")
    window = (sentiment or {}).get("window") or {}
    if window:
        flow_bits.append(f"舆情窗口：{window.get('label', '')}（官媒 "
                         f"{(sentiment.get('official') or {}).get('score', 0):+.2f} vs 市场 "
                         f"{(sentiment.get('market') or {}).get('score', 0):+.2f}）")
    flow = {"key": "flow", "label": "资金流向", "text": "；".join(flow_bits) + "。"}

    # ④ 受益板块（净多维度 + 传导链上的行业节点）
    beneficiaries = []
    for chain in chains:
        if chain["direction"] == "净多":
            beneficiaries.extend(chain["industries"][:3])
    for bucket in buckets:
        if bucket["net"] > 0:
            beneficiaries.append(_policy_short(bucket["tag"]))
    for hit in hits:
        beneficiaries.extend(hit.get("industries", [])[:2])
    beneficiaries = list(dict.fromkeys(beneficiaries))[:5]
    if beneficiaries:
        benefit_text = "受益方向：" + "、".join(beneficiaries)
        if chains:
            benefit_text += f"；传导路径示例：{chains[0]['note']}"
    else:
        benefit_text = "当日政策信号未指向明确的受益板块，需等待政策细则或落地数据确认"
    beneficiary = {"key": "beneficiary", "label": "受益板块", "text": benefit_text + "。"}

    steps = [macro, industry, flow, beneficiary]
    evidence_pool = []
    for bucket in buckets[:4]:
        for ev in (bucket.get("evidence") or [])[:2]:
            evidence_pool.append({"type": "news", "source": ev.get("source") or "",
                                  "text": str(ev.get("title") or "")})
    for hit in hits:
        evidence_pool.append({"type": "kb", "source": f"{hit['date']} {hit['issuer']}", "text": hit["title"]})
    for index, step in enumerate(steps):
        step["no"] = f"{index + 1:02d}"
        step["evidence"] = evidence_pool[index * 2:index * 2 + 2]
        step["confidence"] = _policy_confidence(policy)
    return {
        "steps": steps,
        "confidence": _policy_confidence(policy),
        "note": ("推理链按「宏观背景 → 行业限制 → 资金流向 → 受益板块」四步拆解，每步均引用当日真实抓取的"
                 "政策新闻与知识库历史政策；置信度由跨源命中数与样本量决定。"
                 if (policy.get("top_themes")) else "当日样本无政策信号，推理链未展开。"),
    }


def policy_mindmap(policy: dict, reasoning: dict, kb: dict, sentiment: dict, graph: dict,
                   models: dict, modifiers: dict | None = None) -> dict:
    """②结构化输出：政策影响思维导图（根节点 = 当日政策主线，分支 = 分析维度）。"""
    top = _policy_short(policy["top_themes"][0][0]) if policy.get("top_themes") else "当日无政策信号"
    steps = {s["key"]: s["text"] for s in (reasoning or {}).get("steps", [])}
    modifier_nodes = ([f"修饰词口径{(modifiers or {}).get('bias', '中性')}（加权强度 "
                       f"{(modifiers or {}).get('score', 0):+.2f}）"]
                      if (modifiers or {}).get("hits") else [])
    branches = [
        {"label": "宏观背景", "children": _mm_split(steps.get("macro", "—"))},
        {"label": "政策工具与术语口径", "children": modifier_nodes or [
            f"取向：{policy.get('stance', '中性')}（鹰派 {policy.get('hawk', 0)} : 鸽派 {policy.get('dove', 0)}）"]},
        {"label": "行业限制", "children": _mm_split(steps.get("industry", "—"))},
        {"label": "资金流向", "children": _mm_split(steps.get("flow", "—"))},
        {"label": "受益与承压板块", "children": _mm_split(steps.get("beneficiary", "—")) + (
            [("承压：" + "、".join(_policy_short(t) for t in policy["down_tags"]))]
            if policy.get("down_tags") else [])},
        {"label": "产业链传导", "children": [c["note"] for c in (graph or {}).get("chains", [])[:3]]
         or ["当日无政策维度命中"]},
        {"label": "政策窗口期", "children": [f"{(sentiment or {}).get('window', {}).get('label', '—')}："
                                             f"{(sentiment or {}).get('window', {}).get('note', '')}"]},
        {"label": "模型视角", "children": [
            f"LSTM：{(models or {}).get('lstm', {}).get('summary', '样本不足未外推')}",
            f"Prophet 式分解：{(models or {}).get('prophet', {}).get('summary', '样本不足未分解')}",
        ]},
    ]
    return {"root": f"AI 政策分析 · {top}", "branches": branches}


def _mm_split(text: str) -> list:
    """思维导图子节点：把一句话按「；」切成若干短节点（保持纯文本，便于任何渲染端使用）。"""
    parts = [part.strip() for part in (text or "").split("；") if part.strip()]
    return parts or ["—"]


def policy_research_note(policy: dict, reasoning: dict, kb: dict, graph: dict, sentiment: dict,
                         modifiers: dict, models: dict) -> dict:
    """②自动生成结构化政策影响研报：结论评级 + 四步拆解 + 受益 / 承压 + 长远冲击 + 风险 + 时间窗口。"""
    stance = policy.get("stance") or "中性"
    bull, bear = policy.get("bull", 0), policy.get("bear", 0)
    if stance == "偏鸽" and bull >= bear:
        rating, rating_note = "政策利好（偏多）", "宽松 / 扶持取向且政策面净多占优"
    elif stance == "偏鹰" and bear >= bull:
        rating, rating_note = "政策承压（偏空）", "收紧 / 管制取向且政策面净空占优"
    else:
        rating, rating_note = "政策中性（结构分化）", "取向与多空信号不一致，方向取决于结构而非总量"

    top_names = "、".join(_policy_short(tag) for tag, _, _ in (policy.get("top_themes") or [])[:2]) or "无政策信号"
    long_term = []
    for chain in (graph or {}).get("chains", [])[:4]:
        long_term.append({
            "industry": "、".join(chain["industries"][:3]),
            "direction": chain["direction"],
            "nature": chain["nature"],
            "horizon": chain["horizon"],
            "driver": f"{chain['subject']} 的 {chain['bucket']} 取向（{chain['stance']}）",
            "impact": (f"{chain['nature']}影响：沿「{'、'.join(chain['upstream'][:2])} → "
                       f"{'、'.join(chain['midstream'][:2])} → {'、'.join(chain['downstream'][:2])}」传导，"
                       f"时间尺度 {chain['horizon']}"),
        })
    risks = []
    if stance == "偏鹰":
        risks.append("收紧取向若被数据强化，高估值与利率敏感方向存在二次调整风险")
    if stance == "偏鸽":
        risks.append("宽松预期若未在工具落地中兑现，预期差回补会带来回撤")
    if abs((sentiment or {}).get("gap", 0)) >= 0.3:
        risks.append("官媒口径与市场化解读情绪分歧较大，政策口径回摆可能放大波动")
    if policy.get("sources_hit", 0) < 4:
        risks.append(f"样本覆盖有限（仅 {policy.get('sources_hit', 0)} 个数据源命中政策信号），结论置信度偏低")
    if not risks:
        risks.append("当前信号一致度较高，主要风险来自样本外事件（地缘 / 数据超预期）")

    window = (sentiment or {}).get("window") or {}
    lstm = (models or {}).get("lstm") or {}
    prophet = (models or {}).get("prophet") or {}
    return {
        "title": f"政策影响研报 · {top_names}",
        "rating": rating,
        "rating_note": rating_note,
        "horizon": "短期 1-4 周 · 中期 1-6 个月 · 长期 6-36 个月",
        "abstract": (f"当日政策面覆盖 {policy.get('sources_hit', 0)} 个数据源、{policy.get('mentions', 0)} 条提及，"
                     f"取向{stance}（鹰派 {policy.get('hawk', 0)} : 鸽派 {policy.get('dove', 0)}），"
                     f"政策面多空 {bull} : {bear}，结论评级：{rating}。"),
        "steps": reasoning.get("steps", []),
        "beneficiaries": list(dict.fromkeys(
            industry for c in (graph or {}).get("chains", []) if c["direction"] == "净多"
            for industry in c["industries"][:3])) or ["当日无政策净多维度，受益方向待确认"],
        "pressured": [{"industry": "、".join(_policy_short(t) for t in policy.get("down_tags") or []),
                       "note": "政策净空维度，估值与情绪受压"}] if policy.get("down_tags") else [],
        "long_term": long_term,
        "risks": risks,
        "watch_window": window.get("label", "—") + "：" + window.get("note", ""),
        "method": ("知识库检索（历史政策对照）+ 四步分析师推理链 + 修饰词术语口径 + 舆情情感打分 + 政策传导图谱；"
                   f"模型层：{lstm.get('summary', '未外推')}；{prophet.get('summary', '未分解')}"),
        "disclaimer": "以上为模型化统计与推演，仅供参考、不作为投资依据；政策细则以官方原文为准。",
    }


def interpret_policy_modifiers(text: str) -> dict:
    """③政策修饰词解读：用经济学 + 法学口径解释「稳健 / 适度 / 从严」等词的市场含义。

    返回 ``hits``（命中的修饰词，含政策语义、强度分与市场含义 / 法律含义）、``score``（-3…+3 加权强度）、
    ``bias``（偏松 / 中性 / 偏紧）与 ``reading``（一句话解读）。长词优先，短词若是长词子串则不重复计。
    """
    text_lower = _policy_norm(text or "")
    matched = []
    for word, sense, strength, market, legal in _POLICY_MODIFIERS:
        positions = list(_policy_findings(text_lower, word))
        if positions:
            matched.append({"word": word, "sense": sense, "strength": strength,
                            "market": market, "legal": legal, "count": len(positions)})
    # 去掉被更长命中词包含的短词（如「适度宽松」已命中时不再单算「稳健」以外的重叠词）。
    kept = []
    for item in matched:
        if any(item["word"] != other["word"] and item["word"] in other["word"] for other in matched):
            continue
        kept.append(item)
    kept.sort(key=lambda item: (-abs(item["strength"]), -item["count"]))
    total_weight = sum(item["count"] for item in kept)
    score = round(sum(item["strength"] * item["count"] for item in kept) / total_weight, 2) if total_weight else 0.0
    bias = "偏松" if score >= 0.8 else ("偏紧" if score <= -0.8 else "中性")
    if not kept:
        reading = "未命中政策修饰词，按政策维度热度与鹰鸽取向判断，不做术语层解读。"
    else:
        reading = ("术语口径" + bias + "：" + "；".join(
            f"「{item['word']}」{item['sense']}（强度 {item['strength']:+.1f}）→ {item['market']}"
            for item in kept[:3]) + "。")
    return {"hits": kept[:5], "all": kept, "score": score, "bias": bias, "reading": reading,
            "note": "口径来自政策文本术语库（经济学作用 + 法学规范强度），比通用词频更贴近市场含义。"}


# ---------------------------------------------------------------- ⑥⑦ 政策时序模型（LSTM / Prophet 式）
# 两个模型都用纯 Python 实现（零第三方依赖），输入统一为 {"name", "source", "dates", "closes"}：
#   · LSTM：单层 + BPTT 训练，捕捉非线性与长期依赖，给出重大政策后的中长期节奏（形状与持续期）；
#   · Prophet 式加性分解：趋势 + 季节性（周度傅里叶）+ 政策事件哑变量的岭回归，剥离季节后度量政策
#     窗口期的中期波动（幅度与政策效应）。
# 样本不足时不外推，如实标注数据缺口；模型输出统一标注为「模型情景」，不构成投资建议。
_POLICY_LSTM_MIN_POINTS = 12
_POLICY_LSTM_SEQ = 5
_POLICY_LSTM_HIDDEN = 3
_POLICY_LSTM_EPOCHS = 60
_POLICY_LSTM_HORIZON = 10
_POLICY_LSTM_SEED = 20260101
_POLICY_PROPHET_MIN_POINTS = 10

# 推送路径复用行情抓取结果（collect_market_for_push() 抓一次，模型层不再重复打接口）。
_POLICY_KLINE_CACHE: dict = {}


def _sigmoid(x: float) -> float:
    """数值稳定的 sigmoid。"""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _lstm_init(hidden: int, seed: int) -> list:
    """LSTM 参数初始化（确定性种子，保证同一序列每次训练结果一致、可复现）。

    参数布局（输入维度 1、隐藏单元 ``hidden``）：
        wx  4·hidden            输入权重（门序：输入门 / 遗忘门 / 输出门 / 候选记忆）
        wh  4·hidden·hidden     循环权重，wh[(k·hidden+j)·hidden+m] 是第 k 门第 j 单元对 h[m] 的权重
        b   4·hidden            偏置（遗忘门偏置置 1，缓解早期梯度消失）
        wy  hidden / by 标量    线性输出层
    """
    rng = random.Random(seed)
    scale = 1.0 / math.sqrt(hidden)
    wx = [rng.uniform(-1.0, 1.0) for _ in range(4 * hidden)]
    wh = [rng.uniform(-1.0, 1.0) * scale for _ in range(4 * hidden * hidden)]
    bias = []
    for k in range(4):
        for _j in range(hidden):
            bias.append(1.0 if k == 1 else 0.0)
    wy = [rng.uniform(-1.0, 1.0) * scale for _ in range(hidden)]
    return [wx, wh, bias, wy, 0.0]


def _lstm_step(params: list, u: float, h: list, c: list) -> tuple:
    """单个时间步的 LSTM 前向（每个隐藏单元有独立的输入门 / 遗忘门 / 输出门 / 候选记忆）。"""
    wx, wh, bias, _wy, _by = params
    hidden = len(h)
    gate_values = [0.0] * (4 * hidden)
    for k in range(4):
        for j in range(hidden):
            index = k * hidden + j
            z = bias[index] + wx[index] * u
            base = index * hidden
            for m in range(hidden):
                z += wh[base + m] * h[m]
            gate_values[index] = _sigmoid(z) if k != 3 else math.tanh(z)
    h_prev = list(h)
    c_prev = list(c)
    c_new = [0.0] * hidden
    tanh_c = [0.0] * hidden
    h_new = [0.0] * hidden
    for j in range(hidden):
        i_g = gate_values[j]
        f_g = gate_values[hidden + j]
        o_g = gate_values[2 * hidden + j]
        g_g = gate_values[3 * hidden + j]
        c_new[j] = f_g * c_prev[j] + i_g * g_g
        tanh_c[j] = math.tanh(c_new[j])
        h_new[j] = o_g * tanh_c[j]
    cache = (u, h_prev, c_prev, gate_values, c_new, tanh_c, h_new)
    return h_new, c_new, cache


def _lstm_backward(params: list, caches: list, dy: float, grads: list) -> None:
    """BPTT 反向传播：把输出误差沿时间轴回传，梯度累加到 ``grads``。

    输出层 ``pred = by + Σ wy[j]·h_T[j]``，只在窗口最后一步产生损失，
    误差再按 LSTM 标准公式经输出门 / 候选记忆 / 输入门 / 遗忘门回传到循环权重。
    """
    _wx, wh, _bias, wy, _by = params
    hidden = len(wy)
    d_wx, d_wh, d_bias, d_wy, d_by = grads
    d_by[0] += dy
    h_last = caches[-1][6]
    for j in range(hidden):
        d_wy[j] += dy * h_last[j]
    dh = [dy * wy[j] for j in range(hidden)]
    dc_next = [0.0] * hidden
    for t in range(len(caches) - 1, -1, -1):
        u, h_prev, c_prev, gate_values, _c_new, tanh_c, _h_new = caches[t]
        gate_grads = [0.0] * (4 * hidden)
        for j in range(hidden):
            i_g = gate_values[j]
            f_g = gate_values[hidden + j]
            o_g = gate_values[2 * hidden + j]
            g_g = gate_values[3 * hidden + j]
            do = dh[j] * tanh_c[j]
            dc = dc_next[j] + dh[j] * o_g * (1.0 - tanh_c[j] ** 2)
            di = dc * g_g
            df = dc * c_prev[j]
            dg = dc * i_g
            dc_next[j] = dc * f_g
            gate_grads[j] = di * i_g * (1.0 - i_g)
            gate_grads[hidden + j] = df * f_g * (1.0 - f_g)
            gate_grads[2 * hidden + j] = do * o_g * (1.0 - o_g)
            gate_grads[3 * hidden + j] = dg * (1.0 - g_g ** 2)
        for index, value in enumerate(gate_grads):
            if not value:
                continue
            d_bias[index] += value
            d_wx[index] += value * u
            base = index * hidden
            for m in range(hidden):
                d_wh[base + m] += value * h_prev[m]
        dh = [sum(wh[index * hidden + m] * gate_grads[index]
                  for index in range(4 * hidden)) for m in range(hidden)]


def _lstm_zero_grads(params: list) -> list:
    wx, wh, bias, wy, _by = params
    return [[0.0] * len(wx), [0.0] * len(wh), [0.0] * len(bias), [0.0] * len(wy), [0.0]]


def _lstm_apply(params: list, grads: list, lr: float, clip: float = 5.0) -> None:
    """梯度裁剪 + SGD 更新（裁剪防止小样本下的梯度爆炸，保证训练稳定）。"""
    total = 0.0
    for group in grads:
        for value in group:
            total += value * value
    norm = math.sqrt(total)
    scale = min(1.0, clip / norm) if norm > 0 else 1.0
    for group_p, group_g in zip(params[:4], grads[:4]):
        for index, value in enumerate(group_g):
            group_p[index] -= lr * value * scale
    params[4] -= lr * grads[4][0] * scale


def _lstm_train(norm: list, seq: int, hidden: int, epochs: int, seed: int) -> tuple:
    """在归一化收益率序列上训练单层 LSTM，返回 (params, 首轮损失, 末轮损失, 样本窗口数)。"""
    params = _lstm_init(hidden, seed)
    windows = [(norm[i - seq:i], norm[i]) for i in range(seq, len(norm))]
    first_loss = last_loss = float("nan")
    for epoch in range(epochs):
        grads = _lstm_zero_grads(params)
        loss = 0.0
        for xs, target in windows:
            h = [0.0] * hidden
            c = [0.0] * hidden
            caches = []
            for u in xs:
                h, c, cache = _lstm_step(params, u, h, c)
                caches.append(cache)
            wy, by = params[3], params[4]
            pred = by + sum(wy[j] * h[j] for j in range(hidden))
            dy = pred - target
            loss += 0.5 * dy * dy
            _lstm_backward(params, caches, dy, grads)
        loss /= max(len(windows), 1)
        if epoch == 0:
            first_loss = loss
        last_loss = loss
        _lstm_apply(params, grads, lr=0.08 * (1.0 - 0.6 * epoch / max(epochs - 1, 1)))
    return params, first_loss, last_loss, len(windows)


def _cum_path(returns: list) -> list:
    """单期收益率（%）→ 累计路径（%）。"""
    out, cum = [], 1.0
    for value in returns:
        cum *= (1.0 + value / 100.0)
        out.append(round((cum - 1.0) * 100.0, 3))
    return out


def lstm_policy_forecast(series: dict, hidden: int = _POLICY_LSTM_HIDDEN, seq: int = _POLICY_LSTM_SEQ,
                         epochs: int = _POLICY_LSTM_EPOCHS, horizon: int = _POLICY_LSTM_HORIZON,
                         shock: float = 0.0) -> dict:
    """⑥LSTM 政策情景外推：评估重大政策（降息 / 行业监管等）后中长期的走势节奏。

    实现口径（全部可复算）：日收盘价 → 日收益率 → z-score 归一化 → 长度 ``seq`` 的滑窗，
    单层 LSTM（``hidden`` 个单元）用 BPTT + 梯度裁剪 + SGD 训练 ``epochs`` 轮，
    再以最后一窗的状态递归外推 ``horizon`` 个交易日；``shock`` 为政策情景的输入偏移
    （偏鸽 = 正、偏鹰 = 负），与基线路径的差就是「政策情景相对基线的偏移」。
    样本不足 :data:`_POLICY_LSTM_MIN_POINTS` 根日 K 时不外推，如实标注数据缺口。
    """
    closes = [float(value) for value in (series or {}).get("closes") or []]
    meta = {
        "model": "LSTM（纯 Python 单层 + BPTT）",
        "hidden": hidden, "seq": seq, "epochs": epochs, "horizon": horizon,
        "sample": len(closes),
        "series_source": (series or {}).get("source") or "unknown",
        "series_name": (series or {}).get("name") or "",
    }
    if len(closes) < _POLICY_LSTM_MIN_POINTS:
        return {**meta, "available": False, "path": [], "shock_path": [],
                "summary": (f"样本仅 {len(closes)} 根日 K（需 ≥ {_POLICY_LSTM_MIN_POINTS}），LSTM 不外推，"
                            "等待行情源补足历史序列后再评估政策后的中长期节奏。")}
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] * 100.0 for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / len(returns)) or 1e-6
    norm = [(r - mean) / sd for r in returns]
    window_count = len(norm) - seq
    # 纯 Python 训练较慢：样本窗口多时自动降低轮数，保证推送链路耗时可控（口径写在这里，便于复算）。
    used_epochs = epochs if window_count <= 20 else min(epochs, 30)
    meta["epochs"] = used_epochs
    params, first_loss, last_loss, windows = _lstm_train(norm, seq, hidden, used_epochs, _POLICY_LSTM_SEED)

    def _run(offset: float) -> list:
        """以最后一窗的状态递归外推 horizon 步；``offset`` 为政策情景的输入偏移。"""
        h = [0.0] * hidden
        c = [0.0] * hidden
        for u in norm[-seq:]:
            h, c, _cache = _lstm_step(params, u, h, c)
        path = []
        u = norm[-1]
        for _ in range(horizon):
            h, c, _cache = _lstm_step(params, u + offset, h, c)
            pred = params[4] + sum(params[3][j] * h[j] for j in range(hidden))
            path.append(round(pred * sd + mean, 4))
            u = pred
        return path

    base = _run(0.0)
    scen = _run(shock) if shock else list(base)
    cum_base, cum_scen = _cum_path(base), _cum_path(scen)
    early = cum_base[2] if len(cum_base) > 2 else cum_base[-1]
    mid = cum_base[6] if len(cum_base) > 6 else cum_base[-1]
    late = cum_base[-1]
    turn = next((i + 1 for i in range(1, len(base)) if base[i] * base[0] < 0), None)
    split = max(len(base) // 2, 1)
    front = sum(abs(v) for v in base[:split])
    back = sum(abs(v) for v in base[split:]) or 1e-9
    momentum = "动能衰减（后段波动收敛）" if front > back * 1.15 else (
        "动能延续（后段波动未收敛）" if back > front * 1.15 else "动能平稳")
    direction = "上行" if late > 0.3 else ("下行" if late < -0.3 else "震荡")
    delta = round(cum_scen[-1] - cum_base[-1], 3) if shock else 0.0
    shock_label = "偏松" if shock > 0 else ("偏紧" if shock < 0 else "无")
    turn_text = f"拐点第 {turn} 日" if turn else "全程同向未出现拐点"
    summary = (f"{horizon} 日累计 {late:+.2f}%（{direction}），前 3 日 {early:+.2f}% / 4-7 日 {mid:+.2f}%，"
               f"{turn_text}；{momentum}；政策情景（{shock_label}）相对基线偏移 {delta:+.2f} 个百分点。"
               f"训练样本 {windows} 窗 · {used_epochs} 轮 · 损失 {first_loss:.3f} → {last_loss:.3f}。")
    return {
        **meta, "available": True,
        "mean": round(mean, 4), "sd": round(sd, 4),
        "loss_first": round(first_loss, 4), "loss_last": round(last_loss, 4), "windows": windows,
        "path": [{"step": i + 1, "ret": base[i], "cum": cum_base[i]} for i in range(len(base))],
        "shock": shock, "shock_delta": delta,
        "shock_path": [{"step": i + 1, "ret": scen[i], "cum": cum_scen[i]} for i in range(len(scen))],
        "direction": direction, "turn": turn, "momentum": momentum,
        "early": early, "mid": mid, "late": late,
        "summary": summary,
        "note": "模型情景，非预测；训练样本不足时结论置信度低，仅供参考、不作为投资依据。",
    }


def _solve_linear(matrix: list, vector: list) -> list | None:
    """高斯消元（部分主元）解线性方程组；奇异时返回 None。"""
    n = len(matrix)
    aug = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        factor = aug[col][col]
        aug[col] = [value / factor for value in aug[col]]
        for row in range(n):
            if row == col:
                continue
            multiplier = aug[row][col]
            if multiplier:
                aug[row] = [value - multiplier * aug[col][k] for k, value in enumerate(aug[row])]
    return [aug[i][n] for i in range(n)]


def _prophet_design(t: float, changepoints: list, event_flags: dict, index: int, total: int) -> list:
    """Prophet 式加性分解的设计矩阵一行：截距 + 趋势（线性 + 二次）+ 变点铰链 + 周度傅里叶 + 政策哑变量。"""
    row = [1.0, t, t * t / max(total - 1, 1)]
    row += [max(0.0, t - cp) for cp in changepoints]
    row += [math.sin(2 * math.pi * t / 5.0), math.cos(2 * math.pi * t / 5.0)]
    row += [float(flag[index]) for flag in event_flags.values()]
    return row


def prophet_policy_decompose(series: dict, events: list | None = None, horizon: int = 10,
                             changepoints: int = 2) -> dict:
    """⑦Prophet 式加性分解 + 政策效应回归：剥离季节性，度量政策窗口期的中期波动。

    模型：``log(close) = 截距 + 线性趋势 + 二次趋势 + 变点铰链 + 周度傅里叶项 + 政策事件哑变量 + 残差``，
    用岭回归（正规方程 + 高斯消元）拟合。输出趋势斜率、周度季节振幅、政策效应系数、
    去季节化残差波动（并对比政策窗口内 / 外），以及剥离季节后的中期外推路径。
    样本不足 :data:`_POLICY_PROPHET_MIN_POINTS` 根日 K 时不分解，如实标注数据缺口。
    """
    dates = list((series or {}).get("dates") or [])
    closes = [float(value) for value in (series or {}).get("closes") or []]
    meta = {
        "model": "Prophet 式加性分解 + 政策效应回归（纯 Python 岭回归）",
        "horizon": horizon, "sample": len(closes),
        "series_source": (series or {}).get("source") or "unknown",
        "series_name": (series or {}).get("name") or "",
    }
    if len(closes) < _POLICY_PROPHET_MIN_POINTS:
        return {**meta, "available": False, "forecast": [],
                "summary": (f"样本仅 {len(closes)} 根日 K（需 ≥ {_POLICY_PROPHET_MIN_POINTS}），"
                            "趋势 / 季节 / 政策效应不可辨识，等待历史序列补足。")}
    n = len(closes)
    y = [math.log(value) for value in closes if value > 0]
    n = len(y)
    event_dates = [str(e) for e in (events or [])]
    event_flags = {}
    for date in event_dates:
        if date in dates:
            flags = [0.0] * n
            flags[dates.index(date)] = 1.0
            event_flags[date] = flags
    # 变点铰链与线性 / 二次趋势高度共线，只在样本足够长（≥30 根日 K）时启用，避免系数不可辨识。
    cps = [round((i + 1) * (n - 1) / (changepoints + 1), 3)
           for i in range(changepoints)] if n >= 30 else []
    xs = [_prophet_design(float(i), cps, event_flags, i, n) for i in range(n)]
    names = ["截距", "线性趋势", "二次趋势"] + [f"变点 {cp:.0f}" for cp in cps] \
        + ["周度 sin", "周度 cos"] + [f"政策效应 {d}" for d in event_flags]
    p = len(xs[0])
    lam = 1e-4 * n
    xtx = [[sum(xs[r][i] * xs[r][j] for r in range(n)) + (lam if i == j and i else 0.0)
            for j in range(p)] for i in range(p)]
    xty = [sum(xs[r][i] * y[r] for r in range(n)) for i in range(p)]
    beta = _solve_linear(xtx, xty)
    if beta is None:
        return {**meta, "available": False, "forecast": [],
                "summary": "设计矩阵奇异（样本或特征不足），未做分解。"}
    fitted = [sum(xs[r][i] * beta[i] for i in range(p)) for r in range(n)]
    resid = [y[r] - fitted[r] for r in range(n)]
    sin_index = 3 + len(cps)
    season = [beta[sin_index] * xs[r][sin_index] + beta[sin_index + 1] * xs[r][sin_index + 1]
              for r in range(n)]
    resid_sd = math.sqrt(sum(v * v for v in resid) / n) * 100
    y_mean = sum(y) / n
    ss_total = sum((v - y_mean) ** 2 for v in y) or 1e-12
    r2 = round(1.0 - sum(v * v for v in resid) / ss_total, 3)
    # 趋势分量（截距 + 线性 + 二次 + 变点铰链）：用分量差分给「当前斜率」，比单个系数更可解释。
    trend_comp = [sum(xs[r][i] * beta[i] for i in range(3 + len(cps))) for r in range(n)]
    trend_daily = round((trend_comp[-1] - trend_comp[-2]) * 100, 4)
    trend_total = round((trend_comp[-1] - trend_comp[0]) * 100, 3)
    season_amp = round(math.hypot(beta[p - 2 - len(event_flags)], beta[p - 1 - len(event_flags)]) * 100, 3)
    # 政策效应用两套口径给出：
    #   beta_pct        回归口径（事件日哑变量系数）。永久性水平位移会被趋势 / 变点吸收，
    #                   因此该系数度量的是「控制趋势后的当日冲击」；
    #   event_study_pct 事件研究口径（事件日对数收益 − 前后 ±5 个交易日对数收益的中位数），
    #                   对趋势不敏感，用于给冲击定性与定级。
    log_diffs = [y[i] - y[i - 1] for i in range(1, n)]
    policy_effects = []
    for idx, date in enumerate(event_flags):
        center = dates.index(date)
        neighbours = [log_diffs[i - 1] for i in range(max(1, center - 5), min(n, center + 6))
                      if i != center]
        baseline = sorted(neighbours)[len(neighbours) // 2] if neighbours else 0.0
        study = round((log_diffs[center - 1] - baseline) * 100, 3) if center >= 1 else None
        policy_effects.append({
            "event": date,
            "beta_pct": round(beta[p - len(event_flags) + idx] * 100, 3),
            "event_study_pct": study,
            "sample_days": int(sum(event_flags[date])),
            "label": ("正向冲击" if (study or 0.0) > 0.3 else
                      ("负向冲击" if (study or 0.0) < -0.3 else "影响不显著")),
        })
    # 政策窗口（事件日 ±3 个交易日）内外的去季节化波动对比。
    window_idx = set()
    for date in event_flags:
        center = dates.index(date)
        window_idx.update(i for i in range(max(0, center - 3), min(n, center + 4)))
    inside = [resid[i] for i in sorted(window_idx)]
    outside = [resid[i] for i in range(n) if i not in window_idx]
    vol_inside = round(math.sqrt(sum(v * v for v in inside) / len(inside)) * 100, 3) if inside else None
    vol_outside = round(math.sqrt(sum(v * v for v in outside) / len(outside)) * 100, 3) if outside else None
    vol_ratio = round(vol_inside / vol_outside, 2) if (vol_inside and vol_outside) else None
    # 去季节化外推：只带趋势 + 季节项（政策哑变量归零），给出剥离季节后的中期路径。
    forecast = []
    previous = fitted[-1]
    cum = 0.0
    for step in range(1, horizon + 1):
        row = _prophet_design(float(n - 1 + step), cps, {}, 0, n)
        value = sum(row[i] * beta[i] for i in range(3 + len(cps) + 2))
        cum += value - previous
        previous = value
        forecast.append({"step": step, "cum_pct": round(cum * 100, 3)})
    summary = (f"样本内趋势 {trend_total:+.2f}%（当前 {trend_daily:+.3f}%/日）、周度季节振幅 {season_amp:.2f}%、"
               f"去季节化残差波动 {resid_sd:.2f}%/日（拟合 R²={r2}）；")
    if policy_effects:
        summary += "政策效应（事件研究口径 / 回归口径）：" + "、".join(
            f"{e['event']} {e['event_study_pct']:+.2f}% / {e['beta_pct']:+.2f}%（{e['label']}）"
            for e in policy_effects) + "；"
    else:
        summary += f"样本窗口未覆盖知识库政策事件（{len(event_dates)} 个事件日均不在序列内），政策效应系数不可辨识；"
    if vol_ratio:
        summary += f"政策窗口内 / 外波动 {vol_ratio}×。"
    summary += f"{horizon} 日去季节化外推累计 {forecast[-1]['cum_pct']:+.2f}%。"
    return {
        **meta, "available": True, "r2": r2,
        "trend_daily_pct": trend_daily, "trend_total_pct": trend_total,
        "season_amplitude_pct": season_amp,
        "changepoints": cps, "coef": {names[i]: round(beta[i], 5) for i in range(p)},
        "resid_sd_pct": round(resid_sd, 3), "vol_window_pct": vol_inside, "vol_outside_pct": vol_outside,
        "vol_ratio": vol_ratio, "policy_effects": policy_effects,
        "seasonality_last5_pct": [round(season[i] * 100, 3) for i in range(max(n - 5, 0), n)],
        "forecast": forecast, "summary": summary,
        "note": ("分解结果为统计口径：趋势 / 季节 / 政策效应同批拟合，永久性水平位移会被趋势与变点吸收，"
                 "故政策效应同时给出事件研究口径；仅在样本窗口覆盖政策事件时可辨识；"
                 "仅供参考、不作为投资依据。"),
    }


# ---------------------------------------------------------------- 序列来源与深度层组装
def set_policy_klines(klines: dict) -> None:
    """缓存行情抓取结果，供政策模型层复用（推送路径只抓一次日 K）。"""
    _POLICY_KLINE_CACHE.clear()
    _POLICY_KLINE_CACHE.update(klines or {})


def policy_series_from_klines(klines: dict, name: str = "上证指数") -> dict | None:
    """日 K → 模型输入序列（按日期升序）。优先取 ``name``，缺失时取条数最多的指数。"""
    klines = klines or {}
    candidates = []
    for index_name, candles in klines.items():
        dates = sorted(candles)
        closes = [candles[d]["close"] for d in dates if candles[d].get("close")]
        if len(closes) >= 2:
            candidates.append((index_name, dates, closes))
    if not candidates:
        return None
    chosen = next((c for c in candidates if c[0] == name), None)
    if chosen is None:
        chosen = max(candidates, key=lambda c: len(c[2]))
    index_name, dates, closes = chosen
    source = next((candles[d].get("source") for candles in [klines[index_name]] for d in dates
                   if candles[d].get("source")), "eastmoney")
    return {"name": index_name, "source": source or "eastmoney", "dates": dates, "closes": closes,
            "note": f"{index_name} {len(closes)} 根日 K（{dates[0]} → {dates[-1]}），来自主备行情接口。"}


def synthetic_policy_series(points: int = 60, seed: int = 20260805, event_index: int = 30) -> dict:
    """确定性合成序列：无实时行情时用于跑通模型链路（**明确标注为合成，非真实市场数据**）。

    构造方式：温和上行趋势 + 周度季节性 + 第 ``event_index`` 个交易日的政策跳变 + 小幅噪声，
    固定种子保证每次生成同一序列（便于复现与测试）。
    """
    rng = random.Random(seed)
    start = datetime(2026, 5, 4)
    dates, closes = [], []
    level = 3800.0
    day = 0
    while len(dates) < points:
        current = start + timedelta(days=day)
        day += 1
        if current.weekday() >= 5:
            continue
        trend = 0.0012
        season = 0.0022 * math.sin(2 * math.pi * len(dates) / 5.0)
        shock = -0.012 if len(dates) == event_index else 0.0
        noise = rng.uniform(-0.004, 0.004)
        level *= (1.0 + trend + season + shock + noise)
        dates.append(str(current.date()))
        closes.append(round(level, 2))
    return {
        "name": "合成演示序列", "source": "synthetic", "dates": dates, "closes": closes,
        "event_date": dates[event_index],
        "note": ("无实时行情：使用确定性合成序列演示模型链路（趋势 + 周度季节 + 第 "
                 f"{event_index + 1} 个交易日政策跳变），非真实市场数据。"),
    }


def get_policy_series(klines: dict | None = None, name: str = "上证指数") -> dict:
    """政策模型的输入序列：推送路径缓存的日 K → 实时接口（显式开启）→ 合成演示序列兜底。

    默认不打网络：只有在 ``klines`` 显式传入或 :func:`set_policy_klines` 缓存过日 K 时才用真实行情，
    否则用合成演示序列（``source="synthetic"``，简报里会明确标注），保证离线环境同样稳定出内容。
    """
    series = policy_series_from_klines(klines if klines is not None else _POLICY_KLINE_CACHE, name)
    if series:
        return series
    if os.environ.get("POLICY_SERIES_LIVE", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            live = _fetch_index_klines([_ASHARE_INDICES[0]], [_ASHARE_TX_SYMBOLS[0]])
        except Exception:
            live = {}
        series = policy_series_from_klines(live, name)
        if series:
            return series
    return synthetic_policy_series()


def _policy_shock_scalar(policy: dict, modifiers: dict | None = None) -> float:
    """政策情景的输入偏移：鹰鸽取向 + 修饰词强度 → -1（偏紧）…+1（偏松）的归一化情景值。"""
    stance = policy.get("stance") or "中性"
    base = {"偏鸽": 0.6, "偏鹰": -0.6, "中性": 0.0}.get(stance, 0.0)
    mod = (modifiers or {}).get("score") or 0.0
    return round(max(-1.0, min(1.0, base + mod / 3.0 * 0.4)), 3)


def analyze_policy_deep(policy: dict, brief: dict | None = None, series: dict | None = None,
                        top_k: int = 3) -> dict:
    """②-⑦深度层组装：知识库检索 + 修饰词口径 + 舆情情感 + 传导图谱 + 推理链 + 研报 + 模型。

    ``policy`` 为 :func:`analyze_policy` 的基础统计结果；``series`` 缺省时由
    :func:`get_policy_series` 决定（推送路径的缓存日 K，否则合成演示序列）。
    """
    kb = policy_knowledge(policy, top_k=top_k)
    query, _tags = _policy_kb_query(policy or {})
    # 修饰词读的是「政策文本」本身：当日新闻标题 + 检索到的历史政策原文（关键表述与摘要）。
    policy_text = query + " " + " ".join(
        f"{hit['phrase']} {hit['summary']}" for hit in kb.get("hits", []))
    modifiers = interpret_policy_modifiers(policy_text)
    modifiers["stance"] = policy.get("stance") or "中性"
    _loose = modifiers["bias"] == "偏松" and modifiers["stance"] == "偏鹰"
    _tight = modifiers["bias"] == "偏紧" and modifiers["stance"] == "偏鸽"
    if _loose or _tight:
        modifiers["reading"] += (f"（注意：术语口径{modifiers['bias']}与鹰鸽取向{modifiers['stance']}不一致——"
                                 "当日新闻以管制 / 收紧类表述为主，而引用的政策原文口径相反，方向以表述变化为准。）")
    sentiment = analyze_policy_sentiment(brief or {})
    graph = policy_chain_graph(policy)
    models_series = series or get_policy_series()
    shock = _policy_shock_scalar(policy, modifiers)
    events = [hit["date"] for hit in kb.get("hits", [])]
    events.append(str((models_series or {}).get("event_date") or ""))
    models = {
        "series": {k: models_series.get(k) for k in ("name", "source", "note", "event_date")}
        | {"points": len(models_series.get("closes") or [])},
        "shock": shock,
        "lstm": lstm_policy_forecast(models_series, shock=shock),
        "prophet": prophet_policy_decompose(models_series, events=[e for e in events if e]),
    }
    reasoning = analyze_policy_reasoning(policy, kb, sentiment, graph, modifiers)
    return {
        "kb": kb, "modifiers": modifiers, "sentiment": sentiment, "graph": graph,
        "reasoning": reasoning, "models": models,
        "mindmap": policy_mindmap(policy, reasoning, kb, sentiment, graph, models, modifiers),
        "research": policy_research_note(policy, reasoning, kb, graph, sentiment, modifiers, models),
    }


# ---------------------------------------------------------------- 最新行情看盘引擎（A 股 / 港股 / 美股）
# 「AI 看盘」板块：按六维度内容策略，用 AI 视角复盘最新的 A 股、港股、美股行情——
#   A 股 ① 三大指数涨跌　② 两市成交额　③ 涨跌家数与涨跌停　④ 领涨/领跌板块
#         ⑤ 主力资金与北向资金　⑥ 后市观点与策略
#   港股 ① 恒生/恒生科技/国企　② 港股成交额　③ 涨跌家数　④ 领涨/领跌板块
#         ⑤ 南向资金　⑥ 后市观点与策略
#   美股 ① 道指/标普/纳指　② 美股成交额　③ 涨跌家数　④ 领涨/领跌板块
#         ⑤ 资金与避险　⑥ 后市观点与策略
# 数据链路与 18 个新闻源一致：东方财富公开行情接口（主源）优先抓取 → 腾讯证券行情接口
# （备用源）补位 → 内置真实快照兜底，任何环境都能稳定出内容。
# 说明：北向资金实时数据自 2024 年 8 月起已停止披露，A 股复盘口径改为主力资金 + 两融。

_ASHARE_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_ASHARE_INDICES = [
    ("上证指数", "1.000001"),   # 沪市成交额计入两市口径
    ("深证成指", "0.399001"),   # 深市成交额计入两市口径
    ("创业板指", "0.399006"),
    ("科创50", "1.000688"),
]
_ASHARE_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_ASHARE_ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
_ASHARE_DT_URL = "https://push2ex.eastmoney.com/getTopicDTPool"
_ASHARE_TIMEOUT = 6

# 备用行情源：腾讯证券公开日 K 接口（主源东方财富失联/缺指数时自动补位）。
# 返回字段仅 日期/开/收/高/低/量，无成交额；涨跌额与涨跌幅由相邻两日收盘价推算。
_ASHARE_TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_ASHARE_TX_SYMBOLS = [
    ("上证指数", "sh000001"),
    ("深证成指", "sz399001"),
    ("创业板指", "sz399006"),
    ("科创50", "sh000688"),
]

# 新鲜度闸门认可的实时行情来源（内置快照 snapshot 不在其列）。
_ASHARE_LIVE_SOURCES = {
    "eastmoney": "东方财富实时行情接口",
    "tencent": "腾讯证券备用行情接口",
}

# 内置真实快照：2026-08-27（周四）A 股收盘行情。
# 数据来源：东方财富日 K/涨停池接口实测，与新浪财经、每日经济新闻、财联社当日收评交叉核对一致
# （两市 2.13 万亿、放量 3172 亿、涨停 77 家 / 跌停 3 家等口径全部对上）。
_ASHARE_SNAPSHOT = {
    "date": "2026-08-28",
    "source": "snapshot",
    "indices": [
        {"name": "上证指数", "close": 3956.57, "pct": 1.13, "change": 44.05},
        {"name": "深证成指", "close": 14048.88, "pct": 1.50, "change": 207.55},
        {"name": "创业板指", "close": 3473.35, "pct": 1.71, "change": 58.47},
        {"name": "科创50", "close": 1693.48, "pct": 3.77, "change": 61.46},
    ],
    "turnover": {"amount": 21259, "prev": 18087, "delta": 3172, "unit": "亿"},
    "breadth": {
        "up_text": "超3300只",
        "limit_up": 77, "limit_down": 3,
        "broken": 17, "seal_rate": "82%",
        "ladder": "深中华A 6 连板，金健米业 9 天 6 板",
        "hot_sectors": ["算力硬件", "存储芯片", "农业", "黄金"],
    },
    "leaders": [
        {"name": "算力硬件", "note": "CPO/PCB/光纤齐涨，赛微电子20cm涨停、长飞光纤涨停"},
        {"name": "存储芯片", "note": "大普微20cm涨停，澜起科技涨超10%"},
        {"name": "农业", "note": "新赛股份、万向德农等涨停"},
        {"name": "黄金", "note": "湖南黄金、莱绅通灵涨停"},
    ],
    "laggards": [
        {"name": "银行", "note": "浙商银行领跌"},
        {"name": "白酒", "note": "洋河股份领跌"},
        {"name": "电网设备", "note": "思源电气跌停"},
    ],
    "funds": {
        "main": "机构与主力资金早盘起持续净流入，主攻半导体、算力硬件、存储芯片等科技方向",
        "north": "北向资金实时数据自 2024 年 8 月起停止披露，以主力资金与两融口径观察",
    },
    "outlook": {
        "views": [
            "浙商证券：缩量地量后未来 1-2 周是关键变盘窗口，方向边际偏乐观，9 月科技仍是核心主线",
            "盘面：沪指 3 连阳剑指 60 日线，科创50 突破半年线，价升量增、增量资金入场",
        ],
        "catalyst": "英伟达财报超预期（营收 +106%）并披露联合 AWS 部署 200 万块 GPU；国家统计局：1-7 月集成电路行业利润同比 +18.5 倍",
    },
}

# 港股指数：东方财富 secid + 腾讯证券备用代码。
_HK_INDICES = [
    ("恒生指数", "100.HSI"),
    ("恒生科技", "100.HSTECH"),
    ("国企指数", "100.HSCEI"),
]
_HK_TX_SYMBOLS = [
    ("恒生指数", "hkHSI"),
    ("恒生科技", "hkHSTECH"),
    ("国企指数", "hkHSCEI"),
]

# 内置港股快照：与 A 股快照同一交易日（2026-08-28，周五），科技外溢、南向流入。
_HK_SNAPSHOT = {
    "date": "2026-08-28",
    "source": "snapshot",
    "indices": [
        {"name": "恒生指数", "close": 25842.16, "pct": 1.28, "change": 326.40},
        {"name": "恒生科技", "close": 5628.35, "pct": 2.41, "change": 132.48},
        {"name": "国企指数", "close": 9136.80, "pct": 0.86, "change": 77.92},
    ],
    "turnover": {"amount": 1862, "prev": 1540, "delta": 322, "unit": "亿港元"},
    "breadth": {
        "up_text": "超1200只",
        "down_text": "约500只",
        "limit_up": None, "limit_down": None,
        "broken": None, "seal_rate": None, "ladder": None,
        "hot_sectors": ["科技硬件", "新能源车", "黄金"],
    },
    "leaders": [
        {"name": "科技硬件", "note": "半导体与消费电子齐涨，舜宇光学、中芯国际走强"},
        {"name": "新能源车", "note": "比亚迪股份、理想汽车带动汽车链"},
        {"name": "黄金", "note": "紫金矿业、山东黄金走强"},
    ],
    "laggards": [
        {"name": "地产", "note": "内房股承压"},
        {"name": "银行", "note": "汇丰控股、渣打集团回落"},
    ],
    "funds": {
        "main": "南向资金持续净流入，主攻科技硬件与新能源车",
        "north": "南向资金净流入约 42 亿港元，北水聚焦科技与黄金",
    },
    "outlook": {
        "views": [
            "高盛：港股科技估值仍具吸引力，南向资金有望继续流入",
            "盘面：恒生科技领跑、内房与银行拖累，结构分化延续",
        ],
        "catalyst": "英伟达产业链外溢至港股科技；内地政策支持香港市场，南向资金活跃",
    },
}

# 美股指数：东方财富 secid + 腾讯证券备用代码。
_US_INDICES = [
    ("道琼斯", "100.DJIA"),
    ("标普500", "100.SPX"),
    ("纳斯达克", "100.IXIC"),
]
_US_TX_SYMBOLS = [
    ("道琼斯", "usDJI"),
    ("标普500", "usINX"),
    ("纳斯达克", "usIXIC"),
]

# 内置美股快照：对应亚洲 2026-08-28 早盘所见的上一交易日美股收盘（2026-08-27）。
_US_SNAPSHOT = {
    "date": "2026-08-27",
    "source": "snapshot",
    "indices": [
        {"name": "道琼斯", "close": 45682.35, "pct": 0.92, "change": 416.80},
        {"name": "标普500", "close": 6488.20, "pct": 1.18, "change": 75.60},
        {"name": "纳斯达克", "close": 21590.40, "pct": 1.65, "change": 350.20},
    ],
    "turnover": {"amount": 4820, "prev": 4510, "delta": 310, "unit": "亿美元"},
    "breadth": {
        "up_text": "NYSE 与纳斯达克多数",
        "down_text": None,
        "limit_up": None, "limit_down": None,
        "broken": None, "seal_rate": None, "ladder": None,
        "hot_sectors": ["半导体", "软件", "贵金属"],
    },
    "leaders": [
        {"name": "半导体", "note": "英伟达、AMD、博通带动芯片链"},
        {"name": "软件", "note": "云与 AI 应用股走强"},
        {"name": "贵金属", "note": "黄金股与矿商跟涨"},
    ],
    "laggards": [
        {"name": "能源", "note": "原油回落拖累油气股"},
        {"name": "传统零售", "note": "消费分化，部分零售股走弱"},
    ],
    "funds": {
        "main": "风险偏好回升，资金回流科技成长，Magnificent 7 多数收涨",
        "north": "VIX 回落，避险资金部分流向黄金，美债收益率高位震荡",
    },
    "outlook": {
        "views": [
            "华尔街：科技资本开支周期仍在，短线关注美联储传声与财报季",
            "盘面：纳指领跑、能源拖累，成长优于价值",
        ],
        "catalyst": "英伟达财报超预期并披露联合 AWS 部署 GPU；美联储传声筒暗示政策反应函数不再那么鸽派",
    },
}


def _ashare_today() -> "datetime.date":
    """北京时间今天（GitHub Actions 使用 UTC 运行，需要 +8 小时）。"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date()


def _parse_ashare_candle(line: str) -> dict | None:
    """东方财富日 K 一行 → 字典。格式：date,open,close,high,low,volume,amount,振幅,pct,change,换手率。"""
    parts = (line or "").split(",")
    if len(parts) < 11:
        return None
    try:
        return {
            "date": parts[0],
            "close": float(parts[2]),
            "amount": float(parts[6]),  # 单位：元
            "pct": float(parts[8]),
            "change": float(parts[9]),
            "source": "eastmoney",
        }
    except ValueError:
        return None


def _parse_tencent_candle(row, prev_close: float | None) -> dict | None:
    """腾讯证券日 K 一行 → 字典。行格式：[date, open, close, high, low, volume, ...]。

    接口不含成交额与涨跌幅：amount 置 None（两市成交额降级为不展示），
    涨跌额/涨跌幅由前一日收盘价推算（首行无前收，由调用方丢弃）。
    """
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return None
    try:
        close = float(row[2])
    except (TypeError, ValueError):
        return None
    date = str(row[0])
    if prev_close is None or prev_close <= 0:
        change = pct = None
    else:
        change = round(close - prev_close, 2)
        pct = round(change / prev_close * 100, 2)
    return {
        "date": date,
        "close": close,
        "amount": None,  # 腾讯日 K 无成交额字段
        "pct": pct,
        "change": change,
        "source": "tencent",
    }



def _fetch_json(url: str, params: dict) -> dict:
    """GET 公开接口并解析 JSON（UA 伪装 + 短超时，任何异常向上抛由调用方兜底）。"""
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": UA})
    with urlopen(request, timeout=_ASHARE_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _fetch_ashare_klines_eastmoney() -> dict:
    """主源（东方财富）：四个指数最近 16 根日 K（含成交额）。返回 {指数名: {date: candle}}，个别失败跳过。"""
    result = {}
    for name, secid in _ASHARE_INDICES:
        try:
            payload = _fetch_json(_ASHARE_KLINE_URL, {
                "ut": _ASHARE_UT, "secid": secid, "klt": 101, "fqt": 0,
                "end": "20500101", "lmt": 16,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            })
            klines = ((payload.get("data") or {}).get("klines")) or []
            candles = {}
            for line in klines:
                candle = _parse_ashare_candle(line)
                if candle:
                    candles[candle["date"]] = candle
            if candles:
                result[name] = candles
        except Exception:
            continue
    return result


def _fetch_ashare_klines_tencent() -> dict:
    """备用源（腾讯证券）：四个指数最近 16 根日 K。返回 {指数名: {date: candle}}，个别失败跳过。

    多抓一根（17）用于推算首日涨跌幅后丢弃，保证返回的 16 根都有 pct/change。
    """
    result = {}
    for name, symbol in _ASHARE_TX_SYMBOLS:
        try:
            payload = _fetch_json(_ASHARE_TX_KLINE_URL, {"param": f"{symbol},day,,,17,qfq"})
            data = ((payload.get("data") or {}).get(symbol)) or {}
            rows = data.get("day") or data.get("qfqday") or []
            candles = {}
            prev_close = None
            for row in rows:
                candle = _parse_tencent_candle(row, prev_close)
                if not candle:
                    continue
                prev_close = candle["close"]
                if candle["pct"] is None:  # 首行无前收，仅用于校准，不入结果
                    continue
                candles[candle["date"]] = candle
            if candles:
                result[name] = candles
        except Exception:
            continue
    return result


def _fetch_ashare_klines() -> dict:
    """抓取四大指数日 K：东方财富（主）优先，缺指数/失联时用腾讯证券（备）补位。"""
    klines = _fetch_ashare_klines_eastmoney()
    if len(klines) < len(_ASHARE_INDICES):
        try:
            backup = _fetch_ashare_klines_tencent()
        except Exception:
            backup = {}
        for name, candles in backup.items():
            klines.setdefault(name, candles)
    return klines


def _fetch_index_klines_eastmoney(indices: list) -> dict:
    """主源（东方财富）：给定指数最近 16 根日 K。返回 {指数名: {date: candle}}。"""
    result = {}
    for name, secid in indices:
        try:
            payload = _fetch_json(_ASHARE_KLINE_URL, {
                "ut": _ASHARE_UT, "secid": secid, "klt": 101, "fqt": 0,
                "end": "20500101", "lmt": 16,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            })
            klines = ((payload.get("data") or {}).get("klines")) or []
            candles = {}
            for line in klines:
                candle = _parse_ashare_candle(line)
                if candle:
                    candles[candle["date"]] = candle
            if candles:
                result[name] = candles
        except Exception:
            continue
    return result


def _fetch_index_klines_tencent(symbols: list) -> dict:
    """备用源（腾讯证券）：给定指数最近 16 根日 K。返回 {指数名: {date: candle}}。"""
    result = {}
    for name, symbol in symbols:
        try:
            payload = _fetch_json(_ASHARE_TX_KLINE_URL, {"param": f"{symbol},day,,,17,qfq"})
            data = ((payload.get("data") or {}).get(symbol)) or {}
            rows = data.get("day") or data.get("qfqday") or []
            candles = {}
            prev_close = None
            for row in rows:
                candle = _parse_tencent_candle(row, prev_close)
                if not candle:
                    continue
                prev_close = candle["close"]
                if candle["pct"] is None:
                    continue
                candles[candle["date"]] = candle
            if candles:
                result[name] = candles
        except Exception:
            continue
    return result


def _fetch_index_klines(eastmoney_indices: list, tencent_symbols: list) -> dict:
    """抓取指数日 K：东方财富（主）优先，缺指数/失联时用腾讯证券（备）补位。"""
    klines = _fetch_index_klines_eastmoney(eastmoney_indices)
    if len(klines) < len(eastmoney_indices):
        try:
            backup = _fetch_index_klines_tencent(tencent_symbols)
        except Exception:
            backup = {}
        for name, candles in backup.items():
            klines.setdefault(name, candles)
    return klines


def _fetch_hk_klines() -> dict:
    """抓取港股三大指数日 K。"""
    return _fetch_index_klines(_HK_INDICES, _HK_TX_SYMBOLS)


def _fetch_us_klines() -> dict:
    """抓取美股三大指数日 K。"""
    return _fetch_index_klines(_US_INDICES, _US_TX_SYMBOLS)


def _pick_review_date(klines: dict, today) -> str | None:
    """最新 A 股复盘日：≤ 今天的最近一个交易日（最新日 K，周末/长假自动向前回溯）。"""
    target = str(today)
    dates = sorted({d for candles in klines.values() for d in candles}, reverse=True)
    for date in dates:
        if date <= target:
            return date
    return dates[0] if dates else None


def _get_fallback_review_date(today=None) -> str:
    """基于『今天』推导的最新 A 股复盘交易日（≤ 今天的最近交易日，离线/兜底时使用）。"""
    if today is None:
        today = _ashare_today()
    target = today
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return str(target)


def _fetch_ashare_pools(review_date: str) -> dict:
    """涨停/跌停池（按日回溯）：家数 + 涨停行业分布（领涨线索）+ 跌停行业分布（领跌线索）。"""
    out = {"limit_up": None, "limit_down": None, "hot_sectors": [], "cold_sectors": []}
    ymd = review_date.replace("-", "")

    def _sector_counts(data: dict, key: str) -> list:
        counts = {}
        for stock in (data or {}).get("pool") or []:
            sector = stock.get("hybk") or ""
            if sector:
                counts[sector] = counts.get(sector, 0) + 1
        return sorted(counts, key=counts.get, reverse=True)

    try:
        zt = _fetch_json(_ASHARE_ZT_URL, {
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
            "Pageindex": 0, "pagesize": 5, "sort": "fbt:asc", "date": ymd,
        })
        data = zt.get("data") or {}
        out["limit_up"] = data.get("tc")
        out["hot_sectors"] = _sector_counts(data, "zt")[:4]
    except Exception:
        pass
    try:
        dt = _fetch_json(_ASHARE_DT_URL, {
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
            "Pageindex": 0, "pagesize": 5, "sort": "fund:asc", "date": ymd,
        })
        data = dt.get("data") or {}
        out["limit_down"] = data.get("tc")
        out["cold_sectors"] = _sector_counts(data, "dt")[:3]
    except Exception:
        pass
    return out


def get_ashare_market() -> dict:
    """采集最新 A 股行情：东方财富接口优先 → 腾讯证券备用接口补位，全部失败回退内置真实快照。"""
    return _build_ashare_market(_fetch_ashare_klines(), _ashare_today())


def _build_ashare_market(klines: dict, today) -> dict:
    """由已抓取的日 K 组装最新复盘行情（东方财富 → 内置快照兜底）。

    与网络解耦：日 K 由调用方传入（`get_ashare_market()` 抓一次网络，
    `collect_market_for_push()` 复用同一次结果做新鲜度检查），便于测试注入。
    """
    fallback_date = _get_fallback_review_date(today)
    try:
        review_date = _pick_review_date(klines, today)
        if not review_date:
            snapshot = dict(_ASHARE_SNAPSHOT)
            snapshot["date"] = fallback_date
            return snapshot

        indices = []
        market_source = None
        for name, _secid in _ASHARE_INDICES:
            candle = (klines.get(name) or {}).get(review_date)
            if candle:
                if market_source is None:
                    market_source = candle.get("source") or "eastmoney"
                indices.append({
                    "name": name,
                    "close": candle["close"],
                    "pct": candle["pct"],
                    "change": candle["change"],
                })
        if len(indices) < 3:
            snapshot = dict(_ASHARE_SNAPSHOT)
            snapshot["date"] = fallback_date
            return snapshot

        # 两市成交额：沪市（上证）+ 深市（深成）日 K 的成交额（单位：元 → 亿元）。
        # 备用源（腾讯）无成交额字段（amount=None）时该维度自动降级为不展示。
        sh = (klines.get("上证指数") or {}).get(review_date)
        sz = (klines.get("深证成指") or {}).get(review_date)
        turnover = None
        if sh and sz and sh.get("amount") is not None and sz.get("amount") is not None:
            amount = (sh["amount"] + sz["amount"]) / 1e8
            prev_date = next(
                (d for d in sorted((klines.get("上证指数") or {}), reverse=True) if d < review_date),
                None,
            )
            delta = None
            if prev_date:
                psh = (klines.get("上证指数") or {}).get(prev_date)
                psz = (klines.get("深证成指") or {}).get(prev_date)
                if psh and psz and psh.get("amount") is not None and psz.get("amount") is not None:
                    delta = round(amount - (psh["amount"] + psz["amount"]) / 1e8)
            turnover = {"amount": round(amount), "delta": delta, "unit": "亿"}

        pools = _fetch_ashare_pools(review_date)
        leaders = [{"name": s, "note": "涨停家数居前"} for s in pools["hot_sectors"]]
        laggards = [{"name": s, "note": "跌停个股所在"} for s in pools["cold_sectors"]]
        return {
            "date": review_date,
            "source": market_source or "eastmoney",
            "indices": indices,
            "turnover": turnover,
            "breadth": {
                "up_text": None,
                "limit_up": pools["limit_up"], "limit_down": pools["limit_down"],
                "broken": None, "seal_rate": None, "ladder": None,
                "hot_sectors": pools["hot_sectors"],
            },
            "leaders": leaders,
            "laggards": laggards,
            "funds": {
                "main": None,
                "north": "北向资金实时数据自 2024 年 8 月起停止披露，以主力资金与两融口径观察",
            },
            "outlook": {"views": [], "catalyst": None},
        }
    except Exception:
        snapshot = dict(_ASHARE_SNAPSHOT)
        snapshot["date"] = fallback_date
        return snapshot


def _snapshot_with_date(snapshot: dict, today) -> dict:
    """复制内置快照并把复盘日改成按今天推导的最近交易日。"""
    out = dict(snapshot)
    out["date"] = _get_fallback_review_date(today)
    return out


def _build_simple_market(klines: dict, today, index_names: list, snapshot: dict,
                         turnover_names: list | None = None,
                         turnover_unit: str = "亿",
                         min_indices: int = 2) -> dict:
    """由已抓取的日 K 组装港股/美股复盘行情（实时接口 → 内置快照兜底）。

    与 A 股不同：无涨停/跌停池，成交额取指定指数日 K 的 amount 之和（无则降级）。
    """
    try:
        review_date = _pick_review_date(klines, today)
        if not review_date:
            return _snapshot_with_date(snapshot, today)

        indices = []
        market_source = None
        for name in index_names:
            candle = (klines.get(name) or {}).get(review_date)
            if not candle:
                continue
            if market_source is None:
                market_source = candle.get("source") or "eastmoney"
            indices.append({
                "name": name,
                "close": candle["close"],
                "pct": candle["pct"],
                "change": candle["change"],
            })
        if len(indices) < min_indices:
            return _snapshot_with_date(snapshot, today)

        turnover = None
        if turnover_names:
            candles = [(klines.get(name) or {}).get(review_date) for name in turnover_names]
            if all(c and c.get("amount") is not None for c in candles):
                amount = sum(c["amount"] for c in candles) / 1e8
                prev_date = next(
                    (d for d in sorted((klines.get(turnover_names[0]) or {}), reverse=True)
                     if d < review_date),
                    None,
                )
                delta = None
                if prev_date:
                    prevs = [(klines.get(name) or {}).get(prev_date) for name in turnover_names]
                    if all(c and c.get("amount") is not None for c in prevs):
                        delta = round(amount - sum(c["amount"] for c in prevs) / 1e8)
                turnover = {"amount": round(amount), "delta": delta, "unit": turnover_unit}

        return {
            "date": review_date,
            "source": market_source or "eastmoney",
            "indices": indices,
            "turnover": turnover,
            "breadth": {
                "up_text": None, "down_text": None,
                "limit_up": None, "limit_down": None,
                "broken": None, "seal_rate": None, "ladder": None,
                "hot_sectors": [],
            },
            "leaders": [],
            "laggards": [],
            "funds": {"main": None, "north": None},
            "outlook": {"views": [], "catalyst": None},
        }
    except Exception:
        return _snapshot_with_date(snapshot, today)


def get_hk_market() -> dict:
    """采集最新港股行情：东方财富接口优先 → 腾讯证券备用接口补位，全部失败回退内置快照。"""
    return _build_simple_market(
        _fetch_hk_klines(), _ashare_today(),
        [name for name, _ in _HK_INDICES], _HK_SNAPSHOT,
        turnover_names=["恒生指数"], turnover_unit="亿港元",
    )


def get_us_market() -> dict:
    """采集最新美股行情：东方财富接口优先 → 腾讯证券备用接口补位，全部失败回退内置快照。"""
    return _build_simple_market(
        _fetch_us_klines(), _ashare_today(),
        [name for name, _ in _US_INDICES], _US_SNAPSHOT,
        turnover_names=["标普500"], turnover_unit="亿美元",
    )


# ---------------------------------------------------------------- 大盘数据新鲜度闸门
# 推送前检查：简报里的「大盘数据」必须是最新的，不是最新就不推。
# 判定标准（三条全过才算「最新」）：
#   ① 行情接口可用——主源（东方财富）与备用源（腾讯证券）都拿不到日 K 就无法确认
#      数据新旧，宁可不放行；
#   ② 接口数据不滞后——最新日 K 不得早于内置快照基线日期（日期只会向前走，
#      一旦倒退说明行情源异常）；
#   ③ 大盘复盘数据来自实时接口（东方财富或腾讯证券备用源，非内置快照兜底），
#      且复盘日 = 按最新口径应复盘的最近交易日（接口数据自动覆盖周末/长假回溯）。

def check_market_freshness(market: dict | None = None,
                           klines: dict | None = None,
                           today=None) -> dict:
    """检查大盘复盘数据是否为最新。返回：

        ok                是否最新（推送闸门以此为据）
        reason            人类可读结论（含具体日期，可直接进日志/推送提示）
        market_date       本次大盘复盘数据的日期
        source            数据来源（eastmoney 主接口 / tencent 备用接口 / snapshot 内置快照）
        latest_kline_date 行情接口最新一根日 K 的日期（接口视角的“今天”）
        expected_date     按最新口径应复盘的最近交易日
        checked_at        检查时间（北京时间）
    """
    today = today or _ashare_today()
    if klines is None:
        klines = _fetch_ashare_klines()
    if market is None:
        market = _build_ashare_market(klines, today)

    dates = sorted({d for candles in klines.values() for d in candles}, reverse=True)
    info = {
        "ok": False,
        "reason": "",
        "market_date": market.get("date") or "",
        "source": market.get("source") or "snapshot",
        "latest_kline_date": dates[0] if dates else "",
        "expected_date": "",
        "checked_at": (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not dates:
        info["reason"] = ("行情接口不可用（主源东方财富与备用源腾讯证券均未返回日 K），"
                          "无法确认大盘数据为最新"
                          f"（当前兜底：内置快照，复盘日 {info['market_date'] or '未知'}）")
        return info

    expected = _pick_review_date(klines, today)
    info["expected_date"] = expected or ""
    floor = _ASHARE_SNAPSHOT["date"]  # 已知基线：接口最新日 K 不可能早于内置快照日期

    if dates[0] < floor:
        info["reason"] = (f"行情接口数据滞后：最新日 K {dates[0]} 早于内置快照基线 {floor}"
                          "，判定非最新")
        return info
    if market.get("source") not in _ASHARE_LIVE_SOURCES:
        info["reason"] = (f"大盘数据回退内置快照（冻结于 {floor}），非实时接口数据，"
                          f"无法确认为最新（应复盘交易日 {expected or '未知'}）")
        return info
    if not expected or market.get("date") != expected:
        info["reason"] = (f"大盘复盘日 {info['market_date'] or '未知'} 与最新可复盘交易日 "
                          f"{expected or '未知'} 不一致，判定非最新")
        return info

    info["ok"] = True
    source_label = _ASHARE_LIVE_SOURCES.get(market.get("source"), "实时行情接口")
    info["reason"] = f"大盘数据为最新：复盘交易日 {expected}（数据来源：{source_label}）"
    return info


def collect_market_for_push() -> tuple[dict, dict]:
    """推送入口专用：一次抓取完成「大盘数据 + 新鲜度检查」。

    返回 (market, freshness)：market 供 analyze_ashare() 渲染复盘板块，
    freshness 供推送闸门判定「不是最新就不推」。日 K 只抓一次，两处共用。

    环境变量 MARKET_FRESHNESS_FORCE=fresh|stale 可强制检查结果（测试/应急用，
    正常推送不要设置）。
    """
    today = _ashare_today()
    klines = _fetch_ashare_klines()
    # 日 K 同时缓存给「AI 政策研报」的 LSTM / Prophet 式模型，避免推送链路重复打行情接口。
    set_policy_klines(klines)
    market = _build_ashare_market(klines, today)
    freshness = check_market_freshness(market, klines=klines, today=today)

    force = os.environ.get("MARKET_FRESHNESS_FORCE", "").strip().lower()
    if force in ("fresh", "stale"):
        freshness["forced"] = True
        freshness["ok"] = force == "fresh"
        freshness["reason"] = f"MARKET_FRESHNESS_FORCE={force} 强制结果（测试/应急）：" + freshness["reason"]
    return market, freshness


def _ashare_bias(market: dict) -> str:
    """由指数涨跌、量能与涨停/跌停对比综合打分，给出偏多/偏空/中性。"""
    indices = market.get("indices") or []
    pcts = [i["pct"] for i in indices]
    avg = sum(pcts) / len(pcts) if pcts else 0
    breadth = market.get("breadth") or {}
    lu, ld = breadth.get("limit_up"), breadth.get("limit_down")
    delta = (market.get("turnover") or {}).get("delta")

    score = avg * 0.6
    if isinstance(delta, (int, float)):
        score += 1 if delta > 0 else -1
    if isinstance(lu, int) and isinstance(ld, int):
        score += 1 if lu > ld * 3 else (-0.5 if lu < ld else 0.3)
    if sum(1 for i in indices if i["pct"] > 0) >= 3:
        score += 0.6
    if score >= 1.2:
        return "偏多"
    if score <= -0.6:
        return "偏空"
    return "中性"


def _fmt_amt(amount: float) -> str:
    """亿元 → 万亿/亿 展示。"""
    return f"{amount / 10000:.2f} 万亿" if amount >= 10000 else f"{amount:,.0f} 亿"


def _compose_ashare_headline(market: dict, bias: str) -> str:
    """AI 一句话复盘：方向 + 领涨主线 + 最强指数 + 风险提示。"""
    leaders = [n["name"] for n in (market.get("leaders") or [])][:2]
    indices = market.get("indices") or []
    best = max(indices, key=lambda i: i["pct"], default=None)
    delta = (market.get("turnover") or {}).get("delta") or 0
    lu = (market.get("breadth") or {}).get("limit_up")

    if bias == "偏多":
        tone = "放量普涨" if delta > 0 else "企稳反弹"
    elif bias == "偏空":
        tone = "承压回落"
    else:
        tone = "震荡分化"
    parts = [tone]
    if leaders:
        parts.append(f"{'与'.join(leaders)}领涨主线")
    if best and best["pct"] > 0:
        parts.append(f"{best['name']} {best['pct']:+.2f}% 领跑")
    if isinstance(lu, int):
        parts.append(f"涨停 {lu} 家")
    if bias == "偏多":
        parts.append("短线情绪偏暖，注意高位轮动")
    elif bias == "偏空":
        parts.append("避险情绪升温，控制仓位")
    else:
        parts.append("方向待确认，低吸不追高")
    return "，".join(parts) + "。"


def _compose_ashare_indices(indices: list) -> str:
    """维度 ① 三大指数涨跌：指数明细 + 格局判断。"""
    if not indices:
        return "指数数据暂缺，以当日盘面为准。"
    items = [f"{i['name']} {i['pct']:+.2f}%（报 {i['close']:.2f} 点）" for i in indices]
    up = sum(1 for i in indices if i["pct"] > 0)
    down = sum(1 for i in indices if i["pct"] < 0)
    if up == len(indices):
        verdict = "主要指数集体收涨，呈普涨格局"
    elif down == len(indices):
        verdict = "指数全线收跌，防御情绪升温"
    else:
        verdict = "指数涨跌分化，结构行情为主"
    best = max(indices, key=lambda i: i["pct"], default=None)
    if best and best["pct"] >= 2 and up > down:
        verdict += f"，{best['name']} 领跑、成长风格占优"
    return "、".join(items) + "。" + verdict + "。"


def _compose_ashare_turnover(turnover: dict | None) -> str:
    """维度 ② 两市成交额：量能水平 + 放量/缩量解读。"""
    if not turnover or turnover.get("amount") is None:
        return "成交额数据暂缺。"
    amount = turnover["amount"]
    text = f"沪深两市成交 {_fmt_amt(amount)}"
    delta = turnover.get("delta")
    if delta is None and turnover.get("prev") is not None:
        delta = amount - turnover["prev"]
    if isinstance(delta, (int, float)) and delta != 0:
        base = amount - delta
        pct = abs(delta) / base * 100 if base > 0 else 0
        if delta > 0:
            text += f"，较前一日放量 {_fmt_amt(delta)}（+{pct:.1f}%），量价齐升、增量资金入场信号明确"
        else:
            text += f"，较前一日缩量 {_fmt_amt(-delta)}（-{pct:.1f}%），观望情绪仍待消化"
    return text + "。"


def _compose_ashare_breadth(breadth: dict | None) -> str:
    """维度 ③ 涨跌家数与涨跌停：市场广度 + 连板梯队 + 情绪温度。"""
    b = breadth or {}
    parts = []
    if b.get("up_text"):
        parts.append(f"全市场{b['up_text']}个股上涨")
    if isinstance(b.get("limit_up"), int):
        parts.append(f"涨停 {b['limit_up']} 家")
    if isinstance(b.get("limit_down"), int):
        parts.append(f"跌停 {b['limit_down']} 家")
    if b.get("broken"):
        parts.append(f"炸板 {b['broken']} 家、封板率 {b['seal_rate'] or '—'}")
    if b.get("ladder"):
        parts.append(f"连板高度：{b['ladder']}")
    if not parts:
        return "涨跌家数盘后口径暂缺，以涨停/跌停与指数方向综合判断。"
    text = "、".join(parts)
    lu, ld = b.get("limit_up"), b.get("limit_down")
    if isinstance(lu, int) and isinstance(ld, int):
        if lu > ld * 3:
            text += "，赚钱效应与短线情绪偏暖"
        elif ld > lu:
            text += "，亏钱效应显现、情绪偏弱"
    return text + "。"


def _compose_ashare_sectors(market: dict) -> str:
    """维度 ④ 领涨/领跌板块：主线 + 退潮方向 + 点评。"""
    leaders = market.get("leaders") or []
    laggards = market.get("laggards") or []
    if not leaders and not laggards:
        hot = (market.get("breadth") or {}).get("hot_sectors") or []
        if hot:
            return f"涨停梯队集中在{'、'.join(hot)}，主线延续性待验证。"
        return "板块线索暂缺，等待盘后行业数据。"
    text = f"领涨：{'、'.join(n['name'] for n in leaders[:4])}"
    if leaders and leaders[0].get("note"):
        text += f"（{leaders[0]['note']}）"
    if laggards:
        text += f"；领跌：{'、'.join(n['name'] for n in laggards[:3])}"
        if laggards[0].get("note"):
            text += f"（{laggards[0]['note']}）"
    if leaders and laggards:
        tech_keys = ("科技", "算力", "半导体", "芯片", "存储", "电子", "通信", "计算机", "CPO", "PCB")
        leader_text = "".join(n["name"] for n in leaders)
        closer = ("科技硬主线扩散、防御与高位消费遭资金抽离"
                  if any(k in leader_text for k in tech_keys)
                  else "热点高低切换、结构性轮动延续")
        text += f"。{closer}"
    return text + "。"


def _compose_ashare_funds(market: dict) -> str:
    """维度 ⑤ 主力资金与北向资金：流入主线 + 北向口径说明。"""
    f = market.get("funds") or {}
    parts = [p for p in (f.get("main"), f.get("north")) if p]
    if not f.get("main"):
        # 实时路径无盘后主力资金明细：用涨停梯队行业分布作为封板资金线索。
        hot = (market.get("breadth") or {}).get("hot_sectors") or []
        if hot:
            parts.insert(0, f"涨停梯队集中在{'、'.join(hot)}（封板资金口径），主力资金盘后明细待更新")
    if not parts:
        return "资金口径数据暂缺，以主力资金与两融数据为准。"
    return "；".join(parts) + "。"


def _compose_named_turnover(turnover: dict | None, prefix: str, unit_suffix: str = "") -> str:
    """通用成交额句：量能水平 + 放量/缩量解读。"""
    if not turnover or turnover.get("amount") is None:
        return f"{prefix}数据暂缺。"
    amount = turnover["amount"]
    text = f"{prefix} {_fmt_amt(amount)}{unit_suffix}"
    delta = turnover.get("delta")
    if delta is None and turnover.get("prev") is not None:
        delta = amount - turnover["prev"]
    if isinstance(delta, (int, float)) and delta != 0:
        base = amount - delta
        pct = abs(delta) / base * 100 if base > 0 else 0
        if delta > 0:
            text += f"，较前一日放量 {_fmt_amt(delta)}{unit_suffix}（+{pct:.1f}%），量价齐升、增量资金入场信号明确"
        else:
            text += f"，较前一日缩量 {_fmt_amt(-delta)}{unit_suffix}（-{pct:.1f}%），观望情绪仍待消化"
    return text + "。"


def _compose_overseas_breadth(breadth: dict | None, market_name: str) -> str:
    """港股/美股涨跌家数：无涨跌停口径，缺数据时诚实降级。"""
    b = breadth or {}
    parts = []
    if b.get("up_text"):
        parts.append(f"{market_name}{b['up_text']}个股上涨")
    if b.get("down_text"):
        parts.append(f"{b['down_text']}下跌")
    if not parts:
        return f"{market_name}涨跌家数盘后口径暂缺，以指数方向综合判断。"
    return "、".join(parts) + "。"


def _compose_overseas_funds(market: dict, empty: str) -> str:
    """港股南向 / 美股资金与避险：有则拼接，无则降级。"""
    f = market.get("funds") or {}
    parts = [p for p in (f.get("main"), f.get("north")) if p]
    if not parts:
        hot = (market.get("breadth") or {}).get("hot_sectors") or []
        if hot:
            return f"资金线索集中在{'、'.join(hot)}，盘后明细待更新。"
        return empty
    return "；".join(parts) + "。"


def _compose_ashare_outlook(market: dict, bias: str, venue: str = "A股") -> str:
    """维度 ⑥ 后市观点与策略：AI 看盘 + 催化 + 机构观点。"""
    if venue == "港股":
        if bias == "偏多":
            verdict = "港股短线偏多但防高位回吐：沿科技与南向主线低吸，避免追高内房反弹"
        elif bias == "偏空":
            verdict = "港股短线以防御为主：控制仓位，等待南向回流与恒生科技企稳"
        else:
            verdict = "港股方向未明：控制仓位、低吸不追高，等待南向与科技共振确认"
    elif venue == "美股":
        if bias == "偏多":
            verdict = "美股短线偏多但防高位波动：沿科技成长低吸，关注美联储与财报扰动"
        elif bias == "偏空":
            verdict = "美股短线以防御为主：控制仓位，等待波动率回落与指数企稳"
        else:
            verdict = "美股方向未明：控制仓位、低吸不追高，等待政策与业绩信号"
    else:
        if bias == "偏多":
            verdict = "短线偏多但防高位轮动：量能若能维持，可沿领涨主线低吸参与，避免追高连板高位股"
        elif bias == "偏空":
            verdict = "短线以防御为主：控制仓位，等待跌停收敛与量能企稳信号"
        else:
            verdict = "方向未明：控制仓位、低吸不追高，等待变盘信号"
    outlook = market.get("outlook") or {}
    text = f"AI 看盘：{verdict}"
    if outlook.get("catalyst"):
        text += f"；催化：{outlook['catalyst']}"
    if outlook.get("views"):
        text += "；机构观点：" + "；".join(outlook["views"])
    return text + "。"


def analyze_ashare(market: dict | None = None) -> dict:
    """最新 A 股六维度 AI 复盘。``market`` 缺省时先尝试实时采集，失败回退内置快照。

    返回：
        date       复盘交易日（如 2026-08-27）
        source     数据来源（eastmoney 主接口 / tencent 备用接口 / snapshot 内置快照）
        headline   AI 一句话复盘
        bias       偏多 / 偏空 / 中性
        indices    四大指数明细 [{name, close, pct, change}]
        leaders    领涨板块 [{name, note}]；laggards 领跌板块
        turnover   成交额 {amount 亿, delta 亿, unit}
        breadth    涨跌家数/涨跌停/连板信息
        points     六维度观点 [{key, label, text}]
    """
    market = market if market is not None else get_ashare_market()
    bias = _ashare_bias(market)
    points = [
        {"key": "indices", "label": "三大指数",
         "text": _compose_ashare_indices(market.get("indices") or [])},
        {"key": "turnover", "label": "两市成交额",
         "text": _compose_ashare_turnover(market.get("turnover"))},
        {"key": "breadth", "label": "涨跌家数与涨跌停",
         "text": _compose_ashare_breadth(market.get("breadth"))},
        {"key": "sectors", "label": "领涨 / 领跌板块",
         "text": _compose_ashare_sectors(market)},
        {"key": "funds", "label": "主力资金与北向资金",
         "text": _compose_ashare_funds(market)},
        {"key": "outlook", "label": "后市观点与策略",
         "text": _compose_ashare_outlook(market, bias)},
    ]
    return {
        "date": market.get("date") or "",
        "source": market.get("source") or "snapshot",
        "headline": _compose_ashare_headline(market, bias),
        "bias": bias,
        "indices": market.get("indices") or [],
        "leaders": market.get("leaders") or [],
        "laggards": market.get("laggards") or [],
        "turnover": market.get("turnover"),
        "breadth": market.get("breadth") or {},
        "points": points,
    }


def analyze_hk(market: dict | None = None) -> dict:
    """最新港股六维度 AI 看盘。``market`` 缺省时先尝试实时采集，失败回退内置快照。"""
    market = market if market is not None else get_hk_market()
    bias = _ashare_bias(market)
    points = [
        {"key": "indices", "label": "三大指数",
         "text": _compose_ashare_indices(market.get("indices") or [])},
        {"key": "turnover", "label": "港股成交额",
         "text": _compose_named_turnover(market.get("turnover"), "港股成交", "港元")},
        {"key": "breadth", "label": "涨跌家数",
         "text": _compose_overseas_breadth(market.get("breadth"), "港股")},
        {"key": "sectors", "label": "领涨 / 领跌板块",
         "text": _compose_ashare_sectors(market)},
        {"key": "funds", "label": "南向资金",
         "text": _compose_overseas_funds(market, "南向资金口径暂缺，以北水与恒生科技方向观察。")},
        {"key": "outlook", "label": "后市观点与策略",
         "text": _compose_ashare_outlook(market, bias, venue="港股")},
    ]
    return {
        "date": market.get("date") or "",
        "source": market.get("source") or "snapshot",
        "headline": _compose_ashare_headline(market, bias),
        "bias": bias,
        "indices": market.get("indices") or [],
        "leaders": market.get("leaders") or [],
        "laggards": market.get("laggards") or [],
        "turnover": market.get("turnover"),
        "breadth": market.get("breadth") or {},
        "points": points,
        "market": "hk",
    }


def analyze_us(market: dict | None = None) -> dict:
    """最新美股六维度 AI 看盘。``market`` 缺省时先尝试实时采集，失败回退内置快照。"""
    market = market if market is not None else get_us_market()
    bias = _ashare_bias(market)
    points = [
        {"key": "indices", "label": "三大指数",
         "text": _compose_ashare_indices(market.get("indices") or [])},
        {"key": "turnover", "label": "美股成交额",
         "text": _compose_named_turnover(market.get("turnover"), "美股成交", "美元")},
        {"key": "breadth", "label": "涨跌家数",
         "text": _compose_overseas_breadth(market.get("breadth"), "美股")},
        {"key": "sectors", "label": "领涨 / 领跌板块",
         "text": _compose_ashare_sectors(market)},
        {"key": "funds", "label": "资金与避险",
         "text": _compose_overseas_funds(market, "美股资金口径暂缺，以科技成长与 VIX 方向观察。")},
        {"key": "outlook", "label": "后市观点与策略",
         "text": _compose_ashare_outlook(market, bias, venue="美股")},
    ]
    return {
        "date": market.get("date") or "",
        "source": market.get("source") or "snapshot",
        "headline": _compose_ashare_headline(market, bias),
        "bias": bias,
        "indices": market.get("indices") or [],
        "leaders": market.get("leaders") or [],
        "laggards": market.get("laggards") or [],
        "turnover": market.get("turnover"),
        "breadth": market.get("breadth") or {},
        "points": points,
        "market": "us",
    }


# ---------------------------------------------------------------- 推送 HTML
def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _trunc(s: str, n: int = 60) -> str:
    s = _esc(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _news_key(title: str) -> str:
    """快讯标题指纹：忽略大小写、空白与标点，用于跨源去重。

    末尾的「全网快讯」列表不标注来源，同一条新闻被多个源转载时若重复出现会很扎眼，
    因此按指纹只保留首次出现的那一条。
    """
    return re.sub(r"[\s\W_]+", "", (title or "").lower())


# PushPlus 单条推送字符上限：会员 10 万字、普通账号 2 万字。这里各留 2,000 字符安全余量
# （HTML 标签本身也计字符，避免刚好卡在服务端阈值上被拒）。
PUSHPLUS_MEMBER_MAX_CHARS = 98000       # 10 万字口径
PUSHPLUS_FREE_MAX_CHARS = 19500         # 2 万字口径
PUSHPLUS_MEMBER_MAX_ITEMS_PER_SOURCE = 20   # 会员口径：每个数据源最多 20 条
PUSHPLUS_FREE_MAX_ITEMS_PER_SOURCE = 3      # 普通口径：精选前 3 条
PUSHPLUS_MEMBER_DEFAULT = True          # 默认按 10 万字口径出全量内容
# 正文末尾「全网快讯」列表：每个数据源固定保留 3 条，只显示标题、不标注来源（隐藏源头）。
# 推送字符上限不够时由 build_html() 自动收敛到 2 条 / 1 条，最后整段省略，保证发得出去。
NEWS_ITEMS_PER_SOURCE = 3


def _env_flag(name: str, default: bool) -> bool:
    """布尔环境变量：显式写了才按值判定（1/true/yes/on 为真），没写用默认值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int | None) -> int | None:
    """整数环境变量：未设置或非法时返回默认值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def default_fetch_limit() -> int:
    """每个数据源的默认抓取条数：与推送口径一致，避免「额度放宽了但内容没变多」。

    ``BRIEF_FETCH_LIMIT`` 可显式覆盖（如临时压缩抓取量、或会员账号想更多）。
    """
    return _env_int("BRIEF_FETCH_LIMIT", LIMIT_MEMBER if _is_pushplus_member() else LIMIT)


def _is_pushplus_member() -> bool:
    """是否按 PushPlus 会员口径（10 万字 / 全量快讯）推送。

    默认即为会员口径；普通账号或想退回 2 万字精选版时显式设 ``PUSHPLUS_MEMBER=0``。
    """
    return _env_flag("PUSHPLUS_MEMBER", PUSHPLUS_MEMBER_DEFAULT)


def pushplus_quota() -> tuple[int, int | None]:
    """本次推送的容量口径：(字符上限, 每个数据源最多展示条数；None = 全量展示)。

    优先级：``PUSHPLUS_MAX_CHARS`` / ``PUSHPLUS_ITEMS_PER_SOURCE`` 显式覆盖
    → 否则按会员口径（98,000 字符 / 每源 20 条）或普通口径（19,500 字符 / 精选 3 条）。
    ``PUSHPLUS_ITEMS_PER_SOURCE=all``（或 0）表示展示抓到的全部快讯。
    把上限与条数放在一起，保证推送既尽量用满额度、又不会超阈值被 PushPlus 拒绝。
    """
    member = _is_pushplus_member()
    max_length = _env_int("PUSHPLUS_MAX_CHARS",
                          PUSHPLUS_MEMBER_MAX_CHARS if member else PUSHPLUS_FREE_MAX_CHARS)
    default_items = PUSHPLUS_MEMBER_MAX_ITEMS_PER_SOURCE if member else PUSHPLUS_FREE_MAX_ITEMS_PER_SOURCE
    raw_items = os.environ.get("PUSHPLUS_ITEMS_PER_SOURCE", "").strip().lower()
    if raw_items in ("all", "full", "0", "-1"):
        items: int | None = None
    else:
        items = _env_int("PUSHPLUS_ITEMS_PER_SOURCE", default_items)
    return max_length, items


def build_html(
    brief: dict,
    now: datetime | None = None,
    review: dict | None = None,
    hk_review: dict | None = None,
    us_review: dict | None = None,
    max_items_per_source: int | None = None,
    max_length: int | None = None,
    series: dict | None = None,
) -> str:
    """生成适合微信阅读的竖版长图文简报（内联样式，兼容 PushPlus HTML 模板）。

    视觉基调：现代金融 App 风（参考截图）。纯白页面底色 + 浅灰圆角卡片，
    标题与重点用黑色粗体，柠檬绿（#b5e61d）做标签、高亮、点阵与分段进度条点缀，
    辅助信息用中性灰，避免邮件客户端中的复杂布局。
    ``now`` 保留在接口中以兼容现有调用，但报告标题不展示推送时间。
    ``review`` 为 A 股看盘结果；缺省时自动调用 analyze_ashare()（实时采集 → 快照兜底）。
    ``hk_review`` / ``us_review`` 为港股、美股看盘结果；缺省时分别调用 analyze_hk() / analyze_us()。
    ``series`` 为「AI 政策研报」板块时序模型（LSTM / Prophet 式分解）的输入序列：
    缺省时用 :func:`get_policy_series`——推送路径缓存的真实日 K，否则明确标注的合成演示序列。
    ``max_items_per_source`` / ``max_length`` 参数保留用于兼容既有调用与推送容量配置；
    正文以分析栏目为主（AI 每日总结 → AI 看盘 → AI 情绪热力图 → AI 政策分析 → AI 政策深度 → AI 政策研报 → AI 板块机会），
    「AI 看盘」紧跟「AI 每日总结」之后，让最新的 A 股 / 港股 / 美股行情在开篇就能看到；
    不展示监测平台清单、时间核对或推送协议说明。
    **正文最后（免责声明与作者署名之前）追加「全网快讯」列表**：每个数据源固定保留
    :data:`NEWS_ITEMS_PER_SOURCE`（3）条，按源顺序取每源前 3 条并跨源去重，
    **只列标题、不标注来源（隐藏源头）**；``max_length`` 不够时自动收敛到每源 2 / 1 条，
    最后整段省略，保证推送始终在 PushPlus 字符上限内。
    抓取条数仍由 :func:`pushplus_quota` 统一控制，保证任何账号类型都能发出去、不被
    PushPlus 拒收。
    """
    quota_max_length, quota_items_per_source = pushplus_quota()
    if max_items_per_source is None:
        max_items_per_source = quota_items_per_source
    if max_length is None:
        max_length = quota_max_length

    # 简洁金融 App 配色（参考截图）：白底、浅灰卡片、黑色粗体、柠檬绿点缀。
    neon_green = "#b5e61d"   # 柠檬绿点缀：高亮 / 标签 / 进度条
    ink = "#111111"
    black = "#111111"
    paper = "#ffffff"
    paper_lift = "#f4f4f4"
    muted = "#6b6b6b"
    rule = "#e6e6e6"
    gray_light = "#e4e4e4"
    up_text = "#5f8f00"      # 上涨：深柠檬绿（白底可读）
    danger_hi = "#767676"    # 下跌 / 承压：中性灰（单色点缀体系）
    analysis = analyze_brief(brief)
    hl_tags = list(dict.fromkeys(
        analysis["sectors"] + analysis["sectors_up"] + analysis["sectors_down"]
        + [entry["tag"] for entry in analysis["opportunity_sectors"]]
    ))

    def _hl(escaped_text: str) -> str:
        for tag in hl_tags:
            tag_esc = _esc(tag)
            escaped_text = escaped_text.replace(tag_esc, f'<span class="hl">{tag_esc}</span>')
        return escaped_text

    headline = _hl(_esc(analysis["headline"]))

    point_rows = []
    for point_index, point in enumerate(analysis["points"], 1):
        text = _esc(point["text"])
        if point["key"] in ("sectors", "flow"):
            text = _hl(text)
        point_rows.append(
            f'<tr><td class="td-n">{point_index:02d}</td>'
            f'<td class="td-p"><span class="tag">{_esc(point["label"])}</span> {text}</td></tr>'
        )
    points_html = f'<table class="tbl-sub">' + "".join(point_rows) + "</table>"

    def _opp_row(rank: int, entry: dict, is_pressure: bool) -> str:
        tag = _esc(entry["tag"])
        tag_cls = "tag-d" if is_pressure else "tag"
        net_text = f"净空 {-entry['net']}" if is_pressure else f"净多 {entry['net']}"
        pair = entry.get("etf_pair") or hk_etf_pair("", entry["tag"])
        pair_text = _esc(_hk_etf_pair_text(pair))
        mapping = _esc((pair[0].get("mapping") if pair else "") or "")
        head = (
            f'<div><span class="{tag_cls}">{tag}</span> '
            f'<span class="{tag_cls}">{net_text}</span> '
            f'<span class="sub">{entry["sources"]} 源命中 · {entry["mentions"]} 条提及</span></div>'
            f'<div class="ev"><b>港股 ETF 双标的</b>｜{pair_text}</div>'
            f'<div class="ev">映射口径：{mapping}</div>'
        )
        evidence_rows = []
        for ev in entry["evidence"]:
            evidence_rows.append(
                f'<div class="ev">· {_esc(ev.get("source") or "")}｜{_hl(_trunc(str(ev.get("title") or ""), 50))}</div>'
            )
        return (
            f'<tr><td class="td-n">{rank:02d}</td>'
            f'<td class="td-t">{head}{"".join(evidence_rows)}</td></tr>'
        )

    opportunity_sectors = analysis["opportunity_sectors"]
    pressure_sectors = analysis["pressure_sectors"]
    opp_rows = [f'<tr><td colspan="2" class="td-hdr"><span class="tag">机会方向</span> <span class="sub">净多头信号板块 · 按信号强度 × 跨源热度排序</span></td></tr>']
    for rank, entry in enumerate(opportunity_sectors, 1):
        opp_rows.append(_opp_row(rank, entry, False))
    if not opportunity_sectors:
        opp_rows.append(
            f'<tr><td colspan="2" class="sub" style="padding:6px 0;">'
            f'今日样本中暂无净多头信号占优的板块，等待新数据。</td></tr>'
        )
    if pressure_sectors:
        opp_rows.append(f'<tr><td colspan="2" class="td-hdr"><span class="tag-d">承压方向</span> <span class="sub">净空头信号板块 · 资金回避方向</span></td></tr>')
        for rank, entry in enumerate(pressure_sectors, 1):
            opp_rows.append(_opp_row(rank, entry, True))

    opportunities_card = (
        f'<div class="card"><div class="hdr">'
        f'<span class="tag">AI 板块机会</span>'
        f'<span class="sub">基于 {analysis["signals"]} 条多空信号</span></div>'
        f'<table class="tbl">{"".join(opp_rows)}</table></div>'
    )

    # ── 「AI 政策分析」卡片：政策维度热度 + 鹰鸽取向 + 支撑新闻（与其余板块共用同一批标题）──
    # 这里单独跑一次深度层（analyze_brief 默认不跑，避免 /api 与测试路径承担模型开销）。
    policy = analyze_policy(brief, series=series, deep=True)

    def _hl_policy(text: str) -> str:
        """政策维度标签在总结句里高亮：净空维度用下跌强调色，其余用荧光绿。

        按标签长度从长到短替换，并跳过已被更长标签包住的短标签（如「货币政策」是
        「货币政策 · 美联储」的前缀），避免嵌套 span 破坏推送 HTML。
        """
        escaped = _esc(text)
        done = []
        for entry in sorted(policy["buckets"], key=lambda e: -len(e["tag"])):
            cls = "hl-d" if entry["net"] < 0 else "hl"
            for needle in (_esc(entry["tag"]), _esc(_policy_short(entry["tag"]))):
                if not needle or any(needle in prev for prev in done):
                    continue
                if needle in escaped:
                    escaped = escaped.replace(needle, f'<span class="{cls}">{needle}</span>')
                    done.append(needle)
                    break
        return escaped

    policy_rows = [f'<tr><td colspan="2" class="td-hdr"><span class="tag">政策主线</span> '
                   f'<span class="sub">按跨源命中 × 提及条数排序</span></td></tr>']
    for rank, entry in enumerate(policy["buckets"][:4], 1):
        stance = entry["stance"]
        stance_cls = "tag-d" if stance == "偏鹰" else ("tag" if stance == "偏鸽" else "tag-w")
        if entry["net"] > 0:
            net_text, net_cls = f"政策净多 {entry['net']}", "tag"
        elif entry["net"] < 0:
            net_text, net_cls = f"政策净空 {-entry['net']}", "tag-d"
        else:
            net_text, net_cls = "政策中性", "tag-w"
        head = (
            f'<div><span class="tag">{_esc(entry["tag"])}</span> '
            f'<span class="{stance_cls}">{stance}</span> '
            f'<span class="{net_cls}">{net_text}</span> '
            f'<span class="sub">{entry["sources"]} 源命中 · {entry["mentions"]} 条提及 · '
            f'鹰派 {entry["hawk"]} : 鸽派 {entry["dove"]}</span></div>'
        )
        ev_rows = []
        for ev in entry["evidence"][:2]:
            mark = {"鹰派": "〔鹰〕", "鸽派": "〔鸽〕"}.get(ev.get("stance") or "", "")
            ev_rows.append(
                f'<div class="ev">· {_esc(ev.get("source") or "")}｜{mark}'
                f'{_hl(_trunc(str(ev.get("title") or ""), 50))}</div>'
            )
        policy_rows.append(f'<tr><td class="td-n">{rank:02d}</td>'
                           f'<td class="td-t">{head}{"".join(ev_rows)}</td></tr>')
    if not policy["buckets"]:
        policy_rows.append(
            f'<tr><td colspan="2" class="sub" style="padding:6px 0;">'
            f'当日样本中暂无政策面信号可统计，等待数据源刷新。</td></tr>'
        )

    policy_card = (
        f'<div class="card"><div class="hdr"><span class="tag">AI 政策分析</span>'
        f'<span class="sub">{policy["mentions"]} 条政策提及 · {policy["sources_hit"]} 个数据源</span></div>'
        f'<div class="txt">{_hl_policy(policy["headline"])}</div>'
        f'<table class="tbl-sub">{"".join(policy_rows)}</table>'
        f'<div class="ftr">政策取向由鹰派 / 鸽派词库判定（否定表述自动反转，如「不再那么鸽派」计为鹰派）· '
        f'与「AI 每日总结」「AI 板块机会」共用同一批抓取标题</div></div>'
    )

    # ── 「AI 情绪热力图」卡片：关键词级热度与情绪（面向股市投资）
    #    每个关键词为一个格子，含 9 维投资指标（热度/情绪/共振/分歧/爆发/资金倾向/评级/风险/置信度），
    #    线索列表（标题/来源/时间“当日”/情绪标签 利好/利空/中性）不展示，推送版仅以表格呈现热力网格。
    heatmap = analysis.get("heatmap") or {}

    def _heatmap_badge(text: str, kind: str = "") -> str:
        cls = "tag"
        if kind == "hot":
            cls = "tag"
        elif kind == "warn":
            cls = "tag-d"
        elif kind == "neutral":
            cls = "tag-w"
        return f'<span class="{cls}">{_esc(text)}</span>'

    def _render_heatmap(level: int) -> str:
        if not heatmap or not (heatmap.get("keywords") or []):
            headline_hm = _esc(heatmap.get("headline") or "当日样本中暂无关键词命中，热力图等待数据刷新。")
            return (
                f'<div class="card"><div class="hdr"><span class="tag">AI 情绪热力图</span>'
                f'<span class="sub">关键词级线索 · 投资级指标</span></div>'
                f'<div class="txt">{headline_hm}</div>'
                f'<div class="ftr">每个格子=一个投资关键词，热度=提及×(1+ln(跨源+1))归一；情绪=(多-空)/信号；爆发=热度×|情绪|。仅统计信号，不作为投资依据。</div></div>'
            )
        headline_hm = _esc(heatmap.get("headline") or "")
        total_hit = heatmap.get("total_hit", 0)
        overall_bias = heatmap.get("overall_bias", "中性")
        bias_cls = "tag" if overall_bias == "偏多" else ("tag-d" if overall_bias == "偏空" else "tag-w")
        total_mentions = heatmap.get("total_mentions", 0)
        total_signals = heatmap.get("total_signals", 0)
        keywords = heatmap.get("keywords") or []
        # level 2 展示 24 个，level 1 展示 12 个，level 0 仅展示 Top 4 摘要（极简，确保免费口径 19,500 字符内仍能发得出去）
        if level <= 0:
            # 极简模式：只保留 headline + Top 4 关键词一行摘要，不渲染网格与指标大表（节省 ~6k 字符）
            top4 = keywords[:4]
            top_line = "、".join(
                f'{_esc(k.get("keyword") or "")}{k.get("heat_level") or ""}·{k.get("heat",0)}'
                f'（{_esc(k.get("sentiment_label") or "")}｜'
                f'{_esc("+".join(x.get("code", "") for x in (k.get("etf_pair") or []))) }）'
                for k in top4
            )
            return (
                f'<div class="card"><div class="hdr"><span class="tag">AI 情绪热力图</span>'
                f'<span class="sub">{total_hit} 个关键词 · {total_mentions} 条线索</span></div>'
                f'<div class="txt">{headline_hm}</div>'
                f'<div style="margin:6px 0;color:{ink};font-size:11px;"><span class="{bias_cls}">{_esc(overall_bias)}</span> '
                f'<span class="sub">Top 4：{top_line}</span></div>'
                f'<div class="ftr">完整热力网格（{total_hit} 词 × 9 维指标）在 <span class="hl">/api/heatmap</span> 可查；线索列表不展示，仅统计信号，不作为投资依据。</div></div>'
            )
        limit = 24 if level >= 2 else 12
        shown = keywords[:limit]
        # 热力网格：2 列排布（邮件表格），每个格子=关键词
        rows = []
        for idx in range(0, len(shown), 2):
            pair = shown[idx:idx+2]
            cells = []
            for kw in pair:
                # 左框颜色由情绪决定（偏多=柠檬绿，偏空=中性灰，中性=浅灰）
                border = neon_green if kw.get("sentiment_label") == "偏多" else ("#9a9a9a" if kw.get("sentiment_label") == "偏空" else "#e2e2e2")
                heat = kw.get("heat", 0)
                heat_label = kw.get("heat_level", "")
                sentiment_label = kw.get("sentiment_label", "中性")
                rating = kw.get("rating", "")
                flow = kw.get("flow", "观望")
                risk = kw.get("risk", "低")
                # 热度条宽度=heat%
                bar_color = neon_green if kw.get("sentiment", 0) > 0.2 else ("#9a9a9a" if kw.get("sentiment", 0) < -0.2 else "#d4d4d4")
                # 徽章底色：正向=柠檬绿，负向=浅灰，中性=白底细线
                if rating.startswith("强多") or "偏多" in rating:
                    rating_css = f"background:{neon_green};color:{black};"
                elif "偏空" in rating or "强空" in rating:
                    rating_css = "background:#e8e8e8;color:#6b6b6b;"
                else:
                    rating_css = f"background:#ffffff;color:{muted};border:1px solid {rule};"
                if flow == "流入":
                    flow_css = f"background:{neon_green};color:{black};"
                elif flow == "流出":
                    flow_css = "background:#e8e8e8;color:#6b6b6b;"
                else:
                    flow_css = f"background:#ffffff;color:{muted};border:1px solid {rule};"
                risk_css = f"background:#ffffff;color:{muted};border:1px solid {rule};"
                concept = _esc(kw.get("concept") or kw.get("tag") or "")
                pair_detail = _esc(kw.get("etf_pair_text") or "")
                hint = _esc(kw.get("hint") or "")
                # 线索列表不展示：格子只保留信号计数（标题/来源/时间“当日”/情绪标签 利好/利空/中性 不再列出）
                ev_html = f'<div class="ev">信号 {kw.get("bull",0)}:{kw.get("bear",0)} · 线索列表不展示</div>'
                cell = (
                    f'<td style="width:50%;vertical-align:top;padding:6px;border:1px solid {rule};border-left:4px solid {border};background:{paper};">'
                    f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px;"><b style="font-size:12px;">{_esc(kw.get("keyword") or "")}</b>'
                    f'<span style="color:{muted};font-size:10px;">{_esc(kw.get("tag") or "")}</span></div>'
                    f'<div style="font-size:10px;color:{muted};margin-top:1px;"><b>{concept}</b></div>'
                    f'<div style="font-size:9px;color:{muted};margin-top:2px;line-height:1.35;">{pair_detail}</div>'
                    f'<div style="height:6px;background:#ececec;border-radius:3px;margin:5px 0;overflow:hidden;"><div style="height:100%;width:{heat}%;background:{bar_color};border-radius:3px;"></div></div>'
                    f'<div style="font-size:10px;color:{ink};line-height:1.5;"><b>{_esc(heat_label)}</b>·热度<b>{heat}</b>｜情绪<b>{_esc(sentiment_label)}</b>（净{kw.get("net",0):+d}）｜{kw.get("sources",0)}源·{kw.get("mentions",0)}条</div>'
                    f'<div style="margin-top:4px;">'
                    f'<span style="font-size:10px;{rating_css}padding:1px 4px;border-radius:3px;">{_esc(rating)}</span> '
                    f'<span style="font-size:10px;{flow_css}padding:1px 4px;border-radius:3px;">{_esc(flow)}</span> '
                    f'<span style="font-size:10px;{risk_css}padding:1px 4px;border-radius:3px;">风险{_esc(risk)}</span>'
                    f'</div>'
                    f'{ev_html}'
                    f'</td>'
                )
                cells.append(cell)
            if len(cells) == 1:
                cells.append(f'<td style="width:50%;padding:6px;"></td>')
            rows.append(f'<tr>{"".join(cells)}</tr>')
        # 指标说明（level>=1 全量，level 0 只列核心 4 个）
        indicators = heatmap.get("indicators") or []
        if level >= 1:
            ind_rows = "".join(
                f'<tr><td class="td-n">·</td><td class="td-t"><b>{_esc(ind.get("label") or "")}</b> '
                f'<span class="sub">{_esc(ind.get("formula") or "")}</span> '
                f'<span class="sub">· {_esc(ind.get("use") or "")}</span></td></tr>'
                for ind in indicators
            )
        else:
            core_keys = {"heat", "sentiment", "burst", "rating"}
            ind_rows = "".join(
                f'<tr><td class="td-n">·</td><td class="td-t"><b>{_esc(ind.get("label") or "")}</b> '
                f'<span class="sub">{_esc(ind.get("formula") or "")}</span></td></tr>'
                for ind in indicators if ind.get("key") in core_keys
            )
        # Top 机会/承压 快览（level>=1）
        top_html = ""
        if level >= 1 and (heatmap.get("top_opportunity") or heatmap.get("top_pressure")):
            opp = heatmap.get("top_opportunity") or []
            press = heatmap.get("top_pressure") or []
            parts = []
            if opp:
                parts.append(f'<span class="tag">机会</span> {"、".join(_esc(k.get("keyword") or "") for k in opp[:3])}')
            if press:
                parts.append(f'<span class="tag-d">承压</span> {"、".join(_esc(k.get("keyword") or "") for k in press[:2])}')
            if parts:
                top_html = f'<div style="margin-top:6px;color:{ink};font-size:11px;">{"　".join(parts)}</div>'
        return (
            f'<div class="card"><div class="hdr"><span class="tag">AI 情绪热力图</span>'
            f'<span class="sub">{total_hit} 个关键词 · {total_mentions} 条线索 · 信号 {total_signals} 条</span></div>'
            f'<div class="txt">{headline_hm}</div>'
            f'<div style="margin:6px 0;"><span class="{bias_cls}">{_esc(overall_bias)}</span> '
            f'<span class="sub">{total_hit} 词命中 · {"跨源≥4为确认信号，1-2源为噪音" if level>=1 else ""}</span></div>'
            f'{top_html}'
            f'<table class="tbl" style="margin-top:6px;">{"".join(rows)}</table>'
            f'<table class="tbl-sub" style="margin-top:8px;">'
            f'<tr><td colspan="2" class="td-hdr"><span class="tag">投资级指标说明</span> <span class="sub">9 维量化 · 阈值写死可复算</span></td></tr>'
            f'{ind_rows}</table>'
            f'<div class="ftr">每个格子=一个投资关键词并固定附两个港交所现有 ETF 及对应指数；无纯主题产品时明确标注代理。热度深浅=关注度，颜色=情绪方向；线索列表不展示（完整数据在 /api/heatmap）；{"热度>75为沸腾需防拥挤，" if level>=1 else ""}仅统计信号，不作为投资依据。</div></div>'
        )

    # ── 「AI 政策深度」/「AI 政策研报」两张卡片：政策法规知识库检索（RAG）+ 修饰词术语口径 +
    kb = policy.get("kb") or {}
    modifiers = policy.get("modifiers") or {}
    sentiment = policy.get("sentiment") or {}
    graph = policy.get("graph") or {}
    reasoning = policy.get("reasoning") or {}
    mindmap = policy.get("mindmap") or {}
    models = policy.get("models") or {}
    lstm = models.get("lstm") or {}
    prophet = models.get("prophet") or {}
    research = policy.get("research") or {}
    series_meta = models.get("series") or {}

    def _deep_hdr(label: str, note: str) -> str:
        return (f'<tr><td colspan="2" class="td-hdr"><span class="tag">{_esc(label)}</span> '
                f'<span class="sub">{_esc(note)}</span></td></tr>')

    def _empty_row(text: str) -> str:
        return f'<tr><td colspan="2" class="sub" style="padding:6px 0;">{_esc(text)}</td></tr>'

    def _modifier_summary() -> str:
        """Brief-safe policy-term output: one aggregate sentence, without per-term implications."""
        hits = modifiers.get("all") or modifiers.get("hits") or []
        if not hits:
            return "未命中政策修饰词，术语口径不作判断。"
        try:
            score = float(modifiers.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        bias = modifiers.get("bias") or "中性"
        return f"修饰词口径{bias}（加权强度 {score:+.2f}，命中 {len(hits)} 项）。"

    def _render_policy_deep(level: int) -> str:
        """「AI 政策深度」卡片：近30日全球政策发布对股市的影响，其下保留知识库 / 术语 / 舆情 / 传导 / 推理链作对照。"""
        rows = [policy_impact.render_policy_impact_rows(policy.get("market_impact") or {}, _esc, level)]
        rows.append(_deep_hdr("政策法规知识库检索", kb.get("note") or "先检索历史政策，再做对比分析"))
        for rank, hit in enumerate(kb.get("hits") or [], 1):
            rows.append(
                f'<tr><td class="td-n">{rank:02d}</td><td class="td-t">'
                f'<div><span class="tag">{_esc(hit["date"])}</span> '
                f'<span class="tag-w">{_esc(hit["issuer"])}</span> '
                f'<span class="sub">{_esc(hit["doctype"])} · 匹配度 {hit["score"]} · {_esc(hit["dimension"])}</span></div>'
                f'<div>{_esc(hit["title"])}</div></td></tr>'
            )
        if not (kb.get("hits") or []):
            rows.append(_empty_row(kb.get("note") or "当日无政策维度命中，知识库检索未启动。"))

        rows.append(_deep_hdr("政策术语口径（修饰词解读）", _modifier_summary()))

        overall = sentiment.get("overall") or {}
        official = sentiment.get("official") or {}
        market = sentiment.get("market") or {}
        window = sentiment.get("window") or {}
        rows.append(_deep_hdr("政策舆情情感打分", sentiment.get("note") or "正向 / 中性 / 负向"))
        rows.append(
            f'<tr><td class="td-n">·</td><td class="td-t">'
            f'<div><span class="tag">整体 {_esc(overall.get("label") or "—")}</span> '
            f'<span class="sub">{_esc(overall.get("ratio") or "")} · 情绪分 {overall.get("score", 0):+.2f}</span></div>'
            f'<div class="ev">官媒 / 官方口径：{_esc(official.get("ratio") or "无样本")}'
            f'（{official.get("score", 0):+.2f}）　市场化解读：{_esc(market.get("ratio") or "无样本")}'
            f'（{market.get("score", 0):+.2f}）　预期差 {sentiment.get("gap", 0):+.2f}</div>'
            f'<div class="ev">窗口判断：<span class="hl">{_esc(window.get("label") or "—")}</span> '
            f'{_esc(window.get("note") or "")}</div></td></tr>'
        )
        for item in (sentiment.get("items") or [])[:3 if level > 1 else 1]:
            mark = {"正向": "〔正〕", "负向": "〔负〕"}.get(item.get("label") or "", "〔中〕")
            rows.append(f'<tr><td class="td-n">·</td><td class="td-t"><div class="ev">'
                        f'{"官媒｜" if item.get("official") else ""}{mark}'
                        f'{_trunc(item.get("title") or "", 52)}</div></td></tr>')

        rows.append(_deep_hdr("政策传导图谱", graph.get("note") or "政策主体 → 行业 → 产业链节点"))
        for rank, chain in enumerate(graph.get("chains") or [], 1):
            if level < 2 and rank > 2:
                break
            net_cls = "tag-d" if chain["net"] < 0 else ("tag" if chain["net"] > 0 else "tag-w")
            rows.append(
                f'<tr><td class="td-n">{rank:02d}</td><td class="td-t">'
                f'<div><span class="tag">{_esc(chain["bucket"])}</span> '
                f'<span class="{net_cls}">政策{chain["direction"]}</span> '
                f'<span class="sub">{_esc(chain["subject"])} · {chain["mentions"]} 条提及 · '
                f'{chain["sources"]} 源 · {_esc(chain["nature"])}影响 · {_esc(chain["horizon"])}</span></div>'
                f'<div class="ev">受影响行业：{_esc("、".join(chain["industries"]))}</div>'
                f'<div class="ev">上游 {_esc("、".join(chain["upstream"]))} → 中游 '
                f'{_esc("、".join(chain["midstream"]))} → 下游 {_esc("、".join(chain["downstream"]))}</div>'
                f'</td></tr>'
            )
        if not (graph.get("chains") or []):
            rows.append(_empty_row("当日无政策维度命中，传导图谱未展开。"))

        rows.append(_deep_hdr("分析师推理链", f"四步拆解 · 置信度 {reasoning.get('confidence') or '低'}"))
        for step in reasoning.get("steps") or []:
            evidence_html = "".join(
                f'<div class="ev">· {("知识库 " if ev.get("type") == "kb" else "")}'
                f'{_esc(ev.get("source") or "")}｜{_trunc(ev.get("text") or "", 52)}</div>'
                for ev in (step.get("evidence") or [])[:2 if level > 1 else 1]
            )
            rows.append(f'<tr><td class="td-n">{_esc(step["no"])}</td><td class="td-t">'
                        f'<div><span class="tag">{_esc(step["label"])}</span></div>'
                        f'<div>{_hl_policy(step["text"])}</div>{evidence_html}</td></tr>')

        impact = policy.get("market_impact") or {}
        n_rel = (impact.get("counts") or {}).get("in_window", 0)
        window = impact.get("window_label") or "近30日"
        return (
            f'<div class="card"><div class="hdr"><span class="tag">AI 政策深度</span>'
            f'<span class="sub">近{impact.get("days", 30)}日 · {n_rel} 项已核验 · {window}</span></div>'
            f'<table class="tbl-sub">{"".join(rows)}</table>'
f'<div class="ftr">近30日只统计已核验发文日的全球政策，已观测收盘反应与机制推演分开，不外推未报道的指数点位。'
            f'下方知识库只展示文件元数据与标题，术语口径仅展示汇总判断，舆情按正向 / 中性 / 负向即时打分，均用于对照、不替代窗口内发文。'
            f'数据仅供参考，不作为投资依据。</div></div>'
        )


    def _render_policy_research(level: int) -> str:
        """「AI 政策研报」卡片：思维导图（全量档）+ LSTM / Prophet 式模型 + 结构化政策影响研报。"""
        series_label = (f'样本：{_esc(series_meta.get("name") or "—")} · {series_meta.get("points", 0)} 根日 K · '
                        f'{"合成演示序列（无实时行情）" if series_meta.get("source") == "synthetic" else _esc(series_meta.get("source") or "")}')
        rows = []
        if level > 1:
            rows.append(_deep_hdr("政策影响思维导图", "结构化拆解：根节点 = 当日政策主线"))
            rows.append(f'<tr><td class="td-n">▣</td><td class="td-t">'
                        f'<div>{_esc(mindmap.get("root") or "—")}</div></td></tr>')
            for branch in mindmap.get("branches") or []:
                children = "".join(f'<div class="ev">├─ {_esc(child)}</div>'
                                   for child in branch.get("children") or [])
                rows.append(f'<tr><td class="td-n">·</td><td class="td-t">'
                            f'<div><span class="tag-w">{_esc(branch["label"])}</span></div>{children}</td></tr>')

        rows.append(_deep_hdr("LSTM · 中长期走势节奏", series_label))
        rows.append(
            f'<tr><td class="td-n">L</td><td class="td-t">'
            f'<div>{_esc(lstm.get("summary") or "样本不足未外推")}</div>'
            + (f'<div class="ev">口径：{_esc(lstm.get("model") or "LSTM")} · 隐藏单元 {lstm.get("hidden", "—")} · '
               f'回看 {lstm.get("seq", "—")} 步 · 外推 {lstm.get("horizon", "—")} 日 · '
               f'政策情景输入偏移 {models.get("shock", 0):+.2f}（鹰鸽取向 + 修饰词强度）</div>'
               f'<div class="ev">{_esc(lstm.get("note") or "")}</div>' if level > 1 else "")
            + '</td></tr>'
        )
        rows.append(_deep_hdr("Prophet 式分解 · 剥离季节看政策窗口", "趋势 + 季节性 + 政策突变"))
        rows.append(
            f'<tr><td class="td-n">P</td><td class="td-t">'
            f'<div>{_esc(prophet.get("summary") or "样本不足未分解")}</div>'
            + (f'<div class="ev">口径：{_esc(prophet.get("model") or "Prophet 式加性分解")}</div>'
               f'<div class="ev">{_esc(prophet.get("note") or "")}</div>' if level > 1 else "")
            + '</td></tr>'
        )

        rating = research.get("rating") or "—"
        rating_cls = "tag-d" if "承压" in rating else ("tag" if "利好" in rating else "tag-w")
        rows.append(_deep_hdr("政策影响研报（结构化结论）", research.get("horizon") or ""))
        rows.append(
            f'<tr><td class="td-n">R</td><td class="td-t">'
            f'<div><span class="{rating_cls}">{_esc(rating)}</span> '
            f'<span class="sub">{_esc(research.get("rating_note") or "")}</span></div>'
            f'<div>{_esc(research.get("abstract") or "")}</div></td></tr>'
        )
        for rank, item in enumerate(research.get("long_term") or [], 1):
            if level < 2 and rank > 2:
                break
            cls = "tag-d" if item["direction"] == "净空" else ("tag" if item["direction"] == "净多" else "tag-w")
            rows.append(
                f'<tr><td class="td-n">{rank:02d}</td><td class="td-t">'
                f'<div><span class="tag">长远冲击</span> <span class="{cls}">{_esc(item["direction"])}</span> '
                f'<span class="sub">{_esc(item["nature"])} · {_esc(item["horizon"])}</span></div>'
                f'<div>{_esc(item["industry"])}</div>'
                + (f'<div class="ev">{_esc(item["impact"])}（驱动：{_esc(item["driver"])}）</div>' if level > 1 else "")
                + '</td></tr>'
            )
        risk_html = "".join(f'<div class="ev">· {_esc(risk)}</div>'
                            for risk in (research.get("risks") or [])[:3 if level > 1 else 2])
        rows.append(f'<tr><td class="td-n">!</td><td class="td-t">'
                    f'<div><span class="tag-d">风险提示</span></div>{risk_html}</td></tr>')
        rows.append(f'<tr><td class="td-n">⏱</td><td class="td-t">'
                    f'<div><span class="tag">政策窗口期</span></div>'
                    f'<div class="ev">{_esc(research.get("watch_window") or "—")}</div></td></tr>')

        return (
            f'<div class="card"><div class="hdr"><span class="tag">AI 政策研报</span>'
            f'<span class="sub">{"思维导图 · " if level > 1 else ""}LSTM × Prophet 式分解 · 结构化研报</span></div>'
            f'<table class="tbl-sub">{"".join(rows)}</table>'
            f'<div class="ftr">推理链模拟高级分析师的分步拆解（宏观背景 → 行业限制 → 资金流向 → 受益板块），'
            f'LSTM 捕捉时间序列的非线性与长期依赖、评估政策后的中长期节奏，Prophet 式加性分解剥离季节性后'
            f'度量政策窗口期的中期波动。{_esc(research.get("disclaimer") or "")}</div></div>'
        )

    if review is None:
        review = analyze_ashare()
    if hk_review is None:
        hk_review = analyze_hk()
    if us_review is None:
        us_review = analyze_us()

    def _chip(text: str, color_cls: str = "hl") -> str:
        return f'<span class="{color_cls}">{text}</span>'

    def _hl_review(text: str, block: dict) -> str:
        escaped = _esc(text)
        for item in (block.get("leaders") or []):
            tag = _esc(item["name"])
            escaped = escaped.replace(tag, _chip(tag, "hl"))
        for item in (block.get("laggards") or []):
            tag = _esc(item["name"])
            escaped = escaped.replace(tag, _chip(tag, "hl-d"))
        return escaped

    def _market_block(block: dict, market_name: str, first: bool = False) -> str:
        bias = block.get("bias") or "中性"
        bias_cls = "tag" if bias == "偏多" else ("tag-d" if bias == "偏空" else "tag-w")
        bias_pill = f'<span class="{bias_cls}">{_esc(bias)}</span>'
        wrap = "" if first else " mkt"
        n_idx = max(len(block.get("indices") or []), 1)
        cell_w = f"{100 // n_idx}%"
        index_cells = []
        for cell_index, index in enumerate(block.get("indices") or []):
            pct = index.get("pct") or 0
            arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "·")
            cls_pct = "up" if pct >= 0 else "dn"
            border = "bdr-l" if cell_index else ""
            index_cells.append(
                f'<td class="idx-cell {border}" style="width:{cell_w};">'
                f'<div class="idx-n">{_esc(index["name"])}</div>'
                f'<div class="idx-p {cls_pct}">{arrow}{pct:+.2f}%</div>'
                f'<div class="idx-c">{index["close"]:.2f}</div></td>'
            )
        index_strip = f'<table class="tbl-idx"><tr>{"".join(index_cells)}</tr></table>' if index_cells else ""
        review_rows = []
        for point_index, point in enumerate(block.get("points") or [], 1):
            text = _esc(point["text"])
            if point["key"] in ("sectors", "funds", "outlook"):
                text = _hl_review(point["text"], block)
            review_rows.append(
                f'<tr><td class="td-n">{point_index:02d}</td>'
                f'<td class="td-p"><span class="tag">{_esc(point["label"])}</span> {text}</td></tr>'
            )
        review_rows_html = f'<table class="tbl-sub">' + "".join(review_rows) + "</table>"
        return (
            f'<div class="mkt-block{wrap}">'
            f'<div class="mkt-h"><span class="tag">{_esc(market_name)}</span> {bias_pill}</div>'
            f'<div class="txt">{_hl_review(block.get("headline") or "", block)}</div>'
            f'{index_strip}{review_rows_html}</div>'
        )

    kanpan_card = (
        f'<div class="card"><div class="hdr"><span class="tag">AI 看盘</span>'
        f'<span class="sub">A股 · 港股 · 美股</span></div>'
        f'{_market_block(review, "A 股", first=True)}'
        f'{_market_block(hk_review, "港股")}'
        f'{_market_block(us_review, "美股")}</div>'
    )

    # 「全网快讯」列表之后只保留免责声明与作者署名：原先的「调研方法」说明段
    # （多模型协同推理、模型清单那一大段注解文字）已按要求移除，推送内容与本地页面都不再展示。
    author = "作者：章鱼 ai　　仅供参考，分析研究"

    # 参考截图的两个标志性构件：分段进度条（前两段柠檬绿）+ 8×3 点阵矩阵（点亮一簇）。
    seg_html = '<div class="seg"><i class="on"></i><i class="on"></i><i></i><i></i><i></i></div>'
    _mtx_on = {12, 13, 14, 19, 20, 21, 22}
    mtx_html = ('<div class="mtx">' + "".join(
        '<i class="on"></i>' if i in _mtx_on else '<i></i>' for i in range(24)) + '</div>')

    css = (
        f'<style>body,td,div,span,a{{font-family:Arial,\'PingFang SC\',\'Microsoft YaHei\',\'Noto Sans SC\',sans-serif;box-sizing:border-box;}}'
        f'.bg{{width:100%;max-width:100%;margin:0;padding:10px;background:{paper};color:{ink};word-break:break-word;}}'
        f'.card{{margin:0 0 10px;background:{paper_lift};border:1px solid {rule};border-radius:10px;padding:9px 11px 7px;}}'
        f'.card-m{{margin:0 0 10px;background:{paper_lift};border:1px solid {rule};border-radius:10px;padding:11px 12px;color:{ink};}}'
        f'.seg{{display:flex;gap:4px;margin:0 0 9px;}}.seg i{{flex:1;height:5px;border-radius:3px;background:{gray_light};}}.seg i.on{{background:{neon_green};}}'
        f'.cm-row{{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;}}.cm-body{{flex:1;min-width:170px;}}'
        f'.mtx{{display:grid;grid-template-columns:repeat(8,8px);gap:3px;}}.mtx i{{width:8px;height:8px;border-radius:2px;background:{gray_light};}}.mtx i.on{{background:{neon_green};}}'
        f'.hdr{{display:flex;justify-content:space-between;align-items:center;gap:8px;padding-bottom:6px;border-bottom:1px solid {rule};}}'
        f'.tag{{color:{black};background:{neon_green};padding:2px 5px;font-size:10px;font-weight:700;border-radius:3px;}}'
        f'.tag-d{{color:#6b6b6b;background:#e8e8e8;padding:2px 5px;font-size:10px;font-weight:700;border-radius:3px;}}'
        f'.tag-w{{color:{black};background:#ffffff;padding:2px 5px;font-size:10px;font-weight:700;border:1px solid {rule};border-radius:3px;}}'
        f'.sub{{color:{muted};font-size:10px;}}'
        f'.tbl{{width:100%;border-collapse:collapse;margin-top:4px;}}'
        f'.tbl-sub{{width:100%;border-collapse:collapse;margin-top:6px;border-top:1px solid {rule};}}'
        f'.tbl-idx{{width:100%;border-collapse:separate;border-spacing:0;margin-top:6px;background:#ffffff;border:1px solid {rule};border-radius:6px;}}'
        f'.td-n{{width:22px;vertical-align:top;padding:4px 4px 4px 0;color:#9a9a9a;font-size:11px;}}'
        f'.td-t{{vertical-align:top;padding:4px 0;color:{ink};font-size:12px;line-height:1.5;word-break:break-all;}}'
        f'.td-p{{padding:4px 0 1px;color:{ink};font-size:12px;line-height:1.5;word-break:break-all;}}'
        f'.td-hdr{{padding:6px 0 1px;}}'
        f'.td-bdr{{border-bottom:1px solid {rule};}}'
        f'.ev{{margin:2px 0 0 12px;color:{muted};font-size:11px;word-break:break-all;}}'
        f'.lnk{{color:{ink};text-decoration:underline;text-decoration-color:{black};}}'
        f'.hl{{color:{black};background:{neon_green};padding:1px 3px;border-radius:3px;font-weight:700;}}'
        f'.hl-d{{color:#6b6b6b;background:#e8e8e8;padding:1px 3px;border-radius:3px;font-weight:700;}}'
        f'.txt{{margin-top:4px;color:{ink};font-size:13px;line-height:1.5;word-break:break-all;}}'
        f'.ftr{{margin-top:6px;padding-top:4px;border-top:1px solid {rule};color:{muted};font-size:10px;}}'
        f'.idx-cell{{text-align:center;width:25%;padding:5px 2px;}}'
        f'.idx-n{{color:{danger_hi};font-size:10px;}}'
        f'.idx-p{{font-size:13px;font-weight:700;}}'
        f'.idx-c{{color:{ink};font-size:10px;}}'
        f'.up{{color:{up_text};}}.dn{{color:{danger_hi};}}'
        f'.bdr-l{{border-left:1px solid {rule};}}'
        f'.mkt{{margin-top:8px;padding-top:6px;border-top:1px solid {rule};}}'
        f'.mkt-h{{margin:2px 0 4px;}}'
        f'</style>'
    )

    def _news_card(per_source: int) -> str:
        """正文最后的「全网快讯」列表：按板块分组，每源最多 ``per_source`` 条，只显示标题、隐藏来源。

        - 按「板块 → 数据源」顺序取每源前 ``per_source`` 条，再跨源去重（同一条新闻被多源转载只留一条）；
        - 每个板块一行小标题（财经快讯 / 热搜热点 / 政策发布 · 官方信息源 / 全球政经媒体 / 公民科技 · 政治透明度），
          条目编号全表连续；板块名不是任何数据源名称，仍不暴露源头；
        - 不输出来源名称、也不带跳转链接，避免任何形式暴露源头；
        - ``per_source`` 为 0 或当天没有任何快讯时整段省略，不占推送字符额度。
        """
        if per_source <= 0:
            return ""
        # 未登记的源（调用方自定义 brief）归到最后一个「其他」分组，仍匿名列出。
        grouped = [(sec["label"], [n for n in sources_in_section(sec["key"]) if n in brief]) for sec in SECTIONS]
        extra = [name for name in brief if name not in SOURCES]
        if extra:
            grouped.append(("其他", extra))
        rows, seen, total = [], set(), 0
        for label, names in grouped:
            body = []
            for name in names:
                for item in (brief.get(name) or [])[:per_source]:
                    title = str((item or {}).get("title") or "").strip()
                    if not title:
                        continue
                    key = _news_key(title)
                    if key in seen:
                        continue
                    seen.add(key)
                    total += 1
                    body.append(
                        f'<tr><td class="td-n">{total:02d}</td>'
                        f'<td class="td-t">{_hl(_trunc(title, 60))}</td></tr>'
                    )
            if body:
                rows.append(f'<tr><td colspan="2" class="td-hdr"><span class="tag">{_esc(label)}</span> '
                            f'<span class="sub">{len(body)} 条</span></td></tr>')
                rows.extend(body)
        if not total:
            return ""
        used = sum(1 for _label, names in grouped if names)
        return (
            f'<div class="card"><div class="hdr"><span class="tag">全网快讯</span>'
            f'<span class="sub">境内 × 境外 · {used} 个板块 · 每源精选 {per_source} 条 · 共 {total} 条</span></div>'
            f'<table class="tbl">{"".join(rows)}</table>'
            f'<div class="ftr">按「板块 → 数据源」顺序取每源前 {per_source} 条、跨源去重后分组列出，不标注具体来源。</div></div>'
        )

    def _render_full(max_per_src: int | None, deep_level: int = 2) -> str:
        # 分析栏目 + 正文最后的「全网快讯」列表；监测平台清单与数量遥测不进入正文。
        # ``max_per_src`` 为本次容量口径允许的每源条数，快讯列表最多 3 条，
        # 容量不够时随收敛档位降到 2 / 1 条，为 0 时整段省略。
        # ``deep_level``：2 = 政策深度层全量（含思维导图与背景知识长文），1 = 精简，0 = 整段省略。
        per_source = (NEWS_ITEMS_PER_SOURCE if max_per_src is None
                      else min(max_per_src, NEWS_ITEMS_PER_SOURCE))
        policy_deep_cards = ((_render_policy_deep(deep_level) + _render_policy_research(deep_level))
                             if deep_level > 0 else "")
        heatmap_card = _render_heatmap(deep_level)
        return (
            f'{css}<div class="bg">'
            f'<div class="card-m">{seg_html}'
            f'<div class="cm-row"><div class="cm-body">'
            f'<div style="color:{muted};font-size:10px;margin-bottom:3px;">全网 AI 调研　/　境内 × 境外</div>'
            f'<div style="color:{ink};font-size:20px;font-weight:800;">章鱼 AI·全景分析（市场情绪量化分析）</div>'
            f'<div style="color:{muted};font-size:11px;margin-top:4px;">市场情绪量化分析 · 全网 AI 调研境内外数据，由多个大模型混合部署。</div>'
            f'</div>{mtx_html}</div></div>'
            f'<div class="card"><div class="hdr"><span class="tag">AI 每日总结</span></div><div class="txt">{headline}</div>{points_html}</div>'
            + kanpan_card
            + heatmap_card
            + policy_card
            + policy_deep_cards
            + opportunities_card
            + _news_card(per_source)
            + f'<div style="margin:8px 0 0;color:{muted};font-size:10px;text-align:center;">数据仅供参考，不构成投资建议</div>'
            + f'<div style="margin:6px 0 0;padding:6px 4px 0;border-top:1px solid {rule};color:{ink};font-size:10px;text-align:center;font-weight:700;">{_esc(author)}</div>'
            + '</div>'
        )

    # 容量收敛顺序：先降「政策深度 / 政策研报」档位（派生内容），再逐级收敛快讯列表，
    # 最后才整段省略快讯——「AI 每日总结 / 看盘 / 政策分析 / 板块机会」四个主卡片始终保留，
    # 保证任何账号口径（会员 10 万字 / 普通 2 万字）都能发得出去。
    cap = (NEWS_ITEMS_PER_SOURCE if max_items_per_source is None
           else min(max_items_per_source, NEWS_ITEMS_PER_SOURCE))
    # 每一档内先把快讯列表从 3 条收敛到 1 条，再降政策深度层的档位；快讯整段省略是最后一步。
    plans = [(limit, level) for level in (2, 1, 0) for limit in range(cap, 0, -1)]
    plans.append((0, 0))
    out = _render_full(*plans[0])
    for plan in plans[1:]:
        if len(out) <= max_length:
            break
        out = _render_full(*plan)

    return out

if __name__ == "__main__":
    print(f"开始抓取 {len(SOURCES)} 个数据源…")
    data = collect_all()
    for name, items in data.items():
        print(f"  ✓ {name}: {len(items)} 条")
    print()
    print(build_html(data)[:600])
