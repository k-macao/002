#!/usr/bin/env python3
"""去「文本内容 AI 味」闸门 —— Humanizer-zh 反 AI 写作痕迹规则的静态子集移植。

规则来源（全部 MIT 许可，见 ``docs/humanizer_zh.md``）：

* ``op7418/Humanizer-zh``（主）：31 个检查点，A–F 六组，2026-09-23 重写版强调
  「**保留信息和确定程度**」优先于「改到像人」，并明确「模式清单是检查线索，
  不是词语黑名单」。
* ``blader/humanizer`` v3.1.0（上游）：基于 Wikipedia *Signs of AI writing*，
  25 条模式按强度排序，§1–§5 单次命中即可改，弱信号需聚簇。
* ``jiji262/humanizer-chinese``：36 条中文模式 + 盯防词表（CCL 2023 / HC3 依据）。

**明确排除** ``swaylq/humanize-chinese``：其 LICENSE 为 MIT + **Non-Commercial**
附加限制（禁止商业用途、衍生作品必须保留该限制）。本项目虽有推送场景，但不引入
任何带非商业条款的代码或词表，保持仓库干净的 MIT 立场。

与 ``hallmark_deai.py`` 的分工
----------------------------
* ``hallmark_deai.py`` 管**视觉 / 排版层**的 AI 味（CSS 渐变、眉标、字距、装饰符号）。
* 本模块管**文字内容层**的 AI 味（假对比、套话、升华结尾、讲义腔路标词等）。
两者在推送前依次执行，顺序固定：设计层 → 文本层 → 敏感词终检 → 推送。

事实保护（对齐上游「保留信息和确定程度」与本项目既有 fact-lock）
--------------------------------------------------------------
1. 自动改写**只做纯套话的删除**，不做句式重排、不做同义替换、不新增任何词。
2. 只处理 HTML 的**可见文本节点**；``<style>`` / ``<script>`` / 标签属性 /
   URL / ``<a>`` 链接文字一律不动。
3. **逐字新闻标题受保护**：``class="td-t"``（快讯标题）、``class="ev"``（证据行）、
   ``class="lnk"`` 等容器整体跳过——这些是引用，不是本项目生成的文案。
4. ``polish()`` 结束比对可见文本的**数字串、URL、``<a>`` 数量、引号内文字、
   日期**签名；任一漂移就**整体回滚**该次改写。
5. 需要语义判断的模式（不是 X 而是 Y、三段式、意义拔高、模糊归因）只**检测并报告**，
   不假装能靠正则安全改写；提供 ``AI_PROMPT`` 供外部 LLM 可选接入。

开关（环境变量，风格与 ``sensitive.py`` / ``hallmark_deai.py`` 一致）
-------------------------------------------------------------------
* ``HUMANIZER_MODE=polish``（默认）/ ``audit``（只体检不改写）/ ``off``（跳过）
* ``SKIP_HUMANIZER_CHECK=1`` 跳过闸门（测试 / 应急）
* ``HUMANIZER_FORCE=pass|block`` 强制放行 / 拦截（测试 / 应急）
* ``HUMANIZER_STRICT=1`` 改写后仍有未修复的强信号命中就拦截推送

命令行自检::

    python3 humanizer_zh.py report brief-output.html
    python3 humanizer_zh.py polish brief-output.html --write
    python3 humanizer_zh.py report index.html --surface page
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

ENGINE = "humanizer-zh-deai"
VERSION = "1.0"

SOURCE = {
    "projects": [
        {"name": "op7418/Humanizer-zh", "url": "https://github.com/op7418/Humanizer-zh",
         "license": "MIT", "role": "主规则：31 个检查点 A–F 六组；事实与确定程度优先"},
        {"name": "blader/humanizer", "url": "https://github.com/blader/humanizer",
         "license": "MIT", "role": "上游 v3.1.0：Wikipedia Signs of AI writing 模式强度分级"},
        {"name": "jiji262/humanizer-chinese", "url": "https://github.com/jiji262/humanizer-chinese",
         "license": "MIT", "role": "36 条中文模式与盯防词表（CCL 2023 / HC3）"},
    ],
    "wikipedia": "https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing",
    "excluded": [
        {"name": "swaylq/humanize-chinese",
         "url": "https://github.com/swaylq/humanize-chinese",
         "reason": "LICENSE 为 MIT + Non-Commercial 附加限制，禁止商业用途；本项目不引入其代码或词表"},
    ],
    "adaptation": (
        "本项目只移植可用正则静态判定的模式。上游 skill 的改写依赖 LLM 语义理解"
        "（判断 X 与 Y 是否各自提供信息、判断破折号是否承担解释功能等），"
        "本模块不假装有该能力：强信号一律检测并报告，自动改写只删除零信息的纯套话。"
    ),
}

AI_PROMPT = """你是一名中文财经简报的文字编辑。请按 Humanizer-zh（op7418/Humanizer-zh, MIT）
的 31 个检查点做「去 AI 味」改写，最高优先级是保留信息和确定程度：
1) 删除只强调不新增信息的讲义腔路标词（值得注意的是 / 众所周知 / 毋庸置疑）、
   起跑式铺垫（让我们深入看看）、万能升华结尾（未来可期 / 让我们拭目以待）、
   客服腔与假互动结尾（希望这对您有帮助 / 欢迎留言讨论）。
