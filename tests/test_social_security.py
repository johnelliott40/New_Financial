from datetime import date

import pytest

from modules.social_security import (
    REQUIRED_CREDITS_FOR_FULLY_INSURED,
    annual_ss_benefit,
    bend_point_pia,
    bend_points_for_year,
    claim_age_adjustment_factor,
    compute_aime,
    earnings_test_exempt_amounts_for_year,
    full_retirement_age,
    full_retirement_date,
    insured_annual_ss_benefit,
    is_fully_insured,
    load_bend_point_table,
    qc_threshold_for_year,
    quarters_of_coverage,
    retirement_earnings_test_reduction,
)

_TINY_BEND_POINTS = {
    2020: {"bend_point_1": 900.0, "bend_point_2": 5000.0},
    2024: {"bend_point_1": 1000.0, "bend_point_2": 6000.0},
}


class TestLoadBendPointTable:
    def test_real_data_file_has_expected_2026_values(self):
        # Spot-checked directly against https://www.ssa.gov/oact/cola/bendpoints.html (retrieved
        # 2026-08-31): first bend point $1,286/month, second $7,749/month, for 2026. Also
        # https://www.ssa.gov/oact/cola/QC.html (retrieved 2026-08-31): $1,890 for one quarter of
        # coverage in 2026. Retirement Earnings Test exempt amounts (2026-09-06, NEXT.md item B3)
        # spot-checked against https://www.ssa.gov/news/en/cola/factsheets/2026.html: $24,480/year
        # under FRA, $65,160/year in the FRA year itself.
        table = load_bend_point_table()
        assert table[2026] == {
            "bend_point_1": 1286,
            "bend_point_2": 7749,
            "qc_threshold": 1890,
            "earnings_test_lower_exempt": 24480,
            "earnings_test_higher_exempt": 65160,
        }

    def test_year_keys_are_ints(self):
        table = load_bend_point_table()
        assert all(isinstance(y, int) for y in table)

    def test_covers_1979_through_2026_with_no_gaps(self):
        table = load_bend_point_table()
        assert set(table.keys()) == set(range(1979, 2027))

    def test_bend_points_grow_substantially_from_1979_to_2026(self):
        # A real sanity check against a transcription/row-swap error -- NOT strict year-over-year
        # monotonicity: the real published table actually DIPS from 2010 (761/4,586) to 2011
        # (749/4,517), a genuine historical artifact of the 2009 recession lowering the national
        # average wage index that feeds these figures (with SSA's standard 2-year lag) -- confirmed
        # against the live page, not a transcription error. Overall growth across the full 47-year
        # span is still a real, checkable invariant.
        table = load_bend_point_table()
        assert table[2026]["bend_point_1"] > table[1979]["bend_point_1"] * 5
        assert table[2026]["bend_point_2"] > table[1979]["bend_point_2"] * 5


class TestBendPointsForYear:
    def test_exact_year_match(self):
        assert bend_points_for_year(2024, _TINY_BEND_POINTS) == (1000.0, 6000.0)

    def test_held_flat_beyond_last_configured_year(self):
        assert bend_points_for_year(2050, _TINY_BEND_POINTS) == (1000.0, 6000.0)

    def test_uses_latest_year_at_or_before(self):
        assert bend_points_for_year(2023, _TINY_BEND_POINTS) == (900.0, 5000.0)

    def test_falls_back_to_earliest_year_if_before_every_entry(self):
        assert bend_points_for_year(1950, _TINY_BEND_POINTS) == (900.0, 5000.0)

    def test_raises_on_empty_table(self):
        with pytest.raises(ValueError):
            bend_points_for_year(2026, {})


