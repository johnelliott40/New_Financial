"""
Multi-year projection — the "tax-module adapter" (gross_income_module_spec.md Section 4): maps each
`modules.gross_income` yearly row onto `modules.tax.compute_taxes` and returns one combined row per
year, gross income through net (after-tax) income.

Pure function (`project_multi_year`): no Streamlit, no I/O — takes already-loaded `bracket_table`
(see `modules.tax.load_bracket_table`) same as `compute_taxes` itself.

Scope, deliberately narrow for v1 (see NEXT.md for the full reasoning):
- 401(k)/solo 401(k) contributions are resolved every year via `modules.contributions.resolve_401k_target`
  (CONTRIBUTION_TOGGLE_REDESIGN.md, 2026-08-16 — see that module's own docstring for the full
  two-stage design this function implements). A year absent from `contribution_config_by_year`
  falls back to `modules.contributions.DEFAULT_CONTRIBUTION_CONFIG` ($0 everywhere — every
  pre-existing caller/test that doesn't pass this keeps its exact original $0-contribution
  behavior).
- Traditional/Roth IRA contributions ARE now wired into this module (Stage 2,
  `modules.contributions.fund_from_available_cash`) — unlike 401(k), they're funded FROM already-
  liquid, already-taxed cash, not a payroll deduction, so they compete for `available_cash` rather
  than being resolved independently of it. `compute_taxes` still has no Traditional-IRA-deduction
  parameter (deductibility itself isn't modeled; a real, pre-existing gap, documented in NEXT.md,
  unrelated to and unchanged by this system) — a Traditional IRA contribution here never reduces
  AGI.
- Investment income (LTCG, dividends, interest) and Social Security benefits are not produced by
  `modules.gross_income` at all — this module doesn't invent them; they're 0 in every projected
  year until Modules D/F/G2 exist.
- `age_at_year_end` is computed from `birth_date` and each row's own `year` — the same simple
  `year - birth_date.year` arithmetic already used in `state.py`'s Tax-tab seeding, not a duplicate
  of `modules.demographics`'s `current_age` (which answers a different question — age as of an
  arbitrary date, not age at a future year's end).

Tax brackets beyond the last configured year (`data/tax_brackets.json`'s policy note, resolved
2026-08-09): since this entire model runs in real (inflation-adjusted) dollars end to end, holding
the *nominal-looking* bracket thresholds/rates flat for every year beyond the last one actually
configured is the real-dollar-correct choice, not a placeholder — a real tax system's brackets are
themselves inflation-indexed, so "this year's brackets, unchanged" *is* what "real dollars" means
for a future year's tax law. See `_bracket_year_for` and PROJECT_PLAN.md's "Tax methodology"
section (this resolves the "no forward inflation-indexing" policy question flagged there — the
other candidate policy, eroding the two non-inflation-indexed threshold groups by a modeled
real-wage-growth rate, was considered and explicitly not chosen).
"""

from __future__ import annotations

from datetime import date

from modules.contributions import (
    DEFAULT_CONTRIBUTION_CONFIG,
    fund_from_available_cash,
    resolve_401k_target,
    resolve_roth_ira_target,
    resolve_traditional_ira_target,
)
from modules.gross_income import phase_flags_for_year, project_expenses, project_gross_income
from modules.investing import (
    REBALANCEABLE_ACCOUNT_TYPES,
    annual_withdrawal_target,
    bracket_aware_draw,
    create_lots,
    draw_order_fill,
    rebalance_account,
    roll_forward_portfolio,
)
from modules.portfolio import portfolio_value_by_account_type
from modules.social_security import retirement_earnings_test_reduction
from modules.tax import (
    compute_taxes,
    employer_401k_match,
    max_ira_contribution_limit,
    net_se_earnings_for_retirement,
    ordinary_bracket_ceiling,
    rmd_divisor,
    rmd_start_age,
    roth_ira_magi,
)


def _bracket_year_for(year: int, bracket_table: dict) -> int:
    """
    Which `bracket_table` year's thresholds/rates to actually use for `year`. Real-dollar model
    policy: use the latest configured year at or before `year`; if `year` predates every configured
    year, fall back to the earliest one instead (defensive — shouldn't happen in practice, since
    projections start at `current_date`, never before the bracket table's own start). Raises if
    `bracket_table` is empty entirely — there is no sensible fallback for that.
    """
    if not bracket_table:
        raise ValueError("bracket_table is empty — no tax bracket data configured at all")
    available_years = sorted(bracket_table.keys())
    candidates = [y for y in available_years if y <= year]
    return candidates[-1] if candidates else available_years[0]


