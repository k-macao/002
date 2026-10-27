#!/usr/bin/env python3
"""把「说明 / 注解 / 解析 / 提醒」类文字从推送正文与页面里整段去掉。

判断单位是**句子**而不是整段：
- 整句命中 → 整句丢掉；
- 一段里既有数据又有口径尾巴（如「涨停 77 家……仅统计信号，不作为投资依据。」）→
  只丢尾巴，数据留着。

元素层面：容器里的文字被清空后，连容器一起删（空 `<div class="ev">`、空 `<tr>`、
空 `<table>` 逐层回收），避免页面上只剩下一堆空行。

不做的事
--------
- 不碰 `<style>` / `<script>` 里的内容（页面脚本要照常注入）。
- 不碰属性、URL、数字、表格数值——事实锁：URL 数、数字个数在过滤前后必须一致。
- 不碰「去 AI 味体检」「文本去 AI 味」两个诊断面板（那是用户点名要看的证据，
  里面的「没改的原因」是结论而不是说明）。

命令行::

    python3 meta_text.py --report index.html          # 只列出会被删掉的片段
    python3 meta_text.py --apply  index.html          # 就地清理
    python3 meta_text.py --check  index.html          # 有残留就退出码 1
"""
from __future__ import annotations

import json
import re
import sys

# 两档判断：整句丢 vs 只丢分句。
#
# 甲档（句子级）：口径、方法、免责、流程提示——整句都是「说明」，丢整句。
SENTENCE_META_PATTERNS = [
    # 「映射口径 / 指标口径 / 采集口径」是指南；「官媒口径 / 术语口径 / 事件研究口径」
    # 是数据标签，必须留着——所以只认这几个固定搭配，不认光秃秃的「口径」二字。
    r"(映射|指标|采集|统计|情感|检索|数据|计价|展示)口径",
    r"^口径\s*[:：]",
    r"统计信号",
    # 「北向资金已停止披露」这类制度说明：属于提醒，不是当日数据
    r"停止(实时)?披露",
    r"阈值",
    r"可复算",
    r"启发式",
    r"不是统计概率",
    r"模型情景",
    r"非预测",
    r"证据\s*[:：]\s*\d+\s*源",
    r"数据源故障",
    r"实测\s*\d+\s*项",
    r"五维加权",
    r"不展示",
    r"未展示",
    r"仅列标题",
    r"完整数据",
    r"完整\s*Markdown",
    r"见\s*reports/",
    r"实时结果见",
    r"结果见\s*`?/api",
    r"图例",
    r"供前端",
    r"不构成投资建议",
    r"免责",
    r"disclaimer",
    r"不猜数",
    r"不编数",
    r"说明\s*[:：]",
    # 「注：」要真的在句首/句读之后才算注释——「明日关注：」是标题，不能误伤
    r"(?:^|[。；，、\s（(])注\s*[:：]",
    r"注解",
    r"注析",
    r"提醒",
    r"请注意",
    r"\d{1,2}:\d{2}\s*(采集|交付|启动|归档|推送)",
    r"只借鉴",
    r"不模仿",
    r"不代表其观点",
    r"背书",
]

# 乙档（分句级）：只丢「这一句里报告缺口 / 完整度」的分句，同句里的结论与数值留下。
# 例：「2026-10-27，行情数据暂缺，五维：…，权益仓位 50%」只去掉「行情数据暂缺」。
CLAUSE_META_PATTERNS = [
    # 「采集」只在「采集口径 / 采集层 / 采集明细」这类说法里是说明；
    # 「前置数据采集」是步骤名，必须留着。
    r"采集(口径|层|明细|完成|时间|说明|任务|脚本)",
    # 「仅供参考 / 不作为投资依据」常挂在数据句尾巴上，只掐分句，数据留着。
    r"仅供参考",
    r"不作为投资依据",
    r"^\s*分析研究\s*$",
    r"数据暂缺",
    r"未取得",
    r"数据完整度",
    r"暂无数据",
    r"代理\s*\d+\s*项",
    r"实测\s*\d+\s*项",
    r"降级",
]