class TestComputeAime:
    def test_hand_computed_two_years(self):
        assert compute_aime({2024: 42000.0, 2025: 42000.0}) == pytest.approx(84000.0 / 420.0)

    def test_fewer_than_35_years_still_divides_by_420_not_a_smaller_count(self):
        # The single most common real-world AIME mistake: 10 years of $50,000 must divide by 420
        # (the "missing" 25 years are $0, not years that don't count at all), NOT by 120 (10*12).
        earnings = {2016 + i: 50000.0 for i in range(10)}
        assert compute_aime(earnings) == pytest.approx(500000.0 / 420.0)
        assert compute_aime(earnings) != pytest.approx(500000.0 / 120.0)

    def test_only_the_top_35_years_count_not_every_year_entered(self):
        # 40 years entered: 35 years at $60,000 plus 5 low-earning years at $5,000 -- the 5 low
        # years must be EXCLUDED (top 35 only), not averaged in and dragging AIME down.
        earnings = {1980 + i: 60000.0 for i in range(35)}
        earnings.update({2015 + i: 5000.0 for i in range(5)})
        assert compute_aime(earnings) == pytest.approx(35 * 60000.0 / 420.0)

    def test_empty_earnings_record_gives_zero(self):
        assert compute_aime({}) == 0.0


class TestBendPointPia:
    def test_all_below_first_bend_point(self):
        assert bend_point_pia(800.0, 1286.0, 7749.0) == pytest.approx(0.90 * 800.0)

    def test_between_the_two_bend_points(self):
        aime = 3000.0
        expected = 0.90 * 1286.0 + 0.32 * (3000.0 - 1286.0)
        assert bend_point_pia(aime, 1286.0, 7749.0) == pytest.approx(expected)

    def test_above_second_bend_point(self):
        aime = 8000.0
        expected = 0.90 * 1286.0 + 0.32 * (7749.0 - 1286.0) + 0.15 * (8000.0 - 7749.0)
        assert bend_point_pia(aime, 1286.0, 7749.0) == pytest.approx(expected)

    def test_zero_aime_gives_zero_pia(self):
        assert bend_point_pia(0.0, 1286.0, 7749.0) == 0.0

    def test_monotonically_increasing_in_aime(self):
        pias = [bend_point_pia(aime, 1286.0, 7749.0) for aime in (0, 1000, 3000, 8000, 15000)]
        assert pias == sorted(pias)


class TestFullRetirementAge:
    def test_1937_and_earlier_is_65(self):
        assert full_retirement_age(1930) == (65, 0)
        assert full_retirement_age(1937) == (65, 0)

    def test_rises_two_months_per_year_1938_through_1942(self):
        assert full_retirement_age(1938) == (65, 2)
        assert full_retirement_age(1942) == (65, 10)

    def test_flat_66_from_1943_through_1954(self):
        assert full_retirement_age(1943) == (66, 0)
        assert full_retirement_age(1954) == (66, 0)

    def test_rises_two_months_per_year_1955_through_1959(self):
        assert full_retirement_age(1955) == (66, 2)
        assert full_retirement_age(1959) == (66, 10)

    def test_67_for_1960_and_later(self):
        assert full_retirement_age(1960) == (67, 0)
        assert full_retirement_age(2000) == (67, 0)


class TestFullRetirementDate:
    def test_matches_full_retirement_age_offset(self):
        assert full_retirement_date(date(1965, 3, 10)) == date(1965 + 67, 3, 10)

    def test_january_1_birthday_uses_prior_birth_year_row(self):
        # SSA's own convention: born Jan 1, 1960 is treated as attaining ages against the 1959
        # row (FRA 66y10m), not the 1960 row (FRA 67y0m).
        assert full_retirement_date(date(1960, 1, 1)) == date(1960 + 66, 11, 1)