def project_multi_year(
    income_inputs: dict,
    expense_inputs: dict,
    current_date: date,
    horizon_date: date,
    retirement_date: date,
    birth_date: date,
    filing_status: str,
    bracket_table: dict,
    deferral_priority: str = "w2-first",
    contribution_config_by_year: dict[int, dict] | None = None,
    phase_dates: dict | None = None,
    employer_match_rate: float = 0.0,
    employer_match_cap_pct: float = 0.0,
    universe: dict | None = None,
    initial_lots: list[dict] | None = None,
    initial_prices_by_ticker: dict | None = None,
    inflation_rate: float = 0.0,
    target_allocations: dict | None = None,
    draw_order: list[str] | None = None,
    sale_method: str = "hifo",
    rebalance: bool = False,
    rebalance_band: float = 0.0,
    withdrawal_strategy: str = "flat_percentage",
    withdrawal_strategy_params: dict | None = None,
    rmd_table: dict | None = None,
    ss_annual_benefit: float = 0.0,
    ss_bend_point_table: dict | None = None,
) -> list[dict]:
    """
    Returns one row per calendar year from `current_date` through `horizon_date` (the planning
    horizon — see `modules.demographics.DEFAULT_PLANNING_HORIZON_AGE` — not `retirement_date`; see
    MODEL_WIRING.md §1-§2 and `modules.gross_income`'s own module docstring for the full
    horizon-vs-retirement split, 2026-08-10):
    {
        "year", "years_from_now", "is_partial_year", "partial_year_fraction", "partial_year_reason",
        "gross_w2", "gross_se", "gross_ordinary_break_income", "gross_income", "gross_expense",
        "active_break_labels", "notes",
        "tax_available": bool,   # False only if bracket_table has no data at all (see below)
        "bracket_year_used": int | None,  # which bracket_table year's rates were actually applied
        "total_tax", "net_income", "profit",  # None when tax_available is False
        "earned_income_tax",  # tax on gross_w2+gross_se alone (investment/break income zeroed,
                               # actual 401(k)-family contributions applied) — None when tax_available
                               # is False; see this function's own inline comment at its call site
        "discretionary_spending",  # 2026-09-06 — earned-income surplus NOT becoming a new Stage-2
                                    # contribution this year (see "Discretionary spending" below);
                                    # None when tax_available is False, otherwise never negative
        "dividend_used_for_expenses",  # 2026-09-06 — the portion of this year's Taxable dividend/
                                        # interest spent covering an earned-income shortfall instead
                                        # of reinvesting (see "Dissaving" below); None when portfolio
                                        # roll-forward isn't active, 0.0 whenever there's no shortfall
        "contribution_warnings": list[str],  # over-cap notices for this year's contributions, [] if none
        ... every other modules.tax.compute_taxes() result field, when tax_available is True ...
    }

    `retirement_date` now only controls when `gross_w2`/`gross_se` stop (see
    `modules.gross_income.project_gross_income`) — years after it still get a row, through
    `horizon_date`, with W-2/SE income at $0 (break/pension income and expenses are unaffected).
    `savings_stop_date` no longer zeroes anything (see `discretionary_spending` below, 2026-09-06) —
    real earned income between Savings stop date and Income end date, if any, keeps flowing through
    `gross_w2`/`gross_se`/`gross_income`/`total_tax` exactly like any other year.

    **Contributions (CONTRIBUTION_TOGGLE_REDESIGN.md, 2026-08-16 — replaces the old priority-order
    waterfall + manual-override dual-branch entirely)**: `contribution_config_by_year`, optional
    `{year: {"contribution_401k_mode", "contribution_401k_custom_amount",
    "contribution_401k_custom_type", "maximize_se_employer_401k", "roth_ira_mode",
    "roth_ira_custom_amount", "traditional_ira_mode", "traditional_ira_custom_amount"}}` — see
    `modules.contributions`'s own module docstring for the full two-stage design. A year ABSENT
    from this dict (or the dict itself `None`) falls back to
    `modules.contributions.DEFAULT_CONTRIBUTION_CONFIG` — "custom, $0" everywhere, so every
    existing caller/test that doesn't pass this parameter keeps its exact original $0-contribution
    behavior, fully backward-compatible.

    Every year, unconditionally (no more on/off toggle):
    1. **Stage 1 — 401(k)-family** (`modules.contributions.resolve_401k_target`): a payroll
       deduction, resolved directly from this year's mode against real IRS limits — never gated on
       available cash (see that function's own docstring for why). `w2_401k_contribution_used`/
       `roth_401k_contribution_used`/`se_401k_employee_contribution_used`/
       `se_401k_employer_contribution_used` on the row reflect this; `total_tax`/`net_income` are
       then computed WITH this real deduction already applied.
    2. **Stage 2 — Roth IRA, then Traditional IRA, then Taxable** (`modules.contributions.
       fund_from_available_cash`): `available_cash` = earned-income-only, post-tax (with the real
       401(k) deduction credited), post-expense profit × `saving_fraction` — deliberately EXCLUDING
       investment income already auto-reinvested by `roll_forward_portfolio` (the same double-
       counting exclusion `investable` always used). Roth/Traditional IRA targets come from
       `modules.contributions.resolve_roth_ira_target`/`resolve_traditional_ira_target` (Roth
       resolves first, sharing one combined limit with Traditional — IRC §219(b); both structurally
       gated on IRC §219(f)(1)'s earned-income requirement via `combined_ira_limit`, `$0` whenever
       `gross_w2 + gross_se <= 0` that year, regardless of MAGI/phase-out math). Taxable is always
       the genuine, guaranteed leftover — never an optional side-channel that could forget to get
       populated (WATERFALL_STREAMLINE_REDESIGN.md §2.2's own fix, built into this system from the
       start).

    `phase_dates`: `{"savings_stop_date", "withdrawal_start_date", "ss_claim_date"}` (plain `date`
    objects; `retirement_date` above is reused as the fourth phase date automatically). Optional —
    `saving_fraction`/`withdrawing_fraction` default to `1.0`/`0.0` (i.e. "always accumulating,
    never withdrawing") when omitted, so every year still resolves Stage 1/2 normally; only
    `discretionary_spending` and the withdrawal-phase machinery need it supplied. **Validated once,
    up front, not per-year** (2026-09-06, NEXT.md item B2): `withdrawal_start_date` may not precede
    EITHER `retirement_date` or `savings_stop_date` — same-day is explicitly allowed, raises
    `ValueError` otherwise. This is what structurally guarantees `saving_fraction` reaches exactly
    `0` no later than the year `withdrawing_fraction` turns positive, so Stage-2 voluntary
    contributions (gated on `saving_fraction > 0`) and an active withdrawal can never coincide.

    **Discretionary spending** (2026-09-06, NEXT.md item B1 — replaces the old "coasting-phase
    freeze," WATERFALL_STREAMLINE_REDESIGN.md §3, which forced `gross_w2`/`gross_se`/
    `gross_ordinary_break_income`/`gross_expense` all to `$0` between `savings_stop_date` and
    `withdrawal_start_date` regardless of whether real earned income was still happening — wrong
    whenever `savings_stop_date` fell BEFORE `retirement_date`): `row["discretionary_spending"] =
    max(0.0, profit_earned_only) - available_cash`. Since `available_cash` already equals
    `max(0.0, profit_earned_only) * saving_fraction` (Stage 2, below, unchanged), this is exactly
    `max(0.0, profit_earned_only) * (1 - saving_fraction)` — during full coasting (`saving_fraction
    == 0`), 100% of the earned surplus becomes discretionary spending and `$0` becomes a new Stage-2
    contribution; during normal accumulation (`saving_fraction == 1`) it's exactly `$0`, no change
    to prior behavior. Investment income is untouched by this, by construction: `profit_earned_only`
    is already built from `gross_earned_income` alone (dividends/interest were never part of it).
    `discretionary_spending` is a pure SINK — money leaving the model as spent, never tracked
    further, not a second savings/investment bucket — it can never be negative by construction
    (`max(0.0, ...)` already present in `available_cash` itself).

    `employer_match_rate`/`employer_match_cap_pct`: flat plan-design settings (not year-by-year),
    both defaulting to `0.0` — see `modules.tax.employer_401k_match`. The match never affects that
    year's tax or cash flow either way (employer money — see that function's own docstring).

    **`net_income`/`profit` correctly subtract 401(k)-family contribution dollars actually used**
    (`w2_401k`/`roth_401k`/SE-employee/SE-employer — NOT employer match, NOT IRA, NOT whatever
    reached Taxable): those dollars are deducted from payroll/net profit BEFORE they're ever liquid
    cash, unlike an IRA or Taxable contribution, which is funded FROM already-liquid, already-taxed
    cash (i.e. from `profit` itself, not a further reduction to it). `profit` = `net_income -
    gross_expense`: the year's actual real-dollar LIQUID surplus (or deficit, if negative) after
    tax, after 401(k)-family payroll-style deferrals, and after expenses.

    Every year gets real tax figures, computed with `_bracket_year_for`'s real-dollar policy (see
    module docstring): a year beyond the last one actually configured in `bracket_table` reuses that
    last year's rates/thresholds rather than going without — `tax_available=False` (with
    `total_tax`/`net_income`/`profit` all `None`) now only happens if `bracket_table` is empty
    outright, which `notes` explains plainly rather than raising and losing every other year's real
    output.

    **Portfolio roll-forward (Step 3, MODEL_WIRING.md §4)** — `universe`, `initial_lots`,
    `initial_prices_by_ticker`, `inflation_rate`, `target_allocations`: all optional, all defaulting
    to `None`/`0.0`; when `universe` and `initial_lots` are BOTH supplied, every year's row also
    carries a genuine, stateful tax-lot ledger forward via `modules.investing.roll_forward_portfolio`
    (each holding growing at its OWN asset-class return net of its OWN expense ratio — never a
    blended rate, §4.1's hard requirement) instead of the fully backward-compatible $0-investment-
    income behavior every earlier caller/test still gets by omitting these.

    When active, EACH year, in this order:
    1. `roll_forward_portfolio` runs FIRST, using the lot ledger CARRIED FORWARD from the end of the
       PRIOR year (or `initial_lots`/`initial_prices_by_ticker` for the first year) — deliberately
       NOT including any of THIS year's own new contribution or reinvestment lots, avoiding
       circularity (a new lot doesn't generate distribution income until the following year; see
       `modules.investing`'s own module docstring gap #2).
    2. Taxable-account `qualified`/`ordinary`/`interest` distributions widen `gross_income` (§4.2)
       and feed `compute_taxes` as `qualified_dividends`/`ordinary_dividends`/`interest_income` —
       Traditional 401(k)/IRA distributions are untaxed, Roth/HSA are never taxed, exactly like every
       other flow through those account types.
    3. This year's contribution decision (Stage 1/2 above) runs against the WIDENED `gross_income`
       for tax purposes, but `available_cash` itself stays earned-income-only (see Stage 2's own
       comment at its call site for why).
    4. New contribution dollars are converted into lots via `modules.investing.create_lots` using
       `target_allocations` and THIS year's post-roll-forward prices; reinvestment lots from step 1
       (§4.3: taxable stops reinvesting once `withdrawing_fraction > 0` — from `phase_dates` via
       `phase_flags_for_year`, defaulting to `0.0` i.e. always-reinvest if `phase_dates` is `None`;
       tax-advantaged accounts always reinvest) are added too, MINUS the tax actually owed on a
       Taxable distribution (2026-09-06 fix, see NEXT.md): `roll_forward_portfolio` reinvests every
       distribution at its full gross amount regardless of account type (it has no opinion on
       taxes, by design), but a Taxable distribution's tax — `tax_attributable_to_investment_income`
       below, the same "second `compute_taxes` call with the source zeroed" isolation technique
       `tax_attributable_to_ss` uses — is real and otherwise never actually deducted from anything,
       silently letting Taxable-account dividends/interest compound tax-free forever. Only the
       Taxable reinvestment lots are scaled down by the after-tax fraction; 401(k)/IRA/Roth
       reinvestment lots (genuinely tax-deferred/tax-exempt on reinvestment) are untouched. Both
       kinds of lots are merged into the ledger carried forward to next year's step 1.

    Adds to every row when portfolio roll-forward is active (`None` otherwise): `"gross_investment_income"`,
    `"taxable_qualified_dividends"`, `"taxable_ordinary_dividends"`, `"taxable_interest_income"`
    (the three components already folded into the row's own `gross_income`, broken out per §4.2's own
    instruction), `"tax_attributable_to_investment_income"` (the marginal tax cost of this year's
    Taxable-account dividends/interest, isolated the same way as `tax_attributable_to_ss` — see step
    4 above for how it's actually used, not just reported), `"portfolio_value"` (lots × this year's
    ending prices, summed), `"ending_lots"`, `"ending_prices_by_ticker"` (the full ledger snapshot at
    the END of this year, for the caller to display or feed to next year's call).

    **Dissaving (Step 4, MODEL_WIRING.md §5.1 point 1)** — `draw_order`/`sale_method`: only
    meaningful when portfolio roll-forward is active. Any year where `year < retirement_date.year`
    AND `withdrawing_fraction <= 0` first nets an EARNED-income-only shortfall (`max(0.0,
    -profit_earned_only)`, investment-income-EXCLUDED by construction) against this year's Taxable
    dividend/interest (`dividend_used_for_expenses = min(gross_investment_income, earned_shortfall)`
    — see `row["dividend_used_for_expenses"]`), then sells assets ONLY for whatever remains
    (`remaining_shortfall`) dollar-for-dollar, via `modules.investing.draw_order_fill` — gated on
    `retirement_date` specifically, NOT `withdrawal_start_date`/`saving_fraction`, per §5.1's own
    instruction that dissaving "can occur at any age" and "the engine must not gate it on
    `withdrawal_start_date`." A post-retirement negative-profit year is NOT funded by this
    mechanism — that is the formal retirement-withdrawal strategy (§2.3), reserved for Step 6
    (not yet built; see `modules/investing.py`'s own module docstring gap #4) — a documented,
    honest gap rather than a guess.

    **The earned-vs-investment-income netting above (2026-09-06, NEXT.md item B4/B5) fixes a real
    double-counting bug**: `row["profit"]` is investment-income-INCLUSIVE, so gating the sale on
    `profit < 0` alone (the pre-2026-09-06 behavior) let a dividend large enough to cover an earned-
    income shortfall skip the sale correctly while that SAME dividend cash, completely separately,
    still unconditionally reinvested in full — the identical dollar counted as both "spent on
    expenses" and "bought new shares." Whatever `dividend_used_for_expenses` actually consumes is
    now also deducted from that SAME dividend's own reinvestment lots (composed AFTER the
    investment-income-tax deduction — see the "Taxable-account reinvestment" comment near
    `roll_result["new_lots"]`, below), so a dollar spent on expenses can never also buy new shares.

    `draw_order` defaults to `modules.investing.DEFAULT_DRAW_ORDER` (Taxable → Traditional 401(k) →
    Traditional IRA → Roth 401(k) → Roth IRA; HSA excluded unless the caller opts it in) and
    `sale_method` to `"hifo"` (minimizes realized gain) — both directly passed through to
    `draw_order_fill`/`sell_lots`. Realized gains reach `compute_taxes` correctly BY ACCOUNT TYPE
    (§5.2): a Taxable sale's long/short-term gain → `ltcg`/`short_term_gains`; a Traditional
    401(k)/IRA sale's FULL proceeds (not just gain) → `taxable_retirement_withdrawal`; a Roth/HSA
    sale is never taxed. **A deliberate, documented one-pass approximation, same precedent as the
    contribution engine's own earned-only/full two-pass tax computation (Stage 1/2 above)**: the
    sale is sized to cover the
    ORIGINAL shortfall only — the additional tax owed on the sale's OWN realized gain is reflected
    in the row's final `total_tax`/`net_income`/`profit` (a second, real `compute_taxes` pass), but
    is not itself re-covered by selling more. Fully closing that circularity is explicitly Stage 2's
    job (§5.3, Module G2, fixed-point iteration) — not attempted here. Adds to every row when a sale
    happened this year (`None`/`0.0` otherwise): `"dissaving_proceeds"`, `"dissaving_long_term_gain"`,
    `"dissaving_short_term_gain"`, `"dissaving_retirement_withdrawal"`,
    `"dissaving_unfunded_shortfall"` (§6 — reported, never silently dropped, if the whole draw order
    still couldn't cover the shortfall).

    **Rebalancing (Step 5, MODEL_WIRING.md §7)** — `rebalance`/`rebalance_band`: only meaningful
    when portfolio roll-forward is active; both optional, `rebalance` defaulting to `False` so every
    existing caller/test keeps its exact original behavior. When `True`, EVERY year, AFTER this
    year's contribution purchases and dissaving sale (if any) are applied, each account type in
    `modules.investing.REBALANCEABLE_ACCOUNT_TYPES` (Traditional/Roth 401(k)/IRA, HSA — Taxable is
    NEVER rebalanced by selling, per §7's own instruction) is corrected to its `target_allocations`
    weights via `modules.investing.rebalance_account` — net-zero-cash trades with zero tax
    consequence, so `total_tax`/`profit` are completely unaffected by this step. `rebalance_band`
    (default `0.0`, always correct exactly) is passed straight through. Adds
    `"rebalance_residual_drift"` to every row when active: `{account_type: float}`, the max
    remaining `|current_weight - target_weight|` per account type after correction — `0.0` in the
    common case, nonzero only for an unpriced ticker or one protected by `rebalance_band` (§7's own
    explicit ask: "measured, not assumed away").

    **A real, discovered interaction between dissaving and §4.3's reinvestment rule, left as-is
    rather than special-cased**: a Taxable holding's dividend still reinvests into a new lot this
    same year even when that SAME year also dissaves to cover a shortfall — §4.3's reinvestment
    rule is keyed purely off `withdrawing_fraction`, not off whether a dissaving sale happened, and
    the two mechanisms were built independently (§4 vs §5.1). The dividend cash is still correctly
    counted as taxable income and correctly reduces the shortfall the sale needs to cover; it's only
    the tiny leftover reinvestment lot that looks odd in isolation. Not fixed here — §4.3 predates
    dissaving entirely and doesn't reference it, so this is a documented, low-stakes edge case, not
    a silent guess.

    **Retirement withdrawals (Step 6, MODEL_WIRING.md §2.3/§5.1 point 2)** —
    `withdrawal_strategy`/`withdrawal_strategy_params`: only meaningful when portfolio roll-forward
    is active. Any year with `withdrawing_fraction > 0` (from `phase_dates`, via
    `phase_flags_for_year` — `0.0`, i.e. never withdrawing, when `phase_dates` isn't supplied)
    computes `withdrawal_target = modules.investing.annual_withdrawal_target(
    withdrawal_strategy, withdrawal_strategy_params, {"portfolio_value": <this year's STARTING
    portfolio value, before any of this year's own growth/dividends/contributions>})` —
    `withdrawal_strategy` defaults to `"flat_percentage"`, `withdrawal_strategy_params` defaults to
    `{"rate": 0.04}` when `None` (§2.3's own stated default).

    **The withdrawal target and the year's actual `gross_expense` (renamed `spending_need` in this
    context) are DELIBERATELY two different numbers under `"flat_percentage"`, never silently
    reconciled** (§2.3's own explicit instruction) — both are reported, plus `funding_gap =
    withdrawal_target - spending_need` (positive = withdrawing more than the year's expenses;
    negative = a real shortfall that year's `profit` will show, exactly as-is, with no further
    correction — the flat rule's own known limitation, made visible rather than papered over).

    **`"target_net_spending"` (Module G2 Part 2, MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md,
    2026-08-29) closes that gap instead of just reporting it**, via fixed-point iteration: starting
    from the naive guess `spending_need` itself, each iteration runs one full bracket-aware split +
    sale + tax pass (the same mechanics described below, factored into a per-year `_simulate_
    withdrawal` closure so it can be re-run against an unmutated `current_lots`/`current_prices`
    snapshot without double-selling), compares the resulting `net_retirement_income` against
    `spending_need`, and adjusts the guess by exactly that gap. Because a marginal dollar of pre-tax
    withdrawal always raises `net_retirement_income` by LESS than a dollar (the tax on it), this
    update is a genuine contraction — capped at 20 iterations regardless, with the count recorded on
    the row (`"withdrawal_iteration_count"`, `None` for `"flat_percentage"` and for any year outside
    the withdrawal phase) and non-convergence appended to `contribution_warnings` (reused, not a new
    mechanism) rather than silently accepted. `funding_gap` is still reported under this strategy
    too, and should read ~`0.0` in every converged year — a nonzero value there is a real signal
    (non-convergence, or an unfunded shortfall capping the achievable draw below `spending_need`),
    not a leftover of the flat rule's own by-design gap.

    The amount actually SOLD is `max(0.0, withdrawal_target - dividends_available)` — Taxable-
    account `qualified`/`ordinary`/`interest` distributions already received this year are netted
    out first (§4.3/§5.1's own "net of the cash already available from dividends"), then the
    remainder is raised via `modules.investing.bracket_aware_draw` (MODULE_G2_STEP7_BRACKET_AWARE_
    WITHDRAWALS.md Part 1, 2026-08-23 — replaces the old fixed-`draw_order` sale here; dissaving,
    Step 4 above, is unchanged and still uses `draw_order_fill`/`DEFAULT_DRAW_ORDER`). Realized gains
    reach `compute_taxes` correctly by account type (§5.2, identical routing/one-pass-approximation
    precedent as dissaving — see that section above). **Mutually exclusive with dissaving by
    construction**: dissaving is now additionally gated on `withdrawing_fraction <= 0` (tightened
    from Step 4's original `year < retirement_date.year`-only gate), so a year is either pre-
    withdrawal-phase-and-dissaving-eligible or in the withdrawal phase, never both, even in the
    unusual case where `withdrawal_start_date` is configured earlier than `retirement_date`.

    **Bracket-aware split (replaces the old fixed Taxable→Traditional→Roth order for this SOLD
    amount only — dissaving above is untouched)**: `sale_amount_needed` is split into three
    independent targets, Traditional first (capped at BOTH that year's own headroom under
    `withdrawal_strategy_params.get("target_bracket_rate", 0.22)`'s ordinary-bracket ceiling — via
    `modules.tax.ordinary_bracket_ceiling` against already-locked-in ordinary income for the year —
    AND at Traditional's own current balance), then Taxable (capped at ITS own current balance; not
    itself LTCG-bracket-limited in this first iteration — a documented, flagged simplification, not
    an oversight), then Roth for whatever's left (uncapped here — `bracket_aware_draw`'s own sale
    against Roth's real balance is the true final shortfall signal). Each cap rolls its own shortfall
    down into the next bucket in the chain BEFORE the sale runs, so a bucket that's already exhausted
    never reports a shortfall the next bucket down could actually have covered. The REASONING (not
    just the mechanics): Traditional is 100% ordinary income no matter when it's sold, so there's no
    cost to taking it early up to a comfortable bracket; Taxable's gain gets a real shot at the low/
    0% LTCG bracket precisely because ordinary income was capped, not left to climb; Roth is the only
    account whose UNSPENT balance keeps compounding completely tax-free forever with no RMDs ever
    forcing a distribution, so it's preserved for last rather than spent down first purely to
    minimize THIS year's tax — see MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md for the full case.

    **RMDs**: whenever `rmd_table` is supplied (`modules.tax.load_rmd_table` — `None` skips RMD
    enforcement entirely, fully backward compatible) and `age_at_year_end >=
    modules.tax.rmd_start_age(birth_date.year)`, that year's Required Minimum Distribution
    (`traditional_balance_at_start_of_year / modules.tax.rmd_divisor(age_at_year_end, rmd_table)`,
    IRS Pub 590-B Table III) is enforced as a FLOOR on the Traditional target above, never a ceiling.
    Any amount the RMD forces beyond what `sale_amount_needed` actually required is still sold (and
    still taxed as ordinary income, like any other Traditional distribution — it reaches
    `compute_taxes` via the same `withdrawal_taxable_retirement_distribution` path) but is NOT
    treated as spendable withdrawal income: it's reinvested into Taxable via `modules.investing.
    create_lots`, exactly like a contribution, so the ledger identity (§6 — no dollar silently
    unaccounted for) holds and `net_retirement_income` isn't inflated with cash that was never
    actually needed for spending.

    Adds to every row when portfolio roll-forward is active (`None` when inactive; `0.0`/real
    values when active but `withdrawing_fraction <= 0` this year): `"withdrawal_target"`,
    `"spending_need"`, `"funding_gap"`, `"withdrawal_dividends_applied"`,
    `"withdrawal_sale_proceeds"`, `"withdrawal_long_term_gain"`, `"withdrawal_short_term_gain"`,
    `"withdrawal_taxable_retirement_distribution"`, `"withdrawal_unfunded_shortfall"` (§6 —
    reported, never silently dropped, same convention as `dissaving_unfunded_shortfall`),
    `"withdrawal_unfunded_shortfall_by_bucket"` (`{"traditional", "taxable", "roth"}`, the true
    "even after cascading, we came up short" signal per bucket), `"rmd_amount"` (`0.0` whenever
    `rmd_table` is `None` or the age gate isn't met yet), `"rmd_forced_excess"` (the portion of this
    year's Traditional sale that was forced by the RMD floor beyond `sale_amount_needed` and
    reinvested rather than spent), `"withdrawal_iteration_count"` (`None` under `"flat_percentage"`;
    the number of fixed-point iterations `"target_net_spending"` actually ran, 1-20, otherwise).

    **Social Security (Module F, 2026-08-31)**: `ss_annual_benefit` is the ALREADY-COMPUTED annual
    gross benefit (`modules.social_security.annual_ss_benefit` — AIME from a real earnings history,
    the bend-point PIA formula, this person's own claiming-age adjustment; see that module's own
    docstring) — a single, fixed REAL-dollar figure for the whole retirement (this model's
    real-dollar convention means no separate year-by-year COLA modeling is needed: a flat real
    amount from the claim date onward already represents what COLA is designed to preserve).
    Multiplied by `ss_fraction` (from `phase_flags_for_year`, day-weighted so the claim year itself
    is correctly prorated for a mid-year `ss_claim_date`, `0.0` before it) to get each row's own
    `ss_benefit_gross`, fed into every `compute_taxes` call this function makes — the entire
    provisional-income taxability calculation (0%/50%/85% tiers, CA's full exemption) was already
    correct and just never received a real number before this. Defaults to `0.0` (no benefit at
    all), fully backward compatible with every existing caller.

    **Retirement Earnings Test (2026-09-06, NEXT.md item B3)**: `ss_bend_point_table` (optional,
    `modules.social_security.load_bend_point_table()`'s own return shape — `None` skips this
    entirely, fully backward compatible) applies `modules.social_security.
    retirement_earnings_test_reduction` to whatever `ss_benefit_gross` this year's `ss_fraction`
    already produced — a real dollar-for-dollar withholding for claiming before Full Retirement Age
    while still earning `gross_w2`/`gross_se` (never `gross_investment_income` — the RET explicitly
    excludes it). `row["ss_earnings_test_reduction"]` is the amount ALREADY subtracted out (not a
    separate figure to apply yourself), always `0.0` (never `None`) when no reduction applies, same
    always-populated convention as `ss_benefit_gross` itself. See that function's own docstring for
    the two documented simplifications this carries (the FRA-year annual-vs-monthly approximation,
    and the un-modeled SSA credit-back once FRA is reached).

    Every row also always carries `"withdrawing_fraction"` (the raw day-weighted fraction, `0.0`
    when `phase_dates` wasn't supplied) and `"is_withdrawal_year"` (`withdrawing_fraction > 0`) —
    the correct split point for any pre/post-retirement reporting, per RETIREMENT_REPORTING_AUDIT.md
    §2.4 (2026-08-13): infer it from THIS field, never from `"withdrawal_target" > 0`, which is
    `None` before the withdrawal phase and legitimately `0.0` inside a withdrawal-phase year with no
    actual draw — two different states worth keeping distinguishable.

    **Retirement-income reporting (RETIREMENT_REPORTING_AUDIT.md §2, 2026-08-13) — a confirmed
    defect, now fixed**: `gross_income`/`net_income`/`profit` are REUSED across the withdrawal
    boundary but silently change meaning there — pre-retirement they're the ordinary IRS gross/net-
    earned-income constructs; once `is_withdrawal_year`, the SAME field names only ever capture the
    TAXABLE slice of that year's withdrawal (e.g. a year drawing entirely from Roth accounts shows
    `gross_income`/`net_income` of exactly `$0`, even though real, spendable cash was raised). Three
    new fields give the genuinely correct picture for a withdrawal-phase year, added when
    `portfolio_active and row["is_withdrawal_year"]` (`None` otherwise):
    - `"total_withdrawal_income"` = `withdrawal_dividends_applied + withdrawal_sale_proceeds -
      rmd_forced_excess + ss_benefit_gross` — the TOTAL cash actually raised/received this year FOR
      SPENDING, across every portfolio account type (Traditional, Roth, Taxable, HSA) PLUS Social
      Security (Module F, 2026-08-31 — real cash in hand, taxed separately via `retirement_taxes_
      paid` below, same as any portfolio withdrawal), not just the taxable portion of either. The
      `rmd_forced_excess` subtraction (MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1,
      2026-08-23) excludes a forced-above-target RMD sale, which is reinvested into Taxable rather
      than spent — see this function's own Retirement-withdrawal section above.
    - `"retirement_taxes_paid"` = that year's `total_tax` (a clearer name in this specific context —
      during pure retirement, `total_tax` is already fully attributable to investment income +
      realized gains + Traditional distributions + the Social Security benefit above, so no new tax
      computation is needed).
    - `"net_retirement_income"` = `total_withdrawal_income - retirement_taxes_paid` — the actual
      "money in hand to spend" figure; the corrected version of what `net_income` incorrectly tries
      to be once withdrawing.
    - `"tax_attributable_to_ss"` / `"ss_after_tax_income"` / `"other_after_tax_income"` (2026-08-31,
      user request — the retirement-income chart's own SS-vs-other stacked breakout): a SECOND
      `compute_taxes` call, identical inputs except `ss_benefit_gross=0.0`, isolates the MARGINAL
      tax cost of Social Security specifically (same "second call with one source zeroed" technique
      `earned_only_tax`/`earned_income_tax` already use, Stage 2 above) — `tax_attributable_to_ss =
      total_tax - that_second_call["total_tax"]`, `ss_after_tax_income = ss_benefit_gross -
      tax_attributable_to_ss`, `other_after_tax_income = net_retirement_income -
      ss_after_tax_income`. Deliberately NOT a flat blended rate (`retirement_taxes_paid /
      total_withdrawal_income`) applied uniformly to `ss_benefit_gross` — SS's own taxability
      follows a different 0%/50%/85% provisional-income-tiered formula than the blended rate on
      total income, so a flat split would systematically mis-allocate the two bands most years.
      `ss_after_tax_income + other_after_tax_income == net_retirement_income` always, by
      construction — a real split of the existing total, not a new number.
    - `"discretionary_income"` (2026-08-14 fix) = `net_retirement_income - spending_need` — NOT the
      same thing as `funding_gap` above, a real, user-caught distinction, not a duplicate: unlike
      `funding_gap` (deliberately PRE-TAX — `withdrawal_target - spending_need`, §2.3's own "report
      the strategy's raw mismatch with need, before tax noise" diagnostic), `discretionary_income`
      is computed on the AFTER-TAX figure, so it can never exceed `net_retirement_income`
      (`spending_need` is never negative) — the constraint a "discretionary income" figure is
      supposed to satisfy. `funding_gap` genuinely CAN exceed `net_retirement_income` in a normal,
      non-partial year (e.g. a large Taxable-account LTCG realization drives a big tax bill relative
      to the withdrawal itself) — that isn't a bug in `funding_gap`, it's `funding_gap` correctly
      being a pre-tax number; the bug was ever treating it as if it meant "discretionary income."
      Use THIS field, never `funding_gap`, anywhere the UI displays "discretionary income."
    """
    income_rows = {
        r["year"]: r for r in project_gross_income(income_inputs, current_date, horizon_date, retirement_date)
    }
    expense_rows = {r["year"]: r for r in project_expenses(expense_inputs, current_date, horizon_date)}
    years = sorted(set(income_rows) | set(expense_rows))
    target_allocations = target_allocations or {}

    # ---- Date-ordering validation (2026-09-06, NEXT.md item B2) ----
    # Confirmed with the user: `withdrawal_start_date` may not precede EITHER Income end date
    # (`retirement_date`) or Savings stop date — same day as either (or both) is explicitly fine,
    # only strictly BEFORE raises. This is more than input hygiene: it's what structurally
    # guarantees `saving_fraction` reaches exactly 0 no later than the year `withdrawing_fraction`
    # turns positive, so Stage-2 voluntary contributions (which require `saving_fraction > 0`) and
    # an active withdrawal can never legitimately coincide — see item B5's own "withdrawing money
    # and then re-saving it" concern, structurally closed by this check rather than by extra gating
    # logic elsewhere. Raised here, once, up front — not per-year — since these three dates never
    # change across the loop below; propagates through the same `except ValueError` path
    # `ui/projection_tab.py` already uses for every other `project_multi_year` ValueError.
    if phase_dates is not None:
        withdrawal_start_date = phase_dates["withdrawal_start_date"]
        savings_stop_date = phase_dates["savings_stop_date"]
        if withdrawal_start_date < retirement_date or withdrawal_start_date < savings_stop_date:
            raise ValueError(
                f"Withdrawal start date ({withdrawal_start_date}) cannot be before Income end date "
                f"({retirement_date}) or Savings stop date ({savings_stop_date})."
            )

    # Portfolio roll-forward is only active when BOTH the universe (to look up each ticker's own
    # return/expense/dividend figures) and a starting lot ledger are supplied — omitting either
    # keeps every existing caller/test's exact original $0-investment-income behavior (see
    # docstring above).
    portfolio_active = universe is not None and initial_lots is not None
    current_lots: list[dict] = list(initial_lots) if initial_lots else []
    current_prices: dict[str, float] = dict(initial_prices_by_ticker) if initial_prices_by_ticker else {}

    results = []
    for year in years:
        inc = income_rows.get(year)
        exp = expense_rows.get(year)

        gross_w2 = inc["gross_w2"] if inc else 0.0
        gross_se = inc["gross_se"] if inc else 0.0
        gross_break = inc["gross_ordinary_break_income"] if inc else 0.0
        gross_expense = exp["gross_expense"] if exp else 0.0

        notes = list(inc["notes"]) if inc else []
        age_at_year_end = year - birth_date.year

        # ---- Phase fractions (MODEL_WIRING.md §2.5) — computed once per year, up front, since both
        # the coasting freeze immediately below and the contribution engine further down need them
        # (the latter used to recompute `saving_fraction` a second time via its own call to
        # `phase_flags_for_year` — now reuses this one instead). ----
        withdrawing_fraction = 0.0
        saving_fraction = 1.0  # no phase_dates supplied -> never coasting, always "accumulating"
        ss_fraction = 0.0  # no phase_dates supplied -> never claimed, matches ss_annual_benefit's own 0.0 default
        if phase_dates is not None:
            dates_for_phase = {"retirement_date": retirement_date, **phase_dates}
            year_phase_flags = phase_flags_for_year(year, dates_for_phase)
            withdrawing_fraction = year_phase_flags["withdrawing_fraction"]
            saving_fraction = year_phase_flags["saving_fraction"]
            ss_fraction = year_phase_flags["ss_fraction"]

        # Module F (2026-08-31) — the one fixed real-dollar figure (see this function's own
        # docstring), day-weighted for the claim year itself. `ss_fraction`'s own first real
        # consumer, per NEXT.md's own confirmed spec ("currently UNUSED anywhere downstream").
        ss_benefit_gross = ss_annual_benefit * ss_fraction

        # ---- Retirement Earnings Test (2026-09-06, NEXT.md item B3) ----
        # Only meaningful for a benefit actually being claimed this year (ss_benefit_gross > 0) --
        # applies to the withholding the SAME year the earnings happen, using `gross_w2`/`gross_se`
        # already resolved above (never `gross_investment_income` -- the RET explicitly doesn't
        # count investment income, per `retirement_earnings_test_reduction`'s own docstring).
        # `ss_bend_point_table is None` (every pre-existing caller/test that doesn't pass it) skips
        # this entirely -- $0 reduction, fully backward-compatible.
        ss_earnings_test_reduction = 0.0
        if ss_bend_point_table is not None and ss_benefit_gross > 0:
            ss_earnings_test_reduction = retirement_earnings_test_reduction(
                gross_w2, gross_se, birth_date, year, ss_bend_point_table
            )
            ss_benefit_gross = max(0.0, ss_benefit_gross - ss_earnings_test_reduction)

        # ---- Coasting-phase freeze REMOVED (2026-09-06, NEXT.md item B1) ----
        # WATERFALL_STREAMLINE_REDESIGN.md §3's original "freeze everything to $0 between
        # savings_stop_date and withdrawal_start_date" was explicitly documented as "§3.4's approach
        # (a), simplest, recommended for a first pass" — not a permanent design, and wrong whenever
        # Savings stop date falls BEFORE Income end date (real earned income continuing into the
        # coasting window was being discarded rather than becoming spendable surplus). Replaced by
        # `discretionary_spending` below: gross_w2/gross_se/gross_break/gross_expense now compute
        # normally for every year, regardless of phase, exactly like every other phase boundary in
        # this model (via saving_fraction/withdrawing_fraction themselves, not a separate override).
        # A year with Income end date already passed still naturally shows $0 earned income here —
        # via `project_gross_income`'s own retirement-date clipping, not this block.

        # ---- Portfolio roll-forward (Step 3, MODEL_WIRING.md §4) ----
        # Runs FIRST, against the ledger carried forward from the END of last year (or the initial
        # holdings, for the first year) — this year's OWN new contribution/reinvestment lots are
        # added only after the contribution decision below (see module docstring point 4), so they
        # never generate distribution income this same year (avoids circularity).

        # Captured BEFORE any of this year's own growth/dividends/contributions are applied — the
        # flat-percentage withdrawal strategy's own "portfolio_value_at_start_of_year" (§2.3).
        portfolio_value_at_start_of_year = portfolio_value(current_lots, current_prices) if portfolio_active else 0.0

        roll_result = None
        if portfolio_active:
            roll_result = roll_forward_portfolio(
                current_lots, universe, current_prices, inflation_rate, withdrawing_fraction
            )
            taxable_dist = roll_result["distribution_by_account_and_type"].get("Taxable", {})
            taxable_qualified = taxable_dist.get("qualified", 0.0)
            taxable_ordinary = taxable_dist.get("ordinary", 0.0)
            taxable_interest = taxable_dist.get("interest", 0.0)
        else:
            taxable_qualified = taxable_ordinary = taxable_interest = 0.0

        gross_investment_income = taxable_qualified + taxable_ordinary + taxable_interest
        # Module F (2026-08-31) -- ss_benefit_gross is real, spendable cash (net_income/profit
        # below would otherwise understate it by exactly this amount, even though total_tax
        # already reflects the 0%/50%/85% tax on it) -- folded in here alongside every other
        # income source, not bolted on separately downstream.
        gross_income = gross_w2 + gross_se + gross_break + gross_investment_income + ss_benefit_gross

        row = {
            "year": year,
            "years_from_now": year - current_date.year,
            "is_partial_year": (inc or exp)["is_partial_year"],
            "partial_year_fraction": (inc or exp)["partial_year_fraction"],
            "partial_year_reason": inc["partial_year_reason"] if inc else None,
            "gross_w2": gross_w2,
            "gross_se": gross_se,
            "gross_ordinary_break_income": gross_break,
            "ss_benefit_gross": ss_benefit_gross,  # Module F, 2026-08-31 -- see this function's docstring
            # 2026-09-06, NEXT.md item B3 -- the dollar amount ALREADY subtracted out of the
            # ss_benefit_gross above (not a separate, not-yet-applied figure) -- surfaced so the UI
            # can show why a benefit came back smaller than the raw formula would otherwise produce.
            # Always $0.0 (never None) when no reduction applies, matching ss_benefit_gross's own
            # always-populated convention.
            "ss_earnings_test_reduction": ss_earnings_test_reduction,
            "gross_investment_income": gross_investment_income if portfolio_active else None,
            "taxable_qualified_dividends": taxable_qualified if portfolio_active else None,
            "taxable_ordinary_dividends": taxable_ordinary if portfolio_active else None,
            "taxable_interest_income": taxable_interest if portfolio_active else None,
            "gross_income": gross_income,
            "gross_expense": gross_expense,
            "active_break_labels": inc["active_break_labels"] if inc else [],
            # RETIREMENT_REPORTING_AUDIT.md §2.4 (2026-08-13) — an explicit split point, not a proxy
            # inferred from `withdrawal_target > 0` (which is `None` pre-retirement and legitimately
            # `0.0` in a withdrawal-phase year with no actual draw — two different things worth
            # keeping distinct). Always populated (never `None`) regardless of `portfolio_active`,
            # same convention `withdrawing_fraction` itself already uses — `0.0`/`False` when
            # `phase_dates` wasn't supplied at all, not "unknown."
            "withdrawing_fraction": withdrawing_fraction,
            "is_withdrawal_year": withdrawing_fraction > 0,
            # Captured before this year's own growth/dividends/contributions/withdrawals are
            # applied (ADJUSTED_WEALTH_REDESIGN.md §4a, 2026-08-14) — lets the UI find "wealth at
            # the moment withdrawals begin" without re-deriving it from the (already rolled-forward)
            # `portfolio_value` of the prior row.
            "portfolio_value_at_start_of_year": portfolio_value_at_start_of_year if portfolio_active else None,
        }

        if not bracket_table:
            notes.append("No federal/CA tax bracket data configured at all — see data/tax_brackets.json.")
            row["tax_available"] = False
            row["bracket_year_used"] = None
            row["total_tax"] = None
            row["earned_income_tax"] = None
            row["tax_attributable_to_investment_income"] = None
            row["net_income"] = None
            row["profit"] = None
            row["discretionary_spending"] = None
            row["notes"] = notes
            row["contribution_warnings"] = []
            row["w2_401k_contribution_used"] = 0.0
            row["roth_401k_contribution_used"] = 0.0
            row["se_401k_employee_contribution_used"] = 0.0
            row["se_401k_employer_contribution_used"] = 0.0
            row["traditional_ira_contribution_used"] = 0.0
            row["roth_ira_contribution_used"] = 0.0
            row["employer_401k_match"] = 0.0
            row["contribution_destinations"] = None
            # No tax means no contribution decision this year, but the portfolio still rolls
            # forward (reinvestment only — no new contribution lots, since those depend on a
            # contribution resolution that itself depends on tax) so next year's ledger stays
            # consistent.
            if portfolio_active:
                current_lots = current_lots + [dict(lot, year_acquired=year) for lot in roll_result["new_lots"]]
                current_prices = roll_result["updated_prices_by_ticker"]
            row["portfolio_value"] = portfolio_value(current_lots, current_prices) if portfolio_active else None
            row["ending_lots"] = list(current_lots) if portfolio_active else None
            row["ending_prices_by_ticker"] = dict(current_prices) if portfolio_active else None
            row["rebalance_residual_drift"] = None
            # No tax available means no dissaving/withdrawal decision either -- these previously
            # weren't set at all on this branch (a latent KeyError risk for any caller indexing
            # them unconditionally, never hit in practice since bracket_table is essentially always
            # populated; fixed here while touching this exact code, MODEL_WIRING.md §6's own "no
            # dollar silently unaccounted for" spirit applied to the row schema itself).
            row["dissaving_proceeds"] = None
            row["dissaving_long_term_gain"] = None
            row["dissaving_short_term_gain"] = None
            row["dissaving_retirement_withdrawal"] = None
            row["dissaving_unfunded_shortfall"] = None
            row["dividend_used_for_expenses"] = None
            row["withdrawal_target"] = None
            row["spending_need"] = None
            row["funding_gap"] = None
            row["withdrawal_dividends_applied"] = None
            row["withdrawal_sale_proceeds"] = None
            row["withdrawal_long_term_gain"] = None
            row["withdrawal_short_term_gain"] = None
            row["withdrawal_taxable_retirement_distribution"] = None
            row["withdrawal_unfunded_shortfall"] = None
            row["withdrawal_unfunded_shortfall_by_bucket"] = None
            row["rmd_amount"] = None
            row["rmd_forced_excess"] = None
            row["withdrawal_iteration_count"] = None
            row["total_withdrawal_income"] = None
            row["retirement_taxes_paid"] = None
            row["net_retirement_income"] = None
            row["tax_attributable_to_ss"] = None
            row["ss_after_tax_income"] = None
            row["other_after_tax_income"] = None
            row["discretionary_income"] = None
            results.append(row)
            continue

        bracket_year = _bracket_year_for(year, bracket_table)
        if bracket_year != year:
            notes.append(
                f"No tax bracket data configured for {year} — held flat at {bracket_year}'s brackets "
                "(real-dollar model: rates/thresholds are assumed constant in real terms beyond the "
                "last configured year; see PROJECT_PLAN.md 'Tax methodology')."
            )

        contribution_warnings: list[str] = []
        contrib_config = (contribution_config_by_year or {}).get(year) or DEFAULT_CONTRIBUTION_CONFIG
        se_earnings = net_se_earnings_for_retirement(bracket_year, gross_w2, gross_se, bracket_table)

        def _tax_for(pretax_401k, roth_401k, se_employee, se_employer):
            return compute_taxes(
                tax_year=bracket_year,
                filing_status=filing_status,
                age_at_year_end=age_at_year_end,
                w2_gross=gross_w2,
                pretax_401k=pretax_401k,
                roth_401k=roth_401k,
                pretax_health_dental=0.0,
                se_net_profit=gross_se,
                se_solo_employee_deferral=se_employee,
                se_solo_employer_contribution=se_employer,
                taxable_retirement_withdrawal=0.0,
                ltcg=0.0,
                qualified_dividends=taxable_qualified,
                ordinary_dividends=taxable_ordinary,
                interest_income=taxable_interest,
                ss_benefit_gross=ss_benefit_gross,
                bracket_table=bracket_table,
                deferral_priority=deferral_priority,
                ordinary_break_income=gross_break,
            )

        # ---- Stage 1: 401(k)-family target — a payroll deduction, resolved directly from this
        # year's mode against real IRS limits, never gated on available cash (CONTRIBUTION_TOGGLE_
        # REDESIGN.md §1/§2, 2026-08-16 — see modules.contributions's own module docstring for the
        # full reasoning). ----
        target_401k = resolve_401k_target(
            mode=contrib_config.get("contribution_401k_mode", "custom"),
            custom_amount=contrib_config.get("contribution_401k_custom_amount", 0.0),
            custom_type=contrib_config.get("contribution_401k_custom_type", "pretax"),
            also_maximize_se_employer=contrib_config.get("maximize_se_employer_401k", False),
            tax_year=bracket_year,
            age_at_year_end=age_at_year_end,
            gross_w2=gross_w2,
            se_earnings=se_earnings,
            bracket_table=bracket_table,
        )
        w2_401k_used = target_401k["w2_pretax_target"]
        roth_401k_used = target_401k["w2_roth_target"]
        # SE solo 401(k) employee deferral has no pretax/Roth tax-treatment distinction in this
        # model's tax engine (compute_taxes has a single se_solo_employee_deferral parameter) — a
        # pre-existing limitation unrelated to and unchanged by this redesign; both targets sum
        # into one dollar figure here (see resolve_401k_target's own docstring).
        se_employee_used = target_401k["se_employee_pretax_target"] + target_401k["se_employee_roth_target"]
        se_employer_used = target_401k["se_employer_target"]
        # `target_401k["over_cap_warning"]` (2026-08-30, user request: removed from contribution_
        # warnings/the UI banner/Notes column) is confirmed auto-corrected — resolve_401k_target's
        # own w2_pretax_target/se_employee_pretax_target etc. above are ALREADY clamped to the
        # legal §402(g) maximum regardless of whether this warning fires, so surfacing it added no
        # information the math itself didn't already guarantee. See modules.contributions.
        # resolve_401k_target's own docstring for the (still-computed, still-tested) field itself.

        tax_result = _tax_for(w2_401k_used, roth_401k_used, se_employee_used, se_employer_used)
        employer_match = employer_401k_match(
            gross_w2, w2_401k_used + roth_401k_used, employer_match_rate, employer_match_cap_pct
        )

        # ---- tax_attributable_to_investment_income (2026-09-06 fix — see NEXT.md for the full bug
        # report): the SAME "second compute_taxes call with one source zeroed" isolation technique
        # `tax_attributable_to_ss` uses (below), applied here so the dividend/interest tax
        # `total_tax` already, correctly, bills for actually gets DEDUCTED from something instead of
        # vanishing. `roll_forward_portfolio` (above) reinvests a Taxable-account distribution at its
        # full GROSS amount — correct for tax-advantaged accounts (genuinely tax-deferred/tax-exempt
        # on reinvestment) but wrong for Taxable, where `total_tax` already reports a real, growing
        # liability on those same dollars that nothing ever actually paid. NOT the same call as
        # `earned_income_tax` below — that one also zeros `ss_benefit_gross`/`gross_break`, which
        # would contaminate this isolation with those effects too; this call holds everything else
        # (W-2, SE, this year's actual 401(k)-family contributions, SS, breaks) exactly as in the
        # real `tax_result`, differing only in the three investment-income parameters. Used below,
        # after this year's contribution decision, to scale down just the Taxable reinvestment lots
        # `roll_forward_portfolio` produced — 401(k)/IRA/Roth reinvestment lots are untouched.
        investment_income_zeroed_tax_result = compute_taxes(
            tax_year=bracket_year,
            filing_status=filing_status,
            age_at_year_end=age_at_year_end,
            w2_gross=gross_w2,
            pretax_401k=w2_401k_used,
            roth_401k=roth_401k_used,
            pretax_health_dental=0.0,
            se_net_profit=gross_se,
            se_solo_employee_deferral=se_employee_used,
            se_solo_employer_contribution=se_employer_used,
            taxable_retirement_withdrawal=0.0,
            ltcg=0.0,
            qualified_dividends=0.0,
            ordinary_dividends=0.0,
            interest_income=0.0,
            ss_benefit_gross=ss_benefit_gross,
            bracket_table=bracket_table,
            deferral_priority=deferral_priority,
            ordinary_break_income=gross_break,
        )
        tax_attributable_to_investment_income = tax_result["total_tax"] - investment_income_zeroed_tax_result["total_tax"]

        # ---- available_cash: earned-income-only, post-tax, post-expense, post-401(k)-deduction
        # figure Roth IRA/Traditional IRA/Taxable compete for in Stage 2 (CONTRIBUTION_TOGGLE_
        # REDESIGN.md §1: "tax is computed WITH that real deduction applied, then Roth/Traditional/
        # Taxable compete for what's actually left" — a deliberate improvement over the prior
        # waterfall's own $0-contribution baseline, which never credited back the real tax savings
        # a 401(k) deduction produces). Deliberately EXCLUDES investment income already auto-
        # reinvested by `roll_forward_portfolio` (step 1, above) — that is the portfolio's own
        # dedicated reinvestment mechanism; folding it in here would invest the same after-tax
        # dollar twice (WATERFALL_STREAMLINE_REDESIGN.md §2.3's double-counting trap, reused
        # verbatim: this figure is built from `gross_earned_income`, never `gross_income`). ----
        gross_earned_income = gross_w2 + gross_se + gross_break
        earned_only_tax = compute_taxes(
            tax_year=bracket_year,
            filing_status=filing_status,
            age_at_year_end=age_at_year_end,
            w2_gross=gross_w2,
            pretax_401k=w2_401k_used,
            roth_401k=roth_401k_used,
            pretax_health_dental=0.0,
            se_net_profit=gross_se,
            se_solo_employee_deferral=se_employee_used,
            se_solo_employer_contribution=se_employer_used,
            taxable_retirement_withdrawal=0.0,
            ltcg=0.0,
            qualified_dividends=0.0,
            ordinary_dividends=0.0,
            interest_income=0.0,
            # Module F (2026-08-31): deliberately STAYS $0.0 here, not the real ss_benefit_gross --
            # this call exists specifically to isolate EARNED income (gross_earned_income above
            # excludes it too, for the same reason), and Social Security is neither earned income
            # nor the investment income this isolation already excludes. Including it here would
            # feed available_cash a tax bill reflecting SS-driven bracket effects without the
            # matching income on the other side of net_income_earned_only, understating it.
            ss_benefit_gross=0.0,
            bracket_table=bracket_table,
            deferral_priority=deferral_priority,
            ordinary_break_income=gross_break,
        )
        contributed_401k_family = w2_401k_used + roth_401k_used + se_employee_used + se_employer_used
        net_income_earned_only = gross_earned_income - earned_only_tax["total_tax"] - contributed_401k_family
        profit_earned_only = net_income_earned_only - gross_expense
        available_cash = max(0.0, profit_earned_only) * saving_fraction
        # `discretionary_spending` (2026-09-06, NEXT.md item B1 — see this function's own docstring
        # for the full "replaces the coasting freeze" reasoning): the earned-income surplus that
        # ISN'T becoming a new Stage-2 contribution this year — algebraically `max(0.0,
        # profit_earned_only) * (1 - saving_fraction)`, but written this way (reusing
        # `available_cash` directly) so there is exactly one place computing "how much of the
        # earned surplus stays invested," not two formulas that could drift apart.
        discretionary_spending = max(0.0, profit_earned_only) - available_cash

        # ---- Stage 2: Roth IRA, then Traditional IRA, then Taxable — funded from available_cash
        # (CONTRIBUTION_TOGGLE_REDESIGN.md §3/§5). IRC §219(f)(1): $0 earned income means $0
        # combined IRA limit, full stop, regardless of MAGI/phase-out math — structurally built into
        # `resolve_roth_ira_target` via `combined_ira_limit` below, not a separate warning check. ----
        combined_ira_limit = (
            max_ira_contribution_limit(bracket_year, age_at_year_end, bracket_table)
            if gross_w2 + gross_se > 0
            else 0.0
        )
        magi = roth_ira_magi(tax_result["federal_agi"])
        roth_ira_target = resolve_roth_ira_target(
            mode=contrib_config.get("roth_ira_mode", "custom"),
            custom_amount=contrib_config.get("roth_ira_custom_amount", 0.0),
            tax_year=bracket_year,
            filing_status=filing_status,
            age_at_year_end=age_at_year_end,
            magi=magi,
            combined_ira_limit=combined_ira_limit,
            bracket_table=bracket_table,
        )
        traditional_ira_target = resolve_traditional_ira_target(
            mode=contrib_config.get("traditional_ira_mode", "custom"),
            custom_amount=contrib_config.get("traditional_ira_custom_amount", 0.0),
            combined_ira_limit=combined_ira_limit,
            roth_ira_used=roth_ira_target,
        )
        # (2026-08-30, user request: the old "Roth/Traditional IRA contribution exceeds this
        # year's real legal limit... Reduced to the legal maximum" warnings that used to be built
        # here are removed — confirmed auto-corrected: roth_ira_target/traditional_ira_target above
        # already reflect resolve_roth_ira_target's/resolve_traditional_ira_target's own clamping
        # to the legal max, MAGI phase-out, and §219(f)(1) earned-income gate, regardless of
        # whether a message fires, so the warning added no information the math didn't already
        # guarantee. A genuine cash shortfall in Stage 2 below is a separate, normal, expected
        # outcome, not something this removed check ever flagged anyway.)

        funded = fund_from_available_cash(roth_ira_target, traditional_ira_target, available_cash)
        roth_ira_used = funded["roth_ira_used"]
        traditional_ira_used = funded["traditional_ira_used"]
        taxable_used = funded["taxable_used"]
        contribution_destinations = {"taxable": taxable_used, "uninvested_surplus": 0.0}

        row["tax_available"] = True
        row["bracket_year_used"] = bracket_year
        # net_income/profit correctly subtract the 401(k)-family dollars actually deferred this
        # year (payroll-style, never liquid cash to begin with) — NOT employer match (never the
        # employee's money), NOT IRA/Taxable (funded FROM this already-liquid surplus, not a
        # further reduction to it). See this function's own docstring for the full reasoning.
        row["net_income"] = gross_income - tax_result["total_tax"] - contributed_401k_family
        row["profit"] = row["net_income"] - gross_expense
        row["discretionary_spending"] = discretionary_spending
        row["notes"] = notes
        row["contribution_warnings"] = contribution_warnings
        row["w2_401k_contribution_used"] = w2_401k_used
        row["roth_401k_contribution_used"] = roth_401k_used
        row["se_401k_employee_contribution_used"] = se_employee_used
        row["se_401k_employer_contribution_used"] = se_employer_used
        row["traditional_ira_contribution_used"] = traditional_ira_used
        row["roth_ira_contribution_used"] = roth_ira_used
        row["employer_401k_match"] = employer_match
        row["contribution_destinations"] = contribution_destinations
        row.update(tax_result)
        row["tax_attributable_to_investment_income"] = (
            tax_attributable_to_investment_income if portfolio_active else None
        )

        # `earned_income_tax` (2026-08-15, user request) — the tax owed on gross_w2 + gross_se
        # ALONE, using this row's own ACTUAL 401(k)-family contributions (which do reduce earned-
        # income tax) but with investment income (dividends/interest) and ordinary break income
        # zeroed out — the same "isolate earned income from investment income" technique already
        # used for `available_cash` above, generalized here into a field every row carries so
        # callers (the "Pre-retirement: where gross income goes" chart) can build an earned-income-
        # only breakdown without re-deriving tax logic of
        # their own outside modules/projection.py (CLAUDE.md's "calculation logic lives in
        # modules/*.py" rule). Never fed back into `total_tax`/`net_income`/`profit` — those
        # correctly stay investment-income-inclusive.
        earned_income_tax_result = compute_taxes(
            tax_year=bracket_year,
            filing_status=filing_status,
            age_at_year_end=age_at_year_end,
            w2_gross=gross_w2,
            pretax_401k=w2_401k_used,
            roth_401k=roth_401k_used,
            pretax_health_dental=0.0,
            se_net_profit=gross_se,
            se_solo_employee_deferral=se_employee_used,
            se_solo_employer_contribution=se_employer_used,
            taxable_retirement_withdrawal=0.0,
            ltcg=0.0,
            qualified_dividends=0.0,
            ordinary_dividends=0.0,
            interest_income=0.0,
            # Module F (2026-08-31): deliberately STAYS $0.0 -- this field's own purpose is
            # "the tax owed on gross_w2 + gross_se ALONE" (see the comment above); Social Security
            # isn't earned income, so including it here would corrupt that isolation the same way
            # investment income and break income are already excluded, just above.
            ss_benefit_gross=0.0,
            bracket_table=bracket_table,
            deferral_priority=deferral_priority,
            ordinary_break_income=0.0,
        )
        row["earned_income_tax"] = earned_income_tax_result["total_tax"]

        # ---- Dissaving (Step 4, MODEL_WIRING.md §5.1 point 1) ----
        # Gated on `year < retirement_date.year` specifically -- NOT withdrawing_fraction/
        # saving_fraction -- per §5.1's own instruction (see this function's own docstring). Also
        # gated on `withdrawing_fraction <= 0` (added in Step 6) so a year is never BOTH dissaving-
        # eligible and in the formal withdrawal phase, even in the unusual case where
        # `withdrawal_start_date` is configured earlier than `retirement_date` -- the withdrawal
        # mechanism below takes over entirely once withdrawing_fraction > 0 (now structurally
        # impossible to overlap with an active Stage-2 contribution either, once B2's date-ordering
        # validation above holds — see this function's own docstring).
        #
        # 2026-09-06 fix (NEXT.md item B4/B5, a real double-counting bug): this block used to gate
        # itself on `row["profit"] < 0` -- but `row["profit"]` is investment-income-INCLUSIVE, so a
        # dividend/interest amount large enough to cover an earned-income shortfall on its own made
        # `profit >= 0` and skipped this block entirely, while that SAME dividend cash, completely
        # separately, still unconditionally reinvested in full a few hundred lines below
        # (`roll_result["new_lots"]`) -- the identical dollar counted as both "spent on expenses"
        # (by avoiding a sale) and "bought new shares" (by reinvesting) in the same year. Fixed the
        # same way the withdrawal phase already nets dividends against its own target correctly
        # (`dividends_applied`, below): net the shortfall against EARNED income alone FIRST
        # (`profit_earned_only`, already computed above, investment-income-EXCLUDED by
        # construction), use dividend cash only for whatever earned income alone doesn't cover, and
        # scale down that SAME dividend's own reinvestment lots by exactly that much (see the
        # "Taxable-account reinvestment" comment near `roll_result["new_lots"]`, below -- composed
        # AFTER that section's own investment-income-tax deduction, not instead of it). The OUTER
        # gate (this year, pre-retirement, not withdrawing) is UNCHANGED from before this fix; only
        # the inner "was there really a shortfall, and how much of it did dividends already cover"
        # logic is new.
        dissaving_sale = None
        dividend_used_for_expenses = 0.0
        if portfolio_active and year < retirement_date.year and withdrawing_fraction <= 0:
            earned_shortfall = max(0.0, -profit_earned_only)
            dividend_used_for_expenses = min(gross_investment_income, earned_shortfall)
            remaining_shortfall = max(0.0, earned_shortfall - dividend_used_for_expenses)

            if remaining_shortfall > 0:
                dissaving_sale = draw_order_fill(
                    remaining_shortfall, current_lots, current_prices, year, draw_order=draw_order, sale_method=sale_method
                )
                current_lots = dissaving_sale["remaining_lots"]

                dissaving_long_term_gain = 0.0
                dissaving_short_term_gain = 0.0
                dissaving_retirement_withdrawal = 0.0
                for sold_account_type, sale in dissaving_sale["sales_by_account_type"].items():
                    if sold_account_type == "Taxable":
                        dissaving_long_term_gain += sale["long_term_gain"]
                        dissaving_short_term_gain += sale["short_term_gain"]
                    elif sold_account_type in ("Traditional 401(k)", "Traditional IRA"):
                        dissaving_retirement_withdrawal += sale["total_proceeds"]
                    # Roth 401(k)/Roth IRA/HSA sales: $0 taxable, nothing added (§5.2's own table).

                if dissaving_long_term_gain or dissaving_short_term_gain or dissaving_retirement_withdrawal:
                    # A real, SECOND compute_taxes pass reflecting what actually got realized this
                    # year -- a deliberate one-pass approximation, not an iterated solve; see this
                    # function's own docstring for why (MODEL_WIRING.md §5.3 Stage 1 vs Stage 2).
                    dissaving_tax_result = compute_taxes(
                        tax_year=bracket_year,
                        filing_status=filing_status,
                        age_at_year_end=age_at_year_end,
                        w2_gross=gross_w2,
                        pretax_401k=w2_401k_used,
                        roth_401k=roth_401k_used,
                        pretax_health_dental=0.0,
                        se_net_profit=gross_se,
                        se_solo_employee_deferral=se_employee_used,
                        se_solo_employer_contribution=se_employer_used,
                        taxable_retirement_withdrawal=dissaving_retirement_withdrawal,
                        ltcg=dissaving_long_term_gain,
                        qualified_dividends=taxable_qualified,
                        ordinary_dividends=taxable_ordinary,
                        interest_income=taxable_interest,
                        ss_benefit_gross=ss_benefit_gross,
                        bracket_table=bracket_table,
                        deferral_priority=deferral_priority,
                        ordinary_break_income=gross_break,
                        short_term_gains=dissaving_short_term_gain,
                    )
                    gross_income += dissaving_retirement_withdrawal + dissaving_long_term_gain + dissaving_short_term_gain
                    row["gross_income"] = gross_income
                    row.update(dissaving_tax_result)
                    row["net_income"] = gross_income - dissaving_tax_result["total_tax"] - contributed_401k_family
                    row["profit"] = row["net_income"] - gross_expense

                row["dissaving_proceeds"] = dissaving_sale["total_proceeds"]
                row["dissaving_long_term_gain"] = dissaving_long_term_gain
                row["dissaving_short_term_gain"] = dissaving_short_term_gain
                row["dissaving_retirement_withdrawal"] = dissaving_retirement_withdrawal
                row["dissaving_unfunded_shortfall"] = dissaving_sale["unfunded_shortfall"]
            else:
                row["dissaving_proceeds"] = 0.0
                row["dissaving_long_term_gain"] = 0.0
                row["dissaving_short_term_gain"] = 0.0
                row["dissaving_retirement_withdrawal"] = 0.0
                row["dissaving_unfunded_shortfall"] = 0.0
        else:
            row["dissaving_proceeds"] = 0.0 if portfolio_active else None
            row["dissaving_long_term_gain"] = 0.0 if portfolio_active else None
            row["dissaving_short_term_gain"] = 0.0 if portfolio_active else None
            row["dissaving_retirement_withdrawal"] = 0.0 if portfolio_active else None
            row["dissaving_unfunded_shortfall"] = 0.0 if portfolio_active else None
        row["dividend_used_for_expenses"] = dividend_used_for_expenses if portfolio_active else None

        # ---- Retirement withdrawal (Step 6, MODEL_WIRING.md §2.3 / §5.1 point 2;
        # MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1, 2026-08-23) ----
        # Mutually exclusive with dissaving above (see that block's own gate). The withdrawal
        # TARGET is the strategy's own output, deliberately never reconciled against spending_need
        # -- see this function's own docstring for why (§2.3's explicit "report the gap, don't
        # conflate them" instruction).
        withdrawal_sale = None
        rmd_forced_excess_realized = 0.0
        if portfolio_active and withdrawing_fraction > 0:
            params = withdrawal_strategy_params if withdrawal_strategy_params is not None else {"rate": 0.04}

            # ---- Everything below that does NOT depend on the withdrawal amount itself -- computed
            # once, outside the (possibly-iterated) simulation closure. balances/traditional_
            # balance_at_start_of_year are read from current_lots/current_prices BEFORE this year's
            # own sale -- unchanged since portfolio_value_at_start_of_year was captured above (this
            # branch and dissaving's are mutually exclusive, so nothing has mutated them yet). ----
            balances = portfolio_value_by_account_type(current_lots, current_prices)
            traditional_balance_at_start_of_year = (
                balances["Traditional 401(k)"] + balances["Traditional IRA"]
            )
            taxable_balance = balances["Taxable"]

            # Non-discretionary ordinary income already locked in this year, independent of the
            # withdrawal decision itself. ss_taxable placeholder is 0.0 until Module F (Social
            # Security) exists -- named here, in ONE place, rather than hardcoded at multiple call
            # sites, so Module F's eventual landing is a one-line change, not a re-thread (Part 1's
            # own "Known gap, honestly carried forward").
            ss_taxable_amount_placeholder = 0.0
            other_ordinary_income = taxable_ordinary + taxable_interest + gross_break + ss_taxable_amount_placeholder
            target_rate = (params or {}).get("target_bracket_rate", 0.22)
            bracket_year_federal_brackets = bracket_table[bracket_year]["federal_brackets"][filing_status]
            ceiling = ordinary_bracket_ceiling(bracket_year_federal_brackets, target_rate)
            traditional_headroom = max(0.0, ceiling - other_ordinary_income)

            # RMD floor amount itself doesn't depend on the withdrawal target -- only how much of it
            # ends up forced ABOVE the target does (computed inside the simulation below, per guess).
            rmd_amount = 0.0
            if rmd_table is not None and age_at_year_end >= rmd_start_age(birth_date.year):
                divisor = rmd_divisor(age_at_year_end, rmd_table)
                rmd_amount = traditional_balance_at_start_of_year / divisor if divisor > 0 else 0.0

            base_gross_income = gross_income
            base_total_tax = row["total_tax"]
            base_net_income = row["net_income"]
            base_profit = row["profit"]

            def _simulate_withdrawal(candidate_target: float) -> dict:
                """
                Runs ONE full pass of Part 1's bracket-aware split + sale + (if anything was
                realized) a fresh `compute_taxes` call, for a given candidate withdrawal amount --
                against the SAME starting `current_lots`/`current_prices` every call (neither is
                mutated here; `bracket_aware_draw`/`draw_order_fill`/`sell_lots` all return new lists
                rather than mutating their input), so calling this more than once per year (Module G2
                Part 2's fixed-point iteration, below) never double-sells. The caller commits exactly
                one of these results (the converged one) to `current_lots`/`row` afterward.
                """
                dividends_available = taxable_qualified + taxable_ordinary + taxable_interest
                dividends_applied = min(candidate_target, dividends_available)
                amount_needed = max(0.0, candidate_target - dividends_available)

                trad_target = min(amount_needed, traditional_headroom, traditional_balance_at_start_of_year)
                remaining_after_trad = amount_needed - trad_target
                tax_target = min(remaining_after_trad, taxable_balance)
                r_target = remaining_after_trad - tax_target

                # RMD floor (Part 1 §"RMDs") -- enforced on the Traditional target, never a ceiling.
                rmd_excess_target = max(0.0, min(rmd_amount, traditional_balance_at_start_of_year) - trad_target)
                trad_target_with_rmd = trad_target + rmd_excess_target

                long_term_gain = 0.0
                short_term_gain = 0.0
                taxable_retirement_distribution = 0.0
                shortfall_by_bucket = {"traditional": 0.0, "taxable": 0.0, "roth": 0.0}
                sale_result = None
                excess_realized = 0.0
                if trad_target_with_rmd > 0 or tax_target > 0 or r_target > 0:
                    sale_result = bracket_aware_draw(
                        trad_target_with_rmd, tax_target, r_target, current_lots, current_prices, year,
                        sale_method=sale_method,
                    )
                    shortfall_by_bucket = sale_result["unfunded_shortfall_by_bucket"]
                    traditional_proceeds = 0.0
                    for sold_account_type, sale in sale_result["sales_by_account_type"].items():
                        if sold_account_type == "Taxable":
                            long_term_gain += sale["long_term_gain"]
                            short_term_gain += sale["short_term_gain"]
                        elif sold_account_type in ("Traditional 401(k)", "Traditional IRA"):
                            taxable_retirement_distribution += sale["total_proceeds"]
                            traditional_proceeds += sale["total_proceeds"]
                        # Roth 401(k)/Roth IRA/HSA sales: $0 taxable, nothing added (§5.2's own table).
                    # How much of what was ACTUALLY sold from Traditional represents the RMD's forced
                    # excess beyond genuine spending need -- reinvested into Taxable, not counted as
                    # spendable income.
                    excess_realized = max(0.0, traditional_proceeds - trad_target)

                final_gross_income = base_gross_income
                tax_result_local = None
                final_net_income = base_net_income
                final_profit = base_profit
                final_total_tax = base_total_tax
                if long_term_gain or short_term_gain or taxable_retirement_distribution:
                    # Same real, SECOND compute_taxes pass / one-pass-approximation precedent as
                    # dissaving above.
                    tax_result_local = compute_taxes(
                        tax_year=bracket_year,
                        filing_status=filing_status,
                        age_at_year_end=age_at_year_end,
                        w2_gross=gross_w2,
                        pretax_401k=w2_401k_used,
                        roth_401k=roth_401k_used,
                        pretax_health_dental=0.0,
                        se_net_profit=gross_se,
                        se_solo_employee_deferral=se_employee_used,
                        se_solo_employer_contribution=se_employer_used,
                        taxable_retirement_withdrawal=taxable_retirement_distribution,
                        ltcg=long_term_gain,
                        qualified_dividends=taxable_qualified,
                        ordinary_dividends=taxable_ordinary,
                        interest_income=taxable_interest,
                        ss_benefit_gross=ss_benefit_gross,
                        bracket_table=bracket_table,
                        deferral_priority=deferral_priority,
                        ordinary_break_income=gross_break,
                        short_term_gains=short_term_gain,
                    )
                    final_gross_income = (
                        base_gross_income + taxable_retirement_distribution + long_term_gain + short_term_gain
                    )
                    final_total_tax = tax_result_local["total_tax"]
                    final_net_income = final_gross_income - final_total_tax - contributed_401k_family
                    final_profit = final_net_income - gross_expense

                sale_proceeds = sale_result["total_proceeds"] if sale_result else 0.0
                total_withdrawal_income_local = dividends_applied + sale_proceeds - excess_realized
                net_retirement_income_local = total_withdrawal_income_local - final_total_tax

                return {
                    "withdrawal_dividends_applied": dividends_applied,
                    "withdrawal_sale": sale_result,
                    "withdrawal_long_term_gain": long_term_gain,
                    "withdrawal_short_term_gain": short_term_gain,
                    "withdrawal_taxable_retirement_distribution": taxable_retirement_distribution,
                    "unfunded_shortfall_by_bucket": shortfall_by_bucket,
                    "rmd_forced_excess_realized": excess_realized,
                    "gross_income": final_gross_income,
                    "tax_result": tax_result_local,
                    "net_income": final_net_income,
                    "profit": final_profit,
                    "total_tax": final_total_tax,
                    "net_retirement_income": net_retirement_income_local,
                }

            withdrawal_iteration_count = None
            if withdrawal_strategy == "target_net_spending":
                # ---- Module G2 Part 2 (MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md) -- dynamic
                # sizing that actually CLOSES funding_gap, via fixed-point iteration, instead of the
                # flat rule's "report the gap, don't reconcile it." Start from the naive guess
                # (spending_need itself, via annual_withdrawal_target's own "target_net_spending"
                # branch), run one full bracket-aware split + tax pass, compare net_retirement_income
                # against spending_need, and adjust the guess by exactly that gap -- since a dollar
                # of extra pre-tax withdrawal always raises net income by LESS than a dollar (the
                # marginal tax on it), this simple update is a genuine contraction (each step's own
                # error shrinks by roughly the marginal tax rate), not an arbitrary heuristic. Capped
                # at 20 iterations; non-convergence is surfaced as a warning (same mechanism the
                # contribution-limit checks already use), never silently accepted as "close enough."
                guess = annual_withdrawal_target(
                    withdrawal_strategy, params, {"portfolio_value": portfolio_value_at_start_of_year,
                                                    "spending_need": gross_expense}
                )
                sim = None
                converged = False
                for iteration in range(1, 21):
                    sim = _simulate_withdrawal(guess)
                    gap = gross_expense - sim["net_retirement_income"]
                    new_guess = max(0.0, guess + gap)
                    if abs(new_guess - guess) < 1.0:
                        withdrawal_iteration_count = iteration
                        converged = True
                        break
                    guess = new_guess
                if not converged:
                    withdrawal_iteration_count = 20
                    contribution_warnings.append(
                        f"Dynamic withdrawal sizing did not converge within 20 iterations in {year} "
                        f"(last guess ${guess:,.0f} vs. spending need ${gross_expense:,.0f}) — using "
                        "the final iteration's result rather than looping further."
                    )
                withdrawal_target = guess
            else:
                withdrawal_target = annual_withdrawal_target(
                    withdrawal_strategy, params, {"portfolio_value": portfolio_value_at_start_of_year}
                )
                sim = _simulate_withdrawal(withdrawal_target)

            current_lots = sim["withdrawal_sale"]["remaining_lots"] if sim["withdrawal_sale"] else current_lots
            withdrawal_sale = sim["withdrawal_sale"]
            rmd_forced_excess_realized = sim["rmd_forced_excess_realized"]
            gross_income = sim["gross_income"]
            row["gross_income"] = gross_income
            if sim["tax_result"] is not None:
                row.update(sim["tax_result"])
            row["net_income"] = sim["net_income"]
            row["profit"] = sim["profit"]

            row["withdrawal_target"] = withdrawal_target
            row["spending_need"] = gross_expense
            row["funding_gap"] = withdrawal_target - gross_expense
            row["withdrawal_dividends_applied"] = sim["withdrawal_dividends_applied"]
            row["withdrawal_sale_proceeds"] = withdrawal_sale["total_proceeds"] if withdrawal_sale else 0.0
            row["withdrawal_long_term_gain"] = sim["withdrawal_long_term_gain"]
            row["withdrawal_short_term_gain"] = sim["withdrawal_short_term_gain"]
            row["withdrawal_taxable_retirement_distribution"] = sim["withdrawal_taxable_retirement_distribution"]
            row["withdrawal_unfunded_shortfall"] = withdrawal_sale["unfunded_shortfall"] if withdrawal_sale else 0.0
            row["withdrawal_unfunded_shortfall_by_bucket"] = sim["unfunded_shortfall_by_bucket"]
            row["rmd_amount"] = rmd_amount
            row["rmd_forced_excess"] = rmd_forced_excess_realized
            row["withdrawal_iteration_count"] = withdrawal_iteration_count
        else:
            row["withdrawal_target"] = 0.0 if portfolio_active else None
            row["spending_need"] = 0.0 if portfolio_active else None
            row["funding_gap"] = 0.0 if portfolio_active else None
            row["withdrawal_dividends_applied"] = 0.0 if portfolio_active else None
            row["withdrawal_sale_proceeds"] = 0.0 if portfolio_active else None
            row["withdrawal_long_term_gain"] = 0.0 if portfolio_active else None
            row["withdrawal_short_term_gain"] = 0.0 if portfolio_active else None
            row["withdrawal_taxable_retirement_distribution"] = 0.0 if portfolio_active else None
            row["withdrawal_unfunded_shortfall_by_bucket"] = (
                {"traditional": 0.0, "taxable": 0.0, "roth": 0.0} if portfolio_active else None
            )
            row["rmd_amount"] = 0.0 if portfolio_active else None
            row["rmd_forced_excess"] = 0.0 if portfolio_active else None
            row["withdrawal_iteration_count"] = None
            row["withdrawal_unfunded_shortfall"] = 0.0 if portfolio_active else None

        # ---- Retirement-income reporting (RETIREMENT_REPORTING_AUDIT.md §2.3, 2026-08-13) ----
        # `gross_income`/`net_income`/`profit` are reused across the withdrawal boundary but mean
        # something different on each side (confirmed defect, §2.2): pre-retirement they're the
        # ordinary IRS gross/net-earned-income constructs; once withdrawing, they only ever capture
        # the TAXABLE slice of that year's withdrawal — a Roth-only withdrawal year shows
        # gross_income/net_income of exactly $0 despite real spendable cash having been raised. These
        # three new fields are the genuinely correct "money in hand to spend" figures for a
        # withdrawal-phase year, computed here (not the UI) since "what does this number mean" is a
        # modules/ concern per CLAUDE.md ground rule 4, and a future consumer other than this one UI
        # tab may need them.
        if portfolio_active and row["is_withdrawal_year"]:
            # Total cash actually raised from the portfolio this year for spending, across EVERY
            # account type (Traditional, Roth, Taxable, HSA) — not just the taxable portion. Excludes
            # `rmd_forced_excess` (MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1, 2026-08-23):
            # a forced-above-target RMD sale is reinvested into Taxable (dollars_by_account_type,
            # below), never actually spent, so counting it here would inflate "money in hand to
            # spend" with cash that immediately went right back into the portfolio. Still fully taxed
            # (see `withdrawal_taxable_retirement_distribution`/`retirement_taxes_paid` below, which
            # DO include it) — the RMD's real cost (tax on money you didn't need) still shows up as
            # lower `net_retirement_income`, correctly.
            # Module F (2026-08-31) resolves the CAVEAT this comment used to carry: ss_benefit_gross
            # is real spendable cash too (the gross benefit arrives in hand; federal/CA tax on it is
            # paid separately, already correctly folded into total_tax/retirement_taxes_paid below)
            # — excluding it here would understate "money in hand to spend" by the full benefit
            # while still correctly charging its tax, silently deflating net_retirement_income.
            row["total_withdrawal_income"] = (
                row["withdrawal_dividends_applied"] + row["withdrawal_sale_proceeds"]
                - row["rmd_forced_excess"] + row["ss_benefit_gross"]
            )
            # During pure retirement (no W-2/SE/break income) total_tax is already fully
            # attributable to investment income + realized gains + Traditional distributions + the
            # Social Security benefit above, so no new tax computation is needed here — just a
            # clearer name in this context.
            row["retirement_taxes_paid"] = row["total_tax"]
            row["net_retirement_income"] = row["total_withdrawal_income"] - row["retirement_taxes_paid"]

            # ---- SS vs. other after-tax split (2026-08-31, user request — the retirement-income
            # chart's own stacked-area breakout) ----
            # Isolates the MARGINAL tax cost of the Social Security benefit specifically, holding
            # every other income source this year fixed — same "second compute_taxes call with one
            # source zeroed" isolation technique already used for `earned_only_tax`/`available_cash`
            # (Stage 2, above) and `earned_income_tax` — reused here rather than a flat blended rate
            # (`retirement_taxes_paid / total_withdrawal_income`) applied uniformly, since SS
            # taxability follows its own 0%/50%/85% provisional-income formula, not the blended
            # rate on total income; a flat-rate split would systematically mis-allocate the two
            # bands in most years. Uses the EXACT SAME realized-income mix that produced this row's
            # own `total_tax` above (whichever of the base or the withdrawal's own second-pass
            # compute_taxes call actually won) — only `ss_benefit_gross` differs.
            zero_ss_tax_result = compute_taxes(
                tax_year=bracket_year,
                filing_status=filing_status,
                age_at_year_end=age_at_year_end,
                w2_gross=gross_w2,
                pretax_401k=w2_401k_used,
                roth_401k=roth_401k_used,
                pretax_health_dental=0.0,
                se_net_profit=gross_se,
                se_solo_employee_deferral=se_employee_used,
                se_solo_employer_contribution=se_employer_used,
                taxable_retirement_withdrawal=row["withdrawal_taxable_retirement_distribution"],
                ltcg=row["withdrawal_long_term_gain"],
                qualified_dividends=taxable_qualified,
                ordinary_dividends=taxable_ordinary,
                interest_income=taxable_interest,
                ss_benefit_gross=0.0,
                bracket_table=bracket_table,
                deferral_priority=deferral_priority,
                ordinary_break_income=gross_break,
                short_term_gains=row["withdrawal_short_term_gain"],
            )
            row["tax_attributable_to_ss"] = row["total_tax"] - zero_ss_tax_result["total_tax"]
            row["ss_after_tax_income"] = row["ss_benefit_gross"] - row["tax_attributable_to_ss"]
            row["other_after_tax_income"] = row["net_retirement_income"] - row["ss_after_tax_income"]

            # `discretionary_income` (2026-08-14 fix) — NOT the same thing as `funding_gap` below,
            # a real distinction, not a rename. `funding_gap = withdrawal_target - spending_need` is
            # a deliberately PRE-TAX diagnostic (§2.3's own "report the strategy's raw mismatch with
            # need, honestly, before tax noise") — but it can therefore come out LARGER than
            # `net_retirement_income` whenever this year's realized-gain tax bill is large relative
            # to the withdrawal (a big Taxable-account LTCG realization, for instance): the withdrawal
            # TARGET counts the gross, pre-tax dollars while `net_retirement_income` already has tax
            # subtracted, so "excess over need" measured on the gross side isn't bounded by the net
            # side at all. A true "discretionary income" (money genuinely left over to spend after
            # both tax AND need are accounted for) has to be computed on the AFTER-TAX figure:
            # `net_retirement_income - spending_need`, which by construction can never exceed
            # `net_retirement_income` (spending_need is never negative) — the constraint the name
            # itself implies. Use THIS field, not `funding_gap`, anywhere "discretionary income" is
            # displayed.
            row["discretionary_income"] = row["net_retirement_income"] - row["spending_need"]
        else:
            row["total_withdrawal_income"] = None
            row["retirement_taxes_paid"] = None
            row["net_retirement_income"] = None
            row["tax_attributable_to_ss"] = None
            row["ss_after_tax_income"] = None
            row["other_after_tax_income"] = None
            row["discretionary_income"] = None

        if portfolio_active:
            # New contribution dollars this year, by ACCOUNT TYPE (modules.portfolio.ACCOUNT_TYPES)
            # — same construction the UI's own "Contribution destinations & lots" expander already
            # uses (ui/projection_tab.py), now folded in here so the carried-forward ledger actually
            # includes new purchases, not just existing holdings. HSA is omitted: no HSA capacity is
            # modeled yet (see modules/investing.py), so nothing ever routes dollars there.
            dollars_by_account_type = {
                "Traditional 401(k)": w2_401k_used + se_employee_used + se_employer_used + employer_match,
                "Roth 401(k)": roth_401k_used,
                "Roth IRA": roth_ira_used,
                "Traditional IRA": traditional_ira_used,
                # `rmd_forced_excess_realized` (MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1,
                # 2026-08-23) — a forced-above-target RMD sale, already taxed as ordinary income
                # above, gets reinvested here exactly like a contribution rather than vanishing or
                # inflating total_withdrawal_income with cash that was never actually needed for
                # spending. $0.0 in every year without an RMD floor active (the default).
                "Taxable": (contribution_destinations or {}).get("taxable", 0.0) + rmd_forced_excess_realized,
            }
            contribution_lots = create_lots(dollars_by_account_type, target_allocations, current_prices, year)

            # `tax_attributable_to_investment_income` fix (2026-09-06 — see NEXT.md): a Taxable
            # holding's distribution reinvests at its full GROSS amount by construction
            # (`roll_forward_portfolio` has no opinion on taxes — see that function's own
            # docstring), but `total_tax` above already, correctly, bills real tax on that same
            # dividend/interest income. Left alone, that tax is reported but never actually paid
            # from anywhere — a Taxable account would silently compound tax-free forever. Scale
            # down ONLY the Taxable reinvestment lots by the after-tax fraction of this year's
            # Taxable distribution; 401(k)/IRA/Roth reinvestment lots are genuinely tax-deferred/
            # tax-exempt on reinvestment and stay untouched.
            after_tax_investment_income_fraction = 1.0
            if gross_investment_income > 0:
                after_tax_investment_income_fraction = max(
                    0.0, 1.0 - (tax_attributable_to_investment_income / gross_investment_income)
                )
                if tax_attributable_to_investment_income > gross_investment_income:
                    notes.append(
                        "Tax attributable to this year's Taxable-account investment income "
                        f"(${tax_attributable_to_investment_income:,.2f}) exceeded the investment "
                        f"income itself (${gross_investment_income:,.2f}) — an extreme bracket-"
                        "stacking edge case. Taxable reinvestment clamped to $0 rather than "
                        "reinvesting a negative amount; the shortfall isn't covered by selling "
                        "other Taxable lots (a documented v2 gap, see NEXT.md)."
                    )

            # `dividend_used_for_expenses` fix (2026-09-06 — NEXT.md item B4/B5, see the dissaving
            # block above for the full bug writeup): composed AFTER the tax deduction above, not
            # instead of it — a Taxable dividend this year nets down by (a) the tax it triggered,
            # then (b) whatever portion the dissaving block needed to cover an earned-income
            # shortfall, and only what's left after BOTH actually buys new shares.
            # `dividend_used_for_expenses` is bounded by the FULL gross dividend (see that block's
            # own comment), so this can drive the remaining fraction below zero when tax + shortfall
            # together exceed the after-tax dividend — clamped at $0, same "don't reinvest a
            # negative amount" handling as the tax-only case immediately above.
            final_investment_income_fraction = after_tax_investment_income_fraction
            if gross_investment_income > 0 and dividend_used_for_expenses > 0:
                after_tax_investment_income = gross_investment_income * after_tax_investment_income_fraction
                remaining_investment_income = max(0.0, after_tax_investment_income - dividend_used_for_expenses)
                final_investment_income_fraction = remaining_investment_income / gross_investment_income

            reinvestment_lots = [
                dict(lot, year_acquired=year, shares=lot["shares"] * final_investment_income_fraction)
                if lot["account_type"] == "Taxable"
                else dict(lot, year_acquired=year)
                for lot in roll_result["new_lots"]
            ]
            current_lots = current_lots + reinvestment_lots + contribution_lots
            current_prices = roll_result["updated_prices_by_ticker"]

            # ---- Rebalancing (Step 5, MODEL_WIRING.md §7) ----
            # Runs LAST, after this year's contributions and any dissaving sale are already
            # reflected in current_lots — tax-advantaged account types only (Taxable is never
            # rebalanced by selling; see modules.investing.REBALANCEABLE_ACCOUNT_TYPES). Net-zero-
            # cash, zero-tax-consequence trades, so nothing here touches total_tax/profit above.
            if rebalance:
                rebalance_residual_drift: dict[str, float] = {}
                lots_by_account_type: dict[str, list[dict]] = {}
                for lot in current_lots:
                    lots_by_account_type.setdefault(lot["account_type"], []).append(lot)
                rebalanced_lots = [
                    lot for lot in current_lots if lot["account_type"] not in REBALANCEABLE_ACCOUNT_TYPES
                ]
                for account_type in REBALANCEABLE_ACCOUNT_TYPES:
                    account_lots = lots_by_account_type.get(account_type, [])
                    weights = target_allocations.get(account_type, {})
                    account_result = rebalance_account(
                        account_lots, account_type, weights, current_prices, year, rebalance_band
                    )
                    rebalanced_lots.extend(account_result["rebalanced_lots"])
                    rebalance_residual_drift[account_type] = account_result["residual_drift"]
                current_lots = rebalanced_lots
                row["rebalance_residual_drift"] = rebalance_residual_drift
            else:
                row["rebalance_residual_drift"] = None

            row["portfolio_value"] = portfolio_value(current_lots, current_prices)
            row["ending_lots"] = list(current_lots)
            row["ending_prices_by_ticker"] = dict(current_prices)
        else:
            row["portfolio_value"] = None
            row["ending_lots"] = None
            row["ending_prices_by_ticker"] = None
            row["rebalance_residual_drift"] = None

        results.append(row)

    return results


