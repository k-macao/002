# 六大板块抓取：财经社区 · 政策发布 · 全球政经媒体 · 公民科技

> 更新时间：**2026-10-03**
> 入口：`sources.SECTIONS`（板块目录）、`sources.SOURCE_META[*]["section"]`（源的板块归属）、
> `sources.collect_section(key)`（按板块抓取）、`GET /api/sections`、`GET /api/brief?section=<key>`。

## 一、为什么按「板块」抓

原来的 18 个源只有两类（财经快讯 12 个 + 热搜热点 6 个），全部塞在一张平铺列表里。
系统随后加入政策 / 政治新闻源，并在 2026-10-03 再加入 20 个中英文 / 多类型财经社区；这些来源的抓取方式、更新频率与用途都不一样，因此把「板块」做成一等公民：

| 板块 key | 标签 | 源数 | 抓取方式 | 参考项目 |
| --- | --- | --- | --- | --- |
| `finance` | 财经快讯 | 12 | rebang.vip 聚合通道（`channel` + `origin`） | — |
| `trending` | 热搜热点 | 6 | 自定义收集器（`collector`） | ourongxing/newsnow |
| `community` | 财经社区 · 中英文观点 | 20 | Reddit / 论坛 Atom·RSS；无稳定 RSS 的公开社区优先经 DeepSeek Search API（2026-10-17，需 `DEEPSEEK_API_KEY`）检索，回退 Google News RSS | 中文 8 源 + 英文 12 源，覆盖 18 种投资类型 |
| `policy` | 政策发布 · 官方信息源 | 15 | 境内列表页 + 链接正则（`page` + `pattern`）+ 中国新闻社时政 RSS；境外官方 RSS / Atom（`feed`） | changwu/china-policy-sites · angelinajh/regtech-policy-tracker |
| `world` | 全球政经媒体 | 12 | 官方 RSS（`feed`） | edoardottt/news-list |
| `civic` | 公民科技 · 政治透明度 | 4 | 开放 JSON API（`collector`）/ RSS（`feed`） | g0v · keepittechie/equitystack · GovTrack |

板块贯穿三处：

1. **抓取**：`collect_all()` 并发抓全部源（`BRIEF_FETCH_WORKERS`，默认 8 线程，顺序仍按 `SOURCE_META`）；
   `collect_section("policy")` 只抓一个板块，便于单独调试或按板块定时。
2. **接口**：`GET /api/sections` 返回 `[{key, label, note, sources, count}]`；`GET /api/brief?section=world`
   只返回该板块的抓取结果（同一份 5 分钟缓存），未知板块 404 并列出可用 key。
3. **简报**：正文最后的「全网快讯」按板块分组，每组一行小标题 + 条数，编号全表连续，仍不标注具体来源；
   `analyze_brief()` 额外返回 `sections`（各板块条数 / 有内容的源数）。

## 二、财经社区 · 中英文观点（20 源）

社区板块通过 `language` 与 `community_type` 元数据显式记录语言和投资类型。中文 8 源包括雪球讨论、东方财富股吧、集思录、淘股吧、富途牛牛圈、老虎社区、TradingView 中文观点、知乎财经话题；英文 12 源包括 Reddit 的综合投资 / 股票 / 价值 / 基本面 / 股息 / 期权 / 量化 / ETF 八类社区，以及 Bogleheads、Stocktwits、QuantConnect、Elite Trader。

- Reddit 与支持订阅的论坛直接读取 Atom / RSS。
- 中文社区和没有稳定公开 RSS 的社区，沿用项目中 Reuters 已使用的站内检索方式：配置 `DEEPSEEK_API_KEY` 后优先用 DeepSeek Search 拼 `site:<domain> lang:<zh|en> after:<7 天前>`，未配置/失败回退 Google News RSS 的 `site:<domain> when:7d`（见 `docs/deepseek_search.md`）。
- 单站限流、前端渲染或 RSS 暂时失效时，只回退该源的 `_DEMO` 标题；其余 68 源不受影响。
- 社区标题与新闻源使用同一套中英文主题、多空与政策词库，会进入 AI 总结、情绪热力图与板块机会统计；社区观点只作为情绪样本，不当成已核验事实。

可用 `GET /api/brief?section=community` 单独查看 20 源结果，或用 `GET /api/sections` 核对完整清单与数量。

## 三、政策发布 · 官方信息源（15 源）

### 境内（changwu/china-policy-sites 站点清单）

这些政府站点**没有 RSS**，但「政策发布」栏目都是服务端渲染的列表页，条目链接有稳定特征，
用 `LinkCollector` 收集全部 `<a href>` 后按正则筛选即可（相对链接用 `urljoin` 补全，短锚文本如「解读」「视频」跳过）。