class TestClaimAgeAdjustmentFactor:
    def test_exactly_at_fra_is_1_0(self):
        birth = date(1965, 6, 15)
        assert claim_age_adjustment_factor(birth, full_retirement_date(birth)) == pytest.approx(1.0)

    def test_fra_66_claim_at_62_is_25_percent_reduction(self):
        # MODULE spec's own worked example, verbatim: FRA 66, claiming at 62 = 48 months early =
        # 36*5/9% + 12*5/12% = 25% reduction.
        birth = date(1950, 6, 15)  # FRA = 66
        claim = date(1950 + 62, 6, 15)
        assert claim_age_adjustment_factor(birth, claim) == pytest.approx(0.75)

    def test_fra_67_claim_at_62_is_30_percent_reduction(self):
        # Spec's own second worked example: FRA 67, claiming at 62 = 60 months early =
        # 36*5/9% + 24*5/12% = 30% reduction.
        birth = date(1965, 6, 15)  # FRA = 67
        claim = date(1965 + 62, 6, 15)
        assert claim_age_adjustment_factor(birth, claim) == pytest.approx(0.70)

    def test_delayed_claim_at_70_gives_8_percent_per_year_credit(self):
        birth = date(1965, 6, 15)  # FRA = 67, so 3 years delayed to 70
        claim = date(1965 + 70, 6, 15)
        assert claim_age_adjustment_factor(birth, claim) == pytest.approx(1.0 + 3 * 0.08)

    def test_raises_before_age_62(self):
        birth = date(1965, 6, 15)
        with pytest.raises(ValueError):
            claim_age_adjustment_factor(birth, date(1965 + 61, 6, 15))

    def test_raises_after_age_70(self):
        birth = date(1965, 6, 15)
        with pytest.raises(ValueError):
            claim_age_adjustment_factor(birth, date(1965 + 71, 6, 15))

    def test_boundary_ages_62_and_70_are_valid_not_raised(self):
        birth = date(1965, 6, 15)
        claim_age_adjustment_factor(birth, date(1965 + 62, 6, 15))  # should not raise
        claim_age_adjustment_factor(birth, date(1965 + 70, 6, 15))  # should not raise

    def test_factor_strictly_increases_with_later_claiming(self):
        birth = date(1965, 6, 15)
        factors = [
            claim_age_adjustment_factor(birth, date(1965 + age, 6, 15))
            for age in (62, 64, 66, 67, 68, 70)
        ]
        assert factors == sorted(factors)
        assert len(set(factors)) == len(factors)  # strictly increasing, no ties


class TestAnnualSsBenefit:
    def test_composes_aime_bend_points_and_claim_factor(self):
        birth = date(1965, 6, 15)
        fra = full_retirement_date(birth)
        aime = 8000.0
        bp1, bp2 = bend_points_for_year(1965 + 62, _TINY_BEND_POINTS)  # held flat -> last (2024) entry
        expected_monthly_pia = bend_point_pia(aime, bp1, bp2)
        expected = expected_monthly_pia * 1.0 * 12.0  # factor 1.0 at exact FRA
        assert annual_ss_benefit(aime, _TINY_BEND_POINTS, birth, fra) == pytest.approx(expected)

    def test_claiming_early_reduces_the_annual_benefit(self):
        birth = date(1965, 6, 15)
        fra = full_retirement_date(birth)
        early_claim = date(1965 + 62, 6, 15)
        full = annual_ss_benefit(8000.0, _TINY_BEND_POINTS, birth, fra)
        early = annual_ss_benefit(8000.0, _TINY_BEND_POINTS, birth, early_claim)
        assert early < full

    def test_claiming_late_increases_the_annual_benefit(self):
        birth = date(1965, 6, 15)
        fra = full_retirement_date(birth)
        late_claim = date(1965 + 70, 6, 15)
        full = annual_ss_benefit(8000.0, _TINY_BEND_POINTS, birth, fra)
        late = annual_ss_benefit(8000.0, _TINY_BEND_POINTS, birth, late_claim)
        assert late > full

    def test_zero_aime_gives_zero_benefit(self):
        birth = date(1965, 6, 15)
        fra = full_retirement_date(birth)
        assert annual_ss_benefit(0.0, _TINY_BEND_POINTS, birth, fra) == 0.0

    def test_end_to_end_with_real_bend_point_table(self):
        table = load_bend_point_table()
        birth = date(1965, 6, 15)
        fra = full_retirement_date(birth)
        benefit = annual_ss_benefit(8000.0, table, birth, fra)
        # Sanity range check against a real, high AIME -- current max monthly PIA is roughly
        # $4,000-$4,900 depending on eligibility year, i.e. an annual benefit well under $60,000,
        # and comfortably above $0 for a real high-AIME earner.
        assert 0 < benefit < 70000.0


