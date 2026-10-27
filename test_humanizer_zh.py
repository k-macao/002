#!/usr/bin/env python3
"""文本内容去 AI 味闸门（humanizer_zh.py）单测：零依赖、可离线运行。

覆盖：模式检测、零信息套话删除、逐字新闻标题保护、fact-lock 回滚、
开关/强制/严格模式、两个面 reports、源码级洁净度，以及推送链路先后关系。
"""
import contextlib
import json
import os
import re
import unittest
from unittest import mock

import humanizer_zh as hz

CLEAN_HTML = (
    "<div class='txt'>市场风险偏好回升，资金聚焦美联储与地缘两条线索。</div>"
    "<td class='td-t'>2026-10-27 A 股成交额 1.2 万亿元</td>"
    "<a href='https://example.com/news/42'>来源链接</a>"
)

SLOPPY_HTML = (
    "<div class='txt'>值得注意的是，市场风险偏好回升。未来可期，让我们拭目以待！"
    "希望这对您有帮助。你觉得呢？欢迎在评论区留言讨论。机遇与挑战并存。</div>"
    "<td class='td-t'>Citadel证券：美股7月暴跌不是牛市终结，而是技术性重置</td>"
    "<p>2026-10-27 成交额 1.2 万亿元，北向净流入 35 亿元。</p>"
    "<a href='https://example.com/news/42'>来源链接</a>"
)

# 故意含 AI 味的文案，用于验证自动清理（不含任何数字 / URL / 引号事实）。
BOILERPLATE = (
    "众所周知，市场波动加大。未来可期！让我们一起深入探讨这个问题。"
    "好问题！希望这对您有帮助。你觉得呢？点赞关注。见仁见智。"
)


class EnvMixin(unittest.TestCase):
    _ENV_KEYS = ("HUMANIZER_MODE", "SKIP_HUMANIZER_CHECK", "HUMANIZER_FORCE", "HUMANIZER_STRICT")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self._ENV_KEYS}

    def tearDown(self):
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)
            if self._saved[k] is not None:
                os.environ[k] = self._saved[k]


class AuditTest(unittest.TestCase):
    def test_clean_text_scores_high(self):
        rep = hz.audit(CLEAN_HTML)
        self.assertGreaterEqual(rep["score"], 85)
        self.assertEqual(rep["verdict"], "clean")
        self.assertEqual(rep["engine"], "humanizer-zh-deai")
        self.assertEqual(len(rep["source"]["projects"]), 3)
        self.assertTrue(all(p["license"] == "MIT" for p in rep["source"]["projects"]))

    def test_non_commercial_project_is_explicitly_excluded(self):
        rep = hz.audit(CLEAN_HTML)
        excluded = rep["source"]["excluded"]
        self.assertTrue(any("swaylq" in e["name"] for e in excluded))
        self.assertIn("Non-Commercial", excluded[0]["reason"])

    def test_sloppy_text_detects_strong_patterns(self):
        rep = hz.audit(SLOPPY_HTML)
        gates = {f["gate"] for f in rep["findings"]}
        for gid in ("C12", "E22", "E35", "F31"):
            self.assertIn(gid, gates, f"未检出模式 {gid}")
        self.assertLess(rep["score"], 80)
        self.assertEqual(rep["verdict"], "needs_review")

    def test_weak_signal_needs_clustering(self):
        # 单个讲义腔路标词是弱信号，不计入；三个以上同现才报告（对齐 CCL 2023）。
        single = "<div class='txt'>值得注意的是，市场反弹。</div>"
        cluster = ("<div class='txt'>值得注意的是，市场反弹；众所周知，风险仍在；"
                   "毋庸置疑，需谨慎；不可否认，波动加大。</div>")
        self.assertNotIn("B10", {f["gate"] for f in hz.audit(single)["findings"]})
        self.assertIn("B10", {f["gate"] for f in hz.audit(cluster)["findings"]})

    def test_findings_carry_fix_and_samples(self):
        for f in hz.audit(SLOPPY_HTML)["findings"]:
            self.assertIn("fix", f)
            self.assertIn("tell", f)
            self.assertIn("group", f)
            self.assertIsInstance(f["samples"], list)
            self.assertIn(f["tier"], ("strong", "medium", "weak"))

    def test_style_and_attributes_are_not_scanned(self):
        html = "<style>.x{content:'未来可期'}</style><div title='众所周知'>正文</div>"
        self.assertEqual(hz.audit(html)["findings"], [])

    def test_report_has_no_secrets(self):
        blob = str(hz.audit(CLEAN_HTML))
        for word in ("PUSHPLUS_TOKEN", "api_key=", "Authorization:", "Bearer "):
            self.assertNotIn(word, blob)


