#!/usr/bin/env python3
"""去 AI 味闸门（hallmark_deai.py）单测：零依赖、可离线运行。

覆盖：体检分数、逐 gate 改写、事实保护（fact-lock 回滚）、开关/强制/严格模式、
两个面（推送 HTML / 管理页）、reports() 结构，以及与推送链路的先后关系。
"""
import contextlib
import json
import os
import re
import unittest
from unittest import mock

import hallmark_deai as hd

CLEAN_HTML = (
    "<style>/* Hallmark · de-AI floor · clean fixture */"
    "a:focus-visible{outline:2px solid currentColor;}</style>"
    "<div class='card'><h2>每日简报</h2>"
    "<p>2026-10-27 A 股成交额 1.2 万亿元，北向净流入 35 亿元。</p>"
    "<a href='https://example.com/news/42'>来源链接</a></div>"
)

SLOPPY_HTML = (
    "<style>.kicker{text-transform:uppercase;letter-spacing:.14em;}"
    "body{overflow-x:hidden;min-height:100vh;background:#fff;}"
    "a{transition:all .3s;}</style>"
    "<div class='eyebrow'><span class='live-dot'></span>DAILY BRIEF</div>"
    "<h1>章鱼 AI·<em>全景分析</em></h1>"
    "<span class='sub'>✦ 40 个关键词 · 202 条线索 · 信号 77 条</span>"
    "<p>2026-10-27 成交额 1.2 万亿元，北向净流入 35 亿元。</p>"
    "<a href='https://example.com/news/42'>来源链接</a>"
)

# 只含「需人工判断」的 Floor 命中（gate 19 文案套话，auto=False）。
# 用于验证 DEAI_STRICT=1：自动改写无法清零 Floor 时必须拦截推送。
RESIDUAL_HTML = (
    "<style>/* Hallmark · de-AI floor · residual fixture */"
    "a:focus-visible{outline:2px solid currentColor;}</style>"
    "<div class='card'><h2>每日简报</h2>"
    "<p>本方案赋能产业链，打造闭环生态，未来可期，让我们拭目以待。</p>"
    "<a href='https://example.com/news/42'>来源链接</a></div>"
)


def _facts(s: str):
    """提取可见文本里的数字、URL 与链接数，作为事实签名。"""
    visible = hd._visible_text(s)
    return (sorted(hd._RE_NUMBER.findall(visible)),
            sorted(hd._RE_URL.findall(s)), len(hd._RE_ANCHOR.findall(s)))


class EnvMixin(unittest.TestCase):
    """每个用例前后都清掉去 AI 味开关，避免环境污染。"""

    _ENV_KEYS = ("DEAI_MODE", "SKIP_DEAI_CHECK", "DEAI_FORCE", "DEAI_STRICT")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self._ENV_KEYS}

    def tearDown(self):
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)
            if self._saved[k] is not None:
                os.environ[k] = self._saved[k]


class AuditTest(unittest.TestCase):
    def test_clean_content_scores_high_and_has_no_floor_findings(self):
        rep = hd.audit(CLEAN_HTML)
        self.assertGreaterEqual(rep["score"], 80)
        floors = [f for f in rep["findings"] if f["tier"] == "Floor"]
        # 只有「缺 Hallmark 印记」这条静态 Floor 会命中干净内容
        self.assertLessEqual(len(floors), 1)
        self.assertEqual(rep["engine"], "hallmark-deai")
        self.assertIn("waivers", rep)
        self.assertIn("source", rep)
        self.assertTrue(rep["source"]["url"].startswith("https://github.com/Nutlope/hallmark"))

    def test_sloppy_content_is_detected(self):
        rep = hd.audit(SLOPPY_HTML)
        gates = {f["gate"] for f in rep["findings"]}
        for gid in ("54", "30a", "63", "67", "6a", "34", "10a", "38i", "20"):
            self.assertIn(gid, gates, f"未检出 gate {gid}")
        self.assertLess(rep["score"], 60)
        self.assertEqual(rep["verdict"], "fail")

    def test_findings_carry_fix_and_samples(self):
        rep = hd.audit(SLOPPY_HTML)
        for f in rep["findings"]:
            self.assertIn("fix", f)
            self.assertIn("tell", f)
            self.assertIsInstance(f["samples"], list)
            self.assertIn(f["tier"], ("Floor", "Reflex", "Finish"))

    def test_push_surface_waives_inline_color_gate(self):
        rep = hd.audit("<style>.a{color:#fff}</style><p>正文</p>", surface="push")
        self.assertNotIn("7a", {f["gate"] for f in rep["findings"]})
        self.assertIn("7a", rep["waived_gates"])

    def test_report_has_no_secret_or_external_call(self):
        rep = hd.audit(CLEAN_HTML)
        blob = str(rep)
        # 报告是纯静态结构：不得回显任何凭据字段或大写密钥名。
        # 注：CSS 术语「token 化」属正常说明，不算密钥。
        for word in ("PUSHPLUS_TOKEN", "api_key=", "Authorization:", "Bearer ", "secret="):
            self.assertNotIn(word, blob)
        self.assertNotIn("https://www.pushplus.plus/send", blob)


