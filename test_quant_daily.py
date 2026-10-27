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
        # 北向是「已停止披露」= 不适用，不是「今天缺数据」；不过也别编数
        self.assertIn("北向资金 不适用", md)
        self.assertIn("2024-08", md)
        self.assertNotIn("北向资金净流入", md)
        # 全景、五维两处口径必须一致：都是「不适用」，谁的 ⚠️ 都不该落在北向上
        self.assertEqual(md.count("北向资金：不适用"), 1)
        self.assertEqual(md.count("北向资金 不适用"), 1)
        self.assertNotIn("北向资金 数据暂缺", md)


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
            # 北向资金属于「不适用」（2024-08 起停止披露），其余指标必须是「未取得」
            statuses = {m["status"] for m in dim["metrics"]}
            self.assertTrue(statuses <= {"missing", "na"}, dim["key"])
            self.assertIn("missing", statuses, dim["key"])
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
        score, level, partial = qd._theme_strength(
            {"turnover_share": 8.4, "limit_up": 9, "days": 3})
        self.assertEqual(level, "强")
        self.assertFalse(partial)
        self.assertGreaterEqual(score, qd.SCORING["theme_strong"])
        self.assertEqual(qd._theme_strength({"turnover_share": 1, "limit_up": 1,
                                             "days": 1})[1], "弱")

    def test_theme_proxy_mode_uses_share_thresholds(self):
        # 只有成交额占比时（实盘行业板块口径），按 5% / 3% 的代理阈值判强弱
        score, level, partial = qd._theme_strength({"turnover_share": 6.2})
        self.assertTrue(partial)
        self.assertEqual(level, "强")
        self.assertAlmostEqual(score, 6.2, places=2)
        self.assertEqual(qd._theme_strength({"turnover_share": 3.6})[1], "中")
        self.assertEqual(qd._theme_strength({"turnover_share": 1.2})[1], "弱")

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



# ---------------------------------------------------------------- 实盘装配测试
def _candles(n=200, end="2026-10-26", close=4000.0, amount=5.0e11):
    """造 n 个交易日的合成日 K（含收盘价 / 涨跌幅 / 成交额），用于离线跑实盘装配。"""
    from datetime import datetime, timedelta
    day = datetime.strptime(end, "%Y-%m-%d")
    days = []
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day.strftime("%Y-%m-%d"))
        day -= timedelta(days=1)
    days.reverse()
    out, price = {}, close
    for index, date in enumerate(days):
        pct = round(0.6 if index % 2 else -0.4, 2)
        price = round(price * (1 + pct / 100), 2)
        out[date] = {"date": date, "close": price, "pct": pct, "amount": amount,
                     "change": round(price * pct / 100, 2), "source": "eastmoney"}
    return out


def _yield_candles(n=200, end="2026-10-26"):
    """造收益率序列：围绕 1.82% 小幅波动（日变动 1bp 级），用于久期代理的债券序列。"""
    from datetime import datetime, timedelta
    day = datetime.strptime(end, "%Y-%m-%d")
    days = []
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day.strftime("%Y-%m-%d"))
        day -= timedelta(days=1)
    days.reverse()
    out = {}
    for index, date in enumerate(days):
        value = round(1.82 + ((index % 7) - 3) * 0.005, 3)   # ±1.5bp 内摆动
        out[date] = {"date": date, "close": value, "pct": 0.0,
                     "amount": None, "change": 0.0, "source": "eastmoney"}
    return out


