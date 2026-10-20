#!/usr/bin/env python3
"""sources.py 单元测试：链接解析、源定义、兜底数据、HTML 渲染。

零第三方依赖，直接运行：
    python3 test_sources.py
"""
import math
import os
import random
import unittest
from datetime import date

import sources


# 模拟 rebang.vip 聚合页的服务端渲染结构：<a href> 指回源头站 + 内部导航噪音。
_SAMPLE_HTML = """
<html><body>
<nav><a href="https://www.rebang.vip/">首页</a>
<a href="https://www.rebang.vip/weibo/hot-search">微博热搜</a></nav>
<section>
<a href="https://www.cls.cn/detail/2445436">19分钟前<br/>L3、L4级自动驾驶“强标”明年7月实施</a>
<a href="https://www.cls.cn/detail/2445625">25分钟前 央行将开展5000亿元买断式逆回购</a>
<a href="https://www.cls.cn/detail/2445058">1小时前 日元又成了全世界的问题</a>
</section>
</body></html>
"""


class LinkCollectorTest(unittest.TestCase):
    def test_collects_anchors(self):
        parser = sources.LinkCollector()
        parser.feed(_SAMPLE_HTML)
        hrefs = [href for href, _ in parser.links]
        self.assertIn("https://www.cls.cn/detail/2445436", hrefs)
        self.assertIn("https://www.rebang.vip/weibo/hot-search", hrefs)

    def test_clean_title_strips_time_prefix(self):
        self.assertEqual(sources._clean_title("19分钟前 L3、L4级自动驾驶“强标”"), "L3、L4级自动驾驶“强标”")
        self.assertEqual(sources._clean_title("1小时前 标题"), "标题")
        self.assertEqual(sources._clean_title("无前缀标题"), "无前缀标题")


class ParseChannelTest(unittest.TestCase):
    def test_parse_channel_filters_by_origin(self):
        # 用本地样本验证：只保留指向 cls.cn 的条目，忽略导航噪音，且去重截断。
        parser = sources.LinkCollector()
        parser.feed(_SAMPLE_HTML)
        out = sources._filter_items(parser.links, origin="cls.cn", limit=10)
        self.assertEqual(len(out), 3)
        self.assertTrue(all("cls.cn" in it["url"] for it in out))
        self.assertNotIn("微博热搜", [it["title"] for it in out])
        # 截断生效
        self.assertEqual(len(sources._filter_items(parser.links, "cls.cn", limit=2)), 2)


class SourceDefinitionTest(unittest.TestCase):
    def test_sixty_nine_sources_in_six_sections(self):
        # 原 49 源 + 20 个财经社区；社区覆盖中英文及多种投资类型。
        self.assertEqual(len(sources.SOURCES), 69)
        self.assertEqual(len(sources.SOURCE_META), 69)
        self.assertEqual(len(set(sources.SOURCES)), 69, "数据源名称必须唯一")
        names = [m["name"] for m in sources.SOURCE_META]
        self.assertEqual(names, sources.SOURCES)
        counts = {sec["key"]: sec["count"] for sec in sources.section_catalog()}
        self.assertEqual(counts, {"finance": 12, "trending": 6, "community": 20,
                                  "policy": 15, "world": 12, "civic": 4})

    def test_twenty_communities_are_bilingual_and_diverse(self):
        community = [m for m in sources.SOURCE_META if m.get("section") == "community"]
        self.assertEqual(len(community), 20)
        self.assertEqual({m.get("language") for m in community}, {"zh", "en"})
        self.assertEqual(sum(m.get("language") == "zh" for m in community), 8)
        self.assertEqual(sum(m.get("language") == "en" for m in community), 12)
        self.assertGreaterEqual(len({m.get("community_type") for m in community}), 18)
        self.assertTrue(all(m.get("community_type") for m in community))

    def test_every_source_has_a_fetch_method(self):
        for meta in sources.SOURCE_META:
            # 四选一：rebang 聚合通道（origin+channel）/ 自定义收集器 / RSS·Atom 订阅 / 列表页+正则。
            has_channel = "channel" in meta and "origin" in meta
            has_collector = "collector" in meta and meta["collector"] in sources._COLLECTORS
            has_feed = "feed" in meta
            has_page = "page" in meta and "pattern" in meta
            self.assertTrue(has_channel or has_collector or has_feed or has_page,
                            f"{meta['name']} 缺少抓取方式")
            self.assertIn(meta.get("section"), sources.SECTION_KEYS, f"{meta['name']} 板块归属无效")

    def test_demo_fallback(self):
        for name in sources.SOURCES:
            items = sources._demo_items(name)
            self.assertGreaterEqual(len(items), 1, name)
            self.assertTrue(items[0]["title"])

    def test_collect_one_falls_back_offline(self):
        # 无外网环境（如 CI 沙箱）也能返回兜底数据。
        items = sources.collect_one("金十数据")
        self.assertGreaterEqual(len(items), 1)
        # 新增的源也应该有兜底数据
        items = sources.collect_one("知乎热榜")
        self.assertGreaterEqual(len(items), 1)


_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Bloomberg Politics</title>
<item><title><![CDATA[US Reaches Greenland Deal to End Row That Threatened NATO]]></title>
<link>https://www.bloomberg.com/news/articles/2026-09-19/greenland</link></item>
<item><title>China Slams US Law Tightening Sanctions on Russia and Iran</title>
<link>https://www.bloomberg.com/news/articles/2026-09-19/sanctions</link></item>
<item><title>China Slams US Law Tightening Sanctions on Russia and Iran</title>
<link>https://www.bloomberg.com/news/articles/2026-09-19/sanctions-dup</link></item>
<item><title>German general Breuer elected to head top NATO military body - Reuters</title>
<link>https://news.google.com/rss/articles/abc</link></item>
</channel></rss>"""

_SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>HM Treasury - Activity on GOV.UK</title>
<entry><title>Chancellor sets date for Autumn Budget 2026</title>
<link rel="alternate" type="text/html" href="https://www.gov.uk/government/news/autumn-budget-2026"/></entry>
<entry><title>Pensions investment reform: consultation response</title>
<link rel="self" href="https://www.gov.uk/self"/>
<link rel="alternate" href="https://www.gov.uk/government/consultations/pensions"/></entry>
</feed>"""

# 模拟 gov.cn「最新政策」列表页：相对链接 + 同一条目上的短锚文本 + 栏目导航噪音。
_SAMPLE_GOV_HTML = """
<html><body>
<a href="https://www.gov.cn/zhengce/">最新政策</a>
<ul>
<li><a href="./202609/content_7081441.htm">国务院办公厅转发文化和旅游部等部门《关于促进房车消费的若干措施》的通知</a>
    <a href="./202609/content_7081441.htm">解读</a></li>
<li><a href="/zhengce/content/202609/content_7081356.htm">国务院办公厅关于进一步加强烟花爆竹全链条安全监管的意见</a></li>
<li><a href="https://www.gov.cn/zhengce/jiedu/202609/content_7081473.htm">解读：国务院常务会议部署实施医疗康复护理扩容提升工程</a></li>
<li><a href="https://www.gov.cn/xinwen/2026/other.htm">不是政策条目的其他链接</a></li>
</ul>
</body></html>
"""


class SectionTest(unittest.TestCase):
    """六大板块：板块目录、按板块抓取、通用 RSS·Atom / 列表页抓取器、公民科技 JSON 解析、快讯分组渲染。"""

    def test_section_catalog(self):
        catalog = sources.section_catalog()
        self.assertEqual([sec["key"] for sec in catalog],
                         ["finance", "trending", "community", "policy", "world", "civic"])
        self.assertEqual(sum(sec["count"] for sec in catalog), len(sources.SOURCES))
        labels = [sec["label"] for sec in catalog]
        # 板块名不能与任何数据源名相同，否则「全网快讯」分组标题会暴露来源。
        for label in labels:
            self.assertNotIn(label, sources.SOURCES)
        self.assertEqual(sources.section_of("国务院 最新政策"), "policy")
        self.assertEqual(sources.section_of("Bloomberg 政治"), "world")
        self.assertEqual(sources.section_of("Reddit r/investing"), "community")
        self.assertEqual(sources.section_of("g0v 立法院議案"), "civic")
        self.assertEqual(sources.section_of("金十数据"), "finance")
        self.assertEqual(sources.section_of("未登记的源"), "finance")

    def test_collect_section_falls_back_offline(self):
        # 无外网环境按板块抓取也能整板块回退演示数据，且顺序与 SOURCE_META 一致。
        out = sources.collect_section("policy")
        self.assertEqual(list(out), sources.sources_in_section("policy"))
        self.assertTrue(all(len(items) >= 1 for items in out.values()))
        self.assertEqual(sources.collect_section("no-such-section"), {})

    def test_collect_all_keeps_source_order_when_parallel(self):
        os.environ["BRIEF_FETCH_WORKERS"] = "6"
        try:
            self.assertEqual(sources.fetch_workers(), 6)
            out = sources.collect_all()
        finally:
            os.environ.pop("BRIEF_FETCH_WORKERS", None)
        self.assertEqual(list(out), sources.SOURCES)
        self.assertTrue(all(len(items) >= 1 for items in out.values()))
        self.assertEqual(sources.fetch_workers(), sources.FETCH_WORKERS_DEFAULT)

    def test_parse_feed_rss_dedupes_and_strips_suffix(self):
        items = sources._parse_feed(_SAMPLE_RSS, limit=10, strip_suffix=" - Reuters")
        titles = [it["title"] for it in items]
        self.assertEqual(titles, [
            "US Reaches Greenland Deal to End Row That Threatened NATO",
            "China Slams US Law Tightening Sanctions on Russia and Iran",
            "German general Breuer elected to head top NATO military body",
        ])
        self.assertEqual(items[0]["url"], "https://www.bloomberg.com/news/articles/2026-09-19/greenland")
        self.assertEqual(len(sources._parse_feed(_SAMPLE_RSS, limit=1)), 1)
        # bytes 入参（真实抓取按声明 encoding 解码）同样可用。
        self.assertEqual(len(sources._parse_feed(_SAMPLE_RSS.encode("utf-8"), limit=10)), 3)

    def test_parse_feed_atom_prefers_alternate_link(self):
        items = sources._parse_feed(_SAMPLE_ATOM, limit=10)
        self.assertEqual([it["title"] for it in items], [
            "Chancellor sets date for Autumn Budget 2026",
            "Pensions investment reform: consultation response",
        ])
        self.assertEqual(items[0]["url"], "https://www.gov.uk/government/news/autumn-budget-2026")
        self.assertEqual(items[1]["url"], "https://www.gov.uk/government/consultations/pensions")

    def test_parse_feed_rejects_garbage(self):
        with self.assertRaises(sources.ET.ParseError):
            sources._parse_feed("<html><body>not a feed", limit=5)
        # collect_one 会把解析失败吞掉并回退演示数据。
        self.assertTrue(issubclass(sources.ET.ParseError, sources._FETCH_ERRORS))

    def test_filter_page_links_resolves_relative_and_skips_nav(self):
        parser = sources.LinkCollector()
        parser.feed(_SAMPLE_GOV_HTML)
        items = sources._filter_page_links(parser.links, sources._GOVCN_PATTERN,
                                           "https://www.gov.cn/zhengce/zuixin/", limit=10)
        urls = [it["url"] for it in items]
        self.assertEqual(urls, [
            "https://www.gov.cn/zhengce/zuixin/202609/content_7081441.htm",
            "https://www.gov.cn/zhengce/content/202609/content_7081356.htm",
            "https://www.gov.cn/zhengce/jiedu/202609/content_7081473.htm",
        ])
        titles = [it["title"] for it in items]
        self.assertNotIn("解读", titles)          # 短锚文本不当标题
        self.assertNotIn("最新政策", titles)      # 栏目导航不匹配条目正则
        self.assertEqual(len(sources._filter_page_links(parser.links, sources._GOVCN_PATTERN,
                                                        "https://www.gov.cn/zhengce/zuixin/", limit=2)), 2)

    def test_ministry_patterns_match_real_link_shapes(self):
        meta = {m["name"]: m for m in sources.SOURCE_META}
        cases = {
            "发改委 政策发布": "https://www.ndrc.gov.cn/xxgk/zcfb/tz/202609/t20260917_1407686.html",
            "财政部 政策发布": "http://gks.mof.gov.cn/guizhangzhidu/202609/t20260911_3997286.htm",
            "商务部 政策发布": "https://www.mofcom.gov.cn/zwgk/zcfb/art/2026/art_98f578b88d3f47538d7b516745faf230.html",
            "证监会 新闻发布": "https://www.csrc.gov.cn/csrc/c100028/c7659506/content.shtml",
        }
        import re
        for name, url in cases.items():
            self.assertTrue(re.search(meta[name]["pattern"], url), name)
        # 解读 / 旧栏目链接不算条目。
        self.assertFalse(re.search(meta["发改委 政策发布"]["pattern"], "https://www.ndrc.gov.cn/xxgk/jd/jd/202609/t20260917_1.html"))
        self.assertFalse(re.search(meta["证监会 新闻发布"]["pattern"], "https://www.csrc.gov.cn/csrc/c100029/c7473708/content.shtml"))

    def test_chinanews_policy_source(self):
        # 中国新闻社 时政：政策板块境内的官媒 RSS 源（中新社时政新闻官方订阅）。
        meta = sources.source_meta("中国新闻社 时政")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["section"], "policy")
        self.assertEqual(meta["feed"], "https://www.chinanews.com.cn/rss/china.xml")
        # 官媒口径：舆情情感打分里计入「官媒 / 官方口径」，与市场化解读分开统计。
        self.assertTrue(sources._is_official_voice("中国新闻社 时政", ""))
        # 兜底标题至少一条命中政策维度（监管 / 规划类政策新闻），离线也有政策信号。
        demos = [it["title"] for it in sources._demo_items("中国新闻社 时政")]
        self.assertGreaterEqual(len(demos), 1)
        self.assertTrue(any(sources._policy_tags(sources._policy_norm(t)) for t in demos))

    def test_decode_honours_meta_charset(self):
        gbk_page = '<html><head><meta charset="gb2312"></head><body>财政部 政策发布</body></html>'.encode("gb18030")
        self.assertIn("财政部 政策发布", sources._decode(gbk_page))
        self.assertIn("财政部", sources._decode("财政部".encode("utf-8"), "utf-8"))
        self.assertIn("财政部", sources._decode("财政部".encode("utf-8"), "no-such-charset"))

    def test_format_ly_bills(self):
        data = {"bills": [
            {"議案名稱": "「食品安全衛生管理法部分條文修正草案」，請審議案。", "議案狀態": "排入院會",
             "提案單位/提案委員": "本院委員陳菁徽等17人", "url": "https://ppg.ly.gov.tw/ppg/bills/202110231310000/details"},
            {"議案名稱": "", "議案狀態": "x"},
            {"議案名稱": "Ａ" * 120, "url": ""},
        ]}
        items = sources._format_ly_bills(data, limit=10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "〔排入院會〕「食品安全衛生管理法部分條文修正草案」，請審議案。（本院委員陳菁徽等17人）")
        self.assertEqual(items[0]["url"], "https://ppg.ly.gov.tw/ppg/bills/202110231310000/details")
        self.assertTrue(items[1]["title"].endswith("…"))
        self.assertEqual(sources._format_ly_bills({}, limit=5), [])

    def test_format_equitystack_promises_sorted_by_latest_action(self):
        data = {"items": [
            {"slug": "old", "title": "Older promise", "status": "Partial", "topic": "Education",
             "president": "Donald J. Trump", "promise_date": "2025-01-20", "latest_action_date": "2025-04-23"},
            {"slug": "new", "title": "Newer promise", "status": "Failed", "topic": "Courts",
             "president": "Donald J. Trump", "promise_date": "2026-04-29", "latest_action_date": "2026-04-29"},
            {"slug": "none", "title": ""},
        ]}
        items = sources._format_equitystack_promises(data, limit=10)
        self.assertEqual([it["url"] for it in items],
                         ["https://equitystack.org/promises/new", "https://equitystack.org/promises/old"])
        self.assertEqual(items[0]["title"], "[Failed] Newer promise（Courts · Donald J. Trump）")
        # 裸数组响应也兼容。
        self.assertEqual(len(sources._format_equitystack_promises(data["items"], limit=1)), 1)

    def test_format_equitystack_bills(self):
        data = [{"title": "Reparations study commission", "status": "Idea", "tracked_bills": [
            {"bill_number": "H.R. 40", "title": "Commission to Study and Develop Reparation Proposals for African Americans Act",
             "status": "In Committee", "latest_action": "Referred to the House Committee on the Judiciary.",
             "url": "https://www.congress.gov/bill/118th-congress/hr-bill/40"}]},
            {"title": "Idea without tracked bill", "status": "Idea", "tracked_bills": []}]
        items = sources._format_equitystack_bills(data, limit=10)
        self.assertEqual(items[0]["title"],
                         "H.R. 40｜Commission to Study and Develop Reparation Proposals for African Americans Act"
                         "（In Committee · Referred to the House Committee on the Judiciary.）")
        self.assertEqual(items[0]["url"], "https://www.congress.gov/bill/118th-congress/hr-bill/40")
        self.assertEqual(items[1]["title"], "Idea without tracked bill（Idea）")

    def test_english_titles_feed_theme_policy_and_direction(self):
        # 英文政策 / 媒体标题：主题按单词边界归类（AI 不误命中 SAID / AIR），多空与鹰鸽同样计票。
        ana = sources.analyze_brief({
            "Bloomberg 政治": [
                {"title": "China Slams US Law Tightening Sanctions on Russia and Iran", "url": ""},
                {"title": "Saudi Arabia Issues Rare Air-Raid Alerts for Riyadh", "url": ""},
            ],
            "美联储 新闻稿": [{"title": "Federal Reserve issues FOMC statement", "url": ""}],
            "Financial Times": [{"title": "Gold surges to record high as dollar slides", "url": ""}],
        })
        tags = dict((t[0], t) for t in ana["top_themes"])
        self.assertIn("地缘", tags)
        self.assertIn("美联储", tags)
        self.assertIn("贵金属", tags)
        self.assertNotIn("AI 算力", tags)
        self.assertIn("贵金属", ana["sectors_up"])
        self.assertGreaterEqual(ana["signals"], 2)
        policy = ana["policy"]
        bucket_tags = {b["tag"] for b in policy["buckets"]}
        self.assertIn("地缘与贸易政策", bucket_tags)
        self.assertIn("货币政策 · 美联储", bucket_tags)
        self.assertEqual(policy["stance"], "偏鹰")
        # 板块口径：三条来自两个板块。
        secs = {s["key"]: s for s in ana["sections"]}
        self.assertEqual(secs["world"]["items"], 3)
        self.assertEqual(secs["world"]["sources"], 2)
        self.assertEqual(secs["policy"]["items"], 1)

    def test_demo_policy_sources_hit_policy_buckets(self):
        brief = {name: sources._demo_items(name) for name in sources.sources_in_section("policy")}
        p = sources.analyze_policy(brief)
        self.assertGreaterEqual(p["sources_hit"], 10)
        self.assertGreaterEqual(p["mentions"], 30)

    def test_news_card_groups_by_section(self):
        brief = {
            "金十数据": [{"title": "金十快讯标题一", "url": ""}],
            "国务院 最新政策": [{"title": "国务院办公厅关于加强中小企业回款难问题治理的通知", "url": ""}],
            "Bloomberg 政治": [{"title": "US Reaches Greenland Deal to End Row That Threatened NATO", "url": ""}],
            "外部投稿": [{"title": "调用方额外传入的一条标题", "url": ""}],
        }
        out = sources.build_html(brief, **PolicySectionTest._kanpan())
        card = out[out.index("全网快讯"):]
        for label in ("财经快讯", "政策发布 · 官方信息源", "全球政经媒体", "其他"):
            self.assertIn(label, card)
        self.assertNotIn("热搜热点", card)     # 没有内容的板块不出现
        self.assertIn("4 个板块", card)
        self.assertIn("共 4 条", card)
        # 编号全表连续，来源名不出现。
        self.assertLess(card.index("金十快讯标题一"), card.index("国务院办公厅关于"))
        self.assertLess(card.index("国务院办公厅关于"), card.index("Greenland"))
        self.assertIn(">04</td>", card)
        for name in ("金十数据", "国务院 最新政策", "Bloomberg 政治", "外部投稿"):
            self.assertNotIn(name, card)


