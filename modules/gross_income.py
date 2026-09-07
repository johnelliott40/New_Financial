"""
Gross income & expense projection — a year-by-year array of gross W2 income, gross SE income,
ordinary break income, and gross expenses from *today* through the planning horizon (age 100 by
default, or a Demographics-tab override — see `modules.demographics.DEFAULT_PLANNING_HORIZON_AGE`),
meant to feed `modules/tax.py`'s `compute_taxes` one row at a time (see `modules/projection.py` for
that wiring).

Pure functions only: no Streamlit, no I/O, no reading "today" internally — every function takes
`current_date`/`horizon_date` (and, for income specifically, `retirement_date`) as explicit
arguments, per CLAUDE.md ground rule 4 and this project's established testability convention.

**`horizon_date` vs. `retirement_date` (MODEL_WIRING.md §1-§2, 2026-08-10).** These used to be the
same date — the projection ran from today through retirement and stopped. They are now two
independent concepts: `horizon_date` is the END of the whole projection (the planning horizon, or
eventually Module G1's projected lifespan), while `retirement_date` is only "the date earned income
stops" — a date the projection now runs *past*, not up to. `build_year_windows` (Step A) knows only
about `horizon_date`; it has no opinion on retirement at all. `project_gross_income` is the one
function that also takes `retirement_date`, to clip W-2/SE income (but not breaks/pensions — see
its own docstring) at that boundary within the wider horizon window.

Three things make this harder than "apply a growth curve" — see the build spec this was
implemented from for the full reasoning, summarized here:

1. **Partial years at both ends, not symmetric.** A boundary-crossing year (horizon, or now also
   retirement) is a *projection* like every other future year, so day-weighting it is a reasonable
   approximation. The *current* year is different: some of it already happened (real, lumpy,
   actually-earned income) and smoothing that away would throw out real data — it gets an
   actual/estimate split instead of a smoothed rate (see `baseline_annual_rate` /
   `compute_gross_for_year`).
2. **Income breaks** — periods (unemployment, a stipend, sabbatical, or post-retirement a pension)
   where income is replaced by a different rate for a date range that may itself start/end mid-year
   or span Dec 31, and that (unlike W-2/SE) is not clipped by retirement at all.
3. **Two income representations.** Future full years are an S-curve, day-weighted smoothly
   (including through any boundary year). The current year is two flat dollar amounts —
   already-earned and yet-to-earn — only the yet-to-earn portion is day-weighted.

Day-weighting (`compute_year_income`, Step C) is the one general mechanism used for every
*projected* year; the current year's already-earned/yet-to-earn split (Step D) is a separate,
simpler mechanism used only for the current year. Nothing else special-cases partial years or
breaks outside of Step A (`build_year_windows`) and Step C.
"""

from __future__ import annotations

import calendar
import math
from datetime import date, timedelta

_MIN_STEEPNESS = 1e-9  # below this magnitude, s_curve_value falls back to linear interpolation


def s_curve_value(params: dict, t: float) -> float:
    """
    Logistic curve normalized so it starts exactly at `params["start_value"]` at t=0 — not just
    approximately (a raw logistic only approaches its "start" asymptotically, which would cause a
    small but real discontinuity between the current-year stub value and the curve's year-1 value).

    `params`: {"start_value", "end_value", "midpoint_years", "steepness"}. `t` is a continuous year
    offset: t=0 at Jan 1 of (current_year + 1), t=1 at Jan 1 of (current_year + 2), etc. — see
    `baseline_annual_rate`, the only caller that establishes what `t` means in this module.

    Degenerate case: `steepness` near zero has no well-defined logistic (division by ~0 in the
    normalization) — falls back to linear interpolation from start_value to end_value, reaching
    end_value at t = 2*midpoint_years (symmetric around the midpoint, matching where a real
    logistic curve is ~symmetric), so "no S-curve, just linear growth" is still a usable input
    rather than an error.
    """
    start_value = params["start_value"]
    end_value = params["end_value"]
    midpoint_years = params["midpoint_years"]
    steepness = params["steepness"]

    if abs(steepness) < _MIN_STEEPNESS:
        if midpoint_years <= 0:
            return end_value if t > 0 else start_value
        fraction = min(max(t / (2 * midpoint_years), 0.0), 1.0)
        return start_value + (end_value - start_value) * fraction

    def sigma(x: float) -> float:
        exponent = -steepness * (x - midpoint_years)
        try:
            return 1.0 / (1.0 + math.exp(exponent))
        except OverflowError:
            return 0.0 if exponent > 0 else 1.0

    sigma0 = sigma(0.0)
    denom = 1.0 - sigma0
    if abs(denom) < 1e-12:
        # sigma0 ~= 1: the curve already reached its ceiling by t=0 (e.g. very steep with a
        # negative midpoint) — degenerate, the only sensible value is the ceiling itself.
        return end_value
    normalized = (sigma(t) - sigma0) / denom
    return start_value + (end_value - start_value) * normalized