FULL_FEEDS = {
    "quotes": {"ok": True, "source": "eastmoney", "error": "", "breadth": {"up": 3300, "down": 1600, "flat": 100},
               "quotes": {
                   "沪深300": {"close": 4685.42, "pct": 0.62, "amount": 5120.0, "pe": 13.42, "pb": 1.48},
                   "中证500": {"close": 7420.18, "pct": 1.14, "amount": 3180.5, "pe": 28.6, "pb": 1.92},
                   "中证1000": {"close": 7855.63, "pct": 1.88, "amount": 2960.2, "pe": 38.75, "pb": 2.24},
                   "上证指数": {"close": 3956.57, "pct": 1.13, "amount": 9000.0},
                   "深证成指": {"close": 14048.88, "pct": 1.50, "amount": 12380.0},
                   "国证价值": {"close": 8200.0, "pct": -0.18},
                   "国证成长": {"close": 5600.0, "pct": 1.36},
               }},
    "industry": {"ok": True, "source": "eastmoney", "error": "",
                 "proxy": "东方财富行业板块（申万一级代理）",
                 "boards": [
                     {"name": "半导体", "pct": 3.42, "amount": 1860.0, "main_net": 96.4,
                      "up": 90, "down": 10, "turnover_share": 8.4},
                     {"name": "通信", "pct": 2.86, "amount": 1120.0, "main_net": 58.2,
                      "up": 70, "down": 12, "turnover_share": 5.2},
                     {"name": "公用事业", "pct": -2.14, "amount": 240.0, "main_net": -17.4,
                      "up": 5, "down": 60, "turnover_share": 1.1},
                 ]},
    "margin": {"ok": True, "source": "eastmoney", "error": "",
               "current": 19860.0, "prev": 19810.0, "date": "2026-10-26"},
    "billboard": {"ok": True, "source": "eastmoney", "error": "", "date": "2026-10-26",
                  "note": "营业部席位按席位名归类，不代表特定游资身份",
                  "inst": [{"name": "示例甲", "net": 4.82, "seat": "机构专用", "streak": None}],
                  "hot": [{"name": "示例乙", "net": 2.86, "seat": "某营业部", "streak": None}]},
    "limit_pools": {"ok": True, "source": "eastmoney", "error": "", "limit_up": 86, "limit_down": 4,
                    "ladder": "示例甲 4 连板", "errors": [],
                    "sectors_up": {"半导体": {"count": 9, "max_board": 3},
                                   "通信": {"count": 5, "max_board": 2}},
                    "sectors_down": {"公用事业": {"count": 2, "max_board": 1}}},
    "cross_asset": {"ok": True, "source": "eastmoney", "error": "",
                    "items": {"cgb10y": {"name": "中国10年期国债收益率", "value": 1.82, "prev": 1.85,
                                         "code": "100.CN10Y"},
                              "gold": {"name": "沪金主链", "value": 612.4, "prev": 610.0, "pct": 0.39,
                                       "code": "113.au0"},
                              "commodity": {"name": "南华商品指数", "value": 2310.0, "prev": 2290.0,
                                            "pct": 0.86, "code": "100.NHCI"},
                              "fx": {"name": "美元兑离岸人民币", "value": 7.1086, "prev": 7.117,
                                     "pct": -0.12, "code": "133.USDCNH"}},
                    "used": {}},
}


