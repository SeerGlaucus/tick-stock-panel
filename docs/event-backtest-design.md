# 事件驱动回测（日频）设计方案

> **状态：设计草案，尚未实现。** 文中标注"新增/改造"的模块、接口与字段均为规划内容，
> 不得视为仓库中已存在的 API。实现前应重新核对本文引用的现有模块调用链与测试。

## 1. 背景与目标

当前回测范式是**信号驱动**：策略（`StrategyDef`）作为无状态信号发生器，先全窗口向量化
生成信号面板/矩阵（`backtest/matrix.py`、`StrategyDependencyResolver`），再由
`BacktestEngine.simulate*` 逐候选撮合。配置化运维（META 参数、override、风控、因子、
scoring、优化/walkforward、结果契约）全部建立在"无状态纯函数"这一性质上。

本方案引入**事件驱动回测（日频）**：策略脚本随交易日时钟推进，在 `handle_data` 中持有
跨日状态并主动下单，以表达择时、轮动、动态仓位、状态机等信号驱动范式无法表达的时序
逻辑。

**目标：**

1. 支持日频事件驱动回测，产出与现有回测完全同构的结果契约。
2. 充分延续配置化运维：参数、风控、股票池、因子、scoring、override、AI 生成、优化与
   结果展示链路全部复用。
3. 事件驱动作为 `StrategyDef` 体系的**第五种 execution_backend**，不建平行引擎。

**非目标（v1 明确不做）：**

- 分钟级回放与盘中调度（`handle_bar`、盘中 run_daily）。
- 事件策略作为 composite 子策略。
- 选股/信号历史/实时监控能力（事件策略不产生横截面信号）。
- 实盘桥接（回测即实盘）。仅预留契约余地，见 §8.4。
- `order_target_pct` 等组合级下单方法。

## 2. 核心定位

### 2.1 两类策略的能力分界

| 维度 | 信号策略（现有） | 事件策略（新增） |
| --- | --- | --- |
| 本质 | 无状态、横截面、任意时点可调用的筛选器 | 有状态、全时序、只能整体回放的交易脚本 |
| 能力 | 选股 / 信号历史 / 监控 / 叠加子策略 / 回测 | 仅回测 |
| 代码性质 | 纯函数，可向量化、可合并、可共享矩阵 | 时序脚本，状态随 run 隔离 |

### 2.2 系统位置（方案 4）

- 策略管理仍在现有"策略页"（`/screener`，事实上的策略管理中心），**混排统一网格**，
  顶部在股票/ETF 选择框前增加「信号驱动 / 事件驱动」两态切换器（默认信号驱动）。
- 事件策略卡片：**无点击、无运行、无命中数、无监控开关、无去回测按钮**；保留来源徽标
  +「事件」徽标，动作仅设置齿轮（复用 `StrategySettingsDialog`，按 backend 隐藏买卖信号
  面板）。
- 事件视图下隐藏"命中结果区"与周期筛选（v1 仅日频），可显示说明文字。
- 事件策略**不进策略池**（`StrategyPoolDialog` 的 `available` 过滤 event），因此 run-all、
  池计数、监控映射等选股核心工作流零改动。
- 回测页（`/backtest`）策略下拉两类并列，事件策略带徽标，并隐藏互斥选项（分钟成交等）。
- 叠加策略子策略选择器、监控规则选择器均过滤事件策略。

### 2.3 fail-closed 兜底（能力边界的三层防线）

1. **引擎层**：`StrategyEngine.run()` 对 `execution_backend == "event"` 直接报错。
2. **规则层**：监控规则创建时校验策略能力，拒绝引用事件策略。
3. **UI 层**：列表 API 透出 `execution_backend`（已有），前端各选择器按能力过滤。

## 3. 策略文件契约

沿用模块级常量 + 保留字函数的项目风格。事件策略文件：

