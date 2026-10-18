#!/usr/bin/env python3
"""DeepSeek Search & Fetch 接入测试（2026-10-17 新 API，全程走本地假服务，不碰公网、不需要真 Key）。

覆盖：
- deepseek_search 客户端：配置自检不泄漏 Key、query/count/url 官方校验、
  site: 检索式拼接、results 归一化、鉴权头与请求体、400/401 不重试、429 重试成功、
  未配置 Key 抛 DeepSeekNotConfigured、5 QPS 限流器间隔。
- sources 集成：**全部**原 Google News RSS 搜索型源都带 deepseek_search 标记（防漏接），
  配置 Key 后 collect_one 走 DeepSeek、搜索空结果回退原 RSS 通道、未配置行为不变；
  「Google news 中文」同样 DeepSeek 优先。
- server 接口：/api/deepseek/status、GET/POST /api/search（缺 q→400、未配置→503、缓存命中）、
  GET/POST /api/fetch。
"""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import deepseek_search
import sources

SEARCH_RESULTS = [
    {"rerank_score": 2.4, "title": "DeepSeek V4.1 发布 - Reuters", "url": "https://example.com/a",
     "snippet": "短摘要", "summary": "长摘要", "content": "", "site_name": "Example News",
     "site_icon": None, "date": "2026-10-17T00:00:00+00:00", "authority": 3},
    {"rerank_score": 1.6, "title": "第二条搜索结果", "url": "https://example.com/b",
     "snippet": "短摘要二", "summary": "长摘要二", "content": "正文全文", "site_name": None,
     "site_icon": None, "date": None, "authority": -1},
    {"rerank_score": 1.6, "title": "重复 URL 应去重", "url": "https://example.com/b",
     "snippet": "", "summary": "", "content": "", "site_name": None,
     "site_icon": None, "date": None, "authority": 0},
]


