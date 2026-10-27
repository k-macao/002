# AI 独立开发雷达 · 「AI 提」

> 新增独立栏目：**AI 独立开发雷达**。方法参考 [susumr/radar-dashboard](https://github.com/susumr/radar-dashboard)（MIT 之外的公开仓库，只借鉴结构与模块定义，代码在本仓库用标准库重写）。
>
> 它回答的是另一个问题：**不买股票的人今天能做什么产品**。19 个独立开发 / 产品信号源 → 7 个模块 + 1 条心法；加上「AI 提」——同一批条目可以直接拼成提示词，交给任意大模型生成自己的日报。
>
> 静态快照：`index.html` 里的栏目内容随最近一次采集生成；服务运行时由 `GET /api/radar` 重算。

---

## 一、为什么放在页面上，而不是塞进别的卡片

独立开发信号与证券信号**没有可换算的公共单位**：GitHub star 不等于销量，Product Hunt 票数不等于收入，V2EX 回复数不等于需求强度。把两者混在一张卡片里，读者会误以为「热度 = 机会」。

所以本栏目：

- 在页面上是**独立的 `<section id="indieRadarSection">`**，在推送正文里是**独立的卡片**（排在「AI 板块机会」之后、「大 V 分析」之前）；
- 有自己的计数口径（条信号 / 源有数据 / 模块有料），不与「AI 每日总结」的多空信号混算；
- 素材不足的模块**整行不渲染**，不写占位、不借旧数据。

---

## 二、7 个模块 + 心法

| 编号 | 模块 | 读者要拿到什么 |
|---|---|---|
| 1 | 今日机会 | 信号 → 为什么现在能做 → 建议怎么做 → 变现路径 |
| 2 | 今日赚钱案例 | 做什么 → 收入 → 流量 → 变现 → 能否复制 |
| 3 | 今日产品灵感 | 一句具体的产品 idea，挂在一条真实信号上 |
| 4 | 今日增长技巧 | 可执行的具体动作，不是抽象建议 |
| 5 | 今日工具 | 值得试的工具，一句话说清解决什么问题 |
| 6 | 今日踩坑 | 值得警惕的教训，必须挂在真实信号上 |
| 7 | 今日数据信号 | 趋势观察 + 判断，**只引用当日计数与热度** |
| 心 | 今日心法 | 一句话方法 → 适用场景 → 分步骤做法 → 用当天数据推演一遍 |

心法**不是固定方法库**：`wisdom()` 从当天命中的模块、条目数与头条条目反推，
`derivation` 字段写清「当天 N 条信号里，M 个模块有料」——没有素材就返回 `status="missing"`，不硬凑。

### 归模块的规则

`MODULE_RULES` 是一组正则（`_RULE_RX` 编译一次），按「命中词最多的模块」独占分配：
一条信号只能落进一个模块，同分按模块顺序。独占是为了避免同一条被三个模块各说一遍，读起来像料多。

英文关键词一律带 `\b` 词边界（`idea` 不能命中 `ideal`，`tool` 不能命中 `toolkit`）；
金额用 `\$\s?\d` 而不是双重转义的 `\\$\\d`（后者永远不命中，写错过一次）。

`data_signals` 模块**不跑关键词筛选**——它由当日真实计数生成（源数、条数、热度最高条目），
每条都带一句判断，并明确写「热度是 star / 回复 / 票数这类公开计数，不等于收入或留存」。

---

## 三、19 个信号源

| 类别 | 源 | 需要 Key |
|---|---|---|
| 代码榜 | GitHub 总榜 / JS 榜 / 中文榜 | — |
| 社区 | V2EX（分享创造 / 分享发现 / 程序员）、即刻·AI探索站 / AI讨论组 / 工程师的日常 | — |
| 中文媒体 | 36氪、少数派、开源中国 | — |
| 应用商店 | App Store 中国 / 美国 / 日本 / 台湾 / 韩国 新区 | — |
| 海外 | IndieHackers（Algolia 公开索引） | — |
| 需授权 | Product Hunt | `PH_TOKEN`（或 `PRODUCT_HUNT_TOKEN`） |
| 需授权 | trustmrr（已验证收入 / MRR） | `TRUSTMRR_API_KEY` |
| 需授权 | Reddit r/SideProject | `REDDIT_CLIENT_ID` |

**Key 缺失时状态是 `skipped`，不是 `error`，也不会用别的源顶替。**
上游 README 把 Product Hunt / trustmrr 的「收入验证」当核心信息，本仓库同样不拿 GitHub star 冒充收入：
`data_signals` 会直接写出「未配置的几个源里，Product Hunt / trustmrr 的『收入验证』信息最难替代」。

### 采集的诚实体征

`collect_signals()` 返回的 `sources[]` 每个源都有 `status`：

| status | 含义 |
|---|---|
| `ok` | 抓到条目 |
| `empty` | 抓通了但没有新条目 |
| `skipped` | 需要 Key 而没配 |
| `error` | 请求失败（TLS / DNS / 4xx 5xx），`note` 写原因 |
| `timeout` | 超过整轮预算没返回（`RADAR_BUDGET`，默认 18 秒） |

- 单个源失败**不会中断**其它源，也不会抛异常；
- 整轮都失败时 `analyze()` 的 headline 是「今日雷达没有取到信号，各模块留空（不补旧数据、不编条目）」；
- 采集结果缓存 30 分钟（`CACHE_TTL = 1800`），推送多档位渲染不会重复打 19 个源。

沙箱实测（无外网）：3 个 GitHub 榜 `ok`（18 条），3 个需 Key 的源 `skipped`，其余 `error`——
页面与推送照样出卡片，状态如实标注。

---

## 四、「AI 提」

`AI_PROMPT` 从上游 `hermes-integration/hn-daily-radar-SKILL.md` 的 Cron Prompt Template 改写：
保留模块定义与「心法必须从当天数据反推」，去掉微信小程序方向与 Telegram 输出要求，
换成本仓库需要的中文输出与 JSON schema。

两条使用路径：

```bash
python3 indie_radar.py --prompt        # 打印当日提示词（含编号条目、来源、热度、链接）
curl -s localhost:4173/api/radar?format=prompt
```

提示词里写死的硬约束：

- 不编造条目、项目名、收入、涨粉或下载量，数字只能来自给定条目，没有就写「未披露」；
- 素材不足的模块跳过，JSON 里留空数组，不凑数；
- 每条都要能指回一条真实信号；
- 不写投资建议；不把 GitHub star 当成收入，不把 Product Hunt 票数当成销量。

---

## 五、代码位置

| 文件 | 作用 |
|---|---|
| `indie_radar.py` | 纯标准库：19 源采集、7 模块规则成稿、心法反推、`AI_PROMPT`、页面区块 HTML |
| `sources.py` → `analyze_indie_radar()` | 包装器 + 15 分钟进程内缓存；采集整体异常时返回带 `error` 的空报告而不是抛错 |
| `sources.py` → `build_html()` → `_render_indie_radar()` | 推送卡片；`deep_level` 2/1/0 = 每模块 3 条 / 2 条 + 心法 / 只留栏目与今日结论 |
| `server.py` → `GET /api/radar`（兼容 `/api/indie-radar`） | 结构化结果；`?format=prompt` 直接给提示词；5 分钟缓存 |
| `index.html` / `app.js` / `styles.css` | `#indieRadarSection` 静态快照 + `window._radarReload` 动态覆盖 |
| `test_indie_radar.py` / `test_sources.py` / `test_server.py` | 解析、独占分配、诚实缺口、提示词、页面区块、推送降级、接口 |

### 开关

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `INDIE_RADAR` | `1` | `0` 时推送正文整段跳过该栏目（不联网、不占容量）；页面与接口不受影响 |
| `RADAR_TIMEOUT` | `12` | 单源抓取超时（秒） |
| `RADAR_BUDGET` | `18` | 整轮采集总预算（秒），超时源标 `timeout` |

---

## 六、`GET /api/radar` 响应（节选）

```json
{
  "column": "AI 独立开发雷达",
  "as_of": "2026-10-28",
  "upstream": "https://github.com/susumr/radar-dashboard",
  "counts": {"items": 18, "sources_ok": 3, "sources_total": 19, "sources_skipped": 3,
             "sources_error": 13, "modules_filled": 4},
  "headline": "18 条信号、4 个模块有料、头条「makecindy/cindy」、心法：先看「今日机会」，再看别人怎么收钱",
  "modules": {
    "opportunities": [{"title": "makecindy/cindy", "url": "https://github.com/makecindy/cindy",
                       "source": "GitHub 总榜", "points": 1446, "matched": ["开源"]}],
    "money_cases": [], "ideas": [], "growth": [], "tools": [], "pitfalls": [],
    "data_signals": [{"observation": "3 个源有信号（共 19 个接入源，3 个未配置、13 个抓取失败或超时）",
                      "judgment": "接入面决定了雷达的视野：……"}]
  },
  "module_status": {"opportunities": {"status": "live", "count": 3}, "money_cases": {"status": "missing", "reason": "当日没有条目命中该模块"}},
  "wisdom": {"status": "live", "method": "先看「今日机会」，再看别人怎么收钱", "steps": ["…"], "derivation": "…"},
  "sources": [{"key": "github", "name": "GitHub 总榜", "status": "ok", "note": "6 条"}],
  "disclaimer": "信号整理，不是投资建议；收入与增长数字以原文为准，本模块不推算、不补值。"
}
```

`module_status` 是给读者看的诚实缺口：`live` / `missing`（附原因）/ `empty`。

---

## 七、边界

- **不推算收入、增长、下载量**。原文没写就写「未披露」；`DISCLAIMER` 在每个接口与卡片尾部。
- **不做投资建议**。栏目内容面向产品与独立开发，不与证券多空信号混算。
- **不冒充上游**：模块定义参考上游公开仓库，实现与措辞是本仓库自己的；
  上游有、本仓库没有的能力（需 Key 的源）如实标「未配置」，不用替代数据填充。
- **不因采集失败而糊弄**：整轮失败时给「没有取到信号」，不给昨天的条目冒充当天的。
