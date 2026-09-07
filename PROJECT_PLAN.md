# PROJECT_PLAN.md

## Goal

A Streamlit-based, general-purpose (any US adult) financial planning model. All figures in real
(inflation-adjusted) dollars. Built module by module with a user sign-off gate after each.

The end state projects, year by year, from today through end of life: income, expenses,
saving/investing, portfolio growth, Social Security, retirement withdrawals, tax liability, and
consumption smoothing.

## Architecture principles

These follow from the end state, and constrain every module built before it.

1. **Model flows, not snapshots.** Every quantity that will eventually vary by year (value, income,
   tax, contribution) must be expressible as a function of `t`. A number that only makes sense at
   `t=0` is a display convenience, not a model primitive, and must be labeled as such.
2. **Tax is computed on realized flows, never on balances.** A tax rate applied to a portfolio
   balance is a liquidation estimate, not a model of tax. See "Tax methodology" below.
3. **One canonical schema per entity** (position, account, asset class, cash flow), defined once in
   a module, constructed once, never re-shaped ad hoc in UI code.
4. **Pure calculation modules.** `modules/*.py` take plain data in and return plain data out — no
   Streamlit, no file I/O, no network. The two deliberate exceptions (`market_data.py`,
   `save_state.py`) are I/O-only and contain no calculation.
5. **Assumptions live in `data/` or in user input, never inline in logic.** Tax brackets, bend
   points, asset-class returns, contribution limits.
6. **UI is a thin render layer over pure functions.** The transformation from widget state to
   calculation input is itself a pure, tested function — not inline loops in `app.py`.

## Tax methodology (target state)

The current flat pre-tax rate and flat capital gains rate are **placeholders for display only**.
They will be replaced, not extended. Target design:

- `modules/tax.py`, pure, with the signature shape
  `federal_tax(taxable_income_components, filing_status, year) -> liability`.
- Bracket thresholds, standard deduction, LTCG 0/15/20 thresholds, FICA rates and wage base, and
  NIIT thresholds in `data/tax_*.json`, keyed by tax year.
- Bracket thresholds are **nominal** and indexed forward by the inflation assumption; the resulting
  liability is deflated back to real dollars before entering the model. This is the one sanctioned
  nominal intermediate.
- Taxable income is assembled from flows in a given year: ordinary income (wages, SE income,
  Traditional withdrawals, RMDs, Roth conversions, taxable interest), preferential-rate income
  (qualified dividends, long-term capital gains realized), and Social Security subject to the
  provisional-income formula.
- Realized capital gain comes from the withdrawal module telling the portfolio module *what was
  sold*, against tracked cost basis — not from marking the whole portfolio to a gain.
- State tax is a separate, optional, pluggable component with the same interface.

Until `modules/tax.py` exists, the flat rates stay but are relabeled in the UI as an estimate of
liquidation value today, so no downstream module accidentally treats them as the tax model.

**Status (2026-08-08): `modules/tax.py` now exists**, built from an external build spec the user
supplied (a Downloads-folder doc plus a follow-up addendum, neither checked into this repo — see
`NEXT.md` for a full summary of both), covering federal ordinary/LTCG tax, NIIT, Additional Medicare
Tax, CA income tax + Mental Health Services Tax, FICA/SECA, CASDI, SE 401(k) max, a simplified QBI
phase-in, and Social Security taxability — single-year, filing-status-parametrized
(single/MFJ/HoH), fully unit-tested (39 tests, 7 the build spec's own fixtures, cross-checked
against an independently-written second implementation). Includes a dedicated
`taxable_retirement_withdrawal` parameter (added in the addendum pass) that's ordinary income for
federal/CA/SS-provisional-income purposes but correctly excluded from the NIIT base (IRC §1411) and
the Additional Medicare Tax base — the addendum's fixture 7 regression-tests this exclusion.

Given a Streamlit "Tax" tab as of this same date (see Step 4 below) for single-input, single-year
QC against external calculators.