| 元素 | 类型 | 说明 |
| --- | --- | --- |
| `META` | 配置 | 与现状同构（id/name/description/tags/asset_types/params/basic_filter/scoring/order_by 等）。`timeframes` 必含 `1d`。新增可选键 `history_bars`（默认 60）：脚本最大历史窗口交易日数，决定 warmup 与 `context.history()` 上限 |
| `EXECUTION_BACKEND = "event"` | 配置 | 唯一硬标识 |
| `REQUIRED_FEATURES` | 配置 | **必填**。声明脚本可读的数据列（指标列、`csg_*` 信号、基础列、财务因子名）。脚本只能读声明过的列，未声明列不在视图内（fail-closed）。复用现有 `StrategyDependencyResolver` 解析 |
| 风控常量 | 配置 | `STOP_LOSS / TAKE_PROFIT / TRAILING_STOP / TRAILING_TAKE_PROFIT_ACTIVATE / TRAILING_TAKE_PROFIT_DRAWDOWN / MAX_HOLD_DAYS` 照旧，支持 override；引擎逐 bar 自动评估，**脚本不写风控** |
| `initialize(context)` | 代码（可选） | run 开始一次，初始化状态 |
| `before_trading_start(context)` | 代码（可选） | T 日开盘前，只读 |
| `handle_data(context)` | 代码（**必填**） | T 日收盘后，唯一可下单入口，**单参数** |
| `after_trading_end(context)` | 代码（可选） | T 日撮合完成后，只读（可写状态做记录） |

**没有** `ENTRY_SIGNALS/EXIT_SIGNALS`，**没有** `filter/filter_history`。

加载校验（`strategy/engine.py` 加载期）：

- 缺 `handle_data` → 走现有 `load_errors` 通道报中文错误；
- `timeframes` 不含 `1d` → 拒绝；
- `REQUIRED_FEATURES` 缺失 → 拒绝（事件策略不允许全量特征回退，避免脚本偷读未声明列）。

## 4. context API 契约

### 4.1 时钟节奏与数据可见性

| 时相 | 调用点 | 可见数据 | 下单 |
| --- | --- | --- | --- |
| `before_trading_start` | T 开盘前 | `history()` 仅含 ≤ T-1 完成态日K | 否 |
| `handle_data` | T 收盘后 | T 日完整日K可见；`universe` = T 日候选面板 | **是** |
| `after_trading_end` | T 收盘后、当日订单撮合完成后 | 同 handle_data + 当日订单结果 | 否 |

锚点与现有回测口径对齐：**收盘后决策 → 次日开盘成交** 即现有默认 `entry_fill="open_t+1"`；
`close_t` 保留"信号当日收盘成交"的既有近似语义。事件模式不发明第三种成交口径，成交
时点完全由现有 `entry_fill/exit_fill` 配置决定。

### 4.2 配置与时间（只读）

```text
context.params        # dict: META 默认 + override 解析后的参数
context.current_date  # date: T
context.previous_date # date | None: T-1 交易日
context.asset_type    # str
```

### 4.3 数据视图（只读、防未来函数、polars 风格）

```text
context.universe      # pl.DataFrame: T 日候选面板，一行一标的
context.history(n)    # pl.DataFrame: 最近 n 个完成交易日窗口，按 symbol, date 排序
```

`universe` 列契约：

- 固定基础列：`symbol/date/name/open/high/low/close`（前复权）/`volume/amount`；
  涨跌停判定所需的原始价列按需附带（沿用"前复权计算、原始价判涨跌停"契约）。
- 声明列：`REQUIRED_FEATURES` 中的指标/信号/因子列，由依赖解析器在循环外预计算
  （性能关键：脚本只做切片过滤，不逐行重算）。
- 派生列：`score/rank` 仅在 `META.scoring` 非空时物化（与选股页同一 scoring 逻辑）。
- 已过滤：basic_filter 已应用（含非股票资产的既有中和逻辑），未过标的**不出现在
  universe**。

