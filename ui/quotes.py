"""
Shared live-price resolution and rendering helpers, used by the Portfolio tab.

This is UI-layer glue, not calculation logic: it owns the Streamlit-facing cache and the
manual-price-override widget, and hands modules/portfolio.py plain resolved numbers. Only price
comes from a live source (Yahoo Finance) — expense ratio and dividend rate are user-entered
directly in the ETF universe builder (see ui/portfolio_tab.py) and never touch this module.
"""

from __future__ import annotations

import streamlit as st

from modules import market_data
from modules.market_data import MarketDataError


@st.cache_data(ttl=900, show_spinner="Fetching live price data…")
def _cached_fetch_price(ticker: str) -> float:
    return market_data.fetch_price(ticker)


def get_price(ticker: str) -> tuple[float | None, str | None]:
    """Returns (price, None) on success or (None, error_message) on failure. Live results are cached."""
    try:
        return _cached_fetch_price(ticker), None
    except MarketDataError as exc:
        return None, str(exc)


def resolve_all_prices(tickers: list[str]) -> dict[str, dict]:
    """
    Resolves price for every ticker in the universe up front, in one deterministic pass over a
    fixed, sorted ticker list (audit finding 9 — this used to happen inside the account-render
    loop, so whichever account rendered first decided where a price-failure prompt appeared). No
    UI rendering happens here; callers render whatever prompt an `error` warrants.

    Returns {ticker: {"price": float, "error": str | None}}.
    """
    resolved: dict[str, dict] = {}
    for ticker in tickers:
        price, error = get_price(ticker)
        if error:
            price = st.session_state.get(f"manual_price_{ticker}", 0.0)
        resolved[ticker] = {"price": price, "error": error}
    return resolved


def render_price_override(ticker: str) -> float:
    """Key-only manual price number_input for `ticker` (initialize-once, no `value=`)."""
    price_key = f"manual_price_{ticker}"
    if price_key not in st.session_state:
        existing = st.session_state.manual_quotes.get(ticker, {})
        st.session_state[price_key] = float(existing.get("price") or 0.0)
    st.number_input(f"{ticker} manual price ($)", min_value=0.0, key=price_key)
    value = st.session_state[price_key]
    st.session_state.manual_quotes[ticker] = {"price": value}
    return value
