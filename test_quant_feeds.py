#!/usr/bin/env python3
"""quant_feeds.py 解析回归：用固定报文跑通主源 / 备用源 / 兜底，不联网。

    python3 test_quant_feeds.py

覆盖点：
- 东财行情两种数值口径（fltt=2 小数、未带 fltt 的整数分位）都要解析正确；
- 腾讯备用源（成交额单位是万元）换算成亿元；
- 行业板块 clist 的 list / dict 两种 data 形态；
- 两融、龙虎榜的字段识别与单位换算（元 → 亿元）；
- 大类资产候选代码逐个尝试，主代码失败自动换下一个并如实标注；
- 全部失败时返回 ok=False 且带错误信息，不抛异常、不编数。
"""
import json
import unittest

import quant_feeds as qf


def _json(payload):
    def _loader(url, params=None, headers=None):
        return payload
    return _loader


def _tencent_fields(price="4685.42", pct="0.62", amount_wan="5120000",
                    pe="13.42", pb="1.48") -> list:
    """按腾讯行情字段位置造报文：3=价 32=涨跌幅 37=成交额(万元) 39=PE 46=PB。"""
    fields = ["0"] * 47
    fields[0], fields[1], fields[2] = "1", "沪深300", "000300"
    fields[3], fields[4], fields[5] = price, "4656.56", "4660.00"
    fields[32], fields[37], fields[39], fields[46] = pct, amount_wan, pe, pb
    return fields


def _text(text):
    def _loader(url, headers=None, timeout=None):
        return text
    return _loader


class FeedCase(unittest.TestCase):
    def setUp(self):
        self._json, self._text = qf.GET_JSON, qf.GET_TEXT

    def tearDown(self):
        qf.GET_JSON, qf.GET_TEXT = self._json, self._text


class IndexQuoteTest(FeedCase):
    def test_parses_decimal_quotes_and_breadth(self):
        payload = {"data": {
            "f43": 4685.42, "f48": 5.12e11, "f58": "沪深300",
            "f162": 13.42, "f167": 1.48, "f170": 0.62,
        }}
        qf.GET_JSON = _json(payload)
        result = qf.index_quotes()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "eastmoney")
        quote = result["quotes"]["沪深300"]
        self.assertAlmostEqual(quote["pct"], 0.62, places=2)
        self.assertAlmostEqual(quote["close"], 4685.42, places=2)
        self.assertEqual(quote["amount"], 5120.0)   # 元 → 亿元
        self.assertAlmostEqual(quote["pe"], 13.42, places=2)
        self.assertAlmostEqual(quote["pb"], 1.48, places=2)
        # 涨跌家数只由上证 / 深证两个市场指数累加，单指数不重复计数
        self.assertEqual(result["breadth"], {})

    def test_parses_integer_scaled_quotes(self):
        # 未带 fltt=2 时东财返回整数分位：468542 = 4685.42 点，113 = +1.13%
        payload = {"data": {"f43": 468542, "f170": 113, "f48": 5.12e11,
                            "f162": 1342, "f167": 148}}
        qf.GET_JSON = _json(payload)
        quote = qf.index_quotes()["quotes"]["沪深300"]
        self.assertAlmostEqual(quote["close"], 4685.42, places=2)
        self.assertAlmostEqual(quote["pct"], 1.13, places=2)
        self.assertAlmostEqual(quote["pe"], 13.42, places=2)

    def test_breadth_sums_shanghai_and_shenzhen(self):
        def loader(url, params=None, headers=None):
            secid = (params or {}).get("secid")
            if secid == "1.000001":
                return {"data": {"f43": 3956.57, "f170": 1.13, "f104": 1400, "f105": 700, "f106": 40}}
            if secid == "0.399001":
                return {"data": {"f43": 14048.88, "f170": 1.50, "f104": 1900, "f105": 900, "f106": 60}}
            return {"data": None}
        qf.GET_JSON = loader
        result = qf.index_quotes()
        self.assertTrue(result["ok"])
        self.assertEqual(result["breadth"], {"up": 3300, "down": 1600, "flat": 100})

    def test_all_failed_returns_error_not_exception(self):
        def boom(url, params=None, headers=None):
            raise OSError("network blocked")
        qf.GET_JSON = boom
        result = qf.index_quotes()
        self.assertFalse(result["ok"])
        self.assertIn("network blocked", result["error"])

    def test_tencent_backup_parses_fields(self):
        # 腾讯：3=价 32=涨跌幅 37=成交额（万元） 39=PE 46=PB
        text = f'v_sh000300="{"~".join(_tencent_fields())}";'
        qf.GET_TEXT = _text(text)
        qf.GET_JSON = lambda *a, **k: (_ for _ in ()).throw(OSError("em down"))
        result = qf.index_quotes_tencent()
        self.assertTrue(result["ok"])
        quote = result["quotes"]["沪深300"]
        self.assertAlmostEqual(quote["pct"], 0.62, places=2)
        self.assertAlmostEqual(quote["close"], 4685.42, places=2)
        self.assertEqual(quote["amount"], 512.0)  # 万元 → 亿元


