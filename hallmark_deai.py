#!/usr/bin/env python3
"""「去 AI 味」推送前闸门 —— Hallmark 反 AI-slop 设计规则的静态子集移植。

规则来源：``Nutlope/hallmark``（Together AI 出品，MIT 许可）的
``skills/hallmark/SKILL.md`` 的 The Floor 与 ``references/gates.md`` §4 的 gate 表。
Hallmark 原本是给 Claude Code / Cursor / Codex 用的设计 skill，带一个 Node 版静态
linter（``sloplint.mjs``）。本模块把其中**可用正则静态判定**的 M 类 gate 移植成
零第三方依赖的 Python，用于本项目两类 HTML：

* ``surface="push"``：``sources.build_html()`` 生成的**推送 HTML**（内联 ``<style>``、
  兼容邮件客户端的竖版长图文）。
* ``surface="page"``：``index.html`` + ``styles.css`` 组成的**管理页**（外部 CSS、
  有 ``:root`` token 块）。

三层判定沿用 Hallmark：``Floor``（硬底线，不可 waive）/ ``Reflex``（默认机器习惯，
可凭理由 waive）/ ``Finish``（工艺细节，只报告）。体检报告同时给出**体检前**与
**改写后**两份分数，以及每条 gate 的命中数、样例与修法。

事实保护（对应 Hallmark「keeps claims tied to evidence」与 humanizer「锁住数字、
人物、时间、术语、引用」）：所有自动改写都只作用于**装饰层 / UI chrome**，绝不改
新闻正文、数字、日期、人名、引号内文字与链接。``polish()`` 结束后会比对可见文本
的数字串、URL、``<a>`` 数量的签名，一旦漂移就整体回滚该次改写。

开关（环境变量，风格与 ``sensitive.py`` 一致）：

* ``DEAI_MODE=polish``（默认）/ ``audit``（只体检不改写）/ ``off``（跳过）
* ``SKIP_DEAI_CHECK=1`` 跳过闸门（测试 / 应急）
* ``DEAI_FORCE=pass|block`` 强制放行 / 强制拦截（测试 / 应急）
* ``DEAI_STRICT=1`` 改写后仍有未修复的 Floor 命中就拦截推送

命令行自检::

    python3 hallmark_deai.py report brief-output.html
    python3 hallmark_deai.py report index.html --css styles.css --surface page
    python3 hallmark_deai.py polish index.html --css styles.css --write
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

ENGINE = "hallmark-deai"
VERSION = "1.0"

SOURCE = {
    "project": "Nutlope/hallmark",
    "url": "https://github.com/Nutlope/hallmark",
    "license": "MIT",
    "reference": "skills/hallmark/SKILL.md（The Floor）+ references/gates.md（§4 The gates）",
    "adaptation": (
        "本项目只移植可用正则静态判定的 M 类 gate；Hallmark 的 render / judge 类 gate"
        "（首屏折线、配色面积、字距视错觉等）需要真实浏览器与人眼判定，本模块不假装有结论，"
        "而是在报告的 limits 字段里如实列出。"
    ),
}

# 供外部 LLM 复现同一套「去 AI 味」逻辑；本仓库默认不调用任何外部模型。
AI_PROMPT = """你是一名中文财经简报的排版与文案编辑。请按 Hallmark（Nutlope/hallmark, MIT）
的反 AI-slop 规则做「去 AI 味」改写，只改装饰层，不动事实：
1) 删除标题上方只报出下一节名字的眉标（kicker/eyebrow）、装饰性符号图标（✦ 等）、
   装饰状态圆点；保留功能性导航标签与真实状态。
2) 把小标签的全大写、字距放大（letter-spacing > .03em）、单一彩色词、标题里斜体的
   单个词，改成整句同色同字重的常规排版。
3) 把 `A · B · C` 这类中点串联的元信息改成顿号、分号或换行；把 glue 在链接文字尾部
   的箭头去掉；中文破折号只在确实泛滥时收敛。
4) 删掉机械三段式、空洞升华结尾（未来可期 / 让我们拭目以待）、宣传腔套话
   （赋能 / 打造闭环 / 一站式）与「不是…而是…」的重复排比；改成平实陈述。
5) 不改写任何数字、百分比、日期、人名、机构名、引用原话与 URL；不新增事实、
   不新增引文、不新增指标。缺信息就直说缺，不要补。
6) 输出改写后的完整 HTML/CSS，并附一份逐条说明：改了哪条 gate、改了几处、为何不改
   其余命中（waiver 理由）。
