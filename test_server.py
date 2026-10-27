#!/usr/bin/env python3
"""server.py 冒烟测试：在随机端口启动真实服务，验证各 API 的状态码与响应体。

在虚拟环境中运行（零第三方依赖）：

    python3 -m venv .venv
    .venv/bin/python test_server.py

回归重点：
- POST /api/run 及所有 API 路径都不得返回 405；
- 手动推送可通过 PUSHPLUS_API_URL 指向本地假服务（mock_pushplus.py），
  推送后检查记录文件（假文件）内容，验证推送请求真实发出且载荷正确。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port, extra_env=None):
    """启动 server.py，返回 subprocess.Popen。"""
    env = dict(os.environ)
    env.pop("PUSHPLUS_TOKEN", None)
    env.pop("PUSHPLUS_API_URL", None)
    env.pop("PUSHPLUS_TOPIC", None)
    env.pop("SKIP_SENSITIVE_CHECK", None)
    env.pop("SENSITIVE_FORCE", None)
    env.pop("SENSITIVE_LEXICON", None)
    env["PORT"] = str(port)
    # 测试不联网：单源超时压到 1 秒，「AI 独立开发雷达」的 19 个源并行失败即可返回。
    env.setdefault("RADAR_TIMEOUT", "1")
    env.setdefault("RADAR_BUDGET", "3")
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py")],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/", timeout=1)
            return proc
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"server.py 未能启动（端口 {port}）")


def start_mock(port, record_file, fail=False):
    """启动 mock_pushplus.py，返回 subprocess.Popen。"""
    env = dict(os.environ)
    env["PORT"] = str(port)
    # 测试不联网：单源超时压到 1 秒，「AI 独立开发雷达」的 19 个源并行失败即可返回。
    env.setdefault("RADAR_TIMEOUT", "1")
    env.setdefault("RADAR_BUDGET", "3")
    env["RECORD_FILE"] = str(record_file)
    if fail:
        env["MOCK_FAIL"] = "1"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "mock_pushplus.py")],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return proc
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"mock_pushplus.py 未能启动（端口 {port}）")


def request(base, method, path, body=None, headers=None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers or {},
    )
    try:
        # 推送会连带渲染「AI 独立开发雷达」等栏目，冷启动第一次较慢，给 20 秒上限。
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8")


class ServerSmokeTest(unittest.TestCase):
    """无 token 场景：基础 API 行为，任何路径不得返回 405。"""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = start_server(cls.port)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)

    def test_index_served(self):
        status, _ = request(self.base, "GET", "/")
        self.assertEqual(status, 200)

    def test_sources_returns_list(self):
        status, raw = request(self.base, "GET", "/api/sources")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)

    def test_sections_returns_catalog(self):
        # 六大板块目录：key / label / sources，源清单合计等于 /api/sources。
        status, raw = request(self.base, "GET", "/api/sections")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual([sec["key"] for sec in data],
                         ["finance", "trending", "community", "policy", "world", "civic"])
        _status, raw_sources = request(self.base, "GET", "/api/sources")
        all_sources = json.loads(raw_sources)
        self.assertEqual([n for sec in data for n in sec["sources"]], all_sources)
        for sec in data:
            self.assertTrue(sec["label"])
            self.assertEqual(sec["count"], len(sec["sources"]))

    def test_brief_section_filter(self):
        # ?section=civic 只返回该板块的源；未知板块 404 并列出可用板块。
        status, raw = request(self.base, "GET", "/api/brief?section=civic")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(list(data), ["g0v 立法院議案", "EquityStack 政策承诺", "EquityStack 法案追踪", "GovTrack 国会立法"])
        self.assertTrue(all(len(items) >= 1 for items in data.values()))
        status, raw = request(self.base, "GET", "/api/brief?section=nope")
        self.assertEqual(status, 404)
        self.assertIn("civic", json.loads(raw)["sections"])

    def test_quant_endpoint_demo_and_markdown(self):
        # GET /api/quant?demo=1：五维 + 策略 + 风险 + 41 个原子动作，Markdown 走 ?format=markdown。
        status, raw = request(self.base, "GET", "/api/quant?demo=1")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        for key in ("column", "as_of", "summary", "quality", "dimensions",
                    "strategy", "risks", "markdown", "plan"):
            self.assertIn(key, data)
        self.assertEqual(data["column"], "AI 量化日报")
        self.assertEqual(len(data["dimensions"]), 5)
        self.assertEqual(data["plan"]["action_count"], 41)
        self.assertLessEqual(len(data["summary"]), 100)

        status_md, md = request(self.base, "GET", "/api/quant?demo=1&format=markdown")
        self.assertEqual(status_md, 200)
        self.assertIn("# 2026-10-26 量化交易日报", md)
        self.assertIn("## 核心结论速览", md)

    def test_quant_endpoint_skips_non_trading_day(self):
        status, raw = request(self.base, "GET", "/api/quant?date=2026-10-25")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["status"], "skipped")
        self.assertEqual(data["summary"], "今日非交易日，无日报")

    def test_policy_endpoint_returns_deep_analysis(self):
        # GET /api/policy：「AI 政策分析」深度层（知识库检索 / 推理链 / 模型）随接口一并返回。
        status, raw = request(self.base, "GET", "/api/policy")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        for key in ("buckets", "stance", "headline", "market_impact", "kb", "modifiers", "sentiment",
                    "graph", "reasoning", "mindmap", "research", "models"):
            self.assertIn(key, data)
        self.assertIn("releases", data["market_impact"])
        self.assertIn("markets", data["market_impact"])
        self.assertEqual([step["label"] for step in data["reasoning"]["steps"]],
                         ["宏观背景", "行业限制", "资金流向", "受益板块"])
        self.assertIn("lstm", data["models"])
        self.assertIn("prophet", data["models"])
        # ?deep=0 只要基础统计（不跑知识库检索与模型），响应更快。
        status, raw = request(self.base, "GET", "/api/policy?deep=0")
        shallow = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertIn("headline", shallow)
        self.assertNotIn("kb", shallow)
        # 近30日政策→股市影响不跑模型，浅层接口也要带上。
        self.assertIn("market_impact", shallow)
        self.assertIn("headline", shallow["market_impact"])

    def test_big_v_endpoint_returns_framework_analysis(self):
        # GET /api/big-v：六步框架 + 三个领域 + 情景树，且两个路径别名返回同结构。
        status, raw = request(self.base, "GET", "/api/big-v")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        for key in ("column", "analyst", "headline", "framework_steps", "domains",
                    "scenarios", "falsifiers", "references", "disclaimer", "counts"):
            self.assertIn(key, data)
        self.assertEqual(data["column"], "大 V 分析")
        self.assertEqual(len(data["framework_steps"]), 6)
        self.assertEqual([d["label"] for d in data["domains"]], ["政治", "地缘", "经济"])
        self.assertEqual(sum(s["weight"] for s in data["scenarios"]), 100)
        status_alias, raw_alias = request(self.base, "GET", "/api/bigv")
        self.assertEqual(status_alias, 200)
        self.assertEqual(json.loads(raw_alias)["column"], "大 V 分析")

    def test_radar_endpoint_returns_modules_and_prompt(self):
        # GET /api/radar：「AI 独立开发雷达」7 模块 + 心法；?format=prompt 给当日 AI 提示词。
        # 测试环境把单源超时压到 1 秒：19 个源并行失败也必须正常返回结构，不能 500。
        status, raw = request(self.base, "GET", "/api/radar")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertNotIn("error", data)
        self.assertEqual(data["column"], "AI 独立开发雷达")
        self.assertEqual([module["key"] for module in data["modules"]] if isinstance(data["modules"], list)
                         else sorted(data["modules"]),
                         sorted(["opportunities", "money_cases", "ideas", "growth",
                                 "tools", "pitfalls", "data_signals"]))
        self.assertIn("headline", data)
        self.assertIn("disclaimer", data)
        status_prompt, prompt = request(self.base, "GET", "/api/radar?format=prompt")
        self.assertEqual(status_prompt, 200)
        self.assertIn("独立开发者", prompt)
        self.assertIn('"dailyWisdom"', prompt)

    def test_run_without_token_is_503_not_405(self):
        status, raw = request(
            self.base, "POST", "/api/run", body={},
            headers={"Content-Type": "application/json"},
        )
        # 回归点：无 token 时应是 503 且带明确提示，绝不能是 405。
        self.assertEqual(status, 503)
        self.assertIn("PUSHPLUS_TOKEN", json.loads(raw)["message"])

    def test_options_preflight_is_204(self):
        status, _ = request(self.base, "OPTIONS", "/api/run")
        self.assertEqual(status, 204)

    def test_unknown_api_is_404(self):
        status, _ = request(
            self.base, "POST", "/api/not-exist", body={},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 404)

    def test_get_run_is_404_not_405(self):
        status, _ = request(self.base, "GET", "/api/run")
        self.assertEqual(status, 404)

    def test_sensitive_endpoint_reports_status(self):
        status, raw = request(self.base, "GET", "/api/sensitive")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIn("ok", data)
        self.assertIn("dropped_count", data)
        self.assertIn("lexicon_size", data)
        self.assertGreater(data["lexicon_size"], 0)


class MockedPushTest(unittest.TestCase):
    """手动推送联调：配 token 的 server.py + 本地假 PushPlus。
    推送后检查记录文件（假文件）内容，确认推送真的发出且载荷正确。"""

    def test_push_success_writes_record_file(self):
        mock_port, srv_port = free_port(), free_port()
        with tempfile.TemporaryDirectory() as tmp:
            record_file = os.path.join(tmp, "pushplus_record.json")
            mock = start_mock(mock_port, record_file)
            # MARKET_FRESHNESS_FORCE=fresh：确定性地通过「大盘数据新鲜度」推送闸门
            #（沙箱可能连不上东方财富，真实检查会因快照兜底被拦截）。
            srv = start_server(srv_port, {
                "PUSHPLUS_TOKEN": "fake-token-123",
                "PUSHPLUS_API_URL": f"http://127.0.0.1:{mock_port}/send",
                "MARKET_FRESHNESS_FORCE": "fresh",
            })
            try:
                status, raw = request(
                    f"http://127.0.0.1:{srv_port}", "POST", "/api/run", body={},
                    headers={"Content-Type": "application/json"},
                )
                # 手动推送成功：server.py 返回 200 与成功提示。
                self.assertEqual(status, 200)
                self.assertIn("推送至 PushPlus", json.loads(raw)["message"])

                # 假文件必须存在且有内容。
                self.assertTrue(os.path.exists(record_file), "假文件未生成，推送似乎没有发出")
                record = json.loads(PathRead(record_file))
                payload = record["payload"]
                self.assertEqual(record["method"], "POST")
                self.assertEqual(record["path"], "/send")
                self.assertEqual(payload["token"], "fake-token-123")
                self.assertEqual(payload["template"], "html")
                # 默认一对一推送：载荷不带群组编码，消息只发给 token 所属账号。
                self.assertNotIn("topic", payload)
                self.assertIn("章鱼", payload["title"])
                # 推送内容为真实抓取六大板块数据源的 HTML 简报（网络不可用时回退演示数据）。
                self.assertIn("章鱼", payload["content"])
                self.assertIn("数据源", payload["content"])
                self.assertIn("AI 板块机会", payload["content"])  # 板块机会清单随简报一同推送
                self.assertIn("AI 政策分析", payload["content"])  # 政策面板块（鹰鸽取向）同样随简报推送
                self.assertIn("大 V 分析", payload["content"])  # 新增框架推演栏目同样随简报推送
                # 「AI 看盘」紧跟「AI 每日总结」；政策分析在其后，再接深度层与研报两张卡片。
                for card in ("AI 看盘", "AI 政策深度", "AI 政策研报"):
                    self.assertIn(card, payload["content"])
                content = payload["content"]
                self.assertLess(content.index('<span class="tag">AI 每日总结</span>'),
                                content.index('<span class="tag">AI 看盘</span>'))
                self.assertLess(content.index('<span class="tag">AI 看盘</span>'),
                                content.index('<span class="tag">AI 政策分析</span>'))
                self.assertLess(content.index("AI 政策分析"),
                                content.index("AI 板块机会"))
            finally:
                srv.terminate(); srv.wait(timeout=5)
                mock.terminate(); mock.wait(timeout=5)

    def test_push_rejected_returns_502(self):
        mock_port, srv_port = free_port(), free_port()
        with tempfile.TemporaryDirectory() as tmp:
            record_file = os.path.join(tmp, "pushplus_record.json")
            mock = start_mock(mock_port, record_file, fail=True)
            srv = start_server(srv_port, {
                "PUSHPLUS_TOKEN": "fake-token-123",
                "PUSHPLUS_API_URL": f"http://127.0.0.1:{mock_port}/send",
                "MARKET_FRESHNESS_FORCE": "fresh",
            })
            try:
                status, raw = request(
                    f"http://127.0.0.1:{srv_port}", "POST", "/api/run", body={},
                    headers={"Content-Type": "application/json"},
                )
                # PushPlus 拒绝（业务码非 200）→ server.py 应返回 502 并透出原因。
                self.assertEqual(status, 502)
                self.assertIn("token invalid", json.loads(raw)["message"])
            finally:
                srv.terminate(); srv.wait(timeout=5)
                mock.terminate(); mock.wait(timeout=5)

    def test_push_group_topic_only_when_configured(self):
        # 显式配置 PUSHPLUS_TOPIC 才切回一对多推送：载荷带群组编码。
        mock_port, srv_port = free_port(), free_port()
        with tempfile.TemporaryDirectory() as tmp:
            record_file = os.path.join(tmp, "pushplus_record.json")
            mock = start_mock(mock_port, record_file)
            srv = start_server(srv_port, {
                "PUSHPLUS_TOKEN": "fake-token-123",
                "PUSHPLUS_API_URL": f"http://127.0.0.1:{mock_port}/send",
                "MARKET_FRESHNESS_FORCE": "fresh",
                "PUSHPLUS_TOPIC": "oai.1",
            })
            try:
                status, raw = request(
                    f"http://127.0.0.1:{srv_port}", "POST", "/api/run", body={},
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200)
                record = json.loads(PathRead(record_file))
                self.assertEqual(record["payload"]["topic"], "oai.1")
            finally:
                srv.terminate(); srv.wait(timeout=5)
                mock.terminate(); mock.wait(timeout=5)

    def test_push_blocked_when_market_stale(self):
        # 推送前闸门：大盘数据非最新 → 409 取消推送，假 PushPlus 收不到任何请求。
        mock_port, srv_port = free_port(), free_port()
        with tempfile.TemporaryDirectory() as tmp:
            record_file = os.path.join(tmp, "pushplus_record.json")
            mock = start_mock(mock_port, record_file)
            srv = start_server(srv_port, {
                "PUSHPLUS_TOKEN": "fake-token-123",
                "PUSHPLUS_API_URL": f"http://127.0.0.1:{mock_port}/send",
                "MARKET_FRESHNESS_FORCE": "stale",
            })
            try:
                status, raw = request(
                    f"http://127.0.0.1:{srv_port}", "POST", "/api/run", body={},
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 409)
                message = json.loads(raw)["message"]
                self.assertIn("非最新", message)
                # 透出的 freshness 便于排查（含复盘日/接口最新日K/应复盘交易日）。
                freshness = json.loads(raw)["freshness"]
                for key in ("ok", "reason", "market_date", "source",
                            "latest_kline_date", "expected_date"):
                    self.assertIn(key, freshness)
                self.assertFalse(os.path.exists(record_file),
                                 "大盘数据非最新时不应发出推送")
            finally:
                srv.terminate(); srv.wait(timeout=5)
                mock.terminate(); mock.wait(timeout=5)

    def test_push_blocked_when_sensitive(self):
        # 推送前闸门：敏感词检测未通过 → 422 取消推送，假 PushPlus 收不到任何请求。
        mock_port, srv_port = free_port(), free_port()
        with tempfile.TemporaryDirectory() as tmp:
            record_file = os.path.join(tmp, "pushplus_record.json")
            mock = start_mock(mock_port, record_file)
            srv = start_server(srv_port, {
                "PUSHPLUS_TOKEN": "fake-token-123",
                "PUSHPLUS_API_URL": f"http://127.0.0.1:{mock_port}/send",
                "MARKET_FRESHNESS_FORCE": "fresh",
                "SENSITIVE_FORCE": "block",
            })
            try:
                status, raw = request(
                    f"http://127.0.0.1:{srv_port}", "POST", "/api/run", body={},
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 422)
                body = json.loads(raw)
                self.assertIn("敏感词", body["message"])
                self.assertIn("sensitive", body)
                self.assertFalse(body["sensitive"]["ok"])
                self.assertFalse(os.path.exists(record_file),
                                 "敏感词检测未通过时不应发出推送")
            finally:
                srv.terminate(); srv.wait(timeout=5)
                mock.terminate(); mock.wait(timeout=5)


class MarketEndpointTest(unittest.TestCase):
    """GET /api/market：大盘数据新鲜度状态（推送闸门同一套检查结果）。"""

    def test_market_endpoint_reports_freshness(self):
        srv_port = free_port()
        srv = start_server(srv_port, {"MARKET_FRESHNESS_FORCE": "fresh"})
        try:
            status, raw = request(f"http://127.0.0.1:{srv_port}", "GET", "/api/market")
            self.assertEqual(status, 200)
            data = json.loads(raw)
            self.assertTrue(data["freshness"]["ok"])
            # MARKET_FRESHNESS_FORCE 生效：reason 以强制结果开头。
            self.assertIn("强制结果", data["freshness"]["reason"])
            # 复盘概要：日期 / 来源 / 偏多偏空 / 四大指数（沙箱离线时为快照兜底）。
            self.assertTrue(data["date"])
            self.assertIn(data["source"], ("eastmoney", "snapshot"))
            self.assertIn(data["bias"], ("偏多", "偏空", "中性"))
            self.assertGreaterEqual(len(data["indices"]), 3)
        finally:
            srv.terminate(); srv.wait(timeout=5)


def PathRead(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    unittest.main(verbosity=2)