class LiveBundleTest(unittest.TestCase):
    """实盘装配：喂入仿真采集结果 + 仿真日 K，检查五维是否真的有数、缺口是否说清楚。"""

    def setUp(self):
        self._fetch = qd._fetch_klines

        def fake_klines(entries, lmt=45):
            out = {}
            for name, _secid in entries:
                n = min(lmt, 200)
                if name == "100.CN10Y":      # 国债：收益率序列，日变动约 1bp（不是价格）
                    out[name] = _yield_candles(n)
                else:
                    out[name] = _candles(n=n)
            return out
        qd._fetch_klines = fake_klines
        self.market = {"source": "snapshot", "date": "2026-10-26", "breadth": {}, "turnover": None}

    def tearDown(self):
        qd._fetch_klines = self._fetch

    def _bundle(self, feeds):
        return qd.live_bundle("2026-10-26", market=self.market, brief=None, feeds=feeds)

    def test_full_feeds_fill_all_five_dimensions(self):
        bundle = self._bundle(FULL_FEEDS)
        self.assertEqual(len(bundle["indices"]), 3)
        self.assertEqual(bundle["indices"][0]["pct"], 0.62)
        self.assertIsNotNone(bundle["indices"][0]["pct_5d"])          # 近 5 日从日 K 算
        self.assertEqual(bundle["valuation"]["沪深300"]["pe"], 13.42)
        self.assertIsNotNone(bundle["valuation"]["沪深300"]["price_pct"])  # 价格分位代理
        self.assertEqual(bundle["styles"]["大盘"], 0.62)
        self.assertEqual(bundle["styles"]["成长"], 1.36)
        self.assertEqual(bundle["breadth"]["up_count"], 3300)
        self.assertEqual(bundle["breadth"]["limit_up"], 86)          # 涨停池
        self.assertEqual(bundle["breadth"]["limit_down"], 4)
        self.assertEqual(bundle["breadth"]["ladder"], "示例甲 4 连板")
        # 成交额优先取日 K（当前值与环比同源），不走实时口径混算
        self.assertEqual(bundle["turnover"]["amount"], 10000.0)
        self.assertEqual(bundle["turnover"]["delta"], 0.0)
        self.assertEqual(bundle["funds"]["margin"], 19860.0)
        self.assertEqual(bundle["funds"]["lhb_inst"][0]["name"], "示例甲")
        self.assertAlmostEqual(bundle["funds"]["main_net"], 137.2, places=1)  # 板块主力合计
        self.assertAlmostEqual(bundle["macro"]["cgb10y"], 1.82, places=4)
        self.assertEqual(bundle["macro"]["gold"]["name"], "沪金主链")
        self.assertGreaterEqual(len(bundle["asset_history"]), 3)      # 风险平价有序列
        self.assertIn("股票", bundle["asset_history"])
        self.assertIn("债券", bundle["asset_history"])
        self.assertTrue(any(f.get("proxy") for f in bundle["factors"]))
        self.assertEqual(bundle["industry"][0]["limit_up"], 9)       # 行业涨停家数
        self.assertEqual(bundle["themes"][0]["limit_up"], 9)
        self.assertEqual(bundle["themes"][0]["days"], 3)

    def test_full_feeds_report_is_not_empty(self):
        bundle = self._bundle(FULL_FEEDS)
        report = qd.analyze(bundle, as_of="2026-10-26")
        self.assertGreaterEqual(report["quality"]["score"], 80)
        for dim in report["dimensions"]:
            filled = [m for m in dim["metrics"] if m["status"] != "missing"]
            self.assertTrue(filled, f"{dim['key']} 一个指标都没填上")
        md = report["markdown"]
        self.assertIn("### 采集明细", md)
        self.assertIn("✅ 实测", md)
        self.assertIn("◐ 代理", md)
        self.assertIn("⚠️ 未取得", md)                    # 未采集项照实列出
        self.assertIn("价格代理", md)                     # 估值分位缺口用价格分位并标注
        self.assertIn("上涨 3300 只", md)
        self.assertIn("涨停 86 家", md)
        self.assertIn("示例甲 4 连板", md)
        self.assertNotIn("仅按成交额占比判强弱", md)   # 有涨停分布 → 完整口径，不降级为代理
        self.assertIn("两融余额 19,860 亿", md)
        self.assertNotIn("数据暂缺 ⚠️ | 数据暂缺 ⚠️ | 数据暂缺 ⚠️ | 数据暂缺 ⚠️", md)


    def test_stale_snapshot_breadth_is_dropped(self):
        # 快照日期 != 目标交易日：涨停家数、连板这些当天数据一律不能混进来
        stale = {"source": "snapshot", "date": "2026-10-26", "turnover": None,
                 "breadth": {"limit_up": 77, "limit_down": 3, "ladder": "深中华A 6 连板"}}
        bundle = qd.live_bundle("2026-10-26", market=stale, feeds={})
        self.assertNotIn("limit_up", bundle.get("breadth") or {})
        self.assertTrue(any("不是当日实测接口数据" in n for n in bundle["notes"]))
        self.assertNotIn("深中华A", qd.analyze(bundle, as_of="2026-10-26")["markdown"])

    def test_snapshot_turnover_is_not_used(self):
        # 内置快照即使把 date 改写成目标交易日，也不能当成当日成交额
        market = {"source": "snapshot", "date": "2026-10-26",
                  "turnover": {"amount": 21380.0, "delta": 1840.0, "unit": "亿"}}
        bundle = qd.live_bundle("2026-10-26", market=market, feeds={})
        # 只允许日 K 口径的成交额进来，快照里那个 21,380 亿不能用
        self.assertNotEqual((bundle.get("turnover") or {}).get("amount"), 21380.0)

    def test_turnover_falls_back_to_quotes_without_prev(self):
        qd._fetch_klines = lambda entries, lmt=45: {}
        bundle = self._bundle(FULL_FEEDS)
        self.assertEqual(bundle["turnover"]["amount"], 21380.0)
        self.assertIsNone(bundle["turnover"]["prev"])    # 口径不同，不造环比
        self.assertIsNone(bundle["turnover"]["delta"])

    def test_bond_series_is_guarded_against_non_yield_data(self):
        # 「收益率」日变动 0.4pp 的序列显然是价格指数，不应用久期换算成 -260%/日
        candles = _candles(n=60)
        self.assertEqual(qd._bond_series(candles, "2026-10-26", 60, 6.5), [])
        flat = {d: {**row, "close": 1.8 + (i % 3) * 0.005} for i, (d, row) in
                enumerate(sorted(_candles(n=60).items()))}
        series = qd._bond_series(flat, "2026-10-26", 60, 6.5)
        self.assertTrue(series)
        self.assertTrue(all(abs(value) <= 1 for value in series))

    def test_provenance_marks_agent_only_fields(self):
        bundle = self._bundle(FULL_FEEDS)
        fields = {item["field"]: item for item in bundle["provenance"]}
        self.assertEqual(fields["两融余额"]["status"], "live")
        self.assertEqual(fields["行业板块"]["status"], "proxy")
        self.assertEqual(fields["指增超额"]["status"], "missing")
        self.assertIn("Agent 工具", fields["指增超额"]["source"])
        self.assertEqual(fields["财报 / 业绩预告"]["status"], "missing")

    def test_all_feeds_fail_stays_honest(self):
        dead = {key: {"ok": False, "source": "x", "error": "blocked"} for key in
                ("quotes", "industry", "margin", "billboard", "cross_asset")}
        qd._fetch_klines = lambda entries, lmt=45: {}
        bundle = self._bundle(dead)
        report = qd.analyze(bundle, as_of="2026-10-26")
        self.assertEqual(report["quality"]["score"], 0)
        self.assertNotIn("indices", bundle)
        self.assertNotIn("industry", bundle)
        self.assertTrue(all(item["status"] == "missing" for item in
                            bundle["provenance"] if item["field"] in
                            ("指数行情", "行业板块", "两融余额", "龙虎榜", "10Y 国债收益率")))
        self.assertIn("数据暂缺 ⚠️", report["markdown"])

    def test_live_bundle_without_feeds_does_not_crash(self):
        # 采集层整个抛异常时，live_bundle 仍要返回可分析的骨架
        import quant_feeds
        original = quant_feeds.collect
        quant_feeds.collect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("feed down"))
        try:
            bundle = qd.live_bundle("2026-10-26", market=self.market, brief=None)
            self.assertEqual(bundle["as_of"], "2026-10-26")
            self.assertIn("采集层异常", " ".join(bundle["notes"]))
            self.assertTrue(bundle["provenance"])
        finally:
            quant_feeds.collect = original


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
