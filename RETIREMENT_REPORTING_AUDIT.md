# Contribution Waterfall Audit + Pre/Post-Retirement Reporting Redesign — spec for Claude Code

**Prepared by:** Claude (Cowork), from a read-only review of the codebase on the user's machine
(`New_Financial/`), 2026-08-13. No files were modified during this review — everything below is
analysis plus a concrete implementation spec for Claude Code to execute against the actual repo,
following `CLAUDE.md`'s existing ground rules (one module/step at a time, pure functions in
`modules/`, Streamlit in `ui/`, tests before "done," ask rather than guess on ambiguity).

**Relationship to `NEXT.md`:** `NEXT.md` currently lists "Step 7 (Module G2 — bracket-aware fill +
RMDs)" as the proposed next step, awaiting sign-off. This request is different work — a bug in the
existing Step 2 waterfall's *visibility* (not its math, see Part 1) plus a reporting/UI redesign for
Steps 3-6's existing withdrawal machinery. Treat this document as the user's explicit direction to
do this work now, ahead of Step 7; update `NEXT.md`'s "Next approved steps" section accordingly when
done, per `CLAUDE.md`'s own instruction not to build ahead without sign-off — this document **is**
that sign-off for this specific scope, not a blanket go-ahead for Module G2.

---

## Part 1 — "Only 401(k) and Roth IRA are getting funded"

### 1.1 What I verified: the waterfall's own math is not losing money

`modules/investing.py::contribution_waterfall` (lines 129-153) is a simple greedy fill over
`DEFAULT_CONTRIBUTION_PRIORITY` — `["w2_401k_to_match", "hsa", "roth_ira", "traditional_ira",
"401k_family_remaining", "taxable"]` — where `"taxable"` always gets `capacities["taxable"] =
float("inf")` (set in `modules/projection.py` line 569). By construction, `sum(filled.values())`
(including `uninvested_surplus`) always equals `investable` exactly — there is no code path where
dollars are silently dropped inside the waterfall itself. I confirmed this by running
`project_multi_year` directly against three synthetic scenarios (script + full output below, run
from a clean checkout with `pytest`'s own fixtures untouched):

- **W-2-only, $250k gross, single, no SE income:** `w2_401k_to_match=15,000`, `roth_ira=0` (MAGI
  phase-out — correct, $250k single is well above the Roth limit), `traditional_ira=7,500` (fills
  because Roth is phased out), `401k_family_remaining=9,500`, **`taxable=68,774`** —
  `uninvested_surplus=0`. Taxable *is* getting funded correctly once 401(k)+IRA capacity is
  exhausted.
- **SE-only, $250k net, single:** `se_401k_employee=24,500`, `se_401k_employer=47,043`,
  `traditional_ira=7,500`, **`taxable=24,391`**, `uninvested_surplus=0`.
- **W-2 $120k, expenses $50k, moderate saver:** every dollar of `investable` is absorbed inside
  401(k)+Roth IRA capacity before ever reaching Traditional IRA/Taxable — `uninvested_surplus=0`,
  but `traditional_ira`/`taxable` genuinely compute to `$0` because there's nothing left, **not**
  because of a bug. (This is expected: 401(k) $23,500 + Roth IRA $7,000 alone is a $30,500 combined
  cap that a moderate saver's actual surplus often doesn't exceed.)

**Conclusion: the waterfall correctly cascades and correctly reports every dollar (`taxable` +
`uninvested_surplus` account for 100% of `investable` in every case tested).** If a specific
scenario in the live app shows `uninvested_surplus > 0` for a year, that is the one place real
money is being reported as un-routed — and it already has its own column in the destination table
(`ui/projection_tab.py` line 994, `"Uninvested surplus": uninvested`). **First thing to check
against the user's actual saved state: does that column show a nonzero number in the years they're
concerned about?** If yes, that's a hard structural finding (something in `priority_order`/
`capacities` needs a new destination or a raised cap); if no, the "only 401(k)/Roth" pattern is one
of the two explanations below, not a math bug.

### 1.2 Most likely real explanation: dollars are allocated but never turned into holdings