class AnalyzeBriefTest(unittest.TestCase):
    def _brief(self, titles, sources=("金十数据",)):
        return {name: [{"title": t, "url": ""} for t in titles] for name in sources}

    def test_returns_expected_fields(self):
        ana = sources.analyze_brief(self._brief(["现货铂金上涨超过7%", "标普500涨幅扩大至1.5%"]))
        for key in ("headline", "bias", "bull", "bear", "sectors", "top_themes"):
            self.assertIn(key, ana)
        self.assertEqual(ana["bull"] + ana["bear"], 100)
        self.assertIn(ana["bias"], ("偏多", "偏空", "中性"))

    def test_bullish_input_is_pianduo(self):
        ana = sources.analyze_brief(self._brief(["标普500涨幅扩大至1.5%", "现货铂金上涨超过7%", "云业务积压订单暴增150%"]))
        self.assertEqual(ana["bias"], "偏多")
        self.assertGreater(ana["bull"], ana["bear"])

    def test_bearish_input_is_piankong(self):
        ana = sources.analyze_brief(self._brief(["债务逾期、25起诉讼待解", "美股暴跌风险警示", "公司被立案面临多重危机"]))
        self.assertEqual(ana["bias"], "偏空")
        self.assertLess(ana["bull"], ana["bear"])

    def test_theme_detection(self):
        ana = sources.analyze_brief(self._brief(["OpenAI 发布 GPT-5 技术预览版", "Anthropic 数据中心大额投资"]))
        tags = [t[0] for t in ana["top_themes"]]
        self.assertIn("AI 算力", tags)

    def test_headline_references_top_theme(self):
        ana = sources.analyze_brief(self._brief(["OpenAI 发布 GPT-5 技术预览版", "AI 编程助手效率对比评测"]))
        self.assertTrue(any(tag in ana["headline"] for tag in ana["sectors"]))

    def test_empty_brief_is_neutral(self):
        ana = sources.analyze_brief({})
        self.assertEqual(ana["bias"], "中性")
        self.assertEqual((ana["bull"], ana["bear"]), (50, 50))

    def test_demo_brief_has_nonempty_summary(self):
        brief = {name: sources._demo_items(name) for name in sources.SOURCES}
        ana = sources.analyze_brief(brief)
        self.assertTrue(ana["headline"])
        self.assertTrue(ana["sectors"])

    def test_four_viewpoints_present(self):
        # 开篇四个观点：市场情绪 / 多空博弈概率 / 利好·利差板块 / 资金流向分析。
        ana = sources.analyze_brief(self._brief(["标普500涨幅扩大至1.5%", "现货铂金上涨超过7%"]))
        self.assertEqual(
            [p["label"] for p in ana["points"]],
            ["市场情绪", "多空博弈概率", "利好 / 利差板块", "资金流向分析"],
        )
        self.assertTrue(all(p["text"] for p in ana["points"]))
        for key in ("sectors_up", "sectors_down", "flow"):
            self.assertIn(key, ana)

    def test_battle_point_has_percent_and_sample_size(self):
        ana = sources.analyze_brief(self._brief(["标普500涨幅扩大至1.5%", "美股暴跌风险警示"]))
        battle = next(p["text"] for p in ana["points"] if p["key"] == "battle")
        self.assertIn("多方", battle)
        self.assertIn("%", battle)
        self.assertIn("多空信号", battle)

    def test_bullish_and_bearish_sectors_split(self):
        titles = [
            "AI 算力投资创纪录，数据中心扩产超预期",
            "光模块订单暴增，光通信景气度回升",
            "地产股债务逾期风险警示，房价承压下跌",
        ]
        ana = sources.analyze_brief(self._brief(titles))
        self.assertIn("AI 算力", ana["sectors_up"])
        self.assertNotIn("地产", ana["sectors_up"])
        self.assertIn("地产", ana["sectors_down"])
        self.assertIn("地产", ana["flow"])
        self.assertIn("回避", ana["flow"])

    def test_empty_brief_points_are_honest(self):
        ana = sources.analyze_brief({})
        self.assertEqual(ana["sectors_up"], [])
        self.assertEqual(ana["sectors_down"], [])
        battle = next(p["text"] for p in ana["points"] if p["key"] == "battle")
        self.assertIn("暂无", battle)


class HKEtfPairTest(unittest.TestCase):
    """每个 AI 关键词都只能输出两个现有港股 ETF，并带对应指数 / 参考标的。"""

    def test_every_theme_keyword_has_exactly_two_hk_etfs(self):
        for tag, keywords in sources._THEMES:
            for raw in keywords:
                keyword = sources._heatmap_display(raw)
                pair = sources.hk_etf_pair(keyword, tag)
                self.assertEqual(len(pair), 2, (tag, keyword))
                self.assertEqual(len({item["code"] for item in pair}), 2, (tag, keyword))
                for item in pair:
                    self.assertRegex(item["code"], r"^\d{5}\.HK$")
                    self.assertIn(item["code"], sources._HK_ETF_CATALOG)
                    self.assertTrue(item["name"])
                    self.assertTrue(item["benchmark"])
                    self.assertTrue(item["mapping"])

    def test_precise_keyword_overrides(self):
        self.assertEqual([x["code"] for x in sources.hk_etf_pair("原油", "贵金属")],
                         ["03097.HK", "03175.HK"])
        self.assertEqual([x["code"] for x in sources.hk_etf_pair("数据中心", "AI 算力")],
                         ["02826.HK", "03140.HK"])
        self.assertIn("暂无现行纯白银 ETF",
                      sources.hk_etf_pair("白银", "贵金属")[0]["mapping"])

    def test_heatmap_returns_structured_pair_not_a_share_stocks(self):
        heatmap = sources.analyze_keyword_heatmap({"金十数据": [
            {"title": "AI 数据中心投资上涨，原油价格回升", "url": ""},
            {"title": "半导体芯片需求超预期", "url": ""},
        ]})
        self.assertTrue(heatmap["keywords"])
        legacy_stocks = ("浪潮信息", "中际旭创", "科大讯飞", "北方华创", "贵州茅台")
        for keyword in heatmap["keywords"]:
            self.assertEqual(len(keyword["etf_pair"]), 2)
            self.assertIn("港股 ETF 双标的", keyword["concept"])
            self.assertIn("对应标的：", keyword["etf_pair_text"])
            self.assertTrue(all(name not in keyword["reading"] for name in legacy_stocks))

    def test_sector_opportunity_also_contains_pair(self):
        analysis = sources.analyze_brief({"金十数据": [
            {"title": "AI 算力投资上涨超预期", "url": ""},
        ]})
        self.assertTrue(analysis["opportunity_sectors"])
        entry = analysis["opportunity_sectors"][0]
        self.assertEqual(len(entry["etf_pair"]), 2)
        self.assertIn("对应标的：", entry["etf_pair_text"])

    def test_new_listings_cover_eight_products(self):
        items = sources.hk_etf_new_listings()
        self.assertEqual([item["code"] for item in items], list(sources._HK_ETF_NEW_LISTINGS))
        self.assertEqual(len(items), 8)
        for item in items:
            self.assertIn(item["code"], sources._HK_ETF_CATALOG)
            self.assertTrue(item["name"])
            self.assertTrue(item["benchmark"])
            self.assertEqual(item["counters"]["HKD"], item["code"])
            self.assertIn("HKD", item["counter_text"])

    def test_every_new_listing_is_reachable_from_heatmap_pairs(self):
        used = set()
        for tag, keywords in sources._THEMES:
            used.update(x["code"] for x in sources.hk_etf_pair("", tag))
            for raw in keywords:
                keyword = sources._heatmap_display(raw)
                used.update(x["code"] for x in sources.hk_etf_pair(keyword, tag))
        for code in sources._HK_ETF_NEW_LISTINGS:
            self.assertIn(code, used, f"{code} 未进入任何热力图双标的")

    def test_multi_counter_codes_exposed(self):
        pair = sources.hk_etf_pair("光通信", "光通信")
        self.assertEqual([x["code"] for x in pair], ["03599.HK", "03191.HK"])
        self.assertEqual(pair[0]["counters"],
                         {"HKD": "03599.HK", "CNY": "83599.HK", "USD": "41599.HK"})
        self.assertTrue(pair[0]["multi_counter"])
        self.assertFalse(pair[1]["multi_counter"])
        self.assertEqual(pair[1]["counters"], {"HKD": "03191.HK"})
        self.assertEqual(sources._hk_etf_pair_counter_text(pair),
                         "03599.HK：HKD 03599.HK / CNY 83599.HK / USD 41599.HK")
        # 3197 的美元柜台是 4 位数 9197，按港交所 5 位写法存储。
        self.assertEqual(sources.hk_etf_counters("03197.HK")["USD"], "09197.HK")

    def test_heatmap_exposes_new_listings_and_counters(self):
        heatmap = sources.analyze_keyword_heatmap({"金十数据": [
            {"title": "光模块订单暴增，光通信景气度回升", "url": ""},
            {"title": "马来西亚交易所大型股走强，东盟资金流入", "url": ""},
        ]})
        self.assertEqual(len(heatmap["new_listings"]), 8)
        keywords = {kw["keyword"]: kw for kw in heatmap["keywords"]}
        self.assertIn("光通信", keywords)
        self.assertEqual([x["code"] for x in keywords["光通信"]["etf_pair"]],
                         ["03599.HK", "03191.HK"])
        self.assertIn("CNY 83599.HK", keywords["光通信"]["etf_counters_text"])
        self.assertIn("东盟", keywords)
        self.assertEqual([x["code"] for x in keywords["东盟"]["etf_pair"]],
                         ["03143.HK", "03599.HK"])

    def test_build_html_heatmap_shows_new_etf_pool(self):
        out = sources.build_html({"金十数据": [
            {"title": "光模块订单暴增，光通信景气度回升", "url": ""},
        ]})
        heatmap = out[out.index("AI 情绪热力图"):out.index("AI 政策分析")]
        self.assertIn("本版新纳入 8 只港交所 ETF", heatmap)
        self.assertIn("03599", heatmap)
        self.assertIn("CNY 83599.HK", heatmap)