class IndustryTest(FeedCase):
    def test_parses_list_and_computes_share(self):
        payload = {"data": {"diff": [
            {"f14": "半导体", "f3": 3.42, "f6": 1.86e11, "f62": 9.64e9, "f104": 90, "f105": 10},
            {"f14": "煤炭", "f3": -1.86, "f6": 2.6e10, "f62": -2.41e9, "f104": 5, "f105": 30},
        ]}}
        qf.GET_JSON = _json(payload)
        result = qf.industry_boards()
        self.assertTrue(result["ok"])
        self.assertEqual([b["name"] for b in result["boards"]], ["半导体", "煤炭"])
        self.assertEqual(result["boards"][0]["main_net"], 96.4)
        self.assertEqual(result["boards"][0]["turnover_share"], 87.74)
        self.assertIn("申万一级代理", result["proxy"])

    def test_parses_dict_shaped_diff(self):
        payload = {"data": {"diff": {"0": {"f14": "银行", "f3": -0.84, "f6": 3.8e10}}}}
        qf.GET_JSON = _json(payload)
        boards = qf.industry_boards()["boards"]
        self.assertEqual(boards[0]["name"], "银行")
        self.assertAlmostEqual(boards[0]["pct"], -0.84, places=2)

    def test_empty_payload_is_error(self):
        qf.GET_JSON = _json({"data": {"diff": []}})
        self.assertFalse(qf.industry_boards()["ok"])


class MarginTest(FeedCase):
    def test_sums_balance_and_prev(self):
        payload = {"result": {"data": [
            {"DIM_DATE": "2026-10-26 00:00:00", "RZRQYE": 1.986e12},
            {"DIM_DATE": "2026-10-23 00:00:00", "RZRQYE": 1.981e12},
        ]}}
        qf.GET_JSON = _json(payload)
        result = qf.margin_balances()
        self.assertTrue(result["ok"])
        self.assertEqual(result["current"], 19860.0)
        self.assertEqual(result["prev"], 19810.0)
        self.assertEqual(result["date"], "2026-10-26")

    def test_falls_back_to_components(self):
        payload = {"result": {"data": [{"DIM_DATE": "2026-10-26", "RZYE": 1.9e12, "RQYE": 8.6e10}]}}
        qf.GET_JSON = _json(payload)
        self.assertEqual(qf.margin_balances()["current"], 19860.0)

    def test_missing_fields_is_error(self):
        qf.GET_JSON = _json({"result": {"data": [{"DIM_DATE": "2026-10-26"}]}})
        self.assertFalse(qf.margin_balances()["ok"])


class BillboardTest(FeedCase):
    def test_splits_institution_and_broker_seats(self):
        payload = {"result": {"data": [
            {"TRADE_DATE": "2026-10-26", "SECURITY_NAME_ABBR": "甲股份",
             "OPERATEDEPT_NAME": "机构专用", "NET_BUY_AMT": 4.82e8},
            {"TRADE_DATE": "2026-10-26", "SECURITY_NAME_ABBR": "甲股份",
             "OPERATEDEPT_NAME": "机构专用", "NET_BUY_AMT": 1.0e8},
            {"TRADE_DATE": "2026-10-26", "SECURITY_NAME_ABBR": "乙股份",
             "OPERATEDEPT_NAME": "华鑫证券上海分公司", "NET_BUY_AMT": 2.86e8},
        ]}}
        qf.GET_JSON = _json(payload)
        result = qf.billboard("2026-10-26")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["inst"]), 1)
        self.assertEqual(result["inst"][0]["name"], "甲股份")
        self.assertEqual(result["inst"][0]["net"], 5.82)   # 同股合并
        self.assertEqual(result["hot"][0]["name"], "乙股份")
        self.assertEqual(result["hot"][0]["net"], 2.86)
        self.assertIn("不代表特定游资身份", result["note"])
        self.assertEqual(result["date"], "2026-10-26")

    def test_empty_is_error_with_date(self):
        qf.GET_JSON = _json({"result": {"data": []}})
        result = qf.billboard("2026-10-27")
        self.assertFalse(result["ok"])
        self.assertIn("2026-10-27", result["error"])