class PolishTest(unittest.TestCase):
    def test_polish_improves_score_and_lists_fixes(self):
        rep = hd.polish(SLOPPY_HTML)
        self.assertTrue(rep["changed"])
        self.assertGreater(rep["score_after"], rep["score_before"])
        self.assertGreater(rep["score_delta"], 0)
        gates = {f["gate"] for f in rep["fixes"]}
        # SLOPPY_HTML 的 CSS 是内联 <style>，故 20/印记只作用于外部 CSS 文件（另有用例覆盖）。
        for gid in ("54", "30a", "63", "67", "6a", "34", "10a", "38i", "26a"):
            self.assertIn(gid, gates, f"未修复 gate {gid}")

    def test_score_reacts_to_how_much_got_fixed(self):
        """修得越多分越高：旧惩罚公式在命中 ≥5 处后停止增长，改了 25 处也是 0 分差。"""
        def fake(count):
            html = ("<style>.a{color:#fff}</style>"
                    + "".join(f'<span class="sub">A · B · C ·{i}</span>' for i in range(count)))
            return html
        small = hd.polish(fake(4))
        large = hd.polish(fake(30))
        self.assertGreater(small["score_after"], small["score_before"])
        self.assertGreater(large["score_after"], large["score_before"])
        # 「改得多」的样本在修完之后应当比「改得少」的样本分数更高（同样残留结构下）
        self.assertLess(large["score_before"], small["score_before"])

    def test_polish_exposes_before_after_diffs(self):
        rep = hd.polish(SLOPPY_HTML)
        self.assertTrue(rep["diffs"])
        first = rep["diffs"][0]
        self.assertIn("gate", first)
        self.assertTrue(first["before"])
        self.assertNotEqual(first["before"], first["after"])
        self.assertGreater(rep["fixed_count"], 0)

    def test_blocked_items_say_why_not_auto_fixed(self):
        # 卡片套卡片（gate 4）是 auto=False 的结构问题：改写不了，但必须说清为什么。
        nested = ('<style>.a{color:#111}</style><div class="card">'
                  '<div class="card"><p>正文</p></div></div>')
        rep = hd.polish(nested)
        self.assertTrue(rep["blocked"])
        gate4 = next(item for item in rep["blocked"] if item["gate"] == "4")
        self.assertIn("信息架构", gate4["reason"])
        self.assertNotIn("4", {f["gate"] for f in rep["fixes"]})

    def test_polish_is_fact_preserving(self):
        rep = hd.polish(SLOPPY_HTML)
        self.assertEqual(_facts(SLOPPY_HTML), _facts(rep["html"]))
        self.assertIn("1.2 万亿元", rep["html"])
        self.assertIn("https://example.com/news/42", rep["html"])
        self.assertNotIn("fact-lock", {f["gate"] for f in rep["fixes"]})

    def test_functional_plus_separator_is_never_deleted(self):
        html = ("<div>03140.HK 华夏港美人工智能 ETF ＋ 02807.HK Global X "
                "中国生物科技 ETF</div><span class='sub'>✦ 装饰</span>")
        rep = hd.polish(html)
        self.assertIn("＋", rep["html"], "全角加号是 ETF 分隔符，不能当装饰符号删除")
        self.assertNotIn("✦", rep["html"])

    def test_product_name_interpunct_is_preserved(self):
        html = "<h1>章鱼 AI·全景分析</h1><span class='sub'>✦ 装饰</span>"
        rep = hd.polish(html)
        self.assertIn("章鱼 AI·全景分析", rep["html"])

    def test_balanced_removal_does_not_orphan_tags(self):
        html = ("<div class='kicker'>顶层<span class='inner'>内层</span>"
                "<div class='nested'>嵌套</div>尾部</div><h2>标题</h2>")
        rep = hd.polish(html)
        self.assertIn("<h2>标题</h2>", rep["html"])
        self.assertNotIn("kicker", rep["html"])
        # 不应留下孤立的 </div>
        self.assertLessEqual(rep["html"].count("</div>"), rep["html"].count("<div"))

    def test_css_root_token_definition_not_made_self_referential(self):
        css = ":root{--paper:#ffffff;--ink:#111111;}body{background:#fff;color:var(--ink);}"
        rep = hd.polish("<p>正文</p>", css, surface="page")
        self.assertIn("--paper:#ffffff", rep["css"])
        self.assertNotIn("--paper:var(--paper)", rep["css"])  # 不能自引用
        self.assertIn("background:var(--paper)", rep["css"])

    def test_css_focus_visible_injected_when_missing(self):
        css = "button{background:#fff;}"
        rep = hd.polish("<button>运行</button>", css, surface="page")
        self.assertIn(":focus-visible", rep["css"])

    def test_clean_content_is_noop(self):
        rep = hd.polish(CLEAN_HTML)
        # 只有静态印记可能变化；不允许任何可见文本被改
        self.assertEqual(_facts(CLEAN_HTML), _facts(rep["html"]))
        self.assertTrue(all(f["gate"] == "20" for f in rep["fixes"]) or not rep["fixes"])

    def test_fact_lock_rolls_back_on_number_drift(self):
        """伪造一个会篡改数字的 fixer，签名不一致必须整体回滚。"""
        original = hd._remove_inert_labels
        try:
            hd._remove_inert_labels = lambda markup: (
                markup.replace("35 亿元", "99 亿元"), 1)
            rep = hd.polish(SLOPPY_HTML)
            self.assertEqual(rep["html"], SLOPPY_HTML, "事实漂移必须整体回滚")
            self.assertEqual([f["gate"] for f in rep["fixes"]], ["fact-lock"])
        finally:
            hd._remove_inert_labels = original

    def test_polish_page_surface(self):
        rep = hd.polish("<div class='eyebrow'>✦ DAILY BRIEF</div><h2>标题</h2>",
                        "body{letter-spacing:.2em;}", surface="page")
        self.assertEqual(rep["surface"], "page")
        self.assertGreater(rep["score_after"], rep["score_before"])


