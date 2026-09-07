"""
Module D, Steps 3-6 (MODEL_WIRING.md §2.3, §4-§7) — tax-lot ledger construction from resolved
contribution dollars (`create_lots`), the per-asset roll-forward (each holding growing at its OWN
return, producing its OWN dividend/interest income, reinvested — §4), Stage-1 sales (dissaving + a
fixed, configurable draw order — §5), rebalancing within tax-advantaged accounts (§7), and the
retirement-withdrawal strategy dispatch (§2.3).

**Step 2 (deciding HOW MUCH each destination gets) moved out of this module 2026-08-16**
(CONTRIBUTION_TOGGLE_REDESIGN.md) — the priority-order waterfall that used to live here
(`contribution_waterfall`, `allocate_401k_family_remaining`, `DEFAULT_CONTRIBUTION_PRIORITY`,
`DESTINATION_ACCOUNT_TYPE`) is gone, replaced by `modules/contributions.py`'s per-destination mode
system (max/custom, checked against real IRS limits every year, no priority-order cash competition
for 401(k) at all — see that module's own docstring for why). `create_lots` below is unchanged —
it still just converts a caller-assembled `{account_type: dollars}` dict into tax lots by target
allocation; it never cared how those dollars were decided.

Pure functions only, per CLAUDE.md ground rule 4: no Streamlit, no I/O, no live prices fetched
here — every price/limit/capacity is an already-resolved caller input. This is the module's own
explicit scope boundary, per MODEL_WIRING.md §3.1: "This module consumes their resolved, clamped
output; it does not re-derive limits." `modules/projection.py` is the caller that assembles the
prices and (now via `modules/contributions.py`) the resolved dollar amounts before calling into
this module.

Known, documented gaps in this pass (flagged, not silently guessed — CLAUDE.md ground rule 7):

1. **HSA contribution capacity is not computed anywhere in this model.** No IRS HSA self-only/
   family limit data exists in `data/tax_brackets.json`, and there is no health-coverage-type input
   on the Demographics tab to even select self-only vs. family. `create_lots` below would happily
   convert a nonzero `"HSA"` dollar figure into lots if one ever showed up in
   `dollars_by_account_type`, but nothing upstream ever produces one — a natural, small future
   addition, not attempted here since it wasn't asked for.
2. **Future-year per-ticker prices — CLOSED by Step 3's `roll_forward_holding`/
   `roll_forward_portfolio`** (2026-08-10): each ticker's price now genuinely evolves year over
   year (`price_next = price × (1 + real_price_return)`, §4.1), rather than holding flat. A real
   remaining simplification, documented in `roll_forward_portfolio`'s own docstring: a NEW lot
   purchased THIS year (from this year's resolved contributions — see `modules/contributions.py`)
   doesn't itself generate distribution income until NEXT year — only lots already held at the
   START of the year do.

3. **Withdrawal-phase dividend handling — partially CLOSED by Step 6.** §4.3's full rule
   ("dividends spent first, only the unexpended remainder reinvested") needs a real withdrawal-NEED
   figure. Step 4 supplied one for DISSAVING (pre-retirement, a negative-profit year); Step 6 adds
   the formal one (`annual_withdrawal_target`, below) — Taxable dividends/interest are now
   explicitly netted against that target before anything is sold (`modules/projection.py`'s own
   docstring), which IS "spent first" in substance. What's still a documented approximation:
   taxable-account dividends still stop reinvesting entirely once `withdrawing_fraction > 0`, full
   stop, rather than reinvesting only the specific unspent remainder past the withdrawal target
   (the two amounts are close but not always identical when `funding_gap != 0`); tax-advantaged
   account distributions (Traditional/Roth 401(k)/IRA, HSA) still always reinvest regardless of
   phase — the withdrawal SALE can still draw down a tax-advantaged account directly via
   `draw_order_fill`, but that account's own dividends don't get "netted" against the withdrawal
   target the way Taxable's do (§4.3 itself never singles out tax-advantaged dividends for
   different treatment, so this remains the pre-existing behavior).

4. **Step 4 (§5) builds sale MECHANICS — `sell_lots`/`draw_order_fill` — CLOSED for both DISSAVING
   and formal retirement withdrawals as of Step 6.** §5.1 point 1 (dissaving, pre-retirement) was
   wired in Step 4; §5.1 point 2 (formal withdrawals, driven by §2.3's `annual_withdrawal_target`)
   is wired in Step 6, mutually exclusive with dissaving (a year is EITHER pre-retirement-and-
   dissaving-eligible OR in the withdrawal phase, never both — see `modules/projection.py`'s own
   docstring). What Step 6 does NOT do: §5.3 Stage 2's bracket-aware fill and RMDs (Module G2,
   still specification only) — the withdrawal AMOUNT here is purely the flat-percentage strategy's
   own output, deliberately NOT reconciled against the year's actual spending need (§2.3's own
   explicit instruction: report `funding_gap`, don't silently paper over it).

5. **`average_cost` sale method's holding-period classification is a documented simplification**,
   not literal IRS Form 8606/average-cost-method mechanics: real average-cost accounting still
   tracks each underlying lot's own acquisition date for long/short-term classification even while
   blending BASIS across lots of the same ticker. This implementation does exactly that (each
   ticker's lots are sold oldest-first, in that ticker's own original acquisition-date order, using
   the ticker's blended average basis only for the GAIN calculation) — so this gap is smaller than
   it might sound; the one place it simplifies is that HIFO's own gain-minimizing lot SELECTION
   logic doesn't apply within `average_cost` (there is no "which lot" choice once basis is blended),
   which is the whole point of choosing that method in the first place.

6. **Step 5's `rebalance_account` collapses §7's two-step "direct contributions to underweight
   assets first, THEN sell/buy the remainder" into a single sell/buy-to-exact-target pass.** §7
   itself states rebalancing trades inside a tax-advantaged account "net to zero cash and produce
   zero tax consequence" — with no transaction cost or tax cost modeled for a trade inside these
   accounts (the whole reason §7 restricts rebalancing to them at all), "direct new money to the
   most-underweight assets first, then trade the remainder" and "just trade everything to the exact
   target in one pass" produce IDENTICAL final holdings; the two-step framing only matters when
   trades have a real-world cost, which none does here. `rebalance_account` therefore expects
   `create_lots`' regular target-weighted contribution purchase to have ALREADY happened for that
   account (nothing here replaces it) and simply corrects the resulting FULL post-contribution lot
   set to the exact target in one step, exactly reproducing what the literal two-step process would
   also converge to.
"""

