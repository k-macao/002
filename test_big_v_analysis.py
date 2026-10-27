#!/usr/bin/env python3
"""「大 V 分析」单元测试：框架边界、领域推演、诚实缺口与推送集成。"""
import os
import unittest
from unittest.mock import patch

import big_v_analysis
import sources


class FrameworkTest(unittest.TestCase):
    def test_six_step_framework_and_boundaries(self):
        fw = big_v_analysis.framework()
        self.assertEqual(fw["column"], "大 V 分析")
        self.assertEqual(fw["analyst"], "江学勤")
        self.assertIn("预测历史", fw["name"])
        self.assertEqual(len(fw["steps"]), 6)
        self.assertEqual(fw["steps"][0]["label"], "划定棋盘")
        self.assertEqual(fw["steps"][-1]["label"], "情景证伪")
        self.assertIn("不代表其本人观点", fw["disclaimer"])
        self.assertIn("不可证伪", fw["boundary"])
        self.assertTrue(any("PredictiveHistory" in ref["url"] for ref in fw["references"]))
        self.assertTrue(any("huxiu.com" in ref["url"] for ref in fw["references"]))


class AnalysisTest(unittest.TestCase):
    def _brief(self):
        return {
            "Bloomberg 政治": [
                {"title": "US midterm election puts Iran war costs and tariff powers in focus", "url": ""},
            ],
            "The Guardian": [
                {"title": "Iran threatens Gulf oil facilities, undersea cables and AI data centres", "url": ""},
                {"title": "Hormuz shipping disruption raises Brent and insurance risk", "url": ""},
            ],
            "Bloomberg 经济": [
                {"title": "Fed expected to hold rates as inflation stays sticky and Treasury yields rise", "url": ""},
            ],
        }

    def test_domains_are_grounded_in_current_headlines(self):
        data = big_v_analysis.analyze_big_v(self._brief(), as_of="2026-10-26")
        self.assertEqual(data["as_of"], "2026-10-26")
        self.assertEqual(data["status"], "有证据")
        self.assertEqual([d["label"] for d in data["domains"]], ["政治", "地缘", "经济"])
        self.assertGreater(data["counts"]["sources"], 0)
        self.assertIn("能源咽喉与基础设施", data["themes"])
        for domain in data["domains"]:
            self.assertEqual(domain["status"], "有证据")
            self.assertTrue(domain["chain"])
            self.assertTrue(domain["conclusion"])
            self.assertTrue(domain["evidence"])
        self.assertIn("边谈边压", data["headline"])

    def test_empty_brief_does_not_invent_current_events(self):
        data = big_v_analysis.analyze_big_v({}, as_of="2026-10-26")
        self.assertEqual(data["status"], "证据不足")
        self.assertEqual(data["counts"]["items"], 0)
        self.assertIn("不编造近期事件结论", data["headline"])
        for domain in data["domains"]:
            self.assertEqual(domain["status"], "证据不足")
            self.assertEqual(domain["evidence"], [])
            self.assertIn("不生成事件结论", domain["conclusion"])

    def test_scenarios_and_falsifiers_are_complete(self):
        data = big_v_analysis.analyze_big_v(self._brief(), as_of="2026-10-26")
        self.assertEqual([s["key"] for s in data["scenarios"]], ["base", "escalation", "detente"])
        self.assertEqual(sum(s["weight"] for s in data["scenarios"]), 100)
        self.assertIn("不是统计概率", data["scenario_weights_note"])
        self.assertGreaterEqual(len(data["falsifiers"]), 3)
        self.assertTrue(all(5 <= data["risk_score"] <= 95 for _ in [0]))

    def test_cross_source_duplicate_mentions_are_not_double_counted_as_items(self):
        title = "Iran Hormuz talks ease oil risk"
        brief = {
            "Reuters 路透": [{"title": title, "url": ""}],
            "Bloomberg 政治": [{"title": title, "url": ""}],
        }
        data = big_v_analysis.analyze_big_v(brief, as_of="2026-10-26")
        self.assertEqual(data["counts"]["items"], 1)
        self.assertEqual(data["counts"]["mentions"], 2)
        self.assertEqual(data["counts"]["sources"], 2)

    def test_ascii_keyword_uses_word_boundary(self):
        brief = {"社区": [{"title": "Said the annual report was available", "url": ""}]}
        data = big_v_analysis.analyze_big_v(brief, as_of="2026-10-26")
        self.assertEqual(data["counts"]["items"], 0)


class BuildHtmlIntegrationTest(unittest.TestCase):
    @staticmethod
    def _kanpan():
        return dict(
            review=sources.analyze_ashare(market=sources._ASHARE_SNAPSHOT),
            hk_review=sources.analyze_hk(market=sources._HK_SNAPSHOT),
            us_review=sources.analyze_us(market=sources._US_SNAPSHOT),
        )

    def _brief(self):
        return {
            "Bloomberg 政治": [{"title": "Midterm election and Iran strikes raise political risk", "url": ""}],
            "The Guardian": [{"title": "Iran threatens Hormuz oil and AI data centre facilities", "url": ""}],
            "Bloomberg 经济": [{"title": "Fed holds rates as sticky inflation lifts Treasury yields", "url": ""}],
        }

    def test_analyze_brief_exposes_big_v(self):
        data = sources.analyze_brief(self._brief())
        self.assertIn("big_v", data)
        self.assertEqual(data["big_v"]["column"], "大 V 分析")

    def test_build_html_renders_big_v_between_sectors_and_news(self):
        out = sources.build_html(self._brief(), **self._kanpan())
        for token in ("大 V 分析", "六步逻辑推导", "政治 × 地缘 × 经济", "升级博弈树", "证伪信号"):
            self.assertIn(token, out)

        def at(label):
            return out.index(f'<span class="tag">{label}</span>')

        self.assertLess(at("AI 板块机会"), at("大 V 分析"))
        self.assertLess(at("大 V 分析"), at("全网快讯"))

    def test_build_html_evidence_is_escaped(self):
        brief = {
            "The Guardian": [
                {"title": "Iran Hormuz oil threat <script>alert(1)</script> raises rates", "url": ""},
            ]
        }
        out = sources.build_html(brief, **self._kanpan())
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>alert", out)

    def test_big_v_card_survives_free_tier_capacity(self):
        brief = {name: sources._demo_items(name)[:4] for name in sources.SOURCES}
        with patch.dict(os.environ, {"PUSHPLUS_MEMBER": "0"}, clear=False):
            out = sources.build_html(brief, max_length=19500, **self._kanpan())
        self.assertLessEqual(len(out), 19500)
        self.assertIn("大 V 分析", out)
        self.assertIn("AI 板块机会", out)


if __name__ == "__main__":
    unittest.main()