`history(n)` 约束：`n ≤ META.history_bars`，超限运行时报错；窗口是否含 T 由时相决定。

**不提供** `get_price(symbol, field, n)` 等标量接口；单标的时间序列用
`context.history(n).filter(pl.col("symbol") == "...")`，全项目统一 DataFrame 风格。

### 4.4 组合状态（只读视图）

```text
context.cash          # float: 可用现金（T 收盘后，当日卖出回款已入账）
context.total_value   # float: 现金 + 持仓市值（T close）
context.positions     # pl.DataFrame: symbol/name/entry_date/entry_price/shares/market_value/unrealized_pnl_pct
context.orders_today  # list[dict]: 当日订单 {symbol, side, requested, filled, status, reject_reason}
```

### 4.5 下单 API（v1 两个方法）

```text
context.order_buy(symbol, amount)        # 按金额买入；向下取整手（100 股），零头回现金
context.order_sell(symbol, shares=None)  # shares=None 表示清仓
```

无返回值（成交异步，结果见 `orders_today`）。`order_target_pct` 留二期。

### 4.6 订单生命周期与拒绝语义

**直接拒绝并记录原因（不静默钳制）：**

- 现金不足 / 取整后不足一手；
- 持仓数达 `max_positions`、总仓位超 `max_exposure_pct`（现有硬约束继续生效）；
- 当日买入标的再卖出（T+1）；
- 标的不在当日 universe。

**转挂单（pending，次日重试，run 结束未成交作废并计入统计）：**

- 停牌、涨停不能买、跌停不能卖——复用现有 `pending_exit`/`tradable` 语义，不写第二套。

**成交顺序（确定性承诺）：**

- 同一日卖出先处理（回款当日可用于买入，符合 A 股实际），同侧按脚本下单顺序；
- 风控订单由引擎独立评估，退出优先级沿用：挂单 > 风控 > 信号 > max_hold > end；
- 脚本订单与风控订单共用同一共享订单路由，费用/滑点口径与 `MatcherConfig` 一致。

### 4.7 状态与可复现性

- `context.state`（推荐 dict）：用户状态对象，`initialize` 初始化、各时相读写；
  每次 run / 优化 trial 独立实例，运行结束丢弃。
- 同配置同数据两次运行结果一致，由"成交顺序固定 + 面板循环外预计算"保证。

## 5. 执行链

### 5.1 当前链路（基线）

```text
前端回测表单 → API 守卫（区间/分钟K覆盖）→ StrategyBacktestConfig
→ make_worker_task → run_worker_task（spawn 子进程）
→ StrategyEngine 加载策略 + override → StrategyBacktestService.run
→ backend 分派（matrix_native / polars_expr / minute_filter / composite）
→ 信号面板/矩阵 → _simulate_independent_matrix 撮合（T+1/涨跌停/费用/挂单/风控）
→ stats_v2 → StrategyBacktestResult → SSE/HTTP → 前端渲染
```

### 5.2 事件回测链路（目标）

```text
① 入口复用：前端回测页 → POST /api/backtest/strategy/run 或 SSE /stream
   → API 守卫：区间上限；backend=event 时校验成交选项互斥（分钟成交不可用）
   → StrategyBacktestConfig（表单字段全量复用）

② worker 复用：spawn 子进程 → DataStore/KlineRepository + 因子注册表
   → StrategyEngine 加载策略（新增摘取 initialize/handle_data 等函数）
   → StrategyBacktestService.run 分派到 event 分支

③ 一次性数据准备（复用面板路径）：
   StrategyDependencyResolver 解析特征计划（日频 → polars_expr 面板路径）
   → load_panel_for_backtest（warmup + 正式区间 + mode=full 尾部窗口）
   → 按需 compute_indicators / compute_signals / compute_limit_signals
   → 财务因子按公告日门控附加（如引用）
   → 循环外预计算每日 universe 掩码（basic_filter + 涨跌停信号，向量化）

④ 事件循环（新，逐交易日推进）：
   for T in 交易日序列（面板日期列，升序）:
     ├─ before_trading_start(context)          [可选时相]
     ├─ handle_data(context)                    [脚本主入口，产生订单]
     ├─ 收盘撮合（映射 entry_fill/exit_fill，经共享订单路由）
     ├─ 风控评估（既有持仓，优先级同现状）
     └─ after_trading_end(context)              [可选时相]
   每 N 天: progress_cb 上报 + cancel_event 检查（复用现有 SSE 进度语义）

⑤ 收尾与结果复用：
   mode=full 尾部窗口继续跑离场 → 期末强平
   → TradeRecord 列表（结构原样复用；事件策略 entry_signal_id 等信号字段留空）
   → stats_v2 统计 + 因子归因（下单时快照 universe 因子行，按 (symbol, date) 关联成交）
   → StrategyBacktestResult → worker 序列化 → SSE done / HTTP 返回 → 前端渲染（零改动）
```

