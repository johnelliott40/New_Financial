"""
Tests for modules/gross_income.py — the 9 required test cases from the gross income & expense
projection build spec's §7, plus extras for the S-curve degenerate case and the break-income
double-count trap the spec's own Section 3 note warns about.
"""

from __future__ import annotations

from datetime import date

import pytest

from modules.gross_income import (
    baseline_annual_rate,
    build_year_windows,
    compute_gross_for_year,
    compute_year_income,
    days_between_inclusive,
    days_in_year,
    phase_flags_for_year,
    project_expenses,
    project_gross_income,
    s_curve_value,
)

FLAT_CURVE = {"start_value": 0.0, "end_value": 0.0, "midpoint_years": 5.0, "steepness": 1.0}


def _curve(start, end, midpoint=5.0, steepness=1.0):
    return {"start_value": start, "end_value": end, "midpoint_years": midpoint, "steepness": steepness}


def _break(label, start_date, end_date, annualized_income):
    return {
        "id": label,
        "label": label,
        "start_date": start_date,
        "end_date": end_date,
        "annualized_income_during_break": annualized_income,
    }


def _income_inputs(**overrides):
    base = dict(
        current_year_already_earned_w2=0.0,
        current_year_already_earned_se=0.0,
        current_year_yet_to_earn_w2=0.0,
        current_year_yet_to_earn_se=0.0,
        w2_curve=FLAT_CURVE,
        se_curve=FLAT_CURVE,
        breaks=[],
    )
    base.update(overrides)
    return base


# ---- Section 2: S-curve ----


class TestSCurveValue:
    def test_starts_exactly_at_start_value(self):
        # Required test case 8 — the whole point of the t=0 normalization.
        params = _curve(start=50000, end=100000, midpoint=5, steepness=0.8)
        assert s_curve_value(params, 0) == pytest.approx(50000.0)

    def test_approaches_end_value_far_out(self):
        params = _curve(start=50000, end=100000, midpoint=5, steepness=0.8)
        assert s_curve_value(params, 100) == pytest.approx(100000.0, abs=1.0)

    def test_monotonic_increasing_toward_a_higher_end_value(self):
        params = _curve(start=50000, end=100000, midpoint=5, steepness=0.8)
        values = [s_curve_value(params, t) for t in range(0, 15)]
        assert values == sorted(values)

    def test_degenerate_zero_steepness_falls_back_to_linear(self):
        params = _curve(start=50000, end=100000, midpoint=5, steepness=0.0)
        assert s_curve_value(params, 0) == pytest.approx(50000.0)
        assert s_curve_value(params, 5) == pytest.approx(75000.0)  # halfway to 2*midpoint=10
        assert s_curve_value(params, 10) == pytest.approx(100000.0)
        assert s_curve_value(params, 20) == pytest.approx(100000.0)  # clamped, not overshooting

    def test_extreme_steepness_does_not_raise_overflow_error(self):
        params = _curve(start=0, end=1, midpoint=5, steepness=50)
        # Should not raise OverflowError for t far from the midpoint in either direction.
        assert s_curve_value(params, -1000) == pytest.approx(0.0, abs=1e-6)
        assert s_curve_value(params, 1000) == pytest.approx(1.0, abs=1e-6)


# ---- Section 3 Step A: year windows ----


