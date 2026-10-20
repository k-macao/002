# AI 内容的港股 ETF 双标的口径

> 更新时间：**2026-10-20**  
> 实现：`sources._HK_ETF_CATALOG`、`sources._HK_ETF_COUNTERS`、`sources.hk_etf_pair()`、`sources.hk_etf_new_listings()`、`analyze_keyword_heatmap()`、`analyze_brief()`

## 输出规则

1. 每个 AI 情绪热力图关键词与每个 AI 板块机会方向，固定返回 **2 个港交所现有 ETF 的 HKD 柜台**。
2. 每只 ETF 同时返回 `code`、`name`、`benchmark`（对应指数 / 参考标的）、`mapping`（本次主题映射口径）、
   `counters`（HKD / CNY / USD 柜台代码）与 `counter_text`（如 `HKD 03599.HK / CNY 83599.HK / USD 41599.HK`）。
   只有 HKD 柜台的产品按 `{"HKD": code}` 返回，`multi_counter = false`。
3. 不再输出 A 股个股二选一组合。
4. 港股没有纯主题 ETF 时必须在 `mapping` 中写明“代理”，不能把相关行业 ETF 伪称为直接跟踪产品。
5. ETF 组合仅用于主题观察；不代表两只产品应等权配置，也不构成投资建议。
6. 「AI 情绪热力图」卡片头部固定展示本版新纳入的 8 只港交所 ETF（`new_listings`），每个热力格子标出双标的与多柜台代码；
   push HTML 与本地页面（`index.html` / `app.js`）同源渲染。

接口字段示例：

```json
{
  "keyword": "数据中心",
  "concept": "港股 ETF 双标的：02826.HK × 03140.HK",
  "etf_pair": [
    {
      "code": "02826.HK",
      "name": "Global X 中国云计算 ETF",
      "benchmark": "Solactive China Cloud Computing Index NTR",
      "mapping": "中国云计算 + 港美 AI",
      "counters": {"HKD": "02826.HK"},
      "counter_text": "HKD 02826.HK",
      "multi_counter": false
    },
    {
      "code": "03140.HK",
      "name": "华夏港美人工智能 ETF",
      "benchmark": "Solactive G2 AI 50 Select Index NTR",
      "mapping": "中国云计算 + 港美 AI",
      "counters": {"HKD": "03140.HK"},
      "counter_text": "HKD 03140.HK",
      "multi_counter": false
    }
  ],
  "etf_counters_text": "",
  "etf_has_multi_counter": false
}
```

## 主题默认组合

| AI 主题 | ETF 1 | ETF 2 | 映射口径 |
| --- | --- | --- | --- |
| AI 算力 | 03438.HK 恒生 AI 演进 ETF | 03140.HK 华夏港美人工智能 ETF | 恒生 AI 演进 + 港美 AI（算力主线） |
| 光通信 | 03599.HK 广发港交所科技及美国科技 100 指数 ETF | 03191.HK Global X 中国半导体 ETF | 港美科技 100 + 中国半导体代理；港股暂无纯光通信 ETF |
| 半导体 | 03191.HK Global X 中国半导体 ETF | 03569.HK 华泰盈科港交所韩国交易所半导体指数 ETF | 中国 + 韩国半导体（存储 / 设备） |
| 贵金属 | 02840.HK SPDR 金 ETF | 03566.HK 广发 MSCI 全球战略金属精选指数 ETF | 实物黄金 + 全球战略金属 |
| 美联储 / 利率 | 03436.HK 恒生美国国债 1-3 年 ETF | 03156.HK 博时美国国债 20+ 年 ETF | 短久期 + 长久期美国国债 |
| 地缘 | 02840.HK SPDR 金 ETF | 03197.HK 国泰海通全球战略资源 ETF | 黄金 + 全球战略资源（能源 / 金属）避险观察 |
| 医药 | 02820.HK Global X 中国生物科技 ETF | 02841.HK Global X 中国医疗科技 ETF | 生物科技 + 医疗科技 |
| 智驾 | 02845.HK Global X 中国电动车及电池 ETF | 03093.HK Fullgoal 恒生港美机器人 ETF | 电动车电池 + 港美机器人 |
| 地产 | 03447.HK 南方东英亚太精选 REIT ETF | 03001.HK Premia 中国地产美元债 ETF | 亚太 REIT + 中国地产美元债 |
| 消费 | 02806.HK Global X 中国消费品牌 ETF | 03040.HK Global X MSCI 中国 ETF | 中国消费品牌 + MSCI 中国 |
| 航天 | 03585.HK 国泰海通全球未来科技 ETF | 02834.HK iShares 纳斯达克 100 ETF | 全球未来科技 + 纳指 100 代理；港股暂无纯商业航天 ETF |
| 东盟（新增主题） | 03143.HK 大成银河港交所马来西亚交易所大型股 ETF | 03599.HK 广发港交所科技及美国科技 100 指数 ETF | 马来西亚大型股 + 港美科技 100（产业链转移观察） |