`modules/investing.py::create_lots` (lines 208-257), by explicit design, **silently skips any
account type that received contribution dollars but has no configured `target_allocations` entry**
(docstring, lines 220-224: *"An account type with dollars but no configured target allocation
(empty or all-zero weights) contributes nothing here — its dollars are simply not converted into
lots this year... this is a caller-visible gap ... not a silent loss of the underlying money"*).

`target_allocations` is built in `ui/portfolio_tab.py::_render_target_allocation` (lines 628-671)
**one account type at a time**, from a dropdown (`st.selectbox("Account type", options=ACCOUNT_TYPES,
...)`, line 654) — the user has to explicitly select "Taxable" (or "Traditional IRA", or "HSA")
from that dropdown and enter ticker weights for it before any dollars routed there by the waterfall
ever become real portfolio holdings.

**This means it is entirely possible for the "Contribution destinations" dollar table
(`ui/projection_tab.py` lines 974-1006) to correctly show a nonzero `Taxable`/`Traditional IRA`
number for a year, while the Portfolio ledger / holdings breakdown a few sections below it (lines
1103-1134) shows those account types with zero shares — because the dollars were computed but never
converted to lots.** From the user's description ("the table that shows the savings into different
accounts shows only 401(k) and Roth IRA"), this is the single most likely explanation: either (a)
they are looking at the ledger/holdings view rather than the destination-dollar table and it looks
"empty" for those accounts, or (b) they haven't yet configured target allocations for every account
type their portfolio actually uses.

**Action for Claude Code:**
1. Confirm directly against the user's saved state (`saved_states/`, or ask them to reload their
   session) which account types have `st.session_state.target_allocations` entries and which don't.
2. This is a real, already-partially-documented gap (the caption at lines 969-971 mentions it, but
   only inside a collapsed expander, in small print, after the fact) — **raise its visibility**:
   add an explicit warning banner (not just a caption) whenever a year's destination table has a
   nonzero dollar amount in an account type with no configured target allocation, e.g. *"$X was
   allocated to Taxable this year but no ticker weights are configured for that account type on the
   Portfolio tab — this money is not reflected in any portfolio holdings or Total wealth figure."*
   This is a `ui/`-only change (Streamlit conditional + `st.warning`), no `modules/` change needed.
3. Consider (ask the user before implementing — this is a real design choice, not obviously
   correct): should `create_lots` require target allocations for every account type *before* the
   waterfall is allowed to route money there, rather than silently accepting the gap? That would be
   a `modules/investing.py` behavior change and needs sign-off, per `CLAUDE.md` ground rule 7.

### 1.3 Secondary, smaller findings worth a quick check against the live app