class TestBuildYearWindows:
    def test_full_multi_year_span(self):
        windows = build_year_windows(date(2026, 8, 8), date(2029, 3, 15))
        years = [w["year"] for w in windows]
        assert years == [2026, 2027, 2028, 2029]
        assert windows[0]["window_start"] == date(2026, 8, 8)
        assert windows[0]["window_end"] == date(2026, 12, 31)
        assert windows[0]["is_partial_year"] is True
        assert windows[0]["partial_year_reason"] == "current-year-start"
        assert windows[1]["window_start"] == date(2027, 1, 1)
        assert windows[1]["window_end"] == date(2027, 12, 31)
        assert windows[1]["is_partial_year"] is False
        assert windows[-1]["window_start"] == date(2029, 1, 1)
        assert windows[-1]["window_end"] == date(2029, 3, 15)
        assert windows[-1]["is_partial_year"] is True
        assert windows[-1]["partial_year_reason"] == "horizon-year-end"

    def test_same_year_horizon(self):
        # Required test case 2 (originally "same-year retirement" — build_year_windows itself has
        # no concept of retirement anymore, see MODEL_WIRING.md §1-§2; this is now about the
        # horizon date, with the identical boundary-clipping behavior).
        windows = build_year_windows(date(2026, 8, 8), date(2026, 11, 8))
        assert len(windows) == 1
        w = windows[0]
        assert w["year"] == 2026
        assert w["window_start"] == date(2026, 8, 8)
        assert w["window_end"] == date(2026, 11, 8)
        assert w["is_partial_year"] is True
        # Both boundaries clip the same single row simultaneously — Step A's own reason logic
        # picks "current-year-start" first (checked before "horizon-year-end"), which is fine:
        # this row is still current-year (actual/estimate split), never day-weighted-S-curve,
        # regardless of which reason string comes out.
        assert w["partial_year_reason"] == "current-year-start"

    def test_horizon_date_exactly_jan_1_of_final_year(self):
        # Required test case 7 — explicit, tested decision: emit a real (tiny, 1-day) final-year
        # row rather than a zero-row or an omitted year, keeping Step A's clipping fully general
        # (no extra special-casing for this boundary).
        windows = build_year_windows(date(2026, 8, 8), date(2027, 1, 1))
        assert [w["year"] for w in windows] == [2026, 2027]
        final = windows[-1]
        assert final["window_start"] == date(2027, 1, 1)
        assert final["window_end"] == date(2027, 1, 1)
        assert final["partial_year_fraction"] == pytest.approx(1 / 365)

    def test_horizon_before_current_raises(self):
        with pytest.raises(ValueError):
            build_year_windows(date(2026, 8, 8), date(2025, 1, 1))


class TestDaysHelpers:
    def test_days_in_leap_year(self):
        assert days_in_year(2028) == 366
        assert days_in_year(2026) == 365

    def test_days_between_inclusive_same_day(self):
        assert days_between_inclusive(date(2026, 8, 8), date(2026, 8, 8)) == 1

    def test_days_between_inclusive_end_before_start_is_zero(self):
        assert days_between_inclusive(date(2026, 8, 8), date(2026, 8, 1)) == 0


# ---- Section 3 Step B: baseline annual rate ----


class TestBaselineAnnualRate:
    def test_current_year_annualizes_yet_to_earn_over_remaining_fraction(self):
        # $50,000 yet-to-earn over a fraction of the year -> annualized back up.
        rate = baseline_annual_rate(2026, 2026, 50000.0, 0.5, FLAT_CURVE)
        assert rate == pytest.approx(100000.0)

    def test_future_year_uses_s_curve_at_correct_t(self):
        curve = _curve(start=60000, end=80000, midpoint=3, steepness=0.5)
        # 2028 is t=0 (first full year after current year 2027): 2028 - (2027+1) = 0
        assert baseline_annual_rate(2028, 2027, 0.0, 1.0, curve) == pytest.approx(60000.0)


# ---- Section 3 Step C: day-weighted blend ----


