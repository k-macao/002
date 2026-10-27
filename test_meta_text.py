#!/usr/bin/env python3
"""meta_text 回归：说明 / 注解 / 解析 / 提醒类文字要「整段不见」，数据要「一个字不少」。

    python3 test_meta_text.py

覆盖点：
- 句子级：口径、方法注、免责、流程提示整句丢；
- 分句级：「行情数据暂缺」只丢这一句，同段的结论与数值留下；
- 只认固定搭配：「官方口径 / 术语口径」是数据标签，「映射口径 / 采集口径」是说明；
- 元素级：页脚整块删、清空的容器与只剩编号的行一并回收；
- 保护：`<script>` / `<style>` 与「去 AI 味」诊断面板、41 个原子动作清单不动；
- 事实锁：只减不增，不能凭空冒出数字或链接；
- 页面侧：data-meta 标记幂等，标记前会被体检抓出来。
"""
import json
import re
import unittest

import meta_text as mt


class SentenceTest(unittest.TestCase):
    def test_data_sentence_is_kept(self):
        text = "放量普涨，算力硬件领涨主线，科创50 +3.77% 领跑，涨停 77 家。"
        self.assertEqual(mt.strip_text(text), text)

    def test_method_sentence_is_dropped(self):
        text = "指数分可复算：主题内取最强贡献。≥1.2 偏多 / ≤-1.2 偏空。"
        self.assertEqual(mt.strip_text(text), "≥1.2 偏多 / ≤-1.2 偏空。")

    def test_meta_phrase_fixed_collocations(self):
        self.assertTrue(mt.is_meta("映射口径：黄金 + 全球战略资源"))
        self.assertTrue(mt.is_meta("情感口径：正向词 / 负向词命中差值定极性"))
        self.assertFalse(mt.is_meta("官媒口径情绪 +0.05 高于市场化解读 -0.28（差 +0.32）"))
        self.assertFalse(mt.is_meta("├─ 术语口径中性（加权强度 -0.67）"))

    def test_note_marker_needs_clause_start(self):
        self.assertTrue(mt.is_meta("注：仅按成交额占比判强弱"))
        # 「明日关注：」是标题，不是注释
        self.assertFalse(mt.is_meta("明日关注：半导体（半导体设备招标落地）"))

    def test_step_name_with_collect_is_kept(self):
        self.assertEqual(mt.strip_text("前置数据采集"), "前置数据采集")
        self.assertEqual(mt.strip_text("采集层（quant_feeds.py）对接东财"), "")

    def test_disclaimer_tail_is_clause_level(self):
        text = "官媒口径情绪 +0.05 高于市场化解读 -0.28（差 +0.32）；该差值为统计信号，仅供参考。"
        self.assertEqual(mt.strip_text(text),
                         "官媒口径情绪 +0.05 高于市场化解读 -0.28（差 +0.32）")

    def test_author_line_keeps_name(self):
        self.assertEqual(mt.strip_text("作者：章鱼 ai　　仅供参考，分析研究"), "作者：章鱼 ai")


class ClauseTest(unittest.TestCase):
    def test_missing_data_clause_does_not_eat_conclusion(self):
        text = ("2026-10-27，行情数据暂缺，五维：基本面中性·配置中性，"
                "权益仓位 50%，风险 高0/中2")
        out = mt.strip_text(text)
        self.assertNotIn("数据暂缺", out)
        self.assertIn("权益仓位 50%", out)
        self.assertIn("2026-10-27", out)

    def test_status_clause_in_list(self):
        self.assertEqual(mt.strip_text("事件风险（中）；数据完整度（中）"), "事件风险（中）")

    def test_tail_punctuation_is_cleaned(self):
        out = mt.strip_text("事件风险（中）；数据完整度（中）")
        self.assertFalse(out.endswith("；"))


