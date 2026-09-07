"""
The per-destination contribution toggle system (CONTRIBUTION_TOGGLE_REDESIGN.md, 2026-08-16) —
replaces the priority-order waterfall (formerly `modules/investing.py::contribution_waterfall` /
`allocate_401k_family_remaining` / `DEFAULT_CONTRIBUTION_PRIORITY`, now removed) and the manual-
override/automatic-waterfall dual-branch that used to live in `modules/projection.py`. Every
destination now has an explicit, per-year MODE the user controls directly — max it out, or enter a
custom amount that's always checked against that year's real legal limit — rather than a single
year-wide "waterfall on/off" switch with a six-raw-dollar-field override grid underneath it.

Two genuinely separate stages, matching the spec's own framing (its own quoted words):

    "First we build a method to allocate what dollars should be contributed to retirement accounts
    based on earned income, then we pay expenses and use the remaining dollars to contribute to
    the accounts — first 401k, then roth, then traditional, then taxable."

- **Stage 1 — Targets** (`resolve_401k_target`, `resolve_roth_ira_target`,
  `resolve_traditional_ira_target`, this module): for each destination, what's the *legally
  allowed* amount given this year's mode? Entirely a function of earned income + IRS limits —
  never of whether the money is actually there to spend. Pure, no cash-flow input at all.
- **Stage 2 — Funding** (`fund_from_available_cash`, this module): given the Stage-1 IRA targets,
  and what's actually left of liquid cash after tax and expenses, fund Roth IRA, then Traditional
  IRA, then Taxable (uncapped, guaranteed) — each capped at `min(its own target, whatever cash
  remains)`.

**One asymmetry, preserved from the prior (correct) codebase, not something this redesign
changes**: 401(k) is NOT part of Stage 2 at all. It's a payroll deduction — money that leaves your
paycheck *before* it ever becomes liquid cash, not something funded from after-tax profit the way
Roth IRA/Traditional IRA/Taxable are. So Stage 1's 401(k) target is funded up to its own legal
ceiling directly (bounded only by compensation — you can't defer more than you earned — never by
"is there enough cash left after expenses," since there's no such constraint on a payroll
deduction). The caller (`modules/projection.py`) computes tax *with* that real deduction applied
first, and only *then* sizes the cash pool Roth IRA/Traditional IRA/Taxable compete for in Stage 2.
Building the 401(k) target as though it were also gated on available cash would be a real
regression from what's correct today — flagged here so it isn't rebuilt that way later.
"""

from __future__ import annotations

from modules.tax import check_402g_limit, compute_max_401k_contributions, roth_ira_phase_out_max

CONTRIBUTION_401K_MODES = ("max_pretax", "max_roth", "custom")
CONTRIBUTION_401K_CUSTOM_TYPES = ("pretax", "roth")
IRA_MODES = ("maximize", "custom")

# The per-year config shape (`modules/projection.py`'s `contribution_config_by_year` values,
# `st.session_state.contribution_by_year`'s new per-year entries): one dict with these eight keys.
#
# `DEFAULT_CONTRIBUTION_CONFIG` — the function-level fallback for a year absent from
# `contribution_config_by_year` entirely: "custom, $0" everywhere, producing exactly $0
# contributions. This is what keeps `project_multi_year` fully backward-compatible for every
# caller/test that doesn't pass this parameter at all — same layering `use_contribution_waterfall`
# used to provide (function default OFF/$0, UI default recommended-ON), see NEXT.md.
DEFAULT_CONTRIBUTION_CONFIG: dict = {
    "contribution_401k_mode": "custom",
    "contribution_401k_custom_amount": 0.0,
    "contribution_401k_custom_type": "pretax",
    "maximize_se_employer_401k": False,
    "roth_ira_mode": "custom",
    "roth_ira_custom_amount": 0.0,
    "traditional_ira_mode": "custom",
    "traditional_ira_custom_amount": 0.0,
}