from __future__ import annotations

from modules.portfolio import etf_lookup, expense_adjusted_return, real_return

# The three income_type values this model recognizes (MODEL_WIRING.md §4.2) — anything else on a
# ticker (shouldn't happen; the UI's own dropdown only offers these three) falls back to
# "qualified" rather than silently dropping the income, same defensive fallback
# modules.portfolio.build_universe already uses for a missing income_type entirely.
_INCOME_TYPES = ("qualified", "ordinary", "interest")


def create_lots(
    dollars_by_account_type: dict[str, float],
    target_allocations: dict[str, dict[str, float]],
    prices_by_ticker: dict[str, float],
    year: int,
) -> list[dict]:
    """
    MODEL_WIRING.md §3.2 point 4 / §3.3: within each account TYPE that received investable dollars
    this year, splits by that account type's target ticker weights (normalized, so weights don't
    strictly need to sum to exactly 1.0 — a caller-side warning, not a hard requirement here) and
    creates one tax lot per ticker purchased. Fractional shares are allowed (§3.3 — "the model is
    not simulating a broker's lot minimums").

    An account type with dollars but no configured target allocation (empty or all-zero weights)
    contributes nothing here — its dollars are simply not converted into lots this year, since there
    is nowhere to put them; this is a caller-visible gap (the dollars still show up in
    `dollars_by_account_type`, just produce no lots), not a silent loss of the underlying money.
    Same for a ticker with no known price (`prices_by_ticker.get(ticker)` missing or <= 0) — its
    slice of that account's dollars produces no lot rather than dividing by zero or guessing a
    price.

    Returns a flat list of `{"ticker", "account_type", "shares", "basis_per_share", "year_acquired"}`
    dicts — one per (account type, ticker) pair actually purchased, never blended across years or
    accounts (§3.3: "cost basis is tracked per lot, not blended").
    """
    lots = []
    for account_type, dollars in dollars_by_account_type.items():
        if dollars <= 0:
            continue
        weights = target_allocations.get(account_type, {})
        total_weight = sum(max(0.0, w) for w in weights.values())
        if total_weight <= 0:
            continue
        for ticker, weight in weights.items():
            if weight <= 0:
                continue
            ticker_dollars = dollars * (weight / total_weight)
            price = prices_by_ticker.get(ticker)
            if not price or price <= 0:
                continue
            lots.append(
                {
                    "ticker": ticker,
                    "account_type": account_type,
                    "shares": ticker_dollars / price,
                    "basis_per_share": price,
                    "year_acquired": year,
                }
            )
    return lots


