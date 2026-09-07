from datetime import date

from modules.demographics import HEALTH_STATUS_OPTIONS, current_age, date_at_age, months_between


def test_current_age_birthday_already_passed_this_year():
    assert current_age(date(1990, 1, 15), date(2026, 8, 7)) == 36


def test_current_age_birthday_not_yet_reached_this_year():
    assert current_age(date(1990, 12, 15), date(2026, 8, 7)) == 35


def test_current_age_on_birthday():
    assert current_age(date(1990, 8, 7), date(2026, 8, 7)) == 36


def test_date_at_age_basic():
    assert date_at_age(date(1990, 1, 15), 65) == date(2055, 1, 15)


def test_date_at_age_leap_day_birthday_clamped():
    assert date_at_age(date(2000, 2, 29), 65) == date(2065, 2, 28)


def test_months_between_future_date():
    # (2055-2026)*12 + (1-8) = 341; target day (15) >= start day (7), so no extra decrement.
    assert months_between(date(2026, 8, 7), date(2055, 1, 15)) == 341


def test_months_between_past_date_clamped_to_zero():
    assert months_between(date(2026, 8, 7), date(1990, 1, 15)) == 0


def test_months_between_same_month_day_already_passed():
    assert months_between(date(2026, 8, 7), date(2026, 8, 1)) == 0


def test_months_between_same_month_day_not_yet_reached():
    assert months_between(date(2026, 8, 7), date(2026, 8, 20)) == 0


def test_months_between_exactly_one_month_away():
    assert months_between(date(2026, 8, 7), date(2026, 9, 7)) == 1


def test_months_between_same_date_is_zero():
    assert months_between(date(2026, 8, 7), date(2026, 8, 7)) == 0


def test_health_status_options():
    assert HEALTH_STATUS_OPTIONS == ["Excellent", "Good", "Fair", "Poor"]
