"""AKShare 免费数据源 provider。

方法签名对齐 custom.GenericHTTPProvider (service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

当前实现数据集: daily / adj_factor / minute / realtime / financial(含 shares)。
上游全部为新浪财经、巨潮资讯、沪深京交易所官网 —— 本网络实测可达
(东财系接口在本网络被阻断, 本插件不依赖)。

稳定性措施 (用户要求):
  - _call_with_retry: 指数退避重试 (0.6s/1.2s/2.4s, ≤3 次), 网络错误与空结果(可选)均重试
  - _REQUEST_INTERVAL_S: 单标的请求间隔, 降低新浪封 IP 风险 (文档明示多次获取易封)
  - 单标的失败软跳过并告警, 不阻断整批 (与 fuyao/stocksdk 一致)

口径 (CONTRIBUTING §3.1/§3.2/§3.3):
  - spot 涨跌幅为百分数 (2.983 = +2.983%) → change_pct 小数制显式 /100
  - daily/minute volume 单位股、amount 单位元, 与内部契约一致
  - adj_factor: 巨潮分红送配事件 (stock_dividend_cninfo, 送转/派息为每 10 股口径,
    /10 得每股) + 同源日K前收盘 → ex_factor = C*(1+b+z)/(C-d) (交易所除权参考价
    pre/post 比, 与 fuyao 口径一致, 实测除权日逐一吻合)。配股事件缺配股价字段
    → 跳过 (fail-closed)。注: 新浪 qfq 比值法实测在 2011 年后精确, 但 2001-2006
    年 qfq 数据损坏 (噪声达 135%), 故弃用比值法, 改用真实事件源
  - minute 新浪接口不支持日期窗口, 只返回最近 ~8 个交易日 (实测 1970 根 1 分钟);
    由调用方按 datetime 去重/截取
  - financial: 三张报表与股本表带**真实公告日** (新浪报表「公告日期」/巨潮「公告日期」);
    关键指标 (stock_financial_abstract) 无公告日 → 沿用保守法定披露日方案
    (年报 4/30、中报 8/31、季报 4/30/10/31, 无未来函数, 用户已确认)

akshare 为重量级依赖 (pandas/py_mini_racer/lxml), 懒加载: 首次调用才 import,
availability 探测与测试均不强制安装。
"""
from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from datetime import time as _time

import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.tickflow.rate_limits import chunked

# akshare 内部 requests.get 不传 timeout, 上游(新浪等)连接挂起会让请求永久阻塞、
# 拖死整个后端 worker (实测 2026-08: 全市场分钟同步挂起 → 服务无响应)。
# 设置进程级 socket 默认超时兜底: 30s 未响应即抛错, 由 _call_with_retry 重试/软跳过。
# 其他组件 (fuyao httpx timeout=20 / tickflow SDK) 均自带显式超时, 不受影响。
socket.setdefaulttimeout(30)

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "adj_factor", "minute", "realtime", "financial")

# 请求批次(单标的串行 + 间隔限流, 与 fuyao 对齐)
_BATCH = 40
# 请求间隔(秒): 新浪文档明示多次获取易封 IP, 保守限流
_REQUEST_INTERVAL_S = 0.35
# 指数退避重试: 0.6s / 1.2s / 2.4s
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.6
# 财务 latest_only 拉取的报告期数 (对齐 financial_analyzer._MAX_PERIODS)
_FIN_LATEST_PERIODS = 4

_SH_TZ = timezone(timedelta(hours=8))  # 显式北京时间, 不依赖服务器时区 (§3.3)

# 报告期 YYYY-Q → 法定披露截止日 (仅关键指标无公告日时使用, 保守无未来函数)
_REPORT_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_REPORT_DEADLINE = {1: (4, 30), 2: (8, 31), 3: (10, 31), 4: (4, 30)}