def roll_forward_holding(
    price: float, nominal_return: float, expense_ratio: float, inflation_rate: float, dividend_rate: float
) -> dict:
    """
    MODEL_WIRING.md §4.1 — ONE ticker, ONE year. **Hard requirement, not a suggestion**: this uses
    the ticker's OWN `nominal_return`/`expense_ratio`/`dividend_rate` — never
    `modules.portfolio.summarize_holdings`'s `blended_real_return`/`blended_expense_ratio`, which
    are reporting-only figures for a single point in time and must never appear in a roll-forward
    (compounding a blended average is not the same as compounding each asset and then averaging the
    results — see that function's own docstring).

    `real_price_return = (1 + real_total_return) / (1 + dividend_rate) - 1` — the SAME exact
    geometric decomposition `expense_adjusted_return` uses two lines above, applied here for the
    identical reason: a rate compounds against the current value continuously, not as a lump-sum
    annual subtraction. Splitting `real_total_return` into price return and dividend yield by
    subtraction (`real_total_return - dividend_rate`) looks equivalent but isn't, because the split
    and the reinvestment recombination don't invert each other — reinvested dividend cash buys new
    shares at the start-of-year price (§3.4) which then ALSO earn that same year's own price return,
    so `(1 + dividend_rate) × (1 + real_total_return - dividend_rate)` works out to `1 +
    real_total_return + dividend_rate × real_price_return`, a small extra cross term that compounds
    for decades and silently delivers more than the configured nominal return. The geometric split
    used here eliminates that term by construction: `(1 + dividend_rate) × (1 + real_price_return) =
    (1 + dividend_rate) × [(1 + real_total_return) / (1 + dividend_rate)] = 1 + real_total_return`,
    exactly, for any dividend yield — see `tests/test_investing.py`'s dedicated closed-form test for
    the proof this function doesn't double-count.

    A money-market/cash ticker (e.g. SPAXX, SGOV) is the limiting case: its entire nominal return
    IS its interest rate, so `real_price_return` comes out ~0% nominal and NEGATIVE in real terms
    (real_return of ~0% is negative once inflation is subtracted) — correct, not a bug; the model
    should show a cash position quietly losing real value every year, not smooth it away.

    Returns `{"distribution_income_per_share": float, "new_price": float}` — the caller multiplies
    by a specific lot's `shares` to get that lot's own distribution income (kept per-share here so
    this function has no opinion on shares/lots at all, staying a pure single-ticker calculation).
    """
    net_of_expense = expense_adjusted_return(nominal_return, expense_ratio)
    real_total_return = real_return(net_of_expense, inflation_rate)
    real_price_return = (1.0 + real_total_return) / (1.0 + dividend_rate) - 1.0
    return {
        "distribution_income_per_share": price * dividend_rate,
        "new_price": price * (1.0 + real_price_return),
    }


def roll_forward_portfolio(
    lots: list[dict],
    universe: dict,
    prices_by_ticker: dict[str, float],
    inflation_rate: float,
    withdrawing_fraction: float,
) -> dict:
    """
    MODEL_WIRING.md §4.1-§4.3 — ONE year's roll-forward for a whole lot ledger. Each ticker's price
    evolves independently via `roll_forward_holding` (computed once per ticker, not once per lot —
    price is a ticker-level fact; multiple lots of the same ticker, even across different accounts,
    share one price); each LOT's own distribution income is `lot_shares × price_per_share_income`,
    using the price at the START of this year (before this year's own growth is applied) and that
    lot's own shares — never a blended rate, never mixed across tickers.

    Reinvestment (§4.3): taxable-account distributions reinvest back into the SAME holding while
    `withdrawing_fraction == 0`; once `withdrawing_fraction > 0`, taxable distributions stop
    reinvesting (a documented simplification of §4.3's literal "spent first, remainder reinvested"
    rule — see the module docstring's gap #3 for why: there's no funded withdrawal-need figure yet
    to determine a "remainder"). Tax-advantaged accounts (Traditional/Roth 401(k)/IRA, HSA) always
    reinvest regardless of phase, for the same reason — no funded withdrawal mechanism for them
    exists yet either (§5, not built). All reinvestment happens at the SAME start-of-year price used
    to compute the distribution amount, consistent with §3.4's beginning-of-year purchase
    convention.

    A ticker present in `lots` but missing from `universe` or `prices_by_ticker` (or priced at
    `<= 0`) is skipped for BOTH price roll-forward and income — its price simply carries through
    unchanged rather than guessing, and it contributes no distribution income this year.

    Returns `{
        "updated_prices_by_ticker": {ticker: float},
        "distribution_by_account_and_type": {account_type: {"qualified": float, "ordinary": float, "interest": float}},
        "new_lots": [ {ticker, account_type, shares, basis_per_share} ... ],  # reinvestment lots; caller fills in year_acquired
    }`.
    """
    tickers_held = sorted({lot["ticker"] for lot in lots})
    updated_prices = dict(prices_by_ticker)
    income_per_share_by_ticker: dict[str, float] = {}

    for ticker in tickers_held:
        price = prices_by_ticker.get(ticker)
        if not price or price <= 0 or ticker not in universe.get("etfs", {}):
            continue
        info = etf_lookup(ticker, universe)
        rolled = roll_forward_holding(
            price=price,
            nominal_return=info["nominal_return"],
            expense_ratio=info["expense_ratio"],
            inflation_rate=inflation_rate,
            dividend_rate=info["dividend_rate"],
        )
        updated_prices[ticker] = rolled["new_price"]
        income_per_share_by_ticker[ticker] = rolled["distribution_income_per_share"]

    distribution_by_account_and_type: dict[str, dict[str, float]] = {}
    reinvest_dollars_by_account_ticker: dict[tuple[str, str], float] = {}

    for lot in lots:
        ticker = lot["ticker"]
        account_type = lot["account_type"]
        income_per_share = income_per_share_by_ticker.get(ticker)
        if not income_per_share:
            continue
        distribution_income = lot["shares"] * income_per_share
        if distribution_income <= 0:
            continue

        info = etf_lookup(ticker, universe)
        income_type = info["income_type"] if info["income_type"] in _INCOME_TYPES else "qualified"
        bucket = distribution_by_account_and_type.setdefault(
            account_type, {"qualified": 0.0, "ordinary": 0.0, "interest": 0.0}
        )
        bucket[income_type] += distribution_income

        reinvests = account_type != "Taxable" or withdrawing_fraction <= 0
        if reinvests:
            key = (account_type, ticker)
            reinvest_dollars_by_account_ticker[key] = (
                reinvest_dollars_by_account_ticker.get(key, 0.0) + distribution_income
            )

    new_lots = [
        {
            "ticker": ticker,
            "account_type": account_type,
            "shares": dollars / prices_by_ticker[ticker],
            "basis_per_share": prices_by_ticker[ticker],
        }
        for (account_type, ticker), dollars in reinvest_dollars_by_account_ticker.items()
    ]

    return {
        "updated_prices_by_ticker": updated_prices,
        "distribution_by_account_and_type": distribution_by_account_and_type,
        "new_lots": new_lots,
    }