class TestComputeYearIncome:
    def test_no_breaks_pure_baseline(self):
        result = compute_year_income(date(2026, 1, 1), date(2026, 12, 31), 2026, 100.0, [])
        assert result["baseline_total"] == pytest.approx(100.0 * 365)
        assert result["break_total"] == 0.0
        assert result["active_labels"] == []

    def test_break_entirely_inside_one_year(self):
        # Required test case 3.
        breaks = [_break("Sabbatical", date(2026, 3, 1), date(2026, 6, 1), 12000.0)]
        result = compute_year_income(date(2026, 1, 1), date(2026, 12, 31), 2026, 100.0, breaks)
        break_days = days_between_inclusive(date(2026, 3, 1), date(2026, 5, 31))
        assert result["active_labels"] == ["Sabbatical"]
        expected_break_total = break_days * (12000.0 / 365)
        assert result["break_total"] == pytest.approx(expected_break_total)
        expected_baseline_total = (365 - break_days) * 100.0
        assert result["baseline_total"] == pytest.approx(expected_baseline_total)

    def test_break_spanning_dec_31_boundary(self):
        # Required test case 4 — verify the same break, applied to two separate year windows,
        # correctly splits proportionally and the baseline still applies to non-break days in each.
        breaks = [_break("PhD stipend", date(2026, 11, 1), date(2027, 2, 1), 24000.0)]
        year_2026 = compute_year_income(date(2026, 1, 1), date(2026, 12, 31), 2026, 50.0, breaks)
        year_2027 = compute_year_income(date(2027, 1, 1), date(2027, 12, 31), 2027, 60.0, breaks)

        days_2026 = days_between_inclusive(date(2026, 11, 1), date(2026, 12, 31))
        days_2027 = days_between_inclusive(date(2027, 1, 1), date(2027, 1, 31))
        assert year_2026["break_total"] == pytest.approx(days_2026 * (24000.0 / 365))
        assert year_2027["break_total"] == pytest.approx(days_2027 * (24000.0 / 365))
        assert year_2026["baseline_total"] == pytest.approx((365 - days_2026) * 50.0)
        assert year_2027["baseline_total"] == pytest.approx((365 - days_2027) * 60.0)
        assert year_2026["active_labels"] == ["PhD stipend"]
        assert year_2027["active_labels"] == ["PhD stipend"]

    def test_break_clipped_by_retirement_year_window_boundary(self):
        # Required test case 5 — a break overlapping the retirement-year window boundary is
        # clipped, not counted past retirement_date.
        window_end = date(2030, 6, 15)  # retirement date mid-year
        breaks = [_break("Unemployment", date(2030, 5, 1), date(2030, 9, 1), 18000.0)]
        result = compute_year_income(date(2030, 1, 1), window_end, 2030, 40.0, breaks)
        break_days = days_between_inclusive(date(2030, 5, 1), window_end)  # clipped at window_end
        assert result["break_total"] == pytest.approx(break_days * (18000.0 / 365))

    def test_multiple_non_overlapping_breaks_in_same_year(self):
        # Required test case 9.
        breaks = [
            _break("Break A", date(2026, 2, 1), date(2026, 3, 1), 12000.0),
            _break("Break B", date(2026, 7, 1), date(2026, 8, 1), 6000.0),
        ]
        result = compute_year_income(date(2026, 1, 1), date(2026, 12, 31), 2026, 100.0, breaks)
        assert set(result["active_labels"]) == {"Break A", "Break B"}
        days_a = days_between_inclusive(date(2026, 2, 1), date(2026, 2, 28))
        days_b = days_between_inclusive(date(2026, 7, 1), date(2026, 7, 31))
        expected_break_total = days_a * (12000.0 / 365) + days_b * (6000.0 / 365)
        assert result["break_total"] == pytest.approx(expected_break_total)
        assert result["baseline_total"] == pytest.approx((365 - days_a - days_b) * 100.0)

    def test_overlapping_breaks_first_listed_wins(self):
        # Documented precedence rule, explicitly tested per the build spec's instruction.
        breaks = [
            _break("First", date(2026, 1, 1), date(2026, 2, 1), 36500.0),  # $100/day
            _break("Second", date(2026, 1, 15), date(2026, 2, 15), 73000.0),  # $200/day, overlaps First
        ]
        result = compute_year_income(date(2026, 1, 1), date(2026, 12, 31), 2026, 0.0, breaks)
        assert set(result["active_labels"]) == {"First", "Second"}
        # First claims Jan 1-31 (31 days @ $100). Second's overlap (Jan 15 - Feb 14) minus the
        # days First already claimed (Jan 15-31) leaves Feb 1-14 (14 days @ $200).
        expected = 31 * 100.0 + 14 * 200.0
        assert result["break_total"] == pytest.approx(expected)


# ---- Section 3 Step D: already-earned addition ----