def days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def days_between_inclusive(start: date, end: date) -> int:
    """Whole days from `start` through `end`, both inclusive. 0 if `end` is before `start`."""
    if end < start:
        return 0
    return (end - start).days + 1


def build_year_windows(current_date: date, horizon_date: date) -> list[dict]:
    """
    Step A. One row per calendar year from `current_date`'s year through `horizon_date`'s year,
    each with the actual [window_start, window_end] date range that year's projection covers —
    clipped to `current_date` at the start and `horizon_date` at the end, full calendar years in
    between. Handles `current_date` and `horizon_date` falling in the same year (a single,
    doubly-clipped row) with no special-casing beyond what's already here.

    `horizon_date` is the END of the whole projection (planning horizon — see
    `modules.demographics.DEFAULT_PLANNING_HORIZON_AGE` — or, later, Module G1's projected
    lifespan), NOT `retirement_date` (MODEL_WIRING.md §1.2/§2.1 — a prior version of this function
    conflated the two; `retirement_date` is now only "the date earned income stops," a separate,
    independent concept applied by `project_gross_income`, not by this window-building step at
    all). This function has no opinion on retirement, contributions, or withdrawals — it only ever
    answers "what date range does calendar year N's row cover."
    """
    if horizon_date < current_date:
        raise ValueError("horizon_date must be on or after current_date")

    start_year = current_date.year
    end_year = horizon_date.year
    windows = []
    for year in range(start_year, end_year + 1):
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        window_start = current_date if year == start_year else year_start
        window_end = horizon_date if year == end_year else year_end

        is_partial_year = window_start > year_start or window_end < year_end
        if year == start_year and window_start > year_start:
            partial_year_reason = "current-year-start"
        elif year == end_year and window_end < year_end:
            partial_year_reason = "horizon-year-end"
        else:
            partial_year_reason = None

        windows.append(
            {
                "year": year,
                "window_start": window_start,
                "window_end": window_end,
                "is_partial_year": is_partial_year,
                "partial_year_reason": partial_year_reason,
                "partial_year_fraction": days_between_inclusive(window_start, window_end) / days_in_year(year),
            }
        )
    return windows


def phase_flags_for_year(year: int, dates: dict) -> dict:
    """
    MODEL_WIRING.md §2.5 — resolves the four independent life-phase dates (`retirement_date`,
    `savings_stop_date`, `withdrawal_start_date`, `ss_claim_date`; see §2) against one plain
    calendar year, day-weighted the same way every other partial-year figure in this module is.
    Every downstream module (the future contribution waterfall, withdrawal engine, Social Security
    wiring) reads these fractions rather than comparing dates itself, per the spec's own
    instruction — no ordering between the four dates is assumed here (a real plan can separate
    them arbitrarily: stop saving at 55, retire at 60, start withdrawals at 62, claim SS at 70).

    `dates`: {"retirement_date", "savings_stop_date", "withdrawal_start_date", "ss_claim_date"} —
    plain `date` objects.

    Returns {"earning_fraction", "saving_fraction", "withdrawing_fraction", "ss_fraction",
    "phase_label"}:
    - `earning_fraction`: fraction of `year` on or before `retirement_date` (earning continues
      THROUGH `retirement_date` inclusive — the day after is the first non-earning day — matching
      the existing inclusive-end convention `build_year_windows` already used for this boundary).
    - `saving_fraction`: fraction of `year` on or before `savings_stop_date`, same convention.
    - `withdrawing_fraction`: fraction of `year` on or after `withdrawal_start_date`.
    - `ss_fraction`: fraction of `year` on or after `ss_claim_date`.

    Each is computed against the PLAIN calendar year (Jan 1 - Dec 31) — independent of any further
    clipping a caller's own projection window applies at the very first/last row via
    `build_year_windows`'s `current_date`/`horizon_date` boundaries. The two compose by
    intersection: a caller working with an already-clipped row window narrows these fractions
    further itself (the same way it already narrows `partial_year_fraction`); this function
    doesn't need to know about that clipping to be correct in isolation.

    `phase_label` is a single string naming the DOMINANT phase as of December 31 of `year` — a
    single label can't represent a mid-year transition precisely, so the four fractions above are
    the authoritative numeric data for anything that needs that precision.
    """
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    total_days = days_in_year(year)

    def _fraction_through(cutoff_inclusive: date) -> float:
        """Fraction of [year_start, year_end] on or before cutoff_inclusive."""
        return days_between_inclusive(year_start, min(year_end, cutoff_inclusive)) / total_days

    def _fraction_from(cutoff_inclusive: date) -> float:
        """Fraction of [year_start, year_end] on or after cutoff_inclusive."""
        return 1.0 - _fraction_through(cutoff_inclusive - timedelta(days=1))

    retirement_date = dates["retirement_date"]
    savings_stop_date = dates["savings_stop_date"]
    withdrawal_start_date = dates["withdrawal_start_date"]

    earning_fraction = _fraction_through(retirement_date)
    saving_fraction = _fraction_through(savings_stop_date)
    withdrawing_fraction = _fraction_from(withdrawal_start_date)
    ss_fraction = _fraction_from(dates["ss_claim_date"])

    if year_end <= retirement_date:
        phase_label = "accumulating" if year_end <= savings_stop_date else "coasting_pre_retirement"
    elif year_end < withdrawal_start_date:
        phase_label = "coasting"
    else:
        phase_label = "withdrawing"

    return {
        "earning_fraction": earning_fraction,
        "saving_fraction": saving_fraction,
        "withdrawing_fraction": withdrawing_fraction,
        "ss_fraction": ss_fraction,
        "phase_label": phase_label,
    }


