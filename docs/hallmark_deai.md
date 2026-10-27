# 推送前「去 AI 味」体检 + 改写（Hallmark 闸门）

> 规则来源：[`Nutlope/hallmark`](https://github.com/Nutlope/hallmark)（Together AI，MIT 许可）。
> 本文记录本仓库把该 skill 的静态可判定子集移植为零依赖 Python 闸门的设计、边界与用法。
> 实现：`hallmark_deai.py`；测试：`test_hallmark_deai.py`；接入点：`push_brief.py`、`server.py`。

## 1. 为什么需要这道闸门

Hallmark 原本是给 Claude Code / Cursor / Codex 用的**网页设计 skill**：一份 `SKILL.md`
（The Floor / Reflex / Finish 三层规则）+ 一份 `references/gates.md`（§4 gate 表，带编号与
豁免语法）+ 一个 Node 版静态 linter `scripts/sloplint.mjs`。它针对的是 AI 生成页面里反复出现的
「设计八股」：紫蓝渐变、眉标（kicker/eyebrow）、追踪大写小标签、`✦ ★` 装饰符号当图标、
`A · B · C` 中点元信息、标题里夹一个斜体词、卡片套卡片等等。

本项目有两类 HTML 都会经过 AI 生成或 AI 辅助排版，都存在这些痕迹：

| surface | 内容 | 形态 | 约束 |
| --- | --- | --- | --- |
| `push` | `sources.build_html()` 生成的推送 HTML（含 `brief-output.html` 快照） | 内联 `<style>`、竖版长图文 | 必须兼容邮件客户端（旧式 CSS、字面色值） |
| `page` | 管理页 `index.html` + `styles.css`（`app.js` 动态注入） | 外部 CSS、有 `:root` token | GitHub Pages 纯静态托管，不能有服务端运行时 |

本闸门把 Hallmark 里**能用正则静态判定**的 M 类 gate 移植成零第三方依赖的 Python，
在推送前对 `push` 面做体检 + 安全改写，并对**两个面各出一份体检报告**。
Hallmark 需要真实浏览器与人眼判定的 R/J 类 gate（首屏折线、配色面积、每屏字数、
图片真实性等）**不在本闸门范围内**，也不假装有结论——统一在报告的 `limits` 字段说明。

## 2. 落地形态

### 2.1 推送前闸门（feature）

两条推送链路都接入同一套闸门，顺序完全一致：

```
① 大盘数据新鲜度检查（不是最新 → 退出码 3 / HTTP 409）
② 敏感词快讯过滤（sensitive.filter_brief）
③ 去 AI 味体检 + 改写（hallmark_deai.check_push）  ← 新增
④ 敏感词终检（在③改写后的最终 HTML 上执行，未通过 → 退出码 4 / HTTP 422）
⑤ POST 到 PushPlus
```

- `push_brief.py`：`main()` 在 `build_content()` 之后、`sensitive.check_push()` 之前调用。
  闸门拦截时返回**退出码 5**（已新增到退出码表）。
- `server.py` `POST /api/run`：在 `sources.build_html()` 之后、构造 `payload` 之前调用。
  闸门拦截时返回 **HTTP 422**，响应体带 `hallmark` 诊断字段。
- 闸门返回的 `content` 是改写后的最终 HTML，**③ 的输出直接送给 ④ 和 ⑤**，
  保证敏感词检测与实际推送看到的是同一份内容（有集成测试断言这一点）。
- 支持前端临时切换模式：`POST /api/run` 的 JSON body 可带
  `{"deai_mode": "polish"|"audit"|"off"}`。管理页「推送前 · 去 AI 味体检」区的下拉框
  会把选择记进 `localStorage` 并随 `/api/run` 提交。GitHub Actions 用环境变量控制。

### 2.2 两面体检报告（scope = both）

`GET /api/hallmark`（别名 `/api/deai`）返回两个面的报告：

```bash
curl http://localhost:4173/api/hallmark | python3 -m json.tool
curl "http://localhost:4173/api/hallmark?surface=push"   # 只体检推送 HTML
curl "http://localhost:4173/api/hallmark?surface=page"   # 只体检管理页
curl "http://localhost:4173/api/hallmark?mode=audit"     # 回显请求模式
```

响应顶层字段：`engine` / `version` / `source` / `boundary` / `limits` / `waivers` /
`prompt` / `config` / `gates` / `overall_score_before` / `overall_score_after` /
`surfaces.push` / `surfaces.page`。每个 surface 含 `before` / `after` 两份
`audit()` 报告（分数、逐 gate 命中、样例、修法、tier、是否可自动修复）、
`fixes`（本轮改写清单）、`changed`、`score_delta`。
报告不含改写后的完整 HTML（`_slim()` 裁剪），避免 `/api/hallmark` 返回超大正文。

命令行自检（不需要起服务）：

```bash
python3 hallmark_deai.py report brief-output.html
python3 hallmark_deai.py report index.html --css styles.css --surface page
python3 hallmark_deai.py polish index.html --css styles.css --surface page --write
```

### 2.3 源码级「去 AI 味」

除了运行时的推送前闸门，两个面的**源码本身**也已按 Hallmark 规则收敛（这样报告才是
「已经很干净」而不是每次都在改同一批痕迹）：

- 管理页：删除 11 处眉标 / kicker、9 处 `✦` 装饰前缀、hero 里的装饰状态圆点与
  标题内单个 `<em>` 高亮词；`.eyebrow/.kicker` 的 `text-transform: uppercase` 与
  `.12em` 字距收敛为常规标签（`.ind-label`）；`100vh → 100dvh`、
  `overflow-x: hidden → clip`、token 块外的 `#fff → var(--paper)`；
  补 Hallmark 印记、`:focus-visible` 焦点环与全局 `prefers-reduced-motion` 兜底。
- `app.js`：动态注入的 4 处 `✦` pill 前缀、2 处 `class="kicker"` 一并清理，
  否则页面运行时会把刚去掉的痕迹重新注入回来。
- 推送 HTML（`sources.py` 的内联 CSS 与生成模板）：模板开头加 Hallmark 印记，
  两处装饰 `✦` 改成中性分隔符；`brief-output.html` 快照同步重新生成。
- 保留项（waiver）：🐙 品牌字标、`＋` 全角加号（**两只 ETF 名称的真实分隔符，
  不是装饰**）、`↻ ↗ ☼ ◐ ▶` 功能图标、因果链箭头、邮件模板内联字面色、
  `章鱼 AI·全景分析` 产品名里的间隔号。理由逐条写在 `WAIVERS` 与报告里。

## 3. gate 目录（沿用上游编号，便于对照 gates.md）

| gate | tier | 命中 | 能否自动改 |
| --- | --- | --- | --- |
| 2a | Floor | 紫蓝渐变 / `background-clip:text` 渐变文字 | ✅ 换实心墨色 |
| 4 | Floor | 卡片套卡片 | ❌ 需重排结构，只报告 |
| 7a | Floor | token 块外的 `#000/#fff` 字面色（`page` 面） | ✅ 改 `var(--paper)` |
| 10a | Floor | `transition: all` / 动画 width/height/top/left | ✅ 具名属性，保留时长 |
| 19 | Floor | 宣传腔套话、空洞升华、机械三段式、「不是…而是…」排比 | ❌ 需人工改写正文 |
| 20 | Floor | 缺 Hallmark 印记与批评行 | ✅ 补印记注释 |
| 26a | Floor | 有交互元素但缺 `:focus-visible` | ✅ 补 3:1 焦点环 |
| 27 | Floor | 有 `@keyframes` 但缺 reduced-motion 兜底 | ✅ 补 reduce 媒体查询 |
| 30a | Floor | `✦ ★ ◆` 装饰符号当图标 | ✅ 删除（功能图标豁免） |
| 34 | Floor | `overflow-x: hidden` / `100vw` | ✅ → `clip` |
| 38i | Floor | 标题里夹单个斜体 / 高亮 / 异色词 | ✅ 去标签保文字 |
| 54 | Floor | 眉标 eyebrow / kicker | ✅ 标签栈配平后删除 |
| 63 | Floor | `A · B · C` 中点元信息、链接尾部箭头、装饰状态圆点 | ✅ 仅 UI 标签内改顿号/分号 |
| 67 | Floor | 小标签全大写 / `letter-spacing > .03em` | ✅ 去大写、字距归零 |
| 6a | Floor | 首屏用 `vh` | ✅ → `dvh` |
| 3 | Reflex | 三等宽图标卡片阵列 | ❌ 只报告 |
| 9 | Reflex | 每节等距分节 | ❌ 只报告 |
| 69 | Reflex | 同一数字 / 日期四节以上复读 | ❌ 只报告 |
| F23 | Finish | em dash 密度 | ❌ 只报告 |

分数是启发式体检分（按 tier 加权扣分），**不是 AIGC 检测概率**。`verdict` 取
`fail`（有 Floor 命中）/ `reflex`（只有 Reflex/Finish）/ `clean`。

## 4. 事实保护（最重要的一条）

对应 Hallmark「keeps claims tied to evidence」与中文 humanizer 的「锁住数字、人物、
时间、术语、引用」：

1. 所有自动改写**只作用于装饰层 / UI chrome**，绝不改新闻正文、数字、日期、人名、
   机构名、引号内文字与链接。
2. `polish()` 结束会比对可见文本的数字串、URL、`<a>` 数量的**签名**；一旦漂移就
   **整体回滚**该次改写（`fixes` 只剩 `fact-lock` 一条）。有专门的回滚单测。
3. `＋` 全角加号在推送 HTML 里是两只 ETF 的分隔符（如
   `03140.HK … ＋ 02807.HK …`），已从 30a 的删除集合里排除并在 `WAIVERS` 登记；
   否则会把两个基金名粘在一起。
4. 删除眉标用**标签栈配平**而不是贪婪正则，避免嵌套标签被误删或留下孤立 `</div>`。
5. `auto=True` 的 gate 若仍有残留（例如正文里的中点串联），报告会在
   `residual_note` 里说明「命中位于新闻正文，按事实保护只改 UI 装饰层」，
   避免「标可修却没修」的误导。

## 5. 开关（环境变量，风格与 `sensitive.py` 一致）

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `DEAI_MODE` | `polish` | `polish` 体检 + 改写；`audit` 只体检不改写；`off` 跳过 |
| `SKIP_DEAI_CHECK=1` | 关 | 跳过去 AI 味闸门（测试 / 应急） |
| `DEAI_FORCE=pass\|block` | 空 | 强制放行 / 强制拦截（测试 / 应急），优先于严格模式 |
| `DEAI_STRICT=1` | 关 | 改写后仍有未修复的 Floor 命中就拦截推送 |

**默认非阻塞**：去 AI 味是设计体检，不是合规闸门。默认会把能安全改的装饰痕迹改掉并
在报告里留痕，但不会因为「还有人工才能改的文案套话」就阻止一份合规简报发出。
只有显式 `DEAI_STRICT=1`（仍有残留 Floor）或 `DEAI_FORCE=block` 才会拦截。

## 6. 复现 / 可选的 AI 提示词

`AI_PROMPT`（在 `hallmark_deai.py` 与 `/api/hallmark` 的 `prompt` 字段里）是把同一套
「去 AI 味」逻辑交给外部 LLM 时可用的中文提示词，含 6 条约束（只改装饰层、锁住事实、
输出逐条 gate 说明与 waiver 理由）。**本仓库默认不调用任何外部模型**，闸门完全离线、
零第三方依赖——这是刻意保持与现有规则引擎一致的取舍。

## 7. 边界

- 只清理可静态判定的装饰痕迹与模板标点；不改新闻事实、数字、日期、人名、引用与链接。
- 不模仿任何真人的语气，也不声称能通过 AIGC 检测器；分数是启发式体检分，不是概率。
- R/J 类 gate 需要浏览器渲染与人眼判断，本闸门不检测（`limits` 字段如实列出）。
- 邮件客户端对 CSS 自定义属性支持不一致，推送 HTML 的内联字面色是兼容性取舍，
  按 waiver 保留——这是 `7a` 对 `surface="push"` 豁免的原因。
