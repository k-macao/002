# 说明 / 注解 / 解析 / 提醒类文字：一律不展示

> 需求（2026-10-28）：「说明，注析，解析，提醒等文字，全文隐藏。」
> 两个面都处理：**推送正文**（`sources.build_html()`）与**页面**（`index.html` + `app.js`）。
> 实现：`meta_text.py`（规则 + 清理）、`meta_text.js`（页面运行时，由前者导出）；
> 测试：`test_meta_text.py`；接入点：`sources._hide_meta_text()`、`index.html` 的 `data-meta` 标记。

## 1. 隐藏什么，留什么

| 类别 | 举例 | 处理 |
| --- | --- | --- |
| 口径与方法注 | 「映射口径：…」「采集口径：实测 2 项 / 代理 0 项 / 未取得 18 项」「口径：LSTM（纯 Python 单层）」 | 整句删 |
| 页脚说明 / 免责 | `class="ftr"` 整块、「仅供参考」「不构成投资建议」「不作为投资依据」 | 页脚整块删；句子里的只掐分句 |
| 缺口与状态 | 「数据暂缺」「未取得」「数据完整度 20%」「暂无数据」 | 只删该分句，同句的结论与数值留下 |
| 展示范围解释 | 「线索列表不展示」「完整数据见 /api/heatmap」「实时结果见 /api/quant」 | 整句删 |
| 流程与计分提示 | 「15:30 采集 · 16:00 交付」「五维加权 +0.00（…）」 | 整句删 |
| 框架自我限定 | 「只借鉴公开资料」「不模仿任何特定个人语气」「背书」「历史类比」 | 整句删 |
| **结论与数据** | 「涨停 77 家」「权益仓位 50%」「上证指数 +1.13%」「观察 / 激励 / 约束 / 推演」正文 | **原样保留** |

判断单位是**句子**，不是整段：一句里既报缺口又给结论时，只掐掉缺口那半句。

## 2. 三档规则（`meta_text.py`）

1. **句子级**（`SENTENCE_META_PATTERNS`）：命中即整句删。只认固定搭配——
   「映射口径 / 指标口径 / 采集口径 / 统计口径 / 情感口径」是指南，
   「官媒口径 / 术语口径 / 事件研究口径」是**数据标签**，必须留着。
   `注：` 还要求出现在句首或句读之后（否则「明日关注：」会被误伤）。
2. **分句级**（`CLAUSE_META_PATTERNS`）：只删命中缺口 / 完整度的分句。
   `采集` 同样收窄为「采集口径 / 采集层 / 采集明细」，「前置数据采集」这类步骤名不受影响。
3. **元素级**：
   - `<div class="ftr">` 页脚整块删除；
   - 文本清空的容器（`ev` / `sub` / `td` / `tr` / `table`）逐层回收；
   - 只剩编号的表格行（`<td>α</td><td></td>`）整行去掉；
   - 过滤后只剩 `·` 的空壳文本节点清掉。

**受保护**：`<script>` / `<style>` 原样不动；「去 AI 味体检」「文本去 AI 味」两个诊断面板
（`#deaiSection` / `#humanizerSection`）与 41 个原子动作清单（`#quantActions`）整块跳过——
前两者是用户点名要看的证据（改了什么、为什么没改），后者删掉「兜底 / 说明」列会失去可执行性。

## 3. 事实锁：只减不增

清理只能删说明，**不能改数、不能造数**：

- 清理后出现的每个数字、每个 URL 都必须在清理前出现过（`facts_preserved()`）；
- `--apply` 写入前先跑这条断言，不通过就拒绝写文件、退出码 2。

这条锁的理由：说明文字本身带数字（「数据完整度 20%」「15:30 采集」），所以不能要求前后
数字完全相等；要求的是「不新增、不改写」。

## 4. 页面侧：标记 + 运行时清理

页面里既有结构性空标签（开关滑块 `<span></span>`、分隔线 `<div class="pixel-divider">`），
也有夹在行内 `<code>` 中间的说明文字。**按文本节点全页清洗会把版式拆坏**（试过：滑块、
分隔线、`<b>前置数据采集</b>` 都被切掉）。所以页面走两步：

1. **静态标记**：给整块都是说明的容器加 `data-meta="1"`，`styles.css` 一条
   `[data-meta] { display: none !important; }` 收工。
   ```bash
   python3 meta_text.py --mark-page index.html     # 幂等，已标过的不重复加
   python3 meta_text.py --check-page index.html    # 有露在外面的说明就退出码 1
   ```
   覆盖：`.insight-foot`、`.quant-notice`、`.bigv-notice`、`.group-note`，
   以及 `#quantProvenance`（采集明细）、`#quantPill`、`#quantMeta`、`#heatmapFoot`、
   `#policyDeepFoot`、`#bigVFoot`、`#deaiFoot`、`#hzFoot`。
2. **运行时清理**（`meta_text.js`，`<script>` 在 `app.js` 之前）：`app.js` 注入的内容
   （`/api/quant`、`/api/policy`、`/api/big-v`、`/api/heatmap` 等）里再冒出来的说明文字，
   由 `MutationObserver` 触发同一个规则集清掉。只处理「整块都是说明」的容器选择器，
   不碰表格单元格与结构性空标签。

## 5. 同步与开关

- `meta_text.js` 由 `python3 meta_text.py --emit-js meta_text.js` 生成，
  **不要手改**；`test_meta_text.py` 会比对两边规则，漂了就红。
- 恢复原样：`sources.META_TEXT_ENABLED = False`（推送）与删掉页面的 `data-meta` 标记
  （`git checkout index.html` 即可）。
- 本次删掉的片段记在 `sources.META_TEXT_HIDDEN`，供自检与回归断言。

## 6. 命令

```bash
python3 meta_text.py --report brief-output.html     # 只列会被删的片段，不改文件
python3 meta_text.py --apply  brief-output.html     # 就地清理（先过事实锁）
python3 meta_text.py --check  brief-output.html     # 有残留退出码 1
python3 meta_text.py --mark-page index.html         # 页面标记
python3 meta_text.py --check-page index.html        # 页面体检
python3 meta_text.py --emit-js meta_text.js         # 导出前端规则
python3 test_meta_text.py                           # 29 项回归
```

## 7. 与既有闸门的关系

| 闸门 | 管什么 | 本次改动 |
| --- | --- | --- |
| `hallmark_deai.py` | 视觉 / 排版层的 AI 味（装饰符号、眉标、渐变） | 不动 |
| `humanizer_zh.py` | 文字内容的 AI 腔（三段式、意义拔高） | 不动 |
| `meta_text.py` | **说明 / 注解 / 解析 / 提醒类文字本身** | 新增 |

三者串在同一个推送出口上，互不覆盖：先内容、再排版、最后按需隐藏说明层。