# Default draw order for Stage-1 fixed-order withdrawals/dissaving (MODEL_WIRING.md §5.3 Stage 1):
# "Default Taxable → Traditional → Roth, with HSA excluded unless the user opts in." Expanded here
# to this model's actual account-type taxonomy (modules.portfolio.ACCOUNT_TYPES) — 401(k) drawn
# before its matching IRA within each bucket, an arbitrary but consistent tie-break (the spec states
# no preference). "HSA" and "Other" are both omitted — HSA per the spec's own explicit opt-in
# requirement, "Other" because it has no defined tax treatment for a sale at all. User-editable, per
# the spec ("Default order, user-editable") — a caller passes its own `draw_order` list to override.
DEFAULT_DRAW_ORDER = [
    "Taxable",
    "Traditional 401(k)",
    "Traditional IRA",
    "Roth 401(k)",
    "Roth IRA",
]

_SALE_METHODS = ("hifo", "fifo", "average_cost")


def _average_cost_ordered_lots(lots: list[dict], prices_by_ticker: dict[str, float]) -> list[dict]:
    """
    MODEL_WIRING.md §3.3's "average cost" alternative sale method: each ticker's own lots are sold
    oldest-first (their own real acquisition order, so holding-period/long-vs-short classification
    stays meaningful per lot), but every lot's `basis_per_share` is replaced with that TICKER's
    single blended weighted-average basis for gain-calculation purposes — see the module docstring's
    gap #5 for exactly how this differs from literal IRS average-cost mechanics (a documented,
    smaller-than-it-sounds simplification).
    """
    lots_by_ticker: dict[str, list[dict]] = {}
    for lot in lots:
        lots_by_ticker.setdefault(lot["ticker"], []).append(lot)

    ordered: list[dict] = []
    for ticker, ticker_lots in lots_by_ticker.items():
        total_shares = sum(lot["shares"] for lot in ticker_lots)
        avg_basis = (
            sum(lot["shares"] * lot["basis_per_share"] for lot in ticker_lots) / total_shares
            if total_shares > 0
            else 0.0
        )
        for lot in sorted(ticker_lots, key=lambda l: l["year_acquired"]):
            ordered.append({**lot, "basis_per_share": avg_basis})
    return ordered