**Status (2026-08-08, later same day): 401(k)/solo 401(k) contribution caps added**, per
`401k_contribution_module_spec.md` (Downloads-folder doc, not checked into this repo — see
NEXT.md), wired directly into `compute_taxes` per that spec's own explicit sequencing instruction
(implemented before the gross-income projection module described in its sibling
`gross_income_module_spec.md`, which was read for context only this pass, not built). Key points:
- The IRC §402(g) combined employee-deferral limit (`max_combined_employee_deferral`) is genuinely
  shared between a W-2 job's 401(k) (`pretax_401k` + `roth_401k`) and a self-employed solo 401(k)'s
  employee deferral (`se_solo_employee_deferral`) — `allocate_employee_deferral` splits it with a
  configurable priority (default "w2-first"). The separate §415(c) per-plan limit
  (`max_se_employer_contribution`) bounds the SE employer/profit-sharing contribution
  (`se_solo_employer_contribution`, renamed from the old combined `se_401k_contribution` field) —
  critically, that employer contribution does NOT touch the shared §402(g) pool.
- **A real inconsistency in the spec was found, flagged, and later resolved by the user in a
  revised spec**: `max_se_employer_contribution`'s described behavior requires `age_at_year_end`
  (catch-up affects the §415(c) ceiling), but the first draft's declared function signature omitted
  that parameter entirely. Implemented per the described behavior in that first pass, flagged in
  this file and NEXT.md; a follow-up spec then added `age_at_year_end` back as the function's 2nd
  parameter, which is the signature `max_se_employer_contribution` now has (2026-08-08, third pass
  this date).
- **A real pre-existing bug was found and fixed while touching this code**: solo 401(k)
  contributions (both sides) were only ever being subtracted from the QBI base, never from AGI/CA
  taxable income directly — but they're their own above-the-line deduction (Schedule 1), separate
  from and in addition to their QBI effect. Fixed for both federal and CA (CA conforms to this
  deduction, unlike QBI); regression-tested.
- `data/tax_brackets.json`'s `k401_limits` gained `annual_additions_limit` (§415(c): $70,000 for
  2025, $72,000 for 2026 — sourced from IRS Notice 2025-67, cross-checked against 2 sources) and
  had its other keys renamed for clarity (`elective_deferral_limit`, `catchup_50_to_59`,
  `catchup_60_to_63`).
- 24 new tests (63 total in test_tax.py, 136 project-wide), covering the contribution-spec's own
  §6 required cases (catch-up tiers — both standard and enhanced explicitly, per the revised spec's
  own instruction — employer-vs-employee-deferral independence, comp caps, missing-year errors)
  plus the AGI-deduction bug fix. Verified in-browser: Tax tab's new fields (SE employee/employer
  deferral, age-at-year-end pre-filled from Demographics, deferral priority) render and validate
  correctly, no exceptions.

**Status (2026-08-08, 4th pass this date): `max_se_employer_contribution` missing-constraint bug
fixed.** The user reported a real, live number from the running app: at $16,000 net SE profit
(≈$14,869.64 net SE earnings) with SE employee deferral maxed at that full figure, the app returned
an employer-contribution max of ~$2,973.93 (the unconstrained 20% formula) instead of the correct
$0 — total modeled SE-plan contributions exceeded actual net income. Root cause: the function only
ever capped against the 20%-of-earnings formula and the §415(c) annual-additions limit, never
against net SE earnings themselves (the ordinary "100% of compensation" rule) minus whatever
employee deferral already used. Fixed by adding a third constraint,
`remaining_comp_room = net_se_earnings_for_retirement - se_employee_deferral_used`, and taking the
min across all three. 3 new tests (66 in test_tax.py, 138 project-wide): the exact reported
$16,000/$14,869.64/$0 scenario, and a property test asserting
`se_employee_deferral_used + max_se_employer_contribution(...) <= net_se_earnings_for_retirement`
across a range of earnings/deferral combinations.

**Status (2026-08-09): `ordinary_break_income` parameter added.** A new optional `compute_taxes`
parameter (default `0.0`, fully backward-compatible with every existing call site) for income from
`modules/gross_income.py`'s income breaks (unemployment, a stipend, sabbatical) — ordinary income
for federal AGI / CA taxable income / SS provisional income, with the same NIIT/Additional Medicare
Tax exclusion treatment as `taxable_retirement_withdrawal`, and deliberately not folded into either
of those two existing fields (see NEXT.md for why). 2 new tests (67 in test_tax.py).

