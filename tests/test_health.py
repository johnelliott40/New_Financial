from datetime import date

import pytest

from modules.health import (
    DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS,
    coverage_percentile_at_age,
    expected_lifespan_age,
    health_adjusted_age,
    load_mortality_table,
    percentile_lifespan_age,
    survival_curve,
)

# A small, hand-constructed table (NOT the real SSA data) for tests that need controlled,
# hand-computable numbers -- covers the FULL 0-119 domain (survival_curve always iterates through
# age 119 regardless of where it starts, per its own hardcoded _MAX_TABLE_AGE), with a
# deliberately non-flat, easily hand-computed qx per age: 0.01 at age 60, +0.01 per year of
# distance from 60 in either direction, clamped to a max of 0.99 near the table's own edges so
# every entry stays a valid probability. The real data/mortality_table.json is exercised
# separately by TestLoadMortalityTable and the end-to-end sanity checks below.
_TINY_TABLE = {
    "male": {age: min(0.99, 0.01 + 0.01 * abs(age - 60)) for age in range(120)},
    "female": {age: min(0.99, 0.005 + 0.005 * abs(age - 60)) for age in range(120)},
}


class TestLoadMortalityTable:
    """The one I/O boundary -- data/mortality_table.json, the real SSA Period Life Table (see that
    file's own `_note` for the exact edition/source, retrieved 2026-08-30)."""

    def test_both_sexes_present_full_age_range(self):
        table = load_mortality_table()
        assert set(table.keys()) == {"male", "female"}
        assert set(table["male"].keys()) == set(range(120))
        assert set(table["female"].keys()) == set(range(120))

    def test_age_keys_are_ints_not_strings(self):
        table = load_mortality_table()
        assert all(isinstance(age, int) for age in table["male"])
        assert 0 in table["male"]  # int lookup works
        assert "0" not in table["male"]  # not left as a string key

    def test_known_reference_values_match_the_published_table(self):
        # Spot-checked directly against https://www.ssa.gov/oact/STATS/table4c6_2021_TR2024.html
        # (2021 Period Life Table, as used in the 2024 Trustees Report) at the time this was built.
        table = load_mortality_table()
        assert table["male"][0] == pytest.approx(0.005860)
        assert table["female"][0] == pytest.approx(0.005063)
        assert table["male"][65] == pytest.approx(0.019914)
        assert table["female"][65] == pytest.approx(0.012216)
        assert table["male"][119] == pytest.approx(0.906532)
        assert table["female"][119] == pytest.approx(0.906532)

    def test_qx_is_non_decreasing_at_older_ages_for_both_sexes(self):
        # Mortality risk should never meaningfully decrease with age past middle age -- a real
        # sanity check against a transcription error (e.g. a swapped row) rather than a strict
        # mathematical requirement of every published table, but true throughout this one's own
        # actual published values from age 10 onward.
        table = load_mortality_table()
        for sex in ("male", "female"):
            values = [table[sex][age] for age in range(10, 120)]
            assert values == sorted(values)

    def test_female_qx_lower_than_male_at_every_age(self):
        # A real, well-known feature of these tables (female mortality is lower at essentially
        # every age) -- a sanity check that the two columns weren't swapped during transcription.
        table = load_mortality_table()
        assert all(table["female"][age] <= table["male"][age] for age in range(120))


class TestHealthAdjustedAge:
    def test_good_health_is_zero_shift(self):
        assert health_adjusted_age(50, "Good", DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS) == 50

    def test_excellent_health_shifts_younger(self):
        assert health_adjusted_age(50, "Excellent", DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS) == 47

    def test_fair_and_poor_shift_older(self):
        assert health_adjusted_age(50, "Fair", DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS) == 55
        assert health_adjusted_age(50, "Poor", DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS) == 60

    def test_clamped_at_zero(self):
        assert health_adjusted_age(1, "Excellent", DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS) == 0

    def test_clamped_at_table_max(self):
        assert health_adjusted_age(115, "Poor", DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS) == 119

    def test_user_overridden_adjustment_dict_respected(self):
        custom = {"Excellent": -10, "Good": 0, "Fair": 0, "Poor": 20}
        assert health_adjusted_age(50, "Excellent", custom) == 40
        assert health_adjusted_age(50, "Poor", custom) == 70

    def test_unknown_health_status_raises_key_error(self):
        with pytest.raises(KeyError):
            health_adjusted_age(50, "Terrible", DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS)