2) 处理「不是 X，而是 Y」假对比：若两端各提供独立信息则保留事实但改写句式；
   若否定端只是抬高语气的空话，删除否定端。不得把「可能」写成「确定」，
   不得把「计划」写成「已完成」，不得把相关写成因果。
3) 删除无出处的模糊归因与意义拔高（标志着新时代 / 深远影响 / 至关重要），
   但保留实名信源、真实判断、限定条件与数据时效说明。
4) 不改写任何数字、百分比、日期、人名、机构名、引号内原话与 URL；
   不新增原文没有的事实、引文、指标或第一人称经历；缺信息就直说缺。
5) 逐字引用的新闻标题、证据行、表格数据一律原样保留。
6) 输出改写后的完整文本，并附逐条说明：改了哪条模式、改了几处、为何不改其余命中。"""

BOUNDARY = (
    "只清理零信息的纯套话与可静态判定的文本痕迹；不改新闻事实、数字、日期、人名、"
    "引号内原话与链接；不模仿任何真人的语气，也不声称能通过 AIGC 检测器。"
    "分数是启发式体检分，不是 AI 生成概率。"
)

LIMITS = (
    "需要语义判断的模式（不是 X 而是 Y、强凑三段式、意义拔高、模糊归因、"
    "同义词轮换、句长节奏）只检测并报告，不做正则改写；它们交给 AI_PROMPT 可选接入的"
    "外部模型，或留给人工编辑。检测命中只说明「值得复查」，不证明文本由 AI 生成。"
)

# 本项目有意保留的表达，按上游「模式清单不是词语黑名单」显式登记。
WAIVERS = [
    {"gate": "A1", "item": "逐字引用的新闻标题里的「不是 X，而是 Y」",
     "reason": "属于被引用的原话（如券商观点标题），改写会篡改引文；受保护容器跳过"},
    {"gate": "A1", "item": "推演里「不是单一泡沫，而是分化」这类真对比",
     "reason": "两端各提供独立信息，属于应保留的事实区别；只在源码层人工改写句式"},
    {"gate": "C12", "item": "研报 / 政策文本里的「助力」「推动」「深入实施」",
     "reason": "公文与机构原文用语，属语体规范而非 AI 证据；且多为逐字引用"},
    {"gate": "D21", "item": "中文破折号 —— 与引号 「」",
     "reason": "中文破折号在解释 / 转折中是规范用法，中文版规则明确不全禁；「」为本项目既有引号样式"},
    {"gate": "F30", "item": "因果链里的「→」箭头",
     "reason": "承载方向信息，是结构化推演的符号而非装饰；视觉层另由 Hallmark 闸门判定"},
    {"gate": "E23", "item": "「方法边界 / 快照日期 / 不构成投资建议」等免责说明",
     "reason": "界定数据时效与证据强度，属必须保留的限定；上游明确「截至某日」不应删除"},
]

# ── 模式目录（沿用上游 A–F 分组编号，便于对照 SKILL.md） ─────────────────────
# tier: strong=单次命中即值得改 / medium=需聚簇 / weak=弱信号
# auto: True=本模块能安全自动改写（仅限零信息纯套话删除）
TIER_WEIGHT = {"strong": 6.0, "medium": 3.0, "weak": 1.0}


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern)


# 检测器：只作用于「可见文本」，不看标签属性 / style / script。
PATTERNS = [
    # A. 铺垫代替陈述
    {"id": "A1", "group": "A 铺垫代替陈述", "tier": "strong", "name": "不是 X，而是 Y 假对比",
     "tell": "用否定前半制造深刻感，后半往往只是普通观点；中文点名率最高的单一 AI 句式。",
     "fix": "若两端各有信息，保留事实但改写句式；若否定端是空话则删除。",
     "auto": False, "rx": _rx(r"不是[^，。；！?\n]{1,40}[，,][^。；！?\n]{0,12}而是|不仅仅是[^，。；\n]{1,40}[，,][^。；\n]{0,8}更是")},
    {"id": "A4", "group": "A 铺垫代替陈述", "tier": "strong", "name": "起跑式铺垫 / 路标句",
     "tell": "「让我们深入看看」「以下是你需要知道的」只预告下文，不提供信息。",
     "fix": "删除预告句，直接给内容，不用新知识填补字数。",
     "auto": True, "rx": _rx(r"(?:接下来|下面)?让我们(?:一起)?(?:深入|详细)?(?:了解|看看|探讨|分析)[^。！？\n]{0,18}[。！？，]|以下是你需要知道的[^。！？\n]{0,20}[。！？，]|话不多说[，,：:]")},
    {"id": "A5", "group": "A 铺垫代替陈述", "tier": "medium", "name": "与假想敌辩论",
     "tell": "「不要误会，我并不是…」式无对象自我辩护。",
     "fix": "删除没有具体内容的自我辩护，保留真实反对意见。",
     "auto": False, "rx": _rx(r"不要误会[，,]|我并不是在[^。；\n]{0,20}[，,]|别误会[，,]")},
    # B. 公式化节奏
    {"id": "B6", "group": "B 公式化节奏", "tier": "medium", "name": "强凑三段式",
     "tell": "三句排比看似工整，细看常是同一个意思；AI 对偶句密度显著高于人类。",
     "fix": "逐项检查是否提供独立信息，只合并重复或空泛项，不补造第四项。",
     "auto": False, "rx": _rx(r"(?:[一-龥A-Za-z0-9]{2,12}[、，])(?:[一-龥A-Za-z0-9]{2,12}[、，]){2,}[^。\n]{0,20}")},
    {"id": "B9", "group": "B 公式化节奏", "tier": "weak", "name": "限定词堆叠",
     "tell": "「也许可能会在一定程度上」压缩成一层不确定性即可。",
     "fix": "保留证据强度与适用范围，只删重复限定。",
     "auto": False, "rx": _rx(r"也许可能|或许可能|可能也许|大概可能|在一定程度上可能")},
    {"id": "B10", "group": "B 公式化节奏", "tier": "weak", "name": "讲义腔纯路标词",
     "tell": "「值得注意的是 / 众所周知」等空壳不添任何信息；AI 文本连词密度约为人类 3 倍（CCL 2023）。",
     "fix": "删除纯强调的空壳；承担转折 / 因果的连接词（由此可见 / 综上所述 / 与此同时）保留。",
     "auto": True, "rx": _rx(r"值得注意的是|值得一提的是|需要指出的是|不难看出|不难发现|众所周知|毋庸置疑|不可否认|显而易见")},
    {"id": "B11", "group": "B 公式化节奏", "tier": "weak", "name": "连接词密度（聚簇）",
     "tell": "「然而 / 此外 / 因此 / 与此同时」段段用，但句间没有对应关系。",
     "fix": "按作用判断：真实因果、转折、同时发生要保留；空连接才删。需语义判断，仅报告。",
     "auto": False, "rx": _rx(r"由此可见|归根结底|说到底|综上所述|总而言之|总的来说|与此同时|不仅如此")},
    # C. 拔高与借权威
    {"id": "C12", "group": "C 拔高与借权威", "tier": "strong", "name": "万能升华结尾 / 意义拔高",
     "tell": "结尾脱离具体内容给一个换任何文章都能用的祝愿，或把普通事实拔高成里程碑。",
     "fix": "停在最后一个具体事实上；有真实计划的用计划收尾，没有就不补。",
     "auto": True, "rx": _rx(r"未来可期|让我们拭目以待|拭目以待|期待更多可能|相信在不久的将来|必将迎来更加美好的明天|让我们共同期待|让我们携手|标志着[^。；\n]{0,12}新时代|具有里程碑意义|奠定了基础|产生深远影响|翻开了[^。；\n]{0,10}新篇章")},
    {"id": "C13", "group": "C 拔高与借权威", "tier": "strong", "name": "句尾补充式拔高",
     "tell": "陈述句后用逗号挂一个「体现了…彰显了…」的升华小句。",
     "fix": "删除只重复赞美的尾巴，保留有实际信息的原因、目的和结果。",
     "auto": False, "rx": _rx(r"[，,](?:这)?(?:充分)?(?:体现了|彰显了|展现了|凸显了|反映了|说明了|诠释了)[^。；！？\n]{0,40}")},
    {"id": "C15", "group": "C 拔高与借权威", "tier": "medium", "name": "模糊归因 / 借权威",
     "tell": "把观点挂在无名权威头上；有真实来源就写名字，写不出就删。",
     "fix": "保留已有实名信源与其主张；无出处的笼统归因保留其局限，不改成确定事实。",
     "auto": False, "rx": _rx(r"有专家指出|业内人士认为|有观点认为|不少网友表示|研究表明\([^)]*\)|数据显示\([^)]*\)|被广泛认为|权威人士[表认]")},
    {"id": "C16", "group": "C 拔高与借权威", "tier": "medium", "name": "AI 高频词 / 互联网黑话",
     "tell": "赋能 / 抓手 / 闭环 / 助力 / 打造 / 彰显 / 至关重要 等词换任何话题都能用。",
     "fix": "只在空泛、重复或成簇出现时改；正式词语与工程术语不因命中词表而删除。",
     "auto": False, "rx": _rx(r"赋能|抓手|闭环|底层逻辑|颗粒度|降本增效|认知升级|护城河|全方位|多维度|一站式|全链路|深入探讨|深度剖析|至关重要|不可或缺|举足轻重")},
    # D. 公式化排版
    {"id": "D20", "group": "D 公式化排版", "tier": "weak", "name": "装饰性标题 / emoji 前缀",
     "tell": "🚀💡✅ 做列点标记，规律性强、位置固定。",
     "fix": "去掉 emoji，用排版层级表达结构。",
     "auto": False, "rx": _rx(r"[🚀💡✅✨📌🎯🔥⭐️👉👉]")},
    {"id": "D21", "group": "D 公式化排版", "tier": "weak", "name": "破折号 / 引号强调癖",
     "tell": "反复用破折号制造悬念，或给日常词机械加引号。中文破折号规范用法不全禁。",
     "fix": "按作用判断：承担解释 / 转折的保留，纯装饰的改用逗号或括号。",
     "auto": False, "rx": _rx(r"——")},
    # E. 聊天与草稿残留
    {"id": "E22", "group": "E 聊天与草稿残留", "tier": "strong", "name": "客服腔 / 对话残留",
     "tell": "「好问题！」「希望这对您有帮助」是聊天回复框架直接粘进正文。",
     "fix": "整句删除，保留包在其中的实际信息。",
     "auto": True, "rx": _rx(r"好问题[！!]|当然可以[！!]|这是一个非常好的问题|您说得完全正确|您的观察很敏锐|这个观点很有见地|非常感谢您的指正|希望这(?:对|对您)|希望以上内容|如有任何问题[，,]?请随时|需要我继续吗|让我为您")},
    {"id": "E23", "group": "E 聊天与草稿残留", "tier": "medium", "name": "知识截止免责 / 猜测填充",
     "tell": "重复免责声明或「据推测 / 很可能」把猜测穿上事实外衣。",
     "fix": "去掉重复免责，但保留界定数据时效的「截至某日」与真实不确定性。",
     "auto": False, "rx": _rx(r"截至目前的公开信息|由于相关信息有限|据推测|据信|很可能出生于|鲜少公开露面")},
    {"id": "E35", "group": "E 聊天与草稿残留", "tier": "strong", "name": "假互动与引流结尾",
     "tell": "万能互动问句与机械引流话术，换任何文章都能用。",
     "fix": "删掉万能互动问句与引流话术，停在最后一个具体事实上。",
     "auto": True, "rx": _rx(r"你觉得呢[？?]|你有没有类似的经历[？?]|欢迎在评论区(?:留言|分享|讨论)[^。！？\n]{0,10}|点赞关注|点赞收藏|转发支持|评论区扣\s*1|欢迎分享你的看法")},
    # F. 中文表达补充检查
    {"id": "F27", "group": "F 中文补充", "tier": "weak", "name": "「进行＋动词」公文腔",
     "tell": "「对…进行分析」中的进行 / 作出 / 予以是万能动词，删掉意思不变。",
     "fix": "表达冗长时换直接动词；正式公文语体豁免。",
     "auto": False, "rx": _rx(r"进行(?:深入)?(?:分析|研究|测试|调整|优化|核查|评估)|作出(?:相应)?(?:调整|贡献|回应)|予以(?:妥善)?(?:解决|关注|说明)")},
    {"id": "F30", "group": "F 中文补充", "tier": "weak", "name": "「随着……的发展」式开头",
     "tell": "背景没有独立信息时的万能开场。",
     "fix": "仅当背景零信息时压缩；真实时间背景与因果保留。",
     "auto": False, "rx": _rx(r"随着[^，。\n]{0,18}(?:的)?(?:不断)?发展[，,]|在当今(?:社会|时代)[，,]|在信息爆炸的今天[，,]")},
    {"id": "F31", "group": "F 中文补充", "tier": "medium", "name": "套路化挑战与展望 / 骑墙",
     "tell": "「机遇与挑战并存」「见仁见智」「时间会证明一切」式不表态的万能收尾。",
     "fix": "只留有依据的具体问题；确实两可的写清各自适用条件。",
     "auto": True, "rx": _rx(r"机遇与挑战并存|任重道远|前景广阔|见仁见智|各有利弊|萝卜青菜各有所爱|时间会证明一切|众口难调|存在即合理|需根据实际情况选择")},
]

PATTERN_BY_ID = {p["id"]: p for p in PATTERNS}

# ── 自动改写规则：只允许「删除零信息纯套话」，逐条与上面的 auto=True 模式对应 ──
# 每条是 (gate, 正则, 替换)。替换只能是空串——绝不引入新词、绝不重排句式。
# 规则都是「独立短语」级，不依赖句首/句尾边界（前面的删除会改变边界）。
SAFE_RULES: list[tuple[str, str, str]] = [
    # B10 讲义腔纯路标词：空壳强调，连其后逗号一起删。
    ("B10", r"(?:值得注意的是|值得一提的是|需要指出的是|不难看出|不难发现|众所周知|毋庸置疑|不可否认|显而易见)[，,]?\s*", ""),
    # A4 起跑式铺垫：整句预告，含最多 18 字的宾语补足，止于句末标点。
    ("A4", r"(?:接下来|下面)?让我们(?:一起)?(?:深入|详细)?(?:了解|看看|探讨|分析)[^。！？；\n]{0,18}[。！？；]?[，,]?\s*", ""),
    ("A4", r"以下是你需要知道的(?:全部)?(?:内容)?[：:，,]?\s*", ""),
    ("A4", r"话不多说[，,：:]?\s*", ""),
    # C12 万能升华结尾：删除祝愿句本身，停在最后一个具体事实。
    ("C12", r"(?:相信在不久的将来[^。！？\n]{0,24}|让我们共同期待[^。！？\n]{0,16}|让我们拭目以待|拭目以待|未来可期|期待更多可能|必将迎来更加美好的明天|让我们携手[^。！？\n]{0,16})[。！？，,]?\s*", ""),
    ("C12", r"[，,]?\s*(?:标志着[^。；\n]{0,12}新时代|具有里程碑意义|产生深远影响|奠定了基础)[，,]?", ""),
    # E22 客服腔 / 对话残留
    ("E22", r"(?:好问题|当然可以|这是一个非常好的问题|您说得完全正确|您的观察很敏锐|这个观点很有见地|非常感谢您的指正)[！!。，,]?\s*", ""),
    ("E22", r"希望这(?:对|对您|对大家)[^。！？\n]{0,18}[。！？]?\s*", ""),
    ("E22", r"希望以上内容[^。！？\n]{0,20}[。！？]?\s*", ""),
    ("E22", r"如有任何问题[，,]?请随时[^。！？\n]{0,18}[。！？]?\s*", ""),
    ("E22", r"让我为您[^。！？\n]{0,18}[。！？]?\s*", ""),
    # E35 假互动 / 引流
    ("E35", r"(?:你觉得呢|你有没有类似的经历|欢迎分享你的看法)[？?。！]?\s*", ""),
    ("E35", r"(?:欢迎在评论区(?:留言|分享|讨论)[^。！？\n]{0,10}|点赞关注|点赞收藏|转发支持|评论区扣\s*1)[。！？~～，,]?\s*", ""),
    # F31 骑墙 / 套路展望
    ("F31", r"(?:机遇与挑战并存|任重道远|前景广阔|见仁见智|各有利弊|时间会证明一切|众口难调|存在即合理|需根据实际情况选择)[。！？，,]?\s*", ""),
]

# 自检：自动规则绝不能引入新字符（只允许删除）。
for _gate, _pat, _repl in SAFE_RULES:
    if _repl != "":
        raise RuntimeError(f"SAFE_RULES 只允许删除，不允许替换成 {_repl!r}")

# ── HTML 文本节点切分与受保护区域 ───────────────────────────────────────────
_RE_TOKEN = re.compile(r"(<[^>]*>)|([^<]+)", re.S)
_RE_TAG_PARSE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(/?)>", re.S)
_RE_CLASS = re.compile(r"class\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "col", "area", "base"}
# 容器内是逐字引用（新闻标题 / 证据 / 链接文字 / 代码），整体跳过改写。
_PROTECTED_TAGS = {"style", "script", "title", "a", "code", "pre", "textarea", "option"}
_PROTECTED_CLASSES = {"td-t", "ev", "lnk", "td-n", "td-p", "idx-n", "idx-p", "idx-c",
                      "hl", "hl-d", "tag", "tag-d", "tag-w", "sub", "point-no",
                      "meta-tag", "ai-pill", "kicker", "eyebrow", "ftr"}


def _protected(tag_name: str, attrs: str) -> bool:
    if tag_name.lower() in _PROTECTED_TAGS:
        return True
    m = _RE_CLASS.search(attrs or "")
    if not m:
        return False
    classes = next((g for g in m.groups() if g), "") or ""
    return bool(set(classes.split()) & _PROTECTED_CLASSES)


def visible_text(html: str) -> str:
    """取可见文本（去掉 style/script/标签），用于检测与事实签名。"""
    stripped = re.sub(r"<style\b[^>]*>.*?</style>|<script\b[^>]*>.*?</script>", "", html,
                      flags=re.I | re.S)
    return "".join(m.group(2) for m in _RE_TOKEN.finditer(stripped) if m.group(2))


def text_nodes(html: str) -> list[str]:
    """按 DOM 顺序返回所有可见文本节点（受保护容器内的也包含，供检测用）。"""
    return [m.group(2) for m in _RE_TOKEN.finditer(html) if m.group(2)]


def _rewrite_nodes(html: str) -> tuple[str, dict[str, int]]:
    """只在「非受保护」的可见文本节点上应用 SAFE_RULES。

    用标签栈配平，遇到受保护容器（新闻标题 / 证据 / 链接 / style 等）整段跳过。
    """
    out: list[str] = []
    stack: list[tuple[str, int]] = []
    protected_depth = 0
    applied: dict[str, int] = {}

    for token in _RE_TOKEN.finditer(html):
        tag, text = token.group(1), token.group(2)
        if tag is not None:
            out.append(tag)
            pm = _RE_TAG_PARSE.match(tag)
            if not pm:
                continue
            closing, name, attrs, self_closing = pm.group(1), pm.group(2).lower(), pm.group(3), pm.group(4)
            if closing:
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i][0] == name:
                        for _, inc in stack[i:]:
                            protected_depth -= inc
                        del stack[i:]
                        break
                continue
            if self_closing or name in _VOID_TAGS:
                continue
            inc = 1 if _protected(name, attrs) else 0
            stack.append((name, inc))
            protected_depth += inc
            continue

        if text is None or protected_depth > 0 or not text.strip():
            out.append(text)
            continue
        new_text = text
        node_applied: dict[str, int] = {}
        for gate, pattern, repl in SAFE_RULES:
            new_text, n = re.subn(pattern, repl, new_text)
            if n:
                node_applied[gate] = node_applied.get(gate, 0) + n
        if node_applied:
            # 只在本次确实删过套话的节点上收口空标点；未改动的节点原样保留，
            # 否则会误删 span 之间合法的「、」「；」分隔符。
            for gate, n in node_applied.items():
                applied[gate] = applied.get(gate, 0) + n
            new_text = re.sub(r"[。！？]{2,}", "。", new_text)
            new_text = re.sub(r"^\s*[，,、；;]\s*", "", new_text)
        out.append(new_text)

    return "".join(out), applied


# ── 事实保护签名 ───────────────────────────────────────────────────────────
_RE_NUMBER = re.compile(r"\d+(?:[.,:/-]\d+)*%?")
_RE_URL = re.compile(r"https?://[^\s\"'<>]+")
_RE_ANCHOR = re.compile(r"<a\b", re.I)
_RE_QUOTED = re.compile(r"[「『“\"'][^」』”\"']{1,120}[」』”\"']")
_RE_DATE = re.compile(r"\b(?:19|20)\d{2}[-/年.]\d{1,2}(?:[-/月.]\d{1,2})?")


def fact_signature(html: str) -> tuple:
    text = visible_text(html)
    return (
        sorted(_RE_NUMBER.findall(text)),
        sorted(_RE_URL.findall(html)),
        len(_RE_ANCHOR.findall(html)),
        sorted(_RE_QUOTED.findall(text)),
        sorted(_RE_DATE.findall(text)),
    )


# ── 检测 / 体检 ───────────────────────────────────────────────────────────
def _detect(html: str) -> list[dict]:
    text = visible_text(html)
    findings = []
    for p in PATTERNS:
        hits = [m.group(0) for m in p["rx"].finditer(text)]
        if not hits:
            continue
        # 弱信号需要聚簇：单独一次命中不计入。
        if p["tier"] == "weak" and len(hits) < 3:
            continue
        samples = []
        for h in hits[:3]:
            s = h.strip().replace("\n", " ")[:80]
            if s and s not in samples:
                samples.append(s)
        findings.append({
            "gate": p["id"], "group": p["group"], "tier": p["tier"], "name": p["name"],
            "count": len(hits), "samples": samples, "tell": p["tell"], "fix": p["fix"],
            "auto": p["auto"],
        })
    return findings


def audit(html: str, surface: str = "push") -> dict:
    """对一份 HTML 的**文本内容**做去 AI 味体检，返回结构化报告。"""
    findings = _detect(html)
    penalties = 0.0
    counts = {"strong": 0, "medium": 0, "weak": 0, "auto_fixable": 0, "manual": 0, "hits": 0}
    for f in findings:
        counts[f["tier"]] += 1
        counts["hits"] += f["count"]
        counts["auto_fixable" if f["auto"] else "manual"] += 1
        penalties += TIER_WEIGHT[f["tier"]] * (1 + min(f["count"] - 1, 4) * 0.15)
    score = max(0, min(100, round(100 - penalties)))
    strong = [f for f in findings if f["tier"] == "strong"]
    verdict = "needs_review" if strong else ("review" if findings else "clean")
    auto_residual = [f for f in findings if f["auto"] and f["count"]]
    residual_note = (
        f"{len(auto_residual)} 条标为可自动清理的模式仍有残留：命中位于逐字引用的新闻标题或"
        "需要语义判断的上下文，按事实保护只处理非引用文本节点。"
    ) if auto_residual else ""
    return {
        "engine": ENGINE, "version": VERSION, "surface": surface,
        "checked_at": _now_cn(), "score": score, "verdict": verdict,
        "patterns_checked": len(PATTERNS), "findings": findings, "counts": counts,
        "residual_note": residual_note,
        "summary": (f"{len(findings)} 类模式命中，共 {counts['hits']} 处；"
                    f"可自动清理 {counts['auto_fixable']} 类、需语义判断 {counts['manual']} 类。"),
        "waivers": WAIVERS, "source": SOURCE, "boundary": BOUNDARY, "limits": LIMITS,
    }


def polish(html: str, surface: str = "push") -> dict:
    """去 AI 味改写（只删零信息纯套话），带事实锁。

    返回改写后的 html、修复清单与前后体检报告；任何数字 / URL / 链接 / 引号内容 /
    日期漂移都会触发整体回滚。
    """
    before = audit(html, surface)
    new_html, applied = _rewrite_nodes(html)
    fixes = [{"gate": g, "note": PATTERN_BY_ID[g]["name"] + " → 删除零信息套话",
              "count": n, "surface": "text"} for g, n in sorted(applied.items())]

    if fact_signature(new_html) != fact_signature(html):
        fixes = [{"gate": "fact-lock", "note": "检测到数字/URL/链接/引文/日期漂移，已整体回滚改写",
                  "count": 1, "surface": "guard"}]
        new_html = html

    after = audit(new_html, surface)
    return {
        "engine": ENGINE, "version": VERSION, "surface": surface,
        "checked_at": _now_cn(), "changed": new_html != html,
        "html": new_html, "fixes": fixes, "before": before, "after": after,
        "score_before": before["score"], "score_after": after["score"],
        "score_delta": after["score"] - before["score"],
        "verdict_before": before["verdict"], "verdict_after": after["verdict"],
    }


# ── 开关 ──────────────────────────────────────────────────────────────────
_MODES = ("polish", "audit", "off")


def get_mode(override: str | None = None) -> str:
    raw = override if override is not None else os.environ.get("HUMANIZER_MODE", "polish")
    mode = str(raw).strip().lower()
    return mode if mode in _MODES else "polish"


def skip_enabled() -> bool:
    return os.environ.get("SKIP_HUMANIZER_CHECK", "").strip().lower() in ("1", "true", "yes", "on")


def _force() -> str:
    return os.environ.get("HUMANIZER_FORCE", "").strip().lower()


def strict_enabled() -> bool:
    return os.environ.get("HUMANIZER_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


def public_config() -> dict:
    return {
        "mode": get_mode(), "skip": skip_enabled(), "force": _force(), "strict": strict_enabled(),
        "modes": list(_MODES),
        "env": {"HUMANIZER_MODE": "polish|audit|off", "SKIP_HUMANIZER_CHECK": "1 跳过",
                "HUMANIZER_FORCE": "pass|block", "HUMANIZER_STRICT": "1 有残留强信号就拦截"},
    }


def check_push(title: str, content: str, mode: str | None = None,
               surface: str = "push") -> dict:
    """推送前文本层闸门：体检 +（可选）改写。返回 ok / reason / report / content。

    结构对齐 ``sensitive.check_push`` 与 ``hallmark_deai.check_push``：调用方把
    ``content`` 换成返回的 ``content`` 再送敏感词终检与实际推送。默认非阻塞。
    """
    effective = get_mode(mode)
    info = {
        "ok": True, "reason": "", "mode": effective, "skipped": False,
        "report": None, "content": content, "checked_at": _now_cn(),
        "engine": ENGINE, "surface": surface,
    }
    if skip_enabled() or effective == "off":
        info["skipped"] = True
        info["reason"] = "SKIP_HUMANIZER_CHECK / HUMANIZER_MODE=off：已跳过文本层去 AI 味闸门"
        return info

    force = _force()
    if force in ("pass", "block"):
        info["forced"] = True
        info["ok"] = force == "pass"
        info["reason"] = f"HUMANIZER_FORCE={force} 强制结果（测试/应急）"
        rep = polish(content, surface=surface)
        info["report"] = _slim(rep)
        if force == "pass" and effective == "polish":
            info["content"] = rep["html"]
        return info

    rep = polish(content, surface=surface)
    info["report"] = _slim(rep)
    if effective == "polish":
        info["content"] = rep["html"]
        cleaned = len([f for f in rep["fixes"] if f["gate"] != "fact-lock"])
        info["reason"] = (f"文本去 AI 味：{rep['score_before']} → {rep['score_after']} 分，"
                          f"清理 {cleaned} 类零信息套话")
    else:
        info["reason"] = f"文本去 AI 味体检（audit 模式，未改写）：{rep['score_before']} 分"

    residual = [f for f in rep["after"]["findings"] if f["tier"] == "strong"]
    if strict_enabled() and residual and not info["skipped"]:
        info["ok"] = False
        info["reason"] += f"；HUMANIZER_STRICT=1：仍有 {len(residual)} 类强信号需人工复查"
    return info


def _slim(rep: dict) -> dict:
    """去掉改写后的完整 HTML，只保留体检结论，供 API / 页面渲染。"""
    return {k: v for k, v in rep.items() if k != "html"}


def reports(push_html: str = "", page_html: str = "") -> dict:
    """两个面各出一份文本体检报告 + 改写预览分数。供 ``GET /api/humanizer`` 使用。"""
    out = {
        "engine": ENGINE, "version": VERSION, "generated_by": "humanizer_zh.reports",
        "api": "/api/humanizer（或 /api/text-deai）",
        "source": SOURCE, "boundary": BOUNDARY, "limits": LIMITS,
        "waivers": WAIVERS, "prompt": AI_PROMPT, "config": public_config(),
        "patterns": [{k: v for k, v in p.items() if k != "rx"} for p in PATTERNS],
        "checked_at": _now_cn(), "surfaces": {},
    }
    if push_html:
        out["surfaces"]["push"] = _slim(polish(push_html, surface="push"))
    if page_html:
        out["surfaces"]["page"] = _slim(polish(page_html, surface="page"))
    before = [s["score_before"] for s in out["surfaces"].values()]
    after = [s["score_after"] for s in out["surfaces"].values()]
    out["overall_score_before"] = round(sum(before) / len(before)) if before else 0
    out["overall_score_after"] = round(sum(after) / len(after)) if after else 0
    out["status"] = "ok" if out["surfaces"] else "no_input"
    return out


def _now_cn() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


# ── CLI ────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Humanizer-zh 文本内容去 AI 味静态体检 / 改写")
    ap.add_argument("action", choices=("report", "polish", "text"))
    ap.add_argument("file", help="HTML 文件")
    ap.add_argument("--surface", choices=("push", "page"), default="push")
    ap.add_argument("--write", action="store_true", help="polish 时把结果写回原文件")
    args = ap.parse_args(argv)
    with open(args.file, encoding="utf-8") as f:
        html = f.read()
    if args.action == "report":
        print(json.dumps(_slim(polish(html, args.surface)), ensure_ascii=False, indent=2))
    elif args.action == "text":
        print(visible_text(html))
    else:
        rep = polish(html, args.surface)
        print(json.dumps({k: rep[k] for k in
                          ("score_before", "score_after", "score_delta", "changed", "fixes")},
                         ensure_ascii=False, indent=2))
        if args.write:
            with open(args.file, "w", encoding="utf-8") as f:
                f.write(rep["html"])
            print(f"已写回 {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