def sell_lots(
    lots: list[dict],
    dollars_needed: float,
    prices_by_ticker: dict[str, float],
    current_year: int,
    sale_method: str = "hifo",
) -> dict:
    """
    MODEL_WIRING.md §5.2 — sells from `lots` (already filtered to ONE account by the caller; see
    `draw_order_fill` below, which handles the cross-account draw order) to raise `dollars_needed`
    of cash. Fractional-share (partial-lot) sales are allowed, same convention as purchases (§3.3).

    `sale_method` (§3.3 — "a parameter of the sale function, not a global"):
    - `"hifo"` (default — "specific identification, highest-basis-first," which minimizes realized
      gain): ranks EVERY lot in the account — across all tickers, not just within one — by ascending
      per-share unrealized gain (`price[ticker] - basis_per_share`), selling the smallest-gain lot
      first. This is the literal generalization of "highest basis first" to a multi-ticker account:
      the lot with the highest basis relative to its OWN ticker's price is exactly the lot with the
      smallest unrealized gain per share, and selling those first minimizes total realized gain for
      whatever amount of cash must be raised.
    - `"fifo"`: ranks by `year_acquired` ascending (oldest lots sold first), across all tickers.
    - `"average_cost"`: see `_average_cost_ordered_lots`.

    A lot whose ticker has no known price (missing or `<= 0` in `prices_by_ticker`) is never sold —
    skipped entirely, carried through unchanged in `remaining_lots` — same defensive convention as
    `roll_forward_portfolio`.

    Returns `{
        "lots_sold": [{"ticker", "account_type", "shares_sold", "proceeds", "basis",
                       "realized_gain", "holding_period_years", "is_long_term"}, ...],
        "remaining_lots": [...],   # every lot not fully sold, including untouched/unsellable ones
        "total_proceeds": float,
        "short_term_gain": float,  # sum of realized_gain across lots held < 1 year
        "long_term_gain": float,   # sum of realized_gain across lots held >= 1 year
        "shortfall": float,        # > 0 if this account ran out of sellable value first (§6:
                                    # reported, never silently dropped)
    }`.
    """
    if dollars_needed < 0:
        raise ValueError("dollars_needed must be >= 0 — a negative sale isn't a sale")
    if sale_method not in _SALE_METHODS:
        raise ValueError(f"sale_method must be one of {_SALE_METHODS}, got {sale_method!r}")

    sellable, unsellable = [], []
    for lot in lots:
        price = prices_by_ticker.get(lot["ticker"])
        (sellable if price and price > 0 else unsellable).append(lot)

    if dollars_needed == 0 or not sellable:
        return {
            "lots_sold": [],
            "remaining_lots": list(lots),
            "total_proceeds": 0.0,
            "short_term_gain": 0.0,
            "long_term_gain": 0.0,
            "shortfall": dollars_needed,
        }

    if sale_method == "average_cost":
        ordered = _average_cost_ordered_lots(sellable, prices_by_ticker)
    elif sale_method == "fifo":
        ordered = sorted(sellable, key=lambda lot: lot["year_acquired"])
    else:  # "hifo"
        ordered = sorted(sellable, key=lambda lot: prices_by_ticker[lot["ticker"]] - lot["basis_per_share"])

    remaining_needed = dollars_needed
    lots_sold: list[dict] = []
    remaining_lots: list[dict] = []
    for lot in ordered:
        if remaining_needed <= 0:
            remaining_lots.append(lot)
            continue
        price = prices_by_ticker[lot["ticker"]]
        lot_value = lot["shares"] * price
        sell_value = min(remaining_needed, lot_value)
        shares_sold = sell_value / price
        proceeds = shares_sold * price
        basis = shares_sold * lot["basis_per_share"]
        realized_gain = proceeds - basis
        holding_period_years = current_year - lot["year_acquired"]
        is_long_term = holding_period_years >= 1
        lots_sold.append(
            {
                "ticker": lot["ticker"],
                "account_type": lot["account_type"],
                "shares_sold": shares_sold,
                "proceeds": proceeds,
                "basis": basis,
                "realized_gain": realized_gain,
                "holding_period_years": holding_period_years,
                "is_long_term": is_long_term,
            }
        )
        remaining_needed -= proceeds
        leftover_shares = lot["shares"] - shares_sold
        if leftover_shares > 1e-9:
            remaining_lots.append({**lot, "shares": leftover_shares})

    total_proceeds = sum(s["proceeds"] for s in lots_sold)
    return {
        "lots_sold": lots_sold,
        "remaining_lots": remaining_lots + unsellable,
        "total_proceeds": total_proceeds,
        "short_term_gain": sum(s["realized_gain"] for s in lots_sold if not s["is_long_term"]),
        "long_term_gain": sum(s["realized_gain"] for s in lots_sold if s["is_long_term"]),
        "shortfall": max(0.0, dollars_needed - total_proceeds),
    }