def project_no_income_no_expense_baseline(
    current_date: date,
    horizon_date: date,
    birth_date: date,
    filing_status: str,
    bracket_table: dict,
    withdrawal_start_date: date,
    ss_claim_date: date,
    universe: dict | None = None,
    initial_lots: list[dict] | None = None,
    initial_prices_by_ticker: dict | None = None,
    inflation_rate: float = 0.0,
    target_allocations: dict | None = None,
    draw_order: list[str] | None = None,
    sale_method: str = "hifo",
    rebalance: bool = False,
    rebalance_band: float = 0.0,
    withdrawal_strategy: str = "flat_percentage",
    withdrawal_strategy_params: dict | None = None,
    rmd_table: dict | None = None,
    ss_annual_benefit: float = 0.0,
    ss_bend_point_table: dict | None = None,
) -> list[dict]:
    """
    The "what if no more income is earned and no more expenses are incurred, starting today"
    baseline (ADJUSTED_WEALTH_REDESIGN.md §2, 2026-08-14) — a second `project_multi_year` run
    against the SAME portfolio (`universe`/`initial_lots`/`initial_prices_by_ticker`/
    `target_allocations`), the SAME tax law, and the SAME withdrawal mechanics (`withdrawal_strategy`
    /`withdrawal_strategy_params`/`sale_method`/`rebalance`/`rebalance_band`/`draw_order`/`rmd_table`
    — MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1's bracket-aware split + RMD floor apply
    here identically) as whatever base-case call the caller is comparing against — the only things
    that change are:

    - **`ss_annual_benefit` should be this baseline's OWN, separately-computed figure, not the real
      scenario's** (Module F, 2026-08-31 — a real bug, caught and fixed the same day it shipped):
      this function itself is agnostic and just uses whatever value the caller passes, same as
      every other parameter here, but the caller MUST compute it under this baseline's own "no
      more income from today forward" hypothesis — only ALREADY-EARNED income (historical years,
      plus the current year's own already-earned-so-far piece) could have generated Social
      Security credit if income genuinely stopped today, a smaller AIME than the real scenario's
      own (which includes every future projected year). `ui/social_security_tab.py` computes both
      figures independently, publishing them as two separate cross-tab bridges
      (`computed_ss_annual_benefit` / `computed_ss_annual_benefit_baseline`) for exactly this
      reason — passing the real scenario's own figure here instead (the original, now-fixed bug)
      overstates this baseline's benefit.

    - `ss_bend_point_table` (2026-09-06, NEXT.md item B3) forwards straight through to
      `project_multi_year` for the Retirement Earnings Test, same as every other parameter here —
      but since `gross_w2`/`gross_se` are unconditionally `$0` in this baseline (see immediately
      below), `retirement_earnings_test_reduction` always sees `$0` countable earnings and the
      reduction is always `$0` regardless of whether this is supplied. Accepted anyway so a caller
      forwarding the same kwargs to both this function and the real scenario's own `project_multi_
      year` call doesn't need a special case.

    - `income_inputs`/`expense_inputs`: zeroed entirely (see the literal curve values below) — no
      more W-2/SE income, no income breaks (including any configured pension/break income — a real
      simplification, not an oversight: "no more income is earned" is the user's own literal framing,
      so a configured pension is zeroed here too even though a pension isn't really "stopped" the way
      employment is; flag this to a user who has one configured and expects it to survive).
    - `project_multi_year`'s own `retirement_date` and `phase_dates["savings_stop_date"]` are pulled
      to `min(current_date, withdrawal_start_date)` (there is no `retirement_date` PARAMETER on this
      function at all — the caller's real planned retirement date is deliberately not accepted
      here, so it can't be passed by mistake and silently do nothing) — this closes
      `project_multi_year`'s pre-retirement dissaving gate (`year < retirement_date.year`)
      deterministically for every projected year, and moves `saving_fraction` to 0 immediately (moot
      here since zeroed income already guarantees $0 everywhere via Stage 1/2's own earned-income
      dependence, but a belt-and-suspenders match for the "stopped saving today" framing). The `min`
      (2026-09-06, NEXT.md item B2) — not unconditionally `current_date` — matters only for someone
      ALREADY retired and withdrawing before this baseline even runs (a real `withdrawal_start_date`
      in the past): pinning retirement/savings-stop to `current_date` in that case would put them
      AFTER `withdrawal_start_date`, violating `project_multi_year`'s own date-ordering validation;
      `min` keeps retirement/savings-stop no later than withdrawal starts, always, without changing
      this function's own computed values either way. `withdrawal_start_date` and `ss_claim_date`
      ARE accepted as parameters and passed straight through unchanged — since the point is "what if
      I stop earning/spending today, but still retire and start Social Security on my originally
      planned schedule."
    - `contribution_config_by_year=None`: every year falls back to
      `modules.contributions.DEFAULT_CONTRIBUTION_CONFIG` ($0 everywhere) — moot regardless, since
      $0 earned income already zeroes every Stage 1/2 target on its own.

    Because income/expenses are zeroed, every row's `gross_w2`/`gross_se`/`gross_ordinary_break_
    income`/`gross_expense` come back `0.0`, and `gross_income` (pre-withdrawal) is PURELY
    investment income — taxable dividends/interest, then withdrawal-driven distributions/realized
    gains once `withdrawal_start_date` hits — so `total_tax` on every row here is entirely
    attributable to the existing portfolio, never to earned income. That's exactly the tax stream
    `net_present_value_of_field(..., "total_tax")` is meant to isolate for the "Adjusted wealth"
    metric (see `ui/projection_tab.py`).

    **A known, honest limitation carried over from the base case, not fixed here**: the tax owed on
    pre-withdrawal dividend/interest income isn't funded by any actual cash outflow in this baseline
    either (no dissaving trigger fires once `retirement_date == current_date` closes that gate off
    entirely, as above) — this baseline's `total_tax` figures are the *accrued* obligation, not a
    literal cash-paid figure. That's actually fine for this feature's purpose (a tax *obligation* to
    discount, not a model of how it gets paid) but is worth surfacing in whatever UI text quotes this
    figure.
    """
    zero_curve = {"start_value": 0.0, "end_value": 0.0, "midpoint_years": 1, "steepness": 0.0001}
    zero_income = {
        "current_year_already_earned_w2": 0.0,
        "current_year_already_earned_se": 0.0,
        "current_year_yet_to_earn_w2": 0.0,
        "current_year_yet_to_earn_se": 0.0,
        "w2_curve": zero_curve,
        "se_curve": zero_curve,
        "breaks": [],
    }
    zero_expense = {
        "current_year_already_incurred_expense": 0.0,
        "current_year_yet_to_incur_expense": 0.0,
        "expense_curve": zero_curve,
    }
    # 2026-09-06 (NEXT.md item B2's date-ordering validation, wired into project_multi_year below):
    # "today" isn't necessarily EARLIER than the caller's real `withdrawal_start_date` — someone
    # already retired and already withdrawing before this baseline even runs has a real
    # `withdrawal_start_date` in the past. Pinning retirement_date/savings_stop_date to the LATER of
    # the two would violate "withdrawal can't precede retirement/savings-stop," so both are pulled
    # to whichever is EARLIER instead — moot for this function's own computed values either way
    # (income/expenses are already unconditionally zeroed above, so neither date affects anything
    # but the dissaving gate, itself moot at $0 profit, and this validation).
    retirement_and_savings_stop_date = min(current_date, withdrawal_start_date)
    return project_multi_year(
        zero_income,
        zero_expense,
        current_date,
        horizon_date,
        retirement_date=retirement_and_savings_stop_date,
        birth_date=birth_date,
        filing_status=filing_status,
        bracket_table=bracket_table,
        contribution_config_by_year=None,
        phase_dates={
            "savings_stop_date": retirement_and_savings_stop_date,
            "withdrawal_start_date": withdrawal_start_date,
            "ss_claim_date": ss_claim_date,
        },
        universe=universe,
        initial_lots=initial_lots,
        initial_prices_by_ticker=initial_prices_by_ticker,
        inflation_rate=inflation_rate,
        target_allocations=target_allocations,
        draw_order=draw_order,
        sale_method=sale_method,
        rebalance=rebalance,
        rebalance_band=rebalance_band,
        withdrawal_strategy=withdrawal_strategy,
        withdrawal_strategy_params=withdrawal_strategy_params,
        rmd_table=rmd_table,
        ss_annual_benefit=ss_annual_benefit,
        ss_bend_point_table=ss_bend_point_table,
    )


