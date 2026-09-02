"""扶摇(同花顺金融数据 API)内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

当前实现数据集: realtime (A 股全市场快照, 分页)、daily (日K 原始价)、
adj_factor (复权事件流 → 每股事件 pre/post 比值推导)、
financial (利润表/资产负债表/现金流量表 + 财务指标; 无历史股本表)。
未声明 minute → provider_has_dataset 为 False, 自动回退 tickflow。

单位口径 (CONTRIBUTING §3.1, 不可凭字段名推断):
  - 扶摇 price_change_ratio_pct 为百分数数值 (1.74 = +1.74%), 本项目 realtime
    change_pct 契约为小数制 (0.0174 = 1.74%) → 此处显式 / 100。
  - volume 单位股、turnover 单位元, 与内部契约一致, 直接透传。
  - 财务指标 value 为百分数数值 (15.71 = 15.71%), 直接透传 (内部契约同口径)。

财务口径 (用户已确认采用「保守法定披露日」方案, 2026-08):
  - fuyao 不提供真实公告日 (report_date_ms 实测为数据刷新日, 相邻两年年报共享
    同一时间戳, 不可用作公告日)。metrics 表的 announce_date 取法定披露截止日
    (年报 4/30、中报 8/31、季报 4/30/10/31) —— 真实公告必不晚于该日,
    因子生效只可能偏晚、绝不提前 (无未来函数, CONTRIBUTING §5.3)。
  - fuyao indicators 无 bps (每股净资产): metrics 表保留 bps 列但恒为 null,
    使 fundamentals 可加载 (pb 因子全 null, 其余 6 个财务因子可用)。
  - shares 表 (历史股本) fuyao 不提供: 返回空表, 下游按 §3.4 回退最新维表股本。

复权口径 (CONTRIBUTING §3.2):
  - 日K 取 adjust=none 原始价; 前复权由 enriched 管道用 ex_factor 自算
    (pipeline._apply_adj_factor), 不落库扶摇 adjust=forward 序列 —— 实测扶摇
    前复权采用「累计分红扣除」式算法, 非乘法累积因子口径, 与项目约定不一致。
  - ex_factor 为每股事件 pre/post 比值 (非累积): C(1+b)/(C-d), C=除权日前收盘
    (同源日K raw), d=每股现金分红, b=每股送股比例。REST 事件流不含配股字段
    (仅全市场 dump 含 allotment_ratio/allotment_price), 含配股事件无法精确
    推导 → 跳过并告警, fail-closed 不伪造因子。
"""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from datetime import date as _date
from datetime import time as _time

import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.plugins.fuyao import client as fuyao_client
from app.plugins.fuyao.client import FuyaoClient, FuyaoError
from app.tickflow.rate_limits import chunked

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("realtime", "daily", "adj_factor", "financial")

API_KEY_ENV = "FUYAO_API_KEY"
SECRETS_FIELD = "fuyao_api_key"  # UI 配置的 Key 存 secrets.json, 优先级高于 .env

# 请求批次(单标的串行请求, 与 stocksdk 对齐, 分批仅用于进度反馈/超时控制)
_BATCH = 40
# 除权日前收盘回看余量(天): 事件日之前最近一个交易日可能在事件窗口起点之前
_PRIOR_CLOSE_MARGIN_DAYS = 60

_SH_TZ = timezone(timedelta(hours=8))  # 显式北京时间, 不依赖服务器时区 (§3.3)

# ---- 财务 (fuyao 单标的 REST, 逐标的串行 + 请求间隔缓解限频) ----
_FIN_LATEST_PERIODS = 4   # latest_only=True 的报告期数 (对齐 financial_analyzer._MAX_PERIODS)
_FIN_FULL_PERIODS = 12    # latest_only=False 的报告期数 (近 3 年季度; 全市场同步耗时长, 见文档)
_FIN_WALK_CAP = _FIN_FULL_PERIODS + 6  # 指标报告回走上限(容忍未披露报告期)
_FIN_REQUEST_INTERVAL_S = 0.1  # 财务请求间隔, 降低触发限频 (code=4001) 的概率

