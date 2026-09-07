"""
Tests for modules/market_data.py. yfinance is mocked throughout — these tests never touch the
network, so they stay fast and deterministic regardless of Yahoo Finance's availability.

Only price is fetched here (Module 1 amendment, 2026-08-08) — expense ratio and dividend rate are
user-entered directly in the ETF universe builder now, never pulled from Yahoo Finance.
"""

import pytest

import modules.market_data as market_data
from modules.market_data import MarketDataError, fetch_price


class _FakeTicker:
    def __init__(self, info):
        self.info = info


def _patch_ticker(monkeypatch, info=None, raise_exc=None):
    def fake_ticker(ticker):
        if raise_exc is not None:
            raise raise_exc
        return _FakeTicker(info)

    monkeypatch.setattr(market_data.yf, "Ticker", fake_ticker)


def test_fetch_price_uses_regular_market_price(monkeypatch):
    _patch_ticker(monkeypatch, info={"regularMarketPrice": 100.0})
    assert fetch_price("FXAIX") == 100.0


def test_fetch_price_falls_back_to_previous_close(monkeypatch):
    _patch_ticker(monkeypatch, info={"regularMarketPrice": None, "previousClose": 42.5})
    assert fetch_price("XYZ") == 42.5


def test_fetch_price_falls_back_to_nav_price(monkeypatch):
    _patch_ticker(monkeypatch, info={"regularMarketPrice": None, "previousClose": None, "navPrice": 12.34})
    assert fetch_price("XYZ") == 12.34


def test_fetch_price_raises_when_no_price_available(monkeypatch):
    _patch_ticker(monkeypatch, info={"regularMarketPrice": None, "previousClose": None, "navPrice": None})
    with pytest.raises(MarketDataError):
        fetch_price("BADTICKER")


def test_fetch_price_raises_on_network_error(monkeypatch):
    _patch_ticker(monkeypatch, raise_exc=ConnectionError("no network"))
    with pytest.raises(MarketDataError):
        fetch_price("VTI")