def portfolio_value(lots: list[dict], prices_by_ticker: dict[str, float]) -> float:
    """Sum of `shares × current price` across every lot — $0 for a ticker with no known price.
    Exported (no leading underscore, ADJUSTED_WEALTH_REDESIGN.md §3.2, 2026-08-14) — the UI needs
    today's actual mark-to-market value directly, not just as a row field produced mid-projection."""
    return sum(lot["shares"] * prices_by_ticker.get(lot["ticker"], 0.0) for lot in lots)


def net_present_value_of_field(rows: list[dict], discount_rate: float, field: str) -> float | None:
    """
    NPV, as of `current_date` (t=0), of every row's `field` value — discounted at `discount_rate`
    per year using each row's own `years_from_now` as the exponent: `row[field] / (1 +
    discount_rate) ** years_from_now`. The current (possibly partial) year, at `years_from_now ==
    0`, is counted at full value (discount factor 1), standard NPV convention — only genuinely
    future flows are discounted. Generalized (ADJUSTED_WEALTH_REDESIGN.md §3.1, 2026-08-14) from
    the original `profit`-only `net_present_value` so the same discounting convention can be reused
    for any per-row numeric field — e.g. `"total_tax"` for the Adjusted wealth metric below.

    `discount_rate` is meant to be the portfolio's own blended real expected return (see
    `modules.portfolio.summarize_holdings`'s `blended_real_return`) — the rate the money would
    otherwise earn if invested instead, making this the standard "what are these future cash flows
    worth today, compared to investing instead" question, entirely in real (inflation-adjusted)
    dollars end to end (no separate inflation deflation needed on top — the discount rate is
    already real, and every field value already is too).

    Rows with `row[field] is None` (only possible if `bracket_table` was entirely empty — see
    `project_multi_year`) are excluded from the sum rather than treated as $0, since "unknown" and
    "no cash flow" are different things; returns `None` only if every row's value is unknown.
    """
    known = [r for r in rows if r[field] is not None]
    if not known:
        return None
    return sum(r[field] / (1.0 + discount_rate) ** r["years_from_now"] for r in known)