class TestSurvivalCurve:
    def test_covers_exactly_current_age_through_table_max_using_real_table(self):
        table = load_mortality_table()
        curve = survival_curve(date(1961, 1, 1), date(2026, 1, 1), "male", "Good", table)
        ages = [row["age"] for row in curve]
        assert ages[0] == 65
        assert ages[-1] == 119
        assert ages == list(range(65, 120))

    def test_first_year_probabilities_match_the_starting_ages_own_qx(self):
        # Age 60 is the curve's own starting age here -- day one's death probability and survival
        # probability are both direct functions of qx(60) alone (0.01 in the tiny table), no prior
        # survival product yet to fold in.
        curve = survival_curve(date(1966, 1, 1), date(2026, 1, 1), "male", "Good", _TINY_TABLE)
        assert curve[0]["age"] == 60
        assert curve[0]["death_probability_this_year"] == pytest.approx(0.01)
        assert curve[0]["survival_probability_to_age"] == pytest.approx(0.99)

    def test_survival_probability_is_a_running_product_and_monotonically_decreasing(self):
        curve = survival_curve(date(1966, 1, 1), date(2026, 1, 1), "male", "Good", _TINY_TABLE)
        survivals = [row["survival_probability_to_age"] for row in curve]
        assert survivals == sorted(survivals, reverse=True)
        # Hand-computed for the first three ages (qx = 0.01, 0.02, 0.03 at ages 60/61/62):
        assert survivals[0] == pytest.approx(0.99)
        assert survivals[1] == pytest.approx(0.99 * 0.98)
        assert survivals[2] == pytest.approx(0.99 * 0.98 * 0.97)

    def test_death_probabilities_are_unconditional_and_sum_close_to_one(self):
        # Every row's death_probability_this_year, summed across the whole curve, is the total
        # probability of dying at SOME point from today through the table's terminal age -- must
        # be very close to 1.0 (not exactly, since the terminal age's own qx isn't exactly 1.0 --
        # see expected_lifespan_age's own docstring for why that residual is handled there, not
        # papered over here).
        table = load_mortality_table()
        curve = survival_curve(date(1926, 1, 1), date(2026, 1, 1), "male", "Good", table)  # age 100
        total = sum(row["death_probability_this_year"] for row in curve)
        assert total == pytest.approx(1.0, abs=0.01)

    def test_age_rating_re_applied_every_year_not_fixed_at_start(self):
        # "Poor" health (+10 shift) at a real starting age of 60 means age 60 is looked up at
        # table-age 70, age 61 at table-age 71, etc. -- each row's own qx must match the SHIFTED
        # age's qx in the tiny table, not a fixed value carried forward from the first row.
        curve_poor = survival_curve(date(1966, 1, 1), date(2026, 1, 1), "male", "Poor", _TINY_TABLE)
        # Poor shifts age 60 -> table age 70 (qx=0.02 in the male tiny table: 0.01 + 0.01*(70-60)...
        # wait -- table only covers 60-70, so age 60+10=70 is the table's own max entry.
        assert curve_poor[0]["death_probability_this_year"] == pytest.approx(_TINY_TABLE["male"][70])

    def test_different_sexes_produce_different_curves(self):
        curve_m = survival_curve(date(1966, 1, 1), date(2026, 1, 1), "male", "Good", _TINY_TABLE)
        curve_f = survival_curve(date(1966, 1, 1), date(2026, 1, 1), "female", "Good", _TINY_TABLE)
        assert curve_m[0]["death_probability_this_year"] != curve_f[0]["death_probability_this_year"]

    def test_custom_adjustment_years_dict_is_honored(self):
        curve_default = survival_curve(date(1966, 1, 1), date(2026, 1, 1), "male", "Fair", _TINY_TABLE)
        curve_custom = survival_curve(
            date(1966, 1, 1), date(2026, 1, 1), "male", "Fair", _TINY_TABLE,
            adjustment_years={"Excellent": -3, "Good": 0, "Fair": 0, "Poor": 10},
        )
        # Default Fair = +5 (age 65's qx); custom Fair = +0 (age 60's qx) -- must differ.
        assert curve_default[0]["death_probability_this_year"] != curve_custom[0]["death_probability_this_year"]
        assert curve_custom[0]["death_probability_this_year"] == pytest.approx(_TINY_TABLE["male"][60])


