"""AkShareProvider 契约与单位标准化测试。

不依赖真实网络: 用假 akshare 模块返回样例 DataFrame, 验证字段映射、
百分数→小数制转换 (CONTRIBUTING §3.1)、除权事件比值法推导 (§3.2)、
财务科目映射与真实公告日、shares 万股→股、指数退避重试与软失败。
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd
import pytest

from app.plugins.akshare import provider as ap
from app.plugins.akshare.provider import AkShareProvider


def _patch_ak(monkeypatch, **fns):
    """monkeypatch provider._ak() 返回假模块, 各函数按名注入。

    type() 会把传入函数变成绑定方法 (调用时注入 self), 这里包一层丢弃 self。
    """
    def make(fn):
        def _method(_self, *args, **kwargs):
            return fn(*args, **kwargs)
        return _method
    fake = type("FakeAk", (), {k: make(v) for k, v in fns.items()})()
    monkeypatch.setattr(ap, "_ak", lambda: fake)
    return fake


def _daily_df(dates=("2026-08-24", "2026-08-25")):
    return pd.DataFrame({
        "date": [_dt.date.fromisoformat(d) for d in dates],
        "open": [10.0, 10.5],
        "high": [10.8, 11.0],
        "low": [9.8, 10.2],
        "close": [10.6, 10.9],
        "volume": [1000000.0, 1200000.0],
        "amount": [1.06e7, 1.308e7],
    })


def test_availability_missing_akshare(monkeypatch):
    def _raise():
        raise ImportError("No module named 'akshare'")
    monkeypatch.setattr(ap, "_ak", _raise)
    ok, reason = ap.availability()
    assert ok is False and "akshare" in reason


def test_availability_ok(monkeypatch):
    monkeypatch.setattr(ap, "_ak", lambda: type("M", (), {})())
    assert ap.availability() == (True, "ok")


# ---- 符号转换 ----

def test_symbol_conversions():
    assert ap._to_ak_symbol("600519.SH") == "sh600519"
    assert ap._to_ak_symbol("000001.SZ") == "sz000001"
    assert ap._to_ak_symbol("920000.BJ") == "bj920000"
    assert ap._from_ak_symbol("bj920000") == "920000.BJ"
    assert ap._from_ak_symbol("sh600519") == "600519.SH"
    assert ap._exchange_of("600519") == "SH"
    assert ap._exchange_of("000001") == "SZ"
    assert ap._exchange_of("300750") == "SZ"
    assert ap._exchange_of("920000") == "BJ"


# ---- daily ----

def test_daily_maps_and_units(monkeypatch):
    calls = {}

    def daily(**kw):
        calls.update(kw)
        return _daily_df()
    _patch_ak(monkeypatch, stock_zh_a_daily=daily)
    provider = AkShareProvider()
    df = provider.get_daily(["600519.SH"], None, None)
    assert df.height == 2
    r = df.to_dicts()[0]
    assert r["symbol"] == "600519.SH"
    assert r["date"] == _dt.date(2026, 8, 24)
    assert r["open"] == 10.0 and r["close"] == 10.6
    assert r["volume"] == 1000000.0 and r["amount"] == 1.06e7
    # 新浪日K 不复权原始价 (adjust="") → 前复权交给 enriched 管道自算
    assert calls["adjust"] == ""


def test_daily_soft_fail_on_persistent_error(monkeypatch):
    def boom(**kw):
        raise RuntimeError("connection reset")
    _patch_ak(monkeypatch, stock_zh_a_daily=boom)
    df = AkShareProvider().get_daily(["600519.SH"], None, None)
    assert df.is_empty()


def test_daily_retry_then_succeed(monkeypatch):
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return _daily_df()
    _patch_ak(monkeypatch, stock_zh_a_daily=flaky)
    df = AkShareProvider().get_daily(["600519.SH"], None, None)
    assert df.height == 2 and calls["n"] == 2


# ---- adj_factor (巨潮分红送配事件 + 除权公式) ----

def _adj_daily_df():
    """除权日 2026-06-26 前后日K: 前收盘 (06-25) = 10.0。"""
    return pd.DataFrame({
        "date": [_dt.date(2026, 6, 22 + i) for i in range(6)],
        "close": [10.5, 10.3, 10.1, 10.0, 6.5, 6.6],  # 06-26 除权后跳空
    })


def _div_event(**over):
    row = {"送股比例": None, "转增比例": None, "派息比例": None,
           "除权日": _dt.date(2026, 6, 26), "实施方案分红说明": "10派X元(含税)"}
    row.update(over)
    return row


def test_adj_factor_event_formula(monkeypatch):
    """派息 10派20元 (d=2.0) + 送股 10送1 (b=0.1), C=10 → ex_factor = 10*1.1/8 = 1.375。"""
    events = pd.DataFrame([_div_event(送股比例=1.0, 派息比例=20.0)])
    _patch_ak(monkeypatch,
              stock_dividend_cninfo=lambda symbol="": events,
              stock_zh_a_daily=lambda symbol=None, start_date=None, end_date=None, adjust="": _adj_daily_df())
    df = AkShareProvider().get_adj_factors(["600519.SH"], None, None)
    assert df.height == 1
    r = df.to_dicts()[0]
    assert r["symbol"] == "600519.SH"
    assert r["trade_date"] == _dt.date(2026, 6, 26)
    assert r["ex_factor"] == pytest.approx(10.0 * 1.1 / (10.0 - 2.0))


def test_adj_factor_zhuanzeng_included(monkeypatch):
    """转增 10转3 (z=0.3) + 送股 10送1 (b=0.1), C=10 → 10*1.4/10 = 1.4。"""
    events = pd.DataFrame([_div_event(送股比例=1.0, 转增比例=3.0)])
    _patch_ak(monkeypatch,
              stock_dividend_cninfo=lambda symbol="": events,
              stock_zh_a_daily=lambda symbol=None, start_date=None, end_date=None, adjust="": _adj_daily_df())
    r = AkShareProvider().get_adj_factors(["600519.SH"], None, None).to_dicts()[0]
    assert r["ex_factor"] == pytest.approx(1.4)


def test_adj_factor_no_event_returns_empty(monkeypatch):
    _patch_ak(monkeypatch,
              stock_dividend_cninfo=lambda symbol="": pd.DataFrame(),
              stock_zh_a_daily=lambda symbol=None, start_date=None, end_date=None, adjust="": _adj_daily_df())
    assert AkShareProvider().get_adj_factors(["600519.SH"], None, None).is_empty()


def test_adj_factor_allotment_skipped(monkeypatch):
    """含配股事件缺配股价 → fail-closed 跳过, 不伪造因子。"""
    events = pd.DataFrame([_div_event(派息比例=20.0, 实施方案分红说明="10配3股派X元")])
    _patch_ak(monkeypatch,
              stock_dividend_cninfo=lambda symbol="": events,
              stock_zh_a_daily=lambda symbol=None, start_date=None, end_date=None, adjust="": _adj_daily_df())
    assert AkShareProvider().get_adj_factors(["600519.SH"], None, None).is_empty()


def test_adj_factor_window_filter(monkeypatch):
    """增量窗口: 只输出窗口内事件 (start/end 过滤)。"""
    events = pd.DataFrame([
        _div_event(除权日=_dt.date(2026, 6, 26), 派息比例=20.0),
        _div_event(除权日=_dt.date(2025, 12, 19), 派息比例=20.0),
    ])
    _patch_ak(monkeypatch,
              stock_dividend_cninfo=lambda symbol="": events,
              stock_zh_a_daily=lambda symbol=None, start_date=None, end_date=None, adjust="": _adj_daily_df())
    df = AkShareProvider().get_adj_factors(
        ["600519.SH"], start_time=_dt.datetime(2026, 6, 1), end_time=_dt.datetime(2026, 6, 30),
    )
    assert df.height == 1 and df.to_dicts()[0]["trade_date"] == _dt.date(2026, 6, 26)


# ---- minute ----

def test_minute_maps_datetime(monkeypatch):
    df = pd.DataFrame({
        "day": ["2026-08-28 14:57:00", "2026-08-28 15:00:00"],
        "open": ["1297.83", "1297.40"],
        "high": ["1297.89", "1297.40"],
        "low": ["1297.10", "1297.40"],
        "close": ["1297.80", "1297.40"],
        "volume": ["21300", "28300"],
        "amount": ["27642271.4844", "36716420.0000"],
    })
    calls = {}

    def minute(symbol=None, period="1", adjust=""):
        calls["period"] = period
        return df
    _patch_ak(monkeypatch, stock_zh_a_minute=minute)
    out = AkShareProvider().get_minute(["600519.SH"], None, None, freq="1m")
    assert calls["period"] == "1"
    assert out.height == 2
    r = out.to_dicts()[0]
    assert r["symbol"] == "600519.SH"
    assert r["datetime"] == _dt.datetime(2026, 8, 28, 14, 57)
    assert r["volume"] == 21300.0 and r["amount"] == pytest.approx(27642271.4844)


def test_minute_freq_digits(monkeypatch):
    calls = {}
    _patch_ak(monkeypatch, stock_zh_a_minute=lambda symbol=None, period="1", adjust="": (calls.update(period=period) or pd.DataFrame()))
    AkShareProvider().get_minute(["600519.SH"], None, None, freq="5m")
    assert calls["period"] == "5"


# ---- realtime ----

def test_realtime_maps_percent_and_symbol(monkeypatch):
    df = pd.DataFrame([{
        "代码": "bj920000", "名称": "安徽凤凰", "最新价": 13.81, "涨跌额": 0.4,
        "涨跌幅": 2.983, "买入": 13.80, "卖出": 13.81, "昨收": 13.41,
        "今开": 13.59, "最高": 13.87, "最低": 13.44, "成交量": 813287.0,
        "成交额": 11148105.0, "时间戳": "15:30:01",
    }])
    _patch_ak(monkeypatch, stock_zh_a_spot=lambda: df)
    records = AkShareProvider().get_realtime()
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "920000.BJ"
    assert r["last_price"] == 13.81
    assert r["change_pct"] == pytest.approx(0.02983)  # 百分数 2.983 → 小数制
    assert r["change_amount"] == 0.4
    assert r["prev_close"] == 13.41
    assert r["volume"] == 813287.0 and r["amount"] == 11148105.0
    assert r["name"] == "安徽凤凰"


def test_realtime_error_returns_empty(monkeypatch):
    def boom():
        raise RuntimeError("connection reset")
    _patch_ak(monkeypatch, stock_zh_a_spot=boom)
    assert AkShareProvider().get_realtime() == []


# ---- financial ----

def _metrics_pivot():
    return pd.DataFrame({
        "选项": ["按报告期"] * 10,
        "指标": [
            "营业总收入增长率", "归属母公司净利润增长率", "毛利率", "销售净利率",
            "净资产收益率(ROE)", "资产负债率", "每股净资产", "营业总收入",
            "归母净利润", "基本每股收益",
        ],
        "20260630": [1.47, -1.95, 89.55, 50.75, 16.75, 15.19, 200.0, 92278072083.21, 82320067101.68, 65.66],
        "20260331": [6.54, 1.47, 89.76, 52.22, 10.57, 12.12, 190.0, 45000000000.0, 40000000000.0, 30.0],
        "20251231": [5.0, 4.0, 90.0, 51.0, 30.0, 18.0, 180.0, 1.7e11, 1.5e11, 60.0],
        "20250930": [7.0, 6.0, 91.0, 53.0, 20.0, 14.0, 170.0, 1.2e11, 1.0e11, 45.0],
        "20250630": [8.0, 7.0, 92.0, 54.0, 17.0, 13.0, 160.0, 1.0e11, 8.0e10, 40.0],
        "20250331": [9.0, 8.0, 93.0, 55.0, 15.0, 12.0, 150.0, 8.0e10, 6.0e10, 35.0],
    })


def test_financial_metrics_maps_factors_and_deadline(monkeypatch):
    _patch_ak(monkeypatch, stock_financial_abstract=lambda symbol="": _metrics_pivot())
    df = AkShareProvider().get_financials("metrics", ["600519.SH"], latest_only=True)
    assert df.height == 4  # latest_only → 最近 4 个报告期
    r = df.sort("period_end", descending=True).to_dicts()[0]
    r = df.to_dicts()[0]
    assert r["symbol"] == "600519.SH"
    assert r["period_end"] == _dt.date(2026, 6, 30)
    # 关键指标无公告日 → 保守法定披露日 (中报 8/31, 用户已确认方案)
    assert r["announce_date"] == _dt.date(2026, 8, 31)
    assert r["revenue_yoy"] == pytest.approx(1.47)
    assert r["net_income_yoy"] == pytest.approx(-1.95)
    assert r["gross_margin"] == pytest.approx(89.55)
    assert r["net_margin"] == pytest.approx(50.75)
    assert r["roe"] == pytest.approx(16.75)
    assert r["debt_to_asset_ratio"] == pytest.approx(15.19)
    assert r["bps"] == pytest.approx(200.0)  # fuyao 缺的 bps 由 akshare 补齐 → pb 因子可用


def test_financial_metrics_full_history(monkeypatch):
    _patch_ak(monkeypatch, stock_financial_abstract=lambda symbol="": _metrics_pivot())
    df = AkShareProvider().get_financials("metrics", ["600519.SH"], latest_only=False)
    assert df.height == 6


def test_financial_income_maps_real_announce_date(monkeypatch):
    df = pd.DataFrame([{
        "报告日": "20260630", "公告日期": "2026-08-15",
        "营业总收入": 92278072083.21, "营业收入": 90703260964.48,
        "营业成本": 9473762565.88, "净利润": 85310324833.67,
        "归属于母公司所有者的净利润": 82320067101.68, "基本每股收益": 65.66,
    }])
    _patch_ak(monkeypatch, stock_financial_report_sina=lambda stock="", symbol="": df)
    out = AkShareProvider().get_financials("income", ["600519.SH"], latest_only=True)
    r = out.to_dicts()[0]
    assert r["period_end"] == _dt.date(2026, 6, 30)
    assert r["announce_date"] == _dt.date(2026, 8, 15)  # 真实公告日 (新浪报表列)
    assert r["operating_income"] == pytest.approx(90703260964.48)
    assert r["parent_holder_net_profit"] == pytest.approx(82320067101.68)
    assert "总资产" not in r  # 未映射科目不落库


def test_financial_shares_maps_wan_to_shares_and_announce(monkeypatch):
    df = pd.DataFrame([{
        "证券代码": "600519", "变动日期": _dt.date(2026, 6, 30),
        "公告日期": _dt.date(2026, 8, 15), "已流通股份": 125008.1601, "总股本": 125008.1601,
    }])
    _patch_ak(monkeypatch, stock_share_change_cninfo=lambda symbol="", start_date="", end_date="": df)
    out = AkShareProvider().get_financials("shares", ["600519.SH"], latest_only=True)
    r = out.to_dicts()[0]
    assert r["period_end"] == _dt.date(2026, 6, 30)
    assert r["announce_date"] == _dt.date(2026, 8, 15)  # 真实公告日 (巨潮)
    assert r["float_shares"] == pytest.approx(1250081601.0)  # 万股 → 股 (乘 10000)


def test_financial_unknown_table_returns_empty(monkeypatch):
    _patch_ak(monkeypatch)
    assert AkShareProvider().get_financials("no_such", ["600519.SH"]).is_empty()


def test_financial_soft_fail(monkeypatch):
    _patch_ak(monkeypatch, stock_financial_abstract=lambda symbol="": (_ for _ in ()).throw(RuntimeError("x")))
    assert AkShareProvider().get_financials("metrics", ["600519.SH"], latest_only=True).is_empty()


# ---- instruments ----

def test_instruments_maps_codes(monkeypatch):
    df = pd.DataFrame({"code": ["000001", "600519", "920000"], "name": ["平安银行", "贵州茅台", "安徽凤凰"]})
    _patch_ak(monkeypatch, stock_info_a_code_name=lambda: df)
    rows = AkShareProvider().get_instruments("stock")
    assert len(rows) == 3
    assert rows[0] == {"symbol": "000001.SZ", "name": "平安银行", "code": "000001",
                       "exchange": "SZ", "region": "CN", "type": "stock"}
    assert rows[1]["exchange"] == "SH" and rows[2]["exchange"] == "BJ"
    assert AkShareProvider().get_instruments("etf") == []


# ---- 设置页试拉 ----

def test_test_dataset_realtime_preview(monkeypatch):
    df = pd.DataFrame([{"代码": "sh600519", "名称": "贵州茅台", "最新价": 1300.0, "涨跌额": 5.0,
                        "涨跌幅": 0.39, "买入": 1299.9, "卖出": 1300.1, "昨收": 1295.0,
                        "今开": 1296.0, "最高": 1302.0, "最低": 1293.0, "成交量": 1612611.0,
                        "成交额": 2.086e9, "时间戳": "15:00:01"}])
    _patch_ak(monkeypatch, stock_zh_a_spot=lambda: df)
    out = AkShareProvider().test_dataset("realtime")
    assert out["rows"] == 1 and out["preview"][0]["symbol"] == "600519.SH"
    assert out["preview"][0]["change_pct"] == pytest.approx(0.0039)


def test_test_dataset_unsupported_reports_fallback():
    out = AkShareProvider().test_dataset("no_such_dataset")
    assert "error" in out and "回退" in out["error"]