class TestComputeGrossForYear:
    def test_current_year_adds_already_earned_on_top(self):
        yet_to_earn = {"baseline_total": 20000.0, "break_total": 0.0, "active_labels": []}
        assert compute_gross_for_year(2026, 2026, 50000.0, yet_to_earn) == pytest.approx(70000.0)

    def test_future_year_ignores_already_earned(self):
        yet_to_earn = {"baseline_total": 90000.0, "break_total": 0.0, "active_labels": []}
        # already_earned is only meaningful for the current year -- a nonzero value here must be
        # ignored for any other year.
        assert compute_gross_for_year(2028, 2026, 50000.0, yet_to_earn) == pytest.approx(90000.0)


# ---- project_gross_income: full integration ----


class TestProjectGrossIncome:
    def test_current_year_actual_estimate_split_no_breaks(self):
        # Required test case 1 — model starts Aug 8; already-earned added flat, yet-to-earn
        # day-weighted across the remaining window, no breaks present. horizon_date ==
        # retirement_date here (both the old second argument's value) so the earning window
        # equals the full window, reproducing this test's original pre-MODEL_WIRING.md numbers
        # exactly — this test isn't about the horizon/retirement split at all.
        current_date = date(2026, 8, 8)
        inputs = _income_inputs(
            current_year_already_earned_w2=60000.0,
            current_year_yet_to_earn_w2=20000.0,
        )
        rows = project_gross_income(inputs, current_date, date(2027, 12, 31), date(2027, 12, 31))
        row_2026 = next(r for r in rows if r["year"] == 2026)
        remaining_days = days_between_inclusive(current_date, date(2026, 12, 31))
        annualized = 20000.0 / (remaining_days / 365)
        expected_yet_to_earn = remaining_days * (annualized / 365)
        assert row_2026["gross_w2"] == pytest.approx(60000.0 + expected_yet_to_earn)
        assert row_2026["gross_w2"] == pytest.approx(80000.0, abs=0.01)  # already-earned + all yet-to-earn, undiluted

    def test_same_year_retirement_is_current_year_treatment_not_s_curve(self):
        # Required test case 2, at the project_gross_income level: single-row output, both
        # boundaries applied, still the actual/estimate split (not day-weighted S-curve).
        # horizon_date == retirement_date (same value) — this test isn't about post-retirement
        # continuation, just the current-year-treatment behavior.
        inputs = _income_inputs(
            current_year_already_earned_w2=30000.0,
            current_year_yet_to_earn_w2=10000.0,
            w2_curve=_curve(start=999999, end=999999, midpoint=5, steepness=1.0),  # would be obviously wrong if used
        )
        rows = project_gross_income(inputs, date(2026, 8, 8), date(2026, 11, 8), date(2026, 11, 8))
        assert len(rows) == 1
        assert rows[0]["gross_w2"] == pytest.approx(40000.0, abs=0.01)

    def test_break_inside_current_year_yet_to_earn_window(self):
        # Required test case 6 — break days come out of yet-to-earn day-weighting, already-earned
        # is untouched, break income lands in gross_ordinary_break_income not gross_w2.
        current_date = date(2026, 8, 8)
        inputs = _income_inputs(
            current_year_already_earned_w2=40000.0,
            current_year_yet_to_earn_w2=20000.0,
            breaks=[_break("Sept-Oct break", date(2026, 9, 1), date(2026, 10, 15), 36500.0)],  # $100/day
        )
        rows = project_gross_income(inputs, current_date, date(2027, 12, 31), date(2027, 12, 31))
        row = next(r for r in rows if r["year"] == 2026)
        assert row["gross_w2"] > 40000.0  # already-earned preserved, plus some non-break yet-to-earn
        assert row["gross_w2"] < 60000.0  # but less than the full undiluted yet-to-earn estimate
        assert row["gross_ordinary_break_income"] > 0
        assert row["active_break_labels"] == ["Sept-Oct break"]

    def test_break_income_not_double_counted_across_w2_and_se(self):
        # The exact trap flagged in the module's own docstring: calling compute_year_income once
        # for W2 and once for SE must not sum both calls' break_total together.
        current_date = date(2026, 1, 1)
        inputs = _income_inputs(
            current_year_yet_to_earn_w2=36500.0,
            current_year_yet_to_earn_se=36500.0,
            breaks=[_break("Break", date(2026, 3, 1), date(2026, 4, 1), 36500.0)],
        )
        rows = project_gross_income(inputs, current_date, date(2026, 12, 31), date(2026, 12, 31))
        break_days = days_between_inclusive(date(2026, 3, 1), date(2026, 3, 31))
        expected_break_income = break_days * (36500.0 / 365)
        assert rows[0]["gross_ordinary_break_income"] == pytest.approx(expected_break_income)

    def test_horizon_year_note_present_only_on_the_final_row(self):
        # Renamed/rewritten from the pre-MODEL_WIRING.md "retirement year, unmodeled remainder"
        # note — the projection now runs THROUGH the horizon, not just through retirement, so
        # there's no unmodeled remainder to flag; the note is now about the horizon boundary
        # itself. horizon_date == retirement_date here so no mid-year-retirement note fires too.
        rows = project_gross_income(_income_inputs(), date(2026, 8, 8), date(2030, 6, 15), date(2030, 6, 15))
        final = rows[-1]
        assert final["partial_year_reason"] == "horizon-year-end"
        assert any("Planning-horizon year" in n for n in final["notes"])
        assert all(not r["notes"] for r in rows[:-1])

    def test_years_from_now_indexes_to_current_year(self):
        rows = project_gross_income(_income_inputs(), date(2026, 8, 8), date(2029, 1, 1), date(2029, 1, 1))
        assert [r["years_from_now"] for r in rows] == [0, 1, 2, 3]