# indicators index_id → 内部 metrics 列 (实测 2026-08: 全集跨标的/跨报告期稳定)
_METRICS_INDEX_MAP = {
    "calculate_operating_income_yoy_growth_ratio": "revenue_yoy",
    "calculate_parent_holder_net_profit_yoy_growth_ratio": "net_income_yoy",
    "sale_gross_margin": "gross_margin",
    "sale_net_interest_ratio": "net_margin",
    "index_weighted_avg_roe": "roe",
    "assets_debt_ratio": "debt_to_asset_ratio",
}
# 报告期 YYYY-Q → (period_end 月, 日) 与 (法定披露截止月, 日); Q4 截止日为次年 4/30
_REPORT_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_REPORT_DEADLINE = {1: (4, 30), 2: (8, 31), 3: (10, 31), 4: (4, 30)}

# 三张报表保留的数值列 (剔除 thscode/ticker/period/fiscal_period/currency/report_date_ms)
_STATEMENT_COLS = {
    "income": ["fiscal_year", "basic_eps", "operating_income", "operating_costs",
               "operating_expenses", "operating_profit", "profit_total", "net_profit",
               "parent_holder_net_profit", "income_tax_expense", "interest_expenses",
               "manage_fee", "sales_fee", "research_and_development_expenses"],
    "balance_sheet": ["fiscal_year", "total_current_assets", "non_current_nets_total",
                      "assets_total", "total_debt", "holder_equity_total", "cash",
                      "accounts_receivable"],
    "cash_flow": ["fiscal_year", "act_cash_flow_net", "invest_cash_flow_net",
                  "financing_cash_flow_net", "cash_equivalents_net_addition",
                  "pay_dividends_profits_interest_cash", "pay_fixed_assets_etc_cash"],
}
_STATEMENT_METHOD = {
    "income": "income_statements",
    "balance_sheet": "balance_sheets",
    "cash_flow": "cash_flow_statements",
}

# ---- 稳定性: 全市场逐标的 REST, 需间隔限流 + 瞬时错误指数退避重试 ----
# 实测 (2026-08): 全市场 5551 个请求连发会触发扶摇限流/服务端异常 (3002/5003),
# 大面积失败; 加间隔 + 重试后单次全量同步可正常完成。
_REQUEST_INTERVAL_S = 0.15  # 请求间隔, 降低触发限频 (code=4001) 的概率
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5  # 指数退避: 0.5s / 1.0s / 2.0s
# 瞬时错误 (网络 / 限流 / 服务端) 可重试; 业务错误 (参数/权限/标的不存在/数据未就绪) 不重试
_RETRYABLE_CODES = {4001, 5001, 5002, 5003}


def _is_retryable(e: FuyaoError) -> bool:
    code = e.code
    if code is None:
        return True  # 网络/解析类失败
    try:
        return int(code) in _RETRYABLE_CODES
    except (TypeError, ValueError):
        return False


def _call_with_retry(fn, *, label: str):
    """瞬时错误 (4001/5xxx/网络) 指数退避重试 ≤ _MAX_RETRIES 次; 业务错误直接抛。

    重试耗尽抛 FuyaoError, 由调用方按单标的软失败处理。
    """
    last: FuyaoError | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn()
        except FuyaoError as e:
            last = e
            if not _is_retryable(e):
                raise
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_BASE_S * (2 ** attempt))
    assert last is not None
    raise last


def get_api_key() -> str:
    from app import secrets_store
    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: API Key 已配置(secrets.json 或 .env)才注册为可切换数据源。不抛异常。"""
    if get_api_key():
        return True, "ok"
    return False, f"未配置 {API_KEY_ENV}(可在设置页数据源卡片中直接填写)"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Key 实探一次快照接口(先探后存, 对齐 /tickflow-key 语义)。不落盘。"""
    client = None
    try:
        client = fuyao_client.FuyaoClient(api_key=api_key, timeout=10.0)
        client.snapshot_page(limit=1)
        return True, "ok"
    except FuyaoError as e:
        return False, f"Key 无效或网络失败: {e}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


@dataclass
class _FuyaoConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "fuyao"
    display_name: str = "fuyao"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(row: dict, *names: str):
    """按优先级取第一个非 None 字段。实测字段名与官方文档示例不一致, 两者兼容。"""
    for n in names:
        if row.get(n) is not None:
            return row.get(n)
    return None


