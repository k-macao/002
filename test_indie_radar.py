#!/usr/bin/env python3
"""indie_radar 回归：信号采集 → 7 模块 → 心法 → 页面区块，全程离线。

    python3 test_indie_radar.py

覆盖点：
- 采集器按源解析（GitHub / V2EX / RSS / App Store / IndieHackers），失败不抛；
- 需要 Key 的源标「未配置」而不是失败；整体失败不中断；
- 每条信号只归一个模块，命中词写进 matched；
- 不编造：条目必须能指回输入信号；没素材的模块留空并给原因；
- 心法从当日命中反推，没有信号时给 missing；
- 「AI 提」包含模块定义、当日条目与 JSON schema；
- 页面区块结构（供 index.html / app.js 共用）。
"""
import json
import unittest

import indie_radar as ir

GITHUB_HTML = """
<article class="Box-row">
  <h2><a href="/alice/tinycms">alice / tinycms</a></h2>
  <p class="col-9 color-fg-muted">A tiny CMS you can launch in one command.</p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/alice/tinycms/stargazers">12,345</a>
  <span class="d-inline-block float-sm-right">1,204 stars today</span>
</article>
<article class="Box-row">
  <h2><a href="/bob/saas-kit">bob / saas-kit</a></h2>
  <p class="col-9 color-fg-muted">Boilerplate with pricing page and MRR dashboard.</p>
  <span itemprop="programmingLanguage">TypeScript</span>
  <a href="/bob/saas-kit/stargazers">2,100</a>
  <span class="d-inline-block float-sm-right">320 stars today</span>
</article>
"""

