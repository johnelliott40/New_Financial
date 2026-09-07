"""
Live market data — Yahoo Finance lookups (via yfinance).

This is a deliberate exception to the "no I/O" rule in CLAUDE.md ground rule 4: price is explicitly
required to come from a live source per PROJECT_PLAN.md's Module 1 spec, so that I/O is isolated
here, in one place, behind a small interface. modules/portfolio.py stays pure — it takes an
already-resolved price as a plain input and never imports this module.

Expense ratio and dividend rate are deliberately NOT fetched here (Module 1 amendment,
2026-08-08) — Yahoo's expense-ratio data proved unreliable for several mutual funds (see the git
history / NEXT.md for specifics), and dividend rate was never live-sourced at all. Both are now
user-entered directly when a ticker is added to the ETF universe (see ui/portfolio_tab.py), so
there's nothing here to correct or override.
"""

from __future__ import annotations

import yfinance as yf


class MarketDataError(Exception):
    """Raised when a live Yahoo Finance lookup fails (network issue, bad ticker, no price data)."""


def fetch_price(ticker: str) -> float:
    """
    Looks up a ticker's current price (USD) via Yahoo Finance.

    Raises MarketDataError if no price could be determined at all.
    """
    try:
        info = yf.Ticker(ticker).info
    except Exception as exc:
        raise MarketDataError(f"Yahoo Finance lookup failed for {ticker!r}: {exc}") from exc

    price = info.get("regularMarketPrice") or info.get("previousClose") or info.get("navPrice")
    if price is None:
        raise MarketDataError(f"No price available from Yahoo Finance for {ticker!r}")

    return float(price)