### 5.3 开发清单（代码）

| # | 位置 | 类型 | 内容 |
| --- | --- | --- | --- |
| D1 | `backtest/order_router.py` | 新增 | 共享订单路由：从 `_simulate_independent_matrix` 抽出 `_can_buy/_can_sell/_one_price_limit`、费用/滑点、T+1 判定 |
| D2 | `backtest/engine.py` | 改造 | 矩阵撮合改为调用 D1（纯重构、行为不变，先做） |
| D3 | `backtest/event_engine.py` | 新增 | 时钟与三时相调度、逐日推进、progress/cancel |
| D4 | `backtest/event_context.py` | 新增 | §4 的 context API：数据视图（防未来函数）、只读组合状态、下单方法、状态对象 |
| D5 | `backtest/event_portfolio.py` | 新增 | 组合状态机：现金/持仓/订单簿/挂单、每 bar 风控评估、退出优先级 |
| D6 | `backtest/strategy.py` | 改造 | `run()` 加 event 分支（复用面板加载与特征解析）；结果组装与因子归因复用 |
| D7 | `strategy/engine.py` | 改造（小） | `_load_file` 摘取事件函数；backend="event" 校验（handle_data 必填、timeframes 含 1d、REQUIRED_FEATURES 必填）；`run()` 对 event fail-closed |
| D8 | `api/backtest.py` / `api/strategy.py` | 改造（小） | backend 枚举加 event；event 的守卫与互斥校验 |
| D9 | 前端 `Screener.tsx` / `StrategyCard` / `StrategyBuilderDialog` / `StrategySettingsDialog` / `StrategyPoolDialog` / `lib/api.ts` | 改造 | 两态切换器、事件卡片变体、EVENT_TEMPLATE、设置弹窗按 backend 隐藏信号面板、池过滤、类型同步 |
| D10 | `strategy/prompts/` + `ai_generator.py` | 二期可选 | 事件策略 AI 模板与禁区清单，沿用 ast 校验 |
| D11 | `backend/tests/` | 新增 | 见 §10 |

### 5.4 配置清单（配置物）

| # | 配置物 | 内容 |
| --- | --- | --- |
| C1 | 策略文件脚本段 | `initialize / handle_data` 等（用户写） |
| C2 | `META["execution_backend"]` | `"event"`（唯一必须新增的 META 键，另有可选 `history_bars`） |
| C3 | `META["params"]` | 沿用，UI 编辑 → override → `context.params` |
| C4 | 风控配置 | 继续放 META/override，脚本不写风控 |
| C5 | universe | 回测表单 `symbols` + `basic_filter`，复用资产类型中和逻辑 |
| C6 | 回测表单 | 全部复用现有字段（start/end、资金、费用、fill、仓位上限、mode 等），零新增 |
| C7 | 数据 | enriched 日线 parquet、instruments、历史股本、财务快照，零新增 |

## 6. 创建链路差异

