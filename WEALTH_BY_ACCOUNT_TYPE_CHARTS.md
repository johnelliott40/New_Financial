# Two new "wealth by account type" charts — additive to the existing wealth chart

**Prepared by:** Claude (Cowork), 2026-08-22, against the live codebase as of this write (`ui/projection_tab.py`
97,149 bytes / `modules/projection.py` 75,671 bytes / `modules/portfolio.py` 17,127 bytes / `modules/investing.py`
36,635 bytes — post all prior specs this session). Read `modules/projection.py` lines 380-520 and 950-1010,
`modules/investing.py`'s lot-shape docstrings, `modules/portfolio.py` lines 1-60, and `ui/projection_tab.py`
lines 1-460 and 530-1425 directly before writing this.

## The request, verbatim

> "Can you also make two new charts that are in portfolio that show the wealth over time for 1) the no income
> no expense base case 2) the income scenario based on current inputs. This is a[s]plitting the current wealth
> chart out into two area charts that show the contribution from each account type (401k, roth, taxable, etc.).
> but also retaining the current wealth chart so the net effect is only addition."

Two new **stacked-area** charts, one per scenario, each breaking total portfolio value down by account type
(Taxable / Traditional 401(k) / Traditional IRA / Roth IRA / Roth 401(k) / HSA / Other) — purely additive next
to the existing single-line-per-scenario "Total wealth over time" hero chart, which is not touched.

## Flagging one ambiguity before the design: "in portfolio"

There is a literal `ui/portfolio_tab.py` (44,405 bytes) in this app — a real, separate tab from
`ui/projection_tab.py`. But it has **zero** wealth-over-time content today (confirmed: no match for
`wealth|Wealth` anywhere in that file) — it's the current-snapshot holdings/allocation editor, not a
projection view. The existing "Total wealth over time" hero chart this request explicitly says to "retain"
lives entirely on the **Projection tab** (`_total_wealth_chart`, `ui/projection_tab.py` line 534), fed by
`rows`/`baseline_rows` — the full year-by-year projection output — which `ui/portfolio_tab.py` never computes
or receives.

Given that, I'm reading "in portfolio" as **"about my portfolio's wealth"**, not "on the literal Portfolio
tab" — and placing both new charts on the **Projection tab, directly beneath the existing hero chart**, same
as every other addition this session (Plotly redesign, adjusted-wealth stats). This is the only placement
that doesn't require plumbing the entire projection engine into a tab that doesn't have it. If you actually
want these on the literal Portfolio tab instead, say so and Claude Code can either duplicate the relevant
`rows`/`baseline_rows` computation there or pass it in from the Projection tab's session state — a bigger
change than what's speced below, so flagging rather than guessing.

## What data already exists — no new modeling needed

Every row in `rows` and `baseline_rows` that has a non-`None` `portfolio_value` **also** has non-`None`
`ending_lots` (a flat list of `{"ticker", "account_type", "shares", "basis_per_share", "year_acquired"}`) and
`ending_prices_by_ticker` (`{ticker: price}`) — confirmed both are always set together in the same three spots
in `modules/projection.py` (lines 509-511, 996-998, 1000-1002). `ui/projection_tab.py` already filters to
exactly these usable rows for the existing hero chart:

```python
total_wealth_rows = [r for r in rows if r["tax_available"] and r["portfolio_value"] is not None]
baseline_wealth_rows = [r for r in baseline_rows if r["tax_available"] and r["portfolio_value"] is not None]
```

Both new charts should consume these exact same two row lists — no new filtering, no new baseline run, no
change to how `rows`/`baseline_rows` are produced. The only new work is: for each row, group `ending_lots` by
`account_type` and sum `shares × ending_prices_by_ticker[ticker]` per group. That grouping doesn't exist
anywhere yet (per-row `portfolio_value` is a single summed float, not broken out).

## Step 1 — new pure helper in `modules/portfolio.py`

Add one function next to the existing `ACCOUNT_TYPES`/`summarize_holdings`:

```python
def portfolio_value_by_account_type(lots: list[dict], prices_by_ticker: dict) -> dict[str, float]:
    """
    Sums `shares * prices_by_ticker[ticker]` per `account_type`, across `lots` (the same flat lot
    shape `modules.investing.create_lots`/`roll_forward_portfolio` produce and
    `modules.projection.project_multi_year` stores per row as `ending_lots`). Returns EVERY key in
    `ACCOUNT_TYPES`, in that order, defaulting to 0.0 for any account type not currently held — so a
    caller building a stacked-area chart across many years gets a stable, complete category set
    every year, never a KeyError on a year where (say) HSA happens to be empty.

    A lot whose ticker has no entry in `prices_by_ticker` is skipped (mirrors
    `holding_liquidation_value_estimate`'s own "unpriced ticker" gap elsewhere in this module —
    not silently zeroed against a fabricated price, and not raised, since one bad ticker shouldn't
    take down an entire year's chart).
    """
    totals = {account_type: 0.0 for account_type in ACCOUNT_TYPES}
    for lot in lots:
        price = prices_by_ticker.get(lot["ticker"])
        if price is None:
            continue
        totals[lot["account_type"]] = totals.get(lot["account_type"], 0.0) + lot["shares"] * price
    return totals
```

