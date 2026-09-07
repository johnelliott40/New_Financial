"""
Module G1 — health index / expected lifespan (MODULE_G1_HEALTH_LIFESPAN.md, 2026-08-30).

Pure functions only: no Streamlit calls, no I/O beyond `load_mortality_table`'s own single
boundary. See CLAUDE.md ground rule 4. Answers "how long is this person likely to live, given
their sex and self-rated health" — a survival-curve calculation, built as a standalone module and
NOT yet wired into `project_multi_year`'s own horizon or any withdrawal-sizing logic (per the
spec's own explicit step 5: "this step is G1 alone: compute and display the figures").

**Two figures, two different jobs, deliberately kept separate** (confirmed with the user,
2026-08-30, per the spec's own flagged design fork): `planning_horizon_age`
(`modules.demographics`) stays the actual FUNDING horizon — a manual, conservative input, entirely
untouched by this module. `expected_lifespan_age`/`percentile_lifespan_age` below are NEW,
separate, clearly-labeled consumption-planning figures, not a replacement for it.
"""

from __future__ import annotations

import json
from pathlib import Path

from modules.demographics import current_age

_MORTALITY_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "mortality_table.json"

# A starting default, explicitly flagged (both here and in the UI) as an editable approximation,
# not a clinical claim -- there is no single universally-agreed numeric mapping from a four-option
# self-rated health status to a mortality-table adjustment. "Good" is the reference tier (SSA
# tables are population averages, closest to a "good" self-report in most self-rated-health
# literature) -- 0 years shift. The other three tiers shift the EFFECTIVE age used for table
# lookup only (see health_adjusted_age) -- never the real age used everywhere else in the model.
DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS = {
    "Excellent": -3,
    "Good": 0,
    "Fair": 5,
    "Poor": 10,
}

# The table's own domain (data/mortality_table.json covers exact ages 0-119 for both sexes) --
# hardcoded per the spec's own literal wording, not derived from the loaded table, since this
# schema is fixed (SSA Period Life Tables have published this same 0-119 range for decades).
_MIN_TABLE_AGE = 0
_MAX_TABLE_AGE = 119


def load_mortality_table(path: Path = _MORTALITY_TABLE_PATH) -> dict:
    """
    Reads data/mortality_table.json -- the SSA Period Life Table (see that file's own `_note` for
    the exact edition/source/retrieval date) -- and returns `{"male": {age: qx}, "female": {age:
    qx}}` with age keys converted to `int` (the file stores them as JSON string keys; every
    consumer below looks them up by `int`). This is the module's one I/O boundary, mirroring
    `modules.tax.load_bracket_table`/`load_rmd_table`'s own pattern -- every function below takes
    the already-loaded table as a plain argument, keeping them pure and independently testable
    against fixture tables that don't touch disk.
    """
    with open(path, "r") as f:
        raw = json.load(f)
    return {
        sex: {int(age): qx for age, qx in ages.items()}
        for sex, ages in raw.items()
        if sex != "_note"
    }


def health_adjusted_age(actual_age: int, health_status: str, adjustment_years: dict) -> int:
    """
    Age-rating (the standard actuarial/insurance-underwriting technique for turning a qualitative
    health assessment into a mortality-table adjustment): `actual_age + adjustment_years[health_
    status]`, clamped to `[0, 119]` (the table's own domain) -- this is the age the mortality table
    is LOOKED UP at, never the real age used everywhere else in the model (demographics, tax
    brackets, RMDs, etc. all keep using the person's real age; only this module's own table lookups
    use the health-adjusted one).

    Raises `KeyError` for a `health_status` not present in `adjustment_years` -- the four-option
    set (`modules.demographics.HEALTH_STATUS_OPTIONS`) is closed and validated by the UI's own
    selectbox, so an unrecognized value here is a real caller bug, not a case to silently default.
    """
    adjusted = actual_age + adjustment_years[health_status]
    return max(_MIN_TABLE_AGE, min(_MAX_TABLE_AGE, adjusted))


def survival_curve(
    birth_date,
    as_of,
    sex: str,
    health_status: str,
    mortality_table: dict,
    adjustment_years: dict | None = None,
) -> list[dict]:
    """
    One row per age from the person's current age (`modules.demographics.current_age(birth_date,
    as_of)`) through 119: `{"age", "death_probability_this_year", "survival_probability_to_age"}`.

    Age-rating is re-applied EVERY year (`health_adjusted_age` called fresh for each row's own
    `age`), not fixed once at the start -- this matches how real underwriting age-rating works: a
    70-year-old in "Good" health is looked up at mortality-age 70, not "current age + a fixed
    offset computed decades ago at the start of this curve."

    Both probabilities are UNCONDITIONAL, viewed from `as_of` today (the standard actuarial "joint"
    construction, not the raw table `qx`, which is itself conditional on having already reached
    that age):
    - `death_probability_this_year` at age X = (probability of surviving every prior age in this
      curve) × (this age's own `qx`, at ITS OWN health-adjusted lookup age) -- the probability, as
      of today, of dying specifically during age X's year.
    - `survival_probability_to_age` at age X = the running product of `(1 - qx)` across every age
      from the curve's start through X, inclusive -- the probability, as of today, of being alive
      through the end of age X's year.

    `adjustment_years` defaults to `DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS` when `None` -- the caller
    passes its own (possibly user-edited) dict otherwise.
    """
    adjustment_years = adjustment_years if adjustment_years is not None else DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS
    table = mortality_table[sex]
    starting_age = current_age(birth_date, as_of)

    curve: list[dict] = []
    survival_so_far = 1.0
    for age in range(starting_age, _MAX_TABLE_AGE + 1):
        effective_age = health_adjusted_age(age, health_status, adjustment_years)
        qx = table[effective_age]
        death_probability_this_year = survival_so_far * qx
        survival_so_far *= 1.0 - qx
        curve.append(
            {
                "age": age,
                "death_probability_this_year": death_probability_this_year,
                "survival_probability_to_age": survival_so_far,
            }
        )
    return curve