# The UI-LAYER recommended default for a brand-new year with no saved entry yet (state.py /
# ui/projection_tab.py use this, never `project_multi_year` itself) — maxes out every destination,
# matching this redesign's own stated goal of making "max it out" the easy, recommended default
# rather than something the user has to opt into year by year.
RECOMMENDED_CONTRIBUTION_CONFIG: dict = {
    "contribution_401k_mode": "max_pretax",
    "contribution_401k_custom_amount": 0.0,
    "contribution_401k_custom_type": "pretax",
    "maximize_se_employer_401k": True,
    "roth_ira_mode": "maximize",
    "roth_ira_custom_amount": 0.0,
    "traditional_ira_mode": "maximize",
    "traditional_ira_custom_amount": 0.0,
}


def resolve_401k_target(
    mode: str,
    custom_amount: float,
    custom_type: str,
    also_maximize_se_employer: bool,
    tax_year: int,
    age_at_year_end: int,
    gross_w2: float,
    se_earnings: float,
    bracket_table: dict,
) -> dict:
    """
    Stage 1 — the 401(k)-family target for one year, entirely a function of earned income and IRS
    limits (never of available cash — see this module's own docstring). Reuses
    `modules.tax.compute_max_401k_contributions`'s existing, already-correct dependency-safe
    ordering (W-2 employee deferral, then SE employee deferral, then SE employer contribution —
    computing the employer share before the employee share is known would reintroduce an
    already-fixed §415(c) bug; see that function's own docstring) for every mode's underlying caps.

    `mode`:
    - `"max_pretax"`: the full combined statutory max, entirely pretax — W-2 employee deferral
      remaining room + SE employee deferral, both pretax; `also_maximize_se_employer` controls the
      employer side independently (see below).
    - `"max_roth"`: the SAME combined limit, but the EMPLOYEE-side portions (W-2 + SE employee)
      target Roth instead. SE employer contributions are always pretax by law — there is no Roth
      solo-401(k) employer contribution — so this mode maxes the employee side as Roth and leaves
      the employer side to `also_maximize_se_employer` exactly like every other mode.
    - `"custom"`: `custom_amount` (a single combined dollar figure, `custom_type` says whether it
      targets pretax or Roth) applied first against the W-2 plan's own remaining capacity;
      anything beyond that rolls into SE-employee deferral capacity — the same "W-2 first" ordering
      `compute_max_401k_contributions` already uses for its own internal caps, just applied to a
      user-entered total instead of a fully-maxed one.

    `also_maximize_se_employer`: independent of whichever employee-side mode is selected — a
    real employer either contributes its statutory max or doesn't; "a custom employer
    contribution" isn't a well-defined concept the way an employee election is, so there is no
    custom amount for it, only this on/off toggle. `$0` (not maxed) whenever unchecked, regardless
    of mode — matches the app's existing "no employer money invented" convention (see
    `modules.tax.employer_401k_match`'s own docstring for the parallel case on the match side).

    Returns `{"w2_pretax_target", "w2_roth_target", "se_employee_pretax_target",
    "se_employee_roth_target", "se_employer_target", "over_cap_warning"}` — the last is a warning
    string (or `None`) from `modules.tax.check_402g_limit`, produced only in `"custom"` mode when
    the entered amount exceeds the combined W-2+SE-employee §402(g) pool (reduced to the legal
    maximum, same wording every other over-cap warning in this app uses).

    Note on SE-employee Roth: this model's tax engine (`modules.tax.compute_taxes`) has a single
    `se_solo_employee_deferral` parameter with no pretax/Roth distinction — a pre-existing
    limitation, unrelated to and unchanged by this redesign (the prior manual-entry system had the
    exact same single field). `se_employee_pretax_target`/`se_employee_roth_target` are returned
    separately here for display/tracking clarity even though the tax computation itself currently
    treats them identically; the caller sums both into one dollar figure before calling
    `compute_taxes`.
    """
    if mode not in CONTRIBUTION_401K_MODES:
        raise ValueError(f"401(k) mode must be one of {CONTRIBUTION_401K_MODES}, got {mode!r}")
    if custom_type not in CONTRIBUTION_401K_CUSTOM_TYPES:
        raise ValueError(f"401(k) custom type must be one of {CONTRIBUTION_401K_CUSTOM_TYPES}, got {custom_type!r}")

    maxed = compute_max_401k_contributions(tax_year, age_at_year_end, gross_w2, se_earnings, bracket_table)
    w2_cap = maxed["max_w2_employee_deferral"]
    se_employee_cap = maxed["max_se_employee_deferral"]
    se_employer_cap = maxed["max_se_employer_contribution"]
    se_employer_target = se_employer_cap if also_maximize_se_employer else 0.0

    if mode == "max_pretax":
        return {
            "w2_pretax_target": w2_cap,
            "w2_roth_target": 0.0,
            "se_employee_pretax_target": se_employee_cap,
            "se_employee_roth_target": 0.0,
            "se_employer_target": se_employer_target,
            "over_cap_warning": None,
        }
    if mode == "max_roth":
        return {
            "w2_pretax_target": 0.0,
            "w2_roth_target": w2_cap,
            "se_employee_pretax_target": 0.0,
            "se_employee_roth_target": se_employee_cap,
            "se_employer_target": se_employer_target,
            "over_cap_warning": None,
        }

    # mode == "custom" — one combined figure, W-2 capacity first, overflow rolls into SE-employee
    # capacity (same "W-2 first" ordering compute_max_401k_contributions itself already uses).
    entered = max(0.0, custom_amount)
    to_w2 = min(entered, w2_cap)
    to_se_employee = min(max(0.0, entered - to_w2), se_employee_cap)
    combined_pool = w2_cap + se_employee_cap
    over_cap_warning = check_402g_limit(
        w2_401k_contribution=entered if custom_type == "pretax" else 0.0,
        roth_401k_contribution=entered if custom_type == "roth" else 0.0,
        se_401k_employee_contribution=0.0,
        combined_employee_deferral_pool=combined_pool,
    )
    if over_cap_warning:
        over_cap_warning += " Reduced to the legal maximum for tax purposes."
    if custom_type == "pretax":
        return {
            "w2_pretax_target": to_w2,
            "w2_roth_target": 0.0,
            "se_employee_pretax_target": to_se_employee,
            "se_employee_roth_target": 0.0,
            "se_employer_target": se_employer_target,
            "over_cap_warning": over_cap_warning,
        }
    return {
        "w2_pretax_target": 0.0,
        "w2_roth_target": to_w2,
        "se_employee_pretax_target": 0.0,
        "se_employee_roth_target": to_se_employee,
        "se_employer_target": se_employer_target,
        "over_cap_warning": over_cap_warning,
    }


