# MODEL_WIRING.md — Main wiring spec: lifecycle phases, asset purchasing, and the yearly ledger

Status: **§1-§2 (Step 1), Step 1b (employer match + ticker `interest` income type), Step 2
(`modules/investing.py` — the contribution waterfall + lot ledger, §3), Step 3 (§4 — per-asset
roll-forward + dividends/interest actually reaching `compute_taxes`), Step 4 (§5.1-§5.2 Stage 1 —
sales/dissaving with a fixed, configurable draw order), Step 5 (§7 — rebalancing within
tax-advantaged accounts), and Step 6 (§2.3/§5.1 point 2 — the flat-percentage retirement-withdrawal
rule) built (2026-08-10) — the model now runs genuinely end to end, today through end of life; §5.3
Stage 2 and §8 (bracket-aware/RMD withdrawals, allocation optimization) still specification, not yet
built.** See NEXT.md for the build summaries and the current session's own status. §9's build order
and per-step sign-off gates still govern everything after Step 6 — this document defines the target
design for that remaining work.

Read alongside `CLAUDE.md` (ground rules) and `PROJECT_PLAN.md` (module map, tax methodology). This
document supersedes nothing; it fills in the "Module D / Module E" placeholders those files leave
open, and adds the post-retirement extension the model currently stops short of.

Design decisions confirmed with the user before writing (2026-08-10):

| Question | Decision |
|---|---|
| Time step | **Annual.** One step per calendar year, matching the existing yearly rows. |
| Dividend reinvestment | **Back into the paying asset** (classic DRIP), at that year's price. |
| Rebalancing method | **Contributions first, then sell/buy** to close remaining drift. |
| Withdrawal draw order | **Tax-bracket-aware fill** (complex — phase it, see §5.3). |

Follow-up resolutions to §10's open questions (2026-08-10, second pass — all five now closed):

| Question | Decision |
|---|---|
| Employer match | **Modeled.** New input on the Income & Expenses tab, **default 0%** (§3.2). |
| End-of-life horizon | **Run to age 100** until Module G1 exists (§1.2). |
| Interest income | **Per-ticker income type gains a third option, "Interest"** — SPAXX/SGOV-style cash and money-market funds are tickers, not a separate cash balance (§4.2). |
| Traditional IRA deductibility | **Assume non-deductible**, with a visible note in the app (§3.2). |
| Taxable-account drift | **Contribution-directed correction is sufficient.** Tax-loss harvesting is a future module (§7). |

---

## 0. Scope of this document

Three connected pieces of work, in build order:

1. **§1–2 — Lifecycle extension.** Extend the projection past `retirement_date` to end of life, and
   introduce four independent phase dates that currently don't exist as model primitives.
2. **§3–6 — The asset engine.** A contribution/purchase/sale/rebalance/dividend engine that keeps a
   full share-level ledger per account, feeds realized flows back into the tax calculation, and
   rolls balances forward at each asset's own return rather than a blended one.
3. **§8 — Future: allocation optimization.** Asset-location optimization by account type. Specified
   here so §3's data structures don't foreclose it, but **not** part of this build.