class SwitchTest(EnvMixin):
    def test_default_mode_is_polish_and_non_blocking(self):
        rep = hd.check_push("标题", SLOPPY_HTML)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["mode"], "polish")
        self.assertNotEqual(rep["content"], SLOPPY_HTML)
        self.assertIn("去 AI 味", rep["reason"])

    def test_audit_mode_reports_but_does_not_rewrite(self):
        rep = hd.check_push("标题", SLOPPY_HTML, mode="audit")
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["mode"], "audit")
        self.assertEqual(rep["content"], SLOPPY_HTML)
        self.assertIn("audit", rep["reason"])

    def test_off_mode_skips(self):
        rep = hd.check_push("标题", SLOPPY_HTML, mode="off")
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["skipped"])
        self.assertIsNone(rep["report"])
        self.assertEqual(rep["content"], SLOPPY_HTML)

    def test_unknown_mode_falls_back_to_polish(self):
        self.assertEqual(hd.get_mode("nonsense"), "polish")
        self.assertEqual(hd.get_mode(None), "polish")

    def test_env_mode(self):
        os.environ["DEAI_MODE"] = "audit"
        rep = hd.check_push("标题", SLOPPY_HTML)
        self.assertEqual(rep["mode"], "audit")
        self.assertEqual(rep["content"], SLOPPY_HTML)

    def test_skip_env(self):
        os.environ["SKIP_DEAI_CHECK"] = "1"
        rep = hd.check_push("标题", SLOPPY_HTML)
        self.assertTrue(rep["skipped"])
        self.assertEqual(rep["content"], SLOPPY_HTML)

    def test_force_block(self):
        os.environ["DEAI_FORCE"] = "block"
        rep = hd.check_push("标题", CLEAN_HTML)
        self.assertFalse(rep["ok"])
        self.assertIn("强制", rep["reason"])

    def test_force_pass_bypasses_strict(self):
        os.environ["DEAI_FORCE"] = "pass"
        os.environ["DEAI_STRICT"] = "1"
        rep = hd.check_push("标题", SLOPPY_HTML)
        self.assertTrue(rep["ok"], "DEAI_FORCE=pass 应优先于严格模式")

    def test_strict_blocks_on_residual_floor(self):
        os.environ["DEAI_STRICT"] = "1"
        rep = hd.check_push("标题", RESIDUAL_HTML)
        self.assertFalse(rep["ok"])
        self.assertIn("Floor", rep["reason"])
        # 非严格模式下同一份内容应放行（去 AI 味默认非阻塞）
        os.environ.pop("DEAI_STRICT", None)
        self.assertTrue(hd.check_push("标题", RESIDUAL_HTML)["ok"])

    def test_public_config_has_no_secrets(self):
        cfg = hd.public_config()
        self.assertEqual(cfg["mode"], "polish")
        self.assertIn("DEAI_MODE", cfg["env"])
        self.assertNotIn("token", str(cfg).lower())

    def test_check_push_report_is_slim(self):
        rep = hd.check_push("标题", SLOPPY_HTML)
        self.assertNotIn("html", rep["report"])
        self.assertNotIn("css", rep["report"])
        self.assertIn("before", rep["report"])
        self.assertIn("after", rep["report"])