class TestProjectGrossIncomeRetirementVsHorizon:
    """
    MODEL_WIRING.md §1-§2 (2026-08-10): the projection now runs through `horizon_date`, with
    `retirement_date` only clipping W-2/SE income (not breaks/pensions) somewhere inside that
    wider window. These tests are new for that split — see the module's own docstring.
    """

    def test_w2_se_zero_for_years_entirely_after_retirement(self):
        current_date = date(2026, 1, 1)
        retirement_date = date(2027, 12, 31)
        horizon_date = date(2030, 12, 31)
        inputs = _income_inputs(
            current_year_yet_to_earn_w2=60000.0,
            current_year_yet_to_earn_se=20000.0,
            w2_curve=_curve(start=60000, end=60000, midpoint=1, steepness=1.0),
            se_curve=_curve(start=20000, end=20000, midpoint=1, steepness=1.0),
        )
        rows = project_gross_income(inputs, current_date, horizon_date, retirement_date)
        pre_retirement = [r for r in rows if r["year"] <= 2027]
        post_retirement = [r for r in rows if r["year"] > 2027]
        assert post_retirement, "test setup should produce at least one post-retirement row"
        assert all(r["gross_w2"] > 0 or r["gross_se"] > 0 for r in pre_retirement)
        assert all(r["gross_w2"] == 0.0 for r in post_retirement)
        assert all(r["gross_se"] == 0.0 for r in post_retirement)

    def test_break_income_continues_past_retirement_as_a_pension(self):
        # §1.3: "gross_ordinary_break_income... post-retirement: unchanged mechanism (e.g. a
        # pension)" — a break spanning past retirement_date must still count in full.
        current_date = date(2026, 1, 1)
        retirement_date = date(2026, 12, 31)
        horizon_date = date(2028, 12, 31)
        inputs = _income_inputs(
            breaks=[_break("Pension", date(2027, 1, 1), date(2029, 1, 1), 24000.0)],
        )
        rows = project_gross_income(inputs, current_date, horizon_date, retirement_date)
        row_2027 = next(r for r in rows if r["year"] == 2027)
        row_2028 = next(r for r in rows if r["year"] == 2028)
        assert row_2027["gross_ordinary_break_income"] == pytest.approx(24000.0, abs=0.01)
        assert row_2028["gross_ordinary_break_income"] == pytest.approx(24000.0, abs=0.01)
        assert row_2027["gross_w2"] == 0.0 and row_2027["gross_se"] == 0.0

    def test_mid_year_retirement_notes_the_transition(self):
        current_date = date(2026, 1, 1)
        retirement_date = date(2026, 7, 1)  # retires mid-current-year
        horizon_date = date(2027, 12, 31)
        inputs = _income_inputs(
            current_year_already_earned_w2=40000.0,
            current_year_yet_to_earn_w2=30000.0,
        )
        rows = project_gross_income(inputs, current_date, horizon_date, retirement_date)
        row_2026 = next(r for r in rows if r["year"] == 2026)
        # The full $30,000 "yet to earn" estimate is still realized in total (the user is telling
        # the model "$30k is what's left to earn" — that total doesn't change), but it's now
        # earned entirely within Jan 1 - Jul 1, not smeared across the full remaining year. See
        # the sibling test below for where that distinction actually shows up numerically: a break
        # occurring AFTER retirement_date within the same year.
        assert row_2026["gross_w2"] == pytest.approx(70000.0, abs=0.01)
        assert any("Retirement falls mid-year" in n for n in row_2026["notes"])
        row_2027 = next(r for r in rows if r["year"] == 2027)
        assert row_2027["gross_w2"] == 0.0

    def test_break_after_mid_year_retirement_does_not_shrink_the_pre_retirement_baseline(self):
        # The earning window already ends at retirement_date on its own — a break occurring AFTER
        # that date can't "claim" any pre-retirement baseline days (there's nothing there to
        # claim), so the pre-retirement baseline total is identical with or without that break.
        # This is the numeric proof that baseline and break-total now use genuinely different
        # windows, not just different rates over the same window.
        current_date = date(2026, 1, 1)
        retirement_date = date(2026, 7, 1)
        horizon_date = date(2026, 12, 31)
        inputs_no_break = _income_inputs(current_year_yet_to_earn_w2=30000.0)
        inputs_with_post_retirement_break = _income_inputs(
            current_year_yet_to_earn_w2=30000.0,
            breaks=[_break("Post-retirement stipend", date(2026, 8, 1), date(2026, 9, 1), 12000.0)],
        )
        rows_no_break = project_gross_income(inputs_no_break, current_date, horizon_date, retirement_date)
        rows_with_break = project_gross_income(
            inputs_with_post_retirement_break, current_date, horizon_date, retirement_date
        )
        assert rows_no_break[0]["gross_w2"] == pytest.approx(rows_with_break[0]["gross_w2"])
        assert rows_with_break[0]["gross_w2"] == pytest.approx(30000.0, abs=0.01)
        assert rows_with_break[0]["gross_ordinary_break_income"] > 0

    def test_already_retired_before_current_date_earns_nothing_the_whole_projection(self):
        current_date = date(2026, 1, 1)
        retirement_date = date(2020, 1, 1)  # retired years before the projection even starts
        horizon_date = date(2027, 12, 31)
        inputs = _income_inputs(current_year_yet_to_earn_w2=50000.0, current_year_already_earned_w2=10000.0)
        rows = project_gross_income(inputs, current_date, horizon_date, retirement_date)
        # already_earned is a realized fact regardless of retirement status (e.g. severance paid
        # this calendar year) -- only the day-weighted "yet to earn" S-curve portion is zeroed.
        assert rows[0]["gross_w2"] == pytest.approx(10000.0, abs=0.01)
        assert rows[1]["gross_w2"] == 0.0

    def test_retirement_after_horizon_never_clips_anything(self):
        # Degenerate but must not crash or behave surprisingly — earning_window_end is always
        # capped at the row's own window_end regardless of how far past the horizon retirement is.
        current_date = date(2026, 1, 1)
        horizon_date = date(2027, 12, 31)
        retirement_date = date(2050, 1, 1)
        inputs = _income_inputs(
            current_year_yet_to_earn_w2=50000.0,
            w2_curve=_curve(start=60000, end=60000, midpoint=1, steepness=1.0),
        )
        rows = project_gross_income(inputs, current_date, horizon_date, retirement_date)
        assert all(r["gross_w2"] > 0 for r in rows)


