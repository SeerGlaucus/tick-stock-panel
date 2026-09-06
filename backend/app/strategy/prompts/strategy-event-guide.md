# 事件驱动策略契约（AI 生成与人工开发通用）

事件驱动策略按交易日推进, 在 `handle_data` 中持有跨日状态并主动下单。
只用于回测验证, 不产生横截面信号（不进选股/监控/叠加）。

## 文件结构

```python
import polars as pl

META = {
    "id": "ai_xxx",
    "name": "策略名",
    "description": "一句话描述",
    "tags": [],
    "asset_types": ["stock"],
    "timeframes": ["1d"],            # 必含 "1d"
    "history_bars": 60,              # 可选, 默认 60: context.history(n) 的 n 上限
    "basic_filter": {...},           # 与信号策略同构: 每日候选股票池
    "params": [...],                 # 与信号策略同构: UI 参数编辑器 + context.params
    "scoring": {...},                # 与信号策略同构: 权重总和 1.0, 键为真实字段
}
EXECUTION_BACKEND = "event"
REQUIRED_FEATURES = ["close", "ma20"]   # 必填: 脚本可读的全部列(基础列之外)
STOP_LOSS = -0.07                    # 风控走 META + 设置弹窗, 引擎自动执行
TAKE_PROFIT = None
TRAILING_STOP = None
TRAILING_TAKE_PROFIT_ACTIVATE = None
TRAILING_TAKE_PROFIT_DRAWDOWN = None
MAX_HOLD_DAYS = 20


def initialize(context):
    pass        # 可选: run 开始一次, 初始化 context.state

def before_trading_start(context):
    pass        # 可选: T 开盘前, 只读

def handle_data(context):
    pass        # 必填: T 收盘后, 唯一可下单时相

def after_trading_end(context):
    pass        # 可选: T 撮合完成后, 只读
```

## context API

| 成员 | 说明 |
| --- | --- |
| `context.params` | dict: META 默认 + override 解析后的参数 |
| `context.current_date` / `previous_date` | date / date\|None |
| `context.state` | dict: 跨日自由状态, initialize 初始化 |
| `context.universe` | pl.DataFrame: 当日候选面板(basic_filter 已过滤), 列 = symbol/date/name/open/high/low/close/volume/amount + REQUIRED_FEATURES + score(声明 scoring 时) |
| `context.history(n)` | pl.DataFrame: 最近 n 个交易日窗口, 按 symbol,date 排序; n <= history_bars; handle_data 时相含当日, before 时相不含 |
| `context.cash` / `total_value` / `positions` / `orders_today` | 只读组合视图 |
| `context.order_buy(symbol, amount)` | 按金额买入, 向下取整手(100股), 零头回现金 |
| `context.order_sell(symbol, shares=None)` | 卖出; shares=None 清仓 |

## 铁律（三条禁区）

1. **不写风控**: 止损/止盈/移动止损/持有天数只通过 META 与设置弹窗配置, 引擎自动执行; 脚本内不要手写止损逻辑。
2. **不硬编码股票池**: 候选集由 basic_filter 每日计算; 非候选标的的买入会被拒绝(buy_not_in_universe)。
3. **只读声明列**: 脚本只能读 REQUIRED_FEATURES 声明的列(基础列之外); 未声明列不在面板内。

## 成交语义

- 订单在 handle_data(当日收盘后)提交; `open_t+1` 口径次日开盘成交, `close_t` 口径当日收盘成交(近似)。
- 涨停不能买/跌停不能卖 → 订单转挂单次日重试; T+1: 当日买入当日不可卖。
- 脚本卖单是 "signal" 退出; 引擎风控(挂单 > 风控 > 到期 > 期末)优先级固定。

## 最小示例

```python
def handle_data(context):
    universe = context.universe
    if universe.is_empty():
        return
    top = universe.sort("score", descending=True).head(5)
    for row in top.iter_rows(named=True):
        if row["symbol"] not in context.positions["symbol"].to_list():
            context.order_buy(row["symbol"], context.cash / 5)
```