def resolve_roth_ira_target(
    mode: str,
    custom_amount: float,
    tax_year: int,
    filing_status: str,
    age_at_year_end: int,
    magi: float,
    combined_ira_limit: float,
    bracket_table: dict,
) -> float:
    """
    Stage 1 — the Roth IRA target for one year.

    `combined_ira_limit`: the CALLER's own IRC §219(f)(1)-gated combined Traditional+Roth limit —
    `0.0` whenever that year's earned income (`gross_w2 + se_earnings`) is `<= 0`, else
    `modules.tax.max_ira_contribution_limit(...)`. This is a SEPARATE cap from the MAGI phase-out
    below, not a substitute for it — combined here via `min()` so the earned-income gate can never
    be silently lost regardless of what the phase-out math alone would allow (a real, previously-
    fixed bug: a high-dividend, zero-earned-income year has real MAGI-based Roth room on paper, but
    IRC §219(f)(1) still caps actual IRA contributions at compensation, full stop).

    `"maximize"`: the full amount computed above. `"custom"`: `min(custom_amount, that same
    amount)` — exactly the worked example from the spec: enter $5,000, the real legal limit (after
    both the MAGI phase-out and the earned-income gate) is $3,000, you get $3,000.

    `filing_status` outside `("single", "hoh", "mfj")` (i.e. MFS) is defensively treated as $0
    phase-out room — `modules.tax.roth_ira_phase_out_max` itself raises for that filing status
    (MFS is out of scope; see its own docstring), so this function pre-checks rather than letting
    that propagate, matching the same defensive guard the prior manual-entry system used.
    """
    if mode not in IRA_MODES:
        raise ValueError(f"Roth IRA mode must be one of {IRA_MODES}, got {mode!r}")
    if filing_status in ("single", "hoh", "mfj"):
        phase_out_max = roth_ira_phase_out_max(tax_year, filing_status, age_at_year_end, magi, bracket_table)
    else:
        phase_out_max = 0.0
    max_amount = min(phase_out_max, max(0.0, combined_ira_limit))
    if mode == "maximize":
        return max_amount
    return min(max(0.0, custom_amount), max_amount)