class ElementTest(unittest.TestCase):
    def test_footer_is_dropped_whole(self):
        html = '<div class="card"><div class="txt">涨停 77 家</div><div class="ftr">按五维评分合成，仅供参考。</div></div>'
        cleaned, removed = mt.strip_meta_html(html)
        self.assertNotIn("ftr", cleaned)
        self.assertIn("涨停 77 家", cleaned)
        self.assertTrue(any("页脚" in item for item in removed))

    def test_ev_line_is_dropped_when_all_meta(self):
        html = '<div class="ev">· 采集口径：实测 2 项 / 代理 0 项 / 未取得 18 项</div>'
        cleaned, _ = mt.strip_meta_html(html)
        self.assertEqual(cleaned, "")

    def test_ev_line_keeps_data_fragment(self):
        html = '<div class="ev">信号 4:1 · 线索列表不展示</div>'
        cleaned, _ = mt.strip_meta_html(html)
        self.assertIn("信号 4:1", cleaned)
        self.assertNotIn("不展示", cleaned)

    def test_table_row_with_only_label_is_recycled(self):
        html = ('<table><tr><td class="td-n">α</td><td><div>因子：数据暂缺</div></td></tr>'
                '<tr><td>维</td><td>基本面·中性</td></tr></table>')
        cleaned, _ = mt.strip_meta_html(html)
        self.assertNotIn("α", cleaned)
        self.assertIn("基本面·中性", cleaned)

    def test_bullet_only_text_is_removed(self):
        html = '<div class="ev">· 模型情景，非预测</div>'
        cleaned, _ = mt.strip_meta_html(html)
        self.assertNotIn("·", cleaned)

    def test_style_and_script_are_untouched(self):
        html = ('<style>.x{content:"口径"}</style><script>var s="数据暂缺";</script>'
                '<div class="ev">统计口径：按源计数</div>')
        cleaned, _ = mt.strip_meta_html(html)
        self.assertIn('content:"口径"', cleaned)
        self.assertIn('var s="数据暂缺"', cleaned)
        self.assertNotIn("统计口径", cleaned)

    def test_diagnostic_panels_are_protected(self):
        html = ('<section id="deaiSection"><p>说明：本闸门只做静态判定</p></section>'
                '<section id="humanizerSection"><p>注：B6 属语义项</p></section>'
                '<p>注：这段该被删掉</p>')
        cleaned, _ = mt.strip_meta_html(html)
        self.assertIn("本闸门只做静态判定", cleaned)
        self.assertIn("B6 属语义项", cleaned)
        self.assertNotIn("这段该被删掉", cleaned)

    def test_action_list_is_protected(self):
        html = '<details id="quantActions"><li>口径：按 41 个动作执行</li></details>'
        cleaned, _ = mt.strip_meta_html(html)
        self.assertIn("41 个动作", cleaned)

    def test_idempotent(self):
        html = ('<div class="card"><div class="ev">· 采集口径：实测 1 项</div>'
                '<div class="txt">涨停 77 家</div></div>')
        once, _ = mt.strip_meta_html(html)
        twice, removed = mt.strip_meta_html(once)
        self.assertEqual(once, twice)
        self.assertEqual(removed, [])


class FactLockTest(unittest.TestCase):
    def test_cleanup_only_removes_numbers(self):
        before = '<div class="ev">采集口径：实测 2 项</div><div>涨停 77 家</div>'
        after, _ = mt.strip_meta_html(before)
        ok, why = mt.facts_preserved(before, after)
        self.assertTrue(ok, why)

    def test_corrupted_number_is_caught(self):
        ok, why = mt.facts_preserved("<div>涨停 77 家</div>", "<div>涨停 17 家</div>")
        self.assertFalse(ok)
        self.assertIn("数字", why)

    def test_invented_link_is_caught(self):
        ok, why = mt.facts_preserved("<div>原文</div>", "<div><a href='https://x.test'>新链接</a></div>")
        self.assertFalse(ok)
        self.assertIn("链接", why)


class AuditTest(unittest.TestCase):
    def test_residual_is_zero_after_strip(self):
        html = ('<div class="ev">· 五维加权 +0.00（基本面中性）</div>'
                '<div>2026-10-27，行情数据暂缺，权益仓位 50%</div>')
        cleaned, _ = mt.strip_meta_html(html)
        report = mt.audit(html)
        self.assertEqual(report["residual"], 0)
        self.assertIn("权益仓位 50%", cleaned)


class PageTest(unittest.TestCase):
    PAGE = ('<div class="insight-foot"><span>实时结果见 /api/quant</span></div>'
            '<p class="group-note">按跨源命中排序</p>'
            '<p>涨停 77 家</p>'
            '<label><input type="checkbox"><span></span></label>')

    def test_mark_page_is_idempotent(self):
        marked, hits = mt.mark_page(self.PAGE)
        self.assertGreaterEqual(hits, 2)          # 两个说明容器 + 容器内的说明叶子
        again, second = mt.mark_page(marked)
        self.assertEqual(second, 0)
        self.assertEqual(marked, again)

    def test_page_residuals_before_and_after_marking(self):
        self.assertTrue(mt.page_residuals(self.PAGE))
        marked, _ = mt.mark_page(self.PAGE)
        self.assertEqual(mt.page_residuals(marked), [])

    def test_structural_empty_tags_survive_marking(self):
        marked, _ = mt.mark_page(self.PAGE)
        self.assertIn("<span></span>", marked)      # 开关滑块不能被当成空容器删掉

    def test_real_index_html_is_clean(self):
        import os
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertEqual(mt.page_residuals(html), [])
        # 正文数据必须还在
        for fact in ("涨停", "权益仓位", "上证指数"):
            self.assertIn(fact, html)


class EmitJsTest(unittest.TestCase):
    def test_js_rules_match_python(self):
        import os
        root = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(root, "meta_text.js")
        with open(path, encoding="utf-8") as handle:
            js = handle.read()
        match = re.search(r"window\.META_TEXT_RULES = (\{.*?\});", js, re.S)
        self.assertIsNotNone(match, "meta_text.js 里没有规则对象")
        rules = json.loads(match.group(1))
        self.assertEqual(rules["sentence"], mt.SENTENCE_META_PATTERNS)
        self.assertEqual(rules["clause"], mt.CLAUSE_META_PATTERNS)

    def test_js_snapshot_is_up_to_date(self):
        import os
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "meta_text.js"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), mt.emit_js(),
                             "meta_text.js 与 meta_text.py 不同步，跑 python3 meta_text.py --emit-js meta_text.js")


if __name__ == "__main__":
    unittest.main(verbosity=2)
