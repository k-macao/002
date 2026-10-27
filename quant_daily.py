#!/usr/bin/env python3
"""「AI 量化日报」——面向 AI Agent 的日频量化日报执行引擎。

一句话：把「触发 → 采集 → 五维分析 → 策略信号 → 风险 → 归档」拆成原子动作，
每个动作都对应一次可调用的工具请求，交易日收盘后自动跑完并产出一份标准 Markdown 日报。

设计边界
--------
1. **不编造数值**。任何一个指标没有拿到当日数据，一律写「数据暂缺」并标 ⚠️，
   绝不套用上一日或估计值冒充实测；能算的才写数字，并注明是原值还是代理值（proxy）。
2. **可复算**。所有评分、仓位、风险阈值都写死在常量里（见 ``SCORING`` / ``RISK_RULES``），
   同样输入必然得到同样输出，便于回溯「为什么当时给这个信号」。
3. **零第三方依赖**。纯标准库；行情走仓库既有的东方财富 / 腾讯证券公开接口
   （``sources.py`` 里已实现主备切换），其余数据类由调用方（Agent 工具）通过
   ``--input`` 传入 JSON 数据包，接口见 ``BUNDLE_SCHEMA``。
4. **口径诚实**。北向资金实时数据自 2024 年 8 月起停止披露，本引擎沿用仓库口径：
   该字段不假装有数，改用主力资金与两融观察，并在日报里写明。

对外接口
--------
    plan()                     执行方案（规则、数据源、原子动作、兜底）
    trading_day(as_of)         交易日判定（周末 + 固定公历休市 + 可外掛农历假期）
    live_bundle(as_of)         自动采集能采的部分（行情 / 风格 / 估值 / 舆情 / 政策）
    demo_bundle(as_of)         内置示例数据包（离线、确定性，用于模板预览与回归测试）
    analyze(bundle, as_of)     五维分析 + 策略 + 风险 + 摘要 + Markdown
    daily(...) / run(...)      一次完整执行（含归档），返回结构化结果
    render_markdown(report)    标准日报 Markdown
    build_html_section(report) 推送卡片 HTML（内联样式，供 sources.build_html 调用）

命令行
------
    python3 quant_daily.py                       # 采集当日数据 → 生成 → 归档到 reports/
    python3 quant_daily.py --demo                # 用示例数据包渲染完整模板（离线预览）
    python3 quant_daily.py --input data.json     # 用 Agent 工具返回的数据包生成
    python3 quant_daily.py --date 2026-10-26 --no-archive --json
    python3 quant_daily.py --print-plan          # 只打印原子动作清单

退出码：0 正常；3 非交易日（按兜底规则跳过）；4 数据包不可解析。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

COLUMN_TITLE = "AI 量化日报"
VERSION = "1.0.0"
CST = timezone(timedelta(hours=8))
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
DEMO_AS_OF = "2026-10-26"

DISCLAIMER = (
    "本日报由规则引擎按当日可获取的数据自动生成；标注「数据暂缺」的条目表示当日未取得数值，"
    "未用前值或估计值顶替。所有数值均可按文档口径复算，不构成投资建议。"
)

DELIVERY = {
    "trigger": "15:30",
    "deadline": "16:00",
    "timezone": "北京时间 UTC+8",
    "timeline": [
        {"market": "A 股", "trigger": "15:30", "deadline": "16:00", "note": "收盘后 30 分钟内产出"},
        {"market": "港股", "trigger": "16:30", "deadline": "17:15", "note": "收盘（16:00）后 30 分钟"},
        {"market": "美股", "trigger": "次日 05:00", "deadline": "次日 06:00", "note": "夏令时收盘 04:00 后 1 小时"},
    ],
}

EXECUTION_RULES = [
    "所有结论带数值、带对比：环比（前值 → 当日）或历史分位（近 10 年 / 近 5 日）。",
    "信号只用看多 / 中性 / 看空三档，不写「或」「可能偏强」这类模糊表述。",
    "异常数据（单指标偏离 ±20%、缺失后取前值）自动 ⚠️ 标注，并写进日报的数据体检表。",
    "数据源故障自动切备用源；备用源也失败才标「数据暂缺」，其余模块照常输出。",
]

# 数据源：五类口径 + 主备切换 + 降级方式。备用源不可用时按 degrade 说明降级。
DATA_CLASSES = [
    {
        "key": "market", "label": "行情类",
        "fields": "全市场个股 / 指数 / ETF 行情、申万一级行业板块",
        "primary": "东方财富 push2his 日 K + push2 板块快照",
        "backup": "腾讯证券日 K（无成交额字段，成交额降级为「暂缺」）",
        "degrade": "单指数缺失按其余指数均值近似，全缺则标「数据暂缺」",
    },
    {
        "key": "fundamental", "label": "基本面类",
        "fields": "财报 / 业绩预告、一致预期、估值分位、行业景气度",
        "primary": "交易所披露接口 + 指数公司估值序列（Agent 工具）",
        "backup": "东方财富业绩快报 / 预告汇总",
        "degrade": "未取得明细的指标写「数据暂缺」，不用前值顶替",
    },
    {
        "key": "flow", "label": "资金类",
        "fields": "北向资金、两融余额、龙虎榜、主力资金流向",
        "primary": "交易所两融 / 龙虎榜公开数据",
        "backup": "东方财富资金流（主力净流入口径）",
        "degrade": "北向自 2024-08 停止实时披露 → 明确标注，改用主力资金 + 两融观察",
    },
    {
        "key": "alt", "label": "另类类",
        "fields": "市场舆情热度、财经政策新闻、席位动向",
        "primary": "仓库内 69 个数据源快讯（sources.collect_all）+ 政策发布板块",
        "backup": "内置热力图关键词统计（sources.analyze_keyword_heatmap）",
        "degrade": "无当日样本时标「样本不足」，不出结论",
    },
    {
        "key": "macro", "label": "大类资产",
        "fields": "国债收益率、商品指数、黄金、汇率",
        "primary": "中债 / 南华 / 伦敦金 / 中国外汇交易中心（Agent 工具）",
        "backup": "东方财富宏观快照",
        "degrade": "单资产缺失时该行不参与排序，其余资产照常排序并注明",
    },
]

# ---------------------------------------------------------------- 原子动作清单
# 五步流程拆成 41 个原子动作，每个动作 = 一次可映射为工具调用的请求。
# 字段：编号 / 名称 / 工具动作 / 输入 / 产出 / 失败兜底
_ACTIONS = {
    "1": {
        "label": "前置数据采集", "window": "T 日 15:30 → 15:35（5 分钟内）",
        "goal": "批量拉取 T 日收盘数据并完成校验，缺项切备用源",
        "actions": [
            ("S1-01", "市场全景快照", "market.snapshot", "T 日收盘",
             "沪深300 / 中证500 / 中证1000 涨跌幅、全市场成交额、涨跌家数、涨跌停数",
             "主源失败切腾讯日 K；成交额缺失则该列标「数据暂缺」"),
            ("S1-02", "行业与风格切片", "industry.sw1_returns", "T 日收盘",
             "申万一级行业涨跌幅；大小盘、价值成长风格指数收益差",
             "板块接口失败用个股涨幅中位数近似，标注代理口径"),
            ("S1-03", "业绩披露清单", "fundamental.filings", "T 日披露",
             "当日财报 / 业绩预告（净利润增速、是否超预期）",
             "无当日披露写「今日无披露」，不补前日数据"),
            ("S1-04", "估值与一致预期", "fundamental.valuation", "T 日收盘",
             "核心指数 PE / PB 近 10 年分位；行业一致预期增速环比变动",
             "分位序列缺失时只给 PE / PB 原值，分位标「暂缺」"),
            ("S1-05", "资金三件套", "flow.north_margin_lhb", "T 日收盘",
             "北向净流入、两融余额变动、龙虎榜净买卖 TOP 标的",
             "北向停止披露 → 标注并用主力资金替代；两融缺失不推断"),
            ("S1-06", "大类资产快照", "macro.cross_asset", "T 日收盘",
             "10Y 国债收益率、南华商品指数、黄金现货、美元兑人民币中间价",
             "单资产缺失该行留空，不参与收益排序"),
            ("S1-07", "舆情与事件", "alt.news_sentiment", "当日 09:00 → 15:30",
             "财经热点事件、个股舆情热度排名",
             "当日样本不足标「样本不足」，仅保留政策事件"),
            ("S1-08", "数据校验与切源", "pipeline.validate", "上述 7 步结果",
             "缺失项清单、异常值复核标记、实际使用的数据源",
             "核心指标缺失自动切备用源；±20% 偏离打 ⚠️ 复核"),
        ],
    },
    "2": {
        "label": "五维量化分析", "window": "15:35 → 15:50（15 分钟）",
        "goal": "基本面 / 资产配置 / 阿尔法 / 贝塔 / 另类，各出一张表和一句结论",
        "actions": [
            ("S2-01", "业绩异动分类", "analysis.earnings_split", "S1-03 输出",
             "超预期 / 符合 / 不及预期分类；净利润增速 >50%、下滑 >30% 各 TOP10", "无披露则整节标「数据暂缺」"),
            ("S2-02", "估值分位表", "analysis.valuation_table", "S1-04 输出",
             "沪深300 / 中证500 / 中证1000 的 PE、PB 与近 10 年分位、估值区间", "缺分位只给原值"),
            ("S2-03", "景气度变动", "analysis.consensus_delta", "S1-04 输出",
             "申万一级行业一致预期盈利增速环比，提升 / 下滑 TOP5", "缺一致预期则跳过"),
            ("S2-04", "基本面结论", "analysis.conclusion", "S2-01 ~ S2-03",
             "业绩驱动方向、估值修复空间、看多 / 中性 / 看空", "证据不足按中性处理并注明"),
            ("S2-05", "大类资产收益排序", "allocation.asset_returns", "S1-06 输出",
             "股票 / 债券 / 商品 / 黄金 / 现金当日收益 + 近 1 周 / 近 1 月累计", "历史序列缺失只给当日"),
            ("S2-06", "股债性价比", "allocation.equity_bond", "S1-04 + S1-06",
             "沪深300 盈利收益率 − 10Y 国债收益率、历史分位、配置偏向", "分位序列缺失只给点差"),
            ("S2-07", "风险平价仓位", "allocation.risk_parity", "四类资产 60 日收益序列",
             "股票 / 债券 / 商品 / 现金目标仓位，与上一日对比", "无序列则整表标「数据暂缺」，不猜权重"),
            ("S2-08", "配置结论", "analysis.conclusion", "S2-05 ~ S2-07",
             "大类资产性价比排序、仓位调整方向", "—"),
            ("S2-09", "因子收益", "alpha.factor_returns", "T 日全 A 截面",
             "价值 / 成长 / 动量 / 波动率 / 换手率 / 质量六因子当日收益与排名", "缺因子库则标「数据暂缺」"),
            ("S2-10", "指增超额估算", "alpha.excess_estimate", "中证500 / 1000 成分内选股结果",
             "头部指增产品当日超额、市场平均超额水平", "无持仓数据不外推，标「样本缺失」"),
            ("S2-11", "对冲成本", "alpha.basis", "IF / IC / IM 主力合约",
             "年化贴水率、对冲成本高低判断", "合约缺失该行跳过"),
            ("S2-12", "阿尔法结论", "analysis.conclusion", "S2-09 ~ S2-11",
             "当日有效因子、超额环境优劣、对冲成本变化", "—"),
            ("S2-13", "风格剪刀差", "beta.style_spread", "S1-02 输出",
             "大盘−小盘、价值−成长当日收益差、占优风格、近 5 日趋势", "风格指数缺失用成分股中位数代理"),
            ("S2-14", "行业强弱", "beta.industry_rank", "S1-02 输出",
             "申万一级行业按涨跌幅、成交额增速排序，强弱各 TOP5", "成交额缺失只按涨跌幅排"),
            ("S2-15", "主题资金强度", "beta.theme_strength", "S1-07 输出",
             "热点主题成交额占比、涨停家数、持续性等级（强 / 中 / 弱）", "无主题样本则整节跳过"),
            ("S2-16", "贝塔结论", "analysis.conclusion", "S2-13 ~ S2-15",
             "占优风格、关注 / 规避行业、主题交易热度", "—"),
            ("S2-17", "舆情热度榜", "alt.sentiment_rank", "S1-07 输出",
             "全市场舆情热度 TOP10、热度变动方向、驱动事件", "样本不足标「样本不足」"),
            ("S2-18", "龙虎榜席位", "alt.lhb_seats", "S1-05 输出",
             "机构净买入 TOP5、知名游资净买入 TOP5、连续上榜标记", "席位数据缺失只保留机构口径"),
            ("S2-19", "政策事件评估", "alt.event_impact", "S1-07 输出",
             "重要政策 / 行业事件及对板块影响等级（强 / 中 / 弱）", "无当日政策事件则写明"),
            ("S2-20", "另类结论", "analysis.conclusion", "S2-17 ~ S2-19",
             "情绪驱动方向、资金异动标的、事件性机会提示", "—"),
        ],
    },
    "3": {
        "label": "明日策略信号", "window": "15:50 → 15:55（5 分钟）",
        "goal": "把五维结论翻译成可执行的分策略指引，每条都带数值",
        "actions": [
            ("S3-01", "指数增强指引", "strategy.index_enhance", "S2-12 输出",
             "当日超额估算、因子暴露调整方向（加 / 减哪个因子）", "超额缺失只给因子建议"),
            ("S3-02", "市场中性指引", "strategy.market_neutral", "S2-11 输出",
             "对冲成本、基差操作建议、最优对冲合约", "基差缺失建议标「待数据」"),
            ("S3-03", "贝塔轮动指引", "strategy.beta_rotation", "S2-16 输出",
             "明日推荐风格 / 行业、建议仓位、止盈止损位", "风格信号为中性时建议不换仓"),
            ("S3-04", "事件驱动池", "strategy.event_pool", "S2-20 + S2-01",
             "明日可关注的业绩 / 政策 / 舆情标的池", "无事件则空池，不硬凑"),
            ("S3-05", "总仓位中枢", "strategy.position", "五维信号加权",
             "权益仓位中枢 0~100%，对比上一日变动", "上一日仓位缺失则只给中枢"),
        ],
    },
    "4": {
        "label": "风险预警识别", "window": "15:55 → 15:57（2 分钟）",
        "goal": "四条规则自动跑，命中即高亮，未命中也要明说",
        "actions": [
            ("S4-01", "极端波动", "risk.extreme_move", "S1-01 输出",
             "指数单日 |涨跌幅| ≥ 2%、成交额环比异常放大 / 萎缩", "成交额前值缺失只判涨跌幅"),
            ("S4-02", "流动性风险", "risk.liquidity", "S1-05 输出",
             "北向单日净流出 > 50 亿、两融余额大幅下降", "北向无实时数据时该项标「不可判」"),
            ("S4-03", "风格极端化", "risk.style_extreme", "S1-02 输出",
             "单一风格连续 5 日占优、行业分化度 > 历史 90% 分位", "分位序列缺失只判连续性"),
            ("S4-04", "事件风险", "risk.event", "S1-07 输出",
             "重大政策利空、外围市场大幅波动等外部冲击", "无事件则写明「未触发」"),
        ],
    },
    "5": {
        "label": "日报组装归档", "window": "15:57 → 16:00（3 分钟）",
        "goal": "按模板填充、压出 100 字摘要、按日期归档、异常项打标",
        "actions": [
            ("S5-01", "模板填充", "report.render", "S2 / S3 / S4 结果",
             "标准 Markdown 日报（含市场全景、五维、策略、风险四段）", "缺项写「数据暂缺 ⚠️」"),
            ("S5-02", "摘要压缩", "report.summary", "报告全文",
             "100 字以内精简结论，置于文档顶部", "超长自动裁剪，数字优先保留"),
            ("S5-03", "归档", "report.archive", "报告全文",
             "reports/YYYYMMDD_量化交易日报.md", "目录不可写时回落到 stdout 并报错"),
            ("S5-04", "异常打标与日志", "report.mark", "数据体检结果",
             "⚠️ 异常项、✅ 核心信号，以及本次实际使用的数据源", "全程不修改原始数据"),
        ],
    },
}

FALLBACK_RULES = [
    {"key": "source", "title": "主数据源失效", "rule": "自动切备用源；连续失败标「数据暂缺」，保留其余模块"},
    {"key": "calc", "title": "指标计算异常", "rule": "取前值填充并标「数据异常，待复核」；无前值则留空"},
    {"key": "holiday", "title": "非交易日触发", "rule": "自动跳过，输出「今日非交易日，无日报」（退出码 3）"},
    {"key": "log", "title": "异常记录", "rule": "所有异常写入报告 data_quality 与日志，便于排查"},
]

# ---------------------------------------------------------------- 口径与阈值
SCORING = {
    "signal_positive": 0.25,     # 综合分 ≥ +0.25 → 看多
    "signal_negative": -0.25,    # ≤ −0.25 → 看空
    "dimension_weights": {       # 总仓位中枢的五维权重（合计 1.0）
        "fundamental": 0.22, "allocation": 0.22, "alpha": 0.20,
        "beta": 0.20, "alt": 0.16,
    },
    "position_base": 50,         # 中性仓位中枢
    "position_span": 30,         # 综合分 ±1 对应 ±30pct
    "position_floor": 10,        # 权益仓位下限（不满仓不空仓是纪律，不是预测）
    "position_cap": 95,
    "surge_yoy": 50.0,           # 净利润增速「超预期」筛选线（%）
    "drop_yoy": -30.0,           # 下滑「不及预期」筛选线（%）
    "valuation_low": 30.0,       # PE 分位 < 30% → 低估
    "valuation_high": 70.0,      # > 70% → 高估
    "bond_duration": 6.5,        # 债券收益代理的久期（年）
    "risk_parity_target_vol": 8.0,   # 风险平价目标年化波动（%）
    "risk_parity_weight_cap": 40.0,  # 单资产权重上限（%），避免低波动资产吃掉全部权重
    "basis_low": 3.0,            # 年化贴水 < 3% → 对冲成本偏低
    "basis_high": 8.0,           # > 8% → 偏高
    "theme_strong": 60.0,        # 主题强度分 ≥ 60 → 强（完整口径：成交额占比 + 涨停 + 连板）
    "theme_mid": 35.0,           # ≥ 35 → 中
    "theme_proxy_strong": 5.0,   # 仅成交额占比时：≥ 5% → 强（代理口径）
    "theme_proxy_mid": 3.0,      # ≥ 3% → 中
    "outflow_alert": -50.0,      # 北向单日净流出告警（亿）
    "margin_drop_pct": -1.0,     # 两融余额环比降幅告警（%）
    "turnover_shock": 0.30,      # 成交额环比变动 30% 视为异常
    "index_move": 2.0,           # 指数单日 ±2% 极端波动
    "style_streak": 5,           # 单一风格连续占优天数
    "dispersion_pct": 90.0,      # 行业分化度历史分位阈值
}

RISK_RULES = [
    {"key": "move", "label": "市场极端波动", "rule": "指数单日 |涨跌幅| ≥ 2%；成交额环比变动 ≥ 30%"},
    {"key": "liquidity", "label": "流动性风险", "rule": "北向单日净流出 > 50 亿；两融余额环比下降 > 1%"},
    {"key": "style", "label": "风格极端化", "rule": "单一风格连续 5 日占优；行业分化度 > 历史 90% 分位"},
    {"key": "event", "label": "事件风险", "rule": "重大政策利空；外围市场单日跌幅 ≥ 3%"},
]

INDEX_NAMES = ["沪深300", "中证500", "中证1000"]
STYLE_NAMES = ["大盘", "小盘", "价值", "成长"]
ASSET_NAMES = ["股票", "债券", "商品", "黄金"]

# 固定公历休市日：法定节假日中日期不随农历浮动的那几天，交易所必然休市。
# 春节 / 清明 / 端午 / 中秋按农历浮动，用 HOLIDAYS 环境变量（逗号分隔 YYYY-MM-DD）或
# --holidays 参数补，避免把日历猜错当成「非交易日」。
FIXED_CLOSED = [(1, 1), (1, 2), (1, 3), (5, 1), (5, 2), (5, 3),
                (10, 1), (10, 2), (10, 3), (10, 4), (10, 5), (10, 6), (10, 7)]


# ---------------------------------------------------------------- 通用小工具
def _now() -> datetime:
    return datetime.now(CST)


def _as_of(value=None) -> str:
    if value is None:
        return _now().strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.astimezone(CST).strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"无法解析日期：{value}")


def _num(value) -> float | None:
    """宽松转数值：只有真数值 / 数字字符串才算数，其它（含 None、空串、bool）返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _pct(value, digits: int = 2, signed: bool = True) -> str:
    number = _num(value)
    if number is None:
        return "数据暂缺"
    if round(number, digits) == 0:      # 免得出「-0.00%」这种看着像 bug 的写法
        number = 0.0
    return f"{number:+.{digits}f}%" if signed else f"{number:.{digits}f}%"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _mean(values: list) -> float | None:
    numbers = [_num(v) for v in values]
    numbers = [n for n in numbers if n is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _stdev(values: list) -> float | None:
    numbers = [_num(v) for v in values]
    numbers = [n for n in numbers if n is not None]
    if len(numbers) < 2:
        return None
    mean = sum(numbers) / len(numbers)
    var = sum((n - mean) ** 2 for n in numbers) / (len(numbers) - 1)
    return math.sqrt(var)


def _ann_vol(series: list) -> float | None:
    """日频收益序列（%）→ 年化波动率（%）。"""
    vol = _stdev(series)
    return round(vol * math.sqrt(252), 2) if vol is not None else None


def _cum_return(series: list, days: int) -> float | None:
    """近 N 日累计收益（%）：按日收益复利，序列不足 N 日返回 None。"""
    values = [_num(v) for v in (series or [])]
    if len(values) < days or any(v is None for v in values[-days:]):
        return None
    factor = 1.0
    for value in values[-days:]:
        factor *= 1 + value / 100.0
    return round((factor - 1) * 100, 2)


def _percentile_rank(series: list, value: float) -> float | None:
    """当前值在历史序列中的分位（0~100），序列不足 20 个点视为不可用。"""
    numbers = [_num(v) for v in (series or [])]
    numbers = [n for n in numbers if n is not None]
    if len(numbers) < 20 or value is None:
        return None
    below = sum(1 for n in numbers if n <= value)
    return round(below / len(numbers) * 100, 1)


def _signal(score: float) -> str:
    if score >= SCORING["signal_positive"]:
        return "看多"
    if score <= SCORING["signal_negative"]:
        return "看空"
    return "中性"


def _metric(label: str, value, delta=None, status: str = "ok", note: str = "") -> dict:
    """单个指标：值 + 环比 + 状态（ok / missing / anomaly / proxy）。"""
    if value is None:
        return {"label": label, "value": "数据暂缺", "delta": delta,
                "status": "missing", "note": note}
    return {"label": label, "value": str(value), "delta": delta,
            "status": status, "note": note}


def _status_mark(status: str) -> str:
    return {"missing": "⚠️", "anomaly": "⚠️", "proxy": "（代理）"}.get(status, "")


def _metric_na(label: str, note: str) -> dict:
    """不适用项（如北向资金已停止披露）：照实说明，不用 ⚠️ 冒充「今天缺数据」。"""
    return {"label": label, "value": "不适用", "delta": None, "status": "na", "note": note}


def _esc(text) -> str:
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _trunc(text, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- 交易日
def _holidays() -> set:
    raw = os.environ.get("QUANT_HOLIDAYS", "")
    out = set()
    for token in raw.replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(_as_of(token))
        except ValueError:
            continue
    return out


def trading_day(as_of=None, holidays: set | None = None) -> dict:
    """交易日判定：周末 + 固定公历休市日 + 外掛农历假期。

    不猜农历：春节 / 清明 / 端午 / 中秋的休市日通过 ``QUANT_HOLIDAYS`` 或
    ``--holidays`` 传入，避免把日历猜错当成「非交易日」而漏出日报。
    """
    day = _as_of(as_of)
    parsed = datetime.strptime(day, "%Y-%m-%d").date()
    extra = holidays if holidays is not None else _holidays()
    if parsed.weekday() >= 5:
        return {"as_of": day, "is_trading_day": False,
                "reason": f"{day} 为周{'六日'[parsed.weekday() - 5]}，A 股休市"}
    if (parsed.month, parsed.day) in FIXED_CLOSED:
        return {"as_of": day, "is_trading_day": False,
                "reason": f"{day} 为法定节假日（固定公历休市日）"}
    if day in extra:
        return {"as_of": day, "is_trading_day": False,
                "reason": f"{day} 在休市日历中（QUANT_HOLIDAYS）"}
    return {"as_of": day, "is_trading_day": True, "reason": f"{day} 为交易日"}


def next_session(as_of=None, holidays: set | None = None) -> str:
    day = datetime.strptime(_as_of(as_of), "%Y-%m-%d").date()
    for _ in range(30):
        day += timedelta(days=1)
        if trading_day(day.strftime("%Y-%m-%d"), holidays=holidays)["is_trading_day"]:
            return day.strftime("%Y-%m-%d")
    return ""


# ---------------------------------------------------------------- 数据包：自动采集
_CSI_SECIDS = [("沪深300", "1.000300"), ("中证500", "1.000905"), ("中证1000", "1.000852")]
_STYLE_SECIDS = [("大盘", "1.000300"), ("小盘", "1.000852"),
                 ("价值", "0.399371"), ("成长", "0.399370")]


_NET_CACHE_TTL = 300          # 行情抓取缓存（秒）：推送链路与 /api/quant 共用一次结果
_NET_CACHE: dict = {}


def _cached(key, builder):
    now = time.time()
    entry = _NET_CACHE.get(key)
    if entry and now - entry[0] < _NET_CACHE_TTL:
        return entry[1]
    value = builder()
    _NET_CACHE[key] = (now, value)
    return value


def _fetch_klines(entries: list, lmt: int = 45) -> dict:
    """按 secid 批量取日 K（东方财富主源）。返回 {名称: {date: candle}}，失败跳过。"""
    entries = tuple(entries)
    return _cached(("klines", entries, lmt), lambda: _fetch_klines_live(entries, lmt))


def _fetch_klines_live(entries, lmt: int) -> dict:
    import sources  # 延迟导入：sources 在模块层引用本模块，避免循环导入

    result = {}
    for name, secid in entries:
        try:
            payload = sources._fetch_json(sources._ASHARE_KLINE_URL, {
                "ut": sources._ASHARE_UT, "secid": secid, "klt": 101, "fqt": 0,
                "end": "20500101", "lmt": lmt,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            })
            candles = {}
            for line in ((payload.get("data") or {}).get("klines") or []):
                candle = sources._parse_ashare_candle(line)
                if candle:
                    candles[candle["date"]] = candle
            if candles:
                result[name] = candles
        except Exception:
            continue
    return result


def _fetch_valuation() -> dict:
    """指数 PE / PB 快照（东方财富 quote 接口）。取不到就返回空字典，不猜数。"""
    return _cached(("valuation",), _fetch_valuation_live)


def _fetch_valuation_live() -> dict:
    import sources

    fields = "f43,f58,f162,f167"
    out = {}
    for name, secid in _CSI_SECIDS:
        try:
            payload = sources._fetch_json("https://push2.eastmoney.com/api/qt/stock/get",
                                          {"ut": sources._ASHARE_UT, "secid": secid,
                                           "fields": fields})
            data = payload.get("data") or {}
            pe, pb = _num(data.get("f162")), _num(data.get("f167"))
            row = {}
            if pe and 0 < pe < 300:
                row["pe"] = round(pe, 2)
            if pb and 0 < pb < 60:
                row["pb"] = round(pb, 2)
            if row:
                out[name] = row
        except Exception:
            continue
    return out


def _index_row(name: str, candles: dict, as_of: str) -> dict | None:
    dates = sorted(candles or {})
    if not dates:
        return None
    latest = dates[-1]
    if as_of and latest > as_of:
        dates = [d for d in dates if d <= as_of]
        if not dates:
            return None
        latest = dates[-1]
    candle = candles[latest]
    pcts = [candles[d].get("pct") for d in dates[-5:]]
    pct_5d = None
    if len(pcts) == 5 and all(_num(p) is not None for p in pcts):
        factor = 1.0
        for p in pcts:
            factor *= 1 + p / 100.0
        pct_5d = round((factor - 1) * 100, 2)
    amount = candle.get("amount")
    return {
        "name": name,
        "date": latest,
        "close": candle.get("close"),
        "pct": candle.get("pct"),
        "pct_5d": pct_5d,
        "amount": round(amount / 1e8, 1) if isinstance(amount, (int, float)) else None,
        "stale": bool(as_of and latest < as_of),
    }


def _events_from_brief(brief: dict) -> list:
    """把当日政策板块的维度命中转成事件清单（影响等级按提及数分档）。"""
    import sources

    try:
        policy = sources.analyze_policy(brief, deep=False)
    except Exception:
        return []
    events = []
    for bucket in (policy.get("buckets") or [])[:5]:
        mentions = int(bucket.get("mentions") or 0)
        net = int(bucket.get("net") or 0)
        if mentions <= 0:
            continue
        events.append({
            "title": bucket.get("label") or bucket.get("key") or "政策事件",
            "sector": bucket.get("label") or "",
            "impact": "强" if mentions >= 4 else ("中" if mentions >= 2 else "弱"),
            "direction": "利好" if net > 0 else ("利空" if net < 0 else "中性"),
            "note": f"{mentions} 条提及 · 政策净多 {net:+d}",
        })
    return events


def _sentiment_from_brief(brief: dict) -> list:
    """舆情热度 TOP10：直接复用仓库热力图的关键词热度与情绪口径。"""
    import sources

    try:
        heatmap = sources.analyze_keyword_heatmap(brief, top_n=10)
    except Exception:
        return []
    rows = []
    for item in (heatmap.get("keywords") or [])[:10]:
        heat = _num(item.get("heat"))
        sentiment = _num(item.get("sentiment")) or 0.0
        rows.append({
            "name": item.get("display") or item.get("keyword") or "",
            "heat": round(heat, 0) if heat is not None else None,
            "trend": "升温" if sentiment > 0.15 else ("降温" if sentiment < -0.15 else "持平"),
            "driver": item.get("rating") or item.get("sentiment_label") or "",
            "sources": item.get("sources"),
            "note": _trunc(item.get("hint") or "", 48),
        })
    return rows


def _series_pct(candles: dict, as_of: str, n: int) -> list:
    """取截至 as_of 的最近 n 个交易日涨跌幅（%）。序列不足返回空列表。"""
    dates = [d for d in sorted(candles or {}) if not as_of or d <= as_of]
    pcts = [candles[d].get("pct") for d in dates[-n:]]
    if len(pcts) < n or any(_num(p) is None for p in pcts):
        return []
    return [round(float(p), 3) for p in pcts]


def _price_percentile(candles: dict, as_of: str) -> float | None:
    """收盘价在已取窗口内的分位（0~100）。这是**价格分位**，不是估值分位，报告里会标注代理。"""
    dates = [d for d in sorted(candles or {}) if not as_of or d <= as_of]
    closes = [_num(candles[d].get("close")) for d in dates]
    closes = [c for c in closes if c]
    if len(closes) < 120:      # 不足半年不判分位，避免拿几十根 K 线算「十年分位」
        return None
    latest = closes[-1]
    return round(sum(1 for c in closes if c <= latest) / len(closes) * 100, 1)


def _bond_series(yield_candles: dict, as_of: str, n: int, duration: float) -> list:
    """债券日收益的久期代理：收益率上行 1bp，组合约亏 duration×0.01%。

    护栏：如果「收益率」序列的日均变动超过 0.3 个百分点，说明拿到的不是收益率曲线
    （可能是价格指数或行情点位），这时**宁可不给债券序列**，也不用它算风险平价——
    否则久期一乘就会出现 -200% 这种明显不成立的日收益。
    """
    dates = [d for d in sorted(yield_candles or {}) if not as_of or d <= as_of]
    yields = [_num(yield_candles[d].get("close")) for d in dates]
    pairs = [(dates[i], yields[i], yields[i - 1]) for i in range(1, len(yields))
             if yields[i] is not None and yields[i - 1] is not None]
    if len(pairs) < 20:
        return []
    changes = [abs(value - prev) for _day, value, prev in pairs]
    median_change = sorted(changes)[len(changes) // 2]
    if median_change > 0.3:
        return []
    out = [round(-duration * (value - prev), 4) for _day, value, prev in pairs]
    return out[-n:]


def _asset_history(klines: dict, as_of: str, codes: dict) -> dict:
    """四类资产近 60 个交易日的日收益序列，供近 1 周 / 近 1 月累计与风险平价使用。"""
    history = {}
    stock = _series_pct(klines.get("沪深300") or {}, as_of, 60)
    if stock:
        history["股票"] = stock
    for label, key in (("商品", "commodity"), ("黄金", "gold")):
        candles = klines.get(codes.get(key) or "") or {}
        series = _series_pct(candles, as_of, 60)
        if series:
            history[label] = series
    bond = _bond_series(klines.get(codes.get("cgb10y") or "") or {}, as_of, 60,
                        SCORING["bond_duration"])
    if bond:
        history["债券"] = bond
    return history


def _factor_proxies(klines: dict, quotes: dict, as_of: str) -> list:
    """用指数收益差构造可复算的因子代理：规模 / 价值 / 成长 / 动量。

    质量、换手率、波动率需要个股截面因子库，这里**不造**，留给 Agent 工具输入。
    """
    def _pct_of(name):
        quote = (quotes or {}).get(name) or {}
        value = _num(quote.get("pct"))
        if value is not None:
            return value
        candles = klines.get(name) or {}
        row = _index_row(name, candles, as_of)
        return _num(row.get("pct")) if row else None

    base = _pct_of("沪深300")
    small = _pct_of("中证1000")
    value = _pct_of("国证价值")
    growth = _pct_of("国证成长")
    momentum = _cum_return(_series_pct(klines.get("中证1000") or {}, as_of, 5), 5)
    out = []
    if small is not None and base is not None:
        out.append({"key": "size", "label": "规模（中证1000−沪深300）", "ret": round(small - base, 2),
                    "proxy": True})
    if value is not None and base is not None:
        out.append({"key": "value", "label": "价值（国证价值−沪深300）", "ret": round(value - base, 2),
                    "proxy": True})
    if growth is not None and base is not None:
        out.append({"key": "growth", "label": "成长（国证成长−沪深300）", "ret": round(growth - base, 2),
                    "proxy": True})
    if momentum is not None:
        out.append({"key": "momentum", "label": "动量（中证1000 近 5 日）", "ret": momentum,
                    "proxy": True})
    return out


def _turnover_from_klines(klines: dict, as_of: str) -> dict | None:
    """两市成交额（日 K 口径）：上证 + 深证当日成交额，前值取上一交易日的日 K。"""
    sums: list = []
    for name in ("上证指数", "深证成指"):
        candles = klines.get(name) or {}
        dates = [d for d in sorted(candles) if not as_of or d <= as_of]
        if len(dates) < 2:
            return None
        latest, prev = candles[dates[-1]], candles[dates[-2]]
        if latest.get("amount") is None or prev.get("amount") is None:
            return None
        if as_of and dates[-1] != as_of:
            return None      # 日 K 还没到目标交易日，不能用旧数据当当日
        sums.append((latest["amount"] / 1e8, prev["amount"] / 1e8))
    amount = round(sum(row[0] for row in sums), 0)
    prev = round(sum(row[1] for row in sums), 0)
    return {"amount": amount, "prev": prev, "delta": round(amount - prev, 0), "unit": "亿"}


def _turnover_from_quotes(quotes: dict) -> dict | None:
    """两市成交额（实时行情口径）：只有当日值，**不给环比**。

    行情接口的成交额与日 K 成交额口径不同（一个含盘后/大宗口径差异，一个是逐日汇总），
    混着算环比会凭空造出「放量 113%」这种假异常，所以两端口径不混用。
    """
    sh = (quotes or {}).get("上证指数") or {}
    sz = (quotes or {}).get("深证成指") or {}
    if sh.get("amount") is None or sz.get("amount") is None:
        return None
    return {"amount": round(sh["amount"] + sz["amount"], 0), "prev": None,
            "delta": None, "unit": "亿"}


def _breadth_from_feed(feed: dict, pools: dict, market: dict | None) -> dict:
    """市场宽度：涨跌家数取行情接口，涨跌停家数与连板取涨跌停池。"""
    breadth: dict = {}
    counts = (feed or {}).get("breadth") or {}
    if counts.get("up") or counts.get("down"):
        breadth.update({"up_count": counts.get("up"), "down_count": counts.get("down"),
                        "flat_count": counts.get("flat")})
    if (pools or {}).get("ok"):
        for key in ("limit_up", "limit_down", "ladder"):
            if pools.get(key) is not None:
                breadth[key] = pools[key]
    source_breadth = (market or {}).get("breadth") or {}
    for key in ("limit_up", "limit_down", "broken", "seal_rate", "ladder", "up_text", "down_text"):
        if source_breadth.get(key) is not None and key not in breadth:
            breadth[key] = source_breadth[key]
    return breadth


def live_bundle(as_of=None, market=None, brief=None, feeds: dict | None = None) -> dict:
    """采集当日数据：行情 / 行业 / 两融 / 龙虎榜 / 大类资产 / 舆情事件，逐项标注来源。

    采集实现在 :mod:`quant_feeds`（多源 + 备用源 + 失败返回错误）。这里只做「拼装」：
    把各数据源的结果映射成分析层用的字段，并把每项的状态写进 ``bundle['provenance']``，
    报告据此说明「哪些字段是实测、哪些是代理、哪些需要 Agent 工具补」。
    拿不到就留空，由分析层标「数据暂缺」，绝不用前值或同类资产顶替。
    """
    import sources

    as_of = _as_of(as_of)
    bundle: dict = {"as_of": as_of, "source": "live", "notes": []}
    provenance: list = []

    def note(field, status, source, detail=""):
        provenance.append({"field": field, "status": status, "source": source, "detail": detail})

    if feeds is None:
        try:
            import quant_feeds
            feeds = quant_feeds.collect(as_of)
        except Exception as error:
            feeds = {}
            bundle["notes"].append(f"采集层异常：{error}")
    quote_feed = (feeds or {}).get("quotes") or {}
    quotes = quote_feed.get("quotes") or {}
    quote_source = ("东方财富 push2 行情" if quote_feed.get("source") == "eastmoney"
                    else "腾讯证券备用行情")

    # ── 日 K：指数（长窗口算价格分位）+ 风格 + 大类资产序列 ──
    index_codes = [("沪深300", "1.000300"), ("中证500", "1.000905"), ("中证1000", "1.000852"),
                   ("上证指数", "1.000001"), ("深证成指", "0.399001")]
    style_codes = [("国证价值", "0.399371"), ("国证成长", "0.399370")]
    klines = _fetch_klines(index_codes, lmt=2500) or {}
    klines.update(_fetch_klines(style_codes, lmt=70) or {})

    # ── 指数行情 ──
    indices = []
    for name in INDEX_NAMES:
        quote = quotes.get(name) or {}
        candle_row = _index_row(name, klines.get(name), as_of)
        pct = _num(quote.get("pct"))
        if pct is None and candle_row:
            pct = _num(candle_row.get("pct"))
        if pct is None:
            continue
        row = {
            "name": name,
            "pct": pct,
            "pct_5d": _cum_return(_series_pct(klines.get(name) or {}, as_of, 5), 5),
            "amount": _num(quote.get("amount")) if quote.get("amount") is not None
                      else (_num(candle_row.get("amount")) if candle_row else None),
            "close": _num(quote.get("close")) or (_num(candle_row.get("close")) if candle_row else None),
            "stale": bool(candle_row and candle_row.get("stale")),
        }
        if not row["stale"]:
            indices.append(row)
    if indices:
        bundle["indices"] = indices
        note("指数行情", "live" if quotes else "proxy",
             quote_source if quotes else "东方财富日 K", "沪深300 / 中证500 / 中证1000")
    else:
        note("指数行情", "missing", "东方财富 / 腾讯证券", "主备行情接口均未返回")

    # ── 成交额与市场宽度 ──
    turnover = None
    try:
        market = market if market is not None else sources.get_ashare_market()
    except Exception:
        market = None
    market_ok = bool(market and market.get("source") in ("eastmoney", "tencent")
                     and market.get("date") == as_of)
    if market_ok and market.get("turnover"):
        turnover = market.get("turnover")
        turnover_source = "东方财富 / 腾讯日 K（含环比）"
    else:
        turnover = _turnover_from_klines(klines, as_of)
        turnover_source = "东方财富日 K（含环比）"
        if not turnover:
            turnover = _turnover_from_quotes(quotes)
            turnover_source = "行情接口成交额合计（不含环比，口径与日 K 不同不混算）"
    if turnover:
        bundle["turnover"] = turnover
        note("两市成交额", "live", turnover_source)
    else:
        note("两市成交额", "missing", "东方财富 / 腾讯证券", "成交额字段未取到")

    pools = (feeds or {}).get("limit_pools") or {}
    # 快照兜底只在「日期就是目标交易日」时才用：昨天的涨停家数不能填进今天的日报。
    breadth = _breadth_from_feed(quote_feed, pools, market if market_ok else None)
    if market and not market_ok and (market.get("breadth") or market.get("turnover")):
        # 内置快照的 date 会被改写成「今天」，光比日期拦不住；只认当日实测接口
        # （source 为 eastmoney / tencent）才允许把家数、涨跌停、成交额写进日报。
        bundle["notes"].append(
            f"行情兜底快照（source={market.get('source') or '未知'}，"
            f"date={market.get('date') or '未知'}）不是当日实测接口数据，"
            "已丢弃其涨跌停、家数与成交额，不用它顶替当日值")
    if breadth:
        bundle["breadth"] = breadth
        note("市场宽度", "live" if breadth.get("up_count") else "proxy",
             "行情接口涨跌家数 + 涨跌停池",
             f"上涨 {breadth.get('up_count') or '—'} / 下跌 {breadth.get('down_count') or '—'}"
             f" / 涨停 {breadth.get('limit_up') if breadth.get('limit_up') is not None else '—'}"
             f" / 跌停 {breadth.get('limit_down') if breadth.get('limit_down') is not None else '—'}"
             + (f" / {breadth['ladder']}" if breadth.get("ladder") else ""))
    else:
        note("市场宽度", "missing", "东方财富 / 涨停池", "涨跌家数与涨跌停均未取到")
    if (pools or {}).get("ok"):
        note("涨跌停池", "live", "东方财富 push2ex",
             f"涨停 {pools.get('limit_up') if pools.get('limit_up') is not None else '—'} 家 / "
             f"跌停 {pools.get('limit_down') if pools.get('limit_down') is not None else '—'} 家")
    else:
        note("涨跌停池", "missing", "东方财富 push2ex", (pools or {}).get("error") or "未返回")

    # ── 估值：PE / PB 实测 + 价格分位（代理，不是估值分位） ──
    valuation = {}
    for name in INDEX_NAMES:
        quote = quotes.get(name) or {}
        pe, pb = _num(quote.get("pe")), _num(quote.get("pb"))
        price_pct = _price_percentile(klines.get(name) or {}, as_of)
        if pe is None and pb is None and price_pct is None:
            continue
        valuation[name] = {"pe": pe, "pb": pb, "price_pct": price_pct}
    if valuation:
        bundle["valuation"] = valuation
        has_pct = any(v.get("price_pct") is not None for v in valuation.values())
        note("指数 PE / PB", "live" if any(v.get("pe") for v in valuation.values()) else "missing",
             quote_source, "估值分位需 10 年估值序列")
        note("价格分位（估值分位代理）", "proxy" if has_pct else "missing",
             "东方财富日 K 收盘价序列",
             "估值分位（PE/PB）未取得时以价格分位替代，报告里标「价格代理」")

    # ── 风格 ──
    styles, style_history = {}, {}
    for label, name in (("大盘", "沪深300"), ("小盘", "中证1000"),
                        ("价值", "国证价值"), ("成长", "国证成长")):
        pct = _num((quotes.get(name) or {}).get("pct"))
        if pct is None:
            row = _index_row(name, klines.get(name), as_of)
            pct = _num(row.get("pct")) if row else None
        if pct is not None:
            styles[label] = pct
            series = _series_pct(klines.get(name) or {}, as_of, 5)
            if series:
                style_history[label] = series
    if styles:
        bundle["styles"] = styles
        if len(style_history) == len(styles):
            bundle["style_history"] = style_history
        note("风格剪刀差", "live" if "国证价值" in quotes else "proxy",
             quote_source + "（大盘=沪深300，小盘=中证1000，价值/成长=国证价值/成长）")
    else:
        note("风格剪刀差", "missing", "东方财富", "风格指数未取到")

    # ── 行业板块（申万一级代理） ──
    industry_feed = (feeds or {}).get("industry") or {}
    boards = industry_feed.get("boards") or []
    sectors_up = (pools or {}).get("sectors_up") or {}
    matched_limits = 0
    if boards:
        rows = []
        for board in boards[:40]:
            limit_hit = sectors_up.get(board["name"]) or {}
            if limit_hit:
                matched_limits += 1
            rows.append({
                "name": board["name"], "pct": board["pct"],
                "turnover": board.get("amount"), "turnover_prev": None,
                "main_net": board.get("main_net"),
                "limit_up": limit_hit.get("count"),
                "max_board": limit_hit.get("max_board"),
            })
        bundle["industry"] = rows
        note("行业板块", "proxy", industry_feed.get("proxy") or "东方财富行业板块",
             "东财行业分类比申万一级更细，报告按代理口径标注；成交额环比需历史数据")
        themes = []
        for board in boards[:4]:
            if not board.get("turnover_share"):
                continue
            limit_hit = sectors_up.get(board["name"]) or {}
            themes.append({"name": board["name"], "turnover_share": board["turnover_share"],
                           "limit_up": limit_hit.get("count"),
                           "days": limit_hit.get("max_board"), "pct": board.get("pct")})
        if themes:
            bundle["themes"] = themes
            if matched_limits:
                note("主题强度", "live", "行业板块成交额占比 + 涨停池连板",
                     f"{matched_limits} 个板块匹配到涨停分布，按完整口径（占比 + 涨停 + 连板）判强弱")
            else:
                note("主题强度", "proxy", "行业板块成交额占比",
                     "涨跌停池板块名与行业板块未匹配，只按成交额占比判强弱（代理口径）")
        main_net = [b.get("main_net") for b in boards if b.get("main_net") is not None]
        if main_net:
            bundle.setdefault("funds", {})["main_net"] = round(sum(main_net), 2)
            note("主力资金", "proxy", "行业板块主力净流入合计", "全市场主力净额需个股资金流接口")
    else:
        note("行业板块", "missing", "东方财富行业板块", "板块接口未返回数据")

    # ── 资金：两融 + 龙虎榜 ──
    funds = bundle.setdefault("funds", {})
    funds.setdefault("north", None)
    funds.setdefault("north_note", "北向资金实时数据自 2024-08 起停止披露，"
                                   "改用主力资金与两融观察")
    margin = (feeds or {}).get("margin") or {}
    if margin.get("ok") and margin.get("current") is not None:
        funds["margin"] = margin["current"]
        funds["margin_prev"] = margin.get("prev")
        note("两融余额", "live", "东方财富数据中心",
             f"{margin.get('date') or ''} 两融 {margin['current']:,.0f} 亿"
             + (f"，上一交易日 {margin['prev']:,.0f} 亿" if margin.get("prev") else "")
             + (f"（{margin['report']}）" if margin.get("report") else ""))
    else:
        note("两融余额", "missing", "东方财富数据中心", margin.get("error") or "未返回")
    billboard = (feeds or {}).get("billboard") or {}
    if billboard.get("ok") and (billboard.get("inst") or billboard.get("hot")):
        funds["lhb_inst"] = [{"name": i["name"], "net": i["net"], "streak": i.get("streak")}
                             for i in (billboard.get("inst") or [])[:5]]
        funds["lhb_hot"] = [{"name": i["name"], "net": i["net"], "streak": i.get("streak"),
                             "seat": i.get("seat")} for i in (billboard.get("hot") or [])[:5]]
        if funds.get("lhb_inst") or funds.get("lhb_hot"):
            basis = f"口径：{billboard['basis']}" if billboard.get("basis") else ""
            dated = f"（{billboard['date']}）" if billboard.get("date") else ""
            detail = "；".join(filter(None, [basis, billboard.get("note")])) + dated
            note("龙虎榜",
                 "live" if str(billboard.get("basis", "")).startswith("按席位名") else "proxy",
                 "东方财富数据中心", detail)
    else:
        note("龙虎榜", "missing", "东方财富数据中心", billboard.get("error") or "未返回")
    if not any(funds.get(key) for key in ("margin", "main_net", "lhb_inst", "lhb_hot")):
        bundle.pop("funds", None)   # 只剩「北向不披露」的说明，不算资金类已取得

    # ── 大类资产 ──
    cross = (feeds or {}).get("cross_asset") or {}
    items = cross.get("items") or {}
    macro: dict = {"cash": 0.01}
    codes_for_history = {}
    if items.get("cgb10y"):
        macro["cgb10y"] = items["cgb10y"].get("value")
        macro["cgb10y_prev"] = items["cgb10y"].get("prev")
        codes_for_history["cgb10y"] = items["cgb10y"].get("code")
        ten_year = items["cgb10y"]
        change = ten_year.get("change_bp")
        detail = "，".join(filter(None, [
            f"{ten_year['value']:.3f}%" if ten_year.get("value") is not None else "",
            f"环比 {change:+.1f}bp" if change is not None else "",
            ten_year.get("date") or "",
            "历史收益序列用东财日 K 代理" if ten_year.get("history_proxy") else "",
        ]))
        note("10Y 国债收益率", "live",
             "中债登收益率曲线" if ten_year.get("source") == "chinabond" else "东方财富",
             detail)
    else:
        note("10Y 国债收益率", "missing", "东方财富", cross.get("error") or "未返回")
    for key, label in (("commodity", "商品"), ("gold", "黄金")):
        item = items.get(key)
        if item and item.get("pct") is not None:
            macro[key] = {"name": item.get("name") or label, "pct": item["pct"]}
            codes_for_history[key] = item.get("code")
            note(f"{label}（{macro[key]['name']}）", "live", "东方财富")
        else:
            note(f"{label}", "missing", "东方财富", "候选代码均未返回")
    if items.get("fx"):
        fx = items["fx"]
        macro["fx"] = {"name": fx.get("name") or "美元兑人民币", "value": fx.get("value"),
                       "pct": fx.get("pct")}
        note("汇率", "live", fx.get("name") or "东方财富")
    else:
        note("汇率", "missing", "东方财富", "候选代码均未返回")
    if len(macro) > 1:
        bundle["macro"] = macro

    # ── 资产收益序列（近 1 周 / 近 1 月 + 风险平价） ──
    asset_codes = {k: v for k, v in codes_for_history.items() if v}
    if asset_codes:
        asset_klines = _fetch_klines([(code, code) for code in asset_codes.values()], lmt=70) or {}
        klines.update(asset_klines)
    history = _asset_history(klines, as_of, codes_for_history)
    if len(history) >= 3:
        bundle["asset_history"] = history
        note("资产收益序列", "proxy", "日 K 推导",
             f"股票=沪深300，债券=久期 {SCORING['bond_duration']} 年×收益率变动，"
             f"商品/黄金=对应品种日 K；{len(history)} 类资产可用")
    else:
        note("资产收益序列", "missing", "日 K", "可用序列不足 3 类，风险平价与近 1 周 / 1 月暂缺")

    # ── 因子代理 ──
    factors = _factor_proxies(klines, quotes, as_of)
    if factors:
        bundle["factors"] = factors
        note("因子收益", "proxy", "指数收益差代理",
             "质量 / 换手率 / 波动率需个股截面因子库，留待 Agent 工具输入")

    # ── 需 Agent 工具、公开接口拿不到的部分 ──
    for field, why in (("财报 / 业绩预告", "需交易所披露接口或 Agent 工具"),
                       ("一致预期增速", "需卖方一致预期数据（付费源）"),
                       ("指增超额", "需指增产品净值或持仓明细"),
                       ("IF / IC / IM 基差", "需中金所合约行情与到期日"),
                       ("质量 / 换手率 / 波动率因子", "需个股截面因子库")):
        note(field, "missing", "Agent 工具（--input）", why)

    # ── 舆情与事件（复用仓库 69 源快讯） ──
    if brief:
        sentiment = _sentiment_from_brief(brief)
        if sentiment:
            bundle["sentiment"] = sentiment
            note("舆情热度", "live", "仓库 69 个数据源快讯热力图")
        events = _events_from_brief(brief)
        if events:
            bundle["events"] = events
            note("政策事件", "live", "政策发布板块维度统计")
    if not bundle.get("sentiment"):
        note("舆情热度", "missing", "仓库 69 个数据源快讯", "本次未传入当日快讯")

    bundle["provenance"] = provenance
    return bundle


# ---------------------------------------------------------------- 数据包：示例（离线）
def _demo_series(seed: int, vol: float, drift: float = 0.0, n: int = 60) -> list:
    """确定性伪随机日收益序列（%）：线性同余 + 四次抽样近似正态，用于离线示例数据。

    不引入随机库、不依赖运行时间，保证示例日报每次渲染完全一致（可做回归测试）。
    """
    state = seed
    out = []
    for _ in range(n):
        total = 0.0
        for _ in range(4):
            state = (1103515245 * state + 12345) % 2147483648
            total += state / 2147483648
        z = (total - 2.0) * 1.732  # 近似标准正态（4 个均匀分布之和）
        out.append(round(drift + z * vol, 3))
    return out


def demo_bundle(as_of=DEMO_AS_OF) -> dict:
    """内置示例数据包：2026-10-26 收盘口径，确定性、离线、供模板预览与回归测试。

    数值为示例场景（非真实行情），字段结构与 ``--input`` 完全一致，
    便于对照真实数据包检查「哪一列该有数、哪一列该标暂缺」。
    """
    as_of = _as_of(as_of)
    demo_provenance = [
        {"field": "指数行情", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "沪深300 / 中证500 / 中证1000"},
        {"field": "两市成交额", "status": "live", "source": "示例数据包（离线构造）", "detail": "含环比"},
        {"field": "市场宽度", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "涨跌家数 + 涨停 / 跌停"},
        {"field": "指数 PE / PB", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "含近 10 年分位（示例值）"},
        {"field": "风格剪刀差", "status": "live", "source": "示例数据包（离线构造）", "detail": ""},
        {"field": "行业板块", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "申万一级（示例值）"},
        {"field": "主题强度", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "成交额占比 + 涨停家数 + 连板天数"},
        {"field": "主力资金", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "全市场主力净额"},
        {"field": "两融余额", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "含上一交易日"},
        {"field": "龙虎榜", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "机构 / 营业部席位"},
        {"field": "大类资产", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "10Y 国债 / 商品 / 黄金 / 汇率"},
        {"field": "资产收益序列", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "喂风险平价"},
        {"field": "因子收益", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "六因子（示例值）"},
        {"field": "财报 / 业绩预告", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "10 家（示例值）"},
        {"field": "一致预期增速", "status": "live", "source": "示例数据包（离线构造）",
         "detail": "行业景气度环比（示例值）"},
        {"field": "指增超额", "status": "live", "source": "示例数据包（离线构造）", "detail": ""},
        {"field": "IF / IC / IM 基差", "status": "live", "source": "示例数据包（离线构造）", "detail": ""},
        {"field": "舆情热度", "status": "live", "source": "示例数据包（离线构造）", "detail": ""},
        {"field": "政策事件", "status": "live", "source": "示例数据包（离线构造）", "detail": ""},
    ]
    return {
        "as_of": as_of,
        "source": "demo",
        "provenance": demo_provenance,
        "notes": ["示例数据包：用于离线渲染模板与回归测试，不代表真实行情"],
        "indices": [
            {"name": "沪深300", "pct": 0.62, "pct_5d": 1.85, "amount": 5120.0,
             "close": 4685.42},
            {"name": "中证500", "pct": 1.14, "pct_5d": 2.63, "amount": 3180.5,
             "close": 7420.18},
            {"name": "中证1000", "pct": 1.88, "pct_5d": 3.42, "amount": 2960.2,
             "close": 7855.63},
        ],
        "turnover": {"amount": 21380, "delta": 1840, "unit": "亿", "prev": 19540},
        "breadth": {"up_text": "超3600只", "down_text": "约1400只", "limit_up": 86, "limit_down": 4,
                    "broken": 22, "seal_rate": "79%",
                    "ladder": "某6连板（示例）", "hot_sectors": ["算力硬件", "半导体", "黄金"]},
        "styles": {"大盘": 0.41, "小盘": 1.72, "价值": -0.18, "成长": 1.36},
        "style_history": {
            "大盘": [-0.42, 0.18, 0.35, -0.11, 0.41],
            "小盘": [0.66, 0.52, 0.88, 0.31, 1.72],
            "价值": [0.12, -0.35, -0.22, 0.08, -0.18],
            "成长": [-0.35, 0.62, 0.74, 0.41, 1.36],
        },
        "industry": [
            {"name": "半导体", "pct": 3.42, "turnover": 1860.0, "turnover_prev": 1420.0,
             "main_net": 96.4, "limit_up": 9},
            {"name": "通信", "pct": 2.86, "turnover": 1120.0, "turnover_prev": 980.0,
             "main_net": 58.2, "limit_up": 5},
            {"name": "有色金属", "pct": 2.31, "turnover": 940.0, "turnover_prev": 720.0,
             "main_net": 41.7, "limit_up": 7},
            {"name": "计算机", "pct": 1.94, "turnover": 1480.0, "turnover_prev": 1310.0,
             "main_net": 33.5, "limit_up": 4},
            {"name": "国防军工", "pct": 1.62, "turnover": 620.0, "turnover_prev": 560.0,
             "main_net": 21.3, "limit_up": 3},
            {"name": "银行", "pct": -0.84, "turnover": 380.0, "turnover_prev": 430.0,
             "main_net": -28.6, "limit_up": 0},
            {"name": "食品饮料", "pct": -1.12, "turnover": 410.0, "turnover_prev": 520.0,
             "main_net": -33.2, "limit_up": 0},
            {"name": "房地产", "pct": -1.48, "turnover": 320.0, "turnover_prev": 370.0,
             "main_net": -19.8, "limit_up": 0},
            {"name": "煤炭", "pct": -1.86, "turnover": 260.0, "turnover_prev": 340.0,
             "main_net": -24.1, "limit_up": 0},
            {"name": "公用事业", "pct": -2.14, "turnover": 240.0, "turnover_prev": 310.0,
             "main_net": -17.4, "limit_up": 0},
        ],
        "valuation": {
            "沪深300": {"pe": 13.42, "pb": 1.48, "pe_pct": 58.0, "pb_pct": 42.0},
            "中证500": {"pe": 28.60, "pb": 1.92, "pe_pct": 63.0, "pb_pct": 51.0},
            "中证1000": {"pe": 38.75, "pb": 2.24, "pe_pct": 71.0, "pb_pct": 58.0},
        },
        "earnings": [
            {"name": "示例A", "type": "预告", "yoy": 132.0, "consensus": 45.0, "surprise": "超预期"},
            {"name": "示例B", "type": "三季报", "yoy": 96.5, "consensus": 40.0, "surprise": "超预期"},
            {"name": "示例C", "type": "预告", "yoy": 78.2, "consensus": 30.0, "surprise": "超预期"},
            {"name": "示例D", "type": "三季报", "yoy": 61.4, "consensus": 22.0, "surprise": "超预期"},
            {"name": "示例E", "type": "预告", "yoy": 54.8, "consensus": 18.0, "surprise": "超预期"},
            {"name": "示例F", "type": "三季报", "yoy": 21.0, "consensus": 20.0, "surprise": "符合预期"},
            {"name": "示例G", "type": "三季报", "yoy": 8.4, "consensus": 9.5, "surprise": "符合预期"},
            {"name": "示例H", "type": "预告", "yoy": -48.0, "consensus": -12.0, "surprise": "不及预期"},
            {"name": "示例I", "type": "三季报", "yoy": -63.5, "consensus": -20.0, "surprise": "不及预期"},
            {"name": "示例J", "type": "预告", "yoy": -86.2, "consensus": -30.0, "surprise": "不及预期"},
        ],
        "consensus": [
            {"industry": "半导体", "growth": 42.5, "prev_growth": 35.8},
            {"industry": "通信", "growth": 28.4, "prev_growth": 24.1},
            {"industry": "电力设备", "growth": 19.6, "prev_growth": 16.2},
            {"industry": "计算机", "growth": 17.2, "prev_growth": 14.9},
            {"industry": "有色金属", "growth": 12.8, "prev_growth": 11.0},
            {"industry": "食品饮料", "growth": 5.4, "prev_growth": 8.9},
            {"industry": "房地产", "growth": -8.2, "prev_growth": -3.6},
            {"industry": "煤炭", "growth": -12.5, "prev_growth": -6.4},
            {"industry": "银行", "growth": 1.8, "prev_growth": 2.9},
        ],
        "funds": {
            "north": None,
            "north_note": "北向资金实时数据自 2024-08 起停止披露，改用主力资金与两融观察",
            "main_net": 268.4,
            "margin": 19860.0, "margin_prev": 19810.0,
            "lhb_inst": [
                {"name": "示例甲", "net": 4.82, "streak": 2},
                {"name": "示例乙", "net": 3.15, "streak": 1},
                {"name": "示例丙", "net": 2.46, "streak": 3},
                {"name": "示例丁", "net": 1.92, "streak": 1},
                {"name": "示例戊", "net": 1.35, "streak": 1},
            ],
            "lhb_hot": [
                {"name": "示例己", "net": 2.86, "streak": 4, "seat": "示例游资A"},
                {"name": "示例庚", "net": 2.10, "streak": 2, "seat": "示例游资B"},
                {"name": "示例辛", "net": 1.64, "streak": 1, "seat": "示例游资A"},
                {"name": "示例壬", "net": 1.22, "streak": 1, "seat": "示例游资C"},
                {"name": "示例癸", "net": 0.98, "streak": 1, "seat": "示例游资B"},
            ],
        },
        "macro": {
            "cgb10y": 1.82, "cgb10y_prev": 1.85,
            "commodity": {"name": "南华商品指数", "pct": 0.86},
            "gold": {"name": "黄金现货", "pct": 0.42},
            "fx": {"name": "美元兑人民币中间价", "value": 7.1086, "pct": -0.12},
            "cash": 0.01,
        },
        # 收益序列用确定性伪随机生成：波动率按各资产真实量级设定（股票 1.0%/日、
        # 债券 0.25%/日、商品 1.3%/日、黄金 0.9%/日），保证离线演示的风险平价与
        # 近 1 周 / 近 1 月累计都算得出来，且每次运行结果一致。
        "asset_history": {
            "股票": _demo_series(20261026, 1.00, 0.03),
            "债券": _demo_series(20261027, 0.25, 0.01),
            "商品": _demo_series(20261028, 1.30, 0.02),
            "黄金": _demo_series(20261029, 0.90, 0.03),
            "现金": _demo_series(20261030, 0.004, 0.008),
        },
        "equity_bond_history": [round(5.48 + value, 2) for value in
                                _demo_series(20261031, 0.22, 0.0)],
        "factors": [
            {"key": "momentum", "label": "动量", "ret": 0.62},
            {"key": "growth", "label": "成长", "ret": 0.48},
            {"key": "quality", "label": "质量", "ret": 0.21},
            {"key": "value", "label": "价值", "ret": -0.18},
            {"key": "turnover", "label": "换手率", "ret": -0.36},
            {"key": "volatility", "label": "波动率", "ret": -0.54},
        ],
        "excess": {"中证500": 0.15, "中证1000": 0.22},
        "basis": {"IF": -2.4, "IC": -5.8, "IM": -8.6},
        "basis_prev": {"IF": -2.1, "IC": -5.2, "IM": -7.8},
        "themes": [
            {"name": "算力硬件", "turnover_share": 8.4, "limit_up": 9, "days": 3},
            {"name": "半导体设备", "turnover_share": 5.2, "limit_up": 6, "days": 2},
            {"name": "黄金", "turnover_share": 3.1, "limit_up": 4, "days": 1},
            {"name": "机器人", "turnover_share": 2.6, "limit_up": 2, "days": 1},
        ],
        "sentiment": [
            {"name": "算力", "heat": 96, "trend": "升温", "driver": "海外算力订单上修"},
            {"name": "半导体", "heat": 91, "trend": "升温", "driver": "设备招标落地"},
            {"name": "黄金", "heat": 84, "trend": "持平", "driver": "实际利率回落"},
            {"name": "机器人", "heat": 78, "trend": "升温", "driver": "整机定点预期"},
            {"name": "白酒", "heat": 66, "trend": "降温", "driver": "动销数据偏弱"},
            {"name": "地产", "heat": 62, "trend": "降温", "driver": "销售环比回落"},
            {"name": "创新药", "heat": 58, "trend": "持平", "driver": "海外授权进展"},
            {"name": "储能", "heat": 54, "trend": "升温", "driver": "海外大储招标"},
            {"name": "煤炭", "heat": 47, "trend": "降温", "driver": "日耗低于季节性"},
            {"name": "券商", "heat": 41, "trend": "持平", "driver": "成交额回升"},
        ],
        "events": [
            {"title": "半导体设备招标落地", "sector": "半导体", "impact": "强", "direction": "利好",
             "note": "示例事件"},
            {"title": "地产销售数据低于预期", "sector": "房地产", "impact": "中", "direction": "利空",
             "note": "示例事件"},
            {"title": "黄金配置需求回升", "sector": "有色金属", "impact": "中", "direction": "利好",
             "note": "示例事件"},
        ],
        "style_streak": {"小盘": 5},
        "dispersion_pct": 87.0,
        "prev_position": 58,
        "prev_risk_weights": {"股票": 46.0, "债券": 33.5, "商品": 9.0, "黄金": 6.5, "现金": 5.0},
        "external": {"标普500": 0.34, "纳斯达克": 0.58},
    }


# ---------------------------------------------------------------- 数据体检
def _quality(bundle: dict) -> dict:
    """按五类数据口径检查完整度，并复核异常值（±20% 偏离）。"""
    funds = bundle.get("funds") or {}
    macro = bundle.get("macro") or {}
    valuation = bundle.get("valuation") or {}
    checks = {
        "market": bool(bundle.get("indices")),
        "fundamental": bool(bundle.get("earnings") or bundle.get("consensus")
                            or any(v.get("pe") is not None or v.get("price_pct") is not None
                                   for v in valuation.values())),
        "flow": any(funds.get(key) for key in ("margin", "main_net", "north",
                                               "lhb_inst", "lhb_hot")),
        "alt": bool(bundle.get("sentiment") or bundle.get("events")),
        "macro": any(macro.get(key) for key in ("cgb10y", "commodity", "gold", "fx")),
    }
    classes = []
    for item in DATA_CLASSES:
        ok = checks.get(item["key"], False)
        classes.append({
            "key": item["key"], "label": item["label"],
            "status": "ok" if ok else "missing",
            "note": "当日数据已取得" if ok else f"当日未取得，已按备用口径处理：{item['degrade']}",
        })
    total = len(classes)
    got = sum(1 for c in classes if c["status"] == "ok")

    anomalies = []
    for row in bundle.get("indices") or []:
        pct = _num(row.get("pct"))
        if pct is not None and abs(pct) > 20:
            anomalies.append(f"{row.get('name')} 当日涨跌幅 {pct:+.2f}% 偏离 ±20%，需复核数据源")
    for factor in bundle.get("factors") or []:
        ret = _num(factor.get("ret"))
        if ret is not None and abs(ret) > 15:
            anomalies.append(f"因子「{factor.get('label') or factor.get('key')}」"
                             f"当日收益 {ret:+.2f}% 偏离 ±15%，需复核因子库")
    turnover = bundle.get("turnover") or {}
    amount, prev = _num(turnover.get("amount")), _num(turnover.get("prev"))
    if amount is not None and prev:
        ratio = amount / prev - 1
        if abs(ratio) > SCORING["turnover_shock"]:
            anomalies.append(f"两市成交额环比 {ratio * 100:+.1f}%，超过 "
                             f"±{SCORING['turnover_shock'] * 100:.0f}% 阈值，需复核是否含口径变更")
    detail = list(bundle.get("provenance") or [])
    return {
        "score": round(got / total * 100) if total else 0,
        "classes": classes,
        "anomalies": anomalies,
        "notes": list(bundle.get("notes") or []),
        "detail": detail,
        "sources": [{"label": c["label"], "primary": c["primary"], "backup": c["backup"]}
                    for c in DATA_CLASSES],
    }


# ---------------------------------------------------------------- 五维分析
def _index_map(bundle: dict) -> dict:
    return {row.get("name"): row for row in (bundle.get("indices") or []) if row.get("name")}


def _index_pct(bundle: dict, name: str) -> float | None:
    row = _index_map(bundle).get(name)
    return _num(row.get("pct")) if row else None


def _earnings_split(bundle: dict) -> dict:
    earn = [e for e in (bundle.get("earnings") or []) if isinstance(e, dict)]
    buckets = {"超预期": [], "符合预期": [], "不及预期": []}
    for item in earn:
        tag = item.get("surprise")
        if tag not in buckets:
            yoy, consensus = _num(item.get("yoy")), _num(item.get("consensus"))
            if yoy is None:
                continue
            if consensus is not None:
                tag = ("超预期" if yoy > consensus + 5
                       else ("不及预期" if yoy < consensus - 5 else "符合预期"))
            else:
                tag = ("超预期" if yoy >= SCORING["surge_yoy"]
                       else ("不及预期" if yoy <= SCORING["drop_yoy"] else "符合预期"))
        buckets[tag].append(item)

    def _top(items, limit=10, reverse=True):
        rows = [e for e in items if _num(e.get("yoy")) is not None]
        rows.sort(key=lambda e: _num(e.get("yoy")), reverse=reverse)
        return rows[:limit]

    surge = _top([e for e in earn if (_num(e.get("yoy")) or -1e9) > SCORING["surge_yoy"]])
    drop = _top([e for e in earn if (_num(e.get("yoy")) or 1e9) < SCORING["drop_yoy"]],
                reverse=False)
    return {"buckets": buckets, "surge": surge, "drop": drop, "total": len(earn)}


def _valuation_rows(bundle: dict) -> list:
    valuation = bundle.get("valuation") or {}
    rows = []
    for name in INDEX_NAMES:
        item = valuation.get(name) or {}
        pe, pb = _num(item.get("pe")), _num(item.get("pb"))
        pe_pct, pb_pct = _num(item.get("pe_pct")), _num(item.get("pb_pct"))
        price_pct = _num(item.get("price_pct"))
        # 只有真正的 PE/PB 分位才进「估值分位」；价格分位单独作为代理字段输出，
        # 两者在报告里必须能区分，否则就是拿价格冒充估值。
        zone_pct = pe_pct if pe_pct is not None else None
        zone = "—"
        if zone_pct is not None:
            zone = ("低估" if zone_pct < SCORING["valuation_low"]
                    else ("高估" if zone_pct > SCORING["valuation_high"] else "合理"))
        status = "ok" if pe_pct is not None else ("proxy" if (price_pct is not None or pe is not None)
                                                  else "missing")
        rows.append({
            "name": name,
            "pe": pe, "pb": pb, "pe_pct": pe_pct, "pb_pct": pb_pct,
            "price_pct": price_pct, "zone": zone, "status": status,
        })
    return rows


def _dim_fundamental(bundle: dict) -> dict:
    split = _earnings_split(bundle)
    valuation = _valuation_rows(bundle)
    consensus = [c for c in (bundle.get("consensus") or []) if isinstance(c, dict)]
    deltas = []
    for item in consensus:
        growth, prev = _num(item.get("growth")), _num(item.get("prev_growth"))
        if growth is None:
            continue
        delta = round(growth - prev, 2) if prev is not None else None
        deltas.append({"industry": item.get("industry") or "—", "growth": growth,
                       "prev": prev, "delta": delta})
    deltas.sort(key=lambda d: d["delta"] if d["delta"] is not None else -1e9, reverse=True)
    up5 = [d for d in deltas if (d["delta"] or 0) > 0][:5]
    down5 = [d for d in deltas if (d["delta"] or 0) < 0][-5:][::-1]

    buckets = split["buckets"]
    metrics = [
        _metric("业绩披露 / 预告", f"{split['total']} 家" if split["total"] else None,
                delta=(f"超预期 {len(buckets['超预期'])} · 符合 {len(buckets['符合预期'])} · "
                       f"不及 {len(buckets['不及预期'])}") if split["total"] else None),
        _metric(f"净利润增速 >{SCORING['surge_yoy']:.0f}%",
                f"{len(split['surge'])} 家" if split["total"] else None,
                delta="、".join(e.get("name") or "" for e in split["surge"][:3]) or None),
        _metric(f"净利润下滑 >{abs(SCORING['drop_yoy']):.0f}%",
                f"{len(split['drop'])} 家" if split["total"] else None,
                delta="、".join(e.get("name") or "" for e in split["drop"][:3]) or None),
    ]
    for row in valuation:
        if row["status"] == "missing":
            metrics.append(_metric(f"{row['name']} 估值", None))
            continue
        if row["pe_pct"] is not None:
            pct_text = f"PE {row['pe_pct']:.0f}% 分位 · {row['zone']}"
            status, note = "ok", ""
        elif row["price_pct"] is not None:
            pct_text = f"估值分位暂缺，价格分位 {row['price_pct']:.0f}%（代理）"
            status, note = "proxy", "价格分位不等于估值分位"
        else:
            pct_text = "估值分位暂缺"
            status, note = "proxy", "缺 10 年估值序列，仅给原值"
        metrics.append(_metric(
            f"{row['name']} PE / PB",
            f"{row['pe']:.2f} / {row['pb']:.2f}" if row["pb"] is not None and row["pe"] is not None
            else (f"{row['pe']:.2f}" if row["pe"] is not None
                  else (f"PB {row['pb']:.2f}" if row["pb"] is not None else None)),
            delta=pct_text, status=status if _num(row["pe"]) is not None else "missing",
            note=note,
        ))

    highlights = []
    if split["total"]:
        highlights.append(
            f"业绩结构：超预期 {len(buckets['超预期'])} 家 / 符合 {len(buckets['符合预期'])} 家 / "
            f"不及预期 {len(buckets['不及预期'])} 家，超预期占比 "
            f"{len(buckets['超预期']) / split['total'] * 100:.0f}%")
        if split["surge"]:
            highlights.append("增速领先：" + "、".join(
                f"{e.get('name')} {_pct(e.get('yoy'), 1)}" for e in split["surge"][:5]))
        if split["drop"]:
            highlights.append("下滑最快：" + "、".join(
                f"{e.get('name')} {_pct(e.get('yoy'), 1)}" for e in split["drop"][:5]))
    else:
        highlights.append("数据暂缺：当日未取得财报 / 业绩预告明细，不补前日数据")

    zones = [r for r in valuation if r["pe_pct"] is not None]
    if zones:
        avg_pct = sum(r["pe_pct"] for r in zones) / len(zones)
        highlights.append(f"估值位置：三大宽基 PE 分位均值 {avg_pct:.0f}%，"
                          + ("整体低估，修复空间大" if avg_pct < 30 else
                             ("整体偏高，重业绩兑现" if avg_pct > 70 else "居中，结构机会为主")))
    if up5:
        highlights.append("景气上修：" + "、".join(
            f"{d['industry']} {d['delta']:+.1f}pct" for d in up5))
    if down5:
        highlights.append("景气下修：" + "、".join(
            f"{d['industry']} {d['delta']:+.1f}pct" for d in down5))

    score, parts = 0.0, 0
    if split["total"]:
        net = len(buckets["超预期"]) - len(buckets["不及预期"])
        score += _clamp(net / max(1, split["total"]), -1, 1) * 0.5
        parts += 1
    if zones:
        score += _clamp((50 - avg_pct) / 50, -1, 1) * 0.3
        parts += 1
    if deltas:
        up = sum(1 for d in deltas if (d["delta"] or 0) > 0)
        down = sum(1 for d in deltas if (d["delta"] or 0) < 0)
        score += _clamp((up - down) / max(1, len(deltas)), -1, 1) * 0.2
        parts += 1
    signal_note = (f"业绩 + 估值 + 景气三项加权得分 {score:+.2f}" if parts
                   else "基本面三项数据均未取得，信号按中性处理")

    outputs = ["业绩异动清单（增速 >50% / 下滑 >30% 各 TOP10）",
               "估值分位表（沪深300 / 中证500 / 中证1000）",
               "行业景气度变动排名（一致预期增速环比 TOP5）"]
    return {"short": "基本面", "key": "fundamental", "no": "1", "label": "基本面量化",
            "focus": "业绩驱动方向、估值修复空间",
            "signal": _signal(score), "score": round(score, 3), "signal_note": signal_note,
            "metrics": metrics, "highlights": highlights, "outputs": outputs,
            "tables": {"valuation": valuation, "consensus": deltas,
                       "surge": split["surge"], "drop": split["drop"]}}


def _risk_parity(history: dict, target_vol: float | None = None) -> dict | None:
    """逆波动率 + 协方差缩放的风险平价：风险资产按 1/σ 配比，再缩放到目标波动，余额为现金。"""
    target_vol = target_vol or SCORING["risk_parity_target_vol"]
    names = [n for n in ASSET_NAMES if history.get(n)]
    if len(names) < 3:
        return None
    series = []
    for name in names:
        values = [_num(v) for v in history[name]]
        values = [v for v in values if v is not None]
        if len(values) < 20:
            return None
        series.append(values)
    n = min(len(s) for s in series)
    if n < 20:
        return None
    rets = [[v / 100.0 for v in s[-n:]] for s in series]
    means = [sum(r) / n for r in rets]
    vol_pcts = [_ann_vol(s[-n:]) or 0.0 for s in series]
    vols = [v / 100.0 for v in vol_pcts]
    if any(v <= 0 for v in vols):
        return None
    inv = [1.0 / v for v in vols]
    total = sum(inv)
    weights = [w / total for w in inv]
    count = len(names)
    cov = [[0.0] * count for _ in range(count)]
    for i in range(count):
        for j in range(i, count):
            value = sum((rets[i][k] - means[i]) * (rets[j][k] - means[j])
                        for k in range(n)) / (n - 1) * 252
            cov[i][j] = cov[j][i] = value
    port_vol = math.sqrt(max(0.0, sum(weights[i] * weights[j] * cov[i][j]
                                      for i in range(count) for j in range(count))))
    # 单资产上限：不做上限时低波动资产（债券）会吃掉 80% 以上权重，组合失去分散意义。
    # 触顶后把剩余额度按未触顶资产的 1/σ 比例再分配，直到没有资产超限。
    cap = SCORING["risk_parity_weight_cap"] / 100.0
    weights = list(weights)
    for _ in range(len(names)):
        capped = [i for i, w in enumerate(weights) if w > cap + 1e-9]
        if not capped:
            break
        remaining = 1.0 - cap * len(capped)
        free = [i for i in range(len(names)) if i not in capped]
        if not free or remaining <= 0:
            break
        base = sum(weights[i] for i in free)
        for i in capped:
            weights[i] = cap
        for i in free:
            weights[i] = remaining * (weights[i] / base) if base > 0 else remaining / len(free)

    port_vol = math.sqrt(max(0.0, sum(weights[i] * weights[j] * cov[i][j]
                                      for i in range(count) for j in range(count))))
    # 本产品不加杠杆：缩放系数上限 1.0，波动预算没用满时余额记现金。
    scale = min(1.0, (target_vol / 100.0) / port_vol) if port_vol > 0 else 0.0
    risk_weights = {name: round(weights[i] * scale * 100, 1) for i, name in enumerate(names)}
    cash = round(100 - sum(risk_weights.values()), 1)
    return {"weights": {**risk_weights, "现金": cash},
            "portfolio_vol": round(port_vol * 100, 2),
            "scaled_vol": round(port_vol * scale * 100, 2),
            "target_vol": target_vol,
            "leverage": round(scale, 2),
            "asset_vols": {name: round(vol_pcts[i], 2) for i, name in enumerate(names)}}


def _rp_vol_text(rp: dict) -> str:
    """风险平价的波动预算说明：不加杠杆时余额记现金，别把「没缩放」写成「缩放后仍是」。"""
    if rp["portfolio_vol"] <= rp["target_vol"]:
        return (f"组合年化波动 {rp['portfolio_vol']:.1f}%，低于 {rp['target_vol']:.0f}% 目标，"
                f"不加杠杆、余额记现金")
    return (f"组合年化波动 {rp['portfolio_vol']:.1f}%，缩放到目标 {rp['target_vol']:.0f}%"
            f"（系数 {rp['leverage']:.2f}）")


def _dim_allocation(bundle: dict) -> dict:
    macro = bundle.get("macro") or {}
    valuation = bundle.get("valuation") or {}
    pe300 = _num((valuation.get("沪深300") or {}).get("pe"))
    cgb, cgb_prev = _num(macro.get("cgb10y")), _num(macro.get("cgb10y_prev"))
    stock = _index_pct(bundle, "沪深300")
    bond = (round(-SCORING["bond_duration"] * (cgb - cgb_prev), 3)
            if cgb is not None and cgb_prev is not None else None)
    commodity = _num((macro.get("commodity") or {}).get("pct"))
    gold = _num((macro.get("gold") or {}).get("pct"))
    cash = _num(macro.get("cash"))
    history = bundle.get("asset_history") or {}

    assets = [
        {"name": "股票", "proxy": "沪深300", "ret": stock},
        {"name": "债券", "proxy": f"{SCORING['bond_duration']:.1f} 年久期代理", "ret": bond},
        {"name": "商品", "proxy": (macro.get("commodity") or {}).get("name") or "南华商品指数",
         "ret": commodity},
        {"name": "黄金", "proxy": (macro.get("gold") or {}).get("name") or "黄金现货",
         "ret": gold},
        {"name": "现金", "proxy": "货币基金日收益", "ret": cash},
    ]
    for asset in assets:
        series = history.get(asset["name"])
        asset["w1"] = _cum_return(series, 5) if series else None
        asset["m1"] = _cum_return(series, 21) if series else None
        if asset["name"] == "现金" and cash is not None:
            # 货币基金按当日收益率恒定复利推算：这是口径假设，不是实测，标代理。
            asset["w1"] = round(((1 + cash / 100) ** 5 - 1) * 100, 4)
            asset["m1"] = round(((1 + cash / 100) ** 21 - 1) * 100, 4)
            asset["proxy_history"] = True
    ranked = sorted([a for a in assets if a["ret"] is not None],
                    key=lambda a: a["ret"], reverse=True)

    earnings_yield = round(100.0 / pe300, 2) if pe300 else None
    spread = (round(earnings_yield - cgb, 2)
              if earnings_yield is not None and cgb is not None else None)
    spread_series = bundle.get("equity_bond_history") or []
    spread_pct = _percentile_rank(spread_series, spread) if spread is not None else None
    if spread_pct is None:
        bias = "数据暂缺"
    elif spread_pct >= 70:
        bias = "股票占优"
    elif spread_pct <= 30:
        bias = "债券占优"
    else:
        bias = "股债均衡"

    rp = _risk_parity(history)
    prev_weights = bundle.get("prev_risk_weights") or {}
    weight_rows = []
    if rp:
        for name, value in rp["weights"].items():
            prev = _num(prev_weights.get(name))
            weight_rows.append({"name": name, "weight": value,
                                "delta": round(value - prev, 1) if prev is not None else None})
        equity_weight = rp["weights"].get("股票")
        equity_delta = next((r["delta"] for r in weight_rows if r["name"] == "股票"), None)
    else:
        equity_weight = equity_delta = None

    metrics = [
        _metric("大类资产当日排序",
                " > ".join(f"{a['name']} {_pct(a['ret'], 2)}" for a in ranked) or None,
                delta=f"共 {len(ranked)} 类资产有当日收益" if ranked else None),
        _metric("沪深300 盈利收益率", _pct(earnings_yield, 2, signed=False) if earnings_yield else None,
                delta=f"10Y 国债 {_pct(cgb, 2, signed=False)}" if cgb is not None else None),
        _metric("股债性价比（百分点）",
                f"{spread:+.2f}pct" if spread is not None else None,
                delta=(f"历史 {spread_pct:.1f}% 分位 · {bias}" if spread_pct is not None
                       else "点差为实测值；历史分位需 20 个点以上的点差序列")),
        _metric("风险平价 · 股票目标仓位",
                f"{equity_weight:.1f}%" if equity_weight is not None else None,
                delta=(f"较上日 {equity_delta:+.1f}pct" if equity_delta is not None
                       else "需上一日权重对比") if equity_weight is not None else None),
    ]

    highlights = []
    if ranked:
        highlights.append("当日收益排序：" + " > ".join(
            f"{a['name']} {_pct(a['ret'], 2)}" for a in ranked))
        best = ranked[0]
        if best.get("m1") is not None:
            highlights.append(f"近 1 月累计：{best['name']} {_pct(best['m1'])}，"
                              f"近 1 周 {_pct(best['w1'])}")
    else:
        highlights.append("数据暂缺：大类资产当日收益未取得，排序为空")
    if spread is not None:
        highlights.append(
            f"股债性价比 {_pct(spread)}（盈利收益率 {_pct(earnings_yield, 2, signed=False)} "
            f"− 10Y {_pct(cgb, 2, signed=False)}）"
            + (f"，历史 {spread_pct:.1f}% 分位 → {bias}" if spread_pct is not None
               else "，分位暂缺"))
    if rp:
        highlights.append(
            f"风险平价 {_rp_vol_text(rp)}；权益 {rp['weights'].get('股票', 0):.1f}%、"
            f"现金 {rp['weights'].get('现金', 0):.1f}%")
    else:
        highlights.append("数据暂缺：风险平价需要四类资产 20 日以上收益序列")

    score = 0.0
    if spread_pct is not None:
        score += _clamp((spread_pct - 50) / 50, -1, 1) * 0.6
    if commodity is not None and gold is not None and stock is not None:
        score += _clamp(((commodity + gold) / 2 - stock) / 2, -1, 1) * 0.2
    if equity_weight is not None and equity_delta is not None:
        score += _clamp(equity_delta / 5, -1, 1) * 0.2
    signal_note = (f"股债分位 + 商品相对强弱 + 风险平价调仓三项加权 {score:+.2f}"
                   if spread_pct is not None or rp else "配置三项数据未取得，按中性处理")

    outputs = ["大类资产收益表（当日 / 近 1 周 / 近 1 月）", "股债性价比数值与历史分位",
               "风险平价目标仓位（股票 / 债券 / 商品 / 现金）"]
    return {"short": "配置", "key": "allocation", "no": "2", "label": "资产配置量化",
            "focus": "大类资产性价比排序、仓位调整方向",
            "signal": _signal(score), "score": round(score, 3), "signal_note": signal_note,
            "metrics": metrics, "highlights": highlights, "outputs": outputs,
            "tables": {"assets": assets, "weights": weight_rows, "risk_parity": rp,
                       "spread": spread, "spread_pct": spread_pct, "bias": bias}}


def _factor_list(bundle: dict) -> list:
    rows = []
    for item in bundle.get("factors") or []:
        if not isinstance(item, dict):
            continue
        ret = _num(item.get("ret"))
        if ret is None:
            continue
        label = item.get("label") or item.get("key") or "—"
        rows.append({"key": item.get("key") or label, "label": label, "ret": ret,
                     "proxy": bool(item.get("proxy"))})
    rows.sort(key=lambda r: r["ret"], reverse=True)
    return rows


def _basis_map(bundle: dict) -> dict:
    out = {}
    for contract, value in (bundle.get("basis") or {}).items():
        number = _num(value) if not isinstance(value, dict) else _num(value.get("annualized"))
        if number is not None:
            out[str(contract).upper()] = number
    return out


def _excess_map(bundle: dict) -> dict:
    raw = bundle.get("excess") or {}
    aliases = {"csi500": "中证500", "中证500": "中证500", "500": "中证500",
               "csi1000": "中证1000", "中证1000": "中证1000", "1000": "中证1000"}
    out = {}
    for key, value in raw.items():
        name = aliases.get(str(key).lower(), aliases.get(str(key), str(key)))
        number = _num(value)
        if number is not None:
            out[name] = number
    return out


def _dim_alpha(bundle: dict) -> dict:
    factors = _factor_list(bundle)
    excess = _excess_map(bundle)
    basis = _basis_map(bundle)
    prev_basis = _basis_map({"basis": bundle.get("basis_prev") or {}})

    metrics = []
    if factors:
        proxy_count = sum(1 for f in factors if f.get("proxy"))
        metrics.append(_metric("因子收益排序", " > ".join(
            f"{f['label']} {_pct(f['ret'])}" for f in factors),
            delta=(f"有效因子：{factors[0]['label']}、{factors[1]['label']}"
                   if len(factors) > 1 else None)
                  + (f"；{proxy_count}/{len(factors)} 项为指数收益差代理" if proxy_count else ""),
            status="proxy" if proxy_count else "ok",
            note="" if not proxy_count else "质量 / 换手率 / 波动率需个股因子库"))
    else:
        metrics.append(_metric("因子收益排序", None))
    metrics.append(_metric(
        "指增平均超额",
        (f"中证500 {_pct(excess.get('中证500'))} / 中证1000 {_pct(excess.get('中证1000'))}"
         if excess else None),
        delta=(f"均值 {_pct(_mean(list(excess.values())))}" if excess else None)))
    if basis:
        metrics.append(_metric(
            "IF / IC / IM 年化贴水",
            " / ".join(f"{k} {_pct(v)}" for k, v in basis.items()),
            delta=("对冲成本" + ("偏高" if max(basis.values(), key=abs) <= -SCORING["basis_high"]
                                else ("偏低" if max(basis.values(), key=abs) > -SCORING["basis_low"]
                                      else "中性"))),
            status="ok"))
    else:
        metrics.append(_metric("IF / IC / IM 年化贴水", None))

    highlights = []
    if factors:
        highlights.append("有效因子：" + "、".join(
            f"{f['label']} {_pct(f['ret'])}" for f in factors[:2])
            + "；失效因子：" + "、".join(
                f"{f['label']} {_pct(f['ret'])}" for f in factors[-2:]))
    else:
        highlights.append("数据暂缺：当日因子收益未取得，无法判断有效因子")
    if excess:
        avg = _mean(list(excess.values()))
        highlights.append(f"指增超额：中证500 {_pct(excess.get('中证500'))}、"
                          f"中证1000 {_pct(excess.get('中证1000'))}，均值 {_pct(avg)}，"
                          + ("超额环境偏优" if (avg or 0) > 0 else "超额环境偏弱"))
    else:
        highlights.append("数据暂缺：指增超额需成分内选股结果，未取得不外推")
    if basis:
        changes = [f"{k} {_pct(basis[k])}（上日 {_pct(prev_basis.get(k))}）"
                   for k in basis if k in prev_basis]
        highlights.append("对冲成本：" + ("；".join(changes) if changes
                                          else " / ".join(f"{k} {_pct(v)}"
                                                          for k, v in basis.items())))

    score = 0.0
    if excess:
        score += _clamp((_mean(list(excess.values())) or 0) / 0.5, -1, 1) * 0.6
    if factors:
        spread = factors[0]["ret"] - factors[-1]["ret"]
        score += _clamp(spread / 2, -1, 1) * 0.4
    signal_note = (f"指增超额 {_pct(_mean(list(excess.values())))}、因子极差 "
                   f"{_pct(factors[0]['ret'] - factors[-1]['ret']) if factors else '数据暂缺'}"
                   if (excess or factors) else "阿尔法两项数据未取得，按中性处理")

    outputs = ["因子收益排行（六因子当日收益）", "指增超额估算（中证500 / 中证1000）",
               "IF / IC / IM 主力合约年化贴水"]
    return {"short": "阿尔法", "key": "alpha", "no": "3", "label": "阿尔法量化",
            "focus": "当日有效因子、超额环境优劣、对冲成本变化",
            "signal": _signal(score), "score": round(score, 3), "signal_note": signal_note,
            "metrics": metrics, "highlights": highlights, "outputs": outputs,
            "tables": {"factors": factors, "excess": excess, "basis": basis,
                       "basis_prev": prev_basis}}


def _theme_strength(theme: dict) -> tuple:
    """主题强度：有涨停家数 / 连板天数时用完整口径；只有成交额占比时用代理口径。

    代理口径的阈值单独写（成交额占比 ≥5% 强、≥3% 中），不拿完整口径的 60 分线去卡
    占比数据，否则所有板块都会被误判成「弱」。
    """
    share = _num(theme.get("turnover_share")) or 0.0
    limit_up = _num(theme.get("limit_up"))
    days = _num(theme.get("days"))
    if limit_up is None and days is None:
        if share >= SCORING["theme_proxy_strong"]:
            level = "强"
        elif share >= SCORING["theme_proxy_mid"]:
            level = "中"
        else:
            level = "弱"
        return round(share, 1), level, True
    score = share * 5 + min(limit_up or 0, 20) * 2 + min(days or 0, 5) * 2
    if score >= SCORING["theme_strong"]:
        level = "强"
    elif score >= SCORING["theme_mid"]:
        level = "中"
    else:
        level = "弱"
    return round(score, 1), level, False


def _dim_beta(bundle: dict) -> dict:
    styles = {k: _num(v) for k, v in (bundle.get("styles") or {}).items()}
    history = bundle.get("style_history") or {}
    large_small = (styles.get("大盘") - styles.get("小盘")
                   if styles.get("大盘") is not None and styles.get("小盘") is not None else None)
    value_growth = (styles.get("价值") - styles.get("成长")
                    if styles.get("价值") is not None and styles.get("成长") is not None else None)

    def _spread_5d(a: str, b: str):
        series_a, series_b = history.get(a), history.get(b)
        if not series_a or not series_b:
            return None
        cum_a, cum_b = _cum_return(series_a, 5), _cum_return(series_b, 5)
        return round(cum_a - cum_b, 2) if cum_a is not None and cum_b is not None else None

    ls_5d, vg_5d = _spread_5d("大盘", "小盘"), _spread_5d("价值", "成长")

    industries = []
    for item in bundle.get("industry") or []:
        if not isinstance(item, dict):
            continue
        pct = _num(item.get("pct"))
        turnover, prev = _num(item.get("turnover")), _num(item.get("turnover_prev"))
        growth = round((turnover / prev - 1) * 100, 1) if turnover and prev else None
        industries.append({
            "name": item.get("name") or "—", "pct": pct,
            "turnover_growth": growth,
            "main_net": _num(item.get("main_net")),
            "limit_up": _num(item.get("limit_up")),
            "max_board": _num(item.get("max_board")),
        })
    ranked = [i for i in industries if i["pct"] is not None]
    ranked.sort(key=lambda i: i["pct"], reverse=True)
    # 板块数量不足 10 个时按三等分切，保证同一个板块不会既是「强势」又是「弱势」。
    if len(ranked) >= 10:
        strong, weak = ranked[:5], ranked[-5:][::-1]
    else:
        size = max(1, len(ranked) // 3)
        strong, weak = ranked[:size], ranked[::-1][:size]
    weak = [row for row in weak if row not in strong]

    themes = []
    for item in bundle.get("themes") or []:
        if not isinstance(item, dict):
            continue
        score, level, partial = _theme_strength(item)
        themes.append({"name": item.get("name") or "—", "score": score, "level": level,
                       "partial": partial,
                       "share": _num(item.get("turnover_share")),
                       "limit_up": _num(item.get("limit_up")),
                       "days": _num(item.get("days"))})
    themes.sort(key=lambda t: t["score"], reverse=True)

    if large_small is None:
        dominant = "数据暂缺"
    else:
        dominant = "大盘" if large_small > 0 else ("小盘" if large_small < 0 else "均衡")
    if value_growth is not None:
        dominant += " · " + ("价值" if value_growth > 0 else ("成长" if value_growth < 0 else "均衡"))

    metrics = [
        _metric("大盘 − 小盘", _pct(large_small) if large_small is not None else None,
                delta=f"近 5 日累计剪刀差 {_pct(ls_5d)}" if ls_5d is not None else "近 5 日趋势待历史"),
        _metric("价值 − 成长", _pct(value_growth) if value_growth is not None else None,
                delta=f"近 5 日累计 {_pct(vg_5d)}" if vg_5d is not None else "近 5 日趋势待历史"),
        _metric(f"强势行业 TOP{max(1, len(strong))}",
                "、".join(f"{i['name']} {_pct(i['pct'])}" for i in strong) or None,
                delta=f"行业样本 {len(ranked)} 个" if ranked else None),
        _metric(f"弱势行业 TOP{max(1, len(weak))}",
                "、".join(f"{i['name']} {_pct(i['pct'])}" for i in weak) or None,
                delta="板块不足 10 个时按三等分切，强弱不重叠" if 0 < len(ranked) < 10 else None),
        _metric("主题强度", "、".join(
                    f"{t['name']}·{t['level']}" + ("（代理）" if t.get("partial") else "")
                    for t in themes[:3]) or None,
                delta=(("仅按成交额占比判强弱 · 最高 " if themes and themes[0].get("partial")
                        else "最高强度分 ") + f"{themes[0]['score']:.1f}") if themes else None,
                status="proxy" if themes and themes[0].get("partial") else "ok",
                note="" if not (themes and themes[0].get("partial"))
                     else "缺涨停家数与连板天数"),
    ]

    highlights = []
    if large_small is not None:
        highlights.append(
            f"风格：大盘 − 小盘 {_pct(large_small)}，价值 − 成长 {_pct(value_growth)}，"
            f"占优风格 {dominant}")
    else:
        highlights.append("数据暂缺：风格指数未取得，占优风格不可判定")
    if strong:
        highlights.append("强势行业：" + "、".join(
            f"{i['name']} {_pct(i['pct'])}" + (f"（额 {i['turnover_growth']:+.0f}%）"
                                              if i["turnover_growth"] is not None else "")
            for i in strong))
    if weak:
        highlights.append("弱势行业：" + "、".join(
            f"{i['name']} {_pct(i['pct'])}" for i in weak))
    if themes:
        highlights.append("主题热度：" + "、".join(
            f"{t['name']} {t['level']}（强度 {t['score']:.0f}）" for t in themes[:3]))

    # 贝塔信号取「风险偏好」口径：小盘 / 成长占优、行业上涨面广、主题强度高 → 敢加贝塔。
    # 大盘价值占优通常是避险资金抱团，按风险偏好下降处理。
    score = 0.0
    if large_small is not None:
        score += _clamp(-large_small / 1.5, -1, 1) * 0.35
    if ranked:
        up = sum(1 for i in ranked if i["pct"] > 0)
        score += _clamp((up / len(ranked) - 0.5) * 4, -1, 1) * 0.45
    if themes:
        score += _clamp((themes[0]["score"] - SCORING["theme_mid"]) / SCORING["theme_strong"],
                        -1, 1) * 0.20
    signal_note = (f"风险偏好（小盘−大盘取反 0.35 + 行业上涨占比 0.45 + 主题强度 0.20）"
                   f"加权 {score:+.2f}" if large_small is not None
                   else "风格与行业数据不足，按中性处理")

    outputs = ["风格轮动信号（大盘 / 小盘、价值 / 成长）", "行业强弱排名（涨跌幅 + 成交额增速）",
               "主题热度榜单（成交额占比、涨停家数、持续性）"]
    return {"short": "贝塔", "key": "beta", "no": "4", "label": "贝塔量化",
            "focus": "占优风格、关注 / 规避行业、主题交易热度",
            "signal": _signal(score), "score": round(score, 3), "signal_note": signal_note,
            "metrics": metrics, "highlights": highlights, "outputs": outputs,
            "tables": {"styles": styles, "large_small": large_small,
                       "value_growth": value_growth, "ls_5d": ls_5d, "vg_5d": vg_5d,
                       "industries": ranked, "strong": strong, "weak": weak,
                       "themes": themes, "dominant": dominant}}


def _provenance_stats(provenance: list) -> dict:
    """采集明细的计数：实测 / 代理 / 未取得各几项，给卡片和页面做一句话概括。"""
    stats = {"live": 0, "proxy": 0, "missing": 0, "total": 0}
    for row in provenance or []:
        status = row.get("status")
        if status not in stats:
            continue
        stats[status] += 1
        stats["total"] += 1
    return stats


def _streak_text(rows: list) -> str | None:
    """连续上榜统计：席位明细里没有上榜天数时说明「未取得」，不写「0 只」冒充结论。"""
    if not rows:
        return None
    streaks = [_num(row.get("streak")) for row in rows]
    if all(value is None for value in streaks):
        return "连续上榜天数未取得"
    return f"连续上榜 {sum(1 for value in streaks if (value or 0) >= 2)} 只"


def _north_metric(funds: dict) -> dict:
    """北向资金：有数就照给，没数说明「不适用」而不是打 ⚠️（它已停止披露，不是今天缺数据）。"""
    value = _num(funds.get("north"))
    if value is not None:
        return _metric("北向资金", f"{value:+.1f} 亿")
    return _metric_na("北向资金", funds.get("north_note")
                      or "自 2024-08 停止实时披露，改用主力资金与两融观察")


def _dim_alt(bundle: dict) -> dict:
    sentiment = [s for s in (bundle.get("sentiment") or []) if isinstance(s, dict)]
    sentiment.sort(key=lambda s: _num(s.get("heat")) or 0, reverse=True)
    top10 = sentiment[:10]
    funds = bundle.get("funds") or {}
    inst = [s for s in (funds.get("lhb_inst") or []) if isinstance(s, dict)]
    hot = [s for s in (funds.get("lhb_hot") or []) if isinstance(s, dict)]
    inst.sort(key=lambda s: _num(s.get("net")) or 0, reverse=True)
    hot.sort(key=lambda s: _num(s.get("net")) or 0, reverse=True)
    events = [e for e in (bundle.get("events") or []) if isinstance(e, dict)]
    rank = {"强": 3, "中": 2, "弱": 1}
    events.sort(key=lambda e: rank.get(e.get("impact"), 0), reverse=True)

    metrics = [
        _metric("舆情热度 TOP10",
                "、".join(f"{s.get('name')} {int(_num(s.get('heat')) or 0)}" for s in top10[:5])
                or None,
                delta=(f"最高热度 {int(_num(top10[0].get('heat')) or 0)} · "
                       f"{len(top10)} 个关键词有样本" if top10 else None)),
        _metric("龙虎榜机构净买入 TOP3",
                "、".join(f"{s.get('name')} {_num(s.get('net')):+.2f} 亿"
                          for s in inst[:3] if _num(s.get("net")) is not None) or None,
                delta=_streak_text(inst)),
        _metric("知名游资净买入 TOP3",
                "、".join(f"{s.get('name')} {_num(s.get('net')):+.2f} 亿"
                          for s in hot[:3] if _num(s.get("net")) is not None) or None,
                delta=_streak_text(hot)),
        _metric("重要事件",
                f"{len(events)} 条" if events else None,
                delta=(f"强影响 {sum(1 for e in events if e.get('impact') == '强')} 条"
                       if events else None)),
        _north_metric(funds),
    ]

    highlights = []
    if top10:
        rising = sum(1 for s in top10 if s.get("trend") == "升温")
        highlights.append(f"舆情：热度 TOP3 为 " + "、".join(
            f"{s.get('name')}（{s.get('heat')}，{s.get('trend')}）" for s in top10[:3])
            + f"，TOP10 中升温 {rising} 个、降温 "
              f"{sum(1 for s in top10 if s.get('trend') == '降温')} 个")
    else:
        highlights.append("数据暂缺：当日未取得个股 / 主题舆情热度榜")
    if inst or hot:
        continuous = [s.get("name") for s in inst + hot if (_num(s.get("streak")) or 0) >= 2]
        highlights.append(
            f"龙虎榜：机构净买 {_num((inst[0] if inst else {}).get('net')) or 0:+.2f} 亿领衔，"
            f"游资净买 {_num((hot[0] if hot else {}).get('net')) or 0:+.2f} 亿；"
            f"连续上榜 {len(set(continuous))} 只" + (f"（{'、'.join(sorted(set(continuous))[:4])}）"
                                                    if continuous else ""))
    else:
        highlights.append("数据暂缺：龙虎榜席位明细未取得")
    if events:
        highlights.append("事件：" + "；".join(
            f"{e.get('title')}（{e.get('sector')}，{e.get('impact')}影响，{e.get('direction')}）"
            for e in events[:3]))
    if funds.get("main_net") is not None:
        highlights.append(f"主力资金净流入 {_num(funds.get('main_net')):+.1f} 亿")

    score = 0.0
    if top10:
        rising = sum(1 for s in top10 if s.get("trend") == "升温")
        cooling = sum(1 for s in top10 if s.get("trend") == "降温")
        score += _clamp((rising - cooling) / max(1, len(top10)) * 2, -1, 1) * 0.35
    if events:
        bull = sum(1 for e in events if e.get("direction") == "利好")
        bear = sum(1 for e in events if e.get("direction") == "利空")
        score += _clamp((bull - bear) / max(1, len(events)), -1, 1) * 0.35
    if inst:
        net = sum(_num(s.get("net")) or 0 for s in inst)
        score += _clamp(net / 10, -1, 1) * 0.30
    signal_note = (f"舆情升温占比 + 事件多空比 + 机构净买三项加权 {score:+.2f}"
                   if (top10 or events or inst) else "另类三项数据未取得，按中性处理")

    outputs = ["舆情热度榜（TOP10 + 热度变动 + 驱动事件）", "龙虎榜资金清单（机构 / 游资 TOP5）",
               "事件影响评估（强 / 中 / 弱）"]
    return {"short": "另类", "key": "alt", "no": "5", "label": "另类量化",
            "focus": "情绪驱动方向、资金异动标的、事件性机会",
            "signal": _signal(score), "score": round(score, 3), "signal_note": signal_note,
            "metrics": metrics, "highlights": highlights, "outputs": outputs,
            "tables": {"sentiment": top10, "inst": inst[:5], "hot": hot[:5],
                       "events": events}}


# ---------------------------------------------------------------- 策略信号
def _strategy(bundle: dict, dims: list) -> dict:
    by_key = {d["key"]: d for d in dims}
    alpha_tables = by_key["alpha"]["tables"]
    beta_tables = by_key["beta"]["tables"]
    alt_tables = by_key["alt"]["tables"]
    fund_tables = by_key["fundamental"]["tables"]

    factors = alpha_tables["factors"]
    excess = alpha_tables["excess"]
    basis = alpha_tables["basis"]
    items = []

    # 1 指数增强
    if excess:
        avg = _mean(list(excess.values())) or 0
        detail = (f"当日估算超额 中证500 {_pct(excess.get('中证500'))} / "
                  f"中证1000 {_pct(excess.get('中证1000'))}，均值 {_pct(avg)}。")
    else:
        avg = None
        detail = "当日超额未取得（需成分内选股结果），本策略只给因子侧建议。"
    if factors:
        boost = "、".join(f"{f['label']}（{_pct(f['ret'])}）" for f in factors[:2])
        cut = "、".join(f"{f['label']}（{_pct(f['ret'])}）" for f in factors[-1:])
        detail += f" 因子暴露：提高 {boost}，压低 {cut}；单因子偏离不超过组合跟踪误差预算的 30%。"
        action = "加仓动量 / 成长暴露" if factors[0]["ret"] > 0 else "降低高波动暴露"
    else:
        action = "因子数据暂缺，维持现有暴露"
        detail += " 因子收益未取得，暴露调整待数据。"
    items.append({"key": "index_enhance", "label": "指数增强", "signal": _signal(avg or 0),
                  "title": action, "detail": detail})

    # 2 市场中性
    if basis:
        cheapest = max(basis, key=lambda k: basis[k])  # 贴水最小（最不负）→ 成本最低
        cost = basis[cheapest]
        level = ("偏低" if cost > -SCORING["basis_low"]
                 else ("偏高" if cost <= -SCORING["basis_high"] else "中性"))
        detail = (f"主力合约年化贴水："
                  + "、".join(f"{k} {_pct(v)}" for k, v in basis.items())
                  + f"。成本最低为 {cheapest}（{_pct(cost)}），当前对冲成本{level}。")
        if level == "偏高":
            action = f"用 {cheapest} 对冲，并下调对冲比例 10-20pct"
            detail += " 贴水偏深，全额对冲会吃掉超额，建议保留部分敞口等贴水收敛。"
        elif level == "偏低":
            action = f"用 {cheapest} 维持满额对冲"
            detail += " 对冲成本低，适合把敞口压满以锁定选股超额。"
        else:
            action = f"用 {cheapest} 对冲，比例维持"
            detail += " 成本居中，按目标波动调整对冲比例即可。"
    else:
        action = "基差数据暂缺，暂不调整对冲比例"
        detail = "IF / IC / IM 主力合约年化贴水未取得，无法评估对冲成本。"
    items.append({"key": "market_neutral", "label": "市场中性", "signal": "中性",
                  "title": action, "detail": detail})

    # 3 贝塔轮动
    dominant = beta_tables.get("dominant") or "数据暂缺"
    strong = beta_tables.get("strong") or []
    weak = beta_tables.get("weak") or []
    themes = beta_tables.get("themes") or []
    if strong:
        picks = "、".join(i["name"] for i in strong[:2])
        avoid = "、".join(i["name"] for i in weak[:2]) if weak else "—"
        theme_text = (f"主题侧优先 {themes[0]['name']}（{themes[0]['level']}）"
                      if themes else "主题数据暂缺")
        detail = (f"占优风格 {dominant}。配置 {picks}，规避 {avoid}；{theme_text}。"
                  f"纪律：单一行业上限 20%，组合止盈 +10%、止损 −5%，指数单日 −2.5% 触发整体减仓 10pct。")
        action = f"超配 {picks}"
    else:
        action = "行业数据暂缺，维持现有配置"
        detail = "申万一级行业涨跌幅未取得，轮动建议待数据。"
    items.append({"key": "beta_rotation", "label": "贝塔轮动",
                  "signal": by_key["beta"]["signal"], "title": action, "detail": detail})

    # 4 事件驱动
    pool = []
    for e in (alt_tables.get("events") or []):
        if e.get("impact") in ("强", "中") and e.get("direction") == "利好":
            pool.append(f"{e.get('sector')}（{e.get('title')}）")
    for item in (fund_tables.get("surge") or [])[:3]:
        pool.append(f"{item.get('name')}（业绩 {_pct(item.get('yoy'), 1)}）")
    for s in (alt_tables.get("sentiment") or [])[:2]:
        if s.get("trend") == "升温":
            pool.append(f"{s.get('name')}（舆情 {s.get('heat')}）")
    pool = list(dict.fromkeys(pool))[:8]
    items.append({
        "key": "event", "label": "事件驱动",
        "signal": "看多" if pool else "中性",
        "title": "关注池 " + "、".join(pool[:4]) if pool else "今日无强事件，空池",
        "detail": ("明日关注：" + "、".join(pool) + "。事件驱动仓位不超过总权益 15%，"
                   "单标的不超过 5%，事件兑现即减。") if pool
                  else "当日没有强影响利好事件、超预期业绩或升温舆情，不硬凑标的池。",
    })

    weights = SCORING["dimension_weights"]
    composite = sum(d["score"] * weights.get(d["key"], 0) for d in dims)
    position = round(_clamp(SCORING["position_base"] + SCORING["position_span"] * composite,
                            SCORING["position_floor"], SCORING["position_cap"]))
    prev_raw = _num(bundle.get("prev_position"))
    prev = int(round(prev_raw)) if prev_raw is not None else None
    delta = position - prev if prev is not None else None
    reasons = "、".join(f"{d.get('short') or d['label']}{d['signal']}" for d in dims)
    position_row = {
        "value": position, "prev": prev, "delta": delta,
        "composite": round(composite, 3),
        "reason": f"五维加权 {composite:+.2f}（{reasons}）",
        "signal": "看多" if position >= 60 else ("看空" if position <= 35 else "中性"),
    }
    return {"items": items, "position": position_row}


# ---------------------------------------------------------------- 风险预警
def _risks(bundle: dict, quality: dict) -> list:
    risks = []
    index_rows = bundle.get("indices") or []
    turnover = bundle.get("turnover") or {}
    breadth = bundle.get("breadth") or {}
    funds = bundle.get("funds") or {}
    styles = bundle.get("styles") or {}

    # 1 市场极端波动
    hits, index_shock = [], False
    for row in index_rows:
        pct = _num(row.get("pct"))
        if pct is not None and abs(pct) >= SCORING["index_move"]:
            hits.append(f"{row.get('name')} {pct:+.2f}%")
            index_shock = True
    amount, prev = _num(turnover.get("amount")), _num(turnover.get("prev"))
    if amount is not None and prev:
        ratio = amount / prev - 1
        if abs(ratio) >= SCORING["turnover_shock"]:
            hits.append(f"成交额 {amount:,.0f} 亿，环比 {ratio * 100:+.0f}%")
    if hits:
        risks.append({"key": "move", "level": "高" if index_shock else "中",
            "title": "市场极端波动", "detail": "；".join(hits),
            "rule": "指数单日 |涨跌幅| ≥ 2%；成交额环比变动 ≥ 30%"})

    # 2 流动性
    hits = []
    north = _num(funds.get("north"))
    if north is not None and north <= SCORING["outflow_alert"]:
        hits.append(f"北向净流出 {north:.1f} 亿（阈值 {SCORING['outflow_alert']:.0f} 亿）")
    margin, margin_prev = _num(funds.get("margin")), _num(funds.get("margin_prev"))
    if margin is not None and margin_prev:
        change = (margin / margin_prev - 1) * 100
        if change <= SCORING["margin_drop_pct"]:
            hits.append(f"两融余额环比 {change:+.2f}%（{margin_prev:.0f} → {margin:.0f} 亿）")
    if hits:
        risks.append({"key": "liquidity", "level": "高", "title": "流动性风险",
                      "detail": "；".join(hits),
                      "rule": "北向单日净流出 > 50 亿；两融余额环比下降 > 1%"})
    elif north is None:
        # 北向自 2024-08 停止实时披露，这项天然判不了。写进数据体检，不占风险条目，
        # 避免每天挂一条「低风险」把真正的告警淹没。
        note = "北向资金无实时数据（自 2024-08 停止披露），流动性风险只按两融余额判定"
        if margin is not None and margin_prev:
            note += (f"：当前两融 {margin:,.0f} 亿、环比 {(margin / margin_prev - 1) * 100:+.2f}%")
        else:
            note += "；两融数据也未取得，流动性风险不可判"
        if note not in quality["notes"]:
            quality["notes"].append(note)

    # 3 风格极端化
    streak = bundle.get("style_streak") or {}
    hits = []
    for style, days in streak.items():
        if (_num(days) or 0) >= SCORING["style_streak"]:
            hits.append(f"{style}风格已连续 {int(days)} 日占优")
    dispersion = _num(bundle.get("dispersion_pct"))
    if dispersion is not None and dispersion >= SCORING["dispersion_pct"]:
        hits.append(f"行业分化度处于历史 {dispersion:.0f}% 分位")
    if hits:
        risks.append({"key": "style", "level": "高", "title": "风格极端化",
                      "detail": "；".join(hits),
                      "rule": "单一风格连续 5 日占优；行业分化度 > 历史 90% 分位"})

    # 4 事件风险
    hits = []
    for event in bundle.get("events") or []:
        if event.get("impact") == "强" and event.get("direction") == "利空":
            hits.append(f"{event.get('title')}（{event.get('sector')}）")
    for name, pct in (bundle.get("external") or {}).items():
        number = _num(pct)
        if number is not None and number <= -3:
            hits.append(f"{name} 单日 {number:+.2f}%")
    if hits:
        risks.append({"key": "event", "level": "中", "title": "事件风险",
                      "detail": "；".join(hits),
                      "rule": "重大政策利空；外围市场单日跌幅 ≥ 3%"})

    # 5 数据风险（兜底规则 1 / 2）
    missing = [c["label"] for c in quality["classes"] if c["status"] == "missing"]
    if missing:
        risks.append({"key": "data", "level": "中", "title": "数据完整度",
                      "detail": "、".join(missing) + " 当日未取得，相关结论已按「数据暂缺」处理",
                      "rule": "主备数据源均失败时标注「数据暂缺」，保留其余模块"})

    if not risks:
        risks.append({"key": "none", "level": "低", "title": "今日无风险规则触发",
                      "detail": "四条风险规则（极端波动 / 流动性 / 风格极端化 / 事件）均未命中",
                      "rule": "阈值见 RISK_RULES"})
    return risks


# ---------------------------------------------------------------- 摘要与报告
def _tone(index_rows: list) -> str:
    pcts = [_num(r.get("pct")) for r in index_rows]
    pcts = [p for p in pcts if p is not None]
    if not pcts:
        return "行情数据暂缺"
    up = sum(1 for p in pcts if p > 0)
    if up == len(pcts):
        return "指数集体收涨"
    if up == 0:
        return "指数全线收跌"
    return "指数涨跌分化"


def _summary(report: dict) -> str:
    as_of = report["as_of"]
    overview = report["overview"]
    rows = overview.get("indices") or []
    best = max(rows, key=lambda r: _num(r.get("pct")) or -1e9, default=None)
    market = report["tone"]
    if best and _num(best.get("pct")) is not None:
        market += f"，{best.get('name')} {_pct(best.get('pct'))} 领跑"
    dims = report["dimensions"]
    dims_text = "五维：" + "·".join(
        f"{d.get('short') or d['label']}{d['signal']}" for d in dims)
    position = report["strategy"]["position"]
    pos_text = f"权益仓位 {position['value']}%"
    if position["delta"] is not None:
        pos_text += f"（{position['delta']:+d}pct）"
    high = sum(1 for r in report["risks"] if r["level"] == "高")
    mid = sum(1 for r in report["risks"] if r["level"] == "中")
    risk_text = f"风险 高{high}/中{mid}"

    segments = [as_of, market, dims_text, pos_text, risk_text]
    text = "，".join(segments)
    if len(text) > 100:
        segments = [as_of, market, pos_text, risk_text]
        text = "，".join(segments)
    if len(text) > 100:
        text = text[:99] + "…"
    return text


def analyze(bundle: dict, as_of=None) -> dict:
    """五维分析 + 策略 + 风险 + 摘要 + Markdown。纯函数：同输入同输出。"""
    as_of = _as_of(as_of or bundle.get("as_of"))
    quality = _quality(bundle)
    dims = [_dim_fundamental(bundle), _dim_allocation(bundle), _dim_alpha(bundle),
            _dim_beta(bundle), _dim_alt(bundle)]
    strategy = _strategy(bundle, dims)
    risks = _risks(bundle, quality)

    indices = []
    for row in bundle.get("indices") or []:
        valuation = (bundle.get("valuation") or {}).get(row.get("name")) or {}
        pct = _num(row.get("pct"))
        indices.append({
            "name": row.get("name"),
            "pct": pct,
            "pct_5d": _num(row.get("pct_5d")),
            "amount": _num(row.get("amount")),
            "pe": _num(valuation.get("pe")),
            "pe_pct": _num(valuation.get("pe_pct")),
            "price_pct": _num(valuation.get("price_pct")),
            "stale": bool(row.get("stale")),
        })
    turnover = bundle.get("turnover") or {}
    funds = bundle.get("funds") or {}
    overview = {
        "indices": indices,
        "turnover": {"amount": _num(turnover.get("amount")),
                     "delta": _num(turnover.get("delta")),
                     "prev": _num(turnover.get("prev")),
                     "unit": turnover.get("unit") or "亿"},
        "breadth": bundle.get("breadth") or {},
        "funds": {
            "north": _num(funds.get("north")),
            "north_note": funds.get("north_note")
                          or "北向资金实时数据自 2024-08 起停止披露，改用主力资金与两融观察",
            "main_net": _num(funds.get("main_net")),
            "margin": _num(funds.get("margin")),
            "margin_prev": _num(funds.get("margin_prev")),
        },
    }
    report = {
        "column": COLUMN_TITLE,
        "version": VERSION,
        "as_of": as_of,
        "generated_at": _now().strftime("%Y-%m-%d %H:%M"),
        "source": bundle.get("source") or "unknown",
        # ok = 五类数据齐；partial = 部分数据暂缺但仍出报告；empty = 一类都没取到（照样出报告，
        # 全表标「数据暂缺」，因为「拿不到数」本身就是要让读者看到的结论）。
        "status": "ok" if quality["score"] >= 80 else ("partial" if quality["score"] else "empty"),
        "tone": _tone(indices),
        "quality": quality,
        "overview": overview,
        "dimensions": dims,
        "strategy": strategy,
        "risks": risks,
        "notes": bundle.get("notes") or [],
        "provenance": bundle.get("provenance") or [],
        "disclaimer": DISCLAIMER,
    }
    report["provenance_stats"] = _provenance_stats(report["provenance"])
    report["summary"] = _summary(report)
    report["markdown"] = render_markdown(report)
    return report


# ---------------------------------------------------------------- Markdown 渲染
def _md_pct(value) -> str:
    return _pct(value) if _num(value) is not None else "数据暂缺 ⚠️"


def render_markdown(report: dict) -> str:
    """标准日报 Markdown：市场全景 → 五维 → 策略 → 风险 → 数据源与兜底。"""
    lines = []
    quality = report["quality"]
    lines.append(f"# {report['as_of']} 量化交易日报")
    lines.append("")
    lines.append(f"> 生成时间：{report['generated_at']} ｜ 跟踪基准：沪深300 / 中证500 / 中证1000 "
                 f"｜ 数据完整度：{quality['score']}%")
    lines.append("")
    lines.append("## 核心结论速览")
    lines.append("")
    lines.append(f"> {report['summary']}")
    lines.append("")

    # 一、市场全景
    lines.append("## 一、市场全景概览")
    lines.append("")
    lines.append("| 指数 | 当日涨跌幅 | 近5日涨跌幅 | 成交额（亿） | 估值分位（近10年） |")
    lines.append("|------|------------|-------------|--------------|--------------------|")
    for row in report["overview"]["indices"]:
        pe_pct, price_pct = row.get("pe_pct"), row.get("price_pct")
        if pe_pct is not None:
            pct_cell = f"{pe_pct:.0f}%"
        elif price_pct is not None:
            # 估值分位没取到就用价格分位顶上，但必须写明是代理，不能混成同一个数。
            pct_cell = f"{price_pct:.0f}%（价格代理）"
        else:
            pct_cell = "数据暂缺 ⚠️"
        lines.append("| {name} | {pct} | {pct5} | {amount} | {pe_pct} |".format(
            name=row.get("name") or "—",
            pct=_md_pct(row.get("pct")),
            pct5=_md_pct(row.get("pct_5d")),
            amount=f"{row['amount']:,.0f}" if row.get("amount") is not None else "数据暂缺 ⚠️",
            pe_pct=pct_cell,
        ))
    if not report["overview"]["indices"]:
        lines.append("| 沪深300 / 中证500 / 中证1000 | 数据暂缺 ⚠️ | 数据暂缺 ⚠️ | 数据暂缺 ⚠️ | 数据暂缺 ⚠️ |")
    lines.append("")

    breadth = report["overview"]["breadth"] or {}
    def _count(key, text_key, unit="只"):
        count = breadth.get(key)
        if count is not None:
            return f"{int(count)} {unit}"
        return breadth.get(text_key) or "数据暂缺 ⚠️"
    up_text = _count("up_count", "up_text")
    down_text = _count("down_count", "down_text")
    flat = breadth.get("flat_count")
    flat_text = f"，平盘 {int(flat)} 只" if flat is not None else ""
    limit_up = breadth.get("limit_up")
    limit_down = breadth.get("limit_down")
    lines.append(f"市场情绪：上涨 {up_text} / 下跌 {down_text}{flat_text}，"
                 f"涨停 {limit_up if limit_up is not None else '数据暂缺 ⚠️'} 家 / "
                 f"跌停 {limit_down if limit_down is not None else '数据暂缺 ⚠️'} 家")
    funds = report["overview"]["funds"]
    turnover = report["overview"]["turnover"]
    amount_text = (f"{turnover['amount']:,.0f} 亿" if turnover.get("amount") is not None
                   else "数据暂缺 ⚠️")
    delta_text = (f"，环比 {turnover['delta']:+,.0f} 亿" if turnover.get("delta") is not None else "")
    if funds.get("north") is not None:
        north_text = f"北向资金净流入 {funds['north']:+.1f} 亿"
    else:
        # 北向已停止披露：写「不适用」而不是 ⚠️，⚠️ 只留给「今天本该有却没取到」的项
        north_text = "北向资金 不适用（自 2024-08 起停止实时披露，改用主力资金与两融观察）"
    margin_text = "数据暂缺 ⚠️"
    if funds.get("margin") is not None and funds.get("margin_prev"):
        change = funds["margin"] - funds["margin_prev"]
        margin_text = f"{funds['margin']:,.0f} 亿，环比 {change:+,.0f} 亿"
    lines.append(f"资金动向：{north_text}，两融余额 {margin_text}")
    main_text = (f"{funds['main_net']:+.1f} 亿" if funds.get("main_net") is not None else "数据暂缺 ⚠️")
    lines.append(f"量能与主力：两市成交 {amount_text}{delta_text}，主力资金净流入 {main_text}")
    lines.append("")

    # 数据体检
    lines.append("### 数据体检")
    lines.append("")
    lines.append("| 数据类 | 状态 | 说明 |")
    lines.append("|--------|------|------|")
    for item in quality["classes"]:
        mark = "✅" if item["status"] == "ok" else "⚠️ 数据暂缺"
        lines.append(f"| {item['label']} | {mark} | {item['note']} |")
    lines.append("")
    if quality["anomalies"]:
        lines.append("异常标注（需复核）：")
        for note in quality["anomalies"]:
            lines.append(f"- ⚠️ {note}")
        lines.append("")
    if quality.get("notes"):
        lines.append("数据说明：")
        for note in quality["notes"]:
            lines.append(f"- {note}")
        lines.append("")

    detail = quality.get("detail") or []
    if detail:
        stats = report.get("provenance_stats") or {}
        lines.append("### 采集明细")
        lines.append("")
        if stats.get("total"):
            lines.append(f"实测 {stats.get('live', 0)} 项 · 代理 {stats.get('proxy', 0)} 项 · "
                         f"未取得 {stats.get('missing', 0)} 项（共 {stats['total']} 项）")
            lines.append("")
        if report.get("source") == "demo":
            lines.append("> 示例数据包：状态表示「该字段是否有值」，数值为离线构造的示例，非真实行情。")
            lines.append("")
        lines.append("| 字段 | 状态 | 来源 / 缺口说明 |")
        lines.append("|------|------|----------------|")
        for item in detail:
            mark = {"live": "✅ 实测", "proxy": "◐ 代理", "missing": "⚠️ 未取得"}.get(
                item["status"], item["status"])
            detail_text = item.get("detail") or ""
            source = item.get("source") or ""
            lines.append(f"| {item['field']} | {mark} | {source}"
                         + (f" · {detail_text}" if detail_text else "") + " |")
        lines.append("")

    # 二、五维
    lines.append("## 二、五大量化维度分析")
    lines.append("")
    for dim in report["dimensions"]:
        lines.append(f"### {dim['no']}. {dim['label']}　`{dim['signal']}`")
        lines.append("")
        for metric in dim["metrics"]:
            value = metric["value"]
            mark = "" if (metric["status"] == "proxy" and metric.get("note")) \
                else _status_mark(metric["status"])
            note = f"（{metric['note']}）" if metric.get("note") else ""
            delta = f"　变化：{metric['delta']}" if metric.get("delta") else ""
            lines.append(f"- {metric['label']}：{value}{mark}{note}{delta}")
        lines.append("")
        if dim["key"] == "fundamental":
            table = dim["tables"]
            if table["valuation"]:
                lines.append("| 指数 | PE | PB | PE 分位 | PB 分位 | 区间 |")
                lines.append("|------|----|----|---------|---------|------|")
                for row in table["valuation"]:
                    lines.append("| {name} | {pe} | {pb} | {pe_pct} | {pb_pct} | {zone} |".format(
                        name=row["name"],
                        pe=f"{row['pe']:.2f}" if row["pe"] is not None else "暂缺",
                        pb=f"{row['pb']:.2f}" if row["pb"] is not None else "暂缺",
                        pe_pct=f"{row['pe_pct']:.0f}%" if row["pe_pct"] is not None else "暂缺",
                        pb_pct=f"{row['pb_pct']:.0f}%" if row["pb_pct"] is not None else "暂缺",
                        zone=row["zone"],
                    ))
                lines.append("")
            if table["surge"] or table["drop"]:
                lines.append("业绩异动清单：")
                lines.append("")
                lines.append("| 方向 | 标的 | 净利润增速 | 类型 |")
                lines.append("|------|------|------------|------|")
                for item in table["surge"][:10]:
                    lines.append(f"| 超预期 | {item.get('name')} | {_pct(item.get('yoy'), 1)} | "
                                 f"{item.get('type') or '—'} |")
                for item in table["drop"][:10]:
                    lines.append(f"| 不及预期 | {item.get('name')} | {_pct(item.get('yoy'), 1)} | "
                                 f"{item.get('type') or '—'} |")
                lines.append("")
        if dim["key"] == "allocation":
            table = dim["tables"]
            lines.append("| 资产 | 代理 | 当日 | 近1周 | 近1月 |")
            lines.append("|------|------|------|-------|-------|")
            for asset in table["assets"]:
                suffix = "（代理）" if asset.get("proxy_history") else ""
                lines.append("| {name} | {proxy} | {ret} | {w1} | {m1} |".format(
                    name=asset["name"], proxy=asset["proxy"],
                    ret=_md_pct(asset["ret"]),
                    w1=_md_pct(asset["w1"]) + suffix,
                    m1=_md_pct(asset["m1"]) + suffix,
                ))
            lines.append("")
            if table["weights"]:
                lines.append("风险平价目标仓位：" + "　".join(
                    f"{w['name']} {w['weight']:.1f}%" +
                    (f"（{w['delta']:+.1f}pct）" if w["delta"] is not None else "")
                    for w in table["weights"]))
                rp = table["risk_parity"] or {}
                if rp:
                    lines.append(f"　{_rp_vol_text(rp)}。")
                lines.append("")
            else:
                lines.append("风险平价目标仓位：数据暂缺 ⚠️（需四类资产 20 日以上收益序列）")
                lines.append("")
        if dim["key"] == "alpha":
            table = dim["tables"]
            if table["factors"]:
                lines.append("| 排名 | 因子 | 当日收益 |")
                lines.append("|------|------|----------|")
                for rank, factor in enumerate(table["factors"], 1):
                    lines.append(f"| {rank} | {factor['label']} | {_pct(factor['ret'])} |")
                lines.append("")
            if table["basis"]:
                lines.append("对冲基差：" + "　".join(
                    f"{k} 年化贴水 {_pct(v)}" for k, v in table["basis"].items()))
                lines.append("")
        if dim["key"] == "beta":
            table = dim["tables"]
            if table["industries"]:
                has_growth = any(row.get("turnover_growth") is not None
                                 for row in table["industries"])
                has_limit = any(row.get("limit_up") is not None for row in table["industries"])
                header = "| 行业 | 涨跌幅 |"
                divider = "|------|--------|"
                if has_growth:
                    header += " 成交额增速 |"
                    divider += "------------|"
                if has_limit:
                    header += " 涨停家数 | 连板高度 |"
                    divider += "----------|----------|"
                header += " 主力净流入（亿） |"
                divider += "------------------|"
                lines.append(header)
                lines.append(divider)
                for row in table["strong"] + [{"separator": True}] + table["weak"]:
                    if row.get("separator"):
                        lines.append("| ⋯ 弱势行业 |" + " |" * (header.count("|") - 2))
                        continue
                    cells = [row.get("name") or "—",
                             _pct(row.get("pct")) if row.get("pct") is not None else "—"]
                    if has_growth:
                        cells.append(f"{row['turnover_growth']:+.0f}%"
                                     if row.get("turnover_growth") is not None else "—")
                    if has_limit:
                        cells.append(str(int(row["limit_up"]))
                                     if row.get("limit_up") is not None else "—")
                        cells.append(f"{int(row['max_board'])} 板"
                                     if row.get("max_board") else "—")
                    cells.append(f"{row['main_net']:+.1f}"
                                 if row.get("main_net") is not None else "—")
                    lines.append("| " + " | ".join(cells) + " |")
                notes = []
                if not has_growth:
                    notes.append("成交额增速需历史板块成交额序列，本次未取得（列已省略）")
                if has_limit:
                    notes.append("涨停家数 / 连板高度取自当日涨停池，按板块名精确匹配")
                if notes:
                    lines.append("")
                    lines.append("　注：" + "；".join(notes) + "。")
                lines.append("")
            if table["themes"]:
                proxy_only = all(t.get("partial") for t in table["themes"])
                lines.append("| 主题 | 强度分 | 评级 | 成交额占比 | 涨停家数 | 连板天数 |")
                lines.append("|------|--------|------|------------|----------|----------|")
                for theme in table["themes"]:
                    level = theme["level"] + ("（代理）" if theme.get("partial") else "")
                    lines.append(f"| {theme['name']} | {theme['score']:.0f} | {level} | "
                                 f"{theme['share'] if theme['share'] is not None else '—'}% | "
                                 f"{int(theme['limit_up']) if theme['limit_up'] is not None else '—'} | "
                                 f"{int(theme['days']) if theme['days'] is not None else '—'} |")
                if proxy_only:
                    lines.append("")
                    lines.append("　注：仅按成交额占比判强弱（阈值 ≥5% 强 / ≥3% 中），"
                                 "涨停家数与连板天数未取得。")
                lines.append("")
        if dim["key"] == "alt":
            table = dim["tables"]
            if table["sentiment"]:
                lines.append("| # | 舆情标的 | 热度 | 方向 | 驱动 |")
                lines.append("|---|----------|------|------|------|")
                for rank, row in enumerate(table["sentiment"], 1):
                    lines.append(f"| {rank} | {row.get('name')} | {row.get('heat')} | "
                                 f"{row.get('trend')} | {row.get('driver') or '—'} |")
                lines.append("")
            if table["inst"] or table["hot"]:
                lines.append("| 类型 | 标的 | 净买入（亿） | 连续上榜 |")
                lines.append("|------|------|--------------|----------|")
                for row in table["inst"]:
                    lines.append(f"| 机构 | {row.get('name')} | "
                                 f"{_num(row.get('net')) or 0:+.2f} | "
                                 f"{int(_num(row.get('streak')) or 0)} 日 |")
                for row in table["hot"]:
                    lines.append(f"| 游资（{row.get('seat') or '—'}） | {row.get('name')} | "
                                 f"{_num(row.get('net')) or 0:+.2f} | "
                                 f"{int(_num(row.get('streak')) or 0)} 日 |")
                lines.append("")
            if table["events"]:
                lines.append("| 事件 | 板块 | 影响 | 方向 |")
                lines.append("|------|------|------|------|")
                for event in table["events"]:
                    lines.append(f"| {event.get('title')} | {event.get('sector') or '—'} | "
                                 f"{event.get('impact')} | {event.get('direction')} |")
                lines.append("")
        lines.append(f"**核心结论**：{dim['signal']}。{dim['signal_note']}")
        lines.append("")

    # 三、策略
    lines.append("## 三、明日策略信号")
    lines.append("")
    for index, item in enumerate(report["strategy"]["items"], 1):
        lines.append(f"{index}. **{item['label']}**　`{item['signal']}`　{item['title']}")
        lines.append(f"   - {item['detail']}")
    position = report["strategy"]["position"]
    delta_text = (f"，较上一日 {position['delta']:+d}pct" if position["delta"] is not None
                  else "，上一日仓位未取得")
    lines.append(f"5. **总仓位建议**　`{position['signal']}`　权益仓位中枢 "
                 f"{position['value']}%{delta_text}")
    lines.append(f"   - {position['reason']}；区间 0~100%，下限 "
                 f"{SCORING['position_floor']}%、上限 {SCORING['position_cap']}%")
    lines.append("")

    # 四、风险
    lines.append("## 四、风险预警")
    lines.append("")
    for risk in report["risks"]:
        mark = "⚠️" if risk["level"] in ("高", "中") else "✅"
        lines.append(f"- {mark} **{risk['title']}（{risk['level']}）**：{risk['detail']}"
                     f"　`规则：{risk['rule']}`")
    lines.append("")

    # 五、数据源与兜底
    lines.append("## 五、数据源与异常兜底")
    lines.append("")
    lines.append("| 数据类 | 主源 | 备用源 |")
    lines.append("|--------|------|--------|")
    for source in quality["sources"]:
        lines.append(f"| {source['label']} | {source['primary']} | {source['backup']} |")
    lines.append("")
    for rule in FALLBACK_RULES:
        lines.append(f"- {rule['title']}：{rule['rule']}")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 归档 / 执行
def archive(report: dict, directory: str | None = None) -> str:
    """按 YYYYMMDD_量化交易日报.md 归档，返回写入路径。"""
    directory = directory or REPORT_DIR
    os.makedirs(directory, exist_ok=True)
    stamp = report["as_of"].replace("-", "")
    path = os.path.join(directory, f"{stamp}_量化交易日报.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report["markdown"])
    return path


def daily(as_of=None, bundle: dict | None = None, demo: bool = False,
          archive_report: bool = False, out_dir: str | None = None,
          market: dict | None = None, brief: dict | None = None) -> dict:
    """一次完整执行：交易日判定 → 采集 / 数据包 → 分析 → （可选）归档。"""
    if demo and as_of is None:
        as_of = DEMO_AS_OF
    as_of = _as_of(as_of)
    calendar = trading_day(as_of)
    if not calendar["is_trading_day"] and not demo:
        return {
            "column": COLUMN_TITLE, "version": VERSION, "as_of": as_of,
            "generated_at": _now().strftime("%Y-%m-%d %H:%M"),
            "status": "skipped", "trading_day": calendar,
            "summary": "今日非交易日，无日报",
            "markdown": "", "archive_path": None,
            "plan": plan(), "quality": None, "overview": None,
            "dimensions": [], "strategy": None, "risks": [],
            "notes": [calendar["reason"]],
        }
    if bundle is None:
        bundle = demo_bundle(as_of) if demo else live_bundle(as_of, market=market, brief=brief)
    report = analyze(bundle, as_of=as_of)
    report["trading_day"] = calendar
    report["plan"] = plan()
    if archive_report:
        report["archive_path"] = archive(report, out_dir)
    return report


run = daily


def plan() -> dict:
    """执行方案：规则、五类数据源、41 个原子动作、阈值与兜底。"""
    steps = []
    for key in sorted(_ACTIONS):
        group = _ACTIONS[key]
        steps.append({
            "no": key, "label": group["label"], "window": group["window"],
            "goal": group["goal"],
            "actions": [{"id": a[0], "name": a[1], "tool": a[2], "input": a[3],
                         "output": a[4], "fallback": a[5]} for a in group["actions"]],
        })
    return {
        "column": COLUMN_TITLE,
        "version": VERSION,
        "delivery": DELIVERY,
        "rules": EXECUTION_RULES,
        "data_classes": DATA_CLASSES,
        "steps": steps,
        "action_count": sum(len(step["actions"]) for step in steps),
        "scoring": SCORING,
        "risk_rules": RISK_RULES,
        "fallbacks": FALLBACK_RULES,
        "outputs": [
            "reports/YYYYMMDD_量化交易日报.md",
            "100 字以内核心摘要（置于文档顶部，同时作为接口 summary 字段）",
            "异常项 ⚠️ 标注、核心信号 ✅ 标注",
        ],
    }


# ---------------------------------------------------------------- 推送卡片
def build_html_section(report: dict, level: int = 1) -> str:
    """推送卡片 HTML（内联 class 复用推送样式：card / hdr / tag / txt / tbl / ftr）。

    ``level=1`` 全量（五维信号 + 仓位 + 风险）；``level=0`` 只留一句话结论，
    用于推送字符额度吃紧时逐级压缩——主卡片不省略，但不跟快讯抢长度。
    """
    if not report or report.get("status") == "skipped":
        reason = (report or {}).get("summary") or "今日非交易日，无日报"
        return ('<div class="card"><div class="hdr"><span class="tag">AI 量化日报</span>'
                '<span class="sub">15:30 采集 · 16:00 交付</span></div>'
                f'<div class="txt">{_esc(reason)}</div></div>')

    quality = report["quality"]
    position = report["strategy"]["position"]
    dims = report["dimensions"]
    if level <= 0:
        # 字符额度紧到连快讯都放不下时整段省略：完整日报另有 Markdown 归档与 /api/quant，
        # 不靠推送长度承载，避免为了塞卡片把其它栏目挤掉。
        return ""
    chips = "　".join(f"{d.get('short') or d['label']}·{d['signal']}" for d in dims)
    rows = [f'<tr><td colspan="2" class="td-hdr"><span class="tag-w">五维信号</span> '
            f'<span class="sub">数据完整度 {quality["score"]}%</span></td></tr>',
            f'<tr><td class="td-n">维</td><td class="td-t">{_esc(chips)}</td></tr>']

    best = next((d for d in dims if d["key"] == "alpha"), None)
    if best:
        row = best["metrics"][0]
        rows.append(f'<tr><td class="td-n">α</td><td class="td-t">'
                    f'<div>因子：{_esc(_trunc(row["value"], 60))}</div></td></tr>')
    beta = next((d for d in dims if d["key"] == "beta"), None)
    if beta:
        dominant = beta.get("tables", {}).get("dominant") or "数据暂缺"
        industry_line = next((h for h in beta["highlights"] if h.startswith("强势行业")), "")
        rows.append(f'<tr><td class="td-n">β</td><td class="td-t">'
                    f'<div>占优风格：{_esc(dominant)}</div>'
                    + (f'<div class="ev">· {_esc(_trunc(industry_line, 70))}</div>'
                       if industry_line else "")
                    + '</td></tr>')

    level_rank = {"高": 0, "中": 1, "低": 2}
    risks = sorted(report["risks"], key=lambda r: level_rank.get(r["level"], 3))[:3]
    risk_text = "；".join(f"{r['title']}（{r['level']}）" for r in risks)
    detail = position["reason"]
    stats = report.get("provenance_stats") or {}
    provenance_line = ""
    if stats.get("total"):
        provenance_line = (f'<div class="ev">· 采集口径：实测 {stats.get("live", 0)} 项 / '
                           f'代理 {stats.get("proxy", 0)} 项 / 未取得 {stats.get("missing", 0)} 项</div>')
    return (
        '<div class="card"><div class="hdr"><span class="tag">AI 量化日报</span>'
        f'<span class="sub">15:30 采集 · 16:00 交付 · 数据完整度 {quality["score"]}%</span></div>'
        f'<div class="txt">{_esc(report["summary"])}</div>'
        f'{provenance_line}'
        f'<table class="tbl">{"".join(rows)}'
        f'<tr><td class="td-n">仓</td><td class="td-t"><div>权益仓位中枢 '
        f'<b>{position["value"]}%</b>'
        + (f'（较上日 {position["delta"]:+d}pct）' if position["delta"] is not None else "")
        + f'</div><div class="ev">· {_esc(_trunc(detail, 70))}</div></td></tr>'
        f'<tr><td class="td-n">!</td><td class="td-t"><div>{_esc(_trunc(risk_text, 90))}</div></td></tr>'
        '</table>'
        f'<div class="ftr">按五大维度评分合成，信号只有看多 / 中性 / 看空三档；'
        f'标「数据暂缺」的项当日未取得数值。完整 Markdown 见 '
        f'<code>reports/{report["as_of"].replace("-", "")}_量化交易日报.md</code>。</div></div>'
    )


# ---------------------------------------------------------------- CLI
def _load_bundle(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("数据包必须是 JSON 对象")
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AI 量化日报执行引擎")
    parser.add_argument("--date", help="交易日（YYYY-MM-DD / YYYYMMDD），默认今天")
    parser.add_argument("--demo", action="store_true", help="使用内置示例数据包（离线）")
    parser.add_argument("--input", help="外部数据包 JSON 路径（Agent 工具返回）")
    parser.add_argument("--out", help="归档目录，默认 reports/")
    parser.add_argument("--no-archive", action="store_true", help="只打印，不写文件")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON（含 markdown 字段）")
    parser.add_argument("--summary", action="store_true", help="只输出 100 字摘要")
    parser.add_argument("--print-plan", action="store_true", help="只打印原子动作清单")
    parser.add_argument("--holidays", help="额外休市日，逗号分隔（覆盖 QUANT_HOLIDAYS）")
    args = parser.parse_args(argv)

    if args.print_plan:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return 0

    if args.holidays:
        os.environ["QUANT_HOLIDAYS"] = args.holidays

    as_of = None
    if args.date:
        try:
            as_of = _as_of(args.date)
        except ValueError as error:
            print(f"日期无法解析：{error}", file=sys.stderr)
            return 4

    bundle = None
    if args.input:
        try:
            bundle = _load_bundle(args.input)
        except (OSError, ValueError) as error:
            print(f"数据包不可用：{error}", file=sys.stderr)
            return 4

    report = daily(as_of=as_of, bundle=bundle, demo=args.demo,
                   archive_report=not args.no_archive, out_dir=args.out)

    if args.summary:
        print(report["summary"])
        return 0 if report["status"] != "skipped" else 3
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report["status"] != "skipped" else 3
    if report["status"] == "skipped":
        print(report["summary"])
        return 3
    print(report["markdown"])
    if report.get("archive_path"):
        print(f"\n已归档：{report['archive_path']}")
    if report["quality"]["score"] < 100:
        print(f"\n数据完整度 {report['quality']['score']}%："
              f"{'、'.join(c['label'] for c in report['quality']['classes'] if c['status'] == 'missing')}"
              " 当日未取得，已标「数据暂缺」。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