class SectorOpportunityTest(unittest.TestCase):
    """「AI 板块机会」板块：机会方向（净多头 Top 4）/ 承压方向（净空头 Top 2）+ 支撑证据。"""

    def _brief(self, titles, sources=("金十数据",)):
        return {name: [{"title": t, "url": f"https://example.com/{i}"} for i, t in enumerate(titles)]
                for name in sources}

    def test_opportunity_fields_and_counts(self):
        ana = sources.analyze_brief(self._brief([
            "贵金属黄金价格暴涨创新高",
            "地产股债务逾期风险警示，房价承压下跌",
        ]))
        opp = ana["opportunity_sectors"]
        self.assertEqual(len(opp), 1)
        self.assertEqual(opp[0]["tag"], "贵金属")
        self.assertEqual(opp[0]["up"], 1)
        self.assertEqual(opp[0]["down"], 0)
        self.assertEqual(opp[0]["net"], 1)
        self.assertEqual(opp[0]["mentions"], 1)
        self.assertEqual(opp[0]["sources"], 1)
        self.assertEqual(len(opp[0]["evidence"]), 1)
        self.assertEqual(opp[0]["evidence"][0]["source"], "金十数据")
        self.assertTrue(opp[0]["evidence"][0]["title"])

    def test_opportunity_ranking_and_top4_limit(self):
        # 净多信号：贵金属 3、AI 算力 2、其余各 1 → 按净信号强度排序，且只取 Top 4。
        titles = [
            "黄金价格暴涨创新高",
            "白银大幅上涨，贵金属反弹",
            "稀土矿产出口利好，价格回升",
            "AI 算力投资创新高，数据中心扩产超预期",
            "人工智能大模型订单暴增",
            "光模块订单暴增，光通信景气度回升",
            "半导体设备股大涨，芯片扩产加速",
            "央行宣布降息，美元流动性暴增",
            "医药医疗股反弹，临床数据超预期",
        ]
        ana = sources.analyze_brief(self._brief(titles))
        tags = [e["tag"] for e in ana["opportunity_sectors"]]
        self.assertEqual(len(tags), 4)
        self.assertEqual(tags[0], "贵金属")
        self.assertEqual(tags[1], "AI 算力")
        self.assertNotIn("美联储", tags)  # 净信号较弱的主题被 Top 4 截断
        # 前 3 名必须与「利好 / 利差板块」观点的利好板块一致（同一套排序）。
        self.assertEqual(tags[:3], ana["sectors_up"])

    def test_pressure_ranking_and_top2_limit(self):
        # 净空信号：地产 2、地缘 1、消费 1 → 承压方向只取 Top 2。
        titles = [
            "地产股债务逾期，房价下跌承压",
            "楼市房企风险警示，股价暴跌",
            "伊朗海峡危机风险警示",
            "消费疲软，零售额下跌",
        ]
        ana = sources.analyze_brief(self._brief(titles))
        tags = [e["tag"] for e in ana["pressure_sectors"]]
        self.assertEqual(tags, ["地产", "地缘"])
        self.assertEqual(ana["pressure_sectors"][0]["net"], -2)
        self.assertNotIn("消费", tags)
        self.assertEqual(tags[:2], ana["sectors_down"])

    def test_evidence_prefers_matching_direction(self):
        titles = [
            "AI 大模型扩产，算力投资暴增",
            "人工智能数据中心订单超预期",
            "AI 概念股暴跌，算力泡沫争议",
        ]
        ana = sources.analyze_brief(self._brief(titles))
        opp = ana["opportunity_sectors"][0]
        self.assertEqual(opp["tag"], "AI 算力")
        self.assertEqual(opp["up"], 2)
        self.assertEqual(opp["down"], 1)
        self.assertEqual([ev["title"] for ev in opp["evidence"]],
                         [titles[0], titles[1], titles[2]])  # 多头证据在前

    def test_evidence_keeps_cross_source_corroboration(self):
        # 同一标题被多个数据源命中（跨源佐证）：证据保留两条，便于展示多源共振。
        ana = sources.analyze_brief(self._brief(
            ["黄金价格暴涨创新高"], sources=("金十数据", "财联社 电报")))
        opp = ana["opportunity_sectors"][0]
        self.assertEqual(opp["sources"], 2)
        self.assertEqual(len(opp["evidence"]), 2)
        self.assertEqual({ev["source"] for ev in opp["evidence"]}, {"金十数据", "财联社 电报"})

    def test_signals_sample_size(self):
        ana = sources.analyze_brief(self._brief([
            "黄金价格暴涨创新高",
            "地产股债务逾期风险警示，房价下跌",
            "今天天气不错",  # 无方向信号，不计入样本
        ]))
        self.assertEqual(ana["signals"], 2)

    def test_empty_brief_has_no_opportunities(self):
        ana = sources.analyze_brief({})
        self.assertEqual(ana["opportunity_sectors"], [])
        self.assertEqual(ana["pressure_sectors"], [])
        self.assertEqual(ana["signals"], 0)



class PolicySectionTest(unittest.TestCase):
    """「AI 政策分析」板块：政策维度热度 + 多空方向 + 鹰鸽取向（含否定翻转）。"""

    def _brief(self, titles, src=("金十数据",)):
        return {name: [{"title": t, "url": f"https://example.com/{i}"} for i, t in enumerate(titles)]
                for name in src}

    @staticmethod
    def _kanpan():
        """注入三市场快照，避免 HTML 测试打行情接口。"""
        return dict(
            review=sources.analyze_ashare(market=sources._ASHARE_SNAPSHOT),
            hk_review=sources.analyze_hk(market=sources._HK_SNAPSHOT),
            us_review=sources.analyze_us(market=sources._US_SNAPSHOT),
        )

    def test_policy_buckets_heat_and_counts(self):
        p = sources.analyze_policy(self._brief([
            "美联储加息 25 个基点，缩表加速",
            "央行开展 5000 亿元买断式逆回购，流动性净投放",
            "美国拟对中国数据中心设备加征关税并实施出口管制",
        ]))
        tags = {b["tag"] for b in p["buckets"]}
        self.assertIn("货币政策 · 美联储", tags)
        self.assertIn("央行 · 流动性", tags)
        self.assertIn("财政 · 关税与债务", tags)
        fed = next(b for b in p["buckets"] if b["tag"] == "货币政策 · 美联储")
        self.assertEqual(fed["sources"], 1)
        self.assertEqual(fed["mentions"], 1)
        self.assertEqual(p["sources_hit"], 1)
        self.assertEqual(p["mentions"], 3)

    def test_hawkish_vs_dovish_stance(self):
        hawk = sources.analyze_policy(self._brief(["美联储鹰派发声：年内继续加息、维持缩表"]))
        dove = sources.analyze_policy(self._brief(["央行宣布降准降息，释放流动性支持实体经济"]))
        self.assertEqual(hawk["stance"], "偏鹰")
        self.assertEqual(dove["stance"], "偏鸽")

    def test_negation_flips_stance(self):
        # 「不再那么鸽派」= 立场转鹰：否定词必须把票翻转，而不是按字面计入鸽派。
        p = sources.analyze_policy(self._brief([
            "“新美联储通讯社”：贝森特政策反应函数转向不再那么鸽派",
            "Bessent policy reaction function shifts, no longer so dovish",
        ]))
        self.assertEqual(p["stance"], "偏鹰")
        self.assertGreaterEqual(p["hawk"], 2)
        self.assertEqual(p["dove"], 0)
        fed = next(b for b in p["buckets"] if b["tag"] == "货币政策 · 美联储")
        self.assertEqual(fed["evidence"][0]["stance"], "鹰派")

    def test_ascii_keywords_need_word_boundary(self):
        # FedEx / 含 fed 的普通单词不应被当成「美联储（Fed）」政策信号。
        p = sources.analyze_policy(self._brief(["FedEx 季度营收超预期，上调全年指引"]))
        self.assertEqual(p["mentions"], 0)
        self.assertEqual(p["buckets"], [])
        self.assertIn("暂无政策面信号", p["headline"])

    def test_direction_and_pressure_split(self):
        p = sources.analyze_policy(self._brief([
            "央行降准降息，流动性宽松预期利好股市",
            "伊朗海峡局势升级，制裁与战争风险警示",
        ]))
        self.assertIn("央行 · 流动性", p["up_tags"])
        self.assertIn("地缘与贸易政策", p["down_tags"])
        self.assertEqual(p["bull"], 1)
        self.assertEqual(p["bear"], 1)
        self.assertEqual(p["signals"], 2)

    def test_cross_source_dedupe(self):
        # 同一条新闻被多个源命中：整体提及按 (来源, 标题) 去重计 2 条，但热度只算 2 条独立来源。
        p = sources.analyze_policy(self._brief(
            ["美联储维持利率不变"], src=("金十数据", "财联社 电报")))
        self.assertEqual(p["mentions"], 2)
        self.assertEqual(p["sources_hit"], 2)
        fed = next(b for b in p["buckets"] if b["tag"] == "货币政策 · 美联储")
        self.assertEqual(fed["sources"], 2)

    def test_empty_brief_is_honest(self):
        p = sources.analyze_policy({})
        self.assertEqual(p["buckets"], [])
        self.assertEqual(p["mentions"], 0)
        self.assertEqual(p["stance"], "中性")
        self.assertIn("暂无政策面信号", p["headline"])
        self.assertNotIn("偏鹰", p["headline"])
        self.assertEqual(p["headline"].count("。"), 1)

    def test_demo_brief_has_policy_content(self):
        brief = {name: sources._demo_items(name) for name in sources.SOURCES}
        p = sources.analyze_policy(brief)
        self.assertTrue(p["buckets"])
        self.assertTrue(p["headline"])
        self.assertLessEqual(len(p["headline"]), 200)
        self.assertEqual(p["headline"].count("。"), 1)  # 对外政策总结只用一句话

    def test_analyze_brief_exposes_policy(self):
        ana = sources.analyze_brief(self._brief(["美联储加息"]))
        self.assertIn("policy", ana)
        self.assertEqual(ana["policy"]["buckets"][0]["tag"], "货币政策 · 美联储")

    def test_build_html_renders_policy_card(self):
        brief = {
            "华尔街见闻 快讯": [{"title": "“新美联储通讯社”：贝森特政策反应函数转向不再那么鸽派", "url": ""}],
            "金十数据": [
                {"title": "央行降准降息，流动性宽松利好成长板块", "url": ""},
                {"title": "伊朗海峡危机升级，制裁风险警示", "url": ""},
            ],
        }
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("AI 政策分析", out)
        self.assertIn("政策主线", out)
        self.assertIn("政策净空", out)
        self.assertIn("偏鹰", out)
        self.assertIn("条政策提及", out)
        # 位置：「AI 每日总结 → AI 看盘」之后，其后依次是「AI 政策深度」「AI 政策研报」，再到「AI 板块机会」。
        # 用卡片标题标签定位（正文里也会互相引用板块名，裸文本匹配不可靠）。
        def at(label: str) -> int:
            return out.index(f'<span class="tag">{label}</span>')

        self.assertLess(at("AI 每日总结"), at("AI 看盘"))
        self.assertLess(at("AI 看盘"), at("AI 政策分析"))
        self.assertLess(at("AI 政策分析"), at("AI 政策深度"))
        self.assertLess(at("AI 政策深度"), at("AI 政策研报"))
        self.assertLess(at("AI 政策研报"), at("AI 板块机会"))

    def test_build_html_policy_card_honest_when_no_signals(self):
        out = sources.build_html({"金十数据": [{"title": "今天天气不错", "url": ""}]}, **self._kanpan())
        self.assertIn("AI 政策分析", out)
        self.assertIn("暂无政策面信号可统计", out)

    def test_build_html_policy_evidence_is_escaped(self):
        brief = {"金十数据": [{"title": "美联储：<script>alert(1)</script> 加息", "url": ""}]}
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>alert", out)