class CrossAssetTest(FeedCase):
    def test_uses_candidates_and_records_which_one(self):
        calls = []

        def loader(url, params=None, headers=None):
            secid = (params or {}).get("secid")
            calls.append(secid)
            if secid == "101.GC00Y":
                raise OSError("cme blocked")
            if secid == "113.au0":
                return {"data": {"f43": 612.4, "f58": "沪金主链", "f60": 610.0, "f170": 0.39}}
            if secid == "100.CN10Y":
                return {"data": {"f43": 1.82, "f58": "中国10年期国债收益率", "f60": 1.85, "f170": -1.62}}
            return {"data": None}
        qf.GET_JSON = loader
        result = qf.cross_asset()
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"]["gold"]["name"], "沪金主链")
        self.assertEqual(result["used"]["gold"], "沪金主链")
        self.assertAlmostEqual(result["items"]["cgb10y"]["value"], 1.82, places=4)
        self.assertAlmostEqual(result["items"]["cgb10y"]["prev"], 1.85, places=4)
        # 商品与汇率候选都没数据 → 不塞假值
        self.assertNotIn("commodity", result["items"])
        self.assertIn("113.au0", calls)

    def test_all_candidates_fail(self):
        qf.GET_JSON = lambda *a, **k: {"data": None}
        result = qf.cross_asset()
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])


class LimitPoolTest(FeedCase):
    def test_counts_sectors_and_ladder(self):
        payload = {"data": {"tc": 3, "pool": [
            {"c": "000001", "n": "甲股份", "hybk": "半导体", "lbc": 4},
            {"c": "000002", "n": "乙股份", "hybk": "半导体", "lbc": 1},
            {"c": "000003", "n": "丙股份", "hybk": "算力", "lbc": 2},
        ]}}
        qf.GET_JSON = _json(payload)
        result = qf.limit_pools("2026-10-26")
        self.assertTrue(result["ok"])
        self.assertEqual(result["limit_up"], 3)
        self.assertEqual(result["sectors_up"]["半导体"], {"count": 2, "max_board": 4})
        self.assertEqual(result["sectors_up"]["算力"], {"count": 1, "max_board": 2})
        self.assertIn("甲股份", result["ladder"])
        self.assertIn("4 连板", result["ladder"])

    def test_down_pool_and_failure(self):
        def loader(url, params=None, headers=None):
            if url == qf.EM_ZT_URL:
                raise OSError("zt blocked")
            return {"data": {"tc": 2, "pool": [{"n": "丁股份", "hybk": "煤炭", "lbc": 1}]}}
        qf.GET_JSON = loader
        result = qf.limit_pools("2026-10-26")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["limit_up"])
        self.assertEqual(result["limit_down"], 2)
        self.assertEqual(result["sectors_down"]["煤炭"]["count"], 1)
        self.assertIn("zt blocked", result["errors"][0])

    def test_both_pools_fail(self):
        qf.GET_JSON = lambda *a, **k: (_ for _ in ()).throw(OSError("all blocked"))
        self.assertFalse(qf.limit_pools("2026-10-26")["ok"])


class CollectTest(FeedCase):
    def test_collect_merges_each_feed_and_isolates_failures(self):
        def loader(url, params=None, headers=None):
            if url == qf.EM_DATA_URL:
                raise OSError("datacenter blocked")
            if url == qf.EM_CLIST_URL:
                return {"data": {"diff": [{"f14": "半导体", "f3": 3.42, "f6": 1e11}]}}
            secid = (params or {}).get("secid", "")
            if secid == "1.000300":
                return {"data": {"f43": 4685.42, "f170": 0.62, "f48": 5.12e11}}
            if secid == "100.CN10Y":
                return {"data": {"f43": 1.82, "f60": 1.85, "f170": -1.62}}
            return {"data": None}
        qf.GET_JSON = loader
        results = qf.collect("2026-10-26")
        self.assertEqual(set(results), {"quotes", "industry", "margin", "billboard",
                                        "cross_asset", "limit_pools"})
        self.assertTrue(results["quotes"]["ok"])
        self.assertTrue(results["industry"]["ok"])
        self.assertFalse(results["margin"]["ok"])
        self.assertFalse(results["billboard"]["ok"])
        # 国债取到了 → 大类资产整体可用，缺的资产不塞假值
        self.assertTrue(results["cross_asset"]["ok"])
        self.assertEqual(set(results["cross_asset"]["items"]), {"cgb10y"})

    def test_collect_falls_back_to_tencent_when_eastmoney_dead(self):
        text = f'v_sh000300="{"~".join(_tencent_fields())}";'

        def loader(url, params=None, headers=None):
            raise OSError("eastmoney blocked")
        qf.GET_JSON = loader
        qf.GET_TEXT = _text(text)
        results = qf.collect("2026-10-26")
        self.assertTrue(results["quotes"]["ok"])
        self.assertEqual(results["quotes"]["source"], "tencent")
        self.assertIn("eastmoney blocked", results["quotes"]["primary_error"])

    def test_collect_survives_collector_exception(self):
        original = qf.industry_boards

        def boom(limit=100):
            raise RuntimeError("parser bug")
        qf.industry_boards = boom
        try:
            qf.GET_JSON = _json({"data": {"f43": 4685.42, "f170": 0.62}})
            qf.GET_TEXT = _text("")
            results = qf.collect("2026-10-26")
            self.assertFalse(results["industry"]["ok"])
            self.assertIn("parser bug", results["industry"]["error"])
        finally:
            qf.industry_boards = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