"""

BOUNDARY = (
    "只清理可静态判定的装饰痕迹与模板标点；不改新闻事实、数字、日期、人名、引用与链接；"
    "不模仿任何真人的语气，也不声称能通过 AIGC 检测器。分数是启发式体检分，不是概率。"
)

LIMITS = (
    "Hallmark 的 R/J 类 gate（首屏 1280×800 折线、每屏 80 词、配色面积、标题拥挤度、"
    "图片真实性、hover 一致性等）需要浏览器渲染与人眼判断；本静态闸门不检测它们。"
)

# 这些是本项目有意保留的机器化元素，按 Hallmark 的 waiver 机制在报告里显式登记理由。
WAIVERS = [
    {"gate": "30a", "item": "🐙 品牌字标",
     "reason": "章鱼是产品字标（wordmark），Hallmark 的 30a 只禁「emoji 当图标」，不禁品牌字标"},
    {"gate": "30a", "item": "＋ 全角加号 / ↻ 刷新 / ↗ 展开 / ☼ ◐ ▶ 功能图标",
     "reason": "这些字符在本项目承担真实功能：＋ 分隔两只 ETF 名称，其余是按钮图标。删掉会损坏事实或控件，属于 Hallmark 的「functional icon」豁免，不进自动改写"},
    {"gate": "66", "item": "推送页顶部点阵矩阵与分段进度条",
     "reason": "邮头视觉签名，非伪装成进度的假指标；数量固定 24 点，不承载数据"},
    {"gate": "7a", "item": "推送 HTML 内联颜色字面量",
     "reason": "邮件客户端对 CSS 自定义属性支持不一致，内联字面色是兼容性取舍，不能 token 化"},
    {"gate": "63", "item": "因果链箭头（如 界定棋盘 → 激励 → 约束）",
     "reason": "Hallmark 63 针对 glue 在链接/按钮文字尾部的箭头；逻辑链里的箭头承载方向信息，保留"},
    {"gate": "67", "item": "章鱼 AI·全景分析 名称中的无空格间隔号",
     "reason": "产品名称里的间隔号是专有名词的一部分，不是元信息串联中点"},
]

# 邮件模板的兼容性取舍：这些 gate 对 surface="push" 不做判定（只在 waivers 里登记）。
WAIVED_BY_SURFACE = {"push": {"7a"}, "page": set()}

# ── gate 目录：沿用 Hallmark gates.md 的编号，便于对照上游 ────────────────────────
GATES = [
    {"id": "2a", "tier": "Floor", "name": "紫蓝渐变",
     "tell": "紫→蓝 / 紫→粉 / 深色青色渐变或 background-clip:text 渐变文字，是最典型的 AI 生成配色。",
     "fix": "换成单一锚点色 + 实心墨色；渐变文字改字重或展示字体。", "auto": True},
    {"id": "4", "tier": "Floor", "name": "卡片套卡片",
     "tell": "一层容器里再嵌一层同质感圆角卡片，制造廉价层次。",
     "fix": "只保留一层容器，用留白和细线分层。", "auto": False},
    {"id": "7a", "tier": "Floor", "name": "纯黑白字面色",
     "tell": "token 块之外出现 #000 / #fff / #ffffff / #000000。",
     "fix": "向锚点色轻染；有 token 时改用 var(--paper) / var(--ink)。", "auto": True},
    {"id": "10a", "tier": "Floor", "name": "transition: all",
     "tell": "transition: all 与动画 width/height/top/left，性能差且是模板残留。",
     "fix": "列出具体属性；动画只用 transform / opacity。", "auto": True},
    {"id": "19", "tier": "Floor", "name": "文案套话",
     "tell": "em/en 破折号泛滥、宣传腔 buzzword、空洞升华结尾、机械三段式与「不是…而是…」排比。",
     "fix": "改成平实陈述；保留全部事实与引用。", "auto": False},
    {"id": "20", "tier": "Floor", "name": "缺 Hallmark 印记",
     "tell": "CSS 首行没有 Hallmark 印记与批评行，无法追溯本轮设计的取舍。",
     "fix": "在 CSS 最前面写印记注释、批评行与 waive 列表。", "auto": True},
    {"id": "26a", "tier": "Floor", "name": "缺 focus-visible",
     "tell": "有交互元素但 CSS 里没有 :focus-visible 焦点环。",
     "fix": "补一条 instant 出现的 3:1 焦点环。", "auto": True},
    {"id": "27", "tier": "Floor", "name": "缺 reduced-motion",
     "tell": "有 @keyframes / animation 却没有 prefers-reduced-motion: reduce 兜底。",
     "fix": "补一段 reduce 媒体查询关闭动画。", "auto": True},
    {"id": "30a", "tier": "Floor", "name": "装饰符号当图标",
     "tell": "✦ ★ ◆ ● ▶ 等装饰符号/emoji 被当作图标或项目符号前缀。",
     "fix": "换成排版本身（字重、缩进、细线），或统一 SVG 图标库。", "auto": True},
    {"id": "34", "tier": "Floor", "name": "overflow-x: hidden",
     "tell": "body 用 overflow-x:hidden 或出现 100vw，窄屏容易横向溢出。",
     "fix": "html/body 都用 overflow-x:clip，宽度用 100%。", "auto": True},
    {"id": "38i", "tier": "Floor", "name": "标题里单个斜体词",
     "tell": "一个非斜体标题里夹一个斜体 / 高亮 / 异色词，是仅次于眉标的机器痕迹。",
     "fix": "整条标题同字重同色；或换行另起一行。", "auto": True},
    {"id": "54", "tier": "Floor", "name": "眉标 eyebrow / kicker",
     "tell": "标题上方那行小字只报出下一节的名字，删掉它不丢任何信息。",
     "fix": "让小标题本身承担命名，或换成带事实的说明句。", "auto": True},
    {"id": "63", "tier": "Floor", "name": "模板标点",
     "tell": "A · B · C 式中点串联元信息、链接尾部箭头、装饰状态圆点。",
     "fix": "改用顿号 / 分号 / 换行 / 细线；箭头换成动词标签。", "auto": True},
    {"id": "67", "tier": "Floor", "name": "小标签追踪大写",
     "tell": "15px 以下的标签用全大写、small-caps 或 letter-spacing > .03em。",
     "fix": "改正文体、小一号、句首大写或句格、字距归零。", "auto": True},
    {"id": "6a", "tier": "Floor", "name": "vh 单位",
     "tell": "首屏 / 容器高度用 vh，在移动端浏览器工具栏下会跳动。",
     "fix": "改用 dvh 或 svh。", "auto": True},
    {"id": "3", "tier": "Reflex", "name": "图标卡片阵列",
     "tell": "三等宽列、图标在圆角块上方、卡片左侧粗色条。",
     "fix": "错开宽高、图标行内化、细线或无边框。", "auto": False},
    {"id": "9", "tier": "Reflex", "name": "等距分节",
     "tell": "每一节都用同一种间距和同一条分隔方式。",
     "fix": "按 surface ladder 换用留白、细线、浅色底等不同层级。", "auto": False},
    {"id": "69", "tier": "Reflex", "name": "事实复读",
     "tell": "同一个数字 / 日期在四节以上反复出现，或节末重述标题。",
     "fix": "一个事实只说一次，放在最能起作用的位置。", "auto": False},
    {"id": "F23", "tier": "Finish", "name": "破折号密度",
     "tell": "em dash 使用过密（Hallmark 2.5 的 F23）。",
     "fix": "能用逗号、冒号或断句替代就用；保留确有必要的插入语。", "auto": False},
]

GATE_BY_ID = {g["id"]: g for g in GATES}
_TIER_WEIGHT = {"Floor": 6.0, "Reflex": 3.0, "Finish": 1.0}

# ── 探测器 ─────────────────────────────────────────────────────────────────
_RE_GRADIENT = re.compile(
    r"(?:linear|radial|conic)-gradient\([^)]*?(?:purple|violet|indigo|#6[0-9a-f]{5}|#8[0-9a-f]{5}|"
    r"rgba?\(\s*(?:99|102|139|79|70)\b)[^)]*\)|background-clip:\s*text", re.I)
_RE_PURE_COLOR = re.compile(r"#(?:fff(?:fff)?|000(?:000)?)\b", re.I)
_RE_TRANSITION_ALL = re.compile(r"transition\s*:\s*all\b|transition\s*:\s*[^;}]*\b(?:width|height|top|left)\b", re.I)
_RE_KEYFRAMES = re.compile(r"@keyframes\b|\banimation\s*:", re.I)
_RE_REDUCED = re.compile(r"prefers-reduced-motion", re.I)
_RE_FOCUS = re.compile(r":focus-visible", re.I)
_RE_INTERACTIVE = re.compile(r"<(?:button|a|input|select|textarea)\b", re.I)
_RE_OVERFLOW_HIDDEN = re.compile(r"overflow-x\s*:\s*hidden|100vw\b", re.I)
_RE_ITALIC_IN_HEAD = re.compile(r"<h[1-4]\b[^>]*>(?:(?!</h[1-4]>).)*?<(?:em|i|mark)\b", re.I | re.S)


# class 属性允许单引号 / 双引号 / 无引号，避免漏判内联模板与 JS 生成的标签；
# 但必须真的含有目标类名，不能退化成「匹配所有 span」。
def _cls_containing(names: str) -> str:
    alts = r"(?:%s)" % names
    return (r"class\s*=\s*(?:\"[^\"]*\b" + alts + r"\b[^\"]*\""
            r"|'[^']*\b" + alts + r"\b[^']*'"
            r"|[^\s>]*\b" + alts + r"\b[^\s>]*)")


_RE_EYEBROW = re.compile(
    r"<(?:span|div|p)\b[^>]*" + _cls_containing(r"kicker|eyebrow") + r"[^>]*>", re.I)
_RE_TRACKED = re.compile(r"text-transform\s*:\s*uppercase|letter-spacing\s*:\s*\.?(?:0?[4-9]|1\d|[2-9]\d|0\.[1-9]\d*)em",
                         re.I)
_RE_VH = re.compile(r"\b\d+(?:\.\d+)?vh\b", re.I)
_RE_STAMP = re.compile(r"Hallmark\s*·", re.I)
_RE_CARD_NEST = re.compile(r"<div\b[^>]*class=\"[^\"]*\bcard\b[^\"]*\".*?<div\b[^>]*class=\"[^\"]*\bcard\b", re.I | re.S)
_RE_ICON_TILE = re.compile(r"class=\"[^\"]*\b(?:icon-tile|icon-box|feature-tile)\b")
_RE_DASH = re.compile(r"[—–]")
_RE_BUZZWORD = re.compile(r"赋能|打造闭环|一站式|全方位赋能|深度赋能|未来可期|让我们拭目以待|"
                          r"值得关注的是|不难看出|总而言之|综上所述|seamless|robust|empower", re.I)
_RE_REBUTTAL = re.compile(r"不是[^，。；]{1,24}[，,][^。；]{0,8}而是")
# 只匹配「纯装饰」符号。＋ / ↻ / ↗ / ☼ / ◐ / ▶ 在本项目是功能字符（ETF 分隔符、
# 按钮图标），按 WAIVERS 保留，绝不进入 30a 的删除集合——事实保护优先于去 AI 味。
_RE_DECOR_GLYPH = re.compile(r"[✦✧★☆◆◇]")
_RE_STATUS_DOT = re.compile(
    r"<span\b[^>]*" + _cls_containing(r"live-dot|status-dot|pixel-dot") + r"[^>]*>\s*</span>", re.I)
_RE_META_DOTS = re.compile(r"·|　·　")
_RE_ARROW_GLUED = re.compile(r"[→➜»]\s*</(?:a|button)>")
_RE_LABEL_SPAN = re.compile(
    r"(<span\b[^>]*" + _cls_containing(r"sub|tag|tag-w|tag-d|ai-pill|meta-tag|kicker|eyebrow")
    + r"[^>]*>)([^<]*)(</span>)", re.I)
_RE_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*%?")
_RE_URL = re.compile(r"https?://[^\s\"'<>]+")
_RE_ANCHOR = re.compile(r"<a\b", re.I)
_RE_STYLE_BLOCK = re.compile(r"<style\b[^>]*>.*?</style>|<script\b[^>]*>.*?</script>", re.I | re.S)


def _visible_text(markup: str) -> str:
    """去掉 style/script 后取 >…< 之间的可见文本，用于事实签名比对。"""
    stripped = _RE_STYLE_BLOCK.sub("", markup)
    return "".join(m.group(1) for m in re.finditer(r">([^<>]+)<", stripped))


def _text_segments(markup: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r">([^<>]+)<", _RE_STYLE_BLOCK.sub("", markup))]


def _detect(gid: str, html: str, css: str, surface: str) -> tuple[int, list[str]]:
    blob = html + "\n" + css
    segs = _text_segments(html)
    joined = "".join(segs)
    if gid == "2a":
        hits = _RE_GRADIENT.findall(blob)
    elif gid == "4":
        hits = _RE_CARD_NEST.findall(html)
    elif gid == "7a":
        outside = re.sub(r":root\s*\{[^}]*\}", "", css, flags=re.I | re.S)
        # <meta name="theme-color"> 是浏览器 UI 配色声明，不是页面设计色；不计入 7a。
        html_colors = re.sub(r"<meta\b[^>]*>", "", html, flags=re.I)
        hits = _RE_PURE_COLOR.findall(outside + "\n" + html_colors) if surface == "page" else _RE_PURE_COLOR.findall(outside)
    elif gid == "10a":
        hits = _RE_TRANSITION_ALL.findall(blob)
    elif gid == "19":
        hits = _RE_BUZZWORD.findall(joined) + _RE_REBUTTAL.findall(joined)
    elif gid == "20":
        hits = [] if _RE_STAMP.search(css or html) else ["CSS 首行缺 Hallmark 印记"]
    elif gid == "26a":
        hits = ["无 :focus-visible"] if (_RE_INTERACTIVE.search(html) and not _RE_FOCUS.search(blob)) else []
    elif gid == "27":
        hits = ["动画缺 reduce 兜底"] if (_RE_KEYFRAMES.search(blob) and not _RE_REDUCED.search(blob)) else []
    elif gid == "30a":
        hits = [g for s in segs for g in _RE_DECOR_GLYPH.findall(s)]
    elif gid == "34":
        hits = _RE_OVERFLOW_HIDDEN.findall(blob)
    elif gid == "38i":
        hits = _RE_ITALIC_IN_HEAD.findall(html)
    elif gid == "54":
        hits = _RE_EYEBROW.findall(html)
    elif gid == "63":
        meta = [s for s in segs if len(_RE_META_DOTS.findall(s)) >= 2]
        hits = (meta + _RE_ARROW_GLUED.findall(html)
                + ["装饰状态圆点"] * len(_RE_STATUS_DOT.findall(html)))
    elif gid == "67":
        hits = _RE_TRACKED.findall(blob)
    elif gid == "6a":
        hits = _RE_VH.findall(blob)
    elif gid == "3":
        hits = _RE_ICON_TILE.findall(html)
    elif gid == "9":
        vals = re.findall(r"margin-bottom\s*:\s*(\d+px)", css, re.I)
        hits = [v for v in set(vals) if vals.count(v) >= 4]
    elif gid == "69":
        years: dict[str, int] = {}
        for s in segs:
            for y in set(re.findall(r"\b(?:20\d{2}|[1-9]\d{3,})\b", s)):
                years[y] = years.get(y, 0) + 1
        hits = [y for y, n in years.items() if n >= 4]
    elif gid == "F23":
        hits = _RE_DASH.findall(joined)
    else:
        hits = []
    samples: list[str] = []
    for h in hits[:3]:
        s = (h if isinstance(h, str) else str(h)).strip().replace("\n", " ")
        s = s[:87] + "…" if len(s) > 90 else s
        if s and s not in samples:
            samples.append(s)
    return len(hits), samples


def audit(html: str, css: str = "", surface: str = "push") -> dict:
    """对一份 HTML（及可选 CSS）做 Hallmark 静态体检，返回结构化报告。"""
    findings = []
    penalties = 0.0
    counts = {"floor": 0, "reflex": 0, "finish": 0, "auto_fixable": 0, "manual": 0, "hits": 0}
    waived = WAIVED_BY_SURFACE.get(surface, set())
    for g in GATES:
        if g["id"] in waived:
            continue
        n, samples = _detect(g["id"], html, css, surface)
        if not n:
            continue
        tier = g["tier"]
        findings.append({
            "gate": g["id"], "tier": tier, "name": g["name"], "count": n,
            "samples": samples, "tell": g["tell"], "fix": g["fix"], "auto": g["auto"],
        })
        counts[tier.lower()] += 1
        counts["hits"] += n
        counts["auto_fixable" if g["auto"] else "manual"] += 1
        penalties += _TIER_WEIGHT[tier] * (1 + min(n - 1, 4) * 0.15)
    score = max(0, min(100, round(100 - penalties)))
    floors = [f for f in findings if f["tier"] == "Floor"]
    verdict = "fail" if floors else ("reflex" if findings else "clean")
    # auto=True 的 gate 若仍有残留，说明命中位于新闻正文 / 数据文本而非 UI 标签——
    # 这类按事实保护不自动改写，在报告里显式说明，避免「标可修却没修」的误导。
    auto_residual = [f for f in findings if f["auto"] and f["count"]]
    residual_note = (
        f"{len(auto_residual)} 条标为可自动修复的 gate 仍有残留：命中位于新闻正文或数据文本，"
        "按事实保护只改 UI 装饰层，正文交由人工判断。"
    ) if auto_residual else ""
    return {
        "engine": ENGINE, "version": VERSION, "surface": surface,
        "checked_at": _now_cn(), "score": score, "verdict": verdict,
        "gates_checked": len(GATES) - len(waived), "findings": findings, "counts": counts,
        "waived_gates": sorted(waived), "residual_note": residual_note,
        "summary": (f"{len(findings)} 条 gate 命中，共 {counts['hits']} 处；"
                    f"可自动修复 {counts['auto_fixable']} 条、需人工判断 {counts['manual']} 条。"),
        "waivers": WAIVERS, "source": SOURCE, "boundary": BOUNDARY, "limits": LIMITS,
    }


# ── 改写器 ─────────────────────────────────────────────────────────────────
_STAMP = (
    "/* Hallmark · de-AI floor · 去 AI 味静态闸门（Nutlope/hallmark, MIT）\n"
    "   批评行：眉标 / 追踪大写 / 装饰符号 / 中点元信息 / 单斜体词是本项目最重的机器痕迹，\n"
    "   已在源码级逐条收敛；邮件模板的内联字面色与品牌字标按 waiver 保留。 */\n"
)
_FOCUS_RULE = "button:focus-visible,a:focus-visible,input:focus-visible{outline:2px solid currentColor;outline-offset:3px}"
_REDUCE_RULE = "@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}"


def _fix_label_punctuation(match: re.Match) -> str:
    """只改 UI 标签里的中点串联；正文、新闻标题、引用一律不碰。"""
    open_tag, text, close_tag = match.group(1), match.group(2), match.group(3)
    if len(_RE_META_DOTS.findall(text)) < 2:
        return match.group(0)
    sep = "；" if len(text) > 44 else "、"
    cleaned = re.sub(r"\s*·\s*", sep, text).lstrip(" ")
    return open_tag + cleaned + close_tag


_RE_CLASS_EL = re.compile(
    r"<(?P<tag>span|div|p)\b[^>]*" + _cls_containing(r"kicker|eyebrow") + r"[^>]*>", re.I)


def _remove_inert_labels(markup: str) -> tuple[str, int]:
    """删除眉标 / kicker 元素（含嵌套标签），用标签栈配平，避免误删正文或留孤标签。

    Hallmark 54：这行小字删掉不丢事实。只匹配 class 含 kicker/eyebrow 的元素本身，
    不动其它结构；嵌套的同名 / 异名标签用栈计数直到闭合。
    """
    out, pos, removed = [], 0, 0
    for m in _RE_CLASS_EL.finditer(markup):
        out.append(markup[pos:m.start()])
        tag = m.group("tag").lower()
        depth = 1
        cursor = m.end()
        token = re.compile(r"<(/?)%s\b[^>]*>" % tag, re.I)
        while depth > 0:
            nxt = token.search(markup, cursor)
            if not nxt:
                cursor = len(markup)
                break
            depth += -1 if nxt.group(1) else 1
            cursor = nxt.end()
        pos = cursor
        removed += 1
    out.append(markup[pos:])
    return "".join(out), removed


_RE_ROOT_BLOCK = re.compile(r":root\s*\{[^}]*\}", re.I | re.S)


def _sub_outside_root(pattern: str, repl: str, css: str, flags: int = 0) -> str:
    """只在 :root 之外做替换，避免把 token 定义本身换成自引用的 var()。

    Hallmark 7a 要求「所有颜色走 :root token」，但 token 的声明处必须是字面色值；
    因此 :root 块整体豁免，只修正文里的字面色。
    """
    parts, pos = [], 0
    for block in _RE_ROOT_BLOCK.finditer(css):
        parts.append(re.sub(pattern, repl, css[pos:block.start()], flags=flags))
        parts.append(block.group(0))
        pos = block.end()
    parts.append(re.sub(pattern, repl, css[pos:], flags=flags))
    return "".join(parts)


def polish(html: str, css: str = "", surface: str = "push") -> dict:
    """Hallmark 式「去 AI 味」改写。返回改写后的 html/css、修复清单与前后体检报告。

    只改装饰层；任何可见文本的数字 / URL / 链接数漂移都会触发整体回滚。
    """
    before = audit(html, css, surface)
    fixes: list[dict] = []
    new_html, new_css = html, css

    def sub(pattern, repl, gate, note, target="html", flags=0, count_hint=None):
        nonlocal new_html, new_css
        src = new_html if target == "html" else new_css
        out = re.sub(pattern, repl, src, flags=flags)
        if out == src:
            return
        n = count_hint if count_hint is not None else max(1, len(re.findall(pattern, src, flags=flags))) \
            if not callable(repl) else 1
        if target == "html":
            new_html = out
        else:
            new_css = out
        fixes.append({"gate": gate, "note": note, "count": n, "surface": target})

    # ── CSS 层 ──
    if css:
        sub(r"(background|background-image)\s*:\s*(?:linear|radial|conic)-gradient\([^;}]*\)",
            r"\1: var(--ink)", "2a", "紫蓝渐变 → 实心墨色", "css", re.I)
        sub(r"-webkit-background-clip\s*:\s*text\s*;?|background-clip\s*:\s*text\s*;?",
            "", "2a", "移除渐变文字 clip", "css", re.I)
        sub(r"transition\s*:\s*all\s+([\d.]+m?s)", r"transition: opacity \1, transform \1",
            "10a", "transition: all → 具名属性（保留时长）", "css", re.I)
        sub(r"transition\s*:\s*all\b(?!\s+[\d.]+m?s)", "transition: opacity .18s, transform .18s",
            "10a", "transition: all → 具名属性", "css", re.I)
        sub(r"text-transform\s*:\s*uppercase\s*;?", "", "67", "去全大写", "css", re.I)
        sub(r"letter-spacing\s*:\s*\.?(?:0?[4-9]|1\d|[2-9]\d|0\.[1-9]\d*)em\s*;?",
            "letter-spacing: .02em;", "67", "字距收敛到 .02em", "css", re.I)
        sub(r"overflow-x\s*:\s*hidden", "overflow-x: clip", "34", "hidden → clip", "css", re.I)
        sub(r"\b(\d+(?:\.\d+)?)vh\b", r"\1dvh", "6a", "vh → dvh", "css", re.I)
        if surface == "page" and re.search(r"--paper\s*:", new_css):
            target = new_css
            new_css = _sub_outside_root(r"#fff\b|#ffffff\b", "var(--paper)", new_css, re.I)
            if new_css != target:
                n = len(_RE_PURE_COLOR.findall(_RE_ROOT_BLOCK.sub("", target)))
                fixes.append({"gate": "7a", "note": "纯白字面色 → --paper token",
                              "count": max(1, n), "surface": "css"})
        if not _RE_STAMP.search(new_css):
            new_css = _STAMP + new_css
            fixes.append({"gate": "20", "note": "补 Hallmark 印记与批评行", "count": 1, "surface": "css"})
    elif not _RE_STAMP.search(new_html) and re.search(r"<style\b", new_html, re.I):
        # 推送 HTML 只有内联 <style>、没有外部 CSS：印记写进首个 <style> 的开头，
        # 这样 DEAI_STRICT=1 才有可达的通过路径（否则 gate 20 永远无法修复）。
        new_html = re.sub(r"(<style\b[^>]*>)", r"\1" + _STAMP.strip(), new_html,
                          count=1, flags=re.I)
        fixes.append({"gate": "20", "note": "内联 CSS 补 Hallmark 印记与批评行",
                      "count": 1, "surface": "html"})

    # ── HTML 层（只动装饰结构与 UI 标签） ──
    new_html, n_eyebrow = _remove_inert_labels(new_html)
    if n_eyebrow:
        fixes.append({"gate": "54", "note": f"删除 inert 眉标 / kicker（{n_eyebrow} 处）",
                      "count": n_eyebrow, "surface": "html"})
    # 只删纯装饰符号；＋ / ↻ / ↗ / ☼ / ◐ / ▶ 是功能字符，见 WAIVERS。
    sub(r"[✦✧★☆◆◇]\s*", "", "30a", "删除装饰符号图标", "html", re.I)
    # 只删「空」的状态圆点元素（class 含 live-dot / status-dot / pixel-dot）；
    # 复用探测器的引号兼容正则，保证探测与改写使用同一套模式。
    status_removed = [0]

    def _drop_status_dot(m: re.Match) -> str:
        if not m.group(0).strip().endswith("</span>"):
            return m.group(0)
        status_removed[0] += 1
        return ""

    new_html = _RE_STATUS_DOT.sub(_drop_status_dot, new_html)
    if status_removed[0]:
        fixes.append({"gate": "63", "note": f"删除装饰状态圆点（{status_removed[0]} 处）",
                      "count": status_removed[0], "surface": "html"})

    # 63 标签内中点串联：统计真实改写数（只作用于小标签元素）
    label_changed = [0]

    def _label_repl(m: re.Match) -> str:
        out = _fix_label_punctuation(m)
        if out != m.group(0):
            label_changed[0] += 1
        return out

    new_html = _RE_LABEL_SPAN.sub(_label_repl, new_html)
    if label_changed[0]:
        fixes.append({"gate": "63", "note": f"元信息中点串联 → 顿号/分号（{label_changed[0]} 处标签）",
                      "count": label_changed[0], "surface": "html"})

    # 38i 标题里单个斜体/高亮词 → 去标签保文字
    ital_heads = len(_RE_ITALIC_IN_HEAD.findall(new_html))
    if ital_heads:
        sub(r"(<h[1-4]\b[^>]*>)(.*?)(</h[1-4]>)",
            lambda m: m.group(1) + re.sub(r"</?(?:em|i|mark)\b[^>]*>", "", m.group(2), flags=re.I) + m.group(3),
            "38i", "标题单一斜体/高亮词 → 整条同款", "html", re.I | re.S, count_hint=ital_heads)

    # 内联 <style>（推送 HTML）里的同类底线
    sub(r"transition\s*:\s*all\s+([\d.]+m?s)", r"transition: opacity \1, transform \1",
        "10a", "内联 transition: all → 具名属性（保留时长）", "html", re.I)
    sub(r"transition\s*:\s*all\b(?!\s+[\d.]+m?s)", "transition: opacity .18s, transform .18s",
        "10a", "内联 transition: all → 具名属性", "html", re.I)
    sub(r"text-transform\s*:\s*uppercase\s*;?", "", "67", "内联去全大写", "html", re.I)
    sub(r"overflow-x\s*:\s*hidden", "overflow-x: clip", "34", "内联 overflow-x → clip", "html", re.I)
    sub(r"\b(\d+(?:\.\d+)?)vh\b", r"\1dvh", "6a", "内联 vh → dvh", "html", re.I)
    sub(r"letter-spacing\s*:\s*\.?(?:0?[4-9]|1\d|[2-9]\d|0\.[1-9]\d*)em\s*;?",
        "letter-spacing: .02em;", "67", "内联字距收敛", "html", re.I)

    # 26a / 27 兜底
    blob = new_html + "\n" + new_css
    if _RE_INTERACTIVE.search(new_html) and not _RE_FOCUS.search(blob):
        if css:
            new_css += "\n" + _FOCUS_RULE + "\n"
        else:
            new_html = re.sub(r"(</style>)", _FOCUS_RULE + r"\1", new_html, count=1, flags=re.I) \
                if re.search(r"</style>", new_html, re.I) else new_html + f"<style>{_FOCUS_RULE}</style>"
        fixes.append({"gate": "26a", "note": "补 :focus-visible 焦点环", "count": 1, "surface": "css"})
    if _RE_KEYFRAMES.search(blob) and not _RE_REDUCED.search(blob):
        if css:
            new_css += "\n" + _REDUCE_RULE + "\n"
        else:
            new_html = re.sub(r"(</style>)", _REDUCE_RULE + r"\1", new_html, count=1, flags=re.I) \
                if re.search(r"</style>", new_html, re.I) else new_html + f"<style>{_REDUCE_RULE}</style>"
        fixes.append({"gate": "27", "note": "补 reduced-motion 兜底", "count": 1, "surface": "css"})

    # ── 事实保护：可见文本的数字 / URL / 链接数签名必须一致，否则整体回滚 ──
    def signature(s: str):
        text = _visible_text(s)
        return (sorted(_RE_NUMBER.findall(text)), sorted(_RE_URL.findall(s)),
                len(_RE_ANCHOR.findall(s)))

    if signature(new_html) != signature(html):
        fixes = [{"gate": "fact-lock", "note": "检测到可见文本的数字/链接漂移，已整体回滚改写",
                  "count": 1, "surface": "guard"}]
        new_html, new_css = html, css

    after = audit(new_html, new_css, surface)
    changed = new_html != html or new_css != css
    return {
        "engine": ENGINE, "version": VERSION, "surface": surface,
        "checked_at": _now_cn(), "changed": changed,
        "html": new_html, "css": new_css, "fixes": fixes,
        "before": before, "after": after,
        "score_before": before["score"], "score_after": after["score"],
        "score_delta": after["score"] - before["score"],
        "verdict_before": before["verdict"], "verdict_after": after["verdict"],
    }


# ── 开关 ───────────────────────────────────────────────────────────────────
_MODES = ("polish", "audit", "off")


def get_mode(override: str | None = None) -> str:
    raw = override if override is not None else os.environ.get("DEAI_MODE", "polish")
    mode = str(raw).strip().lower()
    return mode if mode in _MODES else "polish"


def skip_enabled() -> bool:
    return os.environ.get("SKIP_DEAI_CHECK", "").strip().lower() in ("1", "true", "yes", "on")


def _force() -> str:
    return os.environ.get("DEAI_FORCE", "").strip().lower()


def strict_enabled() -> bool:
    return os.environ.get("DEAI_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


def public_config() -> dict:
    return {
        "mode": get_mode(), "skip": skip_enabled(), "force": _force(), "strict": strict_enabled(),
        "modes": list(_MODES),
        "env": {"DEAI_MODE": "polish|audit|off", "SKIP_DEAI_CHECK": "1 跳过",
                "DEAI_FORCE": "pass|block", "DEAI_STRICT": "1 有残留 Floor 就拦截"},
    }


def check_push(title: str, content: str, mode: str | None = None, surface: str = "push") -> dict:
    """推送前闸门：体检 +（可选）改写。返回 ok / reason / report / content。

    结构对齐 ``sensitive.check_push``：调用方把 ``content`` 换成返回的 ``content``
    再送去敏感词检测与实际推送。默认非阻塞（去 AI 味是设计体检，不是合规闸门）；
    ``DEAI_FORCE=block`` 或 ``DEAI_STRICT=1`` 且仍有未修复 Floor 才会拦截。
    """
    effective = get_mode(mode)
    info = {
        "ok": True, "reason": "", "mode": effective, "skipped": False,
        "report": None, "content": content, "checked_at": _now_cn(),
        "engine": ENGINE, "surface": surface,
    }
    if skip_enabled() or effective == "off":
        info["skipped"] = True
        info["reason"] = "SKIP_DEAI_CHECK / DEAI_MODE=off：已跳过去 AI 味闸门"
        return info

    force = _force()
    if force in ("pass", "block"):
        info["forced"] = True
        info["ok"] = force == "pass"
        info["reason"] = f"DEAI_FORCE={force} 强制结果（测试/应急）"
        rep = polish(content, surface=surface)
        info["report"] = _slim(rep)
        if force == "pass" and effective == "polish":
            info["content"] = rep["html"]
        return info

    rep = polish(content, surface=surface)
    info["report"] = _slim(rep)
    if effective == "polish":
        info["content"] = rep["html"]
        info["reason"] = (f"去 AI 味改写完成：{rep['score_before']} → {rep['score_after']} 分，"
                          f"修复 {len([f for f in rep['fixes'] if f['gate'] != 'fact-lock'])} 类痕迹")
    else:
        info["reason"] = f"去 AI 味体检（audit 模式，未改写）：{rep['score_before']} 分"

    residual = [f for f in rep["after"]["findings"] if f["tier"] == "Floor"]
    if strict_enabled() and residual and not info["skipped"]:
        info["ok"] = False
        info["reason"] += f"；DEAI_STRICT=1：仍有 {len(residual)} 条未修复 Floor 命中"
    return info


def _slim(rep: dict) -> dict:
    """去掉改写后的完整 HTML，只保留体检结论，供 API / 页面渲染。"""
    return {k: v for k, v in rep.items() if k not in ("html", "css")}


def reports(push_html: str = "", page_html: str = "", page_css: str = "") -> dict:
    """两个面各出一份体检报告 + 改写预览分数。供 ``GET /api/hallmark`` 使用。"""
    out = {
        "engine": ENGINE, "version": VERSION, "generated_by": "hallmark_deai.reports",
        "source": SOURCE, "boundary": BOUNDARY, "limits": LIMITS,
        "waivers": WAIVERS, "prompt": AI_PROMPT, "config": public_config(),
        "gates": GATES, "checked_at": _now_cn(), "surfaces": {},
    }
    if push_html:
        out["surfaces"]["push"] = _slim(polish(push_html, surface="push"))
    if page_html or page_css:
        out["surfaces"]["page"] = _slim(polish(page_html, css=page_css, surface="page"))
    scores = [s["score_before"] for s in out["surfaces"].values()]
    out["overall_score_before"] = round(sum(scores) / len(scores)) if scores else 0
    scores_after = [s["score_after"] for s in out["surfaces"].values()]
    out["overall_score_after"] = round(sum(scores_after) / len(scores_after)) if scores_after else 0
    out["status"] = "ok" if out["surfaces"] else "no_input"
    return out


def _now_cn() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


# ── CLI ────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Hallmark 去 AI 味静态体检 / 改写")
    ap.add_argument("action", choices=("report", "polish"))
    ap.add_argument("file", help="HTML 文件")
    ap.add_argument("--css", default="", help="外部 CSS 文件（管理页）")
    ap.add_argument("--surface", choices=("push", "page"), default="push")
    ap.add_argument("--write", action="store_true", help="polish 时把结果写回原文件与 CSS")
    args = ap.parse_args(argv)
    with open(args.file, encoding="utf-8") as f:
        html = f.read()
    css = ""
    if args.css:
        with open(args.css, encoding="utf-8") as f:
            css = f.read()
    if args.action == "report":
        print(json.dumps(_slim(polish(html, css, args.surface)), ensure_ascii=False, indent=2))
    else:
        rep = polish(html, css, args.surface)
        print(json.dumps({k: rep[k] for k in
                          ("score_before", "score_after", "score_delta", "changed", "fixes")},
                         ensure_ascii=False, indent=2))
        if args.write:
            with open(args.file, "w", encoding="utf-8") as f:
                f.write(rep["html"])
            if args.css and css:
                with open(args.css, "w", encoding="utf-8") as f:
                    f.write(rep["css"])
            print(f"已写回 {args.file}" + (f" 与 {args.css}" if args.css else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
