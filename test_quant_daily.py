#!/usr/bin/env python3
"""quant_daily.py 回归测试：原子动作清单、交易日、五维口径、风险规则、归档与 CLI。

零第三方依赖，直接跑：

    python3 test_quant_daily.py

重点回归：
- 41 个原子动作覆盖五步流程，编号唯一、每个动作都有可调用工具与兜底说明；
- 缺数据时必须标「数据暂缺」，不许用前值 / 估计值顶替（诚实性是这个栏目的底线）；
- 风险阈值与仓位公式写死，同输入同输出；
- 归档文件名固定 YYYYMMDD_量化交易日报.md。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import quant_daily as qd

ROOT = os.path.dirname(os.path.abspath(__file__))


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.plan = qd.plan()

    def test_five_steps_and_action_count(self):
        steps = self.plan["steps"]
        self.assertEqual([s["no"] for s in steps], ["1", "2", "3", "4", "5"])
        self.assertEqual(self.plan["action_count"], 41)
        self.assertEqual(sum(len(s["actions"]) for s in steps), 41)

    def test_every_action_is_callable_atom(self):
        ids = set()
        for step in self.plan["steps"]:
            for action in step["actions"]:
                self.assertTrue(action["id"].startswith(f"S{step['no']}-"), action["id"])
                self.assertNotIn(action["id"], ids, "动作编号不能重复")
                ids.add(action["id"])
                for field in ("name", "tool", "input", "output", "fallback"):
                    self.assertTrue(action[field].strip(), f"{action['id']} 缺 {field}")
                self.assertIn(".", action["tool"], action["tool"])

    def test_five_data_classes_and_fallbacks(self):
        self.assertEqual(len(self.plan["data_classes"]), 5)
        for item in self.plan["data_classes"]:
            for field in ("label", "fields", "primary", "backup", "degrade"):
                self.assertTrue(item[field], field)
        self.assertEqual(len(self.plan["fallbacks"]), 4)
        self.assertEqual(self.plan["delivery"]["trigger"], "15:30")
        self.assertEqual(self.plan["delivery"]["deadline"], "16:00")

    def test_scoring_thresholds_are_written_down(self):
        scoring = self.plan["scoring"]
        self.assertEqual(scoring["position_base"], 50)
        self.assertEqual(scoring["position_floor"], 10)
        self.assertEqual(scoring["position_cap"], 95)
        self.assertAlmostEqual(sum(scoring["dimension_weights"].values()), 1.0, places=6)


class CalendarTest(unittest.TestCase):
    def test_weekend_is_not_trading_day(self):
        info = qd.trading_day("2026-10-25")
        self.assertFalse(info["is_trading_day"])
        self.assertIn("周日", info["reason"])

    def test_fixed_holiday_is_not_trading_day(self):
        self.assertFalse(qd.trading_day("2026-10-01")["is_trading_day"])
        self.assertFalse(qd.trading_day("2026-05-01")["is_trading_day"])

    def test_normal_weekday_is_trading_day(self):
        info = qd.trading_day("2026-10-26")
        self.assertTrue(info["is_trading_day"])

    def test_extra_holiday_can_be_supplied(self):
        self.assertTrue(qd.trading_day("2026-02-17")["is_trading_day"])
        self.assertFalse(qd.trading_day("2026-02-17",
                                        holidays={"2026-02-17"})["is_trading_day"])

    def test_next_session_skips_weekend(self):
        self.assertEqual(qd.next_session("2026-10-23"), "2026-10-26")


class DemoReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = qd.daily(as_of=qd.DEMO_AS_OF, demo=True)

    def test_five_dimensions_with_explicit_signals(self):
        dims = self.report["dimensions"]
        self.assertEqual(len(dims), 5)
        self.assertEqual([d["key"] for d in dims],
                         ["fundamental", "allocation", "alpha", "beta", "alt"])
        for dim in dims:
            self.assertIn(dim["signal"], ("看多", "中性", "看空"))
            self.assertTrue(dim["metrics"])
            self.assertTrue(dim["highlights"])
            self.assertTrue(dim["outputs"])

    def test_summary_within_100_chars(self):
        summary = self.report["summary"]
        self.assertLessEqual(len(summary), 100, summary)
        self.assertIn(qd.DEMO_AS_OF, summary)

    def test_markdown_follows_template(self):
        md = self.report["markdown"]
        for heading in ("# 2026-10-26 量化交易日报", "## 核心结论速览", "## 一、市场全景概览",
                        "## 二、五大量化维度分析", "## 三、明日策略信号",
                        "## 四、风险预警", "## 五、数据源与异常兜底"):
            self.assertIn(heading, md)
        self.assertIn("| 指数 | 当日涨跌幅 | 近5日涨跌幅 | 成交额（亿） | 估值分位（近10年） |", md)
        for dim in self.report["dimensions"]:
            self.assertIn(f"### {dim['no']}. {dim['label']}", md)

    def test_markdown_marks_signals_and_anomalies(self):
        md = self.report["markdown"]
        self.assertIn("✅", md)
        self.assertIn("⚠️", md)

    def test_position_is_deterministic_and_bounded(self):
        position = self.report["strategy"]["position"]
        self.assertGreaterEqual(position["value"], qd.SCORING["position_floor"])
        self.assertLessEqual(position["value"], qd.SCORING["position_cap"])
        again = qd.analyze(qd.demo_bundle(qd.DEMO_AS_OF), as_of=qd.DEMO_AS_OF)
        self.assertEqual(again["strategy"]["position"], position)

    def test_demo_bundle_is_deterministic(self):
        self.assertEqual(json.dumps(qd.demo_bundle("2026-10-26"), sort_keys=True),
                         json.dumps(qd.demo_bundle("2026-10-26"), sort_keys=True))

    def test_strategy_has_five_items(self):
        items = self.report["strategy"]["items"]
        self.assertEqual([i["key"] for i in items],
                         ["index_enhance", "market_neutral", "beta_rotation", "event"])
        self.assertEqual(len(items) + 1, 5)  # 四条分策略 + 总仓位

    def test_risks_cover_style_streak(self):
        titles = {r["title"]: r for r in self.report["risks"]}
        self.assertIn("风格极端化", titles)
        self.assertEqual(titles["风格极端化"]["level"], "高")

    def test_north_bound_is_not_faked(self):
        md = self.report["markdown"]
        self.assertIn("北向资金 数据暂缺", md)
        self.assertIn("2024-08", md)


class EmptyBundleTest(unittest.TestCase):
    """一类数据都没有时：照出报告，全表标「数据暂缺」，绝不猜数。"""

    @classmethod
    def setUpClass(cls):
        cls.report = qd.daily(as_of="2026-10-26", bundle={"as_of": "2026-10-26"})

    def test_status_and_quality(self):
        self.assertEqual(self.report["status"], "empty")
        self.assertEqual(self.report["quality"]["score"], 0)
        self.assertEqual(len(self.report["quality"]["classes"]), 5)

    def test_all_dimensions_neutral_and_marked_missing(self):
        for dim in self.report["dimensions"]:
            self.assertEqual(dim["signal"], "中性")
            self.assertTrue(all(m["status"] == "missing" for m in dim["metrics"]), dim["key"])
        self.assertEqual(self.report["strategy"]["position"]["value"], 50)

    def test_markdown_says_data_missing(self):
        md = self.report["markdown"]
        self.assertIn("数据暂缺 ⚠️", md)
        self.assertNotIn("nan", md.lower())

    def test_no_risk_rules_triggered_but_data_risk_reported(self):
        keys = [r["key"] for r in self.report["risks"]]
        self.assertIn("data", keys)


class RiskRuleTest(unittest.TestCase):
    def _report(self, **overrides):
        bundle = qd.demo_bundle("2026-10-26")
        bundle.update(overrides)
        return qd.analyze(bundle, as_of="2026-10-26")

    def test_index_move_triggers_high(self):
        report = self._report(indices=[
            {"name": "沪深300", "pct": -2.31}, {"name": "中证500", "pct": -1.2}])
        risk = next(r for r in report["risks"] if r["key"] == "move")
        self.assertEqual(risk["level"], "高")
        self.assertIn("沪深300 -2.31%", risk["detail"])

    def test_turnover_shock_is_flagged(self):
        report = self._report(turnover={"amount": 30000, "prev": 20000, "unit": "亿"})
        risk = next(r for r in report["risks"] if r["key"] == "move")
        self.assertIn("环比 +50%", risk["detail"])
        self.assertTrue(any("成交额环比" in note for note in report["quality"]["anomalies"]))

    def test_north_outflow_triggers(self):
        report = self._report(funds={"north": -68.4, "margin": 19800, "margin_prev": 19900})
        risk = next(r for r in report["risks"] if r["key"] == "liquidity")
        self.assertEqual(risk["level"], "高")
        self.assertIn("-68.4", risk["detail"])

    def test_margin_drop_triggers(self):
        report = self._report(funds={"north": None, "margin": 19000, "margin_prev": 20000})
        risk = next(r for r in report["risks"] if r["key"] == "liquidity")
        self.assertIn("两融余额环比", risk["detail"])

    def test_dispersion_percentile_triggers(self):
        report = self._report(style_streak={}, dispersion_pct=93.0)
        risk = next(r for r in report["risks"] if r["key"] == "style")
        self.assertIn("93% 分位", risk["detail"])

    def test_event_and_external_shock(self):
        report = self._report(
            events=[{"title": "监管收紧示例", "sector": "地产", "impact": "强", "direction": "利空"}],
            external={"标普500": -3.4})
        risk = next(r for r in report["risks"] if r["key"] == "event")
        self.assertIn("监管收紧示例", risk["detail"])
        self.assertIn("标普500", risk["detail"])

    def test_no_rule_hit_reports_clean(self):
        report = self._report(style_streak={}, dispersion_pct=20.0,
                              external={"标普500": 0.4},
                              turnover={"amount": 20000, "prev": 19500, "unit": "亿"})
        self.assertEqual([r["key"] for r in report["risks"]], ["none"])
        self.assertEqual(report["risks"][0]["level"], "低")
        self.assertTrue(any("北向资金无实时数据" in note
                            for note in report["quality"]["notes"]))

    def test_anomaly_review_flags_extreme_pct(self):
        report = self._report(indices=[{"name": "中证1000", "pct": 26.5}])
        self.assertTrue(any("偏离 ±20%" in note for note in report["quality"]["anomalies"]))


class DimensionTest(unittest.TestCase):
    def setUp(self):
        self.bundle = qd.demo_bundle("2026-10-26")

    def test_earnings_classification(self):
        split = qd._earnings_split(self.bundle)
        self.assertEqual(split["total"], 10)
        self.assertEqual(len(split["buckets"]["超预期"]), 5)
        self.assertEqual(len(split["buckets"]["不及预期"]), 3)
        self.assertEqual([e["yoy"] for e in split["surge"]],
                         sorted([e["yoy"] for e in split["surge"]], reverse=True))
        self.assertTrue(all(e["yoy"] > qd.SCORING["surge_yoy"] for e in split["surge"]))
        self.assertTrue(all(e["yoy"] < qd.SCORING["drop_yoy"] for e in split["drop"]))

    def test_valuation_zone(self):
        rows = {r["name"]: r for r in qd._valuation_rows(self.bundle)}
        self.assertEqual(rows["沪深300"]["zone"], "合理")
        self.assertEqual(rows["中证1000"]["zone"], "高估")

    def test_risk_parity_caps_weights_and_sums_to_100(self):
        rp = qd._risk_parity(self.bundle["asset_history"])
        self.assertIsNotNone(rp)
        for name, weight in rp["weights"].items():
            self.assertLessEqual(weight, qd.SCORING["risk_parity_weight_cap"] + 0.1, name)
        self.assertAlmostEqual(sum(rp["weights"].values()), 100.0, places=1)
        self.assertGreaterEqual(rp["weights"]["现金"], 0)

    def test_risk_parity_needs_history(self):
        self.assertIsNone(qd._risk_parity({}))

    def test_equity_bond_spread_uses_history(self):
        dim = qd._dim_allocation(self.bundle)
        self.assertIsNotNone(dim["tables"]["spread"])
        self.assertIsNotNone(dim["tables"]["spread_pct"])
        self.assertIn(dim["tables"]["bias"], ("股票占优", "债券占优", "股债均衡"))

    def test_basis_cost_judgement(self):
        dim = qd._dim_alpha(self.bundle)
        joined = " ".join(m["delta"] or "" for m in dim["metrics"])
        self.assertIn("对冲成本", joined)

    def test_theme_strength_levels(self):
        score, level = qd._theme_strength({"turnover_share": 8.4, "limit_up": 9, "days": 3})
        self.assertEqual(level, "强")
        self.assertGreaterEqual(score, qd.SCORING["theme_strong"])
        self.assertEqual(qd._theme_strength({"turnover_share": 1, "limit_up": 1,
                                             "days": 1})[1], "弱")

    def test_style_signal_uses_risk_appetite(self):
        dim = qd._dim_beta(self.bundle)
        # 小盘 -1.31pct 劣势（大盘 − 小盘 = −1.31）→ 风险偏好抬升，信号不应为看空
        self.assertNotEqual(dim["signal"], "看空")

    def test_missing_tables_stay_empty_not_guessed(self):
        dim = qd._dim_allocation({"as_of": "2026-10-26"})
        self.assertIsNone(dim["tables"]["risk_parity"])
        self.assertEqual(dim["tables"]["assets"][0]["ret"], None)


class ArchiveAndCliTest(unittest.TestCase):
    def test_archive_filename_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = qd.daily(as_of="2026-10-26", demo=True, archive_report=True, out_dir=tmp)
            name = f"20261026_量化交易日报.md"
            path = os.path.join(tmp, name)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(report["archive_path"], path)
            with open(path, encoding="utf-8") as handle:
                self.assertIn("# 2026-10-26 量化交易日报", handle.read())

    def test_non_trading_day_is_skipped(self):
        report = qd.daily(as_of="2026-10-25")
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["summary"], "今日非交易日，无日报")
        self.assertTrue(report["plan"]["steps"])  # 跳过日报，但执行方案仍然随响应返回

    def _run(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "quant_daily.py"), *args],
                              capture_output=True, text=True, cwd=ROOT)

    def test_cli_demo_summary(self):
        result = self._run("--demo", "--summary", "--no-archive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout.strip()), 100)

    def test_cli_plan_json(self):
        result = self._run("--print-plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["action_count"], 41)

    def test_cli_weekend_exit_code(self):
        result = self._run("--date", "2026-10-25", "--no-archive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("今日非交易日", result.stdout)

    def test_cli_bad_input(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("[1,2,3]")
            path = handle.name
        try:
            result = self._run("--input", path, "--no-archive", "--date", "2026-10-26")
            self.assertEqual(result.returncode, 4)
        finally:
            os.unlink(path)

    def test_cli_input_bundle_roundtrip(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(qd.demo_bundle("2026-10-26"), handle, ensure_ascii=False)
            path = handle.name
        try:
            result = self._run("--input", path, "--date", "2026-10-26",
                               "--no-archive", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["quality"]["score"], 100)
            self.assertEqual(len(payload["dimensions"]), 5)
        finally:
            os.unlink(path)


class HtmlSectionTest(unittest.TestCase):
    def test_render_contains_card_and_summary(self):
        report = qd.daily(as_of="2026-10-26", demo=True)
        html = qd.build_html_section(report)
        self.assertIn('<span class="tag">AI 量化日报</span>', html)
        self.assertIn("15:30 采集", html)
        self.assertIn("权益仓位中枢", html)
        self.assertIn("数据完整度", html)

    def test_render_skipped_report(self):
        report = qd.daily(as_of="2026-10-25")
        html = qd.build_html_section(report)
        self.assertIn("今日非交易日，无日报", html)

    def test_html_escapes_untrusted_names(self):
        bundle = qd.demo_bundle("2026-10-26")
        bundle["industry"] = [{"name": "<b>x</b>", "pct": 9.9, "turnover": 100.0,
                               "turnover_prev": 50.0, "main_net": 1.0}]
        report = qd.analyze(bundle, as_of="2026-10-26")
        html = qd.build_html_section(report)
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
