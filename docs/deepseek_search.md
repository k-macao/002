# DeepSeek Search & Fetch 接入说明（2026-10-17 新 API）

DeepSeek 于 **2026-10-17** 首次开放其自研搜索引擎的 Search 与 Fetch API（官方 Change Log：
<https://api-docs.deepseek.com/updates/>，使用指南：<https://api-docs.deepseek.com/guides/search>）。
本仓库的**全部「搜索型」数据路径**已接入该 API：配置 `DEEPSEEK_API_KEY` 后优先走 DeepSeek 搜索，
未配置 / 搜索失败 / 空结果时自动回退原通道，推送与页面行为不中断。

## 官方规格速览

| 项目 | Search | Fetch |
| --- | --- | --- |
| 端点 | `POST https://api.deepseek.com/v1/tools/search` | `POST https://api.deepseek.com/v1/tools/fetch` |
| 入参 | `query`（1-128 字符，支持 `site:` / `-site:` / `"…"` / `lang:zh,en` / `after:` / `before:`）、`count`（1-50，默认 10）、`ori_question`、`localization`、`need_content` | `url`（≤8192 字符，http/https + host，不得带用户名/密码） |
| 出参 | `results[]`：`rerank_score`（0-3，≥1.5 高质量）、`title`、`url`、`snippet`、`summary`、`content`、`site_name`、`site_icon`、`date`、`authority`（0-4，≥3 高权威，-1 未知） | `title` + `content`（Markdown 正文） |
| 鉴权 | `Authorization: Bearer <DeepSeek API Key>`（与模型 API 同一把 Key） | 同左 |
| 计费 | $0.75 / 千次**成功**请求；空结果 / 失败 / 超时免费；`count` 不影响单价 | 同左 |
| 限流 | 5 QPS / 账号（两个接口独立计算，另 Fetch 有按域名限流） | 5 QPS / 账号 |
| 重试 | 仅 408 / 429 / 5xx；402 需先充值；其余 4xx 不改请求不要重试 | 同左 |

注：经 DeepSeek Harness / Claude Code / Codex 的服务端 Search/Fetch 调用在
2026-11-01 00:00（北京时间）前免费，之后按上表计费。本仓库是直接调 REST 接口，
「成功才计费」的口径同样适用。

## 本仓库改了什么

- **新增 `deepseek_search.py`**：纯标准库客户端（保持零第三方依赖）。含官方参数校验、
  账号级 5 QPS 限流器（Search/Fetch 各一个、线程安全）、仅 408/429/5xx 指数退避重试、
  OpenAI 兼容错误解析、`status()` 自检（只回 `***尾号`，永不泄漏 Key 明文）。
- **`sources.py` 全部搜索路径 DeepSeek 优先**（源总数仍为 69、源名不变）：
  原先经 Google News RSS 检索的 11 个源都带上 `deepseek_search` 元信息
  （雪球讨论精选、东方财富股吧热议、集思录、淘股吧、富途牛牛圈、老虎社区、TradingView
  中文观点、知乎财经话题、Stocktwits、QuantConnect、Reuters 路透），采集时拼
  `[term] site:{域名} lang:{zh|en} after:{N 天前}` 查询；「Google news 中文」采集器
  同样改 DeepSeek 优先（查询 `今日新闻 要闻 财经 lang:zh after:{昨天}`，可用
  `DEEPSEEK_NEWS_QUERY` 覆盖）。任一源搜索未启用 / 报错 / 本轮无结果 → 走原
  Google News RSS → 演示数据兜底，和接入前完全一致。清单可用
  `sources.deepseek_search_sources()` 查看，防漏接有专门回归测试
  （`test_every_google_news_search_source_is_deepseek_backed`）。
- **采集条目新增字段**：DeepSeek 命中的条目在既有 `title`/`url` 之外带
  `snippet`、`summary`（600-800 字、给模型读）、`site_name`、`date`、`authority`、
  `rerank_score`、`source_api="deepseek-search"`；现有 `analyze_*` / `build_html()`
  只读 `title`/`url`，不受影响，新字段供后续 RAG / 证据排序使用。