class PolishTest(unittest.TestCase):
    def test_polish_improves_score_and_lists_fixes(self):
        rep = hz.polish(SLOPPY_HTML)
        self.assertTrue(rep["changed"])
        self.assertGreater(rep["score_after"], rep["score_before"])
        gates = {f["gate"] for f in rep["fixes"]}
        for gid in ("C12", "E22", "E35", "F31"):
            self.assertIn(gid, gates, f"未清理模式 {gid}")

    def test_boilerplate_all_removed(self):
        html = f"<div class='txt'>{BOILERPLATE}</div>"
        rep = hz.polish(html)
        for word in ("未来可期", "希望这对您有帮助", "你觉得呢", "点赞关注", "见仁见智"):
            self.assertNotIn(word, rep["html"], f"未清理 {word}")

    def test_facts_are_preserved(self):
        rep = hz.polish(SLOPPY_HTML)
        self.assertEqual(hz.fact_signature(SLOPPY_HTML), hz.fact_signature(rep["html"]))
        self.assertIn("1.2 万亿元", rep["html"])
        self.assertIn("35 亿元", rep["html"])
        self.assertIn("https://example.com/news/42", rep["html"])
        self.assertNotIn("fact-lock", {f["gate"] for f in rep["fixes"]})

    def test_verbatim_news_headline_is_protected(self):
        rep = hz.polish(SLOPPY_HTML)
        self.assertIn("美股7月暴跌不是牛市终结，而是技术性重置", rep["html"])

    def test_link_text_and_quotes_are_protected(self):
        html = ("<a href='https://x.com/1'>未来可期 ·  Orig</a>"
                "<div class='txt'>原文「未来可期」是引用，不应被删。</div>"
                "<div class='txt'>未来可期。</div>")
        rep = hz.polish(html)
        self.assertIn(">未来可期 ·  Orig</a>", rep["html"])

    def test_legitimate_separators_are_not_stripped(self):
        html = ("<div class='txt'>利好：<span class='hl'>货币政策</span>、"
                "<span class='hl'>汇率</span>；承压：<span class='hl-d'>地产</span></div>")
        rep = hz.polish(html)
        self.assertIn("、", rep["html"])
        self.assertIn("；", rep["html"])
        self.assertNotIn("fact-lock", {f["gate"] for f in rep["fixes"]})

    def test_clean_content_is_noop(self):
        rep = hz.polish(CLEAN_HTML)
        self.assertFalse(rep["changed"])
        self.assertEqual(rep["fixes"], [])
        self.assertEqual(rep["html"], CLEAN_HTML)

    def test_fact_lock_rolls_back_on_number_drift(self):
        original = hz._rewrite_nodes
        try:
            hz._rewrite_nodes = lambda html: (html.replace("35 亿元", "99 亿元"), {"C12": 1})
            rep = hz.polish(SLOPPY_HTML)
            self.assertEqual(rep["html"], SLOPPY_HTML, "事实漂移必须整体回滚")
            self.assertEqual([f["gate"] for f in rep["fixes"]], ["fact-lock"])
        finally:
            hz._rewrite_nodes = original

    def test_fact_lock_rolls_back_on_quote_drift(self):
        original = hz._rewrite_nodes
        html = SLOPPY_HTML + "<div class='txt'>券商称「技术性重置」并非反转。</div>"
        try:
            hz._rewrite_nodes = lambda _html: (_html.replace("技术性重置", "另一说法"), {"C12": 1})
            rep = hz.polish(html)
            self.assertEqual(rep["html"], html, "引号内文字漂移必须整体回滚")
            self.assertEqual([f["gate"] for f in rep["fixes"]], ["fact-lock"])
        finally:
            hz._rewrite_nodes = original

    def test_auto_rules_never_introduce_new_words(self):
        for gate, pattern, repl in hz.SAFE_RULES:
            self.assertEqual(repl, "", f"规则 {gate} 引入了新字符")

    def test_visible_text_excludes_tags_and_style(self):
        html = "<style>.a{color:red}</style><div>正文 <b>加粗</b> 结尾</div>"
        self.assertNotIn("color", hz.visible_text(html))
        self.assertIn("正文 加粗 结尾", hz.visible_text(html))


