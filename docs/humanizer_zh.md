# 推送前「文本内容去 AI 味」闸门（Humanizer-zh 静态子集）

> 规则来源：GitHub 上三个 **MIT 许可**的中文去 AI 味项目。本文记录本仓库把它们的
> 静态可判定子集移植为零依赖 Python 闸门的设计、边界与用法。
> 实现：`humanizer_zh.py`；测试：`test_humanizer_zh.py`；接入点：`push_brief.py`、`server.py`。

## 1. 为什么需要这道闸门

上一版的 [Hallmark 闸门](hallmark_deai.md) 管的是**视觉 / 排版层**的 AI 味
（CSS 渐变、眉标、追踪大写、`✦` 装饰符号、中点元信息）。它不处理**文字内容**本身。
本项目推送的简报里有大量 AI 生成或 AI 辅助撰写的中文分析段落，同样会带上
「不是 X，而是 Y」「未来可期」「众所周知」「希望这对您有帮助」这类一眼模型腔的表达。

用户在 2026-10-27 提出「在 GitHub 上找去文本内容 AI 味的项目，然后每次推送用上」。
调研后选用下面三个 MIT 项目（按 `git clone` 读过的 `SKILL.md` / `LICENSE` 为准）：

| 项目 | 许可 | 用途 |
| --- | --- | --- |
| [`op7418/Humanizer-zh`](https://github.com/op7418/Humanizer-zh) | MIT | 主规则：31 个检查点 A–F 六组；2026-09-23 重写版把「**保留信息和确定程度**」列为最高优先级，并明确「模式清单是检查线索，不是词语黑名单」 |
| [`blader/humanizer`](https://github.com/blader/humanizer) | MIT | 上游 v3.1.0：基于 Wikipedia *Signs of AI writing*，模式按强度排序，§1–§5 单次命中即可改，弱信号需聚簇 |
| [`jiji262/humanizer-chinese`](https://github.com/jiji262/humanizer-chinese) | MIT | 36 条中文模式与盯防词表（CCL 2023 / HC3 量化依据） |

**明确排除** [`swaylq/humanize-chinese`](https://github.com/swaylq/humanize-chinese)：
它的 `LICENSE` 是 **MIT + Non-Commercial** 附加条款，禁止商业用途，且要求衍生作品
保留同样的非商业限制。本项目虽是个人推送脚本，但不引入任何带非商业条款的代码或词表，
保持仓库干净的 MIT 立场。该排除理由同时写在模块 `SOURCE["excluded"]` 与 `/api/humanizer`
报告里，可追溯。

## 2. 核心设计取舍：检测要宽，改写要窄

上游三个项目都是 **Agent Skill**（一份 Markdown 规则，交给 Claude / Codex 等 LLM 执行），
不是独立的检测程序。`op7418` 的 README 首句就写着「这是一份由 Agent 读取执行的编辑指导，
不是独立的检测程序……不保证通过任何 AI 检测器」。

本仓库必须**零第三方依赖、可离线、确定性**，不能推送时调外部 LLM。因此设计成：

1. **检测（宽）**：把 18 条可用正则静态判定的模式移植为 Python 检测器，按
   `strong / medium / weak` 分级；弱信号沿用上游「聚簇才算」原则（单篇少于 3 次不计入）。
2. **改写（窄）**：自动改写**只删除零信息纯套话**——升华结尾、客服腔、假互动引流、
   起跑式铺垫、讲义腔纯路标词、骑墙套话。**不做**句式重排、**不做**同义替换、
   **不新增任何词**。代码里有启动自检：`SAFE_RULES` 的替换值只能是空串。
3. **需语义判断的模式只报告**：`不是 X，而是 Y` 假对比、强凑三段式、意义拔高、
   模糊归因、句尾拔高、AI 高频词等，都只检测并给出样例与修法，不假装正则能安全改写。
   这些交给 `AI_PROMPT`（模块里、也在 `/api/humanizer` 的 `prompt` 字段）供外部 LLM
   可选接入，或留给人工编辑。报告里 `auto: false` 与 `limits` 字段如实说明。

这是刻意的保守设计：把「去 AI 味」做成不会篡改事实的静态清洗，而不是一个
可能把「可能」改成「确定」、把「计划」改成「已完成」的改写器。

## 3. 保护机制（最重要）

对应上游「保留信息和确定程度」与本项目既有 fact-lock：

1. **只处理可见文本节点**。`<style>` / `<script>` / `<title>` / 标签属性 / URL /
   `<code>` / `<pre>` / `<a>` 链接文字整体跳过（标签栈配平）。
2. **逐字引用受保护**。推送 HTML 里 `class="td-t"` 的快讯标题、`class="ev"` 的证据行、
   `class="lnk"` 以及 `ftr` / `tag` / `sub` / `hl` 等容器整体不改写——
   196 条快讯标题里有券商原话「不是牛市终结，而是技术性重置」，改写就是篡改引文。
3. **事实锁签名**：`polish()` 结束比对可见文本的**数字串、URL、`<a>` 数量、
   引号内文字、日期**五元组；任一漂移就**整体回滚**该次改写
   （`fixes` 只剩 `fact-lock` 一条）。有专门的回滚单测。
4. **标点收口只作用于本次确实删过套话的节点**。早期版本在全部文本节点上做
   `、；` 收口，误删了 `<span>` 之间合法的分隔顿号——现已在单测里锁死该回归。
5. **保留项（waivers）** 显式登记：逐字引用里的假对比、研报公文用语、中文破折号、
   因果链箭头、界定数据时效的免责说明。理由见模块 `WAIVERS` 与报告。

## 4. 接入推送链路

两条推送链路都接同一套闸门，顺序完全一致：

```
① 大盘数据新鲜度检查（不是最新 → 退出码 3 / HTTP 409）
② 敏感词快讯过滤（sensitive.filter_brief）
③ 去 AI 味 · 设计层（hallmark_deai.check_push）
④ 去 AI 味 · 文本内容层（humanizer_zh.check_push）   ← 本版本新增
⑤ 敏感词终检（在两次改写后的最终 HTML 上执行，未通过 → 退出码 4 / HTTP 422）
⑥ POST 到 PushPlus
```

- `push_brief.py`：`main()` 在设计层之后、构造 `payload` 之前调用。
  文本层严格模式拦截返回**退出码 6**。
- `server.py` `POST /api/run`：同样位置调用；拦截返回 **HTTP 422**，
  响应体带 `humanizer` 诊断字段。
- 前端可临时切换模式：`POST /api/run` 的 JSON body 支持
  `{"deai_mode": ..., "humanizer_mode": "polish"|"audit"|"off"}`；
  管理页两个下拉框把选择记进 `localStorage` 并一起提交。GitHub Actions 用环境变量。
- 设计层与文本层的输出**依次串接**再送敏感词终检和推送，保证三者看到同一份最终 HTML
  （有集成测试断言 `sensitive` 与 `http` 收到的内容完全一致）。

### 体检报告

`GET /api/humanizer`（别名 `/api/text-deai`）对**推送 HTML 与管理页各出一份文本体检报告**：

```bash
curl http://localhost:4173/api/humanizer | python3 -m json.tool
curl "http://localhost:4173/api/humanizer?surface=push"    # 只体检推送 HTML
curl "http://localhost:4173/api/humanizer?surface=page"    # 只体检管理页
```

响应字段与 `/api/hallmark` 对齐：`overall_score_before/after`、`surfaces.push/page`
（含 `before`/`after` 两份 `audit()` 报告、`fixes` 清单）、`patterns`（模式目录）、
`waivers`、`prompt`、`config`、`source`、`boundary`、`limits`。
响应裁剪掉改写后的完整 HTML，避免返回超大正文。

命令行自检（不需要起服务）：

```bash
python3 humanizer_zh.py report brief-output.html
python3 humanizer_zh.py polish brief-output.html --write
python3 humanizer_zh.py text brief-output.html     # 打印可见文本
```

## 5. 模式目录（沿用上游 A–F 分组）

| 编号 | 强度 | 模式 | 自动清理 |
| --- | --- | --- | --- |
| A1 | strong | 不是 X，而是 Y 假对比 | ❌ 需语义判断 |
| A4 | strong | 起跑式铺垫 / 路标句 | ✅ 删预告句 |
| A5 | medium | 与假想敌辩论 | ❌ 只报告 |
| B6 | medium | 强凑三段式 | ❌ 只报告 |
| B9 | weak | 限定词堆叠 | ❌ 只报告 |
| B10 | weak | 讲义腔纯路标词（聚簇） | ✅ 删空壳词 |
| B11 | weak | 连接词密度（聚簇） | ❌ 只报告 |
| C12 | strong | 万能升华结尾 / 意义拔高 | ✅ 删祝愿句 |
| C13 | strong | 句尾补充式拔高 | ❌ 只报告 |
| C15 | medium | 模糊归因 / 借权威 | ❌ 只报告 |
| C16 | medium | AI 高频词 / 互联网黑话 | ❌ 只报告（不能按词表硬删） |
| D20 | weak | 装饰性标题 / emoji 前缀 | ❌ 视觉层已由 Hallmark 处理 |
| D21 | weak | 破折号 / 引号强调癖 | ❌ 中文破折号规范用法不全禁 |
| E22 | strong | 客服腔 / 对话残留 | ✅ 整句删 |
| E23 | medium | 知识截止免责 / 猜测填充 | ❌ 数据时效说明要保留 |
| E35 | strong | 假互动与引流结尾 | ✅ 整句删 |
| F27 | weak | 「进行＋动词」公文腔 | ❌ 公文语体豁免 |
| F30 | weak | 「随着……的发展」式开头 | ❌ 真实时间背景要保留 |
| F31 | medium | 套路化挑战与展望 / 骑墙 | ✅ 删空泛套话 |

分数是启发式体检分（按强度加权扣分），**不是 AI 生成概率**。`verdict` 取
`needs_review`（有强信号）/ `review`（只有中弱信号）/ `clean`。

## 6. 开关（环境变量，风格与 `sensitive.py` 一致）

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `HUMANIZER_MODE` | `polish` | `polish` 体检 + 清理；`audit` 只体检不清理；`off` 跳过 |
| `SKIP_HUMANIZER_CHECK=1` | 关 | 跳过文本层闸门（测试 / 应急） |
| `HUMANIZER_FORCE=pass\|block` | 空 | 强制放行 / 强制拦截（测试 / 应急），优先于严格模式 |
| `HUMANIZER_STRICT=1` | 关 | 清理后仍有未修复的 `strong` 命中就拦截推送 |

**默认非阻塞**：文本层与设计层一样是体检闸门，不是合规闸门。默认会删掉能安全删的
零信息套话并在报告里留痕，但不会因为「逐字引用的新闻标题里有假对比」就阻止一份
合规简报发出。只有显式 `HUMANIZER_STRICT=1` 或 `HUMANIZER_FORCE=block` 才会拦截。

## 7. 源码级收敛

除了运行时的推送前闸门，本仓库**生成的文案模板**也已按规则收敛，使报告不是
「每次都在改同一批痕迹」：

- `big_v_analysis.py` 的 `_headline` 与 `base` 模板去掉「不是三条平行新闻，而是…」
  「不是单一泡沫，而是…」假对比，改成直接陈述同一条因果链；事实不变。
- `sources.py` 的推送渲染串去掉冗余的 `analyst` 前缀与「模仿本人语气」措辞。
- `index.html` 静态快照、推送快照 `brief-output.html` 同步改写；
  研报 / 券商原话等**逐字引用**保持不动。
- 应前次要求删除特定人名「江学勤」三字：`ANALYST` 改为中性值 `公开框架`，
  API 的 `analyst` 键保留（前端与测试仍可用），可见的拼音参考标题中性化但
  **保留原始 URL**（可核验来源不能因改名而失效）。

## 8. 边界

- 只清理零信息的纯套话与可静态判定的文本痕迹；不改新闻事实、数字、日期、人名、
  引号内原话与链接；不模仿任何真人的语气，也不声称能通过 AIGC 检测器。
- 强信号模式（假对比、三段式、拔高、模糊归因）只检测并报告，不做正则改写。
- 分数是启发式体检分，不是概率；命中模式只说明「值得复查」，不证明文本由 AI 生成。
- 上游 skill 依赖 LLM 语义理解，本模块是其**静态子集**，不等价于完整 Humanizer-zh。
- 未引入任何 Non-Commercial 许可的代码或词表。