SENTENCE_META_RX = re.compile("|".join(f"(?:{pattern})" for pattern in SENTENCE_META_PATTERNS))
CLAUSE_META_RX = re.compile("|".join(f"(?:{pattern})" for pattern in CLAUSE_META_PATTERNS))
META_RX = re.compile(f"(?:{SENTENCE_META_RX.pattern})|(?:{CLAUSE_META_RX.pattern})")

# 整条方法注记的开头（「口径：LSTM…」「分解结果为统计口径…」「框架：…」）：
# 这类节点整条都是方法说明，句中夹着的数据也是方法参数，一并去掉。
LEADING_NOTE_RX = re.compile(
    r"^\s*[·•\-—]?\s*(口径\s*[:：]|映射口径|指标口径|采集口径|情感口径|统计口径|数据口径|"
    r"检索口径|分解结果为|框架\s*[:：])")

# 过滤后只剩项目符号 / 标点的空壳
BULLET_ONLY_RX = re.compile(r"^[\s·•\-—，,、；;。：:]*$")

# 句子切分：句末标点保留在前一段，方便原样拼回。
SENTENCE_RX = re.compile(r"(?<=[。；！？])")
# 分句切分：逗号 / 顿号处断开，用来掐掉「行情数据暂缺」这类状态分句。
CLAUSE_RX = re.compile(r"(?<=[，,　])")
# ev 行内的片段分隔（「信号 4:1 · 线索列表不展示」）。
FRAGMENT_RX = re.compile(r"\s*·\s*")

# 内容为空的容器：一轮一轮往上清。
EMPTY_BLOCK_RX = re.compile(
    r"<(div|span|small|p|li|td|th|tr|table|summary|details|em|b|strong)\b[^>]*>"
    r"(?:\s|&nbsp;|<br\s*/?>)*</\1>",
    re.I,
)
EMPTY_ROW_RX = re.compile(r"<tr\b[^>]*>(?:\s*<td\b[^>]*>(?:\s|&nbsp;)*</td>)+\s*</tr>", re.I)
# 只剩编号 / 标签这一个单元格的行：内容被清掉后留着「α」这样的空壳行，整行去掉。
ORPHAN_ROW_RX = re.compile(
    r"<tr\b[^>]*>\s*<td\b[^>]*>[^<>]{0,6}</td>\s*(?:<td\b[^>]*>(?:\s|&nbsp;)*</td>)?\s*</tr>", re.I)

# 页脚「说明 / 免责」块：按设计就是方法口径与免责声明，整块去掉。
FOOTER_RX = re.compile(r'<div class="ftr"[^>]*>.*?</div>', re.S | re.I)

# 受保护区域：脚本与样式原样保留。
PROTECT_RX = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>)", re.S | re.I)

# 这些区块不做过滤：
# - 去 AI 味体检 / 文本去 AI 味：用户点名要看的证据（改了什么、为什么没改）；
# - 41 个原子动作：Agent 执行清单，删掉「兜底 / 说明」列会让表格失去可执行性。
PROTECTED_SECTIONS = ("deaiSection", "humanizerSection")
PROTECTED_IDS = ("quantActions",)
# 整体保护：<section id=…> 与 <details id=…> 两种容器都按 id 整段保留。
PROTECTED_BLOCK_RX = re.compile(
    r'<(section|details)\b[^>]*\bid="(?:deaiSection|humanizerSection|quantActions)"[^>]*>.*?</\1>',
    re.S | re.I)


def is_meta(text: str) -> bool:
    """整句是否属于「说明 / 注解 / 解析 / 提醒」。"""
    return bool(text.strip()) and bool(META_RX.search(text))


def _drop_clauses(sentence: str) -> str:
    """掐掉句子里报告缺口 / 完整度的分句，保留结论与数值。"""
    if not CLAUSE_META_RX.search(sentence):
        return sentence
    kept = [part for part in CLAUSE_RX.split(sentence)
            if part.strip() and not CLAUSE_META_RX.search(part)]
    if not kept:
        return ""
    out = "".join(kept).strip()
    # 掐完可能留下悬空的标点（「事件风险（中）；」），收一下。
    out = re.sub(r"^[，,、；;。]+", "", out)
    out = re.sub(r"[，,、；;]+$", "", out)
    if not out:
        return ""
    return out + "。" if sentence.rstrip().endswith("。") and not out.endswith("。") else out