def draw_order_fill(
    dollars_needed: float,
    lots: list[dict],
    prices_by_ticker: dict[str, float],
    current_year: int,
    draw_order: list[str] | None = None,
    sale_method: str = "hifo",
) -> dict:
    """
    MODEL_WIRING.md §5.3 Stage 1 — raises `dollars_needed` of cash by selling from `lots` (the
    WHOLE ledger, across every account type) in `draw_order` sequence (default `DEFAULT_DRAW_ORDER`
    above): each account type in order is sold via `sell_lots` (capped at whatever's still needed
    after every earlier account type in the order) until the need is met or the order is exhausted.
    An account type absent from `draw_order` (e.g. HSA, unless the caller opts it in) is never
    touched, at all — its lots pass straight through to `remaining_lots` untouched.

    Returns `{
        "sales_by_account_type": {account_type: <sell_lots' own return dict>, ...},  # only for
                                                                                       # account types
                                                                                       # actually sold
        "remaining_lots": [...],        # the full ledger after this year's sales, every account type
        "total_proceeds": float,
        "unfunded_shortfall": float,    # > 0 if the whole draw order couldn't raise dollars_needed —
                                         # §6: reported, never silently dropped or clamped away
    }`.
    """
    if dollars_needed < 0:
        raise ValueError("dollars_needed must be >= 0 — a negative draw isn't a draw")
    order = draw_order if draw_order is not None else DEFAULT_DRAW_ORDER

    lots_by_account_type: dict[str, list[dict]] = {}
    for lot in lots:
        lots_by_account_type.setdefault(lot["account_type"], []).append(lot)

    untouched_lots = [
        lot for account_type, account_lots in lots_by_account_type.items() if account_type not in order
        for lot in account_lots
    ]

    remaining_needed = dollars_needed
    sales_by_account_type: dict[str, dict] = {}
    touched_remaining_lots: list[dict] = []
    for account_type in order:
        account_lots = lots_by_account_type.get(account_type, [])
        if remaining_needed <= 0:
            touched_remaining_lots.extend(account_lots)
            continue
        result = sell_lots(account_lots, remaining_needed, prices_by_ticker, current_year, sale_method)
        if result["lots_sold"]:
            sales_by_account_type[account_type] = result
        touched_remaining_lots.extend(result["remaining_lots"])
        remaining_needed -= result["total_proceeds"]

    total_proceeds = sum(r["total_proceeds"] for r in sales_by_account_type.values())
    return {
        "sales_by_account_type": sales_by_account_type,
        "remaining_lots": untouched_lots + touched_remaining_lots,
        "total_proceeds": total_proceeds,
        "unfunded_shortfall": max(0.0, dollars_needed - total_proceeds),
    }


def bracket_aware_draw(
    traditional_target: float,
    taxable_target: float,
    roth_target: float,
    lots: list[dict],
    prices_by_ticker: dict[str, float],
    current_year: int,
    sale_method: str = "hifo",
) -> dict:
    """
    MODEL_WIRING.md §5.3 Stage 2 / MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1 — sells up to
    `traditional_target` from Traditional 401(k)/IRA, then up to `taxable_target` from Taxable, then
    up to `roth_target` from Roth 401(k)/IRA — three INDEPENDENT dollar targets (the caller has
    already decided the split, per that year's own tax bracket headroom and each bucket's own
    balance; this function only executes it), chained through `draw_order_fill` so each stage's
    `remaining_lots` feeds the next. HSA/Other are never touched, same as `DEFAULT_DRAW_ORDER`.

    Reuses `draw_order_fill` three times rather than adding new lot-selling logic — it already
    accepts a restricted `draw_order` list and a specific `dollars_needed` target per call.

    Returns the same shape as `draw_order_fill`, plus a per-bucket shortfall breakdown:
    `{
        "sales_by_account_type": {...},   # merged across all three stages
        "remaining_lots": [...],
        "total_proceeds": float,
        "unfunded_shortfall": float,      # sum of all three stages' own shortfalls
        "unfunded_shortfall_by_bucket": {"traditional": float, "taxable": float, "roth": float},
    }`
    """
    traditional = draw_order_fill(
        traditional_target, lots, prices_by_ticker, current_year,
        draw_order=["Traditional 401(k)", "Traditional IRA"], sale_method=sale_method,
    )
    taxable = draw_order_fill(
        taxable_target, traditional["remaining_lots"], prices_by_ticker, current_year,
        draw_order=["Taxable"], sale_method=sale_method,
    )
    roth = draw_order_fill(
        roth_target, taxable["remaining_lots"], prices_by_ticker, current_year,
        draw_order=["Roth 401(k)", "Roth IRA"], sale_method=sale_method,
    )
    return {
        "sales_by_account_type": {
            **traditional["sales_by_account_type"],
            **taxable["sales_by_account_type"],
            **roth["sales_by_account_type"],
        },
        "remaining_lots": roth["remaining_lots"],
        "total_proceeds": traditional["total_proceeds"] + taxable["total_proceeds"] + roth["total_proceeds"],
        "unfunded_shortfall": (
            traditional["unfunded_shortfall"] + taxable["unfunded_shortfall"] + roth["unfunded_shortfall"]
        ),
        "unfunded_shortfall_by_bucket": {
            "traditional": traditional["unfunded_shortfall"],
            "taxable": taxable["unfunded_shortfall"],
            "roth": roth["unfunded_shortfall"],
        },
    }