def net_present_value(rows: list[dict], discount_rate: float) -> float | None:
    """NPV of every row's `profit` (the year's real-dollar cash-flow surplus/deficit after tax and
    expenses) — see `net_present_value_of_field` for the discounting convention itself."""
    return net_present_value_of_field(rows, discount_rate, "profit")


def average_annual_field(rows: list[dict], field: str) -> float | None:
    """
    Plain arithmetic mean of `row[field]` across every row where it's populated (not `None`) —
    real (already inflation-adjusted) dollars, deliberately NOT a present value
    (ADJUSTED_WEALTH_REDESIGN.md §4b): "roughly how much X per year should I expect," a typical/
    representative-year question, not a today's-dollars-of-a-future-stream one. The general form
    behind `average_annual_net_retirement_income` below (now a thin wrapper over this) — reused
    directly wherever another per-row dollar figure wants the same framing (e.g. 2026-08-31's
    `ss_after_tax_income`/`other_after_tax_income` split, for the Results section's own "SS
    contribution to net income" / "Net income without SS" stats).

    `None` if no row has `field` populated at all (e.g. the scenario never reaches its own
    withdrawal phase within `horizon_date`).
    """
    values = [r[field] for r in rows if r.get(field) is not None]
    return sum(values) / len(values) if values else None