def strip_text(text: str) -> str:
    """按句子过滤：命中说明模式的句子整句丢；只报缺口的分句单独丢。"""
    if not text or not META_RX.search(text):
        return text
    kept = []
    for sentence in SENTENCE_RX.split(text):
        if not sentence.strip():
            continue
        if SENTENCE_META_RX.search(sentence):
            continue          # 甲档：整句都是说明
        kept.append(_drop_clauses(sentence))
    out = "".join(kept)
    return re.sub(r"[，,、；;]+$", "", out)


def strip_fragments(text: str) -> str:
    """「·」条目的片段过滤：留下数据片段，丢掉口径尾巴。

    只对**以「·」开头**的条目行生效——正文里的「·」是数值分隔
    （「偏多·净+3·12源共振」），按片段切会把数据劈开。
    """
    text = text or ""
    # 只有「带空格的 ·」（「信号 4:1 · 线索列表不展示」）和以 · 开头的条目才按片段切；
    # 「偏多·净+3·12源共振」这种不带空格的 · 是数值分隔，切开就把数据劈了。
    if not (text.lstrip().startswith("·") or FRAGMENT_RX.search(text)):
        return strip_text(text)
    parts = FRAGMENT_RX.split(text)
    kept = [part for part in parts if part.strip() and not SENTENCE_META_RX.search(part)]
    if not kept:
        return ""
    return strip_text("·".join(kept))


def _mask(text: str) -> tuple[str, list[str]]:
    """把受保护区域换成占位符，返回 (骨架, 原文列表)。"""
    saved: list[str] = []

    def _keep(match: re.Match) -> str:
        saved.append(match.group(0))
        return f"\x00META_KEEP_{len(saved) - 1}\x00"

    return PROTECT_RX.sub(_keep, text), saved


def _unmask(text: str, saved: list[str]) -> str:
    for index, chunk in enumerate(saved):
        text = text.replace(f"\x00META_KEEP_{index}\x00", chunk)
    return text


def _protect_sections(html: str) -> tuple[str, list[str]]:
    """保护诊断面板 / 执行清单整段：从容器开标签到对应闭标签。"""
    saved: list[str] = []

    def _keep(match: re.Match) -> str:
        saved.append(match.group(0))
        return f"\x00META_KEEP_S{len(saved) - 1}\x00"

    return PROTECTED_BLOCK_RX.sub(_keep, html), saved


def _restore_sections(html: str, saved: list[str]) -> str:
    for index, chunk in enumerate(saved):
        html = html.replace(f"\x00META_KEEP_S{index}\x00", chunk)
    return html


def _filter_text_nodes(html: str) -> tuple[str, list[str]]:
    """逐文本节点过滤；返回 (新 html, 删掉的片段)。"""
    removed: list[str] = []

    def _sub(match: re.Match) -> str:
        raw = match.group(2)
        if not META_RX.search(raw):
            return match.group(0)
        if LEADING_NOTE_RX.match(raw):
            removed.append(raw.strip())
            return f"{match.group(1)}{match.group(3)}"
        cleaned = strip_fragments(raw)
        if cleaned.strip() == raw.strip():
            return match.group(0)
        for piece in re.split(r"(?<=[。；！？])", raw):
            if not piece.strip():
                continue
            after = strip_fragments(piece)
            if after.strip() != piece.strip() and piece.strip() not in removed:
                removed.append(piece.strip())
        return f"{match.group(1)}{cleaned}{match.group(3)}"

    pattern = re.compile(r"(>)([^<>]+)(<)")
    return pattern.sub(_sub, html), removed


def _drop_empty(html: str) -> str:
    for _ in range(4):
        before = html
        html = EMPTY_ROW_RX.sub("", html)
        html = ORPHAN_ROW_RX.sub("", html)
        html = EMPTY_BLOCK_RX.sub("", html)
        if html == before:
            break
    return html


def strip_meta_html(html: str) -> tuple[str, list[str]]:
    """去掉 HTML 里的说明 / 注解 / 解析 / 提醒文字与清空的容器。"""
    html, sections = _protect_sections(html)
    html, saved = _mask(html)
    footers = FOOTER_RX.findall(html)
    html = FOOTER_RX.sub("", html)
    html, removed = _filter_text_nodes(html)
    removed = ["〔页脚说明〕" + re.sub(r"<[^>]+>", "", chunk)[:60] for chunk in footers] + removed
    html = _drop_empty(html)
    html = _unmask(html, saved)
    return _restore_sections(html, sections), removed