# MODEL_WIRING.md §7 — accounts eligible for rebalancing at all: tax-advantaged only. "Taxable is
# never rebalanced by selling, because that realizes gain for no modeled benefit; taxable drift is
# corrected only through the direction of new contributions" (already true today via `create_lots`'
# target-weighted purchases — no further code needed for Taxable). "Other" is excluded for the same
# reason it's excluded from DEFAULT_DRAW_ORDER: no defined tax treatment for a trade at all.
REBALANCEABLE_ACCOUNT_TYPES = [
    "Traditional 401(k)",
    "Traditional IRA",
    "Roth 401(k)",
    "Roth IRA",
    "HSA",
]


def rebalance_account(
    lots: list[dict],
    account_type: str,
    target_weights: dict[str, float],
    prices_by_ticker: dict[str, float],
    year: int,
    rebalance_band: float = 0.0,
) -> dict:
    """
    MODEL_WIRING.md §7 — rebalances ONE tax-advantaged account TYPE's lots to `target_weights`
    (normalized, same shape as `create_lots`' own `target_allocations[account_type]`) via net-zero-
    cash buy/sell trades. The caller is responsible for never calling this on Taxable lots (see
    `REBALANCEABLE_ACCOUNT_TYPES` above) — this function itself has no opinion on account type
    beyond stamping it onto any newly-created lot.

    Expects `lots` to already include THIS year's contribution purchases (from `create_lots`) and
    dividend reinvestment (from `roll_forward_portfolio`) — it's a correction pass on top of both,
    not a replacement for either (see the module docstring's gap #6 for why the literal "direct
    contributions to underweight assets first" step is skipped without changing the result).

    A ticker with no known price (missing or `<= 0` in `prices_by_ticker`) can't be traded at all —
    its lots are carried through completely unchanged, and if it has any target weight configured,
    that ticker's own deviation counts toward `residual_drift` below (a real, reportable limitation,
    not silently ignored). If `target_weights` has no positive weight anywhere (nothing configured
    for this account type at all), this is a no-op — every lot is returned completely unchanged
    rather than being force-liquidated with nowhere valid to reinvest the proceeds.

    `rebalance_band` (default `0.0` — always correct to the exact target): a ticker whose current
    weight is already within `rebalance_band` of its target is left untouched. §7's own words:
    "an optional `rebalance_band` parameter... allows a threshold approach later without a
    redesign" — implemented here from the start since it costs nothing extra.

    Returns `{
        "rebalanced_lots": [...],                  # this account's full lot list after rebalancing
        "residual_drift": float,                   # max abs(current_weight - target_weight) left
                                                     # AFTER correction — 0.0 in the common case;
                                                     # nonzero only for an unpriced ticker or a
                                                     # rebalance_band-protected one (§7's own
                                                     # explicit ask: "measured, not assumed away")
        "transfers_by_ticker": {ticker: float},     # + = bought, - = sold; must sum to ~0.0 (§6 —
                                                     # rebalancing transfers net to zero within an
                                                     # account)
    }`.
    """
    total_target_weight = sum(max(0.0, w) for w in target_weights.values())
    if total_target_weight <= 0:
        return {"rebalanced_lots": list(lots), "residual_drift": 0.0, "transfers_by_ticker": {}}

    tradeable_lots, untradeable_lots = [], []
    for lot in lots:
        price = prices_by_ticker.get(lot["ticker"])
        (tradeable_lots if price and price > 0 else untradeable_lots).append(lot)

    shares_by_ticker: dict[str, float] = {}
    for lot in tradeable_lots:
        shares_by_ticker[lot["ticker"]] = shares_by_ticker.get(lot["ticker"], 0.0) + lot["shares"]

    total_value = sum(shares * prices_by_ticker[ticker] for ticker, shares in shares_by_ticker.items())

    tradeable_target_tickers = {
        ticker for ticker, weight in target_weights.items() if weight > 0 and prices_by_ticker.get(ticker, 0.0) > 0
    }
    all_tickers = set(shares_by_ticker) | tradeable_target_tickers

    transfers_by_ticker: dict[str, float] = {}
    residual_drift = 0.0
    new_shares_by_ticker = dict(shares_by_ticker)

    for ticker in all_tickers:
        price = prices_by_ticker[ticker]
        current_value = shares_by_ticker.get(ticker, 0.0) * price
        current_weight = current_value / total_value if total_value > 0 else 0.0
        target_weight = max(0.0, target_weights.get(ticker, 0.0)) / total_target_weight
        deviation = current_weight - target_weight

        if abs(deviation) <= rebalance_band:
            residual_drift = max(residual_drift, abs(deviation))
            continue

        target_value = max(0.0, total_value * target_weight)
        transfer_dollars = target_value - current_value  # + = buy, - = sell
        if abs(transfer_dollars) < 1e-9:
            continue
        transfers_by_ticker[ticker] = transfer_dollars
        new_shares_by_ticker[ticker] = max(0.0, new_shares_by_ticker.get(ticker, 0.0) + transfer_dollars / price)

    # A ticker with a target weight but no known price can never be traded toward it — its
    # deviation is real, permanent drift for this year, reported rather than silently dropped.
    for ticker, weight in target_weights.items():
        if weight > 0 and prices_by_ticker.get(ticker, 0.0) <= 0 and ticker not in shares_by_ticker:
            residual_drift = max(residual_drift, weight / total_target_weight)

    # Rebuild the lot ledger: an existing ticker's lots scale pro-rata to its new total share count
    # (no tax consequence to preserve here, so WHICH lot shrinks/grows doesn't matter — see this
    # function's own docstring — but lots stay separate, not blended into one, since basis still
    # matters for a future Taxable rollover/conversion); a brand-new ticker gets one fresh lot at
    # this year's price.
    lots_by_ticker: dict[str, list[dict]] = {}
    for lot in tradeable_lots:
        lots_by_ticker.setdefault(lot["ticker"], []).append(lot)

    rebalanced_lots: list[dict] = []
    for ticker, new_total_shares in new_shares_by_ticker.items():
        existing = lots_by_ticker.get(ticker, [])
        old_total_shares = shares_by_ticker.get(ticker, 0.0)
        if existing and old_total_shares > 0:
            scale = new_total_shares / old_total_shares
            for lot in existing:
                scaled_shares = lot["shares"] * scale
                if scaled_shares > 1e-9:
                    rebalanced_lots.append({**lot, "shares": scaled_shares})
        elif new_total_shares > 1e-9:
            rebalanced_lots.append(
                {
                    "ticker": ticker,
                    "account_type": account_type,
                    "shares": new_total_shares,
                    "basis_per_share": prices_by_ticker[ticker],
                    "year_acquired": year,
                }
            )

    return {
        "rebalanced_lots": rebalanced_lots + untradeable_lots,
        "residual_drift": residual_drift,
        "transfers_by_ticker": transfers_by_ticker,
    }