def _map_snapshot_row(row: dict, fetched_ms: int) -> dict | None:
    """扶摇快照行 → 内部 realtime record。字段缺失时按依赖推导, 不伪造数据。

    实测字段(2026-08): high_price / low_price / prev_price;
    官方文档示例: highest_price / lowest_price / prev_close_price。两者都取。
    """
    symbol = row.get("thscode")
    if not symbol:
        return None
    last = _to_float(row.get("last_price"))
    prev = _to_float(_first(row, "prev_price", "prev_close_price"))

    # 百分数 (1.74 = +1.74%) → 小数制 (0.0174), 契约见模块 docstring
    pct = _to_float(row.get("price_change_ratio_pct"))
    change_pct = pct / 100.0 if pct is not None else None

    change_amount = _to_float(row.get("price_change"))
    if change_amount is None and last is not None and prev is not None:
        change_amount = last - prev
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        # 与 quote_service 的推导同口径: 小数制, 不乘 100
        change_pct = change_amount / prev

    return {
        "symbol": symbol,
        "name": row.get("name"),  # 快照无名称, 由下游维表关联
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("open_price")),
        "high": _to_float(_first(row, "high_price", "highest_price")),
        "low": _to_float(_first(row, "low_price", "lowest_price")),
        "volume": _to_float(row.get("volume")),
        "amount": _to_float(row.get("turnover")),
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": None,      # 快照未提供, 不启发式计算
        "turnover_rate": None,  # 需股本口径 (§3.4), 交给 enriched 管道用历史股本计算
        "timestamp": fetched_ms,
        "session": None,
    }


# ---- 时间与复权辅助 (显式北京时间, §3.3) ----

def _to_ms(dt) -> int | None:
    """datetime/date → 毫秒 Unix 时间戳。naive 输入按 Asia/Shanghai 墙钟解释。

    naive datetime.timestamp() 会隐式用服务器时区, 这里显式补 +08:00 避免歧义。
    """
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, _date) and not isinstance(dt, datetime):
        dt = datetime.combine(dt, _time.min)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SH_TZ)
    return int(dt.timestamp() * 1000)


def _sh_date(ms_value: int) -> _date:
    """毫秒 → Asia/Shanghai 日历日。date_ms 语义为上海零点 (实测), 直接取 UTC 日会偏一天。"""
    return datetime.fromtimestamp(int(ms_value) / 1000, tz=UTC).astimezone(_SH_TZ).date()


def _fmt_date(dt) -> str | None:
    """datetime/date → YYYY-MM-DD (事件流 from/to 参数格式)。"""
    if dt is None:
        return None
    if isinstance(dt, (datetime, _date)):
        return dt.strftime("%Y-%m-%d")
    return str(dt)


def _close_by_date(bars: list[dict]) -> dict[_date, float]:
    """历史K行 → {日期: 收盘价} (用于除权事件前收盘查找)。"""
    out: dict[_date, float] = {}
    for bar in bars:
        ms_v = bar.get("date_ms")
        close = _to_float(bar.get("close_price"))
        if ms_v is None or close is None:
            continue
        out[_sh_date(ms_v)] = close
    return out