关键词会覆盖主题默认值：例如“原油”使用 03097.HK + 03175.HK 两只 WTI 原油期货 ETF；“数据中心”使用 02826.HK + 03140.HK；
“机器人 / robot”使用 03093.HK + 02807.HK；“白银 / 铂金 / 钯金”使用 02840.HK + 03566.HK、“稀土 / rare earth”使用 03566.HK + 03014.HK，
均因没有现行纯主题港股 ETF 而明确标注代理。“东盟 / 东南亚 / 马来西亚 / Malaysia / ASEAN”使用 03143.HK + 03599.HK。

## 2026-10-20 新纳入的 8 只港交所 ETF

按用户提供的港交所上市名单补入 `_HK_ETF_CATALOG` 与 `_HK_ETF_NEW_LISTINGS`，并在热力图头部与相关主题双标的中使用：

| 代码 | 产品 | 对应指数 / 参考标的 | 柜台（HKD / CNY / USD） | 进入的热力图主题 |
| --- | --- | --- | --- | --- |
| 03143.HK | 大成银河香港交易所马来西亚交易所大型股 ETF | Bursa Malaysia Large Cap Index | 03143 | 东盟 |
| 03599.HK | 广发香港交易所科技及美国科技 100 指数 ETF | HKEX Tech & US Tech 100 Index | 03599 / 83599 / 41599 | 光通信、东盟 |
| 03569.HK | 华泰盈科香港交易所韩国交易所半导体指数 ETF | HKEX KRX Semiconductor Index | 03569 | 半导体 |
| 03093.HK | Fullgoal 恒生港美机器人 ETF | Hang Seng HK-US Robotics Index | 03093 | 智驾、机器人关键词 |
| 03566.HK | 广发 MSCI 全球战略金属精选指数 ETF | MSCI Global Strategic Metals Select Index | 03566 / 83566 / 41566 | 贵金属、白银 / 铂金 / 钯金 / 稀土 |
| 03585.HK | 国泰海通全球未来科技 ETF | Global Future Technology Index | 03585 / 83585 / 41585 | 航天 |
| 03197.HK | 国泰海通全球战略资源 ETF | Global Strategic Resources Index | 03197 / 83197 / 09197 | 地缘 |
| 03438.HK | 恒生 AI 演进 ETF | Hang Seng AI Evolution Index | 03438 | AI 算力 |

口径说明：

- 指数名按产品全称转写，正式指数全称、成份与费率仍以上市文件 / 基金管理人资料为准；`_HK_ETF_CATALOG` 只用于主题归类，不做产品推荐。
- 3197 的美元柜台在港交所为 4 位数 `9197`，目录按 5 位标准写法存为 `09197.HK`；行情软件显示 `9197.HK` 属同一柜台。
- 多柜台产品在 heatmap 条目里带 `counter_text` 与 `etf_counters_text`，页面与 push 同步展示；HKD 单柜台产品不额外标注。
- 热力图返回 `new_listings`（8 项，含 `counters` / `multi_counter`），前端 `renderEtfPool()` 在网格上方渲染产品池。

## 核验来源