# Module G2's dynamic strategy (MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 2, 2026-08-29)
# added as a new branch below, per MODEL_WIRING.md §2.3's own design note — not a rewrite of this
# function or its callers.
WITHDRAWAL_STRATEGIES = ("flat_percentage", "target_net_spending")


def annual_withdrawal_target(strategy: str, params: dict, state: dict) -> float:
    """
    MODEL_WIRING.md §2.3 — the single dispatch point for a year's TARGET withdrawal amount, before
    netting against cash already available from dividends (that netting is the caller's job — see
    `modules/projection.py` — this function only answers "how much does the strategy say to draw").

    `state` deliberately carries the union of what ANY planned strategy could need — `portfolio_value`
    (this year's starting value, before this year's own growth/dividends/contributions), plus
    `prior_year_withdrawal`, `years_elapsed`, `spending_need` — the last now used by
    `"target_net_spending"` below — so Module G2's guardrails/floor-ceiling/RMD-based strategies can
    be added as a new branch here later without a signature rewrite of this function or its callers,
    per §2.3's own explicit design note.

    `"flat_percentage"`: `params={"rate": float}` → `rate × state["portfolio_value"]`, clamped to
    `>= 0.0` (a negative portfolio value shouldn't occur, but defensively never produces a negative
    withdrawal target).

    `"target_net_spending"` (Module G2 Part 2, MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md) — a
    dynamic strategy that sizes the TOTAL draw so `net_retirement_income` closes on `spending_need`
    exactly, via fixed-point iteration against the real bracket-aware split and tax pass. This
    function itself only returns the ITERATION'S STARTING GUESS (`state["spending_need"]`, per that
    doc's own "start from a naive guess" instruction, clamped to `>= 0.0`) — the actual solve loop
    runs in `modules/projection.py`, which alone has the per-year context (current lots, tax bracket
    table, RMD table) this function's own signature doesn't carry. Called once per year to seed that
    loop, the same one-call-per-year shape as `"flat_percentage"`.

    Any other `strategy` raises `NotImplementedError` with a plain message — never silently falls
    back to `"flat_percentage"` (§2.3's own explicit instruction: "Unimplemented strategies raise
    `NotImplementedError` with a plain message rather than silently falling back to the flat rule").
    """
    if strategy == "flat_percentage":
        return max(0.0, params["rate"] * state["portfolio_value"])
    if strategy == "target_net_spending":
        return max(0.0, state.get("spending_need", 0.0))
    raise NotImplementedError(
        f"Withdrawal strategy {strategy!r} is not implemented — only {WITHDRAWAL_STRATEGIES} exist "
        "in this build (MODEL_WIRING.md §2.3; guardrails/floor-ceiling strategies remain future "
        "Module G2 work)."
    )