def _daily_frame(symbol: str, rows: list[dict]) -> pl.DataFrame:
    """历史K行 → 内部 daily 契约 [symbol, date, open, high, low, close, volume, amount]。

    date_ms 为 Asia/Shanghai 零点毫秒, 显式转上海时区取日; 字段改名后走
    normalize_daily (与 stocksdk 同路径: float 化 + 停牌过滤 + 规范列选择)。
    """
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows).with_columns(
        pl.from_epoch(pl.col("date_ms").cast(pl.Int64), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone("Asia/Shanghai")
        .dt.date()
        .alias("date"),
        pl.lit(symbol).alias("symbol"),
    ).rename({
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "turnover": "amount",
    })
    return normalize_daily(df, source="fuyao")


def _adj_factor_frame(symbol: str, events: list[dict], client: FuyaoClient) -> pl.DataFrame:
    """复权事件流 → [symbol, trade_date, ex_factor] (每股事件 pre/post 比值)。

    ex_factor = C*(1+b)/(C-d), C=除权日前收盘 (同源日K raw)。前收盘窗口从
    最早事件前 _PRIOR_CLOSE_MARGIN_DAYS 天起, 覆盖所有事件的除权参考价计算。
    缺前收盘 / 前收盘异常 / 无有效事件参数 → 跳过并告警 (fail-closed)。
    """
    ex_dates = sorted(_sh_date(e["ex_date_ms"]) for e in events if e.get("ex_date_ms"))
    if not ex_dates:
        return pl.DataFrame()
    start_ms = _to_ms(ex_dates[0] - timedelta(days=_PRIOR_CLOSE_MARGIN_DAYS))
    end_ms = _to_ms(ex_dates[-1])
    try:
        bars = _call_with_retry(
            lambda: client.historical(symbol, start_ms, end_ms, adjust="none"),
            label=f"adj-daily {symbol}",
        )
    except FuyaoError as e:
        logger.warning("扶摇除权前收盘日K拉取失败(%s): %s", symbol, e)
        return pl.DataFrame()
    closes = _close_by_date(bars)
    rows: list[dict] = []
    for e in events:
        ex_ms = e.get("ex_date_ms")
        if ex_ms is None:
            continue
        ex_date = _sh_date(ex_ms)
        d = _to_float(e.get("dividend_per_share")) or 0.0
        b = _to_float(e.get("per_share_bonus")) or 0.0
        if d <= 0 and b <= 0:
            logger.warning("扶摇除权事件无有效分红/送股参数(%s %s), 跳过", symbol, ex_date)
            continue
        prior = max((dt for dt in closes if dt < ex_date), default=None)
        prior_close = closes.get(prior) if prior else None
        if prior_close is None:
            logger.warning("扶摇除权事件缺前收盘(%s %s), 跳过 (fail-closed)", symbol, ex_date)
            continue
        if prior_close - d <= 0:
            logger.warning("扶摇除权事件前收盘异常(%s %s C=%s d=%s), 跳过", symbol, ex_date, prior_close, d)
            continue
        rows.append({
            "symbol": symbol,
            "trade_date": ex_date,
            "ex_factor": prior_close * (1 + b) / (prior_close - d),
        })
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort("trade_date")


# ---- 财务辅助 ----

def _latest_ended_report(now: datetime | None = None) -> tuple[int, int]:
    """当前最近的「已结束」报告期 (YYYY, Q)。

    以季度是否结束为准 (3/6/9/12 月末即结束), 不假设报告已披露 —— 未披露的
    报告期由 _metrics_frame 的回走逻辑跳过 (code=3002 继续往前)。
    """
    now = now or datetime.now(_SH_TZ)
    q = (now.month - 1) // 3 + 1
    if now.month % 3 == 0:  # 3/6/9/12 月: 本季度已结束
        return now.year, q
    if q == 1:  # 1-2 月: 最近结束的是上一年 Q4
        return now.year - 1, 4
    return now.year, q - 1


def _prev_report(year: int, quarter: int) -> tuple[int, int]:
    if quarter == 1:
        return year - 1, 4
    return year, quarter - 1


def _metrics_row(symbol: str, report: str, abilities: list[dict]) -> dict | None:
    """indicators abilities 数组 → 内部 metrics 行。

    报告期 YYYY-Q → period_end (季末); announce_date 取法定披露截止日 (保守,
    无未来函数, 见模块 docstring)。bps 恒为 null (fuyao 无该指标) —— 保留列使
    fundamentals 可加载, pb 因子全 null, 其余因子正常。
    """
    values: dict = {"bps": None}
    for block in abilities:
        for item in (block.get("indicators") or []):
            iid = item.get("index_id")
            if iid in _METRICS_INDEX_MAP:
                values[_METRICS_INDEX_MAP[iid]] = _to_float(item.get("value"))
    mapped = {k: v for k, v in values.items() if k != "bps" and v is not None}
    if not mapped:
        return None  # 无任何可用指标 → 不占位 (避免空行污染历史累积)
    year_str, q_str = report.split("-")
    year, q = int(year_str), int(q_str)
    end_m, end_d = _REPORT_END[q]
    dl_m, dl_d = _REPORT_DEADLINE[q]
    return {
        "symbol": symbol,
        "period_end": _date(year, end_m, end_d),
        "announce_date": _date(year + 1 if q == 4 else year, dl_m, dl_d),
        **values,
    }


def _metrics_frame(symbol: str, client: FuyaoClient, latest_only: bool) -> pl.DataFrame:
    """财务指标 → 内部 metrics 表 (symbol/period_end/announce_date + 指标列)。

    从最近已结束报告期向前回走, 逐期调用 indicators 端点 (单报告期必填)。
    未披露报告期 (code=3002) 继续往前; 其他错误告警后停止 (fail-closed)。
    返回按 period_end 升序。
    """
    wanted = _FIN_LATEST_PERIODS if latest_only else _FIN_FULL_PERIODS
    year, quarter = _latest_ended_report()
    rows: list[dict] = []
    tries = 0
    while len(rows) < wanted and tries < _FIN_WALK_CAP:
        report = f"{year}-{quarter}"
        tries += 1
        try:
            abilities = _call_with_retry(
                lambda r=report: client.financial_indicators(symbol, r),
                label=f"indicators {symbol} {report}",
            )
        except FuyaoError as e:
            if e.code == 3002:  # 数据尚未准备 → 继续往前 (其余错误含重试耗尽 → 停止)
                year, quarter = _prev_report(year, quarter)
                continue
            logger.warning("扶摇财务指标拉取失败(%s %s): %s", symbol, report, e)
            break
        row = _metrics_row(symbol, report, abilities)
        if row is not None:
            rows.append(row)
        year, quarter = _prev_report(year, quarter)
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort("period_end")


def _statement_frame(symbol: str, table: str, client: FuyaoClient, latest_only: bool) -> pl.DataFrame:
    """财务报表 → 内部表 (symbol/period_end + 数值列)。

    period_end_ms 为 Asia/Shanghai 零点 (与 date_ms 同语义), 显式转上海时区取日。
    report_date_ms 是数据刷新日非公告日, 直接剔除, 不落库误导下游。
    """
    method = getattr(client, _STATEMENT_METHOD[table])
    limit = _FIN_LATEST_PERIODS if latest_only else _FIN_FULL_PERIODS
    rows = _call_with_retry(
        lambda: method(symbol, period="quarterly", limit=limit),
        label=f"{table} {symbol}",
    )
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows).with_columns(
        pl.lit(symbol).alias("symbol"),
        pl.from_epoch(pl.col("period_end_ms").cast(pl.Int64), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone("Asia/Shanghai")
        .dt.date()
        .alias("period_end"),
    )
    keep = [c for c in ["symbol", "period_end"] + _STATEMENT_COLS[table] if c in df.columns]
    return df.select(keep).sort("period_end")


class FuyaoProvider:
    """扶摇数据源。realtime = A 股全市场快照(quote_service 全市场模式轮询调用)。"""

    name = "fuyao"
    builtin = True

    def __init__(self) -> None:
        self.config = _FuyaoConfig()
        self._client: FuyaoClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> FuyaoClient:
        if self._client is None:
            self._client = fuyao_client.FuyaoClient(api_key=get_api_key())
        return self._client

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。"""
        try:
            rows, server_ts = self._get_client().snapshot_all()
        except FuyaoError as e:
            logger.warning("扶摇实时行情拉取失败: %s", e)
            return []

        # 优先用服务端时间戳(行情归属); 缺失时退回本地时间
        fetched_ms = server_ts or int(time.time() * 1000)

        records = []
        dropped = 0
        for row in rows:
            rec = _map_snapshot_row(row, fetched_ms)
            if rec is not None:
                records.append(rec)
            else:
                dropped += 1
        if dropped and not records:
            # 整页都识别不出 thscode → 大概率接口 schema 变了, 明确告警而非静默空数据
            logger.warning("扶摇快照 %d 行全部缺少 thscode 字段, 疑似接口结构变化", dropped)
            return []
        logger.info("扶摇实时行情拉取完成: %d 条(丢弃 %d 行)", len(records), dropped)
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # 扶摇历史K仅覆盖 A 股, 非 stock 由调用方按空结果降级
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """日K: adjust=none 原始价 (前复权由 enriched 管道用 ex_factor 自算, §3.2)。

        扶摇历史K 单标的请求、窗口 ≤ 10 年 (客户端自动切片), 逐 symbol 串行 + 分批进度。
        单标的失败软跳过并告警, 不阻断整批 (与 stocksdk 行为一致)。
        """
        if not symbols:
            return pl.DataFrame()
        end_ms = _to_ms(end_time) or int(time.time() * 1000)
        start_ms = _to_ms(start_time) or (end_ms - 365 * 24 * 3600 * 1000)
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            for j, sym in enumerate(chunk):
                try:
                    rows = _call_with_retry(
                        lambda s=sym: self._get_client().historical(s, start_ms, end_ms, adjust="none"),
                        label=f"daily {sym}",
                    )
                except FuyaoError as e:
                    logger.warning("扶摇日K拉取失败(%s): %s", sym, e)
                    continue
                if not rows:
                    continue
                df = _daily_frame(sym, rows)
                if not df.is_empty():
                    frames.append(df)
                if j < len(chunk) - 1:
                    time.sleep(_REQUEST_INTERVAL_S)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # 复权事件流仅覆盖 A 股
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """除权因子: 复权事件流 → 每股事件 pre/post 比值 ex_factor (非累积)。

        事件流是原始事件 (官方文档明确), 推导见 _adj_factor_frame。start_time/end_time
        作为事件流 from/to 过滤 (增量同步), 前收盘日K 自带回看余量, 不受 from 影响。
        失败软跳过 (返回空 df → sync_adj_factor 按无新增处理)。
        """
        if not symbols:
            return pl.DataFrame()
        from_date = _fmt_date(start_time)
        to_date = _fmt_date(end_time)
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            for j, sym in enumerate(chunk):
                try:
                    events = _call_with_retry(
                        lambda s=sym: self._get_client().adjustment_factors(s, from_date, to_date),
                        label=f"adj-events {sym}",
                    )
                except FuyaoError as e:
                    # code=3002 "No adjustment events" 是合法的"无事件"响应, 不告警
                    if e.code != 3002:
                        logger.warning("扶摇除权事件拉取失败(%s): %s", sym, e)
                    continue
                if not events:
                    continue
                df = _adj_factor_frame(sym, events, self._get_client())
                if not df.is_empty():
                    frames.append(df)
                if j < len(chunk) - 1:
                    time.sleep(_REQUEST_INTERVAL_S)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- financial ----
    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """财务数据: metrics/income/balance_sheet/cash_flow (shares 不支持 → 空表)。

        签名对齐 custom.GenericHTTPProvider.get_financials (financial_sync 分流调用)。
        逐标的串行拉取 + 请求间隔, 单标的失败软跳过 (fail-closed), 不阻断整批。
        latest_only: True=最近 4 期, False=近 3 年 (12 期) —— 全市场全量同步耗时长,
        建议在数据页手动触发后台同步。
        """
        if table == "shares":
            logger.warning("扶摇未提供历史股本表(shares), 返回空表 (下游按 §3.4 回退最新维表股本)")
            return pl.DataFrame()
        if table not in _STATEMENT_METHOD and table != "metrics":
            logger.warning("扶摇财务表不支持: %s", table)
            return pl.DataFrame()
        frames: list[pl.DataFrame] = []
        for i, sym in enumerate(symbols):
            try:
                if table == "metrics":
                    df = _metrics_frame(sym, self._get_client(), latest_only)
                else:
                    df = _statement_frame(sym, table, self._get_client(), latest_only)
            except FuyaoError as e:
                logger.warning("扶摇财务拉取失败(%s %s): %s", table, sym, e)
                continue
            if not df.is_empty():
                frames.append(df)
            if i < len(symbols) - 1:
                time.sleep(_FIN_REQUEST_INTERVAL_S)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset == "realtime":
            try:
                rows, count = self._get_client().snapshot_page(limit=5)
            except FuyaoError as e:
                return {"provider": self.name, "dataset": "realtime", "rows": 0, "error": str(e)}
            fetched_ms = int(time.time() * 1000)
            head = [r for r in (_map_snapshot_row(row, fetched_ms) for row in rows) if r][:5]
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": count or len(head),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        symbols = symbols or ["600519.SH"]
        if dataset == "daily":
            return _preview(self.name, "daily", self.get_daily(symbols, None, None))
        if dataset == "adj_factor":
            return _preview(self.name, "adj_factor", self.get_adj_factors(symbols, None, None))
        if dataset == "financial":
            return _preview(self.name, "financial", self.get_financials("metrics", symbols, latest_only=True))
        return {"provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"扶摇插件未接入 {dataset} 数据集(自动回退 TickFlow)"}


def _preview(provider: str, dataset: str, df: pl.DataFrame) -> dict:
    return {
        "provider": provider,
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": df.head(5).to_dicts() if not df.is_empty() else [],
    }