| 环节 | 现状 | 事件模式 |
| --- | --- | --- |
| 保存入口 | `POST /api/strategy/code/save` → `_save_strategy_code` | **完全复用**（ID 校验、目录落盘、META 归一、ast 安全校验、写文件 → reload → 失败回滚） |
| 文件结构 | META + 常量 + filter 函数 | 同上 + 事件函数段，`execution_backend="event"` |
| 保存前/后校验 | csg 信号存在性 | 加载期校验：handle_data 必填、timeframes 含 1d、REQUIRED_FEATURES 必填 |
| AI 生成 | filter 型模板 | event 模板 + 禁区清单（不写风控、不硬编码股票池、只读声明列） |
| 运行时门控 | 所有策略可进选股/监控 | 事件策略仅回测（§2.3 三层防线） |
| 目录与 ID | `data/strategies/custom/`（`custom_`）/ `ai/`（`ai_`） | **不动**，分类依据是能力（backend）而非目录 |

## 7. 结果契约与可观测性

- `TradeRecord` 结构原样复用；`exit_reason` 枚举一致；
  `entry_signal_id/entry_signal_date` 对事件策略留空（无信号列）。
- stats 新增：`orders_total / orders_rejected_by_reason / pending_expired`。
- 因子归因照旧可用（§5.2 ⑤）。
- 优化/walkforward：事件 trial 慢，一期建议 event 后端默认关闭蒙特卡洛重采样、
  限制网格规模；`PreparedMatrixBacktest` 的矩阵共享不适用，但 PanelCache 可复用。

## 8. 约束对齐（CONTRIBUTING）

1. **防未来函数**（§5.3 红线）：context 只暴露 ≤ 当前时相的数据；日线窗口在收盘前
   时相排除当日完成态；测试矩阵含"未来函数探针策略"。
2. **成交约束**（§5.3）：T+1、涨跌停不可成交、费用/滑点全部经共享订单路由 D1，
   与矩阵撮合同一实现，杜绝第二套口径。
3. **复权契约**（§3.2）：视图价格前复权；涨跌停判定用原始价（复用
   `compute_limit_signals`）。
4. **交易日/时区**（§3.3）：时钟由面板交易日序列驱动，不用自然日；日频无盘中时区
   问题。
5. **公告日门控**（§3.4）：财务因子经现有 `fundamentals` 附加逻辑，不提前泄露。
6. **插件化**（§4）：不直接依赖任何数据源 SDK；日频只用 enriched 面板，无 provider
   能力依赖。
7. **缓存**（§6）：事件回测复用 PanelCache 与 enriched generation 快照语义；脚本
   状态与缓存无耦合。
8. **性能**（§6.3）：特征全部循环外向量化预计算，循环内仅切片与决策；日频为主路径，
   不做分钟级。
9. **兼容**（§8）：现有四种 backend 与历史策略/配置/结果文件完全不受影响；event
   为纯增量后端。
10. **文档纪律**：本文为设计草案；实现阶段在 `docs/strategy.md` 与
    `backend/app/strategy/prompts/strategy-guide.md` 同步事件策略开发规范，不得将本
    文引用的规划 API 当作已实现能力（`docs/secondary-development.md` 状态表约束）。

## 9. 分阶段实施计划

| 阶段 | 内容 | 完成标准 |
| --- | --- | --- |
| Phase 0 | 共享订单路由重构（D1/D2） | 矩阵撮合行为不变，既有回测测试全绿 |
| Phase 1 | 日频事件引擎（D3/D4/D5/D6/D7/D8/D11）+ 前端两态视图（D9） | §10 验证矩阵通过；标准结果契约端到端可用 |
| Phase 2 | 运维增强：event 优化限制、AI 模板（D10）、文档（strategy.md/strategy-guide.md） | 优化/步进可用；AI 可生成事件策略 |
| 远期（单独立项） | 分钟级 `handle_bar` 与盘中调度；实盘桥接；`order_target_pct`；composite 事件子策略 | 各自立项后再评估 |