def audit(html: str) -> dict:
    """体检：还剩多少说明类文字、会被删掉多少片段。"""
    cleaned, removed = strip_meta_html(html)
    residual = [line.strip() for line in re.findall(r">([^<>]+)<", cleaned)
                if META_RX.search(line)]
    return {
        "removed": len(removed),
        "samples": removed[:20],
        "residual": len(residual),
        "residual_samples": residual[:10],
    }


def fact_signature(html: str) -> tuple:
    """事实锁基线：数字与 URL 的多重集。

    说明文字里本来就带数字（「数据完整度 20%」「15:30 采集」），所以不能要求前后相等；
    要求的是**只减不增**：留下的每个数字与 URL 都必须在原文里出现过，且数值本身不被改写。
    """
    numbers = sorted(re.findall(r"\d+(?:\.\d+)?", html))
    urls = sorted(set(re.findall(r"https?://[^\s\"'<>]+", html)))
    return (numbers, urls)


def facts_preserved(before: str, after: str) -> tuple[bool, str]:
    """事实锁：清理只能删说明，不能改数、不能凭空造数。"""
    import collections

    old_numbers, old_urls = fact_signature(before)
    new_numbers, new_urls = fact_signature(after)
    extra = collections.Counter(new_numbers) - collections.Counter(old_numbers)
    if extra:
        return False, f"出现了原文没有的数字：{list(extra)[:5]}"
    stray = [url for url in new_urls if url not in old_urls]
    if stray:
        return False, f"出现了原文没有的链接：{stray[:3]}"
    return True, ""


# 页面（index.html）里整块都是「说明 / 注解 / 提醒」的容器：按 class / id 标 data-meta，
# 由 styles.css 的 [data-meta]{display:none} 一句话隐藏。逐条 write 而不是全页正则清洗——
# 页面里有大量结构性空标签（开关滑块、分隔线）与行内 <code>，按文本节点切会切坏结构。
PAGE_META_CLASSES = ("insight-foot", "quant-notice", "bigv-notice", "group-note")
# 只标「整块都是说明」的 id：quantMeta 里还混着五维 / 仓位这类数据，不能整块藏。
PAGE_META_IDS = ("quantProvenance", "quantPill", "heatmapFoot", "policyDeepFoot",
                 "bigVFoot", "deaiFoot", "hzFoot")

# 叶子级：整段文字都属于说明的行内标签（「数据完整度 100%」「五维加权 +0.29（…）」）。
PAGE_META_LEAF_RX = re.compile(r"<(span|small|b|em|i)\b([^>]*)>([^<>]+)</\1>", re.I)

MARK_RX = re.compile(r"<(\w+)([^>]*\bclass=\"[^\"]*\b(?:%s)\b[^\"]*\"[^>]*)>"
                     % "|".join(PAGE_META_CLASSES))


def mark_page(html: str) -> tuple[str, int]:
    """给页面里的说明类容器加 data-meta 标记；已标过的不重复加。"""
    count = 0

    def _mark(match: re.Match) -> str:
        nonlocal count
        attrs = match.group(2)
        if "data-meta" in attrs:
            return match.group(0)
        count += 1
        return f"<{match.group(1)}{attrs} data-meta=\"1\">"

    html = MARK_RX.sub(_mark, html)

    def _mark_leaf(match: re.Match) -> str:
        nonlocal count
        attrs, text = match.group(2), match.group(3)
        if "data-meta" in attrs or not META_RX.search(text):
            return match.group(0)
        count += 1
        return f"<{match.group(1)}{attrs} data-meta=\"1\">{text}</{match.group(1)}>"

    html = PAGE_META_LEAF_RX.sub(_mark_leaf, html)
    for element_id in PAGE_META_IDS:
        pattern = re.compile(rf'<([a-z]+)([^>]*\bid="{element_id}"[^>]*)>', re.I)
        if f'id="{element_id}"' in html and f'id="{element_id}" data-meta' not in html \
                and f'data-meta="1" id="{element_id}"' not in html:
            new_html, hits = pattern.subn(
                lambda m: (m.group(0) if "data-meta" in m.group(2)
                           else f'<{m.group(1)}{m.group(2)} data-meta="1">'), html, count=1)
            if hits:
                html = new_html
                count += 1
    return html, count


