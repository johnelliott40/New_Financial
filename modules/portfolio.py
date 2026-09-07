"""
Module 1 — Portfolio.

Pure functions only: no Streamlit calls, no network I/O. See CLAUDE.md ground rule 4 and
PROJECT_PLAN.md's architecture principles. Live price/expense-ratio lookups live in
modules/market_data.py; this module only ever consumes already-resolved numbers, which keeps it
fully unit-testable without mocking the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

_ASSET_CLASSES_PATH = Path(__file__).resolve().parent.parent / "data" / "asset_classes.json"

ACCOUNT_TYPES = [
    "Taxable",
    "Traditional 401(k)",
    "Traditional IRA",
    "Roth IRA",
    "Roth 401(k)",
    "HSA",
    "Other",
]

def load_asset_classes(path: Path = _ASSET_CLASSES_PATH) -> dict:
    """
    Returns {code: {"label": str, "nominal_return": float}} — the default nominal-return
    assumption per asset class from data/asset_classes.json. This is a starting point only: the
    UI lets the user edit the return for a class for their own session, and that edited value —
    not this default — is what should be passed into etf_lookup/build_universe from then on.
    """
    with open(path, "r") as f:
        raw = json.load(f)
    return {ac["code"]: {"label": ac["label"], "nominal_return": ac["default_nominal_return"]} for ac in raw["asset_classes"]}


def build_universe(asset_classes: dict, ticker_universe: dict) -> dict:
    """
    Assembles the universe shape etf_lookup expects, from two personal, session-owned inputs:

    asset_classes: {code: {"label": str, "nominal_return": float}} — normally load_asset_classes()'s
        output, with any of the user's in-session return edits already applied by the caller.
    ticker_universe: {ticker: {"asset_class": code, "expense_ratio": float, "dividend_rate": float,
        "income_type": "qualified" | "ordinary" | "interest"}} — the user's own per-ticker
        assignments and inputs from the ETF universe builder (session state / saved states, never
        data/). Expense ratio and dividend rate are entered directly by the user when a ticker is
        added, never pulled from Yahoo Finance or any other live source (Module 1 amendment,
        2026-08-08) — unlike price, which still comes from a live lookup (modules/market_data.py).
        `income_type` (renamed from `dividend_type`, MODEL_WIRING.md §4.2, 2026-08-10) gained a
        third option, `"interest"`, for cash/money-market/short-duration-bond tickers whose
        distributions are interest, not dividends — distinct from `"ordinary"` because interest and
        non-qualified dividends aren't always taxed identically (e.g. Treasury interest is
        state-tax-exempt). Not yet consumed by any calculation — captured for the future per-asset
        roll-forward (MODEL_WIRING.md §4) to route into `compute_taxes` by type.
    """
    return {
        "asset_classes": asset_classes,
        "etfs": {
            ticker: {
                "asset_class": entry["asset_class"],
                "expense_ratio": entry.get("expense_ratio") or 0.0,
                "dividend_rate": entry.get("dividend_rate") or 0.0,
                "income_type": entry.get("income_type") or "qualified",
            }
            for ticker, entry in ticker_universe.items()
        },
    }


def etf_lookup(ticker: str, universe: dict) -> dict:
    """
    Static lookup: asset class, expected nominal return, and the user-entered expense ratio and
    dividend/income info for a ticker. Expense ratio, dividend rate, and income type are per-ticker
    user inputs (see build_universe), not derived or fetched here.
    """
    etf = universe["etfs"][ticker]
    asset_class = universe["asset_classes"][etf["asset_class"]]
    return {
        "asset_class": etf["asset_class"],
        "asset_class_label": asset_class["label"],
        "nominal_return": asset_class["nominal_return"],
        "expense_ratio": etf["expense_ratio"],
        "dividend_rate": etf["dividend_rate"],
        "income_type": etf["income_type"],
    }


def real_return(nominal_return: float, inflation_rate: float) -> float:
    """Convert a nominal rate of return to a real (inflation-adjusted) rate via the Fisher equation."""
    return (1.0 + nominal_return) / (1.0 + inflation_rate) - 1.0


def expense_adjusted_return(gross_return: float, expense_ratio: float) -> float:
    """
    Deducts a fund's expense ratio from a gross return using the exact geometric formulation (the
    fee compounds against the fund's NAV continuously, not as a lump-sum annual subtraction):

        R_net = (1 + R_gross) / (1 + ER) - 1  =  (R_gross - ER) / (1 + ER)
    """
    return (1.0 + gross_return) / (1.0 + expense_ratio) - 1.0


def holding_gross_value(shares: float, price: float) -> float:
    return shares * price


def holding_cost_basis_value(shares: float, cost_basis_per_share: float) -> float:
    return shares * cost_basis_per_share


def summarize_holdings(
    holdings: Iterable[dict],
    universe: dict,
    inflation_rate: float,
) -> dict:
    """
    holdings: iterable of {"ticker", "shares", "price", "cost_basis_per_share", "account_type"}.
    Expense ratio is deliberately not a holding field — it's a per-ticker user input looked up via
    etf_lookup(universe), the same source as the asset class and nominal return, since one ticker's
    expense ratio doesn't vary by which account holds it.

    Returns gross value, value-weighted (by gross value) blended nominal/real return and expense
    ratio, and gross value by asset class.

    blended_nominal_return is the raw asset-class assumption (gross of fees) — a market benchmark
    figure. blended_real_return is net of *both* the expense ratio (via expense_adjusted_return)
    and inflation (via real_return), chained geometrically — the all-in figure an investor actually
    keeps.

    (2026-08-30 — the "Liquidation value (est.)" flat-rate liquidation estimate this function used
    to also compute — `holding_after_tax_value`/`holding_post_capital_gains_value`/`holding_
    liquidation_value_estimate`, plus the `pretax_tax_rate`/`capital_gains_rate` inputs it needed —
    was removed entirely, per user request: it was a display-only, self-contained estimate never
    consumed by `modules/projection.py`/`modules/investing.py`/`modules/tax.py`'s real bracket-based
    tax engine, and duplicated/conflicted with that real engine's own retirement-withdrawal tax
    modeling. `account_type` is kept as a holding field — still used to group `by_account`/target-
    allocation reporting elsewhere — even though this function no longer branches on it itself.)
    """
    total_gross = 0.0
    weighted_nominal = 0.0
    weighted_real = 0.0
    weighted_expense = 0.0
    by_asset_class: dict[str, float] = {}

    for h in holdings:
        gross_value = holding_gross_value(h["shares"], h["price"])
        info = etf_lookup(h["ticker"], universe)
        nominal = info["nominal_return"]
        expense_ratio = info["expense_ratio"]
        net_of_expense = expense_adjusted_return(nominal, expense_ratio)
        real = real_return(net_of_expense, inflation_rate)

        total_gross += gross_value
        weighted_nominal += gross_value * nominal
        weighted_real += gross_value * real
        weighted_expense += gross_value * expense_ratio
        by_asset_class[info["asset_class"]] = by_asset_class.get(info["asset_class"], 0.0) + gross_value

    if total_gross == 0:
        return {
            "total_gross_value": 0.0,
            "blended_nominal_return": 0.0,
            "blended_real_return": 0.0,
            "blended_expense_ratio": 0.0,
            "value_by_asset_class": {},
        }

    return {
        "total_gross_value": total_gross,
        "blended_nominal_return": weighted_nominal / total_gross,
        "blended_real_return": weighted_real / total_gross,
        "blended_expense_ratio": weighted_expense / total_gross,
        "value_by_asset_class": by_asset_class,
    }


def portfolio_value_by_account_type(lots: list[dict], prices_by_ticker: dict) -> dict[str, float]:
    """
    Sums `shares * prices_by_ticker[ticker]` per `account_type`, across `lots` (the same flat lot
    shape `modules.investing.create_lots`/`roll_forward_portfolio` produce and
    `modules.projection.project_multi_year` stores per row as `ending_lots`). Returns EVERY key in
    `ACCOUNT_TYPES`, in that order, defaulting to 0.0 for any account type not currently held — so a
    caller building a stacked-area chart across many years gets a stable, complete category set
    every year, never a KeyError on a year where (say) HSA happens to be empty.

    A lot whose ticker has no entry in `prices_by_ticker` is skipped — not silently zeroed against
    a fabricated price, and not raised, since one bad ticker shouldn't take down an entire year's
    chart (the same "skip an unpriced ticker" posture this module has used everywhere else).
    """
    totals = {account_type: 0.0 for account_type in ACCOUNT_TYPES}
    for lot in lots:
        price = prices_by_ticker.get(lot["ticker"])
        if price is None:
            continue
        totals[lot["account_type"]] = totals.get(lot["account_type"], 0.0) + lot["shares"] * price
    return totals


def target_allocation_blended_return(
    target_allocations: dict[str, dict[str, float]],
    value_by_account_type: dict[str, float],
    universe: dict,
    inflation_rate: float,
) -> dict:
    """
    2026-08-15, user request — a real, correct-behavior gap the user's own "3.95% real return but
    wealth rises at a 4% withdrawal rate" investigation surfaced: `summarize_holdings`'
    `blended_real_return` is a snapshot of TODAY's actual holdings only. It has no way to reflect
    that `rebalance_account` (modules/investing.py) continuously moves tax-advantaged accounts
    toward their OWN configured `target_allocations` (Portfolio tab's "Target allocation by account
    type" section) — a genuinely different, and possibly higher- or lower-return, mix. This answers
    a different, complementary question: "if today's dollars, split by account TYPE exactly as they
    are now, were each held at that account type's OWN target allocation instead of whatever's
    actually held, what would the blended return be" — the forward-looking figure that actually
    governs a rebalanced portfolio's long-run trajectory, shown next to (never replacing) the
    current-holdings figure.

    `value_by_account_type`: `{account_type: current_total_value}` — e.g. `summarize_holdings`'
    per-account totals grouped by account TYPE (there can be many accounts of one type; target
    allocations are configured per type, not per account — see `_render_target_allocation`'s own
    docstring). Each account type's target weights are normalized internally (don't need to sum to
    exactly 1.0, same tolerance the target-allocation editor itself allows) before being applied to
    that type's own current dollar value.

    Only account types that (a) have a nonzero current value AND (b) have at least one nonzero
    target weight configured contribute — an account type holding real money but no configured
    target has nothing to project it into, so it's excluded rather than guessed at. `covered_value`
    (how much of `total_value` the returned blend actually reflects) and `total_value` are both
    returned so a caller can flag a PARTIAL figure rather than silently presenting it as whole-
    portfolio when some account types have no target configured yet.

    Returns `{"blended_nominal_return", "blended_real_return", "blended_expense_ratio",
    "covered_value", "total_value"}` — all-zero returns/`covered_value` when nothing is covered
    (mirrors `summarize_holdings`' own empty-portfolio return shape).
    """
    total_value = sum(value_by_account_type.values())
    covered_value = 0.0
    weighted_nominal = 0.0
    weighted_real = 0.0
    weighted_expense = 0.0

    for account_type, value in value_by_account_type.items():
        if value <= 0:
            continue
        weights = target_allocations.get(account_type) or {}
        weight_total = sum(w for w in weights.values() if w > 0)
        if weight_total <= 0:
            continue
        covered_value += value
        for ticker, weight in weights.items():
            if weight <= 0 or ticker not in universe.get("etfs", {}):
                continue
            info = etf_lookup(ticker, universe)
            nominal = info["nominal_return"]
            expense_ratio = info["expense_ratio"]
            net_of_expense = expense_adjusted_return(nominal, expense_ratio)
            real = real_return(net_of_expense, inflation_rate)
            dollar_weight = value * (weight / weight_total)
            weighted_nominal += dollar_weight * nominal
            weighted_real += dollar_weight * real
            weighted_expense += dollar_weight * expense_ratio

    if covered_value <= 0:
        return {
            "blended_nominal_return": 0.0,
            "blended_real_return": 0.0,
            "blended_expense_ratio": 0.0,
            "covered_value": 0.0,
            "total_value": total_value,
        }
    return {
        "blended_nominal_return": weighted_nominal / covered_value,
        "blended_real_return": weighted_real / covered_value,
        "blended_expense_ratio": weighted_expense / covered_value,
        "covered_value": covered_value,
        "total_value": total_value,
    }


def target_allocation_blended_weights(
    target_allocations: dict[str, dict[str, float]],
    value_by_account_type: dict[str, float],
    universe: dict,
) -> dict:
    """
    2026-08-30, user request (Portfolio tab, "Target allocation by account type") — a companion to
    `target_allocation_blended_return` just above: same "if today's dollars, split by account TYPE
    exactly as they are now, were each held at that account type's OWN target allocation instead"
    question, but blending target WEIGHT (by asset class) rather than target RETURN — e.g. "62% US
    stock / 24% international / 14% bonds" for the portfolio as a whole, not a single return figure.

    Same coverage semantics as `target_allocation_blended_return`: only an account type with BOTH a
    nonzero current value AND at least one nonzero target weight configured contributes;
    `covered_value`/`total_value` let a caller flag a partial figure rather than presenting it as
    whole-portfolio when some account types have no target configured yet.

    Returns `{"weights_by_asset_class": {code: fraction}, "covered_value", "total_value"}` — the
    fractions in `weights_by_asset_class` sum to `1.0` (of `covered_value`, not `total_value`)
    whenever `covered_value > 0`; an empty dict when nothing is covered.
    """
    total_value = sum(value_by_account_type.values())
    covered_value = 0.0
    dollars_by_asset_class: dict[str, float] = {}

    for account_type, value in value_by_account_type.items():
        if value <= 0:
            continue
        weights = target_allocations.get(account_type) or {}
        weight_total = sum(w for w in weights.values() if w > 0)
        if weight_total <= 0:
            continue
        covered_value += value
        for ticker, weight in weights.items():
            if weight <= 0 or ticker not in universe.get("etfs", {}):
                continue
            asset_class = universe["etfs"][ticker]["asset_class"]
            dollar_weight = value * (weight / weight_total)
            dollars_by_asset_class[asset_class] = dollars_by_asset_class.get(asset_class, 0.0) + dollar_weight

    if covered_value <= 0:
        return {"weights_by_asset_class": {}, "covered_value": 0.0, "total_value": total_value}
    return {
        "weights_by_asset_class": {
            code: dollars / covered_value for code, dollars in dollars_by_asset_class.items()
        },
        "covered_value": covered_value,
        "total_value": total_value,
    }


def portfolio_summary(
    accounts: list[dict],
    universe: dict,
    inflation_rate: float,
) -> dict:
    """
    accounts: [{"name": str, "type": str,
                "holdings": [{"ticker","shares","price","cost_basis_per_share"}, ...]}, ...]

    Returns {"by_account": {name: summarize_holdings(...)}, "overall": summarize_holdings(...)}
    — the total-portfolio-value calculation the Module 1 plan calls for, run once per account
    (location-specific) and once pooled across the whole portfolio.
    """
    by_account = {}
    all_holdings = []
    for account in accounts:
        tagged_holdings = [{**h, "account_type": account["type"]} for h in account["holdings"]]
        by_account[account["name"]] = summarize_holdings(tagged_holdings, universe, inflation_rate)
        all_holdings.extend(tagged_holdings)

    overall = summarize_holdings(all_holdings, universe, inflation_rate)
    return {"by_account": by_account, "overall": overall}


def group_positions_by_account(accounts: list[dict], positions: list[dict]) -> list[dict]:
    """
    The widget-state -> calculation-input transformation (audit finding 4): turns a flat position
    list plus an account list into the nested shape portfolio_summary expects.

    accounts: [{"name", "type"}, ...]
    positions: [{"account", "ticker", "shares", "price", "cost_basis_per_share"}, ...]

    Returns [{"name", "type", "holdings": [{"ticker","shares","price","cost_basis_per_share"}, ...]}, ...]
    """
    holding_keys = ("ticker", "shares", "price", "cost_basis_per_share")
    return [
        {
            "name": account["name"],
            "type": account["type"],
            "holdings": [{k: p[k] for k in holding_keys} for p in positions if p["account"] == account["name"]],
        }
        for account in accounts
    ]