### 实盘桥接预留（本期不做）

仅一件事：`handle_data` 收到的 universe 面板字段结构与现有 enriched 契约对齐
（§4.3），未来实时行情流可驱动同一脚本而无需改契约。不实现任何实时驱动代码。

## 10. 验证矩阵

- 未来函数探针：脚本访问 T+1 数据必须失败；`before_trading_start` 读不到 T 日完成态。
- 成交口径：entry_fill/exit_fill 各组合下，订单成交日与成交价逐例断言。
- 退出优先级：挂单 > 风控 > 信号 > max_hold > end，构造同时触发场景验证。
- 拒绝语义：现金不足、整手不足、T+1 卖出、max_positions/max_exposure 超限、标的不在
  universe 五类拒绝，断言 `orders_today` 拒绝原因。
- 涨跌停/停牌：涨停买入转挂单、跌停卖出转挂单、停牌不成交、次日恢复重试、期末作废。
- 确定性：同配置同数据两次运行，trades/equity/stats 完全一致。
- 风控：止损/止盈/移动止损/移动止盈与既有枚举、成交价一致。
- 财务因子：公告日门控边界日断言（引用财务列的脚本）。
- 兼容性：现有四种 backend 策略的回测、优化、历史结果读取不受影响。
- 前端：`pnpm build`；两态切换、事件卡片、设置弹窗面板隐藏、池过滤手工检查。

## 11. 明确不做 / 待定

- **不做（v1）**：分钟级、盘中调度、composite 子策略、监控/选股能力、实盘桥接、
  `order_target_pct`、事件策略的当日多次调用（每时相一次）。
- **待定**：事件策略的 `position_sizing`（equal/score_weight）在事件模式下语义待定
  （脚本自行决定仓位，引擎仅兜底上限），Phase 1 可先忽略该字段。

## 12. 实施任务列表

按依赖顺序排列；每项含涉及模块与验证方式（对齐 CONTRIBUTING §9 验证矩阵）。
"前置"为空或满足后可并行开工。

### Phase 0：共享订单路由重构（纯重构，行为不变）

| 任务 | 内容 | 涉及模块 | 前置 | 验证 |
| --- | --- | --- | --- | --- |
| T0.1 | 盘点约束逻辑清单：`_valid_price / _present / _one_price_limit / _can_buy / _can_sell` 与 `MatcherConfig` 费用模型的输入输出，确定共享接口签名 | `backtest/engine.py` | — | 无代码改动，产出接口说明 |
| T0.2 | 新建 `backtest/order_router.py`：涨跌停/停牌/价格有效性判定 + 费用/滑点计算，输入为标量或数组（可复用于事件组合层） | 新增 `backtest/order_router.py` | T0.1 | 新增单测：每个拒绝原因逐一断言 |
| T0.3 | 矩阵撮合 `_simulate_independent_matrix` 改为调用共享路由，删除内嵌副本 | `backtest/engine.py` | T0.2 | 既有回测测试全绿；`git diff` 确认无行为差异 |
| T0.4 | 回归：全量回测相关测试 + 优化/步进冒烟 | `backend/tests/` | T0.3 | 定向 pytest 全过，无 Ruff 告警 |

### Phase 1：日频事件引擎与前端视图