class TestQcThresholdForYear:
    def test_real_2026_value(self):
        # Spot-checked against https://www.ssa.gov/oact/cola/QC.html (retrieved 2026-08-31).
        table = load_bend_point_table()
        assert qc_threshold_for_year(2026, table) == 1890

    def test_held_flat_beyond_last_configured_year(self):
        table = load_bend_point_table()
        assert qc_threshold_for_year(2075, table) == qc_threshold_for_year(2026, table)

    def test_uses_latest_year_at_or_before(self):
        table = load_bend_point_table()
        assert qc_threshold_for_year(2020, table) == 1410

    def test_raises_on_empty_table(self):
        with pytest.raises(ValueError):
            qc_threshold_for_year(2026, {})


class TestQuartersOfCoverage:
    def test_hand_computed_two_years(self):
        # $1,890/QC: $60,000 -> 4 credits (capped, not 31), $945 -> 0 credits (under one QC).
        earnings = {2024: 60000.0, 2025: 945.0}
        assert quarters_of_coverage(earnings, qc_threshold=1890.0) == 4

    def test_single_year_never_exceeds_4_credits_no_matter_how_high_earnings_are(self):
        # The user's own worked example, verbatim: a single $100k year = exactly 4 credits, not
        # more, regardless of how far above the threshold it is.
        assert quarters_of_coverage({2026: 100000.0}, qc_threshold=1890.0) == 4
        assert quarters_of_coverage({2026: 10_000_000.0}, qc_threshold=1890.0) == 4

    def test_partial_year_below_one_qc_earns_zero_credits(self):
        assert quarters_of_coverage({2026: 500.0}, qc_threshold=1890.0) == 0

    def test_fractional_credits_within_a_year_are_floored(self):
        # $1,890 * 2.9 = $5,481 -> floor(2.9) = 2 credits, not 3.
        assert quarters_of_coverage({2026: 5481.0}, qc_threshold=1890.0) == 2

    def test_multiple_years_sum(self):
        earnings = {2020: 60000.0, 2021: 60000.0, 2022: 0.0}
        assert quarters_of_coverage(earnings, qc_threshold=1890.0) == 8

    def test_empty_earnings_record_gives_zero_credits(self):
        assert quarters_of_coverage({}, qc_threshold=1890.0) == 0


class TestIsFullyInsured:
    def test_default_required_credits_is_40(self):
        assert REQUIRED_CREDITS_FOR_FULLY_INSURED == 40

    def test_single_high_earning_year_is_not_insured(self):
        # The user's own worked example: $100k in one year = 4 credits, nowhere near 40.
        assert is_fully_insured({2026: 100000.0}, qc_threshold=1890.0) is False

    def test_full_career_is_insured(self):
        earnings = {1990 + i: 60000.0 for i in range(10)}  # 10 years * 4 credits/year = 40
        assert is_fully_insured(earnings, qc_threshold=1890.0) is True

    def test_exactly_39_credits_is_not_insured(self):
        earnings = {2000 + i: 60000.0 for i in range(9)}  # 9*4=36
        earnings[2009] = 1890.0 * 3  # +3 -> 39 total
        assert quarters_of_coverage(earnings, qc_threshold=1890.0) == 39
        assert is_fully_insured(earnings, qc_threshold=1890.0) is False

    def test_exactly_40_credits_is_insured(self):
        earnings = {2000 + i: 60000.0 for i in range(9)}  # 36
        earnings[2009] = 1890.0 * 4  # +4 -> 40 total
        assert quarters_of_coverage(earnings, qc_threshold=1890.0) == 40
        assert is_fully_insured(earnings, qc_threshold=1890.0) is True

    def test_custom_required_credits(self):
        assert is_fully_insured({2026: 100000.0}, qc_threshold=1890.0, required_credits=4) is True


