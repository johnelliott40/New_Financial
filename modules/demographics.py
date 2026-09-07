"""
Module 2 — Demographics.

Pure functions only: no Streamlit calls, no I/O. See CLAUDE.md ground rule 4. Plain date/duration
math for now — no calculation engine beyond derived durations, per PROJECT_PLAN.md Step 2. Health
status feeds a future health-index module (Module G1); no calculation on it happens here yet.
"""

from __future__ import annotations

from datetime import date

HEALTH_STATUS_OPTIONS = ["Excellent", "Good", "Fair", "Poor"]

# MODEL_WIRING.md §1.2 — until Module G1 (health index / projected lifespan) exists, the model's
# projection horizon runs to this age, computed via date_at_age(birth_date, ...). This is a
# deliberate PLANNING horizon, not a life-expectancy estimate — well past any national-average
# longevity figure, so a plan that funds to this age is conservative by construction. Label it that
# way in the UI ("planning horizon: age 100") so it isn't mistaken for a mortality assumption.
# User-overridable per Demographics tab input; Module G1 later replaces the *source* of the horizon
# date, not the plumbing that consumes it (modules.gross_income.build_year_windows's horizon_date).
DEFAULT_PLANNING_HORIZON_AGE = 100


def current_age(birth_date: date, as_of: date) -> int:
    """Completed years of age as of `as_of`."""
    age = as_of.year - birth_date.year
    if (as_of.month, as_of.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age


def date_at_age(birth_date: date, age: int) -> date:
    """Calendar date this person turns `age`, clamping a Feb 29 birthday to Feb 28 in non-leap years."""
    try:
        return birth_date.replace(year=birth_date.year + age)
    except ValueError:
        return birth_date.replace(year=birth_date.year + age, day=28)


def months_between(start: date, end: date) -> int:
    """Whole months from `start` to `end`, clamped to 0 if `end` is on or before `start`."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)
