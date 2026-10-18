# NewsNow 项目集成说明

> 历史文档（2026-09 初）：记录 12 → 18 源的接入过程。之后又按同一套「元信息 + 收集器 + 演示兜底」模式
> 加入了「政策发布 · 官方信息源」「全球政经媒体」「公民科技 · 政治透明度」三个板块，总数先扩展到 **49 个源**；
> 2026-10-03 再增加 20 个中英文 / 多类型财经社区，当前为 **六大板块 69 个源**。最新清单以 `sources.SECTIONS` / `SOURCE_META` 和 `docs/policy_sections.md` 为准。

## 概述

本次更新从 GitHub 上的 `ourongxing/newsnow` 项目集成了 6 个新的热搜/热点数据源，将总数据源数量从 **12 个增加到 18 个**。

## 新增的 6 个数据源

1. **知乎热榜** - 知乎热门话题 API
2. **抖音热搜** - 抖音热搜榜单 API（需 cookie）
3. **微博实时热搜** - 微博实时热搜表格解析
4. **虎扑热搜** - 虎扑社区热帖正则匹配
5. **AI Hot** - AI 热点新闻聚合 API
6. **Google news 中文** - Google News RSS 抓取

## 技术实现

### 数据来源

所有新增源的抓取逻辑均参考 `ourongxing/newsnow` 项目的 TypeScript 实现，并翻译为 Python（仅使用标准库）：

- **知乎热榜**: `https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=20&desktop=true`
- **抖音热搜**: `https://www.douyin.com/aweme/v1/web/hot/search/list/` (需先从 login.douyin.com 获取 cookie)
- **微博实时热搜**: `https://s.weibo.com/top/summary?cate=realtimehot` (HTML 表格解析)
- **虎扑热搜**: `https://bbs.hupu.com/topic-daily-hot` (正则表达式匹配)
- **AI Hot**: `https://aihot.virxact.com/api/public/items?mode=all&take=30`
- **Google news 中文**: `https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans` (RSS 解析)

### 架构设计

为支持不同类型的数据源，`sources.py` 现在支持两种抓取模式：

1. **原有 12 个财经源**：使用 `channel` + `origin` 字段，通过 rebang.vip 聚合通道抓取
2. **新增 6 个热搜源**：使用 `collector` 字段，调用自定义收集器函数

#### SOURCE_META 结构

```python
# 原有财经源
{"name": "华尔街见闻 快讯", "origin": "wallstreetcn.com", "channel": "https://www.rebang.vip/..."}

# 新增热搜源
{"name": "知乎热榜", "collector": "zhihu"}
```

#### 收集器函数

每个新增源都有对应的收集器函数（如 `_collect_zhihu()`），在 `_COLLECTORS` 字典中注册：

```python
_COLLECTORS = {
    "zhihu": _collect_zhihu,
    "douyin": _collect_douyin,
    "weibo": _collect_weibo,
    "hupu": _collect_hupu,
    "aihot": _collect_aihot,
    "google_news": _collect_google_news,
}
```

#### collect_one() 逻辑

```python
def collect_one(name, limit):
    meta = find_meta(name)
    
    # 新增源：使用自定义收集器
    if "collector" in meta:
        collector_func = _COLLECTORS[meta["collector"]]
        try:
            items = collector_func(limit)
            if items: return items
        except ...:
            pass
        return _demo_items(name)  # 兜底
    
    # 原有源：使用聚合通道
    if "channel" in meta:
        try:
            items = _parse_channel(...)
            if items: return items
        except ...:
            pass
    
    return _demo_items(name)  # 兜底
```

### 关键实现细节

#### 1. 抖音热搜需要 Cookie

```python
def _collect_douyin(limit):
    # 先从 login.douyin.com 获取 cookie
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    opener.open("https://login.douyin.com/", timeout=TIMEOUT)
    
    # 用 cookie 请求热搜 API
    url = "https://www.douyin.com/aweme/v1/web/hot/search/list/..."
    req = Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ...
```

#### 2. 微博热搜需要 HTML 表格解析

新增 `WeiboTableParser` 类（继承 `HTMLParser`），专门解析微博热搜的表格结构：

```python
class WeiboTableParser(HTMLParser):
    # 解析 <table> -> <tbody> -> <tr> -> <td class="td-02"> -> <a href>title</a>
    ...
```

#### 3. Google news 中文使用 RSS

> 后续更新（2026-10-17 DeepSeek Search API）：该源与仓库全部站内检索型数据源已优先改用 DeepSeek 自研搜索 API（需 `DEEPSEEK_API_KEY`），下述 Google News RSS 仅为未配置/搜索失败时的回退通道，详见 `docs/deepseek_search.md`。