Pure, no Streamlit — matches this module's own ground rule. `ACCOUNT_TYPES` is already the module's own
constant, so this is a one-function addition, not a new import.

**Exactness check for Claude Code to add as a test**: for any row, `sum(portfolio_value_by_account_type(row["ending_lots"], row["ending_prices_by_ticker"]).values())` must equal `row["portfolio_value"]` to floating-point
tolerance — same "provably exact, not approximate" bar the existing pre-retirement income stack already
holds itself to (see that chart's own docstring in `ui/projection_tab.py`).

## Step 2 — new chart-building function in `ui/projection_tab.py`

One function, called twice (once per scenario) — same pattern as `_pre_retirement_overview_chart`'s stacked
area, adapted from six income-destination bands to the seven account-type bands:

```python
from modules.portfolio import ACCOUNT_TYPES, portfolio_value_by_account_type

# Reuses seven of the eight validated categorical hues (dataviz method's own reference palette,
# adjacent-pair CVD-validated ordering for stacks/lines) -- NOT reusing any of _PLOTLY_COLORS' existing
# semantic assignments (gross/net/expense/profit/wealth_base/wealth_baseline), since those mean
# something different on other charts and would be confusing reused here. Order below is this
# palette's own validated adjacent order, not account-type semantics -- unrelated categorical data,
# so a validated-but-otherwise-arbitrary assignment is correct, not a placeholder.
_ACCOUNT_TYPE_COLORS = {
    "Taxable": "#2a78d6",             # slot 1, blue
    "Traditional 401(k)": "#eb6834",  # slot 2, orange
    "Traditional IRA": "#1baf7a",     # slot 3, aqua
    "Roth 401(k)": "#eda100",         # slot 4, yellow
    "Roth IRA": "#e87ba4",            # slot 5, magenta
    "HSA": "#008300",                 # slot 6, green
    "Other": "#4a3aa7",               # slot 7, violet
}
# Bottom-to-top stack order -- Taxable (most liquid / most likely to be drawn down first per
# DEFAULT_DRAW_ORDER) at the base, tax-advantaged accounts above it, "Other" on top since it's the
# catch-all. First-added = bottom, same go.Figure stackgroup convention as
# _pre_retirement_overview_chart. Purely a reading-order choice -- flag if you'd rather order these
# by your own account balances (largest at bottom) instead.
_ACCOUNT_TYPE_STACK_ORDER = ["Taxable", "Traditional 401(k)", "Traditional IRA", "Roth 401(k)", "Roth IRA", "HSA", "Other"]


def _wealth_by_account_type_chart(rows: list[dict], title: str) -> go.Figure:
    """
    Same total portfolio value as `_total_wealth_chart`, for ONE scenario's row set, split into a
    stacked area by account type -- "where is the wealth actually held," not just "how much." Each
    row's `ending_lots`/`ending_prices_by_ticker` (already computed by project_multi_year, no new
    modeling) are grouped via `modules.portfolio.portfolio_value_by_account_type`. Bands sum to
    EXACTLY that row's own `portfolio_value` by construction (see the helper's own docstring) --
    same exactness bar `_pre_retirement_overview_chart` holds itself to for its six income bands.

    Only the account types that ever hold a nonzero balance across `rows` get a trace -- an account
    type that's always $0 (e.g., no HSA configured) is dropped rather than plotted as a flat zero
    band cluttering the legend.
    """
    years = [r["year"] for r in rows]
    by_type: dict[str, list[float]] = {account_type: [] for account_type in _ACCOUNT_TYPE_STACK_ORDER}
    for r in rows:
        values_this_year = portfolio_value_by_account_type(r["ending_lots"], r["ending_prices_by_ticker"])
        for account_type in _ACCOUNT_TYPE_STACK_ORDER:
            by_type[account_type].append(values_this_year[account_type])

    active_types = [t for t in _ACCOUNT_TYPE_STACK_ORDER if any(v > 0.005 for v in by_type[t])]

    fig = go.Figure()
    for account_type in active_types:
        color = _ACCOUNT_TYPE_COLORS[account_type]
        fig.add_trace(
            go.Scatter(
                x=years,
                y=by_type[account_type],
                mode="lines",
                name=account_type,
                stackgroup="wealth_by_account_type",
                line=dict(width=0.5, color=color),
                fillcolor=_hex_to_rgba(color, 0.85),
                hovertemplate="<b>$%{y:,.0f}</b><extra>" + account_type + "</extra>",
            )
        )

    totals = [r["portfolio_value"] for r in rows]
    if years:
        _dollar_end_label(fig, years[-1], totals[-1], _PLOTLY_COLORS["text_primary"])

    y_max = max(totals) if totals else 0
    _style_chart(fig, title=title, height=550, y_max=y_max)
    return fig
```

Two notes on fidelity to the existing conventions already in this file:

- **`allow_negative` stays `False` (the default)** — an account balance can shrink to $0 (fully drawn down)
  but should never go negative under `sell_lots`'/`draw_order_fill`'s own existing logic; if Claude Code's
  own testing finds a case where a band does go negative, that's a real bug to fix upstream (in
  `sell_lots`), not something to paper over by flipping this flag here.