def resolve_traditional_ira_target(
    mode: str, custom_amount: float, combined_ira_limit: float, roth_ira_used: float
) -> float:
    """
    Stage 1 — the Traditional IRA target for one year. Traditional's capacity is NOT independent —
    it shares one combined annual limit with Roth (`modules.tax.max_ira_contribution_limit`), and
    per the spec's own priority order, Roth resolves FIRST (see `resolve_roth_ira_target`, called
    before this by the caller): Traditional's target is *structurally* defined as whatever's left
    of the combined limit after Roth, so the two can never sum past the legal cap by construction —
    not by a separate validation step that could be skipped or get out of sync.

    One pre-existing gap, unrelated to and unchanged by this redesign: this model does not compute
    Traditional IRA deductibility (`compute_taxes` has no Traditional-IRA-deduction parameter) — a
    Traditional IRA contribution here never reduces AGI, same as before this change.
    """
    if mode not in IRA_MODES:
        raise ValueError(f"Traditional IRA mode must be one of {IRA_MODES}, got {mode!r}")
    room = max(0.0, combined_ira_limit - roth_ira_used)
    if mode == "maximize":
        return room
    return min(max(0.0, custom_amount), room)


def fund_from_available_cash(roth_ira_target: float, traditional_ira_target: float, available_cash: float) -> dict:
    """
    Stage 2 — given the Stage-1 IRA targets and what's actually left of liquid cash after tax and
    expenses, fund Roth IRA, then Traditional IRA, then Taxable — uncapped, ALWAYS computed, no
    dict that could forget to get populated (WATERFALL_STREAMLINE_REDESIGN.md §2.2's own fix,
    built into this system from the start rather than retrofitted). Does NOT touch 401(k) at all —
    that's resolved upstream as a payroll deduction before `available_cash` is even computed; see
    this module's own docstring for why.

    `available_cash`: the caller's own earned-income-only, post-tax, post-expense, post-401(k)-
    deduction figure (`modules/projection.py`'s `available_cash`) — MUST already exclude the
    portion of profit that's separately, automatically reinvested by
    `modules.investing.roll_forward_portfolio` (Taxable dividends/interest, pre-withdrawal), or
    that cash gets invested twice — once automatically in place, once more here
    (WATERFALL_STREAMLINE_REDESIGN.md §2.3's double-counting trap; the caller reuses that exact
    exclusion, not recomputed here).

    Returns `{"roth_ira_used", "traditional_ira_used", "taxable_used"}` — always summing to exactly
    `min(available_cash, roth_ira_target + traditional_ira_target) + taxable_used`, with
    `taxable_used` absorbing every dollar of `available_cash` not claimed by the two IRA targets.
    """
    available_cash = max(0.0, available_cash)
    roth_ira_used = min(max(0.0, roth_ira_target), available_cash)
    remaining = available_cash - roth_ira_used
    traditional_ira_used = min(max(0.0, traditional_ira_target), remaining)
    remaining -= traditional_ira_used
    taxable_used = max(0.0, remaining)
    return {
        "roth_ira_used": roth_ira_used,
        "traditional_ira_used": traditional_ira_used,
        "taxable_used": taxable_used,
    }
