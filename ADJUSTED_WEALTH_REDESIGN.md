# Adjusted wealth over time — redesign spec for Claude Code

**Prepared by:** Claude (Cowork), from a read-only review of the codebase on the user's machine
(`New_Financial/`), 2026-08-14. No files were modified — this is analysis plus a concrete
implementation spec, in the same style/rigor as `RETIREMENT_REPORTING_AUDIT.md` (same folder).

**Dependency on `RETIREMENT_REPORTING_AUDIT.md`:** this spec's "average annual net retirement
income" stat (§4) directly reuses `net_retirement_income`, a new field defined in that document's
Part 2.3 (`total_withdrawal_income` / `retirement_taxes_paid` / `net_retirement_income`). If that
work hasn't landed yet, implement it first, or fold the two field-additions to
`modules/projection.py` into one pass — they touch the same function and the same row schema.

---

## 1. What the current "adjusted wealth" metric is, and why it's the wrong shape

`ui/projection_tab.py` lines 849-865 currently show:

```python
discount_rate = st.session_state.portfolio_blended_real_return
npv = net_present_value(rows, discount_rate)
st.metric("NPV of future profit", f"${npv:,.0f}" if npv is not None else "—", ...)
```

`net_present_value` (`modules/projection.py` lines 841-864) discounts every projected year's
`profit` (net income minus expenses, from the **base-case, with-income-and-expenses** projection)
back to today. This is the metric to remove, per the user's request — **this is specifically the
"NPV of future profit" `st.metric` above, not** `modules/portfolio.py`'s
`holding_liquidation_value_estimate`/`total_liquidation_value_estimate` (the Portfolio tab's
current-net-worth estimate, which applies a flat pretax discount rate to today's holdings and has
nothing to do with income/expense projections at all — leave that alone; it's a different,
already-correct concept). **Double-check this distinction before deleting anything** — the two
share the word "estimate" in nearby docstrings but are unrelated code paths.

**Why discounting `profit` produces a conceptually confused number:** `profit` already bakes in a
whole forecast — the user's assumed future income S-curve, their assumed future spending S-curve,
whether they keep working, whether expenses stay flat or grow — layered on top of the market-return
assumption used as the discount rate. The result isn't "what is my portfolio actually worth today,"
it's "what is the present value of my entire life plan, assuming every income/spending assumption
holds exactly." Two different users with identical portfolios but different assumed future saving
behavior get wildly different "wealth" numbers from a figure that's supposed to answer "how rich am
I." **Taxes are different**: the tax bill on an existing portfolio's future dividends, realized
gains, and Traditional-account distributions is a real, near-mechanical future obligation that
exists independent of any income/spending forecast — it's closer to "what does the IRS/FTB already
have a claim on, given what I hold today." Isolating *just* that stream and netting it against
today's mark-to-market portfolio value is the user's proposed fix, and it's the right one: it
produces a "wealth" number that means "what I actually have, after settling the one obligation
that's essentially unavoidable regardless of what I do next" — not entangled with a savings-rate
forecast.

---

## 2. The new baseline: "no future income, no future expenses"

### 2.1 Precise definition

Take **today's actual portfolio** (the same `initial_lots`/`initial_prices_by_ticker`/`universe`
already built from the Portfolio tab and passed into the existing base-case `project_multi_year`
call in `ui/projection_tab.py`) and run a **second** `project_multi_year` call, identical in every
respect **except**:

| Parameter | Base case (existing) | No-income/no-expense baseline (new) |
|---|---|---|
| `income_inputs` | user's actual W-2/SE curves + breaks | **all zeroed** — see §2.2 |
| `expense_inputs` | user's actual expense curve | **all zeroed** — see §2.2 |
| `retirement_date` | user's planned retirement date | **`current_date`** — see §2.3 for why |
| `phase_dates["savings_stop_date"]` | user's planned value | **`current_date`** |
| `phase_dates["withdrawal_start_date"]` | user's planned value | **unchanged** — same as base case |
| `phase_dates["ss_claim_date"]` | user's planned value | **unchanged** — same as base case |
| `use_contribution_waterfall` | `True` (or the user's toggle) | **`False`** — nothing to route |
| `contributions_by_year` | whatever's configured | **`None`** |
| `universe`, `initial_lots`, `initial_prices_by_ticker`, `target_allocations`, `inflation_rate`, `draw_order`, `sale_method`, `rebalance`, `rebalance_band`, `withdrawal_strategy`, `withdrawal_strategy_params`, `filing_status`, `bracket_table`, `birth_date`, `current_date`, `horizon_date` | as configured | **identical to the base case** — the only thing being isolated is the income/expense/contribution behavior, not the portfolio, tax law, or withdrawal mechanics |

This means: the frozen portfolio still rolls forward year by year exactly like the base case (each
holding compounding at its own asset-class return net of expense ratio, dividends taxed and
reinvested pre-withdrawal — the existing Step 3/§4 machinery, untouched), still hits the **same**
`withdrawal_start_date` the user actually plans to retire on, and still runs the **same** withdrawal
strategy (flat-percentage rule, same rate, same draw order) from that date forward. The only thing
that's different is that there's no more W-2/SE income and no more spending assumed from today
onward — exactly the user's framing ("as if no more income is earned and no more expenses are
outlaid... then run the retirement withdrawal process on that situation").

### 2.2 What "all zeroed" means concretely

```python
zero_curve = {"start_value": 0.0, "end_value": 0.0, "midpoint_years": 1, "steepness": 0.0001}
no_income_inputs = {
    "current_year_already_earned_w2": 0.0, "current_year_already_earned_se": 0.0,
    "current_year_yet_to_earn_w2": 0.0, "current_year_yet_to_earn_se": 0.0,
    "w2_curve": zero_curve, "se_curve": zero_curve,
    "breaks": [],  # no income breaks and no pension/break income either — a pension is a form of
                   # future income the user hasn't necessarily "stopped," but the request is
                   # explicit ("no more income is earned") — flag this to the user if they have a
                   # configured pension/break and expect it to survive this baseline; don't guess.
}
no_expense_inputs = {
    "current_year_already_incurred_expense": 0.0, "current_year_yet_to_incur_expense": 0.0,
    "expense_curve": zero_curve,
}
```

This produces `gross_w2 = gross_se = gross_ordinary_break_income = gross_expense = 0.0` for every
projected year — `gross_income` in this baseline is *purely* investment income (taxable dividends/
interest pre-withdrawal, plus withdrawal-driven distributions/gains once `withdrawal_start_date`
hits), and `total_tax` on each row is therefore entirely attributable to the existing portfolio,
never to earned income. This is exactly the tax stream the user wants isolated and discounted.

### 2.3 Why `retirement_date`/`savings_stop_date` move to `current_date` (not left at the user's real plan)

`modules/projection.py`'s dissaving gate (line 629) fires whenever `profit < 0 and year <
retirement_date.year and withdrawing_fraction <= 0`. With expenses at `$0`, `profit` pre-withdrawal
in this baseline is just after-tax dividend income — normally slightly positive, so dissaving
wouldn't fire regardless — but setting `retirement_date = current_date` closes off that gate
entirely and deterministically (`year < retirement_date.year` is false for every projected year),
so there's no chance of an unintended sale before the real withdrawal phase starts, regardless of
edge cases in tax-on-dividends arithmetic. `savings_stop_date = current_date` is the equivalent
belt-and-suspenders move on the saving side (`saving_fraction` immediately drops to 0), though it's
moot here since `use_contribution_waterfall=False` already guarantees no contributions happen.
**`withdrawal_start_date` and `ss_claim_date` must NOT move** — those stay exactly as the user
configured them, since the whole point is "what if I stop earning/spending today, but still retire
and start Social Security on my originally planned schedule."

### 2.4 A known, honest limitation to carry over, not fix here

Same as the base case: taxes owed on pre-withdrawal dividend/interest income aren't funded by an
actual cash outflow anywhere in the model (no dissaving triggers for a merely-tax-driven negative
profit once `retirement_date` gates it off, as above). This baseline's `total_tax` figures are
therefore the *accrued* tax obligation, not a literal cash-paid figure — which is actually fine for
this feature's purpose (we're computing a tax *obligation* to discount, not modeling how it gets
paid), but worth a one-line caveat in the metric's `help=` text so it doesn't read as more precise
than it is.

### 2.5 Suggested implementation shape

Rather than duplicating this parameter-zeroing logic inline in `ui/projection_tab.py`, add a small
pure helper to `modules/projection.py` (keeps "what does the no-income/no-expense baseline mean"
in `modules/`, per `CLAUDE.md` ground rule 4 — Streamlit stays presentation-only):

```python
def project_no_income_no_expense_baseline(
    current_date, horizon_date, retirement_date, birth_date, filing_status, bracket_table,
    withdrawal_start_date, ss_claim_date, universe=None, initial_lots=None,
    initial_prices_by_ticker=None, inflation_rate=0.0, target_allocations=None, draw_order=None,
    sale_method="hifo", rebalance=False, rebalance_band=0.0, withdrawal_strategy="flat_percentage",
    withdrawal_strategy_params=None,
) -> list[dict]:
    """
    The 'what if no more income is earned and no more expenses are incurred, starting today'
    baseline (see ADJUSTED_WEALTH_REDESIGN.md §2) — same portfolio, same withdrawal_start_date/
    ss_claim_date, same withdrawal strategy as whatever the caller's own base-case projection uses,
    but income/expenses zeroed and retirement_date/savings_stop_date pulled to current_date so
    nothing but the withdrawal engine (from withdrawal_start_date onward) ever touches the ledger.
    """
    zero_curve = {"start_value": 0.0, "end_value": 0.0, "midpoint_years": 1, "steepness": 0.0001}
    zero_income = {
        "current_year_already_earned_w2": 0.0, "current_year_already_earned_se": 0.0,
        "current_year_yet_to_earn_w2": 0.0, "current_year_yet_to_earn_se": 0.0,
        "w2_curve": zero_curve, "se_curve": zero_curve, "breaks": [],
    }
    zero_expense = {
        "current_year_already_incurred_expense": 0.0, "current_year_yet_to_incur_expense": 0.0,
        "expense_curve": zero_curve,
    }
    return project_multi_year(
        zero_income, zero_expense, current_date, horizon_date, retirement_date=current_date,
        birth_date=birth_date, filing_status=filing_status, bracket_table=bracket_table,
        use_contribution_waterfall=False,
        phase_dates={
            "savings_stop_date": current_date,
            "withdrawal_start_date": withdrawal_start_date,
            "ss_claim_date": ss_claim_date,
        },
        universe=universe, initial_lots=initial_lots, initial_prices_by_ticker=initial_prices_by_ticker,
        inflation_rate=inflation_rate, target_allocations=target_allocations, draw_order=draw_order,
        sale_method=sale_method, rebalance=rebalance, rebalance_band=rebalance_band,
        withdrawal_strategy=withdrawal_strategy, withdrawal_strategy_params=withdrawal_strategy_params,
    )
```

Note this passes `retirement_date=current_date` as the top-level parameter (not part of
`phase_dates`) — matches `project_multi_year`'s existing signature (`modules/projection.py` line
90), which internally folds it into `phase_dates` for `phase_flags_for_year` (line 340).

---

## 3. NPV of future taxes → the new "Adjusted wealth" metric

### 3.1 Generalize `net_present_value`

`modules/projection.py`'s existing `net_present_value` (lines 841-864) only discounts `profit`.
Generalize it to take a field name, and keep the old name as a one-line wrapper so nothing else
that calls it breaks:

```python
def net_present_value_of_field(rows: list[dict], discount_rate: float, field: str) -> float | None:
    """Same discounting convention as net_present_value (years_from_now exponent, current year at
    full value), generalized to any per-row numeric field — e.g. 'profit' or 'total_tax'."""
    known = [r for r in rows if r[field] is not None]
    if not known:
        return None
    return sum(r[field] / (1.0 + discount_rate) ** r["years_from_now"] for r in known)


def net_present_value(rows: list[dict], discount_rate: float) -> float | None:
    """Present value of every projected year's profit — see net_present_value_of_field."""
    return net_present_value_of_field(rows, discount_rate, "profit")
```

### 3.2 The new metric

```python
baseline_rows = project_no_income_no_expense_baseline(...)  # §2.5
today_portfolio_value = _portfolio_value(initial_lots, initial_prices_by_ticker)  # today's actual
    # mark-to-market value — NOT baseline_rows[0]["portfolio_value"], which is already the END of
    # year 0 after that year's own growth/dividends/tax are applied. "Adjusted wealth" should net
    # today's real, current holdings against the PV of everything the portfolio owes going forward,
    # not a value that's already one step into the future.
pv_future_taxes = net_present_value_of_field(baseline_rows, discount_rate, "total_tax")
adjusted_wealth_today = (
    today_portfolio_value - pv_future_taxes if pv_future_taxes is not None else None
)
```

`_portfolio_value` already exists (`modules/projection.py` line 836) but is currently private
(leading underscore, module-private). Either export it (drop the underscore, it's a one-line pure
function with no reason to stay hidden) or compute the same sum inline in the UI — exporting it is
the cleaner fix and `ui/projection_tab.py` needs it directly for this feature.

Replace the existing `st.metric("NPV of future profit", ...)` block (lines 849-868) with:

```python
st.metric(
    "Adjusted wealth (today, net of future taxes)",
    f"${adjusted_wealth_today:,.0f}" if adjusted_wealth_today is not None else "—",
    help=(
        f"Today's portfolio value (${today_portfolio_value:,.0f}) minus the present value of every "
        f"future year's tax bill on that SAME portfolio if you earned no more income and spent "
        f"nothing further from today forward — dividends/interest pre-retirement, and realized "
        f"gains/taxable distributions from retirement withdrawals starting "
        f"{withdrawal_start_date:%Y}, all discounted at {discount_rate:.2%} (the Portfolio tab's "
        "blended real expected return). This isolates what your CURRENT holdings actually owe in "
        "future tax, independent of any assumption about your future income or spending — it is "
        "not a forecast of your total future wealth (see the two 'wealth at retirement' figures "
        "below for that)."
    ),
)
```

This metric is shown **once**, not per-scenario — it's specifically "what am I worth today, net of
the one future obligation that doesn't depend on behavior." Do not compute an equivalent "adjusted
wealth" for the with-income-and-expenses scenario — the user explicitly asked for it to appear only
here.

---

## 4. Two more stats, per scenario

For **both** the baseline (`project_no_income_no_expense_baseline`) and the base case (the existing
with-income-and-expenses `rows`), compute:

**a. Wealth at retirement** — the portfolio's value at the moment withdrawals begin. This needs a
new field, since it doesn't currently exist on the row: `modules/projection.py` already computes
`portfolio_value_at_start_of_year` as a local variable each loop iteration (line 345,
`_portfolio_value(current_lots, current_prices)`, captured before that year's own
growth/dividends/contributions) but never stores it. Add one line:

```python
row["portfolio_value_at_start_of_year"] = portfolio_value_at_start_of_year if portfolio_active else None
```

Then, in the UI: `wealth_at_retirement = next((r["portfolio_value_at_start_of_year"] for r in rows
if r["withdrawal_target"] is not None and r["withdrawal_target"] >= 0 and <withdrawing_fraction > 0
for this row>), None)` — more precisely, use whatever boolean §2.4 of
`RETIREMENT_REPORTING_AUDIT.md` recommends exposing for "is this a withdrawal-phase row" (that
document flagged `withdrawal_target > 0` as an unreliable proxy for the same reason — reuse
whatever clean boolean gets added there rather than inventing a second one here).

**b. Average annual net retirement income** — arithmetic mean of `net_retirement_income` (from
`RETIREMENT_REPORTING_AUDIT.md` §2.3 — `total_withdrawal_income - retirement_taxes_paid`) across
every withdrawal-phase row in that scenario's own `rows` list, through `horizon_date`. Not
discounted — this is meant as "roughly how much spendable cash per year should I expect in
retirement," a plain average in real (already inflation-adjusted) dollars, not a present value.

```python
def average_annual_net_retirement_income(rows: list[dict]) -> float | None:
    """Plain arithmetic mean of net_retirement_income across withdrawal-phase rows — real dollars,
    not discounted; see ADJUSTED_WEALTH_REDESIGN.md §4b for why an average, not an NPV, here."""
    values = [r["net_retirement_income"] for r in rows if r.get("net_retirement_income") is not None]
    return sum(values) / len(values) if values else None
```

(Put this alongside `net_present_value_of_field` in `modules/projection.py` — small, pure, testable.)

---

## 5. UI layout

Recommend two side-by-side stat groups, directly below (or replacing) the current single "NPV of
future profit" metric — this is a layout suggestion, confirm with the user before finalizing wording:

```
Adjusted wealth (today, net of future taxes): $X          ← §3, shown once, baseline-derived

┌─ If income & spending continue as planned ──┐  ┌─ If you stopped earning & spending today ──┐
│ Wealth at retirement:        $Y              │  │ Wealth at retirement:        $Y'            │
│ Avg. annual net retirement income: $Z        │  │ Avg. annual net retirement income: $Z'       │
└───────────────────────────────────────────────┘  └─────────────────────────────────────────────┘
```

Use `st.columns(2)` with an `st.metric` pair in each, matching the existing metric style at lines
849-868. Confirm exact labels/wording with the user — the above is a functional placeholder, not
final copy.

### 5.1 Wealth-over-time chart — add the second line

The existing "Portfolio value over time" chart (`ui/projection_tab.py` lines 1103-1114) currently
plots one series (the base case's `portfolio_value` per year) and is buried inside the "Contribution
destinations & portfolio ledger" expander. Add a second series from
`project_no_income_no_expense_baseline`'s own `portfolio_value` per year, on the same chart, with a
legend distinguishing the two (e.g. "With income & expenses" vs. "No income or expenses from
today"). Use the long-format pattern already established in `_gross_net_chart` (lines 104-141:
`pd.concat([df.assign(series=...), ...])` + `alt.Color("series:N", ...)`) for consistency with the
rest of this tab's chart style.

**Open question for the user, don't guess:** given this chart now directly supports the new
headline stats above it, should it move out of the collapsed expander into the main page flow
(e.g. directly under the new stat groups), or stay where it is? The expander was reasonable when it
was one supporting chart among several ledger details; it's a much more central figure now that the
whole point of this feature is comparing the two wealth trajectories visually.

---

## 6. Testing/verification checklist

- `tests/test_projection.py`: a test that `project_no_income_no_expense_baseline` produces
  `gross_w2 == gross_se == gross_ordinary_break_income == gross_expense == 0.0` for every row.
- A test that `net_present_value_of_field(rows, 0.0, "total_tax")` (zero discount rate — trivial
  case) equals a plain `sum(r["total_tax"] for r in rows if r["total_tax"] is not None)`, confirming
  the discounting collapses correctly at `rate=0`.
- A test using a portfolio with ONLY Roth holdings: confirm the baseline's `total_tax` is `$0` in
  every year (no dividends taxed if the universe has none, and Roth withdrawals are never taxable) —
  a useful sanity check that `adjusted_wealth_today` in that case equals `today_portfolio_value`
  exactly (no tax obligation to discount at all).
- Manually verify in the running app: for a portfolio the user actually holds today, confirm
  `Adjusted wealth (today, net of future taxes)` is between `$0` and `today_portfolio_value` (it
  should never exceed today's value — taxes are a subtraction, never a credit) for any reasonable
  discount rate and horizon.
- Confirm `wealth_at_retirement` for the baseline is `<=` `wealth_at_retirement` for the base case
  whenever the base case's income exceeds its expenses on average pre-retirement (i.e., the "keep
  earning and saving" plan should generally show more wealth at retirement than "stop today," unless
  the user's real plan is dissaving pre-retirement, e.g. expenses regularly exceeding income) — not
  a strict invariant to assert in a test, but a useful eyeball check while building.

---

## Summary of concrete next actions for Claude Code

1. Confirm the field-naming dependency on `RETIREMENT_REPORTING_AUDIT.md` §2.3
   (`total_withdrawal_income`/`retirement_taxes_paid`/`net_retirement_income`) — implement that
   first if it hasn't landed, since §4 of this document reads `net_retirement_income` directly.
2. Add `project_no_income_no_expense_baseline` to `modules/projection.py` (§2.5), plus
   `net_present_value_of_field` (§3.1) and `average_annual_net_retirement_income` (§4b), plus the
   one-line `portfolio_value_at_start_of_year` row addition (§4a). Export `_portfolio_value` (drop
   the underscore) for the UI's `today_portfolio_value` computation (§3.2).
3. Replace the "NPV of future profit" `st.metric` (lines 849-868) with the new "Adjusted wealth"
   metric (§3.2).
4. Add the two new per-scenario stat groups (§5).
5. Add the second series to the portfolio-value chart (§5.1), and ask the user whether to relocate
   it out of the expander.
6. Add the tests in §6.
7. Update `NEXT.md` when done, same working agreement as before.