def page_residuals(html: str) -> list[str]:
    """页面体检：说明类文字必须落在 data-meta 容器或受保护区块里。"""
    body, _ = _protect_sections(html)
    body, saved = _mask(body)
    # 去掉已标记的容器（连同内容）
    body = re.sub(r'<([a-z]+)\b[^>]*data-meta="1"[^>]*>.*?</\1>', "", body, flags=re.S | re.I)
    # 这些容器由 meta_text.js 在运行时清理（含 app.js 注入的内容），静态检查时算已隐藏。
    for tag in ("small", "summary", "li", "p"):
        body = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", body, flags=re.S | re.I)
    body = _unmask(body, saved)
    return [text.strip() for text in re.findall(r">([^<>]+)<", body)
            if text.strip() and META_RX.search(text)]


def emit_js() -> str:
    """导出前端规则文件：与 Python 端共用同一份模式串，避免两边改漂。"""
    return (
        "// 由 meta_text.py --emit-js 生成，勿手改（改规则请改 meta_text.py）。\n"
        "window.META_TEXT_RULES = " + json.dumps({
            "sentence": SENTENCE_META_PATTERNS,
            "clause": CLAUSE_META_PATTERNS,
            "leading": LEADING_NOTE_RX.pattern,
            "bullet": BULLET_ONLY_RX.pattern,
        }, ensure_ascii=False, indent=2) + ";\n" + _JS_SWEEP)