| 源 | 列表页 | 条目链接特征（`pattern`） |
| --- | --- | --- |
| 国务院 最新政策 | `https://www.gov.cn/zhengce/zuixin/` | `gov.cn/(zhengce\|lianbo\|yaowen\|zhuanti)/.*content_\d+.htm` |
| 国务院 政策解读 | `https://www.gov.cn/zhengce/jiedu/` | 同上 |
| 国务院 政务联播 | `https://www.gov.cn/lianbo/` | 同上（部委发文 / 国新办发布会 / 部门动态混排，按发稿时间） |
| 发改委 政策发布 | `https://www.ndrc.gov.cn/xxgk/zcfb/tz/` | `ndrc.gov.cn/xxgk/zcfb/.+/t\d{8}_\d+.html`（排除 `/xxgk/jd/` 解读） |
| 财政部 政策发布 | `https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/` | `mof.gov.cn/.+/t\d{8}_\d+.htm`（各司局子域） |
| 商务部 政策发布 | `https://www.mofcom.gov.cn/zwgk/zcfb/index.html` | `mofcom.gov.cn/zwgk/zcfb/art/\d{4}/art_\w+.html` |
| 证监会 新闻发布 | `https://www.csrc.gov.cn/csrc/xwfb/index.shtml` | `csrc.gov.cn/csrc/c(100028\|106311\|100039)/c\w+/content.shtml`（要闻 / 头条 / 政策解读；旧栏目 `c100028/common_list.shtml` 已停更） |
| 香港特区政府 新闻公报 | `https://www.info.gov.hk/gia/rss/general_zh.xml` | 官方 RSS（繁体） |
| 中国新闻社 时政 | `https://www.chinanews.com.cn/rss/china.xml` | 官方 RSS（时政新闻；中央 / 部委发文与人事任免的官媒口径，计入「官媒 / 官方口径」舆情） |

> 注意：`https://www.gov.cn/zhengce/zuixin.htm`、`/lianbo/bumen/` 会 302 回首页，必须用上面带斜杠的目录地址。
> 央行（pbc.gov.cn）有 JS 反爬壳，标准库直连拿不到列表，这里不接；央行发文会经「国务院 政务联播」转载进入简报。
> 清单里的其他部委（市场监管总局、能源局、外汇局、网信办、工信部…）都可以照同样方式加一行 `page=` + `pattern=`。

### 境外（angelinajh/regtech-policy-tracker 的做法：直接订阅官方 RSS）

| 源 | 订阅地址 | 格式 |
| --- | --- | --- |
| 美联储 新闻稿 | `https://www.federalreserve.gov/feeds/press_all.xml` | RSS |
| 欧洲央行 新闻稿 | `https://www.ecb.europa.eu/rss/press.html` | RSS（新闻稿 / 讲话 / 发布会） |
| 美国 SEC 新闻稿 | `https://www.sec.gov/news/pressreleases.rss` | RSS（SEC 要求自动化访问声明 UA，见 `headers`） |
| 美国联邦公报 总统文件 | `https://www.federalregister.gov/api/v1/documents.rss?conditions[type][]=PRESDOCU&order=newest` | RSS（行政令 / 总统公告 / 国家紧急状态延续） |
| 英国财政部 GOV.UK | `https://www.gov.uk/government/organisations/hm-treasury.atom` | Atom |
| 英国 FCA 新闻 | `https://www.fca.org.uk/news/rss.xml` | RSS |

`_parse_feed()` 同时支持 RSS 2.0 / RSS 1.0 / Atom：`item|entry` → `title` + `link`（Atom 取 `rel="alternate"` 的 `href`），
bytes 入参交给 XML 解析器按声明 encoding 解码，标题 CDATA / 实体自动还原，同题去重。
regtech-policy-tracker 里的其他订阅（EDPB、GOV.UK 关键词检索 Atom 等）都可以直接加一行 `feed=`。

## 四、全球政经媒体（12 源，edoardottt/news-list 清单）

news-list 只给站点 / 通讯录，不给 RSS；这里选清单中有官方 RSS 的政治 / 地缘 / 经济头部媒体：

| 源 | 订阅地址 |
| --- | --- |
| Reuters 路透 | 路透官方 RSS 已停止：优先用 DeepSeek Search 检索 `site:reuters.com lang:en after:<1 天前>`，回退 Google News RSS `site:reuters.com when:1d`，标题去掉「 - Reuters」后缀（`strip_suffix`） |
| Bloomberg 政治 / 经济 | `https://feeds.bloomberg.com/politics/news.rss` · `https://feeds.bloomberg.com/economics/news.rss` |
| Financial Times | `https://www.ft.com/rss/home`（国际版首页） |
| 纽约时报 国际 | `https://rss.nytimes.com/services/xml/rss/nyt/World.xml` |
| 华盛顿邮报 政治 | `https://feeds.washingtonpost.com/rss/politics` |
| POLITICO | `https://rss.politico.com/politics-news.xml` |
| Foreign Policy | `https://foreignpolicy.com/feed/` |
| The Diplomat | `https://thediplomat.com/feed/` |
| 经济学人 财经 | `https://www.economist.com/finance-and-economics/rss.xml` |
| 日经亚洲 | `https://asia.nikkei.com/rss/feed/nar` |
| 南华早报 | `https://www.scmp.com/rss/91/feed` |