**Status (2026-08-09, later same day): forward-year bracket policy resolved — held flat in real
terms.** The modeling-policy question flagged below (which of two policies to use for years beyond
the last configured bracket year) is now decided, at the user's explicit direction: **hold all
thresholds flat in real terms** (not the alternative — eroding the two non-inflation-indexed
threshold groups by a modeled real-wage-growth/CPI rate). Rationale: this entire model runs in real
dollars end to end, and a real tax system's own brackets are themselves inflation-indexed each
year — so "this year's brackets, unchanged" *is* what holding a real-dollar model's tax law
constant actually means for a future year, not an approximation of it.

This is implemented in the **caller**, not in `compute_taxes` itself: `compute_taxes` still raises
`ValueError` for a `tax_year` with no `data/tax_brackets.json` entry, unchanged (correct for its
role as a strict single-year QC function — see the Tax tab, Step 4). `modules/projection.py`'s
`project_multi_year` (Module B, see Step 5 below) is the one that resolves a real calendar year to
the bracket year whose data actually gets used (`_bracket_year_for`: the latest configured year at
or before the requested one), so every projected year — however far past 2026 — now gets real tax
figures instead of `tax_available=False`.

**Status (2026-08-10): IRA limits + fixed-priority computed-max contributions added.** Built from a
user-supplied `optimization.md` (Downloads-folder doc, not checked into this repo — see NEXT.md).
`modules/tax.py` gained `max_ira_contribution_limit`, `roth_ira_magi`, `roth_ira_phase_out_max` (IRS
Pub. 590-A Worksheet 2-2), `compute_max_401k_contributions` (fixed W-2 → SE-employee → SE-employer
priority — the spec's own literal ordering, employer before employee, was corrected to avoid
recreating the already-fixed §415(c)/100%-comp bug; see NEXT.md), `resolve_contributions_for_year`
(never-raising clamp for planning-tool use, contrast with `compute_taxes`'s own strict validation),
`net_se_earnings_for_retirement` (now standalone, plus a new `compute_taxes` return field), and four
`check_*` warning functions (§402(g), §415(c) — corrected to be per-plan, not combined across a W-2
plan and an SE solo plan, per-spec — Roth IRA phase-out, and the Traditional+Roth §219(b) combined
cap). `data/tax_brackets.json` gained an `ira_limits` block for 2025/2026. `modules/projection.py`'s
`project_multi_year` now actually applies a `contributions_by_year` input to the tax calculation
(previously always $0, per the module's own prior documented scope note). Two real, deliberate gaps
remain, not silently absorbed: Traditional IRA deductibility isn't modeled (an entry never reduces
AGI), and MFS filing status remains entirely out of scope (unchanged from the original tax module).

Two real gaps remain in the tax module overall, both deliberate and documented, not silent:
- **Not wired into the Portfolio tab's placeholder liquidation-value rates.** That tab doesn't yet
  track real per-lot cost basis (see Module D), so `compute_taxes`'s `ltcg` input would be fed from
  a placeholder — deferred until basis tracking is real, so numbers don't look complete when they
  aren't.
- **`compute_taxes` itself has no yearly-loop awareness** (by design — see above); the Tax tab
  remains a manual single-year QC surface, distinct from the Projection tab's own multi-year caller.

## Build order

Each step ends with: passing unit tests, a runnable Streamlit page, and explicit user sign-off
before the next step starts.

### Step 0 — Audit remediation ✅ done, partially (2026-08-07)
Fix the structural issues found in the audit of the Module 1 build before more modules stack on
top. Findings and the remediation list are in `NEXT.md`. No new user-facing features.

**Status note**: findings 1, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15 done. Findings 2, 3, 7, 8
deliberately deferred — see `NEXT.md` for why each was left for later. Not blocking, since none of
the deferred items were on Step 1/2's own critical path.

### Step 1 — Module 1 (Portfolio), amended ✅ done (2026-08-07), further amended (2026-08-08)
Portfolio inputs and current value. Already built; being amended per below.

**ETF universe builder** (new, at the top of the Portfolio tab). Replaces the fixed ticker list in
`data/etf_universe.json`:

- User types in a ticker, its **asset class**, **expense ratio**, **dividend rate**, and whether
  its dividends are **qualified or ordinary** — all four entered directly, at add time (2026-08-08
  amendment; expense ratio was originally pulled live from Yahoo Finance, but that data proved
  unreliable for several mutual funds — see `NEXT.md` — so it, and dividend rate/type, are now
  always user-entered, editable afterward in a "Ticker details" section without needing to remove
  and re-add the ticker). Only **price** still comes from a live Yahoo Finance lookup.
- The asset class is chosen from a canonical dropdown, whose options and default expected
  **nominal return** live in `data/asset_classes.json`. Returns are editable in the UI; the edited
  value applies to every ticker of that class.
- The resulting ticker list is the option set for the holding dropdowns in each account section
  below. No ticker can be held that isn't in the builder.
- The user's ticker→asset-class/expense-ratio/dividend-info assignment is personal data: it lives
  in session state and saved states (`ticker_universe`), not in `data/`. Dividend rate/type are
  captured for visibility only so far — not yet consumed by any calculation (portfolio value,
  return, or the Tax tab's dividend inputs); wiring them in is future work, not done silently.

**Accounts and holdings** (existing): account name + tax-treatment type; per account, holdings of
ticker / shares / cost basis per share.

**Outputs** (existing): portfolio value by account and asset class, blended expense ratio, blended
real expected return, and a labeled liquidation-value estimate.

Deliverables: `modules/portfolio.py`, `data/asset_classes.json`, Streamlit Portfolio tab.

### Step 2 — Module 2: Demographics ✅ done (2026-08-07)
A new Streamlit tab. Plain inputs for now; no calculation engine beyond derived durations.

Inputs: date of birth, planned retirement date, health status, and the age at which
saving/investing stops (may differ from retirement).

Derived: current age, months and years to retirement, months and years to end of accumulation.

Moves `age` and `retirement_date` off the Portfolio tab's macro inputs, where they currently sit.

Deliverable: `modules/demographics.py` (pure date/duration functions), Demographics tab.

### Step 3 — Module 3: Macro assumptions
Inflation and any other economy-wide assumptions, moved out of the Portfolio tab into their own
tab so every module reads them from one place.

### Step 4 — Tax tab (QC surface, not Module E) ✅ done (2026-08-08)
Built out of sequence, at the user's explicit direction, ahead of Step 3 and the deferred audit
items. A single-year manual QC tool for `modules/tax.py`, deliberately *not* the yearly-loop
integration described in the "Tax methodology" section above (which needs Module B and Module G2
to exist first, per the build addendum's own original deferral note — superseded here only because
the user asked for this specific QC surface now, not because that reasoning stopped applying to a
real yearly-loop integration).

- One plain numeric input per `compute_taxes` income source (W-2 gross, pretax 401(k)/health-
  dental, SE net profit, SE 401(k) contribution, taxable retirement withdrawal, LTCG, qualified/
  ordinary dividends, interest, Social Security benefit), plus filing status and tax year.
- `taxable_retirement_withdrawal` and `ss_benefit_gross` are plain inputs here on purpose, even
  though they won't stay that way — they'll eventually be computed by Module G2 (withdrawal
  strategy) and Module F (Social Security/AIME) respectively, not typed in. This tab exists to
  validate `compute_taxes` in isolation before those modules exist, not to anticipate their UI.
- Every `TaxResult` field displayed as its own labeled line (not just summary metrics), so the
  numbers can be compared line-by-line against an external calculator (SmartAsset, NerdWallet, a
  real tax return) for the cross-check `modules/tax.py`'s own test suite couldn't do for lack of
  API access — see `tests/test_tax.py`'s docstring.

Deliverable: `ui/tax_tab.py`, a new "Tax" tab in `app.py`.

### Step 5 — Module B: Income & expense projection + Projection tab ✅ done (2026-08-09)
Built from a user-supplied `gross_income_module_spec.md` (not checked into this repo — see
`NEXT.md`), plus an explicit request for a multi-year UI tab, independent of the Tax tab.

- `modules/gross_income.py`: year-by-year gross W2/SE income and expense projection from today
  through retirement. Normalized S-curve growth for every future year (including the partial
  retirement year); the current year uses an actual/estimate split instead (already-earned income
  bypasses day-weighting entirely). Income breaks (date-ranged periods with a different annualized
  rate, first-listed-wins on overlap) are their own tracked stream, not blended into W2/SE.
- `modules/projection.py`: adapter running each projected year through `compute_taxes`
  (`ordinary_break_income` — see above — carries the break income). A year past the last
  configured bracket year holds that year's brackets flat in real terms (`_bracket_year_for` — see
  "Tax methodology" above); `tax_available=False` is now reserved for the degenerate case of an
  entirely empty `bracket_table`. Each row also gets `profit = net_income - gross_expense`, the
  year's real-dollar cash-flow surplus/deficit after tax and expenses.
- `ui/projection_tab.py`: new "Projection" tab, fully independent of the Tax tab's own session
  state per explicit user request (the Tax tab stays a self-contained single-year tool). Wages/
  Expenses/Income breaks input sections; results as an `st.line_chart` (gross income, net income,
  and profit, all by years from today, with explicit `x`/`y`/`x_label`/`y_label`/`color`) and a
  table. `current_date` is always `date.today()` — never a user input, per "the model starts on
  today's date."
- 401(k) contributions, investment income, and Social Security are all $0 in every projected year
  in this pass — those come from modules (D/F/G2) that don't exist yet. Flagged in
  `modules/projection.py`'s own docstring, not silently assumed away.
- 38 new tests (30 in test_gross_income.py, 8 in test_projection.py), 178 project-wide.

**Status (2026-08-09, later same day): NPV of future profit flows added.** `modules/projection.py`
gained `net_present_value(rows, discount_rate)` — discounts every row's `profit` by
`(1 + discount_rate) ** years_from_now` (the current, possibly-partial year counts at full value,
standard NPV convention; only genuinely future years are discounted) and sums. `discount_rate` is
meant to be the Portfolio tab's own blended real expected return across the user's actual holdings
(`modules.portfolio.summarize_holdings`'s `blended_real_return`) — the rate the money would
otherwise earn if invested instead. That value is now written to
`st.session_state.portfolio_blended_real_return` every rerun by `ui/portfolio_tab.py` (runs before
Projection regardless of which tab is visually active — Streamlit's `st.tabs` renders every tab
body every rerun, only display is toggled by CSS), and the Projection tab reads it for the new "NPV
of future profit" `st.metric`. Entirely real-dollar throughout — no separate inflation deflation
needed, since both the cash flows and the discount rate already are. 7 new tests in
`test_projection.py`. Verified in-browser end to end with the user's own real 9-account portfolio
(read-only Load, never re-saved): blended real return 4.32% -> NPV $594,720, with no discount-rate
caption shown (that only appears at exactly 0%, the empty-portfolio case, verified separately).

### Later modules
Unchanged in intent; detail to be written when each is approached. **`MODEL_WIRING.md` (checked
into this repo, 2026-08-10) is now the detailed wiring spec for Module D and the lifecycle
extension underlying Modules E/F/G2** — read it alongside this file for that block of work; it
doesn't replace anything here, it fills in the placeholders below with a concrete design.

- Module C — Expenses evolution (mostly covered by `project_expenses` above; revisit once Module D
  needs richer expense categories than one blended curve)
- Module D — Saving & investing (contribution allocation, rebalancing, cost-basis tracking) — see
  `MODEL_WIRING.md` §2.3, §3-§8. **§1-§2, Step 1b, Step 2, Step 3, Step 4, Step 5, and Step 6 are
  done (2026-08-10) — MODEL_WIRING.md §9's entire Stage-1 build order (rows 1-6) is now complete,
  and the model runs genuinely end to end, today through end of life**: the lifecycle extension
  (phase dates, projection horizon to age 100), employer 401(k) match, ticker `interest` income
  type, `modules/investing.py`'s contribution waterfall + lot ledger (§3, now the default way
  contributions are decided via `project_multi_year`'s `use_contribution_waterfall`), target
  allocation by account type (Portfolio tab), the per-asset roll-forward (§4: each holding
  compounds at its OWN asset-class return net of its OWN expense ratio, never blended, pays
  dividends/interest classified by ticker and routed to `compute_taxes` by account type, and
  reinvests — closed the "$0 investment income every year" gap flagged since the original
  gross-income-module session), Stage-1 sales/dissaving (§5.1-§5.2: a pre-retirement `profit < 0`
  year sells assets via a configurable draw order — default Taxable → Traditional 401(k) →
  Traditional IRA → Roth 401(k) → Roth IRA, HSA excluded unless opted in — to cover the shortfall,
  with realized gains reaching `compute_taxes` correctly by account type; a real gap closed in
  `compute_taxes` itself, `short_term_gains` — ordinary-rate but NIIT-included, a combination no
  existing parameter had), rebalancing (§7: tax-advantaged accounts — Traditional/Roth 401(k)/IRA,
  HSA — corrected back to their target weights every year via net-zero-cash, zero-tax trades;
  Taxable is never touched, real permanent drift there accepted by design), and — new this pass —
  the flat-percentage retirement-withdrawal rule (§2.3/§5.1 point 2): from `withdrawal_start_date`,
  `modules/investing.py`'s `annual_withdrawal_target` dispatch computes each year's draw (default
  4% of starting portfolio value), reusing Step 4's own `draw_order_fill` sale machinery; the
  withdrawal target and that year's actual spending need are reported side by side as two different
  numbers, never silently reconciled (`funding_gap`) — a real post-retirement shortfall now shows
  up as genuine negative profit rather than being an unfunded, undetected gap. Alongside Step 2, a
  real pre-existing bug in `net_income`/`profit` was found and fixed: 401(k)-family contributions
  previously showed up as pure upside (the tax benefit) with the actual diverted cash never
  subtracted — see `NEXT.md` for the full reasoning, it changes historical NPV numbers for anyone
  with contributions already entered. §5.3 Stage 2 (bracket-aware fill + RMDs, Module G2) and §8
  (allocation optimization, explicitly future work) are not yet started — see `NEXT.md` for the
  exact boundary and `MODEL_WIRING.md` §9's own build order/gates for what's next (Step 7).
- Module E — Deterministic wealth projection
- Module F — Historical earnings record, Social Security / AIME / bend points ✅ done (2026-08-31):
  new "Social Security" tab (historical earnings entry + full QC breakdown), `modules/
  social_security.py` (AIME, bend-point PIA, full retirement age, claiming-age adjustment — see
  NEXT.md for the full build notes and live-verified worked examples), `data/ss_bend_points.json`
  (SSA-sourced). Wired into `project_multi_year`'s real tax computation (previously hardcoded
  `ss_benefit_gross=0.0` at every call site) — benefit taxation itself was already correct and
  needed no changes. No spousal/survivor benefits; historical years capped at today's real wage
  base rather than each year's own historical one (both confirmed acceptable simplifications).
- Module G1 — Health index, projected lifespan, retirement funding need ✅ done (2026-08-30): see
  `modules/health.py` and NEXT.md — a separate, informational expected/percentile-lifespan stat,
  not a replacement for the funding-horizon `planning_horizon_age` input (confirmed with the user).
- Module G2 — Retirement withdrawal strategies and consumption smoothing — bracket-aware fill +
  RMDs (Step 7 Part 1) and dynamic total-sizing (`"target_net_spending"`, Step 7 Part 2 / "G2iii")
  both done — see `MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md` and NEXT.md. Not yet done: using
  Module G1's `expected_lifespan_age` for consumption-smoothing horizon length (named as a future
  integration point in `MODULE_G1_HEALTH_LIFESPAN.md`, not built).

## Status

See `NEXT.md` for the current step and what is approved to build next.