class TestInsuredAnnualSsBenefit:
    def test_not_insured_returns_zero_benefit_and_false_with_credits(self):
        table = load_bend_point_table()
        birth = date(1965, 6, 15)
        fra = full_retirement_date(birth)
        benefit, insured, credits = insured_annual_ss_benefit(
            {2026: 100000.0}, table, qc_threshold=1890.0, birth_date=birth, claim_date=fra
        )
        assert benefit == 0.0
        assert insured is False
        assert credits == 4

    def test_insured_returns_the_real_benefit_and_true_with_credits(self):
        table = load_bend_point_table()
        birth = date(1965, 6, 15)
        fra = full_retirement_date(birth)
        earnings = {1990 + i: 60000.0 for i in range(35)}
        benefit, insured, credits = insured_annual_ss_benefit(
            earnings, table, qc_threshold=1890.0, birth_date=birth, claim_date=fra
        )
        assert insured is True
        assert credits >= 40
        assert benefit > 0.0
        # Matches a direct, independent reconstruction from the smaller pieces.
        expected = annual_ss_benefit(compute_aime(earnings), table, birth, fra)
        assert benefit == pytest.approx(expected)

    def test_never_evaluates_the_benefit_formula_when_not_insured(self):
        # A 0.0 benefit when not insured must come from the early return, not from a formula that
        # happens to compute 0 -- confirmed by using a real, nonzero-AIME-producing earnings
        # record that's still short of 40 credits.
        table = load_bend_point_table()
        birth = date(1965, 6, 15)
        fra = full_retirement_date(birth)
        earnings = {2020 + i: 100000.0 for i in range(5)}  # 5 years * 4 credits = 20, not insured
        aime = compute_aime(earnings)
        assert aime > 0.0  # a real, nonzero AIME the formula WOULD produce a benefit from
        benefit, insured, _ = insured_annual_ss_benefit(
            earnings, table, qc_threshold=1890.0, birth_date=birth, claim_date=fra
        )
        assert insured is False
        assert benefit == 0.0

    def test_still_raises_for_an_invalid_claim_date(self):
        table = load_bend_point_table()
        birth = date(1965, 6, 15)
        earnings = {1990 + i: 60000.0 for i in range(35)}
        with pytest.raises(ValueError):
            insured_annual_ss_benefit(
                earnings, table, qc_threshold=1890.0, birth_date=birth, claim_date=date(1965 + 61, 6, 15)
            )


# 2026-09-06, NEXT.md item B3 — Retirement Earnings Test (RET).
_RET_TABLE = {
    2020: {"bend_point_1": 900.0, "bend_point_2": 5000.0, "qc_threshold": 1410.0,
           "earnings_test_lower_exempt": 18240.0, "earnings_test_higher_exempt": 48600.0},
    2024: {"bend_point_1": 1000.0, "bend_point_2": 6000.0, "qc_threshold": 1730.0,
           "earnings_test_lower_exempt": 22320.0, "earnings_test_higher_exempt": 59520.0},
}


class TestEarningsTestExemptAmountsForYear:
    def test_looks_up_by_calendar_year_directly_not_eligibility_year(self):
        # Unlike bend_points_for_year/qc_threshold_for_year, this is never run through
        # ssa_effective_birth_year(...)+62 -- the plain calendar year is the lookup key itself.
        assert earnings_test_exempt_amounts_for_year(2024, _RET_TABLE) == (22320.0, 59520.0)

    def test_held_flat_beyond_the_last_year_that_defines_it(self):
        assert earnings_test_exempt_amounts_for_year(2030, _RET_TABLE) == (22320.0, 59520.0)

    def test_falls_back_to_earliest_year_that_defines_it_if_asked_for_an_earlier_year(self):
        assert earnings_test_exempt_amounts_for_year(2000, _RET_TABLE) == (18240.0, 48600.0)

    def test_real_data_file_only_defines_it_starting_2026(self):
        # Most of data/ss_bend_points.json's own 1979-2026 range predates this feature -- a lookup
        # for ANY year (even one long past) must still resolve to the 2026 figures, the earliest
        # (and only) year that defines them, not crash on a KeyError from an earlier bare entry.
        table = load_bend_point_table()
        assert earnings_test_exempt_amounts_for_year(1990, table) == (24480.0, 65160.0)
        assert earnings_test_exempt_amounts_for_year(2026, table) == (24480.0, 65160.0)
        assert earnings_test_exempt_amounts_for_year(2040, table) == (24480.0, 65160.0)