class _MockDeepSeek(BaseHTTPRequestHandler):
    """本地假 DeepSeek API：按官方路径/响应形状回放，记录所有请求供断言。"""
    requests_seen: list = []
    flaky_hits: int = 0

    def log_message(self, *_args):  # 静音
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests_seen.append({
            "path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.headers.get("Authorization") == "Bearer bad-key":
            return self._send(401, {"error": {"message": "invalid api key",
                                              "type": "invalid_request_error",
                                              "param": None, "code": "invalid_request_error"}})
        if self.path == "/v1/tools/search":
            query = body.get("query", "")
            if "flaky" in query:
                type(self).flaky_hits += 1
                if type(self).flaky_hits == 1:
                    return self._send(429, {"error": {"message": "rate limited",
                                                      "type": "rate_limit_error",
                                                      "param": None, "code": "rate_limit_error"}})
            if "empty-result-marker" in query:
                return self._send(200, {"results": []})
            return self._send(200, {"results": SEARCH_RESULTS})
        if self.path == "/v1/tools/fetch":
            if "missing" in body.get("url", ""):
                return self._send(404, {"error": {"message": "no usable content",
                                                  "type": "invalid_request_error",
                                                  "param": None, "code": "invalid_request_error"}})
            return self._send(200, {"title": "示例文章", "content": "# 示例文章\n\n正文。"})
        return self._send(404, {"error": {"message": "unknown path", "type": "invalid_request_error",
                                          "param": None, "code": "invalid_request_error"}})


class DeepSeekEnvTestCase(unittest.TestCase):
    """统一管理 DEEPSEEK_* 环境变量与假服务端点。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockDeepSeek)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _MockDeepSeek.requests_seen.clear()
        _MockDeepSeek.flaky_hits = 0
        deepseek_search.reset_rate_limiters()
        # 测试不测真实限速：把间隔关掉跑得快（限流器单独有专项用例）。
        self._intervals = (deepseek_search._SEARCH_LIMITER._interval,
                           deepseek_search._FETCH_LIMITER._interval)
        deepseek_search._SEARCH_LIMITER._interval = 0
        deepseek_search._FETCH_LIMITER._interval = 0
        self._old_env = {k: os.environ.get(k) for k in (
            "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE_URL", "DEEPSEEK_SEARCH_BASE_URL",
            "DEEPSEEK_SEARCH_ENABLED", "DEEPSEEK_SEARCH_MAX_RETRIES")}
        os.environ["DEEPSEEK_API_KEY"] = "test-key-1234"
        os.environ["DEEPSEEK_API_BASE_URL"] = self.base
        os.environ.pop("DEEPSEEK_SEARCH_ENABLED", None)
        os.environ.pop("DEEPSEEK_SEARCH_BASE_URL", None)
        os.environ.pop("DEEPSEEK_SEARCH_MAX_RETRIES", None)

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        (deepseek_search._SEARCH_LIMITER._interval,
         deepseek_search._FETCH_LIMITER._interval) = self._intervals
        deepseek_search.reset_rate_limiters()


class TestClientConfig(DeepSeekEnvTestCase):
    def test_status_never_leaks_key(self):
        info = deepseek_search.status()
        self.assertTrue(info["enabled"])
        self.assertTrue(info["configured"])
        self.assertEqual(info["release"], "2026-10-17")
        self.assertEqual(info["price_per_1000_successful_requests_usd"], 0.75)
        self.assertEqual(info["qps_limit_per_endpoint"], 5)
        self.assertNotIn("test-key-1234", json.dumps(info))
        self.assertEqual(info["api_key_masked"], "***1234")

    def test_not_configured_and_disabled(self):
        os.environ.pop("DEEPSEEK_API_KEY")
        self.assertFalse(deepseek_search.is_enabled())
        with self.assertRaises(deepseek_search.DeepSeekNotConfigured):
            deepseek_search.search("测试")
        os.environ["DEEPSEEK_API_KEY"] = "test-key-1234"
        os.environ["DEEPSEEK_SEARCH_ENABLED"] = "0"
        self.assertFalse(deepseek_search.is_enabled())
        with self.assertRaises(deepseek_search.DeepSeekNotConfigured):
            deepseek_search.fetch_page("https://example.com/")
        self.assertEqual(_MockDeepSeek.requests_seen, [], "未启用时不得真的发请求")


class TestValidation(unittest.TestCase):
    def test_query_validation(self):
        self.assertEqual(deepseek_search.validate_query("DeepSeek site:github.com"),
                         "DeepSeek site:github.com")
        for bad in ("", "   ", "x" * 129):
            with self.assertRaises(deepseek_search.DeepSeekSearchError):
                deepseek_search.validate_query(bad)

    def test_count_clamp(self):
        self.assertEqual(deepseek_search.clamp_count(0), 1)
        self.assertEqual(deepseek_search.clamp_count(999), 50)
        self.assertEqual(deepseek_search.clamp_count("abc"), 10)
        self.assertEqual(deepseek_search.clamp_count(7), 7)

    def test_fetch_url_validation(self):
        self.assertEqual(deepseek_search.validate_fetch_url("https://example.com/a?b=1"),
                         "https://example.com/a?b=1")
        for bad in ("ftp://example.com", "https://user:pass@example.com/", "example.com/no-scheme"):
            with self.assertRaises(deepseek_search.DeepSeekSearchError):
                deepseek_search.validate_fetch_url(bad)

    def test_site_search_query(self):
        from datetime import date
        q = deepseek_search.site_search_query("xueqiu.com", "zh", days=7, today=date(2026, 10, 18))
        self.assertEqual(q, "site:xueqiu.com lang:zh after:2026-10-11")
        q2 = deepseek_search.site_search_query("reuters.com", "en", days=1, today=date(2026, 10, 18))
        self.assertEqual(q2, "site:reuters.com lang:en after:2026-10-17")
        long_q = deepseek_search.site_search_query("zhihu.com", "zh", term="词" * 200,
                                                   today=date(2026, 10, 18))
        self.assertLessEqual(len(long_q), 128)
        self.assertTrue(long_q.endswith("site:zhihu.com lang:zh after:2026-10-11"))

    def test_results_to_items(self):
        items = deepseek_search.results_to_items(SEARCH_RESULTS, strip_suffix=" - Reuters")
        self.assertEqual(len(items), 2, "重复 URL 必须去重")
        self.assertEqual(items[0]["title"], "DeepSeek V4.1 发布")
        self.assertEqual(items[0]["source_api"], "deepseek-search")
        self.assertEqual(items[0]["authority"], 3)
        self.assertNotIn("content", items[0], "content 为空时不带该字段")
        self.assertEqual(items[1]["content"], "正文全文", "need_content 的正文必须保留")


class TestSearchFetchRoundTrip(DeepSeekEnvTestCase):
    def test_search_sends_auth_and_body(self):
        items = deepseek_search.search_items("DeepSeek V4 发布", limit=5)
        self.assertEqual(len(items), 2)
        seen = _MockDeepSeek.requests_seen[-1]
        self.assertEqual(seen["path"], "/v1/tools/search")
        self.assertEqual(seen["auth"], "Bearer test-key-1234")
        self.assertEqual(seen["body"]["query"], "DeepSeek V4 发布")
        self.assertEqual(seen["body"]["count"], 5)
        self.assertFalse(seen["body"]["need_content"])

    def test_fetch_round_trip(self):
        page = deepseek_search.fetch_page("https://example.com/article")
        self.assertEqual(page["title"], "示例文章")
        self.assertIn("正文", page["content"])
        self.assertEqual(_MockDeepSeek.requests_seen[-1]["path"], "/v1/tools/fetch")

    def test_401_not_retried(self):
        os.environ["DEEPSEEK_API_KEY"] = "bad-key"
        with self.assertRaises(deepseek_search.DeepSeekSearchError) as ctx:
            deepseek_search.search("测试")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(_MockDeepSeek.requests_seen), 1, "其余 4xx 不重试")

    def test_429_retried_then_success(self):
        items = deepseek_search.search_items("flaky 搜索", limit=3)
        self.assertEqual(len(items), 2)
        self.assertEqual(_MockDeepSeek.flaky_hits, 2, "429 必须按官方口径重试")

    def test_fetch_404_surfaces(self):
        with self.assertRaises(deepseek_search.DeepSeekSearchError) as ctx:
            deepseek_search.fetch_page("https://example.com/missing-page")
        self.assertEqual(ctx.exception.status, 404)


class TestRateLimiter(unittest.TestCase):
    def test_min_interval(self):
        limiter = deepseek_search._RateLimiter(qps=20)  # 50ms 间隔
        t0 = time.monotonic()
        limiter.wait()
        limiter.wait()
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.045, "两次调用起点至少间隔 1/qps")


class TestSourcesIntegration(DeepSeekEnvTestCase):
    def test_every_google_news_search_source_is_deepseek_backed(self):
        """「全部搜索」防漏：凡 feed 走 Google News RSS 检索的源，必须同时带 deepseek_search。"""
        search_feeds = [m for m in sources.SOURCE_META
                        if "news.google.com/rss/search" in str(m.get("feed", ""))]
        self.assertGreaterEqual(len(search_feeds), 11)
        for meta in search_feeds:
            self.assertIn("deepseek_search", meta, f"搜索型源漏接 DeepSeek：{meta['name']}")
        backed = sources.deepseek_search_sources()
        for meta in search_feeds:
            self.assertIn(meta["name"], backed)
        self.assertIn("Google news 中文", backed)
        self.assertIn("Reuters 路透", backed)

    def test_collect_one_uses_deepseek_when_enabled(self):
        items = sources.collect_one("雪球 讨论精选", limit=5)
        self.assertTrue(items and items[0]["source_api"] == "deepseek-search")
        query = _MockDeepSeek.requests_seen[-1]["body"]["query"]
        self.assertIn("site:xueqiu.com", query)
        self.assertIn("lang:zh", query)
        self.assertIn("after:", query)

    def test_reuters_strips_suffix_via_deepseek(self):
        items = sources.collect_one("Reuters 路透", limit=5)
        self.assertEqual(items[0]["title"], "DeepSeek V4.1 发布", "须去掉「 - Reuters」后缀")
        self.assertIn("site:reuters.com", _MockDeepSeek.requests_seen[-1]["body"]["query"])

    def test_empty_search_falls_back_to_rss(self):
        sentinel = [{"title": "RSS 回退条目", "url": "https://example.com/rss"}]
        with patch.object(deepseek_search, "search_items", return_value=[]), \
                patch.object(sources, "_collect_feed", return_value=sentinel) as mock_feed:
            items = sources.collect_one("雪球 讨论精选", limit=5)
        self.assertEqual(items, sentinel)
        mock_feed.assert_called_once()

    def test_search_error_falls_back_to_rss(self):
        sentinel = [{"title": "RSS 回退条目", "url": "https://example.com/rss"}]
        boom = deepseek_search.DeepSeekSearchError("boom", status=502)
        with patch.object(deepseek_search, "search_items", side_effect=boom), \
                patch.object(sources, "_collect_feed", return_value=sentinel):
            items = sources.collect_one("雪球 讨论精选", limit=5)
        self.assertEqual(items, sentinel, "搜索 API 失败不得让单源掉成演示数据")

    def test_no_key_keeps_legacy_path(self):
        os.environ.pop("DEEPSEEK_API_KEY")
        sentinel = [{"title": "RSS 原通道条目", "url": "https://example.com/rss"}]
        with patch.object(sources, "_collect_feed", return_value=sentinel):
            items = sources.collect_one("雪球 讨论精选", limit=5)
        self.assertEqual(items, sentinel)
        self.assertEqual(_MockDeepSeek.requests_seen, [], "无 Key 不得调用 DeepSeek")

    def test_google_news_collector_deepseek_first(self):
        items = sources._collect_google_news(5)
        self.assertTrue(items and items[0]["source_api"] == "deepseek-search")
        self.assertIn("lang:zh", _MockDeepSeek.requests_seen[-1]["body"]["query"])


class TestServerEndpoints(DeepSeekEnvTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import server as app_server
        cls.app = app_server
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        cls.app_port = cls.httpd.server_address[1]
        cls.app_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.app_thread.start()
        cls.app_base = f"http://127.0.0.1:{cls.app_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.app._SEARCH_CACHE.clear()

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = Request(self.app_base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8") or "{}")

    def test_status_endpoint(self):
        status, data = self._call("GET", "/api/deepseek/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"])
        self.assertGreaterEqual(len(data["search_backed_sources"]), 12)
        self.assertNotIn("test-key-1234", json.dumps(data))

    def test_search_endpoint_and_cache(self):
        status, data = self._call("GET", "/api/search?q=DeepSeek%20V4&count=5")
        self.assertEqual(status, 200)
        self.assertEqual(data["provider"], "deepseek-search")
        self.assertEqual(len(data["results"]), 2)
        self.assertFalse(data["cached"])
        status2, data2 = self._call("GET", "/api/search?q=DeepSeek%20V4&count=5")
        self.assertEqual(status2, 200)
        self.assertTrue(data2["cached"], "同一查询 5 分钟内应命中缓存，避免重复计费")

    def test_search_missing_query_is_400(self):
        status, _ = self._call("GET", "/api/search")
        self.assertEqual(status, 400)
        status, _ = self._call("POST", "/api/search", {})
        self.assertEqual(status, 400)

    def test_search_post_and_fetch_endpoints(self):
        status, data = self._call("POST", "/api/search", {"query": "港股 行情", "count": 3})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["results"]), 2)
        status, page = self._call("POST", "/api/fetch", {"url": "https://example.com/article"})
        self.assertEqual(status, 200)
        self.assertEqual(page["title"], "示例文章")
        status, page = self._call("GET", "/api/fetch?url=https://example.com/article")
        self.assertEqual(status, 200)
        self.assertIn("正文", page["content"])

    def test_unconfigured_returns_503(self):
        os.environ.pop("DEEPSEEK_API_KEY")
        self.app._SEARCH_CACHE.clear()
        status, data = self._call("GET", "/api/search?q=%E6%B5%8B%E8%AF%95")
        self.assertEqual(status, 503)
        self.assertIn("DEEPSEEK_API_KEY", data["message"])
        status, _ = self._call("POST", "/api/fetch", {"url": "https://example.com/"})
        self.assertEqual(status, 503)


if __name__ == "__main__":
    unittest.main(verbosity=2)