# ---- project_expenses ----


class TestProjectExpenses:
    def test_current_year_actual_estimate_split(self):
        current_date = date(2026, 8, 8)
        inputs = dict(
            current_year_already_incurred_expense=25000.0,
            current_year_yet_to_incur_expense=10000.0,
            expense_curve=FLAT_CURVE,
        )
        rows = project_expenses(inputs, current_date, date(2026, 12, 31))
        assert rows[0]["gross_expense"] == pytest.approx(35000.0, abs=0.01)

    def test_future_year_uses_expense_curve(self):
        current_date = date(2026, 8, 8)
        inputs = dict(
            current_year_already_incurred_expense=0.0,
            current_year_yet_to_incur_expense=0.0,
            expense_curve=_curve(start=40000, end=60000, midpoint=3, steepness=0.5),
        )
        rows = project_expenses(inputs, current_date, date(2028, 12, 31))
        row_2027 = next(r for r in rows if r["year"] == 2027)  # t=0 for the expense curve
        assert row_2027["gross_expense"] == pytest.approx(40000.0, abs=0.01)


# ---- phase_flags_for_year (MODEL_WIRING.md §2.5) ----


def _phase_dates(**overrides):
    base = dict(
        retirement_date=date(2056, 6, 1),
        savings_stop_date=date(2056, 6, 1),
        withdrawal_start_date=date(2056, 6, 1),
        ss_claim_date=date(2063, 1, 1),
    )
    base.update(overrides)
    return base