class PolicyKnowledgeBaseTest(unittest.TestCase):
    """①政策法规知识库（RAG）：检索历史政策 → 与新信号对比分析。"""

    def test_kb_covers_every_policy_dimension(self):
        # 知识库必须覆盖全部 8 个政策维度，否则某个维度当天命中时检索不到背景知识。
        dimensions = {entry["dimension"] for entry in sources._POLICY_KB}
        for tag, _keywords in sources._POLICY_BUCKETS:
            self.assertIn(tag, dimensions, f"知识库缺少维度：{tag}")

    def test_kb_entries_have_required_fields(self):
        required = {"id", "date", "issuer", "doctype", "title", "dimension", "phrase",
                    "keywords", "summary", "market_effect", "industries"}
        for entry in sources._POLICY_KB:
            self.assertTrue(required <= set(entry), f"{entry.get('id')} 缺字段：{required - set(entry)}")
        ids = [entry["id"] for entry in sources._POLICY_KB]
        self.assertEqual(len(ids), len(set(ids)), "知识库条目 id 必须唯一")

    def test_retrieval_matches_keywords_and_dimension(self):
        hits = sources.retrieve_policy_context("央行降准并开展买断式逆回购，保持流动性充裕",
                                               tags=["央行 · 流动性"], top_k=3)
        self.assertTrue(hits)
        top = hits[0]
        self.assertEqual(top["dimension"], "央行 · 流动性")
        self.assertIn("降准", top["matched"])
        self.assertGreaterEqual(top["score"], sources._POLICY_KB_WEIGHTS["keyword"]
                                + sources._POLICY_KB_WEIGHTS["dimension"])
        self.assertIn("命中", top["why"])

    def test_retrieval_ranks_by_score_then_recency(self):
        hits = sources.retrieve_policy_context("出口管制 实体清单 制裁 半导体设备",
                                               tags=["地缘与贸易政策"], top_k=5)
        scores = [hit["score"] for hit in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("BIS-CHIPRULE-202412", [hit["id"] for hit in hits])

    def test_retrieval_respects_top_k_and_returns_nothing_without_signal(self):
        self.assertLessEqual(len(sources.retrieve_policy_context("央行降准", tags=["央行 · 流动性"], top_k=2)), 2)
        self.assertEqual(sources.retrieve_policy_context("今天天气不错"), [])

    def test_policy_knowledge_attaches_comparison(self):
        policy = sources.analyze_policy({"金十数据": [{"title": "美联储鹰派转向，暗示继续加息缩表", "url": ""}]},
                                        deep=False)
        kb = sources.policy_knowledge(policy)
        self.assertTrue(kb["hits"])
        self.assertEqual(len(kb["comparison"]), len(kb["hits"]))
        self.assertIn("历史对照", kb["comparison"][0])
        self.assertIn(kb["hits"][0]["phrase"], kb["comparison"][0])

    def test_policy_knowledge_is_honest_without_signals(self):
        kb = sources.policy_knowledge(sources.analyze_policy({"金十数据": [{"title": "今天天气不错", "url": ""}]},
                                                             deep=False))
        self.assertEqual(kb["hits"], [])
        self.assertIn("未启动", kb["note"])


class PolicyModifierTest(unittest.TestCase):
    """③金融术语口径：政策修饰词的经济学 + 法学解读。"""

    def test_easing_and_tightening_modifiers(self):
        loose = sources.interpret_policy_modifiers("稳健的货币政策转为适度宽松，保持流动性充裕")
        tight = sources.interpret_policy_modifiers("监管从严，严禁违规减持，依法依规查处")
        self.assertEqual(loose["bias"], "偏松")
        self.assertEqual(tight["bias"], "偏紧")
        self.assertGreater(loose["score"], 0)
        self.assertLess(tight["score"], 0)

    def test_modifier_carries_market_and_legal_meaning(self):
        hits = sources.interpret_policy_modifiers("证监会从严监管，严禁财务造假")["hits"]
        words = {hit["word"] for hit in hits}
        self.assertIn("从严", words)
        self.assertIn("严禁", words)
        ban = next(hit for hit in hits if hit["word"] == "严禁")
        self.assertLess(ban["strength"], 0)
        self.assertTrue(ban["legal"])       # 禁止性规范必须给出法学含义
        self.assertTrue(ban["market"])      # 同时给出市场含义
        # 解读句要落到具体的市场含义，而不是只给一个标签。
        self.assertIn("降准降息预期升温", sources.interpret_policy_modifiers("适度宽松")["reading"])

    def test_longest_match_wins(self):
        # 「适度宽松」命中后不再重复计入被它包含的短词，避免同一表述被双重计权。
        hits = sources.interpret_policy_modifiers("实施适度宽松的货币政策")["hits"]
        words = [hit["word"] for hit in hits]
        self.assertIn("适度宽松", words)
        self.assertNotIn("适度", words)

    def test_no_modifier_is_honest(self):
        out = sources.interpret_policy_modifiers("现货黄金上涨超过7%")
        self.assertEqual(out["hits"], [])
        self.assertEqual(out["bias"], "中性")
        self.assertIn("未命中政策修饰词", out["reading"])


class PolicySentimentTest(unittest.TestCase):
    """④政策舆情情感打分：正向 / 中性 / 负向 + 官媒与市场的预期差。"""

    @staticmethod
    def _brief(items):
        return {name: [{"title": title, "url": ""} for title in titles] for name, titles in items.items()}

    def test_positive_negative_neutral_counts(self):
        out = sources.analyze_policy_sentiment(self._brief({
            "金十数据": [
                "央行降准降息支持实体经济，流动性宽松利好股市",     # 正向
                "监管从严查处违规减持，立案调查警示风险",           # 负向
                "央行公告开展逆回购操作",                          # 中性（无极性词）
            ],
        }))
        overall = out["overall"]
        self.assertEqual((overall["positive"], overall["negative"], overall["neutral"]), (1, 1, 1))
        self.assertEqual(overall["total"], 3)
        self.assertEqual(overall["ratio"], "正向 1 : 中性 1 : 负向 1")
        labels = {item["title"]: item["label"] for item in out["items"]}
        self.assertEqual(labels["央行降准降息支持实体经济，流动性宽松利好股市"], "正向")
        self.assertEqual(labels["监管从严查处违规减持，立案调查警示风险"], "负向")

    def test_amplifier_words_double_the_weight(self):
        plain = sources.analyze_policy_sentiment(self._brief({"金十数据": ["央行降准，流动性宽松"]}))
        loud = sources.analyze_policy_sentiment(self._brief({"金十数据": ["罕见！央行降准，流动性宽松"]}))
        self.assertGreater(loud["items"][0]["score"], plain["items"][0]["score"])

    def test_official_versus_market_gap_opens_policy_window(self):
        out = sources.analyze_policy_sentiment(self._brief({
            "国务院 政策解读": ["有关负责人解读：政策支持实体经济发展，促进就业稳定"],
            "金十数据": ["监管从严查处，制裁风险警示，市场担忧情绪蔓延"],
        }))
        self.assertEqual(out["official"]["total"], 1)
        self.assertEqual(out["market"]["total"], 1)
        self.assertGreater(out["gap"], 0.3)
        self.assertIn("政策窗口期", out["window"]["label"])
        self.assertIn("不作为投资依据", out["window"]["note"])

    def test_only_policy_related_items_are_scored(self):
        out = sources.analyze_policy_sentiment(self._brief({
            "金十数据": ["某公司季度净利润暴涨超预期", "央行降准释放流动性"],
        }))
        self.assertEqual(out["overall"]["total"], 1)   # 与政策无关的多空新闻不计入政策舆情

    def test_no_policy_news_is_honest(self):
        out = sources.analyze_policy_sentiment(self._brief({"金十数据": ["今天天气不错"]}))
        self.assertEqual(out["overall"]["total"], 0)
        self.assertEqual(out["window"]["label"], "无政策舆情样本")


class PolicyGraphTest(unittest.TestCase):
    """⑤政策传导图谱：政策主体 → 受影响行业 → 产业链上下游节点。"""

    def test_chain_links_subject_industry_and_nodes(self):
        policy = sources.analyze_policy(
            {"金十数据": [{"title": "美国对中国半导体设备实施出口管制并加征关税，制裁风险升温", "url": ""}]},
            deep=False)
        graph = sources.policy_chain_graph(policy)
        self.assertTrue(graph["chains"])
        buckets = {chain["bucket"] for chain in graph["chains"]}
        self.assertIn("地缘与贸易政策", buckets)     # 出口管制 / 制裁命中地缘维度
        self.assertIn("财政 · 关税与债务", buckets)   # 加征关税同时命中财政维度
        chain = next(c for c in graph["chains"] if c["bucket"] == "地缘与贸易政策")
        self.assertTrue(chain["subject"] and chain["industries"])
        self.assertTrue(chain["upstream"] and chain["midstream"] and chain["downstream"])
        self.assertIn(chain["direction"], ("净多", "净空", "中性"))
        self.assertIn("→", chain["note"])
        kinds = {node["kind"] for node in graph["nodes"]}
        self.assertTrue({"subject", "industry", "上游", "中游", "下游"} <= kinds)
        self.assertTrue(graph["edges"])

    def test_graph_is_honest_without_signals(self):
        graph = sources.policy_chain_graph(sources.analyze_policy({"金十数据": [{"title": "天气", "url": ""}]},
                                                                  deep=False))
        self.assertEqual(graph["chains"], [])
        self.assertIn("未展开", graph["note"])


class PolicyReasoningTest(unittest.TestCase):
    """②高级分析师推理链 + 思维导图 + 结构化政策影响研报。"""

    @staticmethod
    def _policy(titles=("美联储鹰派转向暗示继续加息，美债收益率上行",
                        "美国对数据中心设备加征关税并实施出口管制，制裁风险警示",
                        "央行开展买断式逆回购，流动性净投放支持实体经济")):
        return sources.analyze_policy({"华尔街见闻 快讯": [{"title": t, "url": ""} for t in titles]},
                                      deep=False)

    def test_four_steps_in_fixed_order(self):
        reasoning = sources.analyze_policy_reasoning(self._policy())
        self.assertEqual([step["label"] for step in reasoning["steps"]],
                         ["宏观背景", "行业限制", "资金流向", "受益板块"])
        self.assertEqual([step["no"] for step in reasoning["steps"]], ["01", "02", "03", "04"])
        for step in reasoning["steps"]:
            self.assertTrue(step["text"])
            self.assertIn(step["key"], ("macro", "industry", "flow", "beneficiary"))

    def test_steps_are_grounded_in_evidence(self):
        policy = self._policy()
        reasoning = sources.analyze_policy_reasoning(
            policy, kb=sources.policy_knowledge(policy),
            sentiment=sources.analyze_policy_sentiment(
                {"华尔街见闻 快讯": [{"title": t, "url": ""} for t in
                                    ("美联储鹰派转向暗示继续加息，美债收益率上行",)]}),
            graph=sources.policy_chain_graph(policy))
        evidence = [ev for step in reasoning["steps"] for ev in step["evidence"]]
        self.assertTrue(evidence)
        self.assertTrue({ev["type"] for ev in evidence} <= {"news", "kb"})
        self.assertIn("货币政策", reasoning["steps"][0]["text"])    # 宏观背景引用当日真实统计
        self.assertIn("政策净空", reasoning["steps"][1]["text"])   # 行业限制引用净空维度

    def test_confidence_scales_with_sample_size(self):
        self.assertEqual(sources._policy_confidence({"sources_hit": 8, "mentions": 20, "signals": 10}), "高")
        self.assertEqual(sources._policy_confidence({"sources_hit": 2, "mentions": 4, "signals": 2}), "中")
        self.assertEqual(sources._policy_confidence({}), "低")

    def test_mindmap_structure(self):
        policy = self._policy()
        kb = sources.policy_knowledge(policy)
        reasoning = sources.analyze_policy_reasoning(policy, kb=kb)
        mindmap = sources.policy_mindmap(policy, reasoning, kb, sources.analyze_policy_sentiment({}),
                                         sources.policy_chain_graph(policy),
                                         {"lstm": {"summary": "—"}, "prophet": {"summary": "—"}})
        self.assertIn("政策分析", mindmap["root"])
        labels = [branch["label"] for branch in mindmap["branches"]]
        self.assertIn("宏观背景", labels)
        self.assertIn("产业链传导", labels)
        self.assertIn("模型视角", labels)
        for branch in mindmap["branches"]:
            self.assertTrue(branch["children"], f"分支「{branch['label']}」没有子节点")

    def test_research_note_rating_long_term_and_risks(self):
        policy = self._policy()
        kb = sources.policy_knowledge(policy)
        sentiment = sources.analyze_policy_sentiment({})
        graph = sources.policy_chain_graph(policy)
        modifiers = sources.interpret_policy_modifiers(kb["query"])
        reasoning = sources.analyze_policy_reasoning(policy, kb, sentiment, graph, modifiers)
        note = sources.policy_research_note(policy, reasoning, kb, graph, sentiment, modifiers,
                                            {"lstm": {"summary": "L"}, "prophet": {"summary": "P"}})
        self.assertEqual(note["rating"], "政策承压（偏空）")
        self.assertTrue(note["abstract"] and note["horizon"])
        self.assertTrue(note["long_term"])
        for item in note["long_term"]:
            self.assertIn(item["nature"], ("结构性", "周期性"))
            self.assertIn("个月", item["horizon"])
            self.assertIn("→", item["impact"])
        self.assertTrue(note["risks"])
        self.assertIn("不作为投资依据", note["disclaimer"])

    def test_deep_layer_is_honest_without_signals(self):
        policy = sources.analyze_policy({"金十数据": [{"title": "今天天气不错", "url": ""}]}, deep=False)
        reasoning = sources.analyze_policy_reasoning(policy)
        self.assertIn("证据不足", reasoning["steps"][1]["text"])
        note = sources.policy_research_note(policy, reasoning, sources.policy_knowledge(policy),
                                            sources.policy_chain_graph(policy),
                                            sources.analyze_policy_sentiment({}),
                                            sources.interpret_policy_modifiers(""),
                                            {"lstm": {"summary": "—"}, "prophet": {"summary": "—"}})
        self.assertEqual(note["rating"], "政策中性（结构分化）")
        self.assertTrue(any("样本覆盖有限" in risk for risk in note["risks"]))


class PolicyModelTest(unittest.TestCase):
    """⑥⑦LSTM 与 Prophet 式分解：真实算法、可复算、样本不足时诚实降级。"""

    @staticmethod
    def _series(closes, start="2026-01-05"):
        day = sources.datetime.strptime(start, "%Y-%m-%d")
        dates = []
        for _ in closes:
            while day.weekday() >= 5:
                day += sources.timedelta(days=1)
            dates.append(str(day.date()))
            day += sources.timedelta(days=1)
        return {"name": "测试序列", "source": "test", "dates": dates, "closes": closes}

    @staticmethod
    def _ar1_series(points=120, seed=7):
        rng = random.Random(seed)
        closes, level, prev, returns = [100.0], 100.0, 0.5, []
        for _ in range(points):
            ret = 0.75 * prev + rng.uniform(-0.05, 0.05)
            returns.append(ret)
            prev = ret
            level *= (1 + ret / 100.0)
            closes.append(round(level, 4))
        return PolicyModelTest._series(closes), returns

    def test_lstm_trains_and_forecasts(self):
        series, returns = self._ar1_series()
        out = sources.lstm_policy_forecast(series, epochs=60)
        self.assertTrue(out["available"])
        self.assertLess(out["loss_last"], out["loss_first"])          # 训练确实在优化
        self.assertEqual(len(out["path"]), out["horizon"])
        self.assertEqual([p["step"] for p in out["path"]], list(range(1, out["horizon"] + 1)))
        # 学到 AR(1) 的符号：上一个收益为正时，首日外推同向。
        self.assertEqual(out["path"][0]["ret"] > 0, (0.75 * returns[-1]) > 0)
        self.assertIn("训练样本", out["summary"])
        self.assertIn("模型情景", out["note"])

    def test_lstm_policy_scenario_shifts_the_path(self):
        series, _returns = self._ar1_series(points=60)
        base = sources.lstm_policy_forecast(series, epochs=30)
        loose = sources.lstm_policy_forecast(series, epochs=30, shock=0.8)
        self.assertNotEqual(loose["shock_delta"], 0.0)
        self.assertEqual(len(loose["shock_path"]), len(base["path"]))

    def test_lstm_refuses_short_series(self):
        out = sources.lstm_policy_forecast(self._series([100.0, 101.0, 102.0]))
        self.assertFalse(out["available"])
        self.assertEqual(out["path"], [])
        self.assertIn("不外推", out["summary"])

    def test_prophet_recovers_trend_seasonality_and_policy_shock(self):
        rng = random.Random(11)
        closes, level, day, event = [], 1000.0, 0, None
        dates = []
        while len(dates) < 80:
            current = sources.datetime(2026, 1, 5) + sources.timedelta(days=day)
            day += 1
            if current.weekday() >= 5:
                continue
            season = 0.004 * math.sin(2 * math.pi * len(dates) / 5.0)
            shock = 0.0
            if len(dates) == 40:
                shock, event = 0.02, str(current.date())
            level *= 1 + 0.0015 + season + shock + rng.uniform(-0.002, 0.002)
            dates.append(str(current.date()))
            closes.append(round(level, 4))
        dec = sources.prophet_policy_decompose({"name": "SYN", "source": "test", "dates": dates,
                                                "closes": closes}, events=[event], changepoints=2)
        self.assertTrue(dec["available"])
        self.assertGreater(dec["r2"], 0.9)
        self.assertGreater(dec["trend_total_pct"], 0)                      # 真值：温和上行趋势
        self.assertGreater(dec["season_amplitude_pct"], 0.15)              # 真值：周度振幅 0.40%
        self.assertLess(dec["season_amplitude_pct"], 0.8)
        effect = dec["policy_effects"][0]
        self.assertEqual(effect["event"], event)
        self.assertGreater(effect["event_study_pct"], 1.4)                 # 真值：+2.0% 的政策跳变
        self.assertLess(effect["event_study_pct"], 2.6)
        self.assertEqual(effect["label"], "正向冲击")
        self.assertGreater(dec["vol_ratio"], 1.0)                          # 政策窗口内波动放大
        self.assertEqual(len(dec["forecast"]), dec["horizon"])
        self.assertEqual(dec["seasonality_last5_pct"].__len__(), 5)

    def test_prophet_notes_when_no_event_in_window(self):
        dec = sources.prophet_policy_decompose(self._series([100 + i * 0.4 for i in range(40)]))
        self.assertTrue(dec["available"])
        self.assertEqual(dec["policy_effects"], [])
        self.assertIn("不可辨识", dec["summary"])

    def test_prophet_refuses_short_series(self):
        out = sources.prophet_policy_decompose(self._series([100.0, 101.0]))
        self.assertFalse(out["available"])
        self.assertIn("不可辨识", out["summary"])

    def test_series_sources(self):
        klines = {"上证指数": {
            "2026-09-01": {"close": 3900.0, "source": "eastmoney"},
            "2026-09-02": {"close": 3910.0, "source": "eastmoney"},
            "2026-09-03": {"close": 3925.0, "source": "eastmoney"},
        }}
        series = sources.policy_series_from_klines(klines)
        self.assertEqual(series["name"], "上证指数")
        self.assertEqual(series["source"], "eastmoney")
        self.assertEqual(series["closes"], [3900.0, 3910.0, 3925.0])
        self.assertEqual(series["dates"], ["2026-09-01", "2026-09-02", "2026-09-03"])
        # 缓存的日 K 优先；没有缓存时回退合成演示序列，并明确标注为非真实数据。
        sources.set_policy_klines(klines)
        try:
            self.assertEqual(sources.get_policy_series()["source"], "eastmoney")
        finally:
            sources.set_policy_klines({})
        fallback = sources.get_policy_series()
        self.assertEqual(fallback["source"], "synthetic")
        self.assertIn("非真实市场数据", fallback["note"])
        self.assertIn(fallback["event_date"], fallback["dates"])

    def test_policy_shock_scalar_follows_stance(self):
        dove = sources._policy_shock_scalar({"stance": "偏鸽"}, {"score": 1.5})
        hawk = sources._policy_shock_scalar({"stance": "偏鹰"}, {"score": -1.5})
        self.assertGreater(dove, 0)
        self.assertLess(hawk, 0)
        self.assertLessEqual(abs(dove), 1.0)
        self.assertLessEqual(abs(hawk), 1.0)


class PolicyDeepWiringTest(unittest.TestCase):
    """深度层装配：analyze_policy(deep=True) / analyze_brief(deep_policy) / 推送卡片位置。"""

    def setUp(self):
        # collect_market_for_push() 会把抓到的日 K 缓存给政策模型层复用；
        # MarketFreshnessTest 会替换 _fetch_ashare_klines 且不还原，这里显式清空缓存，
        # 保证「无实时行情 → 合成演示序列」的断言不受用例执行顺序影响。
        sources.set_policy_klines({})

    def _kanpan(self):
        return dict(
            review=sources.analyze_ashare(market=sources._ASHARE_SNAPSHOT),
            hk_review=sources.analyze_hk(market=sources._HK_SNAPSHOT),
            us_review=sources.analyze_us(market=sources._US_SNAPSHOT),
        )

    def test_analyze_policy_deep_keys(self):
        brief = {"华尔街见闻 快讯": [{"title": "美联储鹰派转向，暗示继续加息缩表", "url": ""}],
                 "国务院 政策解读": [{"title": "有关负责人解读：政策支持实体经济发展", "url": ""}]}
        policy = sources.analyze_policy(brief)          # deep 默认开启
        for key in ("kb", "modifiers", "sentiment", "graph", "reasoning", "mindmap", "research", "models"):
            self.assertIn(key, policy)
        self.assertEqual(len(policy["reasoning"]["steps"]), 4)
        self.assertIn("lstm", policy["models"])
        self.assertIn("prophet", policy["models"])
        self.assertLessEqual(len(policy["headline"]), 200)   # 总结句口径不受深度层影响

    def test_analyze_brief_stays_shallow_by_default(self):
        brief = {"金十数据": [{"title": "美联储加息", "url": ""}]}
        self.assertNotIn("kb", sources.analyze_brief(brief)["policy"])
        self.assertIn("kb", sources.analyze_brief(brief, deep_policy=True)["policy"])

    def test_build_html_places_policy_right_after_daily_summary(self):
        brief = {name: sources._demo_items(name) for name in sources.SOURCES}
        out = sources.build_html(brief, **self._kanpan())

        def at(label: str) -> int:
            return out.index(f'<span class="tag">{label}</span>')

        positions = [at(label) for label in ("AI 每日总结", "AI 看盘", "AI 政策分析", "AI 政策深度",
                                             "AI 政策研报", "AI 板块机会")]
        self.assertEqual(positions, sorted(positions))

    def test_build_html_renders_deep_layer_blocks(self):
        brief = {name: sources._demo_items(name) for name in sources.SOURCES}
        out = sources.build_html(brief, **self._kanpan())
        for token in ("政策法规知识库检索", "政策术语口径（修饰词解读）", "政策舆情情感打分",
                      "政策传导图谱", "分析师推理链", "政策影响思维导图", "LSTM · 中长期走势节奏",
                      "Prophet 式分解 · 剥离季节看政策窗口", "政策影响研报（结构化结论）",
                      "长远冲击", "风险提示", "政策窗口期"):
            self.assertIn(token, out)
        for hidden in ("关键表述：", "背景知识：", "历史市场含义：", "LLM 对比分析"):
            self.assertNotIn(hidden, out)
        term_start = out.index("政策术语口径（修饰词解读）")
        term_end = out.index("政策舆情情感打分", term_start)
        term_block = out[term_start:term_end]
        self.assertEqual(term_block.count("加权强度"), 1)
        self.assertNotIn("市场含义", term_block)
        self.assertNotIn("法律含义", term_block)
        # 宏观背景 → 行业限制 → 资金流向 → 受益板块 四步齐全且顺序正确
        steps = [out.index(f'<span class="tag">{label}</span>')
                 for label in ("宏观背景", "行业限制", "资金流向", "受益板块")]
        self.assertEqual(steps, sorted(steps))

    def test_build_html_marks_synthetic_model_series(self):
        brief = {"金十数据": [{"title": "央行降准降息，流动性宽松利好成长板块", "url": ""}]}
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("合成演示序列", out)

    def test_build_html_uses_injected_series(self):
        brief = {"金十数据": [{"title": "央行降准降息，流动性宽松利好成长板块", "url": ""}]}
        closes = [3800 + i * 4 for i in range(40)]
        dates = [f"2026-07-{i + 1:02d}" for i in range(40)]
        out = sources.build_html(brief, series={"name": "上证指数", "source": "eastmoney",
                                               "dates": dates, "closes": closes}, **self._kanpan())
        self.assertIn("上证指数", out)
        self.assertNotIn("合成演示序列（无实时行情）", out)

    def test_deep_layer_escapes_html(self):
        brief = {"金十数据": [{"title": "美联储 <script>alert(1)</script> 加息，监管从严", "url": ""}]}
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>alert", out)

    def test_deep_cards_degrade_when_capacity_is_tight(self):
        brief = {name: [{"title": f"{name} 的长标题快讯测试内容" * 3, "url": ""} for _ in range(20)]
                 for name in sources.SOURCES}
        tight = sources.build_html(brief, max_length=16000, **self._kanpan())
        self.assertLessEqual(len(tight), 16000)
        self.assertNotIn("AI 政策研报", tight)            # 深度层让位，主分析卡片保留
        self.assertIn("AI 政策分析", tight)


class PolicyMarketImpactTest(unittest.TestCase):
    """「AI 政策深度」主问题：近 30 日已核验的全球政策发布对 A股 / 港股 / 美股的影响。"""

    AS_OF = date(2026, 9, 23)

    def impact(self, brief=None, **kwargs):
        kwargs.setdefault("as_of", self.AS_OF)
        return sources.policy_impact.analyze_policy_market_impact(brief, **kwargs)

    def test_window_is_thirty_inclusive_days(self):
        start, end = sources.policy_impact.policy_window_bounds(self.AS_OF, 30)
        self.assertEqual(end, self.AS_OF)
        self.assertEqual(start, date(2026, 8, 25))
        self.assertEqual((end - start).days + 1, 30)

    def test_default_window_keeps_verified_releases_and_drops_older_announcement(self):
        out = self.impact()
        ids = [item["id"] for item in out["releases"]]
        self.assertIn("FED-HIKE-20260916", ids)
        self.assertIn("PBC-HOUSING-20260828", ids)
        self.assertIn("ECB-HIKE-20260910", ids)
        self.assertIn("BOJ-HIKE-20260918", ids)
        self.assertIn("PBC-LPR-20260920", ids)
        # 8 月 19 日的回购发文在窗口外；9 月 9 日只记生效，不重复计为新发文。
        self.assertNotIn("UST-BUYBACK-20260819", ids)
        self.assertIn("UST-BUYBACK-20260909", ids)
        self.assertEqual(out["counts"]["发布"], 8)
        self.assertEqual(out["counts"]["信号"], 1)
        self.assertEqual(out["counts"]["生效"], 1)
        self.assertEqual(out["counts"]["outside"], 1)
        self.assertEqual(out["latest_release"], "2026-09-20")
        self.assertIn("未核实日期的快讯不当成政策发布", out["gap_note"])

    def test_earlier_as_of_includes_august_announcement(self):
        out = self.impact(as_of=date(2026, 8, 24))
        ids = [item["id"] for item in out["releases"]]
        self.assertIn("UST-BUYBACK-20260819", ids)
        self.assertNotIn("FED-HIKE-20260916", ids)
        self.assertEqual(out["start"], "2026-07-26")

    def test_empty_window_does_not_invent_market_impact(self):
        out = self.impact(as_of=date(2024, 1, 15))
        self.assertEqual(out["releases"], [])
        self.assertIn("没有已核验的全球政策发布", out["headline"])
        self.assertIn("不外推", out["headline"])
        self.assertTrue(all(market["bias"] == "—" for market in out["markets"]))

    def test_fed_hike_is_observed_us_pressure_while_a_share_stays_split(self):
        out = self.impact()
        by_name = {market["name"]: market for market in out["markets"]}
        self.assertEqual(by_name["美股"]["bias"], "偏空")
        self.assertTrue(by_name["美股"]["observed"])
        self.assertLessEqual(by_name["美股"]["score"], -1.2)
        self.assertIn("长久期成长", by_name["美股"]["pressure"])
        self.assertIn("银行", by_name["美股"]["support"])
        # 国内地产组合拳与海外加息对冲，指数不编成单边。
        self.assertEqual(by_name["A股"]["bias"], "中性")
        self.assertFalse(by_name["A股"]["observed"])
        self.assertIn("地产链", by_name["A股"]["support"])
        self.assertIn("长久期成长", by_name["A股"]["pressure"])
        self.assertEqual(by_name["港股"]["bias"], "中性")
        self.assertIn("内房股", by_name["港股"]["support"])
        self.assertIn("恒生科技", by_name["港股"]["pressure"])
        fed = next(item for item in out["releases"] if item["id"] == "FED-HIKE-20260916")
        self.assertIn("已观测", fed["stock_line"])
        self.assertIn("约 600 点", fed["summary"])
        self.assertEqual(out["pressure_events"][0]["id"], "FED-HIKE-20260916")

    def test_live_titles_do_not_change_score_and_are_escaped(self):
        plain = self.impact()
        brief = {"金十数据": [{"title": "Jackson Hole <script>alert(1)</script> 讲话", "url": ""}]}
        hit = self.impact(brief)
        self.assertEqual(plain["markets"][0]["score"], hit["markets"][0]["score"])
        jackson = next(item for item in hit["releases"] if item["id"] == "FED-JH-20260828")
        self.assertTrue(jackson["mentions"])
        html = sources.policy_impact.render_policy_impact_rows(hit, sources._esc, 2)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("近30日全球政策", html)

    def test_bias_thresholds_are_fixed(self):
        label = sources.policy_impact._bias_label
        self.assertEqual(label(1.2), "偏多")
        self.assertEqual(label(0.45), "中性偏多")
        self.assertEqual(label(0), "中性")
        self.assertEqual(label(-0.45), "中性")
        self.assertEqual(label(-0.46), "中性偏空")
        self.assertEqual(label(-1.2), "偏空")

    def test_analyze_policy_exposes_market_impact_without_deep_models(self):
        shallow = sources.analyze_policy({}, deep=False)
        self.assertIn("market_impact", shallow)
        self.assertNotIn("kb", shallow)
        self.assertIn("releases", shallow["market_impact"])

    def test_build_html_leads_policy_deep_with_thirty_day_impact(self):
        out = sources.build_html({"金十数据": [{"title": "今天天气不错", "url": ""}]})
        deep = out[out.index("AI 政策深度"):out.index("AI 政策研报")]
        self.assertIn("近30日全球政策 → 股市影响", deep)
        self.assertLess(deep.index("近30日全球政策"), deep.index("政策法规知识库检索"))
        for token in ("A股", "港股", "美股", "窗口内政策发布", "板块传导"):
            self.assertIn(token, deep)
        # 对照层仍在，避免深度卡片退回成只有当日词频。
        self.assertIn("政策法规知识库检索", deep)


class BuildHtmlTest(unittest.TestCase):

    def _kanpan(self):
        """注入三市场快照，避免 HTML 测试打行情接口，并让看盘内容可断言。"""
        return dict(
            review=sources.analyze_ashare(market=sources._ASHARE_SNAPSHOT),
            hk_review=sources.analyze_hk(market=sources._HK_SNAPSHOT),
            us_review=sources.analyze_us(market=sources._US_SNAPSHOT),
        )

    def test_build_html_omits_monitoring_list_and_keeps_disclaimer(self):
        brief = {name: sources._demo_items(name)[:2] for name in sources.SOURCES}
        out = sources.build_html(brief, **self._kanpan())
        # 04「监测平台列表与雷达矩阵」不再作为原始来源卡片输出。
        self.assertNotIn("覆盖 18 个数据源", out)
        self.assertNotIn("金十数据 第", out)
        self.assertIn("不构成投资建议", out)

    def test_build_html_renders_four_viewpoints(self):
        brief = {name: sources._demo_items(name)[:2] for name in sources.SOURCES}
        out = sources.build_html(brief, **self._kanpan())
        for label in ("市场情绪", "多空博弈概率", "利好 / 利差板块", "资金流向分析"):
            self.assertIn(label, out)

    def test_build_html_empty_brief_renders_points_fallback(self):
        out = sources.build_html({}, **self._kanpan())
        self.assertIn("暂无可统计的多空信号", out)
        self.assertIn("资金流向分析", out)
        self.assertIn("暂无净多头信号占优的板块", out)

    def test_build_html_renders_sector_opportunity_card(self):
        # 「AI 板块机会」：机会方向（净多头排序 + 证据）与承压方向（净空头）都在简报里渲染。
        brief = {
            "华尔街见闻 快讯": [{"title": "AI 算力投资创新高，数据中心扩产超预期", "url": ""}],
            "金十数据": [
                {"title": "黄金价格暴涨创新高", "url": ""},
                {"title": "地产股债务逾期风险警示，房价下跌", "url": ""},
            ],
        }
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("AI 板块机会", out)
        self.assertIn("机会方向", out)
        self.assertIn("净多", out)
        self.assertIn("承压方向", out)
        self.assertIn("净空", out)
        # 证据要带上数据源名称
        self.assertIn("华尔街见闻 快讯", out)
        self.assertIn("基于 3 条多空信号", out)
        # 位置：在「AI 每日总结」「AI 看盘」之后、「全网快讯」之前（开头 AI 部分）。
        self.assertLess(out.index("AI 每日总结"), out.index("AI 板块机会"))
        self.assertLess(out.index('<span class="tag">AI 看盘</span>'), out.index("AI 板块机会"))
        self.assertLess(out.index("AI 板块机会"), out.index("全网快讯"))

    def test_build_html_places_kanpan_right_after_daily_summary(self):
        # 「AI 看盘」紧跟「AI 每日总结」之后：两张卡片之间不能夹任何其它卡片，
        # 其后才是「AI 情绪热力图 → AI 政策分析 → … → AI 板块机会 → 全网快讯」。
        brief = {name: sources._demo_items(name) for name in sources.SOURCES}
        out = sources.build_html(brief, **self._kanpan())

        def at(label: str) -> int:
            return out.index(f'<span class="tag">{label}</span>')

        order = ["AI 每日总结", "AI 看盘", "AI 情绪热力图", "AI 政策分析", "AI 政策深度",
                 "AI 政策研报", "AI 板块机会", "全网快讯"]
        positions = [at(label) for label in order]
        self.assertEqual(positions, sorted(positions), "板块顺序应为 " + " → ".join(order))
        between = out[at("AI 每日总结"):at("AI 看盘")]
        self.assertEqual(between.count('<div class="card">'), 1, "每日总结与看盘之间不应再夹其它卡片")
        # 三个市场在同一张看盘卡里，且都排在热力图之前。
        for market in ("A 股", "港股", "美股"):
            self.assertLess(at("AI 看盘"), out.index(f'<span class="tag">{market}</span>'))
            self.assertLess(out.index(f'<span class="tag">{market}</span>'), at("AI 情绪热力图"))

    def test_build_html_opportunity_card_honest_when_no_signals(self):
        out = sources.build_html({}, **self._kanpan())
        self.assertIn("AI 板块机会", out)
        self.assertIn("暂无净多头信号占优的板块", out)
        self.assertNotIn("承压方向", out)

    def test_build_html_opportunity_evidence_is_escaped(self):
        brief = {"金十数据": [{"title": "AI 大模型扩产 <script>alert(1)</script> 暴涨", "url": ""}]}
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>alert", out)

    def test_escapes_html(self):
        out = sources.build_html({"金十数据": [{"title": "<b>x</b>", "url": ""}]}, **self._kanpan())
        # 原始来源条目不进入正文，因此也不会把未转义的 HTML 带入推送。
        self.assertNotIn("<b>x</b>", out)

    def test_build_html_length_under_pushplus_limit(self):
        # 默认 10 万字口径：即使每个源都有大量长标题快讯，生成 HTML 依然严格控制在 98,000 字以内
        huge_brief = {
            name: [{"title": f"{name} 的长标题快讯测试内容" * 3, "url": "https://example.com/test"} for _ in range(60)]
            for name in sources.SOURCES
        }
        out = sources.build_html(huge_brief, **self._kanpan())
        self.assertLessEqual(len(out), 98000)

    def test_build_html_default_quota_lists_three_per_source(self):
        # 默认仍按会员口径抓取（10 万字 / 每源 20 条），正文最后的「全网快讯」固定每源 3 条。
        self.assertEqual(sources.pushplus_quota(), (98000, 20))
        self.assertEqual(sources.default_fetch_limit(), sources.LIMIT_MEMBER)
        self.assertEqual(sources.NEWS_ITEMS_PER_SOURCE, 3)
        brief = {name: [{"title": f"快讯标题第 {i} 条", "url": ""} for i in range(8)]
                 for name in sources.SOURCES}
        out = sources.build_html(brief, **self._kanpan())
        for kept in ("快讯标题第 0 条", "快讯标题第 1 条", "快讯标题第 2 条"):
            self.assertIn(kept, out)
        for dropped in ("快讯标题第 3 条", "快讯标题第 7 条"):
            self.assertNotIn(dropped, out)
        self.assertIn("不构成投资建议", out)

    def test_build_html_news_list_is_last_and_hides_sources(self):
        # 「全网快讯」列在正文最后（免责声明与作者署名之前），且整段不出现任何数据源名称。
        # 标题用「源序号-条序」编号，保证跨源不重复，同时不含任何来源名。
        brief = {name: [{"title": f"中立标题 {si}-{i}", "url": f"https://example.com/{si}-{i}"}
                        for i in range(5)] for si, name in enumerate(sources.SOURCES)}
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("全网快讯", out)
        # 位置：AI 看盘之后、免责声明与作者署名之前。
        self.assertLess(out.index("AI 看盘"), out.index("全网快讯"))
        self.assertLess(out.index("全网快讯"), out.index("不构成投资建议"))
        self.assertLess(out.index("全网快讯"), out.index("作者：章鱼 ai"))
        card = out[out.index("全网快讯"):]
        for name in sources.SOURCES:
            self.assertNotIn(name, card)
        # 不带跳转链接，避免域名反向暴露来源。
        self.assertNotIn("<a ", card)
        self.assertNotIn("example.com", card)

    def test_build_html_news_list_dedupes_across_sources(self):
        # 来源被隐藏后，同一条新闻被多源转载只列一次。
        dup = "现货黄金上涨超过7%，报1745.93美元/盎司"
        brief = {
            "金十数据": [{"title": dup, "url": ""}, {"title": "金十独家标题", "url": ""}],
            "财联社 电报": [{"title": dup.replace("，", ", "), "url": ""}, {"title": "财联社独家标题", "url": ""}],
        }
        out = sources.build_html(brief, **self._kanpan())
        card = out[out.index("全网快讯"):]
        self.assertEqual(card.count("现货黄金上涨超过7%"), 1)
        self.assertIn("金十独家标题", card)
        self.assertIn("财联社独家标题", card)

    def test_build_html_news_list_omitted_when_brief_empty(self):
        out = sources.build_html({}, **self._kanpan())
        self.assertNotIn("全网快讯", out)
        self.assertIn("作者：章鱼 ai", out)

    def test_build_html_news_list_titles_are_escaped(self):
        brief = {"金十数据": [{"title": "<b>x</b> 快讯标题 <script>alert(1)</script>", "url": ""}]}
        out = sources.build_html(brief, **self._kanpan())
        card = out[out.index("全网快讯"):]
        self.assertIn("&lt;script&gt;", card)
        self.assertNotIn("<script>alert", card)
        self.assertNotIn("<b>x</b>", card)

    def test_build_html_free_mode_still_capped_at_20k(self):
        # 显式 PUSHPLUS_MEMBER=0 仍退回普通账号口径：抓取/容量上限 19,500 字符、快讯每源精选 3 条。
        os.environ["PUSHPLUS_MEMBER"] = "0"
        try:
            self.assertFalse(sources._is_pushplus_member())
            self.assertEqual(sources.pushplus_quota(), (19500, 3))
            self.assertEqual(sources.default_fetch_limit(), sources.LIMIT)
            huge_brief = {
                name: [{"title": f"{name} 的长标题快讯测试内容" * 3, "url": "https://example.com/t"}
                       for _ in range(60)]
                for name in sources.SOURCES
            }
            out = sources.build_html(huge_brief, **self._kanpan())
            self.assertLessEqual(len(out), 19500)
            # 69 源的超长唯一标题在免费额度下允许按最后档位省略；核心分析必须保留。
            self.assertIn("AI 板块机会", out)
            self.assertNotIn("全网快讯", out)
            # 跨源同题会去重成 3 条，能放下时仍保留正文最后的精选快讯。
            brief = {name: [{"title": f"免费口径快讯第 {i} 条", "url": ""} for i in range(8)]
                     for name in sources.SOURCES}
            out2 = sources.build_html(brief, **self._kanpan())
            self.assertLessEqual(len(out2), 19500)
            for kept in ("免费口径快讯第 0 条", "免费口径快讯第 1 条", "免费口径快讯第 2 条"):
                self.assertIn(kept, out2)
            self.assertNotIn("免费口径快讯第 3 条", out2)
        finally:
            os.environ.pop("PUSHPLUS_MEMBER", None)

    def test_pushplus_quota_env_overrides(self):
        # PUSHPLUS_MAX_CHARS / PUSHPLUS_ITEMS_PER_SOURCE / BRIEF_FETCH_LIMIT 可显式覆盖，无需改代码。
        try:
            os.environ["PUSHPLUS_MAX_CHARS"] = "50000"
            os.environ["PUSHPLUS_ITEMS_PER_SOURCE"] = "6"
            os.environ["BRIEF_FETCH_LIMIT"] = "9"
            self.assertEqual(sources.pushplus_quota(), (50000, 6))
            self.assertEqual(sources.default_fetch_limit(), 9)
            os.environ["PUSHPLUS_ITEMS_PER_SOURCE"] = "all"
            self.assertEqual(sources.pushplus_quota(), (50000, None))
            # 非法值回退默认，不至于把推送配置成 0 字或负数条。
            os.environ["PUSHPLUS_MAX_CHARS"] = "abc"
            os.environ["PUSHPLUS_ITEMS_PER_SOURCE"] = "-3"
            del os.environ["PUSHPLUS_ITEMS_PER_SOURCE"]
            os.environ["PUSHPLUS_ITEMS_PER_SOURCE"] = "xyz"
            self.assertEqual(sources.pushplus_quota()[0], 98000)
            self.assertEqual(sources.pushplus_quota()[1], 20)
        finally:
            for key in ("PUSHPLUS_MAX_CHARS", "PUSHPLUS_ITEMS_PER_SOURCE", "BRIEF_FETCH_LIMIT"):
                os.environ.pop(key, None)

    def test_raw_source_cards_replaced_by_anonymous_news_list(self):
        # 04「监测平台列表与雷达矩阵」仍不渲染；原始快讯改为正文最后的匿名列表（每源 3 条）。
        brief = {name: [{"title": f"唯一原始标题 {si}-{i}", "url": ""} for i in range(3)]
                 for si, name in enumerate(sources.SOURCES)}
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("唯一原始标题", out)
        self.assertNotIn("覆盖 18 个数据源", out)
        self.assertNotIn('class="td-n td-bdr"', out)
        # 69 源 × 3 条 = 207 行，编号全表连续且不带来源名；按六大板块分组。
        total = len(sources.SOURCES) * 3
        news_card = out[out.index("全网快讯"):]
        self.assertEqual(news_card.count("唯一原始标题"), total)
        self.assertIn(f"共 {total} 条", out)
        self.assertIn(f">{total}</td>", out)
        card = out[out.index("全网快讯"):]
        for name in sources.SOURCES:
            self.assertNotIn(name, card)
        for sec in sources.SECTIONS:
            self.assertIn(sec["label"], card)
        self.assertIn("6 个板块", card)

    def test_build_html_custom_max_chars_shrinks_items(self):
        # 自定义较小上限时仍保证生成内容不超出推送限制。
        brief = {name: [{"title": f"{name} 的快讯标题内容示例" * 2, "url": ""} for _ in range(20)]
                 for name in sources.SOURCES}
        out_small = sources.build_html(brief, max_length=24000, **self._kanpan())
        self.assertLessEqual(len(out_small), 24000)
        self.assertIn("AI 政策分析", out_small)   # AI 板块始终保留，只收敛快讯列表

    def test_build_html_shrinks_news_list_before_dropping_it(self):
        # 字符额度越紧，按档位收敛：①每档内快讯 3 → 2 → 1 条；②再降政策深度层档位
        # （全量 → 精简：思维导图与背景长文先去掉 → 整段省略）；③最后才整段省略快讯列表。
        # 四个主分析栏目（每日总结 / 政策分析 / 板块机会 / 看盘）始终保留。
        brief = {name: [{"title": f"快讯条目 {si}-{i}：撑开字符额度的较长标题内容示例文本", "url": ""}
                        for i in range(20)] for si, name in enumerate(sources.SOURCES)}
        n = len(sources.SOURCES)

        def shrink(previous: str) -> str:
            return sources.build_html(brief, max_length=len(previous) - 100, **self._kanpan())

        def news_count(html: str) -> int:
            # 只数「全网快讯」卡片里的条目：政策舆情板块也会引用官方信息源的标题（合法行为）。
            return html.count("快讯条目", html.index("全网快讯")) if "全网快讯" in html else 0

        full = sources.build_html(brief, **self._kanpan())
        self.assertEqual(news_count(full), n * 3)
        self.assertIn("政策影响思维导图", full)          # 政策深度层全量
        two = shrink(full)
        self.assertEqual(news_count(two), n * 2)
        one = shrink(two)
        self.assertEqual(news_count(one), n)
        compact = shrink(one)
        # 快讯已到每源 1 条，再压就先降政策深度层档位：思维导图与背景长文让位，快讯条数回到每源 3 条。
        self.assertNotIn("政策影响思维导图", compact)
        self.assertIn("AI 政策深度", compact)
        self.assertGreaterEqual(news_count(compact), n)
        slim = compact
        while "AI 政策深度" in slim:                      # 继续压：政策深度层整段省略
            slim = shrink(slim)
        self.assertNotIn("AI 政策深度", slim)
        self.assertIn("全网快讯", slim)                   # 快讯列表仍在（省略是最后一步）
        for kept in ("AI 每日总结", "AI 政策分析", "AI 板块机会", "AI 看盘"):
            self.assertIn(kept, slim)
        # 继续压缩才会走到最后一档（快讯整段省略）：档位之间不是严格单调，
        # 因此按「压到快讯消失为止」判定，同时确认四个主分析栏目始终保留。
        none = slim
        for _ in range(6):
            none = shrink(none)
            if "全网快讯" not in none:
                break
        self.assertNotIn("全网快讯", none)                # 最后才整段省略快讯
        for kept in ("AI 每日总结", "AI 政策分析", "AI 板块机会", "AI 看盘"):
            self.assertIn(kept, none)
        self.assertNotIn("快讯条目", none)
        for out in (two, one, none):
            self.assertIn("AI 每日总结", out)
            self.assertIn("AI 政策分析", out)
            self.assertIn("AI 看盘", out)
            self.assertIn("作者：章鱼 ai", out)


class AshareReviewTest(unittest.TestCase):
    """最新 A 股六维度复盘：内容策略、数据解析、复盘日选取、离线兜底与 HTML 渲染。"""

    CANDLE = ("2026-08-27,3911.89,3956.57,3958.03,3909.31,516777549,"
              "1010226573954.60,1.25,1.13,44.05,1.07")

    def test_parse_candle(self):
        candle = sources._parse_ashare_candle(self.CANDLE)
        self.assertEqual(candle["date"], "2026-08-27")
        self.assertEqual(candle["close"], 3956.57)
        self.assertEqual(candle["pct"], 1.13)
        self.assertEqual(candle["change"], 44.05)
        self.assertEqual(candle["amount"], 1010226573954.60)
        self.assertEqual(candle["source"], "eastmoney")
        self.assertIsNone(sources._parse_ashare_candle(""))

    def test_parse_tencent_candle(self):
        # 备用源（腾讯）行格式：[date, open, close, high, low, volume]，涨跌由前收推算。
        row = ["2026-08-28", "3911.89", "3956.57", "3958.03", "3909.31", "516777549.00"]
        candle = sources._parse_tencent_candle(row, prev_close=3912.52)
        self.assertEqual(candle["date"], "2026-08-28")
        self.assertEqual(candle["close"], 3956.57)
        self.assertEqual(candle["change"], 44.05)
        self.assertEqual(candle["pct"], 1.13)
        self.assertIsNone(candle["amount"])  # 腾讯日 K 无成交额字段
        self.assertEqual(candle["source"], "tencent")
        # 首行无前收：pct/change 置 None（由抓取层丢弃，只用于校准）。
        first = sources._parse_tencent_candle(row, prev_close=None)
        self.assertIsNone(first["pct"])
        self.assertIsNone(first["change"])
        # 非法输入
        self.assertIsNone(sources._parse_tencent_candle(None, 1.0))
        self.assertIsNone(sources._parse_tencent_candle(["2026-08-28", "x", "y"], 1.0))

    def test_fetch_klines_backup_fills_missing_indices(self):
        # 主源（东方财富）失联/缺指数时，备用源（腾讯）自动补位；主源已有的指数不被覆盖。
        em_candle = sources._parse_ashare_candle(self.CANDLE)
        orig_em = sources._fetch_ashare_klines_eastmoney
        orig_tx = sources._fetch_ashare_klines_tencent
        tx_candle = sources._parse_tencent_candle(
            ["2026-08-27", "3911.89", "3900.00", "3958.03", "3909.31", "1"], prev_close=3850.0)
        try:
            # 场景一：主源全挂 → 全部来自备用源。
            sources._fetch_ashare_klines_eastmoney = lambda: {}
            sources._fetch_ashare_klines_tencent = lambda: {
                name: {"2026-08-27": dict(tx_candle)}
                for name, _ in sources._ASHARE_TX_SYMBOLS
            }
            klines = sources._fetch_ashare_klines()
            self.assertEqual(len(klines), 4)
            self.assertEqual(klines["上证指数"]["2026-08-27"]["source"], "tencent")

            # 场景二：主源只缺一个指数 → 备用源只补缺口，不覆盖主源数据。
            sources._fetch_ashare_klines_eastmoney = lambda: {
                "上证指数": {"2026-08-27": dict(em_candle)},
                "深证成指": {"2026-08-27": dict(em_candle)},
                "创业板指": {"2026-08-27": dict(em_candle)},
            }
            klines = sources._fetch_ashare_klines()
            self.assertEqual(klines["上证指数"]["2026-08-27"]["source"], "eastmoney")
            self.assertEqual(klines["科创50"]["2026-08-27"]["source"], "tencent")

            # 场景三：主源齐全 → 不触发备用源。
            def _boom():
                raise AssertionError("主源齐全时不应调用备用源")
            sources._fetch_ashare_klines_eastmoney = lambda: {
                name: {"2026-08-27": dict(em_candle)} for name, _ in sources._ASHARE_INDICES
            }
            sources._fetch_ashare_klines_tencent = _boom
            klines = sources._fetch_ashare_klines()
            self.assertEqual(len(klines), 4)
        finally:
            sources._fetch_ashare_klines_eastmoney = orig_em
            sources._fetch_ashare_klines_tencent = orig_tx

    def test_build_market_from_tencent_backup(self):
        # 完全由备用源组装：source=tencent，成交额（备用源无此字段）自动降级为 None。
        orig_pools = sources._fetch_ashare_pools
        sources._fetch_ashare_pools = lambda review_date: {
            "limit_up": None, "limit_down": None, "hot_sectors": [], "cold_sectors": []}
        try:
            candle = sources._parse_tencent_candle(
                ["2026-08-28", "3911.89", "3956.57", "3958.03", "3909.31", "1"],
                prev_close=3912.52)
            klines = {name: {"2026-08-28": dict(candle)}
                      for name, _ in sources._ASHARE_TX_SYMBOLS}
            market = sources._build_ashare_market(klines, date(2026, 8, 28))
        finally:
            sources._fetch_ashare_pools = orig_pools
        self.assertEqual(market["source"], "tencent")
        self.assertEqual(market["date"], "2026-08-28")
        self.assertEqual(len(market["indices"]), 4)
        self.assertIsNone(market["turnover"])

    def test_pick_review_date_is_latest_trading_day(self):
        # 今天 2026-08-29（周六）→ 最近交易日 = 2026-08-28（周五）。
        klines = {
            "上证指数": {"2026-08-27": {}, "2026-08-28": {}},
            "深证成指": {"2026-08-27": {}},
        }
        from datetime import date
        self.assertEqual(sources._pick_review_date(klines, date(2026, 8, 29)), "2026-08-28")
        # 周一 2026-08-31（接口已有当天日 K）：直接复盘 2026-08-31。
        klines_mon = {
            "上证指数": {"2026-08-28": {}, "2026-08-31": {}},
            "深证成指": {"2026-08-28": {}, "2026-08-31": {}},
        }
        self.assertEqual(sources._pick_review_date(klines_mon, date(2026, 8, 31)), "2026-08-31")
        # 周一 2026-08-31（盘前接口仅到周五）：自动回溯到 2026-08-28。
        self.assertEqual(sources._pick_review_date(klines, date(2026, 8, 31)), "2026-08-28")

    def test_get_fallback_review_date(self):
        from datetime import date
        # 周日 2026-08-30 → 最近交易日周五 2026-08-28
        self.assertEqual(sources._get_fallback_review_date(date(2026, 8, 30)), "2026-08-28")
        # 周一 2026-08-31 → 当天 2026-08-31
        self.assertEqual(sources._get_fallback_review_date(date(2026, 8, 31)), "2026-08-31")
        # 周六 2026-08-29 → 周五 2026-08-28
        self.assertEqual(sources._get_fallback_review_date(date(2026, 8, 29)), "2026-08-28")
        # 周五 2026-08-28 → 当天 2026-08-28
        self.assertEqual(sources._get_fallback_review_date(date(2026, 8, 28)), "2026-08-28")

    def test_analyze_ashare_has_six_dimensions(self):
        review = sources.analyze_ashare(market=sources._ASHARE_SNAPSHOT)
        self.assertEqual(review["date"], sources._ASHARE_SNAPSHOT["date"])
        self.assertEqual(
            [p["label"] for p in review["points"]],
            ["三大指数", "两市成交额", "涨跌家数与涨跌停",
             "领涨 / 领跌板块", "主力资金与北向资金", "后市观点与策略"],
        )
        self.assertTrue(all(p["text"] for p in review["points"]))
        self.assertIn(review["bias"], ("偏多", "偏空", "中性"))

    def test_analyze_ashare_snapshot_content(self):
        review = sources.analyze_ashare(market=sources._ASHARE_SNAPSHOT)
        texts = {p["label"]: p["text"] for p in review["points"]}
        # ① 指数：四大指数收盘与涨幅
        self.assertIn("3956.57", texts["三大指数"])
        self.assertIn("科创50", texts["三大指数"])
        # ② 成交额：2.13 万亿、放量 3172 亿（与新浪/每经收评交叉核对口径）
        self.assertIn("2.13 万亿", texts["两市成交额"])
        self.assertIn("3,172 亿", texts["两市成交额"])
        # ③ 涨跌家数与涨跌停：涨停 77 / 跌停 3 / 封板率 82%
        self.assertIn("涨停 77 家", texts["涨跌家数与涨跌停"])
        self.assertIn("跌停 3 家", texts["涨跌家数与涨跌停"])
        # ④ 领涨/领跌板块
        self.assertIn("算力硬件", texts["领涨 / 领跌板块"])
        self.assertIn("银行", texts["领涨 / 领跌板块"])
        # ⑤ 北向口径说明
        self.assertIn("北向资金", texts["主力资金与北向资金"])
        # ⑥ 后市观点与策略
        self.assertIn("AI 看盘", texts["后市观点与策略"])
        self.assertIn("浙商证券", texts["后市观点与策略"])

    def test_snapshot_headline_mentions_leaders(self):
        review = sources.analyze_ashare(market=sources._ASHARE_SNAPSHOT)
        self.assertIn("算力硬件", review["headline"])
        self.assertIn("存储芯片", review["headline"])

    def test_get_ashare_market_falls_back_offline(self):
        # 无外网环境（如 CI 沙箱）：网络层抛错时必须回退内置真实快照（且复盘日期基于当前日期动态推导），绝不空转。
        original = sources.urlopen

        def _offline(*args, **kwargs):
            raise OSError("network unreachable")

        sources.urlopen = _offline
        try:
            market = sources.get_ashare_market()
        finally:
            sources.urlopen = original
        self.assertEqual(market["date"], sources._get_fallback_review_date())
        self.assertEqual(market["source"], "snapshot")

    def test_build_html_renders_ashare_review_card(self):
        brief = {name: sources._demo_items(name)[:2] for name in sources.SOURCES}
        review = sources.analyze_ashare(market=sources._ASHARE_SNAPSHOT)
        hk_review = sources.analyze_hk(market=sources._HK_SNAPSHOT)
        us_review = sources.analyze_us(market=sources._US_SNAPSHOT)
        out = sources.build_html(brief, review=review, hk_review=hk_review, us_review=us_review)
        self.assertIn("AI 看盘", out)
        # 05「数据获取与时间核对」不进入看盘正文；行情分析本身仍保留。
        # 政策深度会写已核验发文日（其中 8 月 28 日与快照日同一天），不能拿全文字符串当看盘日期泄漏的判据；
        # 「AI 看盘」现在紧跟「AI 每日总结」、排在热力图与政策卡之前，所以只截取看盘卡本身（到下一张卡为止）。
        kanpan_start = out.index('<span class="tag">AI 看盘</span>')
        kanpan = out[kanpan_start:out.index('<span class="tag">AI 情绪热力图</span>', kanpan_start)]
        self.assertNotIn(sources._ASHARE_SNAPSHOT["date"], kanpan)
        self.assertNotIn("东方财富行情接口", out)
        for label in ("三大指数", "两市成交额", "涨跌家数与涨跌停",
                      "领涨 / 领跌板块", "主力资金与北向资金", "后市观点与策略"):
            self.assertIn(label, out)
        self.assertIn("偏多", out)
        # 同一张「AI 看盘」卡覆盖 A 股 / 港股 / 美股。
        self.assertIn("港股", out)
        self.assertIn("美股", out)
        self.assertIn("恒生指数", out)
        self.assertIn("纳斯达克", out)
        self.assertIn("南向资金", out)
        self.assertIn("资金与避险", out)

    def test_review_sector_names_are_escaped(self):
        market = dict(sources._ASHARE_SNAPSHOT)
        market["leaders"] = [{"name": "<算力>", "note": "x"}]
        market["laggards"] = [{"name": "银行", "note": "x"}]
        brief = {"金十数据": [{"title": "x", "url": ""}]}
        out = sources.build_html(
            brief,
            review=sources.analyze_ashare(market=market),
            hk_review=sources.analyze_hk(market=sources._HK_SNAPSHOT),
            us_review=sources.analyze_us(market=sources._US_SNAPSHOT),
        )
        self.assertIn("&lt;算力&gt;", out)
        self.assertNotIn("<算力>", out)


class OverseasReviewTest(unittest.TestCase):
    """港股 / 美股看盘：六维度内容、离线兜底与 HTML 渲染。"""

    def test_analyze_hk_has_six_dimensions(self):
        review = sources.analyze_hk(market=sources._HK_SNAPSHOT)
        self.assertEqual(review["date"], sources._HK_SNAPSHOT["date"])
        self.assertEqual(
            [p["label"] for p in review["points"]],
            ["三大指数", "港股成交额", "涨跌家数",
             "领涨 / 领跌板块", "南向资金", "后市观点与策略"],
        )
        self.assertTrue(all(p["text"] for p in review["points"]))
        self.assertIn(review["bias"], ("偏多", "偏空", "中性"))
        self.assertEqual(review["market"], "hk")

    def test_analyze_hk_snapshot_content(self):
        review = sources.analyze_hk(market=sources._HK_SNAPSHOT)
        texts = {p["label"]: p["text"] for p in review["points"]}
        self.assertIn("25842.16", texts["三大指数"])
        self.assertIn("恒生科技", texts["三大指数"])
        self.assertIn("1,862 亿港元", texts["港股成交额"])
        self.assertIn("超1200只", texts["涨跌家数"])
        self.assertIn("科技硬件", texts["领涨 / 领跌板块"])
        self.assertIn("地产", texts["领涨 / 领跌板块"])
        self.assertIn("南向资金", texts["南向资金"])
        self.assertIn("AI 看盘", texts["后市观点与策略"])
        self.assertIn("高盛", texts["后市观点与策略"])

    def test_analyze_us_has_six_dimensions(self):
        review = sources.analyze_us(market=sources._US_SNAPSHOT)
        self.assertEqual(review["date"], sources._US_SNAPSHOT["date"])
        self.assertEqual(
            [p["label"] for p in review["points"]],
            ["三大指数", "美股成交额", "涨跌家数",
             "领涨 / 领跌板块", "资金与避险", "后市观点与策略"],
        )
        self.assertTrue(all(p["text"] for p in review["points"]))
        self.assertIn(review["bias"], ("偏多", "偏空", "中性"))
        self.assertEqual(review["market"], "us")

    def test_analyze_us_snapshot_content(self):
        review = sources.analyze_us(market=sources._US_SNAPSHOT)
        texts = {p["label"]: p["text"] for p in review["points"]}
        self.assertIn("6488.20", texts["三大指数"])
        self.assertIn("纳斯达克", texts["三大指数"])
        self.assertIn("4,820 亿美元", texts["美股成交额"])
        self.assertIn("半导体", texts["领涨 / 领跌板块"])
        self.assertIn("能源", texts["领涨 / 领跌板块"])
        self.assertIn("VIX", texts["资金与避险"])
        self.assertIn("AI 看盘", texts["后市观点与策略"])
        self.assertIn("英伟达", texts["后市观点与策略"])

    def test_hk_us_headline_mentions_leaders(self):
        hk = sources.analyze_hk(market=sources._HK_SNAPSHOT)
        self.assertIn("科技硬件", hk["headline"])
        self.assertIn("恒生科技", hk["headline"])
        us = sources.analyze_us(market=sources._US_SNAPSHOT)
        self.assertIn("半导体", us["headline"])
        self.assertIn("纳斯达克", us["headline"])

    def test_get_hk_us_market_falls_back_offline(self):
        original = sources.urlopen

        def _offline(*args, **kwargs):
            raise OSError("network unreachable")

        sources.urlopen = _offline
        try:
            hk = sources.get_hk_market()
            us = sources.get_us_market()
        finally:
            sources.urlopen = original
        self.assertEqual(hk["date"], sources._get_fallback_review_date())
        self.assertEqual(hk["source"], "snapshot")
        self.assertEqual(us["date"], sources._get_fallback_review_date())
        self.assertEqual(us["source"], "snapshot")

    def test_build_simple_market_from_klines(self):
        candle = sources._parse_ashare_candle(
            "2026-08-28,25000.0,25842.16,25900.0,24900.0,1000000,186200000000.0,1.8,1.28,326.40,1.0")
        klines = {name: {"2026-08-28": dict(candle)} for name, _ in sources._HK_INDICES}
        market = sources._build_simple_market(
            klines, date(2026, 8, 28),
            [name for name, _ in sources._HK_INDICES], sources._HK_SNAPSHOT,
            turnover_names=["恒生指数"], turnover_unit="亿港元",
        )
        self.assertEqual(market["source"], "eastmoney")
        self.assertEqual(market["date"], "2026-08-28")
        self.assertEqual(len(market["indices"]), 3)
        self.assertEqual(market["turnover"]["amount"], 1862)
        self.assertEqual(market["turnover"]["unit"], "亿港元")


class MarketFreshnessTest(unittest.TestCase):
    """推送前大盘数据新鲜度检查：不是最新就不推。

    行情接口全部走本地注入的日 K，杜绝测试依赖外网。
    场景基准：today = 2026-08-31（周一），最新日 K = 2026-08-31。
    """

    TODAY = date(2026, 8, 31)
    EXPECTED = "2026-08-31"

    def setUp(self):
        self._orig_klines = sources._fetch_ashare_klines
        self._orig_pools = sources._fetch_ashare_pools
        # 涨停/跌停池与网络无关：注入空结果即可。
        sources._fetch_ashare_pools = lambda review_date: {
            "limit_up": None, "limit_down": None, "hot_sectors": [], "cold_sectors": []}

    def tearDown(self):
        sources._fetch_ashare_klines = self._orig_klines
        sources._fetch_ashare_pools = self._orig_pools
        os.environ.pop("MARKET_FRESHNESS_FORCE", None)

    def _klines(self, *dates):
        """构造四大指数的日 K：{指数名: {日期: candle}}。"""
        out = {}
        for name in ("上证指数", "深证成指", "创业板指", "科创50"):
            out[name] = {
                d: sources._parse_ashare_candle(
                    f"{d},3900.0,3950.0,3960.0,3890.0,1000000,200000000000.0,1.8,1.28,50.0,1.0")
                for d in dates
            }
        return out

    def _tx_klines(self, *dates):
        """构造四大指数的备用源（腾讯）日 K：无成交额，涨跌由前收推算。"""
        out = {}
        for name in ("上证指数", "深证成指", "创业板指", "科创50"):
            candles = {}
            prev_close = 3900.0
            for d in dates:
                candle = sources._parse_tencent_candle(
                    [d, "3900.0", "3950.0", "3960.0", "3890.0", "1"], prev_close)
                candles[d] = candle
                prev_close = candle["close"]
            out[name] = candles
        return out

    def test_fresh_market_passes_gate(self):
        # 接口正常：最新日 K = 今天，复盘日 = 2026-08-31 → 放行。
        klines = self._klines("2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31")
        market = sources._build_ashare_market(klines, self.TODAY)
        fresh = sources.check_market_freshness(market, klines=klines, today=self.TODAY)
        self.assertTrue(fresh["ok"], fresh["reason"])
        self.assertEqual(fresh["market_date"], self.EXPECTED)
        self.assertEqual(fresh["expected_date"], self.EXPECTED)
        self.assertEqual(fresh["latest_kline_date"], "2026-08-31")
        self.assertEqual(fresh["source"], "eastmoney")
        self.assertIn("最新", fresh["reason"])

    def test_tencent_backup_market_passes_gate(self):
        # 主源失联但备用源（腾讯）数据最新：source=tencent 同样放行。
        klines = self._tx_klines("2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31")
        market = sources._build_ashare_market(klines, self.TODAY)
        fresh = sources.check_market_freshness(market, klines=klines, today=self.TODAY)
        self.assertTrue(fresh["ok"], fresh["reason"])
        self.assertEqual(fresh["source"], "tencent")
        self.assertEqual(fresh["market_date"], self.EXPECTED)
        self.assertIn("备用", fresh["reason"])

    def test_offline_snapshot_is_blocked(self):
        # 行情接口不可用 → 无法确认数据新旧 → 拦截（宁可不放行）。
        fresh = sources.check_market_freshness(klines={}, today=self.TODAY)
        self.assertFalse(fresh["ok"])
        self.assertIn("行情接口不可用", fresh["reason"])
        self.assertEqual(fresh["source"], "snapshot")

    def test_lagging_feed_is_blocked(self):
        # 接口能通，但最新日 K 早于内置快照基线 → 行情源异常 → 拦截。
        klines = self._klines("2026-08-19", "2026-08-20")
        market = sources._build_ashare_market(klines, self.TODAY)
        fresh = sources.check_market_freshness(market, klines=klines, today=self.TODAY)
        self.assertFalse(fresh["ok"])
        self.assertIn("滞后", fresh["reason"])

    def test_stale_review_date_is_blocked(self):
        # 数据来源是实时接口，但复盘日落后于应复盘交易日 → 拦截。
        klines = self._klines("2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31")
        market = {"date": "2026-08-28", "source": "eastmoney"}
        fresh = sources.check_market_freshness(market, klines=klines, today=self.TODAY)
        self.assertFalse(fresh["ok"])
        self.assertIn("不一致", fresh["reason"])

    def test_snapshot_fallback_is_blocked_even_if_date_matches(self):
        # 即便快照日期恰好等于应复盘日，数值也是冻结的旧数据 → 仍按非最新拦截。
        klines = self._klines("2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31")
        market = {"date": self.EXPECTED, "source": "snapshot"}
        fresh = sources.check_market_freshness(market, klines=klines, today=self.TODAY)
        self.assertFalse(fresh["ok"])
        self.assertIn("内置快照", fresh["reason"])

    def test_collect_market_for_push_returns_both(self):
        # 推送入口：一次抓取同时返回 market 与 freshness，且两者指向同一份日 K。
        klines = self._klines("2026-08-27", "2026-08-28", "2026-08-31")
        sources._fetch_ashare_klines = lambda: klines
        market, fresh = sources.collect_market_for_push()
        self.assertEqual(market["date"], self.EXPECTED)
        self.assertTrue(fresh["ok"], fresh["reason"])

    def test_force_env_pins_gate_result(self):
        # MARKET_FRESHNESS_FORCE=fresh|stale（测试/应急）可以强制检查结果。
        klines = self._klines("2026-08-27", "2026-08-28", "2026-08-31")
        sources._fetch_ashare_klines = lambda: klines

        os.environ["MARKET_FRESHNESS_FORCE"] = "stale"
        _, fresh = sources.collect_market_for_push()
        self.assertFalse(fresh["ok"])
        self.assertTrue(fresh.get("forced"))
        self.assertIn("MARKET_FRESHNESS_FORCE=stale", fresh["reason"])

        os.environ["MARKET_FRESHNESS_FORCE"] = "fresh"
        _, fresh = sources.collect_market_for_push()
        self.assertTrue(fresh["ok"])
        self.assertTrue(fresh.get("forced"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