class SwitchTest(EnvMixin):
    def test_default_mode_is_polish_and_non_blocking(self):
        rep = hz.check_push("标题", SLOPPY_HTML)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["mode"], "polish")
        self.assertNotEqual(rep["content"], SLOPPY_HTML)
        self.assertIn("文本去 AI 味", rep["reason"])

    def test_audit_mode_reports_but_does_not_rewrite(self):
        rep = hz.check_push("标题", SLOPPY_HTML, mode="audit")
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["content"], SLOPPY_HTML)
        self.assertIn("audit", rep["reason"])

    def test_off_mode_skips(self):
        rep = hz.check_push("标题", SLOPPY_HTML, mode="off")
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["skipped"])
        self.assertIsNone(rep["report"])

    def test_unknown_mode_falls_back_to_polish(self):
        self.assertEqual(hz.get_mode("nonsense"), "polish")

    def test_env_mode(self):
        os.environ["HUMANIZER_MODE"] = "audit"
        self.assertEqual(hz.check_push("标题", SLOPPY_HTML)["content"], SLOPPY_HTML)

    def test_skip_env(self):
        os.environ["SKIP_HUMANIZER_CHECK"] = "1"
        rep = hz.check_push("标题", SLOPPY_HTML)
        self.assertTrue(rep["skipped"])
        self.assertEqual(rep["content"], SLOPPY_HTML)

    def test_force_block(self):
        os.environ["HUMANIZER_FORCE"] = "block"
        self.assertFalse(hz.check_push("标题", CLEAN_HTML)["ok"])

    def test_force_pass_bypasses_strict(self):
        os.environ["HUMANIZER_FORCE"] = "pass"
        os.environ["HUMANIZER_STRICT"] = "1"
        self.assertTrue(hz.check_push("标题", SLOPPY_HTML)["ok"])

    def test_strict_blocks_on_residual_strong_signal(self):
        os.environ["HUMANIZER_STRICT"] = "1"
        # 逐字标题里的「不是 X 而是 Y」是受保护引用，仍会被检测为强信号。
        rep = hz.check_push("标题", SLOPPY_HTML)
        self.assertFalse(rep["ok"])
        self.assertIn("HUMANIZER_STRICT", rep["reason"])

    def test_public_config_has_no_secrets(self):
        cfg = hz.public_config()
        self.assertEqual(cfg["mode"], "polish")
        self.assertIn("HUMANIZER_MODE", cfg["env"])

    def test_check_push_report_is_slim(self):
        rep = hz.check_push("标题", SLOPPY_HTML)
        self.assertNotIn("html", rep["report"])
        self.assertIn("before", rep["report"])


class ReportsTest(unittest.TestCase):
    def test_reports_both_surfaces(self):
        rep = hz.reports(SLOPPY_HTML, SLOPPY_HTML)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(set(rep["surfaces"]), {"push", "page"})
        self.assertIn("patterns", rep)
        self.assertIn("prompt", rep)
        self.assertIn("config", rep)

    def test_reports_no_input(self):
        rep = hz.reports()
        self.assertEqual(rep["status"], "no_input")

    def test_overall_score_is_mean(self):
        rep = hz.reports(SLOPPY_HTML, CLEAN_HTML)
        scores = [s["score_before"] for s in rep["surfaces"].values()]
        self.assertEqual(rep["overall_score_before"], round(sum(scores) / len(scores)))


