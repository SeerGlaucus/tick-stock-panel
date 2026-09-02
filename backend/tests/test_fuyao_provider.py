"""FuyaoProvider 契约与单位标准化测试。

不依赖真实网络: 用假 FuyaoClient 返回样例快照页, 验证字段映射、
百分数→小数制转换 (CONTRIBUTING §3.1)、分页合并、软失败、
能力声明 (未声明数据集回退 tickflow) 与设置页试拉。
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta

import pytest

from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp
from app.plugins.fuyao.provider import FuyaoProvider


class _FakeClient:
    """按调用次数返回预置页, 记录调用供分页断言。snapshot_all 同真实客户端语义。

    同时承载 daily/adj_factor/financial 的桩:
      - historical_rows / adj_events: historical() 与 adjustment_factors() 固定返回
      - financial_rows: {table: rows} 固定返回; indicators 支持 dict {report: abilities}
      - indicator_errors: {report: Exception} 按报告期注入错误 (模拟 3002 未就绪)
    均可注入全局 error 模拟失败。
    """

    def __init__(self, pages: list[list[dict]], count: int, error: Exception | None = None,
                 server_ts: int = 0, historical_rows: list[dict] | None = None,
                 adj_events: list[dict] | None = None, financial_rows: dict | None = None,
                 indicator_errors: dict | None = None):
        self.pages = pages
        self.count = count
        self.error = error
        self.server_ts = server_ts
        self.historical_rows = historical_rows or []
        self.adj_events = adj_events or []
        self.financial_rows = financial_rows or {}
        self.indicator_errors = indicator_errors or {}
        self.calls: list[dict] = []
        self.historical_calls: list[dict] = []
        self.adj_calls: list[dict] = []
        self.financial_calls: list = []
        self.last_server_ts = server_ts

    def snapshot_page(self, limit=500, offset=0):
        self.calls.append({"limit": limit, "offset": offset})
        if self.error:
            raise self.error
        if not self.pages:
            return [], self.count
        # 按调用次数取页(provider 每轮 offset += len(rows), 页序与调用序一致)
        idx = min(len(self.calls) - 1, len(self.pages) - 1)
        return list(self.pages[idx]), self.count

    def snapshot_all(self):
        """与真实客户端同语义的分页循环 (无页间隔, 测试用)。返回 (rows, server_ts)。"""
        out: list[dict] = []
        offset = 0
        for _ in range(50):
            rows, count = self.snapshot_page(offset=offset)
            if not rows:
                break
            out.extend(rows)
            if count and len(out) >= count:
                break
            offset += len(rows)
        if not out:
            raise fc.FuyaoError("全市场快照为空")
        return out, self.server_ts

    def historical(self, thscode, start_ms, end_ms, adjust="none"):
        self.historical_calls.append({
            "thscode": thscode, "start_ms": start_ms, "end_ms": end_ms, "adjust": adjust,
        })
        if self.error:
            raise self.error
        return list(self.historical_rows)

    def adjustment_factors(self, thscode, from_date=None, to_date=None):
        self.adj_calls.append({"thscode": thscode, "from": from_date, "to": to_date})
        if self.error:
            raise self.error
        return list(self.adj_events)

    def income_statements(self, thscode, period="quarterly", limit=4):
        self.financial_calls.append(("income", thscode, period, limit))
        if self.error:
            raise self.error
        return list(self.financial_rows.get("income", []))

    def balance_sheets(self, thscode, period="quarterly", limit=4):
        self.financial_calls.append(("balance_sheet", thscode, period, limit))
        if self.error:
            raise self.error
        return list(self.financial_rows.get("balance_sheet", []))

    def cash_flow_statements(self, thscode, period="quarterly", limit=4):
        self.financial_calls.append(("cash_flow", thscode, period, limit))
        if self.error:
            raise self.error
        return list(self.financial_rows.get("cash_flow", []))

    def financial_indicators(self, thscode, report):
        self.financial_calls.append(("indicators", thscode, report))
        if self.error:
            raise self.error
        err = self.indicator_errors.get(report)
        if err:
            raise err
        rows = self.financial_rows.get("indicators")
        if isinstance(rows, dict):
            return list(rows.get(report, []))
        return list(rows or [])

    def close(self):
        pass


def _row(thscode: str = "600519.SH", **over):
    """实测快照行结构(2026-08): high_price/low_price/prev_price 命名。"""
    row = {
        "thscode": thscode,
        "last_price": 1480.0,
        "price_change": 25.0,
        "price_change_ratio_pct": 1.72,
        "open_price": 1460.0,
        "high_price": 1490.5,
        "low_price": 1455.0,
        "prev_price": 1455.0,
        "volume": 1234500,
        "turnover": 1.83e9,
    }
    row.update(over)
    return row


def _provider_with(monkeypatch, pages, count=None, error=None, **fake_kwargs):
    fake = _FakeClient(pages, count if count is not None else sum(len(p) for p in pages), error, **fake_kwargs)
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    return FuyaoProvider(), fake


# ---- 单位与字段映射 ----

def test_snapshot_units_and_field_mapping(monkeypatch):
    """核心口径: price_change_ratio_pct 百分数 → change_pct 小数制 (1.72 → 0.0172)。"""
    provider, _ = _provider_with(monkeypatch, [[_row()]])
    records = provider.get_realtime()
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "600519.SH"
    assert r["change_pct"] == pytest.approx(0.0172)
    assert r["change_amount"] == pytest.approx(25.0)
    assert r["prev_close"] == 1455.0
    assert r["open"] == 1460.0
    assert r["high"] == 1490.5
    assert r["low"] == 1455.0
    assert r["volume"] == 1234500
    assert r["amount"] == 1.83e9
    assert r["timestamp"] > 0
    # 快照不提供的字段必须为 None, 不启发式伪造
    assert r["name"] is None
    assert r["amplitude"] is None
    assert r["turnover_rate"] is None


def test_missing_pct_derives_decimal_from_change_amount(monkeypatch):
    """涨跌幅缺失时按 change_amount/prev_close 推导, 仍为小数制。"""
    provider, _ = _provider_with(monkeypatch, [[_row(price_change_ratio_pct=None)]])
    r = provider.get_realtime()[0]
    assert r["change_pct"] == pytest.approx(25.0 / 1455.0)
    assert r["change_amount"] == pytest.approx(25.0)


def test_missing_change_amount_derived_from_prices(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(price_change=None)]])
    r = provider.get_realtime()[0]
    assert r["change_amount"] == pytest.approx(1480.0 - 1455.0)


def test_row_without_thscode_dropped(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(), {"last_price": 1.0}, _row("000001.SZ")]])
    records = provider.get_realtime()
    assert [r["symbol"] for r in records] == ["600519.SH", "000001.SZ"]


def test_all_rows_unrecognized_returns_empty_with_no_fake_data(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[{"foo": "bar"}, {"baz": 1}]])
    assert provider.get_realtime() == []


# ---- 客户端信封解析 (实测结构 vs 文档示例) ----

def _patch_http(monkeypatch, payload, status_code=200):
    class _Resp:
        def json(self):
            return payload
    _Resp.status_code = status_code

    class _Http:
        def get(self, path, params=None):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: _Http())


def test_client_parses_real_world_envelope(monkeypatch):
    """实测信封(2026-08): data={timestamp, total, item}。"""
    _patch_http(monkeypatch, {
        "code": 0, "message": "success",
        "data": {"timestamp": 1787542612000, "total": 2,
                 "item": [_row(), _row("000001.SZ")]},
    })
    c = fc.FuyaoClient(api_key="k")
    rows, total = c.snapshot_page()
    assert total == 2 and len(rows) == 2
    assert c.last_server_ts == 1787542612000


def test_client_parses_documented_envelope(monkeypatch):
    """官方文档示例信封: data={count, data}。"""
    _patch_http(monkeypatch, {
        "code": 0, "message": "OK",
        "data": {"count": 3, "data": [_row()]},
    })
    c = fc.FuyaoClient(api_key="k")
    rows, total = c.snapshot_page()
    assert total == 3 and len(rows) == 1


def test_client_raises_on_error_code(monkeypatch):
    _patch_http(monkeypatch, {"code": 4001, "message": "rate limited", "data": None})
    c = fc.FuyaoClient(api_key="k")
    with pytest.raises(fc.FuyaoError, match="4001"):
        c.snapshot_page()


# ---- 字段名兼容与服务端时间戳 ----

def test_doc_style_field_names_fallback(monkeypatch):
    """文档示例字段名 (highest_price/lowest_price/prev_close_price) 也能映射。"""
    row = {
        "thscode": "600519.SH", "last_price": 1480.0, "price_change": 25.0,
        "price_change_ratio_pct": 1.72, "open_price": 1460.0,
        "highest_price": 1490.5, "lowest_price": 1455.0, "prev_close_price": 1455.0,
        "volume": 1234500, "turnover": 1.83e9,
    }
    provider, _ = _provider_with(monkeypatch, [[row]])
    r = provider.get_realtime()[0]
    assert r["high"] == 1490.5
    assert r["low"] == 1455.0
    assert r["prev_close"] == 1455.0


def test_realtime_uses_server_timestamp(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]], server_ts=1787542612000)
    assert provider.get_realtime()[0]["timestamp"] == 1787542612000


def test_realtime_falls_back_to_local_time_without_server_ts(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]], server_ts=0)
    assert provider.get_realtime()[0]["timestamp"] > 0


# ---- 分页 ----

def test_snapshot_pagination_merges_pages(monkeypatch):
    page1 = [_row(f"{600000 + i}.SH") for i in range(2)]
    page2 = [_row(f"{688000 + i}.SH") for i in range(1)]
    provider, fake = _provider_with(monkeypatch, [page1, page2], count=3)
    records = provider.get_realtime()
    assert len(records) == 3
    assert len(fake.calls) == 2
    assert fake.calls[1]["offset"] == 2


def test_snapshot_stops_when_page_empty(monkeypatch):
    provider, fake = _provider_with(monkeypatch, [[_row()], []], count=0)
    assert len(provider.get_realtime()) == 1
    assert len(fake.calls) == 2


# ---- 软失败 ----

def test_realtime_error_returns_empty_list(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[]], error=fc.FuyaoError("扶摇接口错误 code=4001: 频率超限"))
    assert provider.get_realtime() == []


def test_client_requires_api_key():
    with pytest.raises(fc.FuyaoError):
        fc.FuyaoClient(api_key="")


# ---- 能力声明与注册 ----

def test_datasets_declaration():
    """声明 realtime/daily/adj_factor/financial; 未接入的 minute 必须为 False (回退 tickflow)。"""
    config = FuyaoProvider().config
    assert "realtime" in config.datasets
    assert "daily" in config.datasets
    assert "adj_factor" in config.datasets
    assert "financial" in config.datasets
    assert "minute" not in config.datasets


# ---- API Key 解析 (secrets.json > .env, 对齐 tickflow 语义) ----

def test_get_api_key_secrets_store_takes_priority(monkeypatch):
    from app import secrets_store
    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {fp.SECRETS_FIELD: "sk-from-ui"})
    assert fp.get_api_key() == "sk-from-ui"


def test_get_api_key_falls_back_to_env(monkeypatch):
    from app import secrets_store
    monkeypatch.setenv(fp.API_KEY_ENV, "sk-from-env")
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    assert fp.get_api_key() == "sk-from-env"


def test_availability_accepts_secrets_store_key(monkeypatch):
    from app import secrets_store
    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {fp.SECRETS_FIELD: "sk-from-ui"})
    assert fp.availability() == (True, "ok")


def test_availability_requires_env_key(monkeypatch):
    from app import secrets_store
    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    ok, reason = fp.availability()
    assert ok is False and fp.API_KEY_ENV in reason


# ---- 先探后存 (probe_api_key) ----

def _patch_client_cls(monkeypatch, fake):
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))


def test_probe_api_key_ok(monkeypatch):
    _patch_client_cls(monkeypatch, _FakeClient([[_row()]], 1))
    ok, reason = fp.probe_api_key("sk-candidate")
    assert ok is True and reason == "ok"


def test_probe_api_key_invalid_key(monkeypatch):
    _patch_client_cls(monkeypatch, _FakeClient([[]], 0, error=fc.FuyaoError("扶摇接口错误 code=1001: 无效 api key")))
    ok, reason = fp.probe_api_key("sk-bad")
    assert ok is False and "无效" in reason


def test_loader_probe_plugin_key_dispatch(monkeypatch):
    import app.plugins.fuyao.provider as provider_mod
    from app.data_providers.custom import loader

    monkeypatch.setattr(provider_mod, "probe_api_key", lambda key: (True, "ok") if key == "good" else (False, "bad"))
    assert loader.probe_plugin_key("fuyao", "good") == (True, "ok")
    assert loader.probe_plugin_key("fuyao", "bad") == (False, "bad")


def test_loader_probe_plugin_key_unsupported_plugin():
    from app.data_providers.custom import loader
    # stock-sdk 未声明 api_key_env → 不支持界面配 Key
    ok, reason = loader.probe_plugin_key("stocksdk", "x")
    assert ok is False and "不支持" in reason
    ok, reason = loader.probe_plugin_key("no_such_plugin", "x")
    assert ok is False and "不存在" in reason


# ---- 保存/清除端点 (直接调用 handler, 先探后存语义) ----

def test_save_plugin_key_invalid_key_not_persisted(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    saved: dict = {}
    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda n, k: (False, "Key 无效"))
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.update(updates) or updates)
    out = settings_api.save_plugin_key(settings_api.PluginKeyIn(plugin="fuyao", api_key="bad"))
    assert out["ok"] is False and out["reason"] == "invalid"
    assert saved == {}  # 无效 Key 不落盘


def test_save_plugin_key_valid_persists_and_rescans(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    saved: dict = {}
    reloaded = []
    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda n, k: (True, "ok"))
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.update(updates) or updates)
    monkeypatch.setattr(settings_api.secrets_store, "mask", lambda key, prefix=4, suffix=4: "abcd••••wxyz")
    monkeypatch.setattr(custom_sources, "load_all", lambda: reloaded.append(1))
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [{"name": "fuyao", "available": True}])
    out = settings_api.save_plugin_key(settings_api.PluginKeyIn(plugin="fuyao", api_key="good-key"))
    assert out["ok"] is True
    assert saved == {"fuyao_api_key": "good-key"}  # 字段名与 provider.SECRETS_FIELD 一致
    assert out["plugin_available"] is True
    assert reloaded == [1]  # 保存后重扫, 插件即刻可用


def test_clear_plugin_key(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    cleared: list = []
    monkeypatch.setattr(custom_sources, "is_builtin", lambda n: n == "fuyao")
    monkeypatch.setattr(settings_api.secrets_store, "clear", lambda *keys: cleared.extend(keys))
    monkeypatch.setattr(custom_sources, "load_all", lambda: None)
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [{"name": "fuyao", "available": False}])
    out = settings_api.clear_plugin_key("fuyao")
    assert out["ok"] is True and out["plugin_available"] is False
    assert cleared == ["fuyao_api_key"]


def test_manifest_declares_realtime_dataset():
    from app.data_providers.custom import loader
    manifest = loader.plugin_manifest("fuyao")
    assert manifest is not None
    assert manifest["entry"] == "app.plugins.fuyao.provider:FuyaoProvider"
    assert "realtime" in (manifest.get("datasets") or [])
    assert manifest.get("runtime") == "none"
    assert manifest.get("api_key_env") == fp.API_KEY_ENV


def test_manifest_visible_and_declares_datasets():
    """扶摇当前为可见插件: hidden=False, 声明 realtime/daily/adj_factor/financial (未声明 minute)。"""
    from app.data_providers.custom import loader
    manifest = loader.plugin_manifest("fuyao")
    assert manifest is not None
    assert manifest.get("hidden") is False
    assert set(manifest.get("datasets") or []) == {"realtime", "daily", "adj_factor", "financial"}


def test_hidden_plugin_not_registered():
    """hidden: true 的插件不注册、不在数据源页展示 (机制测试, 用合成清单避免依赖 fuyao 当前清单)。"""
    from app.data_providers.custom import loader
    manifest = {
        "name": "synthetic_hidden",
        "entry": "app.plugins.fuyao.provider:FuyaoProvider",
        "runtime": "none",
        "hidden": True,
        "check": "app.plugins.fuyao.provider:availability",
    }
    loader._register_one_plugin(manifest)
    assert "synthetic_hidden" not in loader._PLUGIN_STATUS
    assert "synthetic_hidden" not in loader._PROVIDERS


# ---- 设置页试拉 ----

def test_test_dataset_realtime_preview(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(), _row("000001.SZ")]], count=5400)
    out = provider.test_dataset("realtime")
    assert out["provider"] == "fuyao"
    assert out["rows"] == 5400
    assert out["preview"][0]["symbol"] == "600519.SH"
    assert out["preview"][0]["change_pct"] == pytest.approx(0.0172)


def test_test_dataset_unsupported_dataset_reports_fallback(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[]])
    out = provider.test_dataset("minute")
    assert "error" in out and "回退" in out["error"]


def test_close_is_idempotent(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]])
    provider.close()
    provider.close()


# ---- 时间辅助 (显式北京时间, §3.3) ----

def test_sh_date_beijing_midnight():
    """date_ms 为 Asia/Shanghai 零点 (实测 1469980800000 = 2016-08-01), 不得按 UTC 取日。"""
    assert fp._sh_date(1469980800000) == _date(2016, 8, 1)


def test_to_ms_explicit_beijing_tz():
    """naive datetime 按 Asia/Shanghai 解释, 不依赖服务器时区。"""
    assert fp._to_ms(datetime(2016, 8, 1)) == 1469980800000
    assert fp._to_ms(_date(2016, 8, 1)) == 1469980800000
    assert fp._to_ms(None) is None


def test_slice_range_caps_at_10y():
    """窗口 > 上限时切片; 端点 10 年窗口限制由客户端自动规避。"""
    day_ms = 24 * 3600 * 1000
    start = fp._to_ms(datetime(2016, 1, 1))
    end = fp._to_ms(datetime(2026, 1, 1))
    windows = list(fc._slice_range(start, end))
    assert len(windows) == 2
    for s, e in windows:
        assert (e - s) // day_ms <= fc._HISTORICAL_MAX_RANGE_DAYS
    # 切片连续无缝隙 (cur = nxt + 1ms)
    assert windows[1][0] == windows[0][1] + 1
    # 短窗口不切片
    assert list(fc._slice_range(start, start + 30 * day_ms)) == [(start, start + 30 * day_ms)]


# ---- daily ----

def _bar(date_ms: int, close: float, **over):
    bar = {
        "date_ms": date_ms,
        "open_price": close,
        "high_price": close * 1.01,
        "low_price": close * 0.99,
        "close_price": close,
        "volume": 1000000.0,
        "turnover": close * 1000000.0,
    }
    bar.update(over)
    return bar


def test_daily_maps_fields_and_beijing_date(monkeypatch):
    """date_ms(上海零点) → date; open/high/low/close/volume/amount 直通 (单位与内部契约一致)。"""
    provider, fake = _provider_with(
        monkeypatch, [[]],
        historical_rows=[_bar(1469980800000, 100.0)],  # 2016-08-01 00:00 +08:00
    )
    df = provider.get_daily(["600519.SH"], None, None)
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["symbol"] == "600519.SH"
    assert row["date"] == _date(2016, 8, 1)
    assert row["open"] == 100.0 and row["high"] == 101.0
    assert row["low"] == 99.0 and row["close"] == 100.0
    assert row["volume"] == 1000000.0 and row["amount"] == 100000000.0
    # 原始价请求: 前复权交给 enriched 管道自算, 不落库 adjust=forward (§3.2)
    assert fake.historical_calls[0]["adjust"] == "none"
    assert fake.historical_calls[0]["thscode"] == "600519.SH"


def test_daily_batches_symbols_and_merges(monkeypatch):
    provider, fake = _provider_with(
        monkeypatch, [[]],
        historical_rows=[_bar(1469980800000, 100.0)],
    )
    df = provider.get_daily(["600519.SH", "000001.SZ"], None, None)
    assert df.height == 2
    assert set(df["symbol"].to_list()) == {"600519.SH", "000001.SZ"}
    assert len(fake.historical_calls) == 2
    assert len({c["start_ms"] for c in fake.historical_calls}) == 1  # 同一窗口


def test_daily_empty_and_error_soft_fail(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[]], historical_rows=[])
    assert provider.get_daily([], None, None).is_empty()
    provider_err, _ = _provider_with(monkeypatch, [[]], error=fc.FuyaoError("扶摇接口错误 code=4001: 频率超限"))
    assert provider_err.get_daily(["600519.SH"], None, None).is_empty()


def test_daily_default_window_is_one_year(monkeypatch):
    provider, fake = _provider_with(monkeypatch, [[]], historical_rows=[_bar(1469980800000, 100.0)])
    provider.get_daily(["600519.SH"], None, None)
    (start_ms, end_ms) = fake.historical_calls[0]["start_ms"], fake.historical_calls[0]["end_ms"]
    assert (end_ms - start_ms) == 365 * 24 * 3600 * 1000


def test_test_dataset_daily_preview(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[]], historical_rows=[_bar(1469980800000, 100.0)])
    out = provider.test_dataset("daily")
    assert out["dataset"] == "daily" and out["rows"] == 1
    assert out["columns"][0] == "symbol"


# ---- adj_factor ----

def _event(ex_date: datetime, dividend: float = 0.0, bonus: float = 0.0) -> dict:
    return {
        "ex_date_ms": fp._to_ms(ex_date),
        "dividend_per_share": dividend,
        "per_share_bonus": bonus,
    }


def _adj_provider(monkeypatch, events, bars):
    return _provider_with(monkeypatch, [[]], adj_events=events, historical_rows=bars)


def test_adj_factor_cash_dividend_uses_prior_close(monkeypatch):
    """纯现金分红: ex_factor = C/(C-d), C=除权日前一交易日收盘。"""
    ex = datetime(2026, 6, 26)
    bars = [
        _bar(fp._to_ms(datetime(2026, 6, 24)), 1713.71),
        _bar(fp._to_ms(datetime(2026, 6, 25)), 1700.0),   # 前收盘
        _bar(fp._to_ms(datetime(2026, 6, 26)), 1680.0),   # 除权日
    ]
    provider, fake = _adj_provider(monkeypatch, [_event(ex, dividend=25.911)], bars)
    df = provider.get_adj_factors(["600519.SH"], None, None)
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["trade_date"] == ex.date()
    assert row["ex_factor"] == pytest.approx(1700.0 / (1700.0 - 25.911))
    # 前收盘窗口: 最早事件前 _PRIOR_CLOSE_MARGIN_DAYS 天起
    margin = fp._PRIOR_CLOSE_MARGIN_DAYS
    assert fake.historical_calls[0]["start_ms"] == fp._to_ms(datetime(2026, 6, 26) - timedelta(days=margin))
    assert fake.historical_calls[0]["end_ms"] == fp._to_ms(datetime(2026, 6, 26))


def test_adj_factor_bonus_ratio_exact(monkeypatch):
    """纯送股 10送5 (b=0.5): ex_factor 精确等于 1.5 (test_price_limits 同口径)。"""
    ex = datetime(2026, 6, 26)
    bars = [
        _bar(fp._to_ms(datetime(2026, 6, 25)), 10.0),
        _bar(fp._to_ms(datetime(2026, 6, 26)), 6.67),
    ]
    provider, _ = _adj_provider(monkeypatch, [_event(ex, bonus=0.5)], bars)
    df = provider.get_adj_factors(["600519.SH"], None, None)
    assert df.to_dicts()[0]["ex_factor"] == pytest.approx(1.5)


def test_adj_factor_combined_dividend_and_bonus(monkeypatch):
    """现金分红 + 送股: C(1+b)/(C-d)。"""
    ex = datetime(2026, 6, 26)
    bars = [
        _bar(fp._to_ms(datetime(2026, 6, 25)), 10.0),
        _bar(fp._to_ms(datetime(2026, 6, 26)), 6.0),
    ]
    provider, _ = _adj_provider(monkeypatch, [_event(ex, dividend=2.0, bonus=0.1)], bars)
    df = provider.get_adj_factors(["600519.SH"], None, None)
    assert df.to_dicts()[0]["ex_factor"] == pytest.approx(10.0 * 1.1 / (10.0 - 2.0))


def test_adj_factor_skips_when_prior_close_missing(monkeypatch):
    """前收盘不可得 (事件早于可用日K) → fail-closed 跳过, 不伪造因子。"""
    ex = datetime(2001, 8, 27)  # 早于所有 bar
    bars = [_bar(fp._to_ms(datetime(2005, 1, 4)), 10.0)]
    provider, _ = _adj_provider(monkeypatch, [_event(ex, dividend=1.0)], bars)
    df = provider.get_adj_factors(["600519.SH"], None, None)
    assert df.is_empty()


def test_adj_factor_empty_events_and_error(monkeypatch):
    provider, _ = _adj_provider(monkeypatch, [], [])
    assert provider.get_adj_factors(["600519.SH"], None, None).is_empty()
    provider_err, _ = _provider_with(monkeypatch, [[]], error=fc.FuyaoError("code=3002 数据尚未准备"))
    assert provider_err.get_adj_factors(["600519.SH"], None, None).is_empty()


def test_adj_factor_incremental_passes_from_to(monkeypatch):
    """增量同步: start/end → 事件流 from/to 过滤; 前收盘窗口自带回看余量不受 from 影响。"""
    ex = datetime(2026, 6, 26)
    bars = [
        _bar(fp._to_ms(datetime(2026, 6, 25)), 1700.0),
        _bar(fp._to_ms(datetime(2026, 6, 26)), 1680.0),
    ]
    provider, fake = _adj_provider(monkeypatch, [_event(ex, dividend=25.911)], bars)
    provider.get_adj_factors(
        ["600519.SH"],
        start_time=datetime(2026, 6, 1),
        end_time=datetime(2026, 6, 30),
    )
    assert fake.adj_calls[0]["from"] == "2026-06-01"
    assert fake.adj_calls[0]["to"] == "2026-06-30"
    # 前收盘窗口起点 = 最早事件前 60 天, 早于 from (2026-06-01) → 能取到 2026-06-25 收盘
    assert fake.historical_calls[0]["start_ms"] < fp._to_ms(datetime(2026, 6, 1))


def test_test_dataset_adj_factor_preview(monkeypatch):
    ex = datetime(2026, 6, 26)
    bars = [
        _bar(fp._to_ms(datetime(2026, 6, 25)), 1700.0),
        _bar(fp._to_ms(datetime(2026, 6, 26)), 1680.0),
    ]
    provider, _ = _adj_provider(monkeypatch, [_event(ex, dividend=25.911)], bars)
    out = provider.test_dataset("adj_factor")
    assert out["dataset"] == "adj_factor" and out["rows"] == 1
    assert out["preview"][0]["ex_factor"] > 1.0


# ---- financial ----

def _abilities(*pairs):
    """构造 abilities 数组: [(index_id, value), ...] → 单个 growth 块。"""
    return [{"ability": "growth", "indicators": [{"index_id": i, "value": str(v)} for i, v in pairs]}]


def test_financial_income_maps_period_end_and_drops_report_date(monkeypatch):
    """period_end_ms(上海零点) → period_end; report_date_ms(数据刷新日) 剔除不落库。"""
    row = {
        "thscode": "600519.SH", "ticker": "600519", "period": "quarterly", "fiscal_year": 2025,
        "fiscal_period": "FY", "currency": "CNY",
        "period_end_ms": 1767110400000,  # 2025-12-31 00:00 +08:00 (实测)
        "report_date_ms": 1776355200000,  # 数据刷新日, 非公告日
        "operating_income": 168838102514.79, "net_profit": 85310324833.67, "basic_eps": 65.66,
    }
    provider, fake = _provider_with(monkeypatch, [[]], financial_rows={"income": [row]})
    df = provider.get_financials("income", ["600519.SH"], latest_only=True)
    assert df.height == 1
    r = df.to_dicts()[0]
    assert r["symbol"] == "600519.SH"
    assert r["period_end"] == _date(2025, 12, 31)
    assert r["operating_income"] == pytest.approx(168838102514.79)
    assert r["basic_eps"] == pytest.approx(65.66)
    assert "report_date_ms" not in r and "currency" not in r
    # latest_only=True → limit=4
    assert fake.financial_calls == [("income", "600519.SH", "quarterly", 4)]


def test_financial_statements_full_history_uses_12_periods(monkeypatch):
    b = {"thscode": "600519.SH", "period_end_ms": 1767110400000, "assets_total": 1.0, "total_debt": 0.2}
    c = {"thscode": "600519.SH", "period_end_ms": 1767110400000, "act_cash_flow_net": 5.0}
    provider, fake = _provider_with(
        monkeypatch, [[]], financial_rows={"balance_sheet": [b], "cash_flow": [c]},
    )
    dfb = provider.get_financials("balance_sheet", ["600519.SH"], latest_only=False)
    dfc = provider.get_financials("cash_flow", ["600519.SH"], latest_only=False)
    assert dfb.to_dicts()[0]["assets_total"] == pytest.approx(1.0)
    assert dfc.to_dicts()[0]["act_cash_flow_net"] == pytest.approx(5.0)
    # latest_only=False → limit=12
    assert fake.financial_calls == [
        ("balance_sheet", "600519.SH", "quarterly", 12),
        ("cash_flow", "600519.SH", "quarterly", 12),
    ]


def test_financial_metrics_maps_indicators(monkeypatch):
    """index_id → 内部列; period_end 按报告期; announce_date 取法定披露截止日; bps 恒 null。"""
    abilities = _abilities(
        ("calculate_operating_income_yoy_growth_ratio", 15.71),
        ("calculate_parent_holder_net_profit_yoy_growth_ratio", 15.38),
        ("sale_gross_margin", 91.93),
        ("sale_net_interest_ratio", 52.27),
        ("index_weighted_avg_roe", 36.02),
        ("assets_debt_ratio", 19.04),
    )
    provider, fake = _provider_with(monkeypatch, [[]], financial_rows={"indicators": abilities})
    df = provider.get_financials("metrics", ["600519.SH"], latest_only=True)
    assert df.height == 4  # latest_only → 4 期
    r = df.sort("period_end").to_dicts()[-1]
    assert r["revenue_yoy"] == pytest.approx(15.71)
    assert r["net_income_yoy"] == pytest.approx(15.38)
    assert r["gross_margin"] == pytest.approx(91.93)
    assert r["net_margin"] == pytest.approx(52.27)
    assert r["roe"] == pytest.approx(36.02)
    assert r["debt_to_asset_ratio"] == pytest.approx(19.04)
    assert r["bps"] is None  # fuyao 无 bps → pb 因子全 null, 其余因子可用
    # 报告期从最近已结束季度向前回走 (静态桩: 每期都返回同样数据)
    assert len([c for c in fake.financial_calls if c[0] == "indicators"]) == 4


def test_financial_metrics_announce_deadline():
    """保守公告日: 年报 次年4/30, 中报 8/31, 季报 4/30 与 10/31 (用户已确认方案)。"""
    abilities = _abilities(("calculate_operating_income_yoy_growth_ratio", 15.71))
    r4 = fp._metrics_row("600519.SH", "2025-4", abilities)
    assert r4["period_end"] == _date(2025, 12, 31)
    assert r4["announce_date"] == _date(2026, 4, 30)
    r1 = fp._metrics_row("600519.SH", "2026-1", abilities)
    assert r1["period_end"] == _date(2026, 3, 31) and r1["announce_date"] == _date(2026, 4, 30)
    r2 = fp._metrics_row("600519.SH", "2026-2", abilities)
    assert r2["period_end"] == _date(2026, 6, 30) and r2["announce_date"] == _date(2026, 8, 31)
    r3 = fp._metrics_row("600519.SH", "2026-3", abilities)
    assert r3["period_end"] == _date(2026, 9, 30) and r3["announce_date"] == _date(2026, 10, 31)


def test_financial_metrics_walk_skips_unready_report(monkeypatch):
    """code=3002 未就绪报告期 → 继续向前回走, 不中断整批。"""
    abilities = _abilities(("calculate_operating_income_yoy_growth_ratio", 15.71))
    latest = fp._latest_ended_report()
    year, quarter = latest
    # 最近期(3002 未就绪)之外的所有报告期都有数据
    report_data: dict[str, list] = {}
    r = (year, quarter)
    for _ in range(8):
        r = fp._prev_report(*r)
        report_data[f"{r[0]}-{r[1]}"] = abilities
    provider, fake = _provider_with(
        monkeypatch, [[]],
        financial_rows={"indicators": report_data},
        indicator_errors={f"{year}-{quarter}": fc.FuyaoError("code=3002 数据尚未准备", code=3002)},
    )
    df = provider.get_financials("metrics", ["600519.SH"], latest_only=True)
    assert df.height == 4  # 跳过未就绪期后仍取满 4 期
    calls = [c for c in fake.financial_calls if c[0] == "indicators"]
    assert calls[0][2] == f"{year}-{quarter}"  # 先尝试最近期
    assert calls[1][2] == f"{fp._prev_report(year, quarter)[0]}-{fp._prev_report(year, quarter)[1]}"  # 未就绪 → 往前一期


def test_financial_metrics_hard_error_stops_walk(monkeypatch):
    """非 3002 硬错误 → 停止回走并返回已收集数据 (fail-closed)。"""
    abilities = _abilities(("calculate_operating_income_yoy_growth_ratio", 15.71))
    latest = fp._latest_ended_report()
    provider, fake = _provider_with(
        monkeypatch, [[]],
        financial_rows={"indicators": abilities},
        indicator_errors={f"{latest[0]}-{latest[1]}": fc.FuyaoError("code=4001 限流", code=4001)},
    )
    df = provider.get_financials("metrics", ["600519.SH"], latest_only=True)
    assert df.is_empty()
    # 4001 为可重试错误 → 指数退避重试 3 次后停止 (fail-closed)
    assert len([c for c in fake.financial_calls if c[0] == "indicators"]) == 3


def test_financial_shares_unsupported_and_unknown_table(monkeypatch):
    """shares 表扶摇不提供 → 空表 (下游回退最新维表股本); 未知表同样空表。"""
    provider, fake = _provider_with(monkeypatch, [[]])
    assert provider.get_financials("shares", ["600519.SH"]).is_empty()
    assert provider.get_financials("no_such_table", ["600519.SH"]).is_empty()
    assert fake.financial_calls == []


def test_financial_error_soft_fail(monkeypatch):
    """整批软失败: 返回空表, 不抛异常 (financial_sync 按空数据处理)。"""
    provider, _ = _provider_with(monkeypatch, [[]], error=fc.FuyaoError("code=4001 限流", code=4001))
    assert provider.get_financials("income", ["600519.SH"]).is_empty()
    assert provider.get_financials("metrics", ["600519.SH"]).is_empty()


def test_test_dataset_financial_preview(monkeypatch):
    abilities = _abilities(("calculate_operating_income_yoy_growth_ratio", 15.71))
    provider, _ = _provider_with(monkeypatch, [[]], financial_rows={"indicators": abilities})
    out = provider.test_dataset("financial")
    assert out["dataset"] == "financial" and out["rows"] == 4
    assert "revenue_yoy" in out["columns"]
    assert "announce_date" in out["columns"]