def average_annual_net_retirement_income(rows: list[dict]) -> float | None:
    """
    Plain arithmetic mean of `net_retirement_income` (RETIREMENT_REPORTING_AUDIT.md §2.3 —
    `total_withdrawal_income - retirement_taxes_paid`, the true after-tax spendable cash in a
    withdrawal-phase year) across every row where it's populated — real (already inflation-
    adjusted) dollars, deliberately NOT a present value (ADJUSTED_WEALTH_REDESIGN.md §4b): this
    answers "roughly how much spendable cash per year should I expect in retirement," which is a
    typical/representative-year question, not a today's-dollars-of-a-future-stream question.
    `None` if no row has a `net_retirement_income` at all (e.g. the scenario never reaches its own
    withdrawal phase within `horizon_date`).
    """
    return average_annual_field(rows, "net_retirement_income")


def average_retirement_tax_rate(rows: list[dict]) -> float | None:
    """
    Plain arithmetic mean of each withdrawal-phase row's own EFFECTIVE tax rate —
    `retirement_taxes_paid / total_withdrawal_income` (both already-defined row fields; the
    latter is the GROSS, pre-tax cash raised that year — see RETIREMENT_REPORTING_AUDIT.md §2.3 —
    so this is "of every dollar drawn out in retirement, what fraction goes to tax," not a rate on
    net income) — across every row where `total_withdrawal_income > 0`. 2026-08-30, user request:
    feeds the "Adjusted wealth" metric's new calculation (see
    `adjusted_wealth_via_retirement_tax_rate` below), replacing that metric's original NPV-of-
    every-future-tax-bill approach.

    A row with `total_withdrawal_income <= 0` (nothing drawn that year, or an unusual net-negative
    figure) is excluded rather than treated as a 0% or undefined rate — same "unknown is not the
    same as zero" posture `net_present_value_of_field` already uses for a `None` field value.
    `None` if no row qualifies (e.g. the scenario never reaches its own withdrawal phase within
    `horizon_date`, or reaches it but never actually draws anything).
    """
    rates = [
        r["retirement_taxes_paid"] / r["total_withdrawal_income"]
        for r in rows
        if r.get("total_withdrawal_income") and r["total_withdrawal_income"] > 0
    ]
    return sum(rates) / len(rates) if rates else None