def expected_lifespan_age(curve: list[dict]) -> float | None:
    """
    Expected value of age at death: `sum(age * death_probability_this_year)` across the curve,
    normalized by the curve's own total captured death probability rather than assumed to be
    exactly `1.0` -- the table's terminal age (119) has a real, published `qx` of ~0.907, not
    `1.0`, so a curve technically leaves a tiny residual survival probability beyond age 119
    unaccounted for. Dividing by the actual total (not blindly by 1.0) keeps this exact for any
    input curve rather than silently introducing a small downward bias, at the cost of assuming
    that residual tail's own age distribution mirrors the rest -- immaterial in practice (the
    residual is a small fraction of a percent for any real starting age, since survival is already
    near zero well before 119) but done honestly rather than ignored.

    Returns `None` for an empty curve (nothing to compute an expectation from) or one with zero
    total death probability (a degenerate/malformed input) -- not `0.0`, which would misleadingly
    read as "age zero," a real, different answer.

    **A known, deliberate ~0.5-year gap from the SSA table's own published "life expectancy"
    column, not a bug**: this is the CURTATE expectation (each death counted at its whole integer
    age, per the spec's own literal formula), while SSA's published figure is the COMPLETE
    expectation (assumes deaths occur, on average, mid-year, adding ~0.5). Verified against the
    table itself: age-0 "Good"-health male comes out ~73.0 here vs. SSA's own published 73.54 for
    the same table -- a textbook, well-understood actuarial convention difference, not a
    calculation error, and immaterial for this module's own "roughly matches, for a display-only
    planning figure" bar (see MODULE_G1_HEALTH_LIFESPAN.md's own framing).
    """
    total_death_probability = sum(row["death_probability_this_year"] for row in curve)
    if total_death_probability <= 0:
        return None
    weighted_age_sum = sum(row["age"] * row["death_probability_this_year"] for row in curve)
    return weighted_age_sum / total_death_probability


def percentile_lifespan_age(curve: list[dict], percentile: float) -> int | None:
    """
    The CONSERVATIVE, funding-safety-oriented percentile of the lifespan distribution: the first
    age in `curve` at which `survival_probability_to_age` drops below `1 - percentile` -- e.g.
    `percentile=0.90` is the age by which there is a 90% cumulative chance of having already died
    (equivalently, only a 10% chance of living LONGER than this age) -- the standard actuarial
    reading of "90th-percentile lifespan," and the higher-percentile-means-later-and-safer-age
    behavior the funding-horizon-safety framing (see this module's own top-of-file docstring)
    needs: as `percentile` climbs toward `1.0`, the threshold shrinks toward `0.0`, pushing the
    returned age later, toward the table's own terminal age.

    Falls back to the curve's own LAST age (i.e., the table's terminal age, currently 119) if
    survival never drops below the threshold within the curve at all -- an extreme percentile
    request (e.g. `0.999...`) or a curve starting at a very old age can legitimately never cross
    the threshold before running out of table; the terminal age is still a real, computable answer
    (matches `modules.tax.rmd_divisor`'s own "always computable, never raise for a request past the
    table's edge" convention), not `None`.

    Returns `None` only for a genuinely empty curve.
    """
    if not curve:
        return None
    threshold = 1.0 - percentile
    for row in curve:
        if row["survival_probability_to_age"] < threshold:
            return row["age"]
    return curve[-1]["age"]


def coverage_percentile_at_age(curve: list[dict], age: int) -> float | None:
    """
    The inverse of `percentile_lifespan_age` -- what fraction of this curve's own projected
    outcomes are already resolved (the person has died) by the END of `age`'s year:
    `1 - survival_probability_to_age` at that row. Answers "my planning horizon is age X -- what
    percentile of my own survival curve does that actually cover?" (`ui/demographics_tab.py`'s own
    caption comparing `planning_horizon_age` against this module's figures, per MODULE_G1_HEALTH_
    LIFESPAN.md's own point 4).

    Returns `None` if `age` isn't a row in `curve` at all -- below the curve's own starting age
    (nothing resolved to look up) or above its terminal age (`age` exceeds what the table can
    represent at all, currently 119) -- the caller decides how to present either edge case (e.g.
    "beyond the table's own range, treat as fully covered" for the latter), not silently guessed
    at here.
    """
    for row in curve:
        if row["age"] == age:
            return 1.0 - row["survival_probability_to_age"]
    return None