V2EX_JSON = [
    {"id": 101, "title": "做了个工具，想听听大家意见", "content": "一个 CLI 小工具",
     "replies": 24, "node": {"title": "分享创造"}},
    {"id": 102, "title": "求推荐能替代 Notion 的笔记方案", "content": "需求：本地优先",
     "replies": 8, "node": {"title": "程序员"}},
]

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>开源社区发布新框架</title><link>https://example.com/a</link>
<description>一个脚手架</description></item>
<item><title>某产品上线三天翻车</title><link>https://example.com/b</link>
<description>踩坑记录</description></item>
</channel></rss>"""

APPSTORE_JSON = {"feed": {"entry": [
    {"im:name": {"label": "Tiny Habit"}, "id": {"attributes": {"im:id": "123"}},
     "link": {"attributes": {"href": "https://apps.apple.com/app/id123"}},
     "category": {"attributes": {"label": "效率"}}, "im:price": {"label": "$0.99"}},
]}}

ALGOLIA_JSON = {"hits": [
    {"title": "How I got to $8k MRR with a Chrome extension", "objectID": "p1",
     "revenueRange": "$5k-10k"},
]}


def sample_signals():
    return {
        "fetched_at": "2026-10-28 09:00",
        "items": [
            {"source": "github", "source_name": "GitHub 总榜", "title": "alice/tinycms",
             "url": "https://github.com/alice/tinycms",
             "note": "A tiny CMS you can launch in one command.", "points": 1204, "tags": []},
            {"source": "github", "source_name": "GitHub 总榜", "title": "bob/saas-kit",
             "url": "https://github.com/bob/saas-kit",
             "note": "Boilerplate with pricing page and MRR dashboard.", "points": 320, "tags": []},
            {"source": "v2ex", "source_name": "V2EX", "title": "[分享创造] 做了个工具，想听听大家意见",
             "url": "https://www.v2ex.com/t/101", "note": "一个 CLI 小工具", "points": 24, "tags": []},
            {"source": "v2ex", "source_name": "V2EX", "title": "[程序员] 求推荐能替代 Notion 的笔记方案",
             "url": "https://www.v2ex.com/t/102", "note": "需求：本地优先", "points": 8, "tags": []},
            {"source": "kr36", "source_name": "36氪", "title": "某产品上线三天翻车",
             "url": "https://example.com/b", "note": "踩坑记录", "points": 0, "tags": []},
        ],
        "sources": [
            {"key": "github", "name": "GitHub 总榜", "status": "ok", "note": "2 条"},
            {"key": "v2ex", "name": "V2EX", "status": "ok", "note": "2 条"},
            {"key": "kr36", "name": "36氪", "status": "ok", "note": "1 条"},
            {"key": "producthunt", "name": "Product Hunt", "status": "skipped",
             "note": "未配置 PH_TOKEN"},
        ],
        "ok": 3, "skipped": 1, "error": 0, "empty": 0,
    }


class ModuleTest(unittest.TestCase):
    def setUp(self):
        self.report = ir.analyze(signals=sample_signals(), as_of="2026-10-28")

    def test_all_modules_present_and_ordered(self):
        keys = [module["key"] for module in ir.MODULES]
        self.assertEqual(keys, ["opportunities", "money_cases", "ideas", "growth",
                                "tools", "pitfalls", "data_signals"])
        for key in keys:
            self.assertIn(key, self.report["modules"])

    def test_exclusive_assignment(self):
        titles = [entry["title"] for key, entries in self.report["modules"].items()
                  if key != "data_signals" for entry in entries]
        self.assertEqual(len(titles), len(set(titles)), "同一条信号不能出现在两个模块里")

    def test_matches_are_recorded(self):
        for key, entries in self.report["modules"].items():
            if key == "data_signals":
                continue
            for entry in entries:
                self.assertTrue(entry["matched"], f"{key} 的条目没有命中词：{entry['title']}")
                self.assertTrue(entry["url"].startswith("http"))

    def test_selection_is_rule_based(self):
        # 「launch」只落在「今日机会」，不会被同一条同时算进「今日工具」
        opportunities = [entry["title"] for entry in self.report["modules"]["opportunities"]]
        self.assertIn("alice/tinycms", opportunities)
        # 描述里带 MRR / pricing 的进「今日赚钱案例」
        money = [entry["title"] for entry in self.report["modules"]["money_cases"]]
        self.assertIn("bob/saas-kit", money)
        # 「工具」来自标题本身，不来自空描述
        tools = [entry["title"] for entry in self.report["modules"]["tools"]]
        self.assertTrue(any("工具" in title for title in tools), tools)
        # 「求推荐」是需求 → 产品灵感
        ideas = [entry["title"] for entry in self.report["modules"]["ideas"]]
        self.assertTrue(any("Notion" in title for title in ideas), ideas)

    def test_no_invented_items(self):
        known = {item["title"] for item in sample_signals()["items"]}
        for key, entries in self.report["modules"].items():
            if key == "data_signals":
                continue
            for entry in entries:
                self.assertIn(entry["title"], known)

    def test_missing_module_states(self):
        empty = ir.analyze(signals={"items": [], "sources": [], "ok": 0,
                                    "skipped": 0, "error": 0})
        self.assertTrue(all(state["status"] == "missing"
                            for state in empty["module_status"].values()))
        self.assertIn("没有取到信号", empty["headline"])
        self.assertEqual(empty["wisdom"]["status"], "missing")

    def test_wisdom_is_derived(self):
        wisdom = self.report["wisdom"]
        self.assertEqual(wisdom["status"], "live")
        self.assertTrue(wisdom["steps"])
        self.assertIn(str(len(sample_signals()["items"])), wisdom["derivation"])


class DataSignalTest(unittest.TestCase):
    def test_counts_only_real_numbers(self):
        report = ir.analyze(signals=sample_signals())
        observations = " ".join(entry["observation"] for entry in report["modules"]["data_signals"])
        self.assertIn("3 个源有信号", observations)
        self.assertIn("1 个未配置", observations)
        self.assertIn("1204", observations)            # 热度来自真实 points

    def test_heat_is_labelled_as_public_counter(self):
        report = ir.analyze(signals=sample_signals())
        judgments = " ".join(entry["judgment"] for entry in report["modules"]["data_signals"])
        self.assertIn("不等于收入", judgments)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self._text, self._json = ir.GET_TEXT, ir.GET_JSON
        ir._CACHE.update({"at": 0.0, "payload": None})

        def fake_text(url, headers=None, timeout=None):
            if "github.com/trending" in url:
                return GITHUB_HTML
            if url.startswith("https://36kr.com/feed"):
                return RSS_XML
            raise OSError("blocked")

        def fake_json(url, params=None, headers=None, body=None):
            if "v2ex" in url:
                return V2EX_JSON
            if "itunes.apple.com" in url:
                return APPSTORE_JSON
            if "algolia" in url:
                return ALGOLIA_JSON
            raise OSError("blocked")

        ir.GET_TEXT, ir.GET_JSON = fake_text, fake_json

    def tearDown(self):
        ir.GET_TEXT, ir.GET_JSON = self._text, self._json
        ir._CACHE.update({"at": 0.0, "payload": None})

    def test_parsers(self):
        data = ir.collect_signals(use_cache=False)
        by_key = {source["key"]: source for source in data["sources"]}
        self.assertEqual(by_key["github"]["status"], "ok")
        self.assertEqual(by_key["v2ex"]["status"], "ok")
        self.assertEqual(by_key["appstore-cn"]["status"], "ok")
        self.assertEqual(by_key["indiehackers"]["status"], "ok")
        self.assertEqual(by_key["sspai"]["status"], "error")       # 被挡住 → error 不是崩溃
        self.assertEqual(by_key["producthunt"]["status"], "skipped")
        titles = [item["title"] for item in data["items"]]
        self.assertIn("alice/tinycms", titles)
        self.assertIn("Tiny Habit", titles)
        self.assertTrue(any("MRR" in title for title in titles))

    def test_github_points_and_language(self):
        data = ir.collect_signals(use_cache=False)
        repo = next(item for item in data["items"] if item["title"] == "alice/tinycms")
        self.assertEqual(repo["points"], 1204)         # 今日 star 增量优先
        self.assertEqual(repo["tags"], ["Python"])

    def test_key_sources_are_skipped_not_failed(self):
        data = ir.collect_signals(use_cache=False)
        skipped = [source for source in data["sources"] if source["status"] == "skipped"]
        self.assertTrue(skipped)
        self.assertTrue(all("未配置" in source["note"] for source in skipped))

    def test_cache_is_reused(self):
        first = ir.collect_signals(use_cache=True)
        second = ir.collect_signals(use_cache=True)
        self.assertIs(first, second)

    def test_all_sources_down_is_handled(self):
        def boom(*args, **kwargs):
            raise OSError("no network")

        ir.GET_TEXT, ir.GET_JSON = boom, boom
        data = ir.collect_signals(use_cache=False)
        self.assertEqual(data["items"], [])
        self.assertGreater(data["error"], 0)
        report = ir.analyze(signals=data)
        self.assertIn("没有取到信号", report["headline"])


class PromptTest(unittest.TestCase):
    def test_prompt_contains_modules_and_schema(self):
        prompt = ir.prompt_today(signals=sample_signals(), as_of="2026-10-28")
        self.assertIn(ir.AI_PROMPT.strip().splitlines()[0], prompt)
        for module in ir.MODULES:
            if module["key"] == "data_signals":
                continue
            self.assertIn(module["label"], prompt)
        for key in ("opportunities", "moneyCases", "productInspirations", "growthTips",
                    "tools", "pitfalls", "dataSignals", "dailyWisdom"):
            self.assertIn(key, ir.AI_PROMPT)
        self.assertIn("2026-10-28", prompt)
        self.assertIn("https://github.com/alice/tinycms", prompt)
        self.assertIn("不编造", ir.AI_PROMPT)

    def test_prompt_without_signals_is_honest(self):
        prompt = ir.prompt_today(signals={"items": []})
        self.assertIn("不要虚构条目", prompt)


class FrameworkTest(unittest.TestCase):
    def test_nineteen_sources(self):
        # 对齐上游 README 的「19 个信息源」
        self.assertEqual(len(ir.RADAR_SOURCES), 19)
        keys = [source["key"] for source in ir.RADAR_SOURCES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_key_requirement_is_declared(self):
        needs = {source["key"]: source["needs_key"] for source in ir.RADAR_SOURCES
                 if source["needs_key"]}
        self.assertEqual(needs["producthunt"], "PH_TOKEN")
        self.assertEqual(needs["trustmrr"], "TRUSTMRR_API_KEY")

    def test_framework_payload(self):
        payload = ir.framework()
        self.assertEqual(payload["upstream"], "https://github.com/susumr/radar-dashboard")
        self.assertEqual(len(payload["modules"]), 7)
        self.assertIn("prompt", payload)
        self.assertNotIn("url", payload["sources"][0])   # 源清单不对外暴露抓取地址

    def test_status_line(self):
        report = ir.analyze(signals=sample_signals())
        self.assertIn("5 条信号", ir.status_line(report))


class PageSectionTest(unittest.TestCase):
    def test_section_markup(self):
        html = ir.page_section_html(ir.analyze(signals=sample_signals(), as_of="2026-10-28"))
        self.assertIn('id="indieRadarSection"', html)
        self.assertIn("AI 独立开发雷达", html)
        self.assertIn("今日机会", html)
        self.assertIn("今日心法", html)
        self.assertIn("https://github.com/alice/tinycms", html)
        self.assertIn("radarPill", html)

    def test_empty_report_does_not_crash(self):
        html = ir.page_section_html(ir.analyze(signals={"items": [], "sources": []}))
        self.assertIn("indieRadarSection", html)
        self.assertNotIn("今日心法", html)

    def test_escaped_and_safe_links(self):
        report = ir.analyze(signals={"items": [
            {"source": "v2ex", "source_name": "V2EX", "title": "<script>x</script> 开源工具",
             "url": "javascript:alert(1)", "note": "", "points": 1, "tags": []},
        ], "sources": [], "ok": 1, "skipped": 0, "error": 0})
        html = ir.page_section_html(report)
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