def adjusted_wealth_via_retirement_tax_rate(baseline_rows: list[dict], discount_rate: float) -> dict | None:
    """
    2026-08-30, user request — replaces the "Adjusted wealth (today, net of future taxes)"
    metric's original calculation (a full NPV of every future year's tax bill,
    ADJUSTED_WEALTH_REDESIGN.md §3, 2026-08-14) with a simpler three-step recipe against the SAME
    no-income/no-expense `baseline_rows` (`project_no_income_no_expense_baseline`'s own output):

    1. `average_retirement_tax_rate(baseline_rows)` — this scenario's own average effective tax
       rate on retirement-phase withdrawals.
    2. Apply that ONE rate to the portfolio's value AT the retirement/withdrawal-start year (the
       first `is_withdrawal_year` row's own `portfolio_value_at_start_of_year`) — e.g. a 10%
       average rate on a $3M portfolio → $2.7M after-tax.
    3. Discount that single after-tax, at-retirement dollar figure back to TODAY at
       `discount_rate` (meant to be the portfolio's own blended real expected return — see
       `net_present_value_of_field`'s own docstring for why that's the right rate), over the
       number of years from today to the retirement year (that same row's own `years_from_now`).

    Returns `None` if the scenario never reaches its own withdrawal phase within the projected
    horizon, or has no computable average tax rate (e.g. $0 ever drawn in every withdrawal-phase
    year) — there's nothing to apply a rate to or discount in either case. Otherwise returns
    `{"adjusted_wealth", "average_tax_rate", "wealth_at_retirement", "years_to_retirement"}` — the
    three intermediate figures are returned alongside the final answer so a caller can show its
    work rather than presenting one opaque number.
    """
    retirement_row = next((r for r in baseline_rows if r["is_withdrawal_year"]), None)
    if retirement_row is None:
        return None
    average_tax_rate = average_retirement_tax_rate(baseline_rows)
    if average_tax_rate is None:
        return None
    wealth_at_retirement = retirement_row["portfolio_value_at_start_of_year"]
    after_tax_at_retirement = wealth_at_retirement * (1.0 - average_tax_rate)
    years_to_retirement = retirement_row["years_from_now"]
    adjusted_wealth = after_tax_at_retirement / (1.0 + discount_rate) ** years_to_retirement
    return {
        "adjusted_wealth": adjusted_wealth,
        "average_tax_rate": average_tax_rate,
        "wealth_at_retirement": wealth_at_retirement,
        "years_to_retirement": years_to_retirement,
    }