目录原有 25 只产品按 2026-10-03 可获得的港交所基金资料库、上市文件与基金管理人产品页核对，优先保留 HKD 柜台代码；
2026-10-20 新增的 8 只按用户提供的港交所上市名单与产品全称补入（代码、产品名、柜台一一对应），
指数全称待港交所基金资料库 / 上市文件链接补齐后回填，后续维护同样以官方资料为准：

- [港交所基金资料库：3436 恒生美国国债 1-3 年 ETF](https://ifp.hkex.com.hk/fund/BVB873)
- [港交所基金资料库：2807 中国机器人及人工智能 ETF](https://ifp.hkex.com.hk/fund/BPT301)、[3191 中国半导体 ETF](https://ifp.hkex.com.hk/fund/BPT300)、[2845 中国电动车及电池 ETF](https://ifp.hkex.com.hk/fund/BOW995)、[2826 中国云计算 ETF](https://ifp.hkex.com.hk/fund/BOJ918)
- [港交所上市文件：3140 华夏港美人工智能 ETF](https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0312/2026031200011.pdf)
- [港交所基金资料库：2806 中国消费品牌 ETF](https://ifp.hkex.com.hk/fund/BOW994)、[3040 MSCI 中国 ETF](https://ifp.hkex.com.hk/fund/BAR385)、[2834 iShares NASDAQ 100 ETF](https://ifp.hkex.com.hk/fund/BHG161)
- [港交所基金资料库：2800 盈富基金](https://ifp.hkex.com.hk/fund/AFG988)、[3195 恒生标普 500 ETF](https://ifp.hkex.com.hk/fund/BUP049)、[3081 价值黄金 ETF](https://ifp.hkex.com.hk/fund/ATY588)
- [Premia：3001 中国地产美元债 ETF](https://etfprod.premia-partners.com/etf/3001)
- [Global X：2820 中国生物科技 ETF](https://www.globalxetfs.com.hk/funds/china-biotech-etf/)、[2841 中国医疗科技 ETF](https://www.globalxetfs.com.hk/funds/china-medtech-etf/)、[2809 中国洁净能源 ETF](https://www.globalxetfs.com.hk/campaign/clean-energy/)
- [CSOP：3447 亚太精选 REIT ETF](https://www.csopasset.com/en/products/hk-crit)、[Samsung：3175 原油期货 ETF](https://www.samsungetfhk.com/en/product/3175/)
- [Solactive：2824 全球金矿精选 ETF 上市资料](https://www.solactive.com/articles/press-release-e-fund-launches-e-fund-hk-solactive-global-gold-miner-select-index-etf-tracking-the-solactive-global-gold-miner-select-index/)、[Global X：3014 铜矿 ETF 上市资料](https://www.prnewswire.com/apac/news-releases/global-x-launched-hong-kongs-first-copper-miners-etf-30149014-hk-302877721.html)
- [港交所：3156 美国国债 20+ 年 ETF 指数资料](https://www.hkex.com.hk/-/media/HKEX-Market/Services/Circulars-and-Notices/Participant-and-Members-Circulars/SEHK/2024/ce_SEHK_CT_020_2024_1.pdf)、[香港黄金 ETF 资料](https://www.hkex.com.hk/-/media/HKEX-Market/Products/Securities/Exchange-Traded-Products/Attachment/The-Rise-of-Gold-ETFs_vF.pdf)

产品仍可能更名、合并或终止上市；后续维护应先复核上述官方资料，再更新 `_HK_ETF_CATALOG`，不能只凭历史代码表继续输出。

## 验收

`HKEtfPairTest` 会检查：

- `_THEMES` 中每一个中英文关键词都能得到恰好两个不同 ETF；
- 代码统一符合 `NNNNN.HK`，并存在于核对后的目录；
- 每只产品都有非空对应指数；
- 原油、数据中心等精确覆盖生效；
- 热力图和板块机会均返回结构化 `etf_pair`；
- 8 只新纳入产品都在目录内、都能从某个主题 / 关键词双标的中取到；
- 多柜台代码（3599 / 3566 / 3585 / 3197）与 `etf_counters_text` 正确；
- 热力图返回 `new_listings`，push HTML 头部显示「本版新纳入 8 只港交所 ETF」；
- 旧 A 股个股组合不再出现在 AI 关键词解读中。
