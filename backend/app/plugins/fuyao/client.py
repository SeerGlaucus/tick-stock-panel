"""扶摇(同花顺金融数据 API) HTTP 客户端。

职责: 认证、统一信封解包、分页拉取快照、历史日K窗口切片、复权事件流。
不知道 provider / services 层。
文档: https://fuyao.aicubes.cn/docs — REST + X-api-key, 响应信封 {code, message, data}。
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"

# A 股约 5400 只, 500/页约 11 页; 50 页上限防御 count 异常导致的死循环。
_SNAPSHOT_PAGE_SIZE = 500
_SNAPSHOT_MAX_PAGES = 50
_PAGE_INTERVAL_S = 0.15  # 页间隔, 降低触发限频 (code=4001) 的概率

# 历史K端点: 单标的、interval 仅支持 1d、窗口 > 10 年返回 code=1003。
# 实测 offset 参数被忽略, 一次返回窗口内全部 K 线, 无需分页; 长窗口在此切片合并。
_HISTORICAL_MAX_RANGE_DAYS = 3640  # ≈9.96 年, 规避 10 年上限的日历/闰年歧义


class FuyaoError(Exception):
    """扶摇接口错误(配置缺失 / 网络失败 / 信封 code != 0)。

    code: 业务错误码 (如 3002=数据尚未准备, 4001=限流)。网络类失败为 None,
    供调用方区分「可继续回走的未就绪」与「应当停止的硬错误」。
    """

    def __init__(self, message: str, code: int | str | None = None) -> None:
        super().__init__(message)
        self.code = code


class FuyaoClient:
    """扶摇 REST 客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        if not api_key:
            raise FuyaoError("未配置 FUYAO_API_KEY")
        self.last_server_ts = 0  # 最近一页响应里的服务端时间戳(ms), 供行情归属
        self._http = httpx.Client(
            base_url=base_url,
            headers={"X-api-key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # ---- 内部 ----
    def _get(self, path: str, params: dict) -> dict:
        """GET + 信封解包。code != 0 时抛 FuyaoError(含 code 与 message)。"""
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise FuyaoError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            raise FuyaoError(f"HTTP {resp.status_code}: {path}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise FuyaoError(f"响应不是 JSON: {path}") from e
        code = payload.get("code")
        if code not in (0, "0", None):
            raise FuyaoError(f"扶摇接口错误 code={code}: {payload.get('message', '')} ({path})", code=code)
        return payload.get("data") or {}

    # ---- 快照 ----
    def snapshot_page(self, limit: int = _SNAPSHOT_PAGE_SIZE, offset: int = 0) -> tuple[list[dict], int]:
        """拉取一页 A 股全市场快照。返回 (rows, total), total 为全市场总数。

        实测响应(2026-08): data={timestamp, total, item}; 官方文档示例为
        data={count, data}。两者都兼容, 以实测为准。
        """
        data = self._get("/api/a-share/prices/snapshot", {"limit": limit, "offset": offset})
        try:
            self.last_server_ts = int(data.get("timestamp") or 0)
        except (TypeError, ValueError):
            self.last_server_ts = 0
        rows = data.get("item")
        if not isinstance(rows, list):
            rows = data.get("data") if isinstance(data.get("data"), list) else []
        raw_total = data.get("total")
        if raw_total is None:
            raw_total = data.get("count") or 0
        try:
            total = int(raw_total or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total

    def snapshot_all(self) -> tuple[list[dict], int]:
        """分页拉取全市场快照。返回 (rows, 服务端时间戳ms)。

        服务端时间戳用于行情归属; 缺失时返回 0, 由调用方退回本地时间。
        空数据 / 中途失败时抛 FuyaoError。
        """
        out: list[dict] = []
        server_ts = 0
        offset = 0
        for page in range(_SNAPSHOT_MAX_PAGES):
            if page > 0:
                time.sleep(_PAGE_INTERVAL_S)
            rows, total = self.snapshot_page(offset=offset)
            if not rows:
                break
            out.extend(rows)
            if not server_ts:
                server_ts = self.last_server_ts
            if total and len(out) >= total:
                break
            offset += len(rows)
        if not out:
            raise FuyaoError("全市场快照为空")
        return out, server_ts

    # ---- 历史日K ----
    def historical(
        self,
        thscode: str,
        start_ms: int,
        end_ms: int,
        adjust: str = "none",
    ) -> list[dict]:
        """单只标的日K (interval=1d, adjust=none|forward|backward)。

        窗口超过 _HISTORICAL_MAX_RANGE_DAYS 时按上限自动切片并按 date_ms 去重合并。
        返回 K 线列表: date_ms/open_price/high_price/low_price/close_price/volume/turnover。
        """
        out: list[dict] = []
        seen: set[int] = set()
        for s, e in _slice_range(start_ms, end_ms):
            data = self._get("/api/a-share/prices/historical", {
                "thscode": thscode,
                "interval": "1d",
                "start": s,
                "end": e,
                "adjust": adjust,
            })
            for bar in data.get("item") or []:
                key = bar.get("date_ms")
                if key is None or key in seen:
                    continue
                seen.add(key)
                out.append(bar)
        return out

    # ---- 复权事件流 ----
    def adjustment_factors(
        self,
        thscode: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict]:
        """单只标的复权因子事件流 (现金分红/送股)。

        返回原始事件 (ex_date_ms/dividend_per_share/per_share_bonus), 按 ex_date_ms 降序;
        因子推导在 provider 层 (事件是原始数据, 官方文档明确需调用方自行推导)。
        """
        params: dict = {"thscode": thscode}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        data = self._get("/api/a-share/corporate-actions/adjustment-factors", params)
        return data.get("item") or []

    # ---- 财务报表(单标的, limit=最近N期) ----
    def income_statements(self, thscode: str, period: str = "quarterly", limit: int = 4) -> list[dict]:
        """单只标的利润表多期序列 (按 period_end_ms 降序)。"""
        data = self._get("/api/a-share/financials/income-statements", {
            "thscode": thscode, "period": period, "limit": limit,
        })
        return data.get("item") or []

    def balance_sheets(self, thscode: str, period: str = "quarterly", limit: int = 4) -> list[dict]:
        """单只标的资产负债表多期序列。"""
        data = self._get("/api/a-share/financials/balance-sheets", {
            "thscode": thscode, "period": period, "limit": limit,
        })
        return data.get("item") or []

    def cash_flow_statements(self, thscode: str, period: str = "quarterly", limit: int = 4) -> list[dict]:
        """单只标的现金流量表多期序列。"""
        data = self._get("/api/a-share/financials/cash-flow-statements", {
            "thscode": thscode, "period": period, "limit": limit,
        })
        return data.get("item") or []

    # ---- 财务指标(单标的 + 单报告期) ----
    def financial_indicators(self, thscode: str, report: str) -> list[dict]:
        """单只标的指定报告期财务指标。

        report 格式 YYYY-[1-4] (1=一季报 2=中报 3=三季报 4=年报)。
        返回 abilities 数组: [{ability, indicators: [{index_id, value}]}], 固定 5 类。
        """
        data = self._get("/api/a-share/financials/indicators", {
            "thscode": thscode, "report": report,
        })
        return data.get("abilities") or []


def _slice_range(start_ms: int, end_ms: int):
    """把 [start_ms, end_ms] 切成 ≤ _HISTORICAL_MAX_RANGE_DAYS 的连续子窗口 (闭区间)。

    供 historical() 规避端点 10 年窗口上限; 独立成函数便于单元测试。
    """
    step_ms = _HISTORICAL_MAX_RANGE_DAYS * 24 * 3600 * 1000
    cur = start_ms
    while cur <= end_ms:
        nxt = min(cur + step_ms, end_ms)
        yield cur, nxt
        cur = nxt + 1