# 新浪财务报表科目 → 内部英文列名 (仅保留信息量大的科目, 缺列自动跳过)
_INCOME_MAP = {
    "营业总收入": "total_revenue",
    "营业收入": "operating_income",
    "营业成本": "operating_costs",
    "研发费用": "research_and_development_expenses",
    "销售费用": "sales_fee",
    "管理费用": "manage_fee",
    "财务费用": "financial_expense",
    "营业利润": "operating_profit",
    "利润总额": "profit_total",
    "所得税费用": "income_tax_expense",
    "净利润": "net_profit",
    "归属于母公司所有者的净利润": "parent_holder_net_profit",
    "基本每股收益": "basic_eps",
}
_BALANCE_MAP = {
    "流动资产合计": "total_current_assets",
    "非流动资产合计": "non_current_assets_total",
    "资产总计": "assets_total",
    "负债合计": "total_debt",
    "归属于母公司股东权益合计": "holder_equity_total",
    "所有者权益(或股东权益)合计": "equity_total",
    "货币资金": "cash",
    "应收账款": "accounts_receivable",
    "存货": "inventory",
    "固定资产净额": "fixed_assets",
}
_CASHFLOW_MAP = {
    "经营活动产生的现金流量净额": "act_cash_flow_net",
    "投资活动产生的现金流量净额": "invest_cash_flow_net",
    "筹资活动产生的现金流量净额": "financing_cash_flow_net",
    "现金及现金等价物净增加额": "cash_equivalents_net_addition",
    "分配股利、利润或偿付利息所支付的现金": "pay_dividends_profits_interest_cash",
    "购建固定资产、无形资产和其他长期资产所支付的现金": "pay_fixed_assets_etc_cash",
}
_STATEMENT_COLMAP = {
    "income": _INCOME_MAP,
    "balance_sheet": _BALANCE_MAP,
    "cash_flow": _CASHFLOW_MAP,
}
_STATEMENT_SINA_NAME = {
    "income": "利润表",
    "balance_sheet": "资产负债表",
    "cash_flow": "现金流量表",
}

# 关键指标 (stock_financial_abstract) 指标名 → 内部 metrics 列 (实测 2026-08)
_METRICS_LABEL_MAP = {
    "营业总收入增长率": "revenue_yoy",
    "归属母公司净利润增长率": "net_income_yoy",
    "毛利率": "gross_margin",
    "销售净利率": "net_margin",
    "净资产收益率(ROE)": "roe",
    "资产负债率": "debt_to_asset_ratio",
    "每股净资产": "bps",
    "营业总收入": "total_revenue",
    "归母净利润": "parent_holder_net_profit",
    "基本每股收益": "basic_eps",
}

# 全市场快照列 (新浪, 实测 2026-08)
_SPOT_COLS = ["代码", "名称", "最新价", "涨跌额", "涨跌幅", "买入", "卖出", "昨收", "今开", "最高", "最低", "成交量", "成交额", "时间戳"]


class AkShareError(Exception):
    """AKShare 调用错误(网络失败 / 重试耗尽 / 数据形状异常)。"""


def _ak():
    """懒加载 akshare 模块 (重量级依赖, 避免插件加载/测试强制安装)。"""
    import akshare
    return akshare


def availability() -> tuple[bool, str]:
    """loader 启动自检: akshare 已安装才注册为可切换数据源。不抛异常。"""
    try:
        _ak()
        return True, "ok"
    except ImportError:
        return False, "未安装 akshare, 运行: uv pip install akshare (或 pip install akshare)"


# ---- 稳定性: 指数退避重试 + 请求间隔 ----

def _call_with_retry(fn, *, retry_empty: bool = False, label: str = "akshare"):
    """指数退避重试: 网络错误/异常 (可选: 空 DataFrame) → 退避重试 ≤ _MAX_RETRIES 次。

    retry_empty=True 用于"已知应有数据"的接口 (日K/分钟), 空结果视为异常重试;
    财务/股本等"可能确实无数据"的接口传 False。
    重试耗尽抛 AkShareError, 由调用方按单标的软失败处理。
    """
    last: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            result = fn()
            if retry_empty and result is not None and getattr(result, "empty", False):
                raise AkShareError(f"{label} 返回空结果")
            return result
        except Exception as e:
            last = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_BASE_S * (2 ** attempt))
    raise AkShareError(f"{label} 重试 {_MAX_RETRIES} 次仍失败: {last}")