_JS_SWEEP = r"""
/* 页面侧的同一套规则：说明 / 注解 / 解析 / 提醒类文字不展示。
   静态部分已在 index.html 上标了 data-meta（styles.css 直接 display:none），
   这里负责把 app.js 运行时注入的说明文字也清掉。

   刻意保守：只处理「整块都是说明」的容器（.insight-foot / .quant-notice /
   .bigv-notice / .group-note / .note / .ev / .ftr / small / summary 等），
   不碰表格单元格与结构性空标签（开关滑块、分隔线），避免把版式拆坏。 */
(function () {
  var R = window.META_TEXT_RULES;
  if (!R) return;
  var SENTENCE = new RegExp(R.sentence.join('|'));
  var CLAUSE = new RegExp(R.clause.join('|'));
  var ANY = new RegExp('(?:' + SENTENCE.source + ')|(?:' + CLAUSE.source + ')');
  var SPLIT = /(?<=[。；！？])/;
  var CLS = /(?<=[，,　])/;
  var PROTECT = { deaiSection: 1, humanizerSection: 1, quantActions: 1 };
  var META_BLOCKS = '[data-meta], .insight-foot, .quant-notice, .bigv-notice, .group-note,' +
                    ' .note, .foot, .hint, .ev, .ftr, .sub, .quant-dim-note, small, summary,' +
                    ' li, p';

  function dropClauses(text) {
    if (!CLAUSE.test(text)) return text;
    var kept = text.split(CLS).filter(function (part) {
      return part.trim() && !CLAUSE.test(part);
    });
    var out = kept.join('').trim().replace(/^[，,、；;。]+/, '').replace(/[，,、；;]+$/, '');
    if (!out) return '';
    return /[。]\s*$/.test(text) && !/[。]$/.test(out) ? out + '。' : out;
  }

  function stripText(text) {
    if (!text || !ANY.test(text)) return text;
    var pieces = text.split(SPLIT), kept = [];
    for (var i = 0; i < pieces.length; i++) {
      var piece = pieces[i];
      if (!piece.trim()) continue;
      if (SENTENCE.test(piece)) continue;
      kept.push(dropClauses(piece));
    }
    return kept.join('').replace(/[，,、；;]+$/, '');
  }

  function inProtected(node) {
    for (var el = node.parentNode; el && el.nodeType === 1; el = el.parentNode) {
      if (PROTECT[el.id]) return true;
    }
    return false;
  }

  function cleanBlock(el) {
    if (PROTECT[el.id] || el.getAttribute('data-meta-done')) return false;
    el.setAttribute('data-meta-done', '1');
    if (el.hasAttribute('data-meta')) {          /* 整块说明：直接移出文档流 */
      el.style.display = 'none';
      return true;
    }
    var text = el.textContent;
    if (!text || !ANY.test(text)) return false;
    var cleaned = stripText(text);
    if (cleaned.trim() === text.trim()) return false;
    if (!cleaned.trim()) {                       /* 清空就整块收掉 */
      el.style.display = 'none';
      el.setAttribute('data-meta', '1');
      return true;
    }
    /* 只在「没有行内结构」时改写文字，避免把 <code> / <b> 拆散。 */
    if (!el.querySelector('code,b,i,em,strong,a')) el.textContent = cleaned;
    return true;
  }

  function sweep(root) {
    if (!root || !root.querySelectorAll) return 0;
    var changed = 0;
    Array.prototype.forEach.call(root.querySelectorAll(META_BLOCKS), function (el) {
      if (inProtected(el)) return;
      if (cleanBlock(el)) changed++;
    });
    return changed;
  }

  function boot() {
    sweep(document.body);
    var pending = false;
    new MutationObserver(function (records) {
      var relevant = records.some(function (r) {
        return r.addedNodes && r.addedNodes.length;
      });
      if (!relevant || pending) return;
      pending = true;
      requestAnimationFrame(function () {
        try { sweep(document.body); } finally { pending = false; }
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
"""


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="隐藏说明 / 注解 / 解析 / 提醒类文字")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--report", action="store_true", help="列出会被删掉的片段")
    group.add_argument("--apply", action="store_true", help="就地清理文件")
    group.add_argument("--check", action="store_true", help="有残留则退出码 1")
    group.add_argument("--mark-page", action="store_true", help="给页面里的说明容器加 data-meta")
    group.add_argument("--check-page", action="store_true", help="检查页面说明文字是否都藏在 data-meta 里")
    parser.add_argument("files", nargs="*")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--emit-js", metavar="PATH",
                        help="把规则导出成前端可用的 meta_text.js（与 Python 同一套规则）")
    args = parser.parse_args(argv)

    if not (args.report or args.apply or args.check or args.mark_page
            or args.check_page or args.emit_js):
        parser.error("需要指定 --report / --apply / --check / --mark-page / --check-page / --emit-js")

    if args.emit_js:
        open(args.emit_js, "w", encoding="utf-8").write(emit_js())
        print(f"{args.emit_js}: 已导出 {len(SENTENCE_META_PATTERNS)} + "
              f"{len(CLAUSE_META_PATTERNS)} 条规则")
        if not args.files:
            return 0

    if args.mark_page or args.check_page:
        status = 0
        for path in args.files:
            raw = open(path, encoding="utf-8").read()
            if args.mark_page:
                marked, hits = mark_page(raw)
                open(path, "w", encoding="utf-8").write(marked)
                print(f"{path}: 标记 {hits} 个说明容器")
                raw = marked
            residual = page_residuals(raw)
            if residual:
                print(f"{path}: 还有 {len(residual)} 处说明文字露在外面")
                for sample in residual[:8]:
                    print(f"  - {sample[:80]}")
                status = 1
            else:
                print(f"{path}: 说明文字已全部隐藏")
        return status

    status = 0
    for path in args.files:
        raw = open(path, encoding="utf-8").read()
        report = audit(raw)
        if args.apply:
            cleaned, removed = strip_meta_html(raw)
            ok, why = facts_preserved(raw, cleaned)
            if not ok:
                print(f"{path}: 事实锁不通过（{why}），已放弃写入", file=sys.stderr)
                status = 2
                continue
            open(path, "w", encoding="utf-8").write(cleaned)
            print(f"{path}: 删掉 {len(removed)} 处说明文字，残留 {report['residual']} 处")
        elif args.check:
            if report["residual"]:
                print(f"{path}: 仍有 {report['residual']} 处说明文字")
                status = 1
            else:
                print(f"{path}: 无说明类文字")
        else:
            if args.json:
                print(json.dumps({"file": path, **report}, ensure_ascii=False, indent=2))
            else:
                print(f"{path}: 会删 {report['removed']} 处")
                for sample in report["samples"]:
                    print(f"  - {sample}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