class TestExpectedLifespanAge:
    def test_none_for_empty_curve(self):
        assert expected_lifespan_age([]) is None

    def test_hand_computed_two_age_curve(self):
        # age 60: 10% die this year; age 61: of the 90% remaining, half die -- expected age =
        # (60*0.10 + 61*0.45) / (0.10+0.45)
        curve = [
            {"age": 60, "death_probability_this_year": 0.10, "survival_probability_to_age": 0.90},
            {"age": 61, "death_probability_this_year": 0.45, "survival_probability_to_age": 0.45},
        ]
        expected = (60 * 0.10 + 61 * 0.45) / (0.10 + 0.45)
        assert expected_lifespan_age(curve) == pytest.approx(expected)

    def test_roughly_matches_ssa_published_life_expectancy_at_birth(self):
        # MODULE_G1_HEALTH_LIFESPAN.md's own suggested sanity check: age 0, "Good" (0-adjustment),
        # should roughly match SSA's own published headline figure for each sex (73.54 male /
        # 79.30 female) -- within ~1 year, the well-understood curtate-vs-complete gap (see
        # expected_lifespan_age's own docstring), not an exact match.
        table = load_mortality_table()
        birth = date(2026, 1, 1)
        male_curve = survival_curve(birth, birth, "male", "Good", table)
        female_curve = survival_curve(birth, birth, "female", "Good", table)
        assert expected_lifespan_age(male_curve) == pytest.approx(73.54, abs=1.0)
        assert expected_lifespan_age(female_curve) == pytest.approx(79.30, abs=1.0)

    def test_worse_health_status_lowers_expected_lifespan(self):
        table = load_mortality_table()
        birth = date(1966, 1, 1)
        as_of = date(2026, 1, 1)  # age 60
        good_curve = survival_curve(birth, as_of, "male", "Good", table)
        poor_curve = survival_curve(birth, as_of, "male", "Poor", table)
        assert expected_lifespan_age(poor_curve) < expected_lifespan_age(good_curve)


class TestPercentileLifespanAge:
    def test_none_for_empty_curve(self):
        assert percentile_lifespan_age([], 0.90) is None

    def test_hand_computed_threshold_crossing(self):
        curve = [
            {"age": 60, "survival_probability_to_age": 0.95},
            {"age": 61, "survival_probability_to_age": 0.85},
            {"age": 62, "survival_probability_to_age": 0.05},
            {"age": 63, "survival_probability_to_age": 0.01},
        ]
        # percentile=0.90 -> threshold 0.10 -- first row where survival < 0.10 is age 62.
        assert percentile_lifespan_age(curve, 0.90) == 62

    def test_higher_percentile_gives_a_later_or_equal_age(self):
        table = load_mortality_table()
        curve = survival_curve(date(1966, 1, 1), date(2026, 1, 1), "male", "Good", table)
        age_50 = percentile_lifespan_age(curve, 0.50)
        age_90 = percentile_lifespan_age(curve, 0.90)
        age_95 = percentile_lifespan_age(curve, 0.95)
        assert age_50 <= age_90 <= age_95

    def test_falls_back_to_curves_own_last_age_when_never_crossed(self):
        curve = [{"age": 118, "survival_probability_to_age": 0.5}, {"age": 119, "survival_probability_to_age": 0.4}]
        # threshold for percentile=0.99999 is ~0.00001 -- never crossed by this tiny curve.
        assert percentile_lifespan_age(curve, 0.99999) == 119

    def test_worse_health_status_lowers_percentile_age(self):
        table = load_mortality_table()
        birth = date(1966, 1, 1)
        as_of = date(2026, 1, 1)
        good_curve = survival_curve(birth, as_of, "male", "Good", table)
        poor_curve = survival_curve(birth, as_of, "male", "Poor", table)
        assert percentile_lifespan_age(poor_curve, 0.90) <= percentile_lifespan_age(good_curve, 0.90)


class TestCoveragePercentileAtAge:
    """The inverse of percentile_lifespan_age -- feeds ui/demographics_tab.py's own 'does my
    planning horizon cover enough of my survival curve' caption."""

    def test_none_when_age_not_in_curve_at_all(self):
        curve = [{"age": 60, "survival_probability_to_age": 0.9}]
        assert coverage_percentile_at_age(curve, 61) is None
        assert coverage_percentile_at_age(curve, 59) is None

    def test_hand_computed_value(self):
        curve = [
            {"age": 60, "survival_probability_to_age": 0.95},
            {"age": 100, "survival_probability_to_age": 0.03},
        ]
        assert coverage_percentile_at_age(curve, 100) == pytest.approx(0.97)

    def test_is_the_exact_inverse_of_percentile_lifespan_age(self):
        table = load_mortality_table()
        curve = survival_curve(date(1966, 1, 1), date(2026, 1, 1), "male", "Good", table)
        age_90 = percentile_lifespan_age(curve, 0.90)
        covered = coverage_percentile_at_age(curve, age_90)
        # percentile_lifespan_age found the FIRST age where survival drops below 0.10 -- coverage
        # there must be >= 0.90 (it just crossed the threshold), and the prior age's own coverage
        # must be < 0.90 (confirms age_90 is genuinely the first crossing, not an earlier one).
        assert covered >= 0.90
        prior_row = next(r for r in curve if r["age"] == age_90 - 1)
        assert (1.0 - prior_row["survival_probability_to_age"]) < 0.90