def baseline_annual_rate(
    year: int,
    current_year: int,
    current_year_yet_to_earn: float,
    partial_year_fraction: float,
    curve: dict,
) -> float:
    """
    Step B. The annual-rate baseline for `year`, before day-weighting against breaks (Step C).

    Only the *yet-to-earn* portion of the current year feeds this — already-earned income bypasses
    it entirely (see `compute_gross_for_year`, Step D). For the current year, annualizes the
    yet-to-earn estimate over its remaining window so it can be day-weighted the same way every
    other year is. For every year after the current one — including the partial retirement year —
    uses the smooth S-curve rate: an intentional asymmetry, since the retirement year is still a
    projection this far out (smoothing it is a reasonable approximation) unlike the current year,
    where real already-known income exists and shouldn't be smoothed away.
    """
    if year == current_year:
        if partial_year_fraction <= 0:
            return 0.0
        return current_year_yet_to_earn / partial_year_fraction
    t = year - (current_year + 1)  # t=0 at the first full year after the current one
    return s_curve_value(curve, t)


def compute_year_income(
    window_start: date,
    window_end: date,
    year: int,
    daily_baseline_rate: float,
    breaks: list[dict],
) -> dict:
    """
    Step C — the core day-weighting mechanism, used for every projected year (all future years,
    including the partial retirement year) and for the current year's yet-to-earn window. Blends
    `daily_baseline_rate` against any `breaks` overlapping `[window_start, window_end]`, including
    breaks that cross a Dec 31 boundary (handled automatically since only the overlap with *this*
    window is ever counted) — no special-casing beyond what's already here.

    `breaks`: [{"id", "label", "start_date" (inclusive), "end_date" (exclusive), "annualized_income_during_break"}, ...].
    Break income is deliberately not split into W2/SE streams — see the module docstring on why
    it's tracked as one separate "ordinary income, no payroll/SE tax" stream instead
    (`break_total`), not blended back into `baseline_total`.

    Precedence for overlapping breaks: **first-listed wins**. If two breaks in `breaks` cover the
    same day, only the earlier one's rate counts for that day — documented here per the build
    spec's explicit instruction not to leave this implicit; `ui/projection_tab.py` also validates
    against creating overlapping breaks in the first place, so this precedence rule is a defensive
    fallback, not the primary safeguard.

    Returns {"baseline_total": float, "break_total": float, "active_labels": list[str]}.
    """
    total_days = days_between_inclusive(window_start, window_end)
    claimed_days: set[date] = set()
    break_total = 0.0
    active_labels: list[str] = []

    for brk in breaks:
        overlap_start = max(window_start, brk["start_date"])
        overlap_end = min(window_end, brk["end_date"] - timedelta(days=1))  # end_date is exclusive
        if overlap_end < overlap_start:
            continue

        span = (overlap_end - overlap_start).days + 1
        this_break_days = [
            d for i in range(span) if (d := overlap_start + timedelta(days=i)) not in claimed_days
        ]
        if not this_break_days:
            continue  # every day in this break's overlap was already claimed by an earlier break

        active_labels.append(brk["label"])
        daily_break_rate = brk["annualized_income_during_break"] / days_in_year(year)
        break_total += len(this_break_days) * daily_break_rate
        claimed_days.update(this_break_days)

    non_break_days = total_days - len(claimed_days)
    baseline_total = non_break_days * daily_baseline_rate

    return {"baseline_total": baseline_total, "break_total": break_total, "active_labels": active_labels}