class ReportsTest(unittest.TestCase):
    def test_reports_both_surfaces(self):
        rep = hd.reports(SLOPPY_HTML, SLOPPY_HTML, "body{letter-spacing:.2em;}")
        self.assertEqual(rep["status"], "ok")
        self.assertIn("push", rep["surfaces"])
        self.assertIn("page", rep["surfaces"])
        for key, s in rep["surfaces"].items():
            self.assertNotIn("html", s)
            self.assertIn("before", s)
            self.assertIn("after", s)
            self.assertGreaterEqual(s["score_after"], s["score_before"])
        self.assertIn("gates", rep)
        self.assertIn("waivers", rep)
        self.assertIn("prompt", rep)
        self.assertIn("source", rep)

    def test_reports_no_input(self):
        rep = hd.reports()
        self.assertEqual(rep["status"], "no_input")
        self.assertEqual(rep["surfaces"], {})

    def test_overall_score_is_mean(self):
        rep = hd.reports(SLOPPY_HTML, SLOPPY_HTML, "body{}")
        scores = [s["score_before"] for s in rep["surfaces"].values()]
        self.assertEqual(rep["overall_score_before"], round(sum(scores) / len(scores)))


class IntegrationTest(EnvMixin):
    """闸门必须排在敏感词终检与实际 POST 之前。"""

    @staticmethod
    def _push_patches(calls, posted, content=None):
        """构造 push_brief.main() 全部外部副作用的内存桩（不碰网络与磁盘）。"""
        import push_brief

        def fake_urlopen(request, timeout=None):
            payload = json.loads(request.data.decode("utf-8"))
            calls.append(("http", payload.get("content", "")))
            posted.append(payload)
            resp = mock.MagicMock()
            resp.read.return_value = b'{"code": 200, "msg": "ok", "data": "ok"}'
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        patches = [
            mock.patch.dict(os.environ, {"PUSHPLUS_TOKEN": "fake-token-123"}),
            mock.patch.object(push_brief.sources, "collect_all", return_value={}),
            mock.patch.object(push_brief.sources, "collect_market_for_push",
                              return_value=({}, {"ok": True, "reason": "ok"})),
            mock.patch.object(push_brief.sources, "analyze_ashare", return_value=None),
            mock.patch.object(push_brief.sources, "pushplus_quota", return_value=(98000, None)),
            mock.patch.object(push_brief.sensitive, "filter_brief",
                              side_effect=lambda b: (b, {"dropped_count": 0})),
            mock.patch.object(push_brief.sensitive, "check_push",
                              side_effect=lambda t, c: (calls.append(("sensitive", c)) or
                                                        {"ok": True, "reason": "", "hits": [],
                                                         "categories": [], "checked_at": "",
                                                         "lexicon_size": 0})),
            mock.patch.object(push_brief, "build_content",
                              return_value=SLOPPY_HTML if content is None else content),
            mock.patch.object(push_brief, "urlopen", side_effect=fake_urlopen),
        ]
        return patches

    def test_check_push_before_sensitive_and_http(self):
        import push_brief
        calls, posted = [], []
        with contextlib.ExitStack() as es:
            for p in self._push_patches(calls, posted):
                es.enter_context(p)
            code = push_brief.main()

        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls], ["sensitive", "http"])
        # 送给敏感词与 HTTP 的都是去 AI 味改写后的最终 HTML
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertNotIn("text-transform:uppercase", calls[0][1])
        self.assertNotIn("✦", calls[0][1])
        self.assertEqual(_facts(SLOPPY_HTML), _facts(calls[0][1]))

    def test_strict_block_skips_http(self):
        import push_brief
        os.environ["DEAI_STRICT"] = "1"
        calls, posted = [], []
        with contextlib.ExitStack() as es:
            for p in self._push_patches(calls, posted, content=RESIDUAL_HTML):
                es.enter_context(p)
            code = push_brief.main()

        self.assertEqual(code, 5)
        self.assertEqual(posted, [], "严格模式拦截时不得发出 HTTP 推送")

    def test_server_hallmark_endpoint(self):
        import server
        with mock.patch.object(server, "get_brief", return_value={}), \
             mock.patch.object(server, "get_market", return_value={"market": {}}), \
             mock.patch.object(server.sources, "build_html", return_value=SLOPPY_HTML), \
             mock.patch.object(server.sources, "analyze_ashare", return_value=None), \
             mock.patch.object(server, "_read_surface", side_effect=lambda n:
                               SLOPPY_HTML if n == "index.html" else "body{letter-spacing:.2em;}"):
            rep = server.hallmark_report("all")
        self.assertIn("push", rep["surfaces"])
        self.assertIn("page", rep["surfaces"])
        self.assertEqual(rep["surface"], "all")
        self.assertIn("config", rep)

    def test_server_surface_filter_and_mode_echo(self):
        import server
        server._HALLMARK_CACHE.update({"at": 0.0, "payload": None})
        with mock.patch.object(server, "_read_surface", return_value=SLOPPY_HTML):
            rep = server.hallmark_report("page", "audit")
        self.assertEqual(set(rep["surfaces"]), {"page"})
        self.assertEqual(rep["config"]["requested_mode"], "audit")

    def test_real_repo_files_are_mostly_clean(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "index.html"), encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(root, "styles.css"), encoding="utf-8") as f:
            css = f.read()
        rep = hd.audit(html, css, surface="page")
        self.assertGreaterEqual(rep["score"], 65)
        gates = {f["gate"] for f in rep["findings"]}
        for gid in ("54", "30a", "67", "6a", "34", "20", "10a", "7a"):
            self.assertNotIn(gid, gates, f"源码级仍有未处理的 gate {gid}")

    def test_real_push_snapshot_has_no_decoration(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "brief-output.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("✦", html)
        self.assertIn("＋", html)  # 功能性 ETF 分隔符保留
        self.assertIn("Hallmark ·", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