# ---- 符号转换 ----

def _to_ak_symbol(symbol: str) -> str:
    """600519.SH → sh600519; 000001.SZ → sz000001; 920000.BJ → bj920000。"""
    code, _, ex = str(symbol).partition(".")
    return ex.lower() + code


def _from_ak_symbol(ak_symbol: str) -> str:
    """sh600519 → 600519.SH; bj920000 → 920000.BJ。"""
    s = str(ak_symbol)
    if len(s) < 3:
        return s
    return f"{s[2:]}.{s[:2].upper()}"


def _exchange_of(code: str) -> str:
    """纯代码 → 交易所后缀 (6 开头沪、0/3 深、4/8/92 京)。"""
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith(("0", "3")):
        return "SZ"
    return "SH"


def _to_float(value) -> float | None:
    """健壮数值解析: 容忍逗号/百分号/空格/字符串。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None  # NaN → None
    s = str(value).strip().replace(",", "").replace("%", "").replace(" ", "")
    if not s or s in {"-", "--", "nan", "None", "null"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _as_date(value) -> _date | None:
    """date / datetime / 'YYYYMMDD' / 'YYYY-MM-DD' → date。"""
    if value is None:
        return None
    if isinstance(value, _date):
        # pandas NaT 的 NaTType 同时伪装成 date 和 int 子类 (pandas 2.x),
        # str(NaT) == "NaT" → 显式拒绝, 否则会漏进 polars 报类型错
        if str(value).strip().upper() == "NAT":
            return None
        return value
    s = str(value).strip()
    if not s or s.upper() in {"NAT", "NAN", "NONE", "NULL", "-", "--"}:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _fmt_yyyymmdd(dt) -> str | None:
    """datetime/date → YYYYMMDD (新浪日期参数)。"""
    if dt is None:
        return None
    if isinstance(dt, _date) and not isinstance(dt, datetime):
        dt = datetime.combine(dt, _time.min)
    return dt.strftime("%Y%m%d")


# ---- 日K ----

def _daily_frame(symbol: str, df) -> pl.DataFrame:
    """新浪日K df → 内部 daily 契约 (经 normalize_daily: float 化 + 停牌过滤)。"""
    if df is None or getattr(df, "empty", True):
        return pl.DataFrame()
    # date 列已是 date 对象; volume 单位股、amount 单位元 (实测), 与契约一致
    out = pl.from_pandas(df).with_columns(
        pl.lit(symbol).alias("symbol"),
        pl.col("date").cast(pl.Date),
    )
    return normalize_daily(out, source="akshare")


# ---- 除权因子 (巨潮分红送配事件 + 除权公式) ----

def _adj_factor_frame(symbol: str, events, closes: dict[_date, float]) -> pl.DataFrame:
    """分红送配事件 + 前收盘 → [symbol, trade_date, ex_factor] (每股事件 pre/post 比值)。

    巨潮事件 (stock_dividend_cninfo, 实测与 fuyao 除权日逐一吻合):
      送股/转增/派息比例均为「每 10 股」口径 → /10 得每股;
    ex_factor = C*(1+b+z)/(C-d): C=除权日前收盘(来自同源日K), d=每股现金分红,
    b=每股送股, z=每股转增 (交易所除权参考价公式的 pre/post 比, 同 fuyao 口径)。
    配股事件缺配股价字段无法精确推导 → 跳过并告警 (fail-closed)。
    """
    rows: list[dict] = []
    for rec in events:
        ex_date = _as_date(rec.get("除权日"))
        if ex_date is None:
            continue
        note = str(rec.get("实施方案分红说明") or "")
        if "配" in note and "股" in note:
            # "10配3股派X元" / "配股" 等: 缺配股价字段无法精确推导 (fail-closed)
            logger.warning("akshare 除权事件含配股(%s %s), 无法精确推导, 跳过", symbol, ex_date)
            continue
        b = (_to_float(rec.get("送股比例")) or 0.0) / 10.0
        z = (_to_float(rec.get("转增比例")) or 0.0) / 10.0
        d = (_to_float(rec.get("派息比例")) or 0.0) / 10.0
        if b <= 0 and z <= 0 and d <= 0:
            continue
        prior = max((dt for dt in closes if dt < ex_date), default=None)
        c = closes.get(prior) if prior else None
        if c is None:
            logger.warning("akshare 除权事件缺前收盘(%s %s), 跳过 (fail-closed)", symbol, ex_date)
            continue
        if c - d <= 0:
            logger.warning("akshare 除权事件前收盘异常(%s %s C=%s d=%s), 跳过", symbol, ex_date, c, d)
            continue
        rows.append({
            "symbol": symbol,
            "trade_date": ex_date,
            "ex_factor": c * (1 + b + z) / (c - d),
        })
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort("trade_date")


# ---- 分钟K ----

def _minute_frame(symbol: str, df) -> pl.DataFrame:
    """新浪分钟 df (day="2026-08-28 14:57:00" 字符串) → 内部 minute 契约。"""
    if df is None or getattr(df, "empty", True):
        return pl.DataFrame()
    out = pl.from_pandas(df).with_columns(
        pl.lit(symbol).alias("symbol"),
        pl.col("day").str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False).alias("datetime"),
    )
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in out.columns:
            out = out.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    keep = [c for c in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount") if c in out.columns]
    return out.select(keep).drop_nulls(["datetime"]) if "datetime" in keep else pl.DataFrame()


# ---- 实时快照 ----

def _spot_records(df, fetched_ms: int) -> list[dict]:
    """新浪全市场快照 df → 内部 realtime records。

    涨跌幅为百分数 (2.983 = +2.983%) → change_pct 小数制 /100 (§3.1);
    成交量单位股、成交额单位元 (实测), 直接透传。代码 sh600519 → 600519.SH。
    """
    records: list[dict] = []
    if df is None or getattr(df, "empty", True):
        return records
    for row in df.to_dict("records"):
        symbol = _from_ak_symbol(row.get("代码"))
        if not symbol or "." not in symbol:
            continue
        last = _to_float(row.get("最新价"))
        prev = _to_float(row.get("昨收"))
        pct = _to_float(row.get("涨跌幅"))
        records.append({
            "symbol": symbol,
            "name": row.get("名称"),
            "last_price": last,
            "prev_close": prev,
            "open": _to_float(row.get("今开")),
            "high": _to_float(row.get("最高")),
            "low": _to_float(row.get("最低")),
            "volume": _to_float(row.get("成交量")),
            "amount": _to_float(row.get("成交额")),
            "change_pct": pct / 100.0 if pct is not None else None,
            "change_amount": _to_float(row.get("涨跌额")),
            "amplitude": None,
            "turnover_rate": None,  # 需股本口径 (§3.4), 交给 enriched 管道
            "timestamp": fetched_ms,
            "session": None,
        })
    return records


# ---- 财务 ----

def _metrics_rows(symbol: str, df, latest_only: bool) -> list[dict]:
    """关键指标 pivot (指标 x 报告期) → 内部 metrics 行。

    无公告日 → announce_date 取法定披露截止日 (保守, 无未来函数, 用户已确认)。
    bps 由新浪「每股净资产」直接提供 (fuyao 缺失的 pb 因子得以补齐)。
    """
    if df is None or getattr(df, "empty", True):
        return []
    # 列: [选项, 指标, <报告期 "20260630"...>]; 报告期列按降序 (最新在前)
    period_cols = [c for c in df.columns if str(c).isdigit() and len(str(c)) == 8]
    period_cols.sort(reverse=True)
    if latest_only:
        period_cols = period_cols[:_FIN_LATEST_PERIODS]
    if not period_cols:
        return []
    # 指标名 → 该指标行在各报告期的值 (首个非空匹配)
    label_to_row: dict[str, dict] = {}
    for _, row in df.iterrows():
        label = str(row.get("指标") or "").strip()
        if label in _METRICS_LABEL_MAP and label not in label_to_row:
            label_to_row[label] = row
    if not label_to_row:
        return []
    rows: list[dict] = []
    for period in period_cols:
        values: dict = {}
        for label, col in _METRICS_LABEL_MAP.items():
            row = label_to_row.get(label)
            if row is None:
                continue
            values[col] = _to_float(row.get(period))
        if not any(v is not None for v in values.values()):
            continue
        period_end = _as_date(period)
        if period_end is None:
            continue
        year, q = period_end.year, (period_end.month - 1) // 3 + 1
        dl_m, dl_d = _REPORT_DEADLINE[q]
        rows.append({
            "symbol": symbol,
            "period_end": period_end,
            "announce_date": _date(year + 1 if q == 4 else year, dl_m, dl_d),
            **values,
        })
    return rows


def _statement_rows(symbol: str, df, latest_only: bool, colmap: dict) -> list[dict]:
    """新浪财务报表 (报告期 x 科目) → 内部行 (symbol/period_end/announce_date + 映射科目)。

    新浪报表带**真实公告日** (「公告日期」列, 实测与巨潮一致) → 点时至多不提前泄露。
    """
    if df is None or getattr(df, "empty", True):
        return []
    rows: list[dict] = []
    frame = df.to_dict("records")
    if latest_only:
        frame = frame[:_FIN_LATEST_PERIODS]
    for rec in frame:
        period_end = _as_date(rec.get("报告日"))
        if period_end is None:
            continue
        row: dict = {"symbol": symbol, "period_end": period_end}
        announce = _as_date(rec.get("公告日期"))
        if announce is not None:
            row["announce_date"] = announce
        for cn, en in colmap.items():
            if cn in rec:
                row[en] = _to_float(rec.get(cn))
        rows.append(row)
    return rows


def _shares_rows(symbol: str, df) -> list[dict]:
    """巨潮股本变动 → 内部 shares 行 (symbol/period_end/announce_date/float_shares)。

    period_end=变动日期 (股本归属日), announce_date=公告日期 (真实公告日, 实测
    定期报告 2026-06-30 → 公告 2026-08-15), float_shares=已流通股份 (万股 → 股)。
    契约见 share_capital.apply_historical_float_shares (公告日不晚于交易日可用)。
    """
    if df is None or getattr(df, "empty", True):
        return []
    rows: list[dict] = []
    for rec in df.to_dict("records"):
        asof = _as_date(rec.get("变动日期"))
        float_wan = _to_float(rec.get("已流通股份"))
        if asof is None or float_wan is None or float_wan <= 0:
            continue
        row: dict = {
            "symbol": symbol,
            "period_end": asof,
            "float_shares": float_wan * 10000.0,  # 万股 → 股
        }
        announce = _as_date(rec.get("公告日期"))
        if announce is not None:
            row["announce_date"] = announce
        rows.append(row)
    return rows


# ---- Provider ----

@dataclass
class _AkShareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "akshare"
    display_name: str = "AKShare"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class AkShareProvider:
    """AKShare 数据源。全部走新浪/巨潮/交易所官网 (本网络可达)。"""

    name = "akshare"
    builtin = True

    def __init__(self) -> None:
        self.config = _AkShareConfig()

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        pass

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # 新浪日K 覆盖沪深京 A 股
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """日K: 新浪 stock_zh_a_daily (不复权原始价, 前复权由 enriched 管道自算 §3.2)。

        单标的串行 + 间隔限流 + 指数退避重试; 单标的失败软跳过。
        """
        if not symbols:
            return pl.DataFrame()
        start = _fmt_yyyymmdd(start_time)
        end = _fmt_yyyymmdd(end_time) or datetime.now().strftime("%Y%m%d")
        if not start:
            # 未传窗口 (设置页试拉): 默认近 1 年
            start = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        ak = _ak()
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            for j, sym in enumerate(chunk):
                try:
                    df = _call_with_retry(
                        lambda s=sym: ak.stock_zh_a_daily(symbol=_to_ak_symbol(s), start_date=start, end_date=end, adjust=""),
                        retry_empty=True, label=f"daily {sym}",
                    )
                except AkShareError as e:
                    logger.warning("akshare 日K拉取失败(%s): %s", sym, e)
                    continue
                frame = _daily_frame(sym, df)
                if not frame.is_empty():
                    frames.append(frame)
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
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """除权因子: 巨潮分红送配事件 + 同源日K前收盘 → 每股事件 pre/post 比值。

        每标的 2 次请求: stock_dividend_cninfo (全量事件, 无日期参数, 本地按窗口过滤)
        + stock_zh_a_daily 原始价 (覆盖 [最早事件-45天, 最晚事件])。
        增量模式下回看余量保证区间起点事件的前收盘可取; 事件行由 sync_adj_factor
        按 (symbol, trade_date) 去重合并。
        """
        if not symbols:
            return pl.DataFrame()
        end = _fmt_yyyymmdd(end_time)
        start = _fmt_yyyymmdd(start_time)
        ak = _ak()
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            for j, sym in enumerate(chunk):
                try:
                    events_df = _call_with_retry(
                        lambda s=sym: ak.stock_dividend_cninfo(symbol=s.split(".")[0]),
                        label=f"adj-events {sym}",
                    )
                except AkShareError as e:
                    logger.warning("akshare 除权事件拉取失败(%s): %s", sym, e)
                    continue
                if events_df is None or getattr(events_df, "empty", True):
                    continue
                events = events_df.to_dict("records")
                # 按请求窗口过滤事件 (全量时不限)
                if start or end:
                    kept = []
                    for rec in events:
                        ex = _as_date(rec.get("除权日"))
                        if ex is None:
                            continue
                        if start and _as_date(start) and ex < _as_date(start):
                            continue
                        if end and _as_date(end) and ex > _as_date(end):
                            continue
                        kept.append(rec)
                    events = kept
                if not events:
                    continue
                # 前收盘窗口: 最早事件前 45 天起到最晚事件 (含边界前收盘)
                ex_dates = [_as_date(r.get("除权日")) for r in events if _as_date(r.get("除权日"))]
                daily_start = (min(ex_dates) - timedelta(days=45)).strftime("%Y%m%d")
                daily_end = (max(ex_dates) + timedelta(days=1)).strftime("%Y%m%d")
                try:
                    raw = _call_with_retry(
                        lambda s=sym, ds=daily_start, de=daily_end: ak.stock_zh_a_daily(
                            symbol=_to_ak_symbol(s), start_date=ds, end_date=de, adjust="",
                        ),
                        retry_empty=True, label=f"adj-daily {sym}",
                    )
                except AkShareError as e:
                    logger.warning("akshare 除权前收盘日K拉取失败(%s): %s", sym, e)
                    continue
                closes: dict[_date, float] = {}
                for _, r in raw.iterrows():
                    d = _as_date(r.get("date"))
                    c = _to_float(r.get("close"))
                    if d is not None and c is not None:
                        closes[d] = c
                frame = _adj_factor_frame(sym, events, closes)
                if not frame.is_empty():
                    frames.append(frame)
                if j < len(chunk) - 1:
                    time.sleep(_REQUEST_INTERVAL_S)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """分钟K: 新浪 stock_zh_a_minute (1/5/15/30/60 分钟)。

        新浪接口不支持日期窗口, 只返回最近 ~8 个交易日 (实测 1970 根 1 分钟);
        start/end 被忽略, 由调用方按 datetime 去重/截取。
        """
        if not symbols:
            return pl.DataFrame()
        period = "".join(ch for ch in str(freq) if ch.isdigit()) or "1"
        ak = _ak()
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            for j, sym in enumerate(chunk):
                try:
                    df = _call_with_retry(
                        lambda s=sym: ak.stock_zh_a_minute(symbol=_to_ak_symbol(s), period=period, adjust=""),
                        retry_empty=True, label=f"minute {sym}",
                    )
                except AkShareError as e:
                    logger.warning("akshare 分钟K拉取失败(%s): %s", sym, e)
                    continue
                frame = _minute_frame(sym, df)
                if not frame.is_empty():
                    frames.append(frame)
                if j < len(chunk) - 1:
                    time.sleep(_REQUEST_INTERVAL_S)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照: 新浪 stock_zh_a_spot (5550 只, 单请求)。失败软返回空列表。"""
        try:
            df = _call_with_retry(lambda: _ak().stock_zh_a_spot(), label="realtime spot")
        except AkShareError as e:
            logger.warning("akshare 实时快照拉取失败: %s", e)
            return []
        fetched_ms = int(time.time() * 1000)
        records = _spot_records(df, fetched_ms)
        logger.info("akshare 实时快照拉取完成: %d 条", len(records))
        return records

    # ---- financial ----
    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """财务数据: metrics/income/balance_sheet/cash_flow/shares。

        签名对齐 custom.GenericHTTPProvider.get_financials (financial_sync 分流调用)。
        单标的串行 + 间隔限流; 失败软跳过。akshare 财务接口单次返回全部历史,
        latest_only 在本地按报告期截取。
        """
        if not symbols:
            return pl.DataFrame()
        if table not in (*_STATEMENT_SINA_NAME, "metrics", "shares"):
            logger.warning("akshare 财务表不支持: %s", table)
            return pl.DataFrame()
        ak = _ak()
        rows: list[dict] = []
        for sym in symbols:
            try:
                if table == "metrics":
                    df = _call_with_retry(
                        lambda s=sym: ak.stock_financial_abstract(symbol=s.split(".")[0]),
                        label=f"metrics {sym}",
                    )
                    rows.extend(_metrics_rows(sym, df, latest_only))
                elif table == "shares":
                    df = _call_with_retry(
                        lambda s=sym: ak.stock_share_change_cninfo(
                            symbol=s.split(".")[0],
                            start_date="19900101",
                            end_date=datetime.now().strftime("%Y%m%d"),
                        ),
                        label=f"shares {sym}",
                    )
                    rows.extend(_shares_rows(sym, df))
                else:
                    df = _call_with_retry(
                        lambda s=sym, t=table: ak.stock_financial_report_sina(
                            stock=_to_ak_symbol(s), symbol=_STATEMENT_SINA_NAME[t],
                        ),
                        label=f"{table} {sym}",
                    )
                    rows.extend(_statement_rows(sym, df, latest_only, _STATEMENT_COLMAP[table]))
            except AkShareError as e:
                logger.warning("akshare 财务拉取失败(%s %s): %s", table, sym, e)
                continue
            time.sleep(_REQUEST_INTERVAL_S)
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame(rows)

    # ---- instruments (标的维表, 日K数据源接管时由 instrument_sync 调用) ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """沪深京 A 股代码表 → tickflow Instrument 形状行 (symbol/name/code/exchange)。

        stock_info_a_code_name 只返回 code+name, 交易所按代码前缀推导。
        """
        if asset_type != "stock":
            return []
        try:
            df = _call_with_retry(lambda: _ak().stock_info_a_code_name(), label="instruments")
        except AkShareError as e:
            logger.warning("akshare 代码表拉取失败: %s", e)
            return []
        if df is None or getattr(df, "empty", True) or "code" not in df.columns:
            return []
        out: list[dict] = []
        for rec in df.to_dict("records"):
            code = str(rec.get("code") or "")
            name = rec.get("name")
            if not code or not code.isdigit():
                continue
            out.append({
                "symbol": f"{code}.{_exchange_of(code)}",
                "name": name,
                "code": code,
                "exchange": _exchange_of(code),
                "region": "CN",
                "type": "stock",
            })
        return out

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        symbols = symbols or ["600519.SH"]
        if dataset == "realtime":
            records = self.get_realtime()
            head = records[:5]
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(records),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        if dataset == "daily":
            return _preview(self.name, "daily", self.get_daily(symbols, None, None))
        if dataset == "adj_factor":
            return _preview(self.name, "adj_factor", self.get_adj_factors(symbols, None, None))
        if dataset == "minute":
            return _preview(self.name, "minute", self.get_minute(symbols, None, None))
        if dataset == "financial":
            return _preview(self.name, "financial", self.get_financials("metrics", symbols, latest_only=True))
        return {"provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"AKShare 插件未接入 {dataset} 数据集(自动回退 TickFlow)"}


def _preview(provider: str, dataset: str, df: pl.DataFrame) -> dict:
    return {
        "provider": provider,
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": df.head(5).to_dicts() if not df.is_empty() else [],
    }