- **HSA capacity is hardcoded to `$0`** everywhere (`modules/projection.py` line 565, comment:
  *"no HSA limit source modeled yet"*) — this is an already-signed-off gap per `NEXT.md` ("Confirmed
  acceptable to leave unaddressed (user sign-off, 2026-08-10)"). If the user expects HSA
  contributions to appear, that sign-off may be worth revisiting, but it is not a bug — it's
  working as explicitly agreed.
- **Traditional IRA gets $0 whenever Roth IRA has full room** — this is correct by design (IRS
  Traditional+Roth contributions share one combined annual limit;
  `modules/projection.py` lines 554-561 computes `roth_capacity` first, then
  `traditional_capacity = combined_ira_limit - roth_capacity`), not a bug, but worth explaining to
  the user if they expected both to fill independently.
- **`priority_order` and `draw_order` have no UI editor at all** — always
  `DEFAULT_CONTRIBUTION_PRIORITY`/`DEFAULT_DRAW_ORDER` in practice (already flagged in `NEXT.md`,
  "Remaining known gaps"). If the user wants Taxable prioritized *ahead* of maxing 401(k)-family
  (e.g. for liquidity), that requires a UI editor for `priority_order`, which doesn't exist yet —
  worth asking the user whether this is actually what they want, since "put more into Taxable
  sooner" is a different request from "fix a bug."
- **Recommend adding a regression test**: `tests/test_investing.py` should get an explicit
  reconciliation test asserting `sum(contribution_waterfall(...).values()) == investable` (modulo
  floating point) for a range of `capacities` combinations, including the "everything absorbed
  before taxable" case and the "surplus reaches taxable" case — this documents the exact behavior
  above as a locked-in contract, not just a docstring claim.

---

## Part 2 — Gross/net income conflation across the retirement boundary (confirmed defect)

### 2.1 What "retirement" means here — use `withdrawal_start_date`, not `retirement_date`

The model already has the exact two concepts the user is asking to split on, as **separate**
fields (`ui/sidebar.py` lines 273-383, `modules/gross_income.py::phase_flags_for_year`):

- `retirement_date` — when W-2/SE earned income stops.
- `withdrawal_start_date` — when portfolio withdrawals begin. **This is the date the user means by
  "the retirement date" in this request** ("the retirement date I mean here is the date where I
  start taking withdrawals").

The row-level flag to key everything off is `withdrawing_fraction` (from
`phase_flags_for_year`, exposed per-row implicitly via `row["withdrawal_target"] is not None and
row["withdrawal_target"] > 0` in the current UI, though a more direct boolean should be added — see
§2.4). **Every change below should split pre/post on `withdrawal_start_date`/`withdrawing_fraction >
0`, not on `retirement_date`** — someone who stops working at 55 but doesn't start withdrawing until
62 should still see pre-retirement-style gross/net/expense/profit reporting for ages 55-61.

### 2.2 The defect, confirmed by running the code

`modules/projection.py` reuses `gross_income`/`net_income`/`profit` for both phases. Pre-retirement
they mean the ordinary IRS constructs (W-2 + SE + break income, minus tax, minus 401(k)-family
deferrals). Once `withdrawing_fraction > 0`, the **same fields** get overwritten by the retirement
withdrawal block (lines 748-756): `gross_income` becomes W-2/SE (now $0) + investment income +
`withdrawal_taxable_retirement_distribution` + realized gains; `net_income` becomes that minus tax.

I ran `project_multi_year` against a 20-year W-2 accumulation → retirement scenario with an active
portfolio (script below). Results, years around and after `withdrawal_start_date` = 2046:

| Year | gross_income | net_income | profit | portfolio_value | withdrawal_target | withdrawal_sale_proceeds | withdrawal_taxable_retirement_distribution |
|---|---|---|---|---|---|---|---|
| 2045 (working) | 120,000 | 66,745 | 16,745 | 1,692,440 | 0 | 0 | 0 |
| 2046 (first withdrawal yr) | 41,806 | 38,132 | -11,868 | 1,624,743 | 67,698 | 67,698 | 41,477 |
| 2047 | 64,990 | 57,244 | 7,244 | 1,559,753 | 64,990 | 64,990 | 64,990 |

Two distinct problems, both real:

1. **Label conflation.** `gross_income`/`net_income` silently switch meaning at the retirement
   boundary — pre-2046 they're IRS gross/net earned income; from 2046 on they're actually "taxable
   portion of this year's withdrawal, and that minus tax." Same field name, same chart line
   (`ui/projection_tab.py::_gross_net_chart`, lines 104-141, plotted across the *entire* horizon with
   no boundary marker or split), two unrelated meanings. This is exactly what the user flagged.

2. **`net_income` *understates* actual spendable retirement income, materially.** In 2046,
   `withdrawal_sale_proceeds = 67,698` (real cash raised across every account sold, including Roth)
   but `withdrawal_taxable_retirement_distribution = 41,477` (only the Traditional-account portion,
   which is what actually enters `gross_income`/`net_income`). Any Roth withdrawal principal and any
   return-of-basis portion of a Taxable-account sale is **completely invisible** in `net_income` —
   real, spendable cash that never shows up anywhere in the current reporting. `net_income` during
   retirement is not "what the retiree actually has to spend"; it's closer to "the taxable-income
   component of what they withdrew." This needs a genuinely new figure, not a relabeling of the
   existing one — see §2.3.

**Reproduction script** (paste into a scratch file at the project root with `.venv` active, or hand
this to a test — it needs `data/asset_classes.json`, `data/tax_brackets.json`, and a `modules/`
`__init__.py` if one doesn't already exist):

```python
import sys, json
from datetime import date
sys.path.insert(0, ".")
from modules.tax import load_bracket_table
from modules.portfolio import build_universe
from modules.projection import project_multi_year

bracket_table = load_bracket_table("data/tax_brackets.json")
current_date, horizon_date, retirement_date = date(2026, 1, 1), date(2066, 1, 1), date(2046, 1, 1)
birth_date = date(1986, 1, 1)
flat = lambda v: {"start_value": v, "end_value": v, "midpoint_years": 1, "steepness": 0.0001}

income_inputs = {
    "current_year_already_earned_w2": 0.0, "current_year_already_earned_se": 0.0,
    "current_year_yet_to_earn_w2": 120000.0, "current_year_yet_to_earn_se": 0.0,
    "w2_curve": flat(120000.0), "se_curve": flat(0.0), "breaks": [],
}
expense_inputs = {
    "current_year_already_incurred_expense": 0.0, "current_year_yet_to_incur_expense": 50000.0,
    "expense_curve": flat(50000.0),
}
phase_dates = {"savings_stop_date": retirement_date, "withdrawal_start_date": retirement_date,
                "ss_claim_date": date(2050, 1, 1)}

universe = build_universe(json.load(open("data/asset_classes.json")), {})
initial_lots = [{"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 5000,
                  "basis_per_share": 100.0, "year_acquired": 2020}]
target_allocations = {"Traditional 401(k)": {"VTI": 1.0}, "Taxable": {"VTI": 1.0},
                       "Roth IRA": {"VTI": 1.0}, "Traditional IRA": {"VTI": 1.0}}

rows = project_multi_year(
    income_inputs, expense_inputs, current_date, horizon_date, retirement_date, birth_date,
    filing_status="single", bracket_table=bracket_table, use_contribution_waterfall=True,
    phase_dates=phase_dates, universe=universe, initial_lots=initial_lots,
    initial_prices_by_ticker={"VTI": 200.0}, inflation_rate=0.0, target_allocations=target_allocations,
)
for r in rows:
    if r["year"] in (2045, 2046, 2047):
        print(r["year"], r["gross_income"], r["net_income"], r["profit"],
              r["withdrawal_target"], r["withdrawal_sale_proceeds"],
              r["withdrawal_taxable_retirement_distribution"])
```

### 2.3 Spec: what the new figures should be

Define, precisely, so Claude Code isn't guessing:

- **`total_withdrawal_income`** (new field, retirement-phase only) = `withdrawal_dividends_applied +
  withdrawal_sale_proceeds` — the **total cash actually raised from the portfolio** this year for
  spending, across every account type (Traditional, Roth, Taxable, HSA). This is "gross withdrawal"
  in the user's request — it is *not* the same as `gross_income` (which only ever captured the
  taxable slice). Already-computed inputs exist for this (`row["withdrawal_dividends_applied"]`,
  `row["withdrawal_sale_proceeds"]`) — this is a derived sum, no new modeling needed.
- **`retirement_taxes_paid`** (new field, retirement-phase only) = `row["total_tax"]` for that year.
  During pure retirement (no W-2/SE/break income), `total_tax` is already fully attributable to
  investment income + realized gains + Traditional distributions, so no new tax computation is
  needed — just a clearer name in this context. **Caveat to flag in code/comments**: once Module F
  (Social Security) lands, `total_tax` will also include tax on SS benefits, and this field's
  "attributable to withdrawals only" framing will need revisiting — leave a comment pointing at
  that, don't silently ignore it.
- **`net_retirement_income`** (new field, retirement-phase only) = `total_withdrawal_income -
  retirement_taxes_paid`. This is the actual "money in hand to spend" figure the user is asking the
  new chart to show — the corrected version of what `net_income` currently, incorrectly, tries to be.

Implement these as new columns computed in `ui/projection_tab.py` (a pure derivation from existing
row fields, no `modules/` change required) unless Claude Code judges it cleaner to add them
directly onto each row in `modules/projection.py::project_multi_year` (arguably the more correct
home, since `modules/` is where "what does this number mean" should live per `CLAUDE.md` ground rule
4 — Claude Code's call, but keep them out of Streamlit-only code if there's any chance a future
consumer other than this one UI tab needs them, e.g. an eventual Dashboard tab).

### 2.4 Spec: chart and table changes

**Add an explicit `is_withdrawal_year` (or reuse `withdrawing_fraction > 0`) split point** wherever
rows are partitioned below — don't infer it from `withdrawal_target > 0` as a proxy (that's `None`
pre-retirement and `0.0` in a withdrawal-phase year with no actual draw, which are different things
worth keeping distinct).

1. **Retire the combined "Gross vs. net income" chart at the withdrawal boundary.**
   `_gross_net_chart` (`ui/projection_tab.py` lines 104-141) should only plot rows through the last
   pre-withdrawal year. Two reasonable ways to do this — ask the user which they'd prefer, don't
   guess: (a) simply stop the line series at `withdrawal_start_date`, leaving the chart shorter than
   the other two (Expenses, Profit) which presumably still run the full horizon, or (b) keep all
   three charts on the same x-domain length but leave gross/net blank (no line) past the boundary.
   Given the user said "discontinue the lines," (a) is the more literal reading — implement that,
   but flag the choice explicitly in the response back to the user.
2. **New "Total retirement income" chart** — a fourth chart (or replaces the gross/net chart's slot
   for the retirement-year range), plotting `total_withdrawal_income` and `net_retirement_income` as
   two lines (mirroring the existing gross/net chart's shaded-band-for-tax visual convention,
   `_gross_net_chart`'s `band` layer, lines 121-123) for withdrawal-phase years only.
3. **New pre-retirement wealth table** — columns exactly as requested: `Year`, `Total wealth`
   (= `portfolio_value`), `Gross income`, `Net income`, `Expenses` (= `gross_expense`), `Profit`.
   Filter to rows before `withdrawal_start_date` (`withdrawing_fraction == 0` for the whole year, or
   however Claude Code decides to handle a partial first withdrawal year — flag that edge case to
   the user rather than silently picking a convention). This effectively replaces/extends the
   existing combined `table_df` (lines 888-938) for the pre-retirement range, with `Total wealth`
   added — that column doesn't exist on the current table at all despite `portfolio_value` already
   being computed per row.
4. **New retirement income table** — columns exactly as requested: `Year`, `Gross withdrawal`
   (= `total_withdrawal_income`), `Net income` (= `net_retirement_income`), `Taxes paid`
   (= `retirement_taxes_paid`). Filter to `withdrawing_fraction > 0` years. This is a distinct table
   from the existing "Retirement withdrawals — flat-percentage rule" breakdown already in the
   ledger expander (lines 1043-1075) — that one is a mechanics breakdown (target vs. spending need,
   dividends vs. sale proceeds, gain by type); this new one is the summary the user is asking for at
   the top level, not buried in an expander. Consider whether the existing breakdown table becomes
   redundant or should stay as supporting detail underneath the new summary table — ask the user,
   don't delete working functionality unprompted.
5. **The existing combined `table_df` (lines 888-938) needs a decision, not a guess**: does it stay
   as-is for power users who want everything in one place, get removed in favor of the two new
   split tables, or get kept but re-labeled to make clear it spans both phases with two different
   meanings for Gross/Net columns? Ask the user which they want before deleting anything.

### 2.5 Suggested test coverage

- `tests/test_projection.py`: a new test asserting that for a portfolio-active, withdrawal-phase
  row, `total_withdrawal_income >= withdrawal_taxable_retirement_distribution` (the new figure must
  be at least as large as the old, understated one) and that it exactly equals
  `withdrawal_dividends_applied + withdrawal_sale_proceeds`.
- A test confirming the Roth-only-withdrawal edge case specifically: construct a scenario where the
  draw order pulls entirely from a Roth account (e.g. `draw_order=["Roth IRA"]`), confirm
  `gross_income`/`net_income` stay at $0 (since Roth distributions are never taxable) while the new
  `total_withdrawal_income`/`net_retirement_income` correctly show the real cash raised — this is
  the exact case that motivated this whole section, so it should be locked in as a regression test.

---

## Summary of concrete next actions for Claude Code

1. Verify against the user's actual saved state whether `uninvested_surplus` is nonzero in the
   years they're concerned about (§1.1) — if yes, that's the real bug to chase; if no, move to #2.
2. Check whether `target_allocations` is configured for every account type the waterfall is routing
   money to (§1.2) — almost certainly the explanation if `uninvested_surplus` is $0 everywhere.
3. Add the destination-table warning banner for unconverted contribution dollars (§1.2, point 2) —
   small, low-risk UI change, ship regardless of what #1/#2 find.
4. Ask the user the open design questions in §2.4 (points 1 and 5) before touching the chart/table
   layout — everything else in Part 2 is unambiguous enough to build directly.
5. Implement `total_withdrawal_income`/`retirement_taxes_paid`/`net_retirement_income` (§2.3), the
   new retirement-income chart and both new tables (§2.4), and the regression tests (§2.5).
6. Update `NEXT.md`'s "Where things stand"/"Next approved steps" sections when done, per the
   existing working agreement — this work is not Module G2/Step 7, so Step 7 remains the next
   *sign-off-pending* step after this.