| 任务 | 内容 | 涉及模块 | 前置 | 验证 |
| --- | --- | --- | --- | --- |
| T1.1 | 加载器扩展：`StrategyDef` 加事件函数字段；`_load_file` 摘取 `initialize/handle_data/before_trading_start/after_trading_end`；backend="event" 校验（handle_data 必填、timeframes 含 1d、REQUIRED_FEATURES 必填）；`StrategyEngine.run()` 对 event fail-closed | `strategy/engine.py` | Phase 0 | 加载单测：缺函数/缺字段/非法 timeframes 报错进 `load_errors`；run 拒绝事件策略 |
| T1.2 | `EventContext`：§4 全部契约（配置/时间、universe/history 防未来函数视图、只读组合状态、state、order_buy/order_sell） | 新增 `backtest/event_context.py` | T1.1 | 单测：时相可见性边界、history_bars 上限、未声明列不可见 |
| T1.3 | 组合状态机：现金/持仓/订单簿；整手取整、T+1、max_positions/max_exposure、五类拒绝原因记录；挂单重试；风控评估（退出优先级）；卖出先于买入、顺序确定性 | 新增 `backtest/event_portfolio.py` | T1.1（与 T1.2 可并行） | 单测：五类拒绝、挂单生命周期、风控优先级、确定性（同配置跑两次逐字段一致） |
| T1.4 | 时钟引擎：交易日推进、三时相调用、订单→路由→撮合编排、progress/cancel、mode=full 尾部、期末强平 | 新增 `backtest/event_engine.py` | T1.2、T1.3、T0.2 | 集成测试：端到端小样本跑通，结果结构符合 `StrategyBacktestResult` |
| T1.5 | 回测服务接入：`StrategyDependencyResolver.resolve` 支持 event（面板路径、REQUIRED_FEATURES 必填、history_bars→warmup）；`run()` 加 event 分支；stats 新字段（orders_total 等）；因子归因快照 | `backtest/strategy.py` | T1.4 | 回测测试：成交日/价对齐 entry_fill/exit_fill；因子归因可用 |
| T1.6 | API：backend 枚举加 event；event 守卫与互斥校验（分钟成交禁用）；列表/详情透出 capability 相关字段 | `api/backtest.py`、`api/strategy.py` | T1.5 | API 测试：event 回测成功/守卫拒绝/无数据路径 |
| T1.7 | 验证矩阵 §10 全项测试：未来函数探针、成交口径、退出优先级、拒绝语义、涨跌停挂单、确定性、风控、财务因子门控、兼容性 | `backend/tests/` | T1.6 | 定向 pytest 全过；未实现前测试须在旧代码上失败 |
| T1.8 | 前端：两态切换器、事件卡片变体（无点击/无运行/无监控/仅设置）、设置弹窗按 backend 隐藏信号面板、策略池过滤 event、回测页徽标与互斥项隐藏、`lib/api.ts` 类型同步 | `Screener.tsx`、`StrategyCard.tsx`、`StrategySettingsDialog.tsx`、`StrategyPoolDialog.tsx`、`backtest/StrategyBacktest.tsx`、`lib/api.ts` | T1.6（契约定稿即可并行） | `pnpm build` 通过；两态切换/卡片/弹窗手工检查 |
| T1.9 | 端到端联调：一个示例事件策略（如 MACD 金叉买/死叉卖 + 持仓上限）从创建→回测→结果页全链路 | 全链路 | T1.7、T1.8 | 手工验收 + 截图；结果与同逻辑 matrix 策略口径可解释 |

### Phase 2：运维增强与文档

| 任务 | 内容 | 涉及模块 | 前置 | 验证 |
| --- | --- | --- | --- | --- |
| T2.1 | 优化/步进对 event 的限制：默认关蒙特卡洛重采样、网格规模上限提示 | `backtest/optimizer.py`、`walkforward.py` | Phase 1 | 优化测试：event 策略参数网格跑通；限制生效 |
| T2.2 | AI 事件模板与禁区清单（不写风控、不硬编码股票池、只读声明列） | `strategy/prompts/`、`ai_generator.py` | Phase 1 | 生成示例策略通过 ast 校验并加载成功 |
| T2.3 | 文档：`docs/strategy.md`、`strategy-guide.md` 增事件策略开发规范；本文档状态从"设计草案"更新 | `docs/`、`prompts/` | Phase 1 | 文档与实现一致（引用真实 API） |
| T2.4 | 收尾：全量回归 + `git diff --check` + PR 描述按 CONTRIBUTING §10 八项 | 全仓 | 全部 | 全量测试/构建通过；无无关改动 |