## 五、公民科技 · 政治透明度（4 源）

| 源 | 数据 | 条目格式 |
| --- | --- | --- |
| g0v 立法院議案 | g0v 生态「立法院 API v2」`https://ly.govapi.tw/v2/bills?limit=N`（按最新進度日期倒序） | `〔議案狀態〕議案名稱（提案單位/提案委員）` → `ppg.ly.gov.tw` 議案頁 |
| EquityStack 政策承诺 | `https://equitystack.org/api/promises`（Promises → Actions → Outcomes 追踪） | `[状态] 承诺标题（议题 · 总统）` → `equitystack.org/promises/<slug>` |
| EquityStack 法案追踪 | `https://equitystack.org/api/future-bills`（与承诺挂钩的国会法案，Congress.gov 数据） | `法案号｜法案名（状态 · 最新动作）` → congress.gov |
| GovTrack 国会立法 | `https://www.govtrack.us/events/events.rss?feeds=misc:activebills2`（重大立法动态） | RSS 原标题 |

g0v 组织本身的项目（vTaiwan、揪松網、people-in-news…）多为网站 / 社群工具，没有稳定的公开条目接口，
`g0v.news`（Medium）feed 目前 500，故以其生态内的立法院 API 作为入口；后续有稳定 feed 时同样一行 `feed=` 即可接入。

## 六、抓取链路与兜底

```
collect_one(name)
  ├─ collector → _COLLECTORS[name](limit)           # 热搜 / 公民科技 JSON
  ├─ feed      → _collect_feed(url, headers, strip)  # RSS / Atom
  ├─ page      → _collect_page(url, pattern)         # 政府列表页
  ├─ channel   → _parse_channel(channel, origin)     # rebang.vip 聚合
  └─ 任一步抛 _FETCH_ERRORS（网络 / 超时 / 解析）或结果为空 → _DEMO[name] 演示数据
```

- 每个源都有 `_DEMO` 兜底；社区兜底标题明确是离线演示讨论，不冒充实时帖子，保证离线 / CI 沙箱也能出完整简报。
- `_fetch()` 按「响应头 charset → `<meta charset>` / XML encoding → UTF-8」解码，GB2312/GBK 统一用 gb18030。
- 单源超时 8 秒；69 源默认 8 线程并发，任一社区限流只触发该源兜底，不阻塞其他板块。

## 七、对分析栏目的影响

英文财经社区以及政策 / 媒体板块的标题会共同进入分析，因此三套词库都补了英文词，并统一使用 **ASCII 词按单词边界匹配**
（末尾 `*` 允许后缀，大小写不敏感）：

- 主题词库 `_THEMES`：`Fed / Federal Reserve / rate cut* / ECB / BOJ`（美联储）、`Iran / NATO / sanction* / Trump / Ukraine`（地缘）、
  `chip* / semiconductor* / Nvidia`（半导体）、`gold / oil / crude / rare earth*`（贵金属）等；`AI` 不再误命中 `SAID`，`gold` 不再误命中 `Goldman`。
- 多空词库 `_BULLISH_EN / _BEARISH_EN`：`surge* / rall* / record high / stimulus …` vs `plunge* / sanction* / tariff* / crisis / war …`。
- 政策维度 `_POLICY_BUCKETS` 与鹰鸽词库 `_HAWKISH / _DOVISH`：补 `Federal Reserve / interest rate* / treasury / tax / SEC / regulation* /
  executive order* / NATO / Congress …` 与 `restrictive / crackdown / ban / probe*` vs `relief / bailout / tax cut* / exempt* / 促进 / 优惠`。

演示数据实测：政策面 40 源命中 · 115 条提及，取向偏鹰（地缘与贸易政策、资本市场监管最热）；
中文用例（`PolicySectionTest`）全部保持原判定。

## 八、如何再加一个源

1. 在 `sources.SOURCE_META` 对应板块下加一行：
   - RSS / Atom：`{"name": "…", "section": "policy", "feed": "https://…/rss.xml"}`（可选 `headers` / `strip_suffix`）
   - 列表页：`{"name": "…", "section": "policy", "page": "https://…/zcfb/", "pattern": r"…/t\d{8}_\d+\.html"}`
   - 自定义：`{"name": "…", "section": "civic", "collector": "xxx"}` + 在 `_COLLECTORS` 注册 `_collect_xxx(limit)`
2. 在 `_DEMO` 补 1 条以上兜底标题（`test_demo_fallback` 会检查）。
3. 更新 `test_sixty_nine_sources_in_six_sections` 里的板块计数，跑 `python3 test_sources.py`。
4. 源名不得与任何板块标签相同（`test_section_catalog` 会检查），否则「全网快讯」分组标题会暴露来源。