class TestPhaseFlagsForYear:
    def test_year_entirely_before_every_phase_date_is_fully_earning_and_saving(self):
        flags = phase_flags_for_year(2030, _phase_dates())
        assert flags["earning_fraction"] == pytest.approx(1.0)
        assert flags["saving_fraction"] == pytest.approx(1.0)
        assert flags["withdrawing_fraction"] == pytest.approx(0.0)
        assert flags["ss_fraction"] == pytest.approx(0.0)
        assert flags["phase_label"] == "accumulating"

    def test_year_entirely_after_every_phase_date_is_fully_withdrawing_and_claiming(self):
        flags = phase_flags_for_year(2070, _phase_dates())
        assert flags["earning_fraction"] == pytest.approx(0.0)
        assert flags["saving_fraction"] == pytest.approx(0.0)
        assert flags["withdrawing_fraction"] == pytest.approx(1.0)
        assert flags["ss_fraction"] == pytest.approx(1.0)
        assert flags["phase_label"] == "withdrawing"

    def test_retirement_year_day_weighted(self):
        # Retires June 1, 2056 (day 152 of a leap year — 2056 is divisible by 4 and not a century
        # year, so it's a leap year: 366 days).
        flags = phase_flags_for_year(2056, _phase_dates())
        retirement_date = date(2056, 6, 1)
        expected = days_between_inclusive(date(2056, 1, 1), retirement_date) / days_in_year(2056)
        assert flags["earning_fraction"] == pytest.approx(expected)
        assert flags["saving_fraction"] == pytest.approx(expected)  # same date in this fixture

    def test_withdrawal_and_ss_claim_can_start_years_apart_from_retirement(self):
        # A real plan: stop saving at 55, retire at 60, start withdrawals at 62, claim SS at 70 —
        # no ordering between the four dates is assumed.
        dates = dict(
            retirement_date=date(2031, 1, 1),
            savings_stop_date=date(2026, 1, 1),
            withdrawal_start_date=date(2033, 1, 1),
            ss_claim_date=date(2041, 1, 1),
        )
        # 2028: past savings_stop_date, not yet retired, not yet withdrawing, not yet claiming.
        flags_2028 = phase_flags_for_year(2028, dates)
        assert flags_2028["earning_fraction"] == pytest.approx(1.0)
        assert flags_2028["saving_fraction"] == pytest.approx(0.0)
        assert flags_2028["withdrawing_fraction"] == pytest.approx(0.0)
        assert flags_2028["phase_label"] == "coasting_pre_retirement"

        # 2032: retired, not yet withdrawing (the "coasting" phase between retirement and the
        # withdrawal start date).
        flags_2032 = phase_flags_for_year(2032, dates)
        assert flags_2032["earning_fraction"] == pytest.approx(0.0)
        assert flags_2032["withdrawing_fraction"] == pytest.approx(0.0)
        assert flags_2032["phase_label"] == "coasting"

        # 2035: withdrawing, not yet claiming SS.
        flags_2035 = phase_flags_for_year(2035, dates)
        assert flags_2035["withdrawing_fraction"] == pytest.approx(1.0)
        assert flags_2035["ss_fraction"] == pytest.approx(0.0)
        assert flags_2035["phase_label"] == "withdrawing"

    def test_savings_stop_and_withdrawal_start_on_the_same_day_boundary(self):
        # NEXT.md item B2 (2026-09-06) — a new project_multi_year validation explicitly makes
        # `withdrawal_start_date == savings_stop_date` LEGAL (only strictly BEFORE is rejected),
        # so this exact-equality transition year is now a guaranteed-legal case worth its own test,
        # not just an assumption that the day-weighting already works at that boundary.
        same_day = date(2056, 7, 1)  # day 183 of a 366-day leap year -- a genuine mid-year split
        dates = dict(
            retirement_date=date(2050, 1, 1),  # already retired well before this transition
            savings_stop_date=same_day,
            withdrawal_start_date=same_day,
            ss_claim_date=same_day,
        )
        flags = phase_flags_for_year(2056, dates)
        expected_saving_fraction = days_between_inclusive(date(2056, 1, 1), same_day) / days_in_year(2056)
        expected_withdrawing_fraction = days_between_inclusive(same_day, date(2056, 12, 31)) / days_in_year(2056)
        assert flags["saving_fraction"] == pytest.approx(expected_saving_fraction)
        assert flags["withdrawing_fraction"] == pytest.approx(expected_withdrawing_fraction)
        # Both fractions are strictly positive -- the split year is genuinely divided, not a gap
        # where neither mechanism applies for part of the year.
        assert 0.0 < flags["saving_fraction"] < 1.0
        assert 0.0 < flags["withdrawing_fraction"] < 1.0
        # `saving_fraction` is "on or BEFORE" the shared day and `withdrawing_fraction` is "on or
        # AFTER" it -- both INCLUSIVE of that one shared day by this function's own documented
        # convention (the same "boundary day counts on both sides" rule already used for
        # `earning_fraction`/`retirement_date`, per this module's own docstring: "earning continues
        # THROUGH retirement_date inclusive"). The two therefore sum to slightly MORE than 1.0, by
        # exactly one day's worth — a deliberate, documented design choice, not an off-by-one bug:
        # confirming this exact number pins the behavior precisely rather than leaving it assumed.
        one_day_fraction = 1.0 / days_in_year(2056)
        assert (flags["saving_fraction"] + flags["withdrawing_fraction"]) == pytest.approx(1.0 + one_day_fraction)
        # The NEXT full year (no transition day at all) is where saving_fraction actually reaches
        # exactly 0 and withdrawing_fraction exactly 1 -- confirming the split fully resolves the
        # following year, with no year ever double-counted beyond this one shared boundary day.
        flags_next_year = phase_flags_for_year(2057, dates)
        assert flags_next_year["saving_fraction"] == pytest.approx(0.0)
        assert flags_next_year["withdrawing_fraction"] == pytest.approx(1.0)

    def test_fractions_never_exceed_1_or_go_negative_across_a_range_of_years(self):
        dates = _phase_dates()
        for year in range(2020, 2080):
            flags = phase_flags_for_year(year, dates)
            for key in ("earning_fraction", "saving_fraction", "withdrawing_fraction", "ss_fraction"):
                assert 0.0 <= flags[key] <= 1.0, f"{key}={flags[key]} out of [0,1] for year {year}"