def compute_gross_for_year(year: int, current_year: int, already_earned: float, yet_to_earn_result: dict) -> float:
    """
    Step D. For every year except the current one, `yet_to_earn_result["baseline_total"]` (from
    `compute_year_income`) *is* the year's total — the baseline rate already covers the whole
    window. For the current year, `already_earned` is added on top: it sits outside the
    day-weighting entirely, since it's realized fact, not a rate to be spread across days.
    `yet_to_earn_result["break_total"]` is NOT added here in either case — it flows into
    `gross_ordinary_break_income` instead (see `project_gross_income`).
    """
    already_earned_component = already_earned if year == current_year else 0.0
    return already_earned_component + yet_to_earn_result["baseline_total"]


def project_gross_income(
    inputs: dict, current_date: date, horizon_date: date, retirement_date: date
) -> list[dict]:
    """
    Steps B-E combined: produces one row per calendar year from `current_date` through
    `horizon_date` (the planning horizon — see `build_year_windows`), not through
    `retirement_date`.

    `retirement_date` (MODEL_WIRING.md §1.3/§2.1) is now a separate, independent concept: the date
    W-2/SE earned income stops. It is NOT the end of the projection — years after it still get a
    row (through `horizon_date`), just with `gross_w2`/`gross_se` clipped to $0 for any day after
    it. `gross_ordinary_break_income` (income breaks — unemployment, a stipend, sabbatical, or,
    post-retirement, a pension: "unchanged mechanism" per §1.3) is DELIBERATELY NOT clipped at
    retirement_date — it's computed over the row's FULL window regardless of retirement, so a
    break/pension configured to span or follow retirement still counts. This is why the window used
    for `compute_year_income`'s break-total call and the (possibly narrower) window used for its
    baseline-total call are different below — conflating them would either wrongly zero out a
    post-retirement pension or wrongly keep earning W-2/SE income past retirement.

    `inputs`: {
        "current_year_already_earned_w2", "current_year_already_earned_se",
        "current_year_yet_to_earn_w2", "current_year_yet_to_earn_se",
        "w2_curve", "se_curve" (SCurveParams dicts — see s_curve_value),
        "breaks": [IncomeBreak dicts — see compute_year_income],
    }

    Returns [{"year", "years_from_now", "is_partial_year", "partial_year_fraction",
    "partial_year_reason", "gross_w2", "gross_se", "gross_ordinary_break_income",
    "active_break_labels", "notes"}, ...].
    """
    current_year = current_date.year
    windows = build_year_windows(current_date, horizon_date)
    breaks = inputs.get("breaks", [])
    rows = []

    for w in windows:
        year = w["year"]
        fraction = w["partial_year_fraction"]

        # The EARNING window for this row: the row's own [window_start, window_end], further
        # clipped at the end by retirement_date (inclusive — retirement_date itself is still an
        # earning day, matching the pre-existing convention). Empty (no earning at all this year)
        # when retirement_date precedes window_start entirely — e.g. every year from horizon-
        # extension's post-retirement years onward, or the whole projection for someone already
        # retired before current_date.
        earning_window_end = min(w["window_end"], retirement_date)
        has_earning_window = earning_window_end >= w["window_start"]
        earning_fraction = (
            days_between_inclusive(w["window_start"], earning_window_end) / days_in_year(year)
            if has_earning_window
            else 0.0
        )

        # Annualizing this year's "yet to earn" input must use the EARNING window's own fraction,
        # not the row's overall (horizon-clipped) fraction — the two are only ever the same value
        # when retirement doesn't fall inside this year at all. Using the overall fraction here
        # would silently understate the annualized rate for a mid-year retirement, since the
        # remaining-year denominator would include non-earning days.
        w2_annual_rate = baseline_annual_rate(
            year, current_year, inputs["current_year_yet_to_earn_w2"], earning_fraction, inputs["w2_curve"]
        )
        se_annual_rate = baseline_annual_rate(
            year, current_year, inputs["current_year_yet_to_earn_se"], earning_fraction, inputs["se_curve"]
        )
        w2_daily_rate = w2_annual_rate / days_in_year(year)
        se_daily_rate = se_annual_rate / days_in_year(year)

        # Break/pension income covers the row's FULL window (unaffected by retirement — §1.3).
        # Computed once (W2's daily rate is passed but discarded here; only break_total/
        # active_labels are used) — see the identical-break-total trap this module has always
        # documented: break-day coverage doesn't depend on which stream's daily rate was passed in.
        full_window_result = compute_year_income(w["window_start"], w["window_end"], year, w2_daily_rate, breaks)
        gross_ordinary_break_income = full_window_result["break_total"]
        active_break_labels = full_window_result["active_labels"]

        # Baseline (S-curve) earned income covers only the EARNING window — this is what actually
        # stops W-2/SE income at retirement_date. Skipped entirely (0.0) when there's no earning
        # window at all this year, rather than calling compute_year_income with an inverted range.
        if has_earning_window:
            w2_yet_to_earn = compute_year_income(w["window_start"], earning_window_end, year, w2_daily_rate, breaks)
            se_yet_to_earn = compute_year_income(w["window_start"], earning_window_end, year, se_daily_rate, breaks)
        else:
            w2_yet_to_earn = {"baseline_total": 0.0, "break_total": 0.0, "active_labels": []}
            se_yet_to_earn = {"baseline_total": 0.0, "break_total": 0.0, "active_labels": []}

        gross_w2 = compute_gross_for_year(year, current_year, inputs["current_year_already_earned_w2"], w2_yet_to_earn)
        gross_se = compute_gross_for_year(year, current_year, inputs["current_year_already_earned_se"], se_yet_to_earn)

        notes = []
        if w["partial_year_reason"] == "horizon-year-end":
            notes.append(
                "Planning-horizon year: this row is clipped at the planning horizon "
                "(end-of-life date), not at retirement — income/expense figures still apply for "
                "the full portion of this year before that date."
            )
        if year == current_year and has_earning_window and earning_window_end < w["window_end"]:
            notes.append(
                f"Retirement falls mid-year ({retirement_date.isoformat()}): W-2/SE income is "
                "earned only through that date; break/pension income and expenses continue for "
                "the rest of the calendar year as usual."
            )
        # No note for an ordinary fully-post-retirement year (retirement_date already behind
        # window_start) — that's the expected steady state for most of a multi-decade projection,
        # not worth a per-row annotation; gross_w2/gross_se simply read $0, which is self-evident.

        rows.append(
            {
                "year": year,
                "years_from_now": year - current_year,
                "is_partial_year": w["is_partial_year"],
                "partial_year_fraction": fraction,
                "partial_year_reason": w["partial_year_reason"],
                "gross_w2": gross_w2,
                "gross_se": gross_se,
                "gross_ordinary_break_income": gross_ordinary_break_income,
                "active_break_labels": active_break_labels,
                "notes": notes,
            }
        )
    return rows