Google News 提供了稳定的 RSS 订阅源，通过 `xml.etree.ElementTree` 解析标题和链接。

#### 4. 虎扑热搜使用正则表达式

```python
def _collect_hupu(limit):
    url = "https://bbs.hupu.com/topic-daily-hot"
    raw = _fetch(url)
    regex = re.compile(
        r'<li class="bbs-sl-web-post-body">[\s\S]*?'
        r'<a href="(/[^"]+?\.html)"[^>]*?class="p-title"[^>]*>([^<]+)</a>'
    )
    for match in regex.finditer(raw):
        path, title = match.groups()
        ...
```

## 兜底机制

所有新增源都内置了演示数据（`_DEMO` 字典），当网络不可用或抓取失败时自动回退：

```python
_DEMO = {
    ...
    "知乎热榜": [
        "如何看待 2026 年高考录取分数线公布？",
        "为什么现在的年轻人越来越喜欢独居？",
        ...
    ],
    "抖音热搜": [...],
    ...
}
```

这保证了在 GitHub Actions / CI 沙箱等无外网环境下仍能正常生成简报。

## 文件变更清单

### `sources.py`
- 新增 `import json` 和 `http.cookiejar` 相关导入
- `SOURCE_META` 增加 6 个新源定义
- `_DEMO` 增加 6 个新源的演示数据
- 新增 6 个收集器函数：`_collect_zhihu()`, `_collect_douyin()`, `_collect_weibo()`, `_collect_hupu()`, `_collect_aihot()`, `_collect_google_news()`
- 新增 `WeiboTableParser` 类
- 新增 `_COLLECTORS` 字典
- `collect_one()` 函数支持自定义收集器
- `collect_all()` 自动遍历全部 18 个源
- 更新模块文档字符串

### `app.js`
- `fallbackSources` 数组从 12 个增加到 18 个
- `stockWords` 数组增加 "热搜", "热榜" 关键词

### `index.html`
- 数据源统计数字从 12 更新为 18

### `test_sources.py`
- `test_twelve_sources` 重命名为 `test_eighteen_sources`，断言 18 个源
- `test_every_source_has_origin_and_channel` 改为 `test_every_source_has_origin_or_collector`，支持两种模式
- `test_collect_one_falls_back_offline` 增加对新增源的测试
- `test_build_html_contains_sources_and_items` 更新断言为 18 个源

## 测试结果

```
$ python3 test_sources.py
test_build_html_contains_sources_and_items ... ok
test_escapes_html ... ok
test_clean_title_strips_time_prefix ... ok
test_collects_anchors ... ok
test_parse_channel_filters_by_origin ... ok
test_collect_one_falls_back_offline ... ok
test_demo_fallback ... ok
test_eighteen_sources ... ok
test_every_source_has_origin_or_collector ... ok

----------------------------------------------------------------------
Ran 9 tests in 0.315s

OK
```

## 使用说明

### 本地运行

```bash
# 启动服务（包含全部 18 个源）
PUSHPLUS_TOKEN=你的token python3 server.py
```

### API 接口

- `GET /api/sources` - 返回 18 个源的名称列表
- `GET /api/brief` - 抓取全部 18 个源的数据（带 5 分钟缓存）
- `POST /api/run` - 手动触发抓取并推送至 PushPlus

### 手动测试

```bash
# 测试单个源
python3 -c "import sources; print(sources.collect_one('知乎热榜', limit=5))"

# 测试全部源
python3 sources.py
```

## 已知限制

1. **抖音热搜**：需要有效的 cookie，如果 cookie 过期或被封禁会回退到演示数据
2. **微博热搜**：依赖特定的 Cookie（已硬编码），可能需要定期更新
3. **Google news 中文**：通过 Google News RSS 抓取，如果不可用会回退到演示数据
4. **所有新增源**：在网络受限环境（如 GitHub Actions 沙箱）下会自动回退到演示数据

## 未来改进

- [ ] 为微博热搜实现自动 Cookie 刷新机制
- [ ] 为抖音热搜增加 Cookie 持久化和自动更新
- [ ] 考虑为新增源也接入 rebang.vip 聚合通道（如果可用）
- [ ] 增加源健康度监控和自动降级策略

## 参考

- NewsNow 项目：https://github.com/ourongxing/newsnow
- 原始 TypeScript 实现：`/server/sources/{zhihu,douyin,weibo,hupu,aihot,zaobao}.ts`（Google News 为 RSS 抓取）