- **`server.py` 新接口**：`GET /api/deepseek/status`（自检，不含 Key）、
  `GET|POST /api/search`（全网搜索，同一查询 5 分钟内走服务端缓存避免重复计费）、
  `GET|POST /api/fetch`（单页正文）。未配置 Key 返回 503 并说明如何开通；
  参数错误 400、上游失败 502，任何路径不返回 405。
- **页面新板块**：`index.html` 底部「全网搜索」卡片（`app.js` 走相对路径
  `api/search`，浏览器不直连 `api.deepseek.com`，Key 只留在服务端）。
- **GitHub Actions**：`daily-push.yml` 透传可选 Secret `DEEPSEEK_API_KEY`
  （不配置也能照常推送——全部搜索型源走 RSS 回退）。
- **测试**：`test_deepseek_search.py`（25 例）用本地假 DeepSeek 服务验证鉴权头/请求体、
  校验、归一化、429 重试、401 不重试、限流间隔、采集分发与回退、服务端接口与缓存，
  全程离线、不需要真 Key。

## 启用方法

```bash
# 本地服务（页面「全网搜索」+ 采集优先 DeepSeek）
DEEPSEEK_API_KEY=sk-... python3 server.py
# 打开 http://localhost:4173 ，状态自检： curl http://localhost:4173/api/deepseek/status

# 推送脚本直接受益：12 个搜索型源自动切换后端（无需改动推送命令）
DEEPSEEK_API_KEY=sk-... PUSHPLUS_TOKEN=xxx python3 push_brief.py
```

GitHub Actions：在仓库 Secrets 里加 `DEEPSEEK_API_KEY` 即可（已写进
`daily-push.yml`，为空时自动回退 RSS）。

直接调 API 的例子：

```bash
curl "http://localhost:4173/api/search?q=A股%20site:wallstreetcn.com%20lang:zh&count=5"
curl -X POST http://localhost:4173/api/fetch -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/article"}'
```

代码内调用：

```python
import sources
sources.web_search("DeepSeek V4.1 发布", limit=5)   # -> [{title, url, summary, ...}]
sources.fetch_web_page("https://example.com/a")     # -> {title, content(Markdown), url}
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | （空=禁用） | DeepSeek API Key，与模型 API 共用；GitHub Actions 走同名 Secret |
| `DEEPSEEK_SEARCH_ENABLED` | 已配 Key 即启用 | 设 `0` 显式禁用，全部搜索回退原通道 |
| `DEEPSEEK_API_BASE_URL` / `DEEPSEEK_SEARCH_BASE_URL` | `https://api.deepseek.com` | 覆盖 Base URL（测试指向本地假服务用） |
| `DEEPSEEK_SEARCH_TIMEOUT` | `15` | 单请求超时秒数 |
| `DEEPSEEK_SEARCH_MAX_RETRIES` | `2` | 408/429/5xx 与网络异常的最大重试次数 |
| `DEEPSEEK_NEWS_QUERY` | `今日新闻 要闻 财经 lang:zh after:{昨天}` | 「Google news 中文」源的 DeepSeek 检索词覆盖 |

## 成本与行为边界（诚实标注）

- 每次推送全量采集最多新增约 **12 次** Search 调用（每个搜索型源 1 次，空/失败不计费），
  即单日两次推送约 24 次 ≈ **$0.018/天** 量级；`/api/brief` 本身已有 5 分钟缓存，
  `/api/search` 另有同查询 5 分钟缓存。Fetch 暂只供 `/api/fetch` 与代码调用，
  推送采集路径不自动抓正文（`need_content=false`），避免静默膨胀推送体积与请求数。
- 未配置 Key 时本接入**零影响**：不发起任何 DeepSeek 请求，全部源行为与接入前一致，
  现有测试集在无 Key 环境仍按原口径运行。
- DeepSeek 搜索结果进入简报前，仍会经过既有的新鲜度闸门与敏感词闸门
  （见 `docs/sensitive_filter.md`），接入不绕过任何推送前检查。
- 已知限制：`site:` 纯算子查询是否被官方接受以真实账号联调为准——本实现对
  400/空结果都设计了 RSS 回退，最坏情况退化为接入前行为，不会丢整源数据。
  真实 Key 联调建议先跑 `GET /api/deepseek/status` → `GET /api/search?q=测试` 两条自检。