def project_expenses(inputs: dict, current_date: date, horizon_date: date) -> list[dict]:
    """
    Mirrors `project_gross_income`'s day-weighting shape for expenses — same already-
    incurred/yet-to-incur split for the current year, same partial-year handling at the horizon
    boundary, but no breaks (out of scope for v1 per the build spec).

    Unlike `project_gross_income`, expenses have no `retirement_date` parameter at all —
    MODEL_WIRING.md §1.3 doesn't list expenses among the streams that change composition at
    retirement (people keep spending money in retirement), so the existing behavior — one
    continuous S-curve across the whole window — is already correct; only the window itself
    (`current_date` through `horizon_date`, the planning horizon, rather than through
    `retirement_date`) needed to change.

    `inputs`: {"current_year_already_incurred_expense", "current_year_yet_to_incur_expense",
    "expense_curve"}.

    Returns [{"year", "years_from_now", "is_partial_year", "partial_year_fraction", "gross_expense"}, ...].
    """
    current_year = current_date.year
    windows = build_year_windows(current_date, horizon_date)
    rows = []

    for w in windows:
        year = w["year"]
        fraction = w["partial_year_fraction"]
        annual_rate = baseline_annual_rate(
            year, current_year, inputs["current_year_yet_to_incur_expense"], fraction, inputs["expense_curve"]
        )
        daily_rate = annual_rate / days_in_year(year)
        total_days = days_between_inclusive(w["window_start"], w["window_end"])
        yet_to_incur_total = total_days * daily_rate
        already_incurred = inputs["current_year_already_incurred_expense"] if year == current_year else 0.0

        rows.append(
            {
                "year": year,
                "years_from_now": year - current_year,
                "is_partial_year": w["is_partial_year"],
                "partial_year_fraction": fraction,
                "gross_expense": already_incurred + yet_to_incur_total,
            }
        )
    return rows