class TestRetirementEarningsTestReduction:
    def test_zero_after_full_retirement_age_year_regardless_of_earnings(self):
        birth = date(1961, 5, 1)  # FRA (67) reached 2028-05-01
        assert retirement_earnings_test_reduction(500000.0, 0.0, birth, 2029, _RET_TABLE) == pytest.approx(0.0)

    def test_zero_when_earnings_at_or_under_the_exempt_amount(self):
        birth = date(1961, 5, 1)
        assert retirement_earnings_test_reduction(18240.0, 0.0, birth, 2020, _RET_TABLE) == pytest.approx(0.0)
        assert retirement_earnings_test_reduction(10000.0, 0.0, birth, 2020, _RET_TABLE) == pytest.approx(0.0)

    def test_one_dollar_withheld_per_two_earned_above_lower_exempt_before_fra_year(self):
        birth = date(1961, 5, 1)  # FRA year is 2028 -- 2020 is strictly before it
        # $30,000 earned, $18,240 exempt (2020's own lower exempt) -> $11,760 excess / 2.
        reduction = retirement_earnings_test_reduction(30000.0, 0.0, birth, 2020, _RET_TABLE)
        assert reduction == pytest.approx((30000.0 - 18240.0) / 2.0)

    def test_one_dollar_withheld_per_three_earned_above_higher_exempt_in_fra_year(self):
        birth = date(1961, 5, 1)  # FRA year is 2028 -- held flat at 2024's own higher exempt
        reduction = retirement_earnings_test_reduction(80000.0, 0.0, birth, 2028, _RET_TABLE)
        assert reduction == pytest.approx((80000.0 - 59520.0) / 3.0)

    def test_countable_earnings_are_w2_plus_se_only_gross_se_alone_still_counts(self):
        birth = date(1961, 5, 1)
        reduction_w2 = retirement_earnings_test_reduction(30000.0, 0.0, birth, 2020, _RET_TABLE)
        reduction_se = retirement_earnings_test_reduction(0.0, 30000.0, birth, 2020, _RET_TABLE)
        reduction_split = retirement_earnings_test_reduction(15000.0, 15000.0, birth, 2020, _RET_TABLE)
        assert reduction_w2 == pytest.approx(reduction_se) == pytest.approx(reduction_split)

    def test_never_reduces_for_a_year_strictly_after_fra_even_with_huge_earnings(self):
        birth = date(1961, 5, 1)
        for year_offset in (1, 5, 20):
            reduction = retirement_earnings_test_reduction(1000000.0, 0.0, birth, 2028 + year_offset, _RET_TABLE)
            assert reduction == pytest.approx(0.0)

    def test_real_data_file_matches_ssa_2026_figures(self):
        # Spot-checked directly against https://www.ssa.gov/news/en/cola/factsheets/2026.html
        # (retrieved 2026-09-06): $24,480/year lower exempt, $65,160/year higher (FRA-year) exempt.
        table = load_bend_point_table()
        birth = date(1961, 5, 1)  # FRA year 2028 -- 2026 is strictly before it
        reduction = retirement_earnings_test_reduction(50000.0, 0.0, birth, 2026, table)
        assert reduction == pytest.approx((50000.0 - 24480.0) / 2.0)