Everything is in **real (today's) dollars**, per `CLAUDE.md` ground rule 1. The one sanctioned
nominal intermediate remains tax-bracket lookup, already handled by `modules/projection.py`'s
`_bracket_year_for` (hold brackets flat in real terms — see `PROJECT_PLAN.md`).

---

## 1. Lifecycle extension: from "through retirement" to "through end of life"

### 1.1 What exists today

`modules/gross_income.py`'s `build_year_windows(current_date, retirement_date)` generates rows from
`current_date` to `retirement_date` and stops. `modules/projection.py`'s `project_multi_year` has
the same horizon. Everything after retirement is currently unmodeled.

### 1.2 What changes

The projection horizon becomes `current_date → end_of_life_date`, where `end_of_life_date` will
eventually come from Module G1 (health index / projected lifespan).

**Until Module G1 exists, the model runs to age 100** — `end_of_life_date = date_at_age(birth_date,
100)`, using `modules/demographics.py`'s existing `date_at_age`. This is a deliberate planning
horizon, not a life-expectancy estimate: it is well past any national-average longevity figure, so
a plan that funds to 100 is conservative by construction. Label it that way in the UI ("planning
horizon: age 100") so it isn't mistaken for a mortality assumption.

The horizon age is a constant (`DEFAULT_PLANNING_HORIZON_AGE = 100`) with a Demographics-tab
override, not a hardcoded literal buried in the loop — Module G1 replaces the *source* of this
number, not the plumbing around it.

`build_year_windows` gains an explicit horizon parameter rather than deriving it from
`retirement_date`. Its existing partial-year logic still applies, but now to **any** phase-boundary
year, not just the retirement year — see §2.2.

### 1.3 Income streams by phase

Post-retirement years have no W-2 or SE income by default, but the row schema does not change; the
same fields simply go to zero and other fields become non-zero:

| Stream | Pre-retirement | Post-retirement |
|---|---|---|
| `gross_w2`, `gross_se` | from `project_gross_income` | 0.0 (unless the user models phased work) |
| `gross_ordinary_break_income` | income breaks | unchanged mechanism (e.g. a pension) |
| `taxable_retirement_withdrawal` | 0.0 | from §5, the withdrawal engine |
| `ss_benefit_gross` | 0.0 | from §2.4 / Module F |
| `qualified_dividends`, `ordinary_dividends`, `interest_income` | from §4, the portfolio | same |
| `ltcg` | from §4, realized sales only | same |

Note the implication: **investment income exists in every year, not just retirement years.** That is
a live gap today — `modules/projection.py`'s docstring says these are $0 "until Modules D/F/G2
exist." This spec is what closes it.

---

## 2. The four phase dates

These are four **independent** user inputs on the Demographics tab. The model must not assume any
particular ordering beyond validation warnings, because real plans separate them: someone can stop
saving at 55, retire at 60, start withdrawals at 62, and claim Social Security at 70.

### 2.1 `retirement_date` (exists)

Already a Demographics input. Meaning is narrowed by this spec: **the date earned income stops.**
It no longer implies anything about savings, withdrawals, or Social Security — those are now their
own dates.

### 2.2 `savings_stop_date` — "consumption jumps to 100% of profit"

Partially exists: the Demographics tab already has a `saving_stop_age` and derives
`months_to_end_of_accumulation`, but nothing consumes it.

**Semantics.** Before this date, the contribution engine (§3) directs the planned contribution
amounts into accounts and the remainder of `profit` is unspent surplus. On and after this date,
**all contributions cease and 100% of `profit` is consumed** — nothing new is invested. Dividends
are still reinvested in this phase (nothing is being drawn yet), per §4.3.

**Boundary year.** If `savings_stop_date` falls mid-year, that year's contributions are prorated by
the fraction of the year before the date, using the same day-count convention
`modules/gross_income.py` already uses (`days_between_inclusive` / `days_in_year`). Contribution
**limits** (§402(g), §415(c), IRA) are *not* prorated — they are annual statutory amounts and a
person can hit the full limit in January. Only the *planned* contribution is prorated.

**Ordering validation.** `savings_stop_date` after `retirement_date` should raise a UI warning
("no earned income to save from"), not an error — a user modeling severance or deferred comp may
legitimately want it.

### 2.3 `withdrawal_start_date` — retirement withdrawals begin

**Semantics.** The date the withdrawal engine (§5) starts producing a spending draw from the
portfolio. Between `savings_stop_date` and this date the portfolio simply compounds untouched
(dividends reinvested, no contributions, no sales except rebalancing).

**Strategy selector**, a Demographics or dedicated Withdrawal-tab input:

- **Flat percentage rule** — `withdrawal = rate × portfolio_value_at_start_of_year`, default 4.0%,
  user-editable. This is the one implemented in this build.
- **Dynamic spending rule** — placeholder. Reserved for Module G2 (guardrails / Guyton-Klinger,
  floor-and-ceiling, RMD-based). The strategy is selected through a single dispatch function so
  adding it later is a new branch, not a rewrite:

  ```python
  def annual_withdrawal_target(strategy: str, params: dict, state: dict) -> float
  ```

  `state` carries portfolio value, prior-year withdrawal, years elapsed, and the expense need — the
  union of what any planned strategy requires. Unimplemented strategies raise `NotImplementedError`
  with a plain message rather than silently falling back to the flat rule.

**Interaction with expenses.** The withdrawal target and the projected expense need are *different
numbers*. The model computes both and reports the gap explicitly per year (`withdrawal_target`,
`spending_need`, `funding_gap`). It does **not** silently set one to the other — that conflation is
exactly what a retirement model is supposed to expose.

### 2.4 `ss_claim_date` — Social Security claiming begins

**Semantics.** The date `ss_benefit_gross` becomes non-zero. Benefit amount comes from Module F
(AIME / bend points / claiming-age adjustment). Until Module F exists, this is a plain annual-benefit
input with the claim date controlling *when* it starts, so the phase wiring can be built and tested
independently of the benefit calculation.

**Partial first year.** Prorated by months claimed in that calendar year. `compute_taxes` already
handles `ss_benefit_gross` including the provisional-income taxability formula — no change needed
there.

### 2.5 Phase resolution

One pure helper, so no module re-derives phase logic inline:

```python
def phase_flags_for_year(year: int, dates: PhaseDates) -> dict:
    """
    Returns {"earning_fraction", "saving_fraction", "withdrawing_fraction",
             "ss_fraction", "phase_label"} — each fraction in [0.0, 1.0], the portion of
    this calendar year the given activity is active. A year fully inside a phase gets 1.0;
    a boundary year gets the day-weighted fraction.
    """
```

Every downstream module reads these fractions rather than comparing dates itself.

---

## 3. Asset purchasing module

New module: `modules/investing.py`. Pure functions, per ground rule 4.

### 3.1 Inputs

**Target allocation by account type.** A dict of `{account_type: {ticker: weight}}` where weights
sum to 1.0 per account type. For this build the user enters these directly on the Portfolio tab.
(§8 replaces this with a derived, optimized allocation — the data shape is chosen now so that
substitution is a change of *source*, not of *interface*.)

**Contributable dollars by account type per year.** This already largely exists: the Projection
tab's contribution-planning section plus `modules/tax.py`'s `resolve_contributions_for_year`,
`compute_max_401k_contributions`, `max_ira_contribution_limit`, and `roth_ira_phase_out_max`. This
module consumes their *resolved, clamped* output; it does not re-derive limits.

**Employer 401(k) match.** Two new inputs in the Income & Expenses tab's existing contribution-
planning section, **both defaulting to 0.0** so no user gets a match they don't have:

- `employer_match_rate` — employer dollars per employee dollar deferred (e.g. 0.50 = 50¢ on the
  dollar).
- `employer_match_cap_pct` — cap as a percent of W-2 gross compensation (e.g. 0.06 = matched only
  on the first 6% of pay deferred).

Resulting match for the year:

```
matched_deferral = min(employee_deferral, employer_match_cap_pct × w2_gross)
employer_match   = employer_match_rate × matched_deferral
```

Three properties of employer match that the engine must get right:

1. It is **employer money, not employee money** — it does not come out of `profit`, and it does not
   consume the §402(g) employee-deferral limit. It is a pure addition to the portfolio.
2. It **does** count toward the §415(c) annual-additions limit for that plan. `modules/tax.py`
   already has `annual_additions_limit` in `data/tax_brackets.json` and a `check_415c_limit`;
   the match must be passed into that check, which currently only sees employee-side dollars.
3. It is **always Traditional (pre-tax)** in this model, even when the employee deferral is Roth —
   which is the common real-world default. Roth-match plans (permitted post-SECURE 2.0) are out of
   scope and noted as such.

Match dollars buy shares in the W-2 401(k) account at that account's target allocation, in the same
year, on the same beginning-of-year convention as every other purchase (§3.4).

**Traditional IRA deductibility — assumed non-deductible.** Per user direction, a Traditional IRA
contribution in this model **never reduces AGI**. This matches what `modules/tax.py` already does
(it has no Traditional-IRA-deduction parameter at all), so the assumption is now explicit and
intentional rather than an unflagged gap. Consequences:

- Traditional IRA basis is after-tax, so withdrawals from it would in reality be partly a tax-free
  return of basis (Form 8606 pro-rata rule). **This model does not implement the pro-rata rule** —
  §5.2 taxes the full Traditional withdrawal as ordinary income, which is *conservative* (overstates
  tax) under a non-deductible assumption. Both halves of this simplification are stated together so
  the direction of the error is known.
- Because the contribution gets no deduction and the growth is still taxed as ordinary income on
  withdrawal, a Traditional IRA is strictly worse than a Roth IRA under this model's assumptions.
  The §3.2 waterfall therefore prefers Roth IRA, and only routes to Traditional IRA when the user
  is above the Roth phase-out (`roth_ira_phase_out_max`) — i.e. the backdoor-Roth situation, which
  the model does not itself execute.

**UI note (required, not optional).** The Income & Expenses tab and the Portfolio tab each display,
next to the Traditional IRA input:

> Traditional IRA contributions are modeled as **non-deductible** — they do not reduce your taxable
> income, and withdrawals are taxed in full as ordinary income (the Form 8606 pro-rata basis
> recovery rule is not modeled). Deductibility depends on income and workplace-plan coverage; if you
> expect a deduction, treat this projection's tax figures as conservative.

### 3.2 The contribution waterfall

Per year, given `profit` (from `modules/projection.py`, already net of tax and expenses) and the
resolved contribution capacities:

1. **Determine investable dollars.** `investable = profit × saving_fraction` (§2.5). If
   `saving_fraction == 0`, skip to §4 — no purchases this year.
2. **Fill accounts in priority order**, each capped at its resolved statutory limit *and* at the
   remaining investable dollars. Default order, user-editable:
   `W-2 401(k) to employer match → HSA → Roth IRA (or Traditional if income-limited) → remaining
   401(k) to §402(g) → SE employer contribution → taxable brokerage (residual)`.
3. **Residual to taxable.** Any investable dollars remaining after all tax-advantaged capacity is
   exhausted go to the taxable account. If a taxable account doesn't exist in the user's portfolio,
   the residual is reported as `uninvested_surplus` and flagged — never silently dropped (§6).
4. **Purchase shares.** Within each account, the dollars allocated to that account buy shares of
   each ticker per the account's target allocation, at that year's price. Fractional shares are
   allowed (the model is not simulating a broker's lot minimums).

### 3.3 Share and lot accounting

Every purchase creates a **tax lot**:

```python
{"ticker": str, "account": str, "shares": float, "basis_per_share": float, "year_acquired": int}
```

Cost basis is tracked per lot, not blended, because §5 needs holding-period and basis detail to
compute realized gain correctly. Lots in tax-advantaged accounts still track basis (cheap, and
needed if Roth conversion or basis-in-Traditional-IRA modeling is ever added) but basis has no tax
effect there.

**Default sale method: specific identification, highest-basis-first** (HIFO), which minimizes
realized gain. User-selectable alternatives: FIFO, average cost. The method is a parameter of the
sale function, not a global.

### 3.4 Timing rule — purchases happen in the year the income is earned

Income earned in year *t* is invested in year *t*, and the resulting shares participate in year *t*'s
growth. The model uses a **beginning-of-year purchase** convention for simplicity and states it
explicitly rather than half-applying a mid-year convention. This slightly overstates the first
year's growth on new contributions versus a real dollar-cost-averaged schedule; if that matters
later, the fix is a documented `contribution_timing` parameter (`"boy"` / `"moy"` / `"eoy"`),
defaulting to `"boy"`, not an undocumented change of convention.

---

## 4. Growth, dividends, and per-asset roll-forward

### 4.1 Per-asset growth, not blended

**This is a hard requirement.** Each holding rolls forward at *its own* asset class's expected
return, net of *its own* expense ratio. `modules/portfolio.py`'s `blended_real_return` and
`blended_expense_ratio` are **reporting figures only** and must not appear anywhere in the
roll-forward.

For each holding, each year:

```
price_next    = price × (1 + real_price_return)
real_price_return = (1 + real_total_return) / (1 + dividend_rate) - 1
                    where real_total_return = expense_adjusted_return(nominal_return, expense_ratio)
                    deflated by inflation
```

The distribution rate — dividend *or* interest, per §4.2's income type — is **divided out of the
total return geometrically to get price return**, the same exact decomposition
`expense_adjusted_return` uses for the expense ratio, and for the identical reason: a rate compounds
against the current value continuously, not as a lump-sum annual subtraction. A plain subtraction
(`real_total_return - dividend_rate`) looks equivalent but isn't once reinvestment is added: the
distribution reinvests as new shares at the start-of-year price (§3.4) and those shares then earn
that same year's own price return too, so `(1 + dividend_rate) × (1 + real_total_return -
dividend_rate)` expands to `1 + real_total_return + dividend_rate × real_price_return` — a small
extra cross term that compounds for decades and silently inflates every result above the configured
nominal return. The geometric division eliminates that term by construction: `(1 + dividend_rate) ×
(1 + real_price_return) = 1 + real_total_return`, exactly, for any dividend yield. This is the
single easiest error to make in this module and the one most likely to silently inflate every
result. It gets a dedicated unit test.

A money-market or cash ticker is the limiting case of this: its entire nominal return is its
interest rate, so its price return is ~0% nominal and *negative* in real terms — which is correct,
and which the model should show rather than smooth over.

Existing helpers `expense_adjusted_return` and `real_return` in `modules/portfolio.py` are reused,
not reimplemented.

### 4.2 Dividends and interest as income

Each holding produces `distribution_income = shares × price × dividend_rate` for the year,
classified by the ticker's income type in the ticker universe. (The rate and type fields already
exist and are captured but unused — `PROJECT_PLAN.md` Step 1 flags exactly this: "not yet consumed
by any calculation." This closes it.)

**The income-type dropdown gains a third option.** It currently offers Qualified / Ordinary; it
becomes:

| Income type | Meaning | `compute_taxes` parameter |
|---|---|---|
| `qualified` | Qualified dividends — preferential LTCG rates | `qualified_dividends` |
| `ordinary` | Non-qualified dividends — ordinary rates | `ordinary_dividends` |
| `interest` | **New.** Interest distributions from cash, money-market, and short-duration bond funds (SPAXX, SGOV, and similar) | `interest_income` |

This makes cash and money-market positions **tickers like any other**, not a separate per-account
cash balance — they already exist in the user's ticker universe today (`NEXT.md` confirms SPAXX and
SGOV are handled through the identical code path as every other ticker, with no special-casing
anywhere), so no new entity is introduced. A money-market ticker is simply one whose asset class has
a near-zero real price return and whose entire return arrives as `interest`.

Why `interest` needs to be its own path rather than being folded into `ordinary`: the two are
identical for **federal** ordinary-rate purposes, but not everywhere else. Municipal-bond funds,
Treasury interest (state-exempt — material given this model already computes CA tax), and any future
state-tax refinement all key off interest specifically. Folding it into `ordinary_dividends` would
be the same shortcut that has already produced two real bugs in this codebase (see §6's last bullet).
`compute_taxes` already has a distinct `interest_income` parameter; this uses it.

Routing into tax, **by account type** (applies to all three income types):

| Account type | Treatment in the year received |
|---|---|
| Taxable | Flows to `compute_taxes` as `qualified_dividends` / `ordinary_dividends` / `interest_income` per the ticker's type. All three enter the NIIT base — already handled inside `compute_taxes`. |
| Traditional 401(k)/IRA | No current tax. Taxed later as ordinary income when withdrawn. |
| Roth 401(k)/IRA, HSA | Never taxed. |

**Migration.** Existing saved states have tickers with only `qualified`/`ordinary` set. They load
unchanged — nothing is auto-reclassified as `interest`, since guessing from a ticker symbol would be
exactly the kind of hardcoded per-ticker special-casing this codebase has deliberately avoided. The
user reclassifies their cash funds themselves through the existing "Ticker details" editor.

So the income sheet's gross income for a year is:

```
gross_income = gross_w2 + gross_se + gross_ordinary_break_income
             + taxable_account_dividends (qualified + ordinary)
             + taxable_account_interest
             + realized_ltcg
             + taxable_retirement_withdrawal
             + ss_benefit_gross
```

This is a genuine change to the meaning of `gross_income` in `modules/projection.py`'s row schema —
it currently means earned income only. The field keeps its name but its composition widens, and the
Projection tab must break the components out rather than showing one merged line.

### 4.3 Reinvestment rule

**Accumulation and coast phases** (`withdrawing_fraction == 0`): dividends are reinvested **back
into the paying asset** — each holding's dividends buy more shares of that same holding, at that
year's price. This creates a new tax lot with basis equal to the reinvested amount (correct: the
dividend was already taxed in a taxable account, so reinvesting it steps up basis and must not be
taxed again at sale).

Dividends earned in a taxable account are reinvested **net of nothing** — the tax on them is paid
from the year's overall cash flow, not withheld from the dividend, since `compute_taxes` computes
one combined liability against all income. This is stated so the ledger check in §6 balances.

**Withdrawal and dissaving phases** (`withdrawing_fraction > 0`): dividends are **spent first**, and
only the unexpended remainder is reinvested:

```
cash_available   = dividends + interest
spent_from_cash  = min(cash_available, withdrawal_need)
reinvested       = cash_available - spent_from_cash
remaining_need   = withdrawal_need - spent_from_cash   → satisfied by asset sales (§5)
```

Reinvestment of the remainder again goes back into the paying asset, pro-rata across the assets that
produced the unexpended cash.

---

## 5. Sales: dissaving and retirement withdrawals

### 5.1 When sales happen

1. **Dissaving** — any year where `profit < 0` (expenses exceed after-tax income) *before*
   retirement. The shortfall is funded by selling assets. This can occur at any age; it is not a
   retirement-only event, and the engine must not gate it on `withdrawal_start_date`.
2. **Retirement withdrawals** — from `withdrawal_start_date`, per §2.3's strategy, net of the cash
   already available from dividends (§4.3).
3. **Rebalancing** — §7. Only inside tax-advantaged accounts, so it generates no realized gain.

### 5.2 What a sale produces

For each lot sold, in the account's chosen sale-method order:

```
proceeds       = shares_sold × price
basis          = shares_sold × lot.basis_per_share
realized_gain  = proceeds - basis
holding_period = current_year - lot.year_acquired   # ≥1 → long-term
```

Routing into tax, **by account type**:

| Account type | Tax consequence of the sale |
|---|---|
| Taxable | `realized_gain` → `compute_taxes`'s `ltcg` (long-term) or ordinary income (short-term). Return of basis is **not** income. |
| Traditional 401(k)/IRA | **Full proceeds** → `taxable_retirement_withdrawal` (ordinary income). Gain/basis irrelevant. Correctly excluded from NIIT and Additional Medicare Tax — `compute_taxes` already does this. |
| Roth IRA/401(k) | $0 taxable, assuming qualified distribution. Non-qualified-distribution rules are **out of scope** and documented as such. |
| HSA | $0 taxable, assuming qualified medical use. Non-qualified use is out of scope. |

Short-term gains are modeled but should be rare — they only arise from selling a lot bought the same
year, which the waterfall makes unusual.

### 5.3 Draw order — tax-bracket-aware fill

Chosen strategy. Because it is the most complex option, it is **built in two stages** and the stage
boundary is explicit so the first stage is genuinely usable:

**Stage 1 (this build): fixed order, configurable.** Default `Taxable → Traditional → Roth`,
with HSA excluded unless the user opts in. This makes the whole ledger runnable and testable
end to end.

**Stage 2 (Module G2): bracket-aware fill.** Each year:

1. Compute taxable income from all non-discretionary sources (SS, dividends, interest, pension).
2. Draw from **Traditional** up to the top of a user-chosen target bracket (default: the 12%/22%
   boundary, read from `data/tax_brackets.json`, not hardcoded).
3. Fund any remaining need from **taxable** (realizing LTCG, which may be 0%-rate-eligible).
4. Fund any still-remaining need from **Roth**, which adds no taxable income.

The circularity here is real and must be handled honestly: the withdrawal amount affects taxable
income, which affects the tax owed, which affects the withdrawal amount needed. Resolve by **fixed-
point iteration** on the year's draw (iterate `compute_taxes` until the draw changes by < $1,
capped at ~20 iterations), and record the iteration count in the row. Do not paper over
non-convergence — surface it.

**RMDs.** From the applicable RMD age, the Traditional draw is `max(strategy_draw, rmd_amount)`,
with the RMD divisor table living in `data/rmd_table.json`, not inline. Excess RMD beyond the
spending need is not consumed — it moves to the taxable account as a purchase, preserving the
dollar (§6).

---

## 6. The ledger identity — every dollar accounted for

The engine's correctness condition, asserted for every account and every year as a unit test and as
an optional runtime check:

```
value_end_of_year = value_start_of_year
                  + contributions
                  + dividends_reinvested
                  + market_growth
                  - sales_proceeds
                  ± rebalancing_transfers      # must net to exactly 0 within an account
```

And at the household level:

```
net_income (after tax, all sources)
  = expenses_consumed
  + contributions_to_accounts
  + uninvested_surplus            # explicitly reported, never silently dropped
  - withdrawals_from_accounts
  - dissaving_proceeds
```

Rules that follow from this and must not be violated:

- **No dollar is created or destroyed.** Any residual that can't be placed becomes
  `uninvested_surplus` (or `unfunded_shortfall` if negative) — a named, reported field, so a
  balancing failure is visible in the UI rather than absorbed.
- **Every sale flows into that year's tax calculation.** No sale is netted against a purchase before
  reaching `compute_taxes`.
- **Every dividend flows into that year's gross income.** The income sheet reflects total income,
  not just earned income (§4.2).
- **Streams stay separate all the way to `compute_taxes`.** W-2, SE, break income, qualified
  dividends, ordinary dividends, interest, LTCG, taxable retirement withdrawal, and SS benefit are
  nine distinct parameters that already exist on `compute_taxes` and must be passed distinctly. Do
  not sum any two of them upstream as a shortcut — this exact class of shortcut has already been a
  real bug in this codebase twice (see `NEXT.md` on `taxable_retirement_withdrawal` vs
  `interest_income`, and on `ordinary_break_income`).

---

## 7. Rebalancing

**Where:** tax-advantaged accounts only (Traditional 401(k)/IRA, Roth IRA/401(k), HSA). Taxable
accounts are **never** rebalanced by selling, because that realizes gain for no modeled benefit;
taxable drift is corrected only through the direction of new contributions.

**When:** once per year, at year end, after growth and dividends are applied.

**How — contributions first, then sell/buy:**

1. Compute each account's current weights after growth, dividends, contributions, and any
   withdrawals.
2. **Direct that year's incoming contributions** to the most-underweight assets first, up to the
   contribution amount available. Often this alone closes the drift.
3. **Then sell overweight / buy underweight** within the account to reach target weights exactly.
   These transactions net to zero cash and produce zero tax consequence.
4. Record the transactions in the ledger so §6's identity still balances (rebalancing transfers must
   sum to 0 within the account).

An optional `rebalance_band` parameter (default 0.0 = always rebalance to exact target) allows a
threshold approach later without a redesign.

**Taxable-account drift — accepted, corrected by contributions only.** Per user direction,
contribution-directed correction is sufficient; no sell-side rebalancing in taxable accounts, and no
tax-loss harvesting in this build (TLH is a future module).

Two consequences worth being honest about in the UI rather than hiding:

- Drift is **real and permanent** over a multi-decade horizon. Once contributions stop at
  `savings_stop_date` (§2.2), there is no correction mechanism left at all — the taxable account's
  allocation is frozen in its drifted state and continues to drift with differential returns. The
  per-year stacked-bar allocation chart (§8) is what makes this visible, and it should be read as a
  feature of the report, not a defect of the model.
- Correction capacity is bounded by contribution size. In late accumulation years, when the taxable
  balance is large relative to annual contributions, directing 100% of new money at the underweight
  asset may still not close the gap. The engine should report `residual_drift` per account per year
  (max absolute deviation from target weight after contributions are directed) so the limitation is
  measured rather than assumed away.

---

## 8. Future work — allocation optimization module

**Not part of this build.** Recorded here so §3's interfaces don't foreclose it.

**Goal:** the user specifies one **overall portfolio target allocation** on the Portfolio tab, and
the model derives per-account-type allocations by asset-location optimization.

**Location priority** (highest tax drag first into tax-sheltered space):

1. Highest-dividend / highest-ordinary-income assets → tax-advantaged accounts first (bonds, REITs,
   high-yield — their income is taxed at ordinary rates annually in a taxable account).
2. Then highest-expected-return assets → Roth first (maximizing the value of never-taxed growth).
3. Low-return, low-yield, and tax-efficient equity index funds → taxable accounts (they benefit from
   the qualified-dividend rate, the step-up at death, and tax-loss harvesting).

**Mechanics:**

- Account-type capacities come from the same projection this document already produces — the
  contributable dollars per account type per year from §3.1.
- Because those capacities change every year, **the derived per-account allocation is computed per
  year**, not once. `{year: {account_type: {ticker: weight}}}`.
- The optimizer is a pure function taking overall targets + per-account capacity + per-ticker
  yield/return/tax characteristics, returning that mapping. Same output shape as §3.1's manual
  input, so it slots in as a replacement source.

**Reporting:** a **stacked bar chart by year** on the Portfolio tab showing the resulting overall
portfolio asset proportions over time — the realized allocation, which drifts from target as
different accounts grow at different rates and as capacity shifts.

---

## 9. Build order and gates

Per `CLAUDE.md` ground rule 2 — one module at a time, user sign-off between each. Proposed sequence:

| # | Deliverable | Gate |
|---|---|---|
| 1 | ✅ **Done (2026-08-10).** Phase dates (§2) — Demographics inputs, `phase_flags_for_year`, horizon extension to age 100 | Projection runs to age 100; post-retirement rows exist with zero income |
| 1b | ✅ **Done (2026-08-10).** Ticker income type gains `interest` (§4.2); employer-match inputs (§3.1) + `check_415c_limit` signature change | Existing saved states load unmodified (migration path for both `saving_stop_age` and `dividend_type`); match excluded from §402(g), included in §415(c) |
| 2 | ✅ **Done (2026-08-10).** `modules/investing.py` contribution waterfall + lot ledger (§3) | Contributions land in the right accounts, respect limits, produce lots |
| 3 | ✅ **Done (2026-08-10).** Per-asset roll-forward + dividends/interest (§4) — `modules/investing.py`'s `roll_forward_holding`/`roll_forward_portfolio`, wired into `project_multi_year` and the Projection/Portfolio tabs | Ledger identity §6 holds; all three income types reach `compute_taxes` distinctly; no blended rate anywhere in roll-forward |
| 4 | ✅ **Done (2026-08-10).** Sales, dissaving, Stage-1 fixed-order withdrawals (§5.1–5.2, Stage 1) — `modules/investing.py`'s `sell_lots`/`draw_order_fill`, wired into `project_multi_year` for DISSAVING only (a pre-retirement `profit < 0` year); formal retirement-withdrawal wiring is Step 6's job (needs §2.3's spending-rate strategy, not yet built) | Realized gains reach `compute_taxes` correctly by account type |
| 5 | ✅ **Done (2026-08-10).** Rebalancing (§7) — `modules/investing.py`'s `rebalance_account`, wired into `project_multi_year` (opt-in, `rebalance`/`rebalance_band`) and the Projection tab | Tax-advantaged only; transfers net to zero |
| 6 | ✅ **Done (2026-08-10).** Flat-percentage withdrawal rule wired to `withdrawal_start_date` (§2.3) — `modules/investing.py`'s `annual_withdrawal_target` dispatch, wired into `project_multi_year` (mutually exclusive with dissaving), reusing Step 4's `draw_order_fill` machinery; `withdrawal_target`/`spending_need`/`funding_gap` reported explicitly, never conflated | End-to-end run from today to end of life |
| 7 | *(Module G2)* **Part 1 ✅ Done (2026-08-23).** Bracket-aware fill + RMDs (§5.3 Stage 2) — `modules/investing.py`'s `bracket_aware_draw`, `modules/tax.py`'s `ordinary_bracket_ceiling`/`rmd_start_age`/`rmd_divisor`, wired into `project_multi_year`'s withdrawal-phase sale (dissaving, Step 4, is untouched — still fixed `DEFAULT_DRAW_ORDER`); see MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md. **Part 2 — dynamic spending rule (closing `funding_gap` via fixed-point iteration) — not started**, a separate sign-off per that doc's own scoping. | Traditional capped at bracket ceiling before Taxable/Roth; RMD enforced as a floor, forced excess reinvested not spent; total realized tax lower than the old fixed order for a representative scenario |
| 8 | *(Future)* Allocation optimization (§8) | — |

### Testing requirements

Beyond per-function unit tests, these **property tests** are required — they are what actually
protects the ledger:

- Ledger identity (§6) holds for every account, every year, across randomized scenarios.
- Household cash identity (§6) holds for every year.
- A portfolio of one asset rolled forward N years equals the closed-form
  `value × (1 + r)^N` — catches double-counting of dividends (§4.1).
- Total shares sold never exceeds shares held; no negative share counts anywhere.
- Sum of lot basis ≤ sum of lot value in an appreciating scenario; basis never goes negative.
- Contributions never exceed the resolved statutory limit for that year.
- Rebalancing transfers within an account sum to 0.0 (within float tolerance).
- A 100%-money-market portfolio (all return arriving as `interest`, ~0% nominal price return) loses
  value in real terms every year and never gains shares except through reinvestment — catches the
  §4.1 distribution/price-return split in its limiting case.
- `employer_match == 0.0` for every year when both match inputs are at their 0.0 defaults, so the
  match feature is provably inert for a user who doesn't have one.
- Employer match never reduces `profit` and never consumes §402(g) room, but does count toward
  §415(c).

---

## 10. Question log

### 10.1 Resolved (2026-08-10) — no longer blocking

| # | Question | Resolution | Where it lives |
|---|---|---|---|
| 1 | Employer match | Two inputs on the Income & Expenses tab (`employer_match_rate`, `employer_match_cap_pct`), **both default 0.0**. Employer money: outside `profit`, outside §402(g), inside §415(c), always pre-tax. | §3.2 |
| 2 | End-of-life horizon | **Run to age 100** (`DEFAULT_PLANNING_HORIZON_AGE`), labeled a planning horizon rather than a life expectancy. Module G1 later replaces the source, not the plumbing. | §1.2 |
| 3 | Interest income source | Cash/money-market are **tickers**, not a separate cash balance. Per-ticker income type gains a third option, `interest`, routed to `compute_taxes`'s existing `interest_income`. | §4.1, §4.2 |
| 4 | Traditional IRA deductibility | **Assumed non-deductible.** Waterfall prefers Roth IRA; Traditional only above the Roth phase-out. Required UI note; Form 8606 pro-rata basis recovery explicitly not modeled (error direction is conservative). | §3.2 |
| 5 | Taxable-account drift | **Contribution-directed correction only.** No taxable sell-side rebalancing, no TLH this build. Engine reports `residual_drift` so the limitation is measured. | §7 |

### 10.2 Newly opened by those resolutions — resolved (2026-08-10, Step 1 sign-off)

1. **`check_415c_limit` needs the employer match passed in.** Still pending — this is Step 1b's own
   deliverable (§9), not resolved by a question, just scheduled. Signature change to an existing,
   tested `modules/tax.py` function; new tests needed: match + deferral together breaching §415(c).
2. **Does the employer match continue past `retirement_date` in a partial-work year?** **Resolved:
   yes, automatic proration is correct** — no special-casing needed; match = rate × min(deferral,
   cap×W-2 gross), and W-2 gross already prorates for the partial year via the Step 1 machinery.
3. **Ticker income type: one type per ticker vs. a per-ticker split?** **Resolved: one type per
   ticker**, per the spec's own recommendation — split a mixed-distribution fund into two ticker
   entries if that level of precision is ever needed.
4. **Does the Roth-preference rule in §3.2 need an explicit Traditional-IRA override?** **Resolved:
   no** — manually typing a value into the Traditional IRA field (already possible today,
   independent of whatever the future waterfall would compute) is a sufficient override for
   modeling an existing balance or contribution.