- **Relief-rule flag, per the reference palette's own documented gate**: two of the seven hues used above
  (magenta, slot 5 / yellow, slot 4) sit below 3:1 contrast against this app's light chart surface — the
  palette doc's own "relief rule" applies, same as the existing gross/net chart already ships a mitigation
  for its own sub-3:1 pairing (direct end-labels). Here the mitigation is already structural: the legend
  below the chart carries the text label for every band (not color alone), and the unified hover tooltip
  names each band explicitly on hover — both already satisfy "never color alone" without extra work. Not
  adding per-band end-labels on top of that (would be 5-7 competing labels on a stacked chart, the exact
  clutter `_dollar_end_label`'s own docstring warns against) — only the grand-total label at the very top,
  as coded above.

## Step 3 — call sites: additive, directly under the existing hero chart

In the render function, immediately after the existing `_total_wealth_chart` block (`ui/projection_tab.py`
around line 1407-1425) — **that block is not modified at all**, this is appended right after it:

```python
    if total_wealth_rows:
        st.plotly_chart(
            _wealth_by_account_type_chart(
                total_wealth_rows, "Wealth by account type — with income & expenses (as planned)"
            ),
            theme=None,
            config=PLOTLY_CONFIG,
            width="stretch",
        )
    if baseline_wealth_rows:
        st.plotly_chart(
            _wealth_by_account_type_chart(
                baseline_wealth_rows, "Wealth by account type — no income or expenses from today"
            ),
            theme=None,
            config=PLOTLY_CONFIG,
            width="stretch",
        )
```

Order matches the request's own numbering read literally reversed for placement (request says "1) no
income/no expense, 2) current inputs," but the current-inputs/"as planned" scenario is the one already shown
first everywhere else on this page — e.g. the hero chart's own two traces, added in "as planned" then
"baseline" order). Flagging the ordering choice rather than silently picking one: swap the two `st.plotly_chart`
calls if you'd rather match the request's literal 1-then-2 order instead.

## What stays completely untouched

- `_total_wealth_chart` itself — zero changes, same two lines, same reference vline, same title.
- `rows`/`baseline_rows`/`total_wealth_rows`/`baseline_wealth_rows` computation — reused exactly as-is, no
  new filtering, no new project_multi_year call, no new project_no_income_no_expense_baseline call.
- Every other chart/section on the Projection tab (pre-retirement stack, retirement-income chart, tables,
  stat groups) — untouched.
- `ui/portfolio_tab.py` — untouched (see the "in portfolio" note above for why).

## Summary of concrete next actions for Claude Code

1. Add `portfolio_value_by_account_type` to `modules/portfolio.py` (Step 1) plus the exactness test
   described there (`tests/test_portfolio.py` — sum-of-bands equals `row["portfolio_value"]`).
2. Add `_ACCOUNT_TYPE_COLORS`, `_ACCOUNT_TYPE_STACK_ORDER`, and `_wealth_by_account_type_chart` to
   `ui/projection_tab.py` (Step 2), importing `ACCOUNT_TYPES`/`portfolio_value_by_account_type` from
   `modules.portfolio`.
3. Insert the two new `st.plotly_chart` calls immediately after the existing hero-chart block (Step 3) —
   confirm via diff that the existing block's own lines are byte-for-byte unchanged.
4. Run the app against `saved_states/real_portfolio.json` (or whatever save is current) and visually confirm:
   the top band of each new stack, summed, tracks the single line on the existing hero chart for the same
   scenario, year for year.
5. If the answer to the "in portfolio" ambiguity above is "the literal Portfolio tab, not the Projection
   tab," come back before building — that's a materially different, larger change (passing the full
   projection into a tab that doesn't currently receive it) than what's speced here.