class IntegrationTest(EnvMixin):
    """文本层闸门必须排在敏感词终检与实际 POST 之前，且在设计层之后。"""

    @staticmethod
    def _push_patches(calls, posted, content=None, strict=False):
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

        return [
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
            mock.patch.object(push_brief, "urlopen",
                              side_effect=(lambda *a, **k: posted.append(1)) if strict else fake_urlopen),
        ]

    def test_text_gate_runs_before_sensitive_and_http(self):
        import push_brief
        calls, posted = [], []
        with contextlib.ExitStack() as es:
            for p in self._push_patches(calls, posted):
                es.enter_context(p)
            code = push_brief.main()

        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls], ["sensitive", "http"])
        self.assertEqual(calls[0][1], calls[1][1])
        # 文本层确实清理了套话；逐字新闻标题仍在。
        self.assertNotIn("未来可期", calls[0][1])
        self.assertNotIn("点赞关注", calls[0][1])
        self.assertIn("美股7月暴跌不是牛市终结，而是技术性重置", calls[0][1])
        self.assertEqual(hz.fact_signature(SLOPPY_HTML), hz.fact_signature(calls[0][1]))

    def test_strict_block_skips_http(self):
        import push_brief
        os.environ["HUMANIZER_STRICT"] = "1"
        calls, posted = [], []
        with contextlib.ExitStack() as es:
            for p in self._push_patches(calls, posted, strict=True):
                es.enter_context(p)
            code = push_brief.main()

        self.assertEqual(code, 6)
        self.assertEqual(posted, [], "文本层严格模式拦截时不得发出 HTTP 推送")

    def test_server_humanizer_endpoint(self):
        import server
        with mock.patch.object(server, "get_brief", return_value={}), \
             mock.patch.object(server, "get_market", return_value={"market": {}}), \
             mock.patch.object(server.sources, "build_html", return_value=SLOPPY_HTML), \
             mock.patch.object(server.sources, "analyze_ashare", return_value=None), \
             mock.patch.object(server, "_read_surface", return_value=SLOPPY_HTML):
            rep = server.humanizer_report("all")
        self.assertEqual(set(rep["surfaces"]), {"push", "page"})
        self.assertIn("config", rep)

    def test_server_surface_filter_and_mode_echo(self):
        import server
        server._HUMANIZER_CACHE.update({"at": 0.0, "payload": None})
        with mock.patch.object(server, "_read_surface", return_value=SLOPPY_HTML):
            rep = server.humanizer_report("page", "audit")
        self.assertEqual(set(rep["surfaces"]), {"page"})
        self.assertEqual(rep["config"]["requested_mode"], "audit")


class SourceCleanlinessTest(unittest.TestCase):
    """源码级去 AI 味：真实文件不应残留零信息纯套话，也不应再出现特定人名。"""

    def test_no_personal_name_in_repo_sources(self):
        root = os.path.dirname(os.path.abspath(__file__))
        for name in ("big_v_analysis.py", "sources.py", "index.html", "app.js",
                     "README.md", "brief-output.html", "docs/big_v_analysis.md"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                self.assertNotIn("江学勤", f.read(), f"{name} 仍残留该人名")

    def test_api_analyst_field_is_neutral(self):
        import big_v_analysis
        self.assertEqual(big_v_analysis.framework()["analyst"], "公开框架")

    def test_real_push_snapshot_has_no_boilerplate(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "brief-output.html"), encoding="utf-8") as f:
            html = f.read()
        rep = hz.polish(html)
        self.assertFalse(any(f["gate"] == "fact-lock" for f in rep["fixes"]))
        for word in ("未来可期", "拭目以待", "希望这对您有帮助", "点赞关注"):
            self.assertNotIn(word, html)

    def test_generated_headline_no_false_contrast(self):
        import big_v_analysis
        data = big_v_analysis.analyze_big_v(
            {"Bloomberg 政治": [{"title": "US midterm election and Iran war costs", "url": ""}]},
            as_of="2026-10-26")
        self.assertNotIn("不是三条平行新闻", data["headline"])
        self.assertIn("边谈边压", data["headline"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
