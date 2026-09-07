# Module G1 — health index / expected lifespan, ready to spec

**Prepared by:** Claude (Cowork), 2026-08-23, against the live codebase. Read `modules/demographics.py`,
`ui/demographics_tab.py`, `PROJECT_PLAN.md`'s Module G1 entry, and `MODEL_WIRING.md` §1.2 (the planning-horizon
section) before writing this — this module already has a real, waiting integration point: `health_status`
(`HEALTH_STATUS_OPTIONS = ["Excellent", "Good", "Fair", "Poor"]`) is already collected on the Demographics tab
today and explicitly documented as feeding "a future health-index module (Module G1); no calculation on it
here yet" — this spec is that calculation.

## What this module is for, per your own framing

You asked for this "so we can properly manage the tax implications for consumption over the lifetime" — i.e.,
not primarily a new funding-safety feature, but an input to Module G2's withdrawal/spending decisions: how
much can reasonably be drawn each year, over how many likely remaining years, changes the bracket-aware
sizing and RMD planning in `MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md`. This document scopes G1 on its own
terms first (it's a real, self-contained module — a survival-curve calculation) and then names exactly where
it plugs into G2, without building that G2 side here.

## One design fork to flag before anything else — funding safety vs. consumption planning

There are two different questions a "how long will I live" figure can answer, and they want opposite
defaults:

- **"How long should I fund for, so I don't run out of money?"** — this wants a CONSERVATIVE answer (a high
  percentile of the survival curve, e.g. the 90th or 95th), because funding to your *median* life expectancy
  means a real, roughly 50% chance of outliving your plan.
- **"What's a realistic planning assumption for smoothing consumption/taxes over my actual likely years?"** —
  this wants the EXPECTED (mean) or MEDIAN figure, because a spending/withdrawal strategy built around a
  worst-case 95th-percentile horizon over-saves and under-spends in the much-more-likely case you don't live
  that long.

**Recommendation, flagged rather than silently picked**: keep `planning_horizon_age` (today's conservative,
explicitly-labeled age-100 default) as the actual FUNDING horizon — the number that determines "does the
model of my full projection run out of money." Add this module's expected/median lifespan as a NEW, separate,
clearly-labeled figure used only for consumption-smoothing and tax-timing decisions (Module G2's eventual
dynamic spending rule), not as a replacement for the funding horizon. This matches `MODEL_WIRING.md` §1.2's
own existing framing almost exactly — it already says Module G1 "replaces the *source*" of the horizon
number, not necessarily that the horizon number itself should become the expected value rather than a
conservative one; confirm this reading with the user before building if you'd rather have G1 directly
override `planning_horizon_age` with, say, a 90th-percentile figure instead of leaving it as a manual input
alongside a new informational stat.

## A second gap to flag — this app has no "biological sex" input yet

Standard mortality tables (SSA Period Life Tables, CDC actuarial tables) are published separately by sex —
male and female cohort mortality differs enough (multiple years of life expectancy) that a single blended
"unisex" table would be a real accuracy loss, not a minor rounding difference. `modules/demographics.py`
today collects birth date, retirement date, health status, and planning horizon — no sex field. **This needs
a new Demographics-tab input** (`st.selectbox("Biological sex (for life-expectancy table lookup)", options=
["Female", "Male"], key="sex_for_mortality")`) before a real table lookup is possible. Flagging explicitly
rather than silently defaulting to one sex or blending — confirm the label/framing is comfortable before
adding it; some users may prefer an unlabeled "Table" choice with the sex framing hidden in help text.

## Data source — new `data/mortality_table.json`

Same convention as `data/tax_brackets.json`: a cited, cross-checked external source, not hand-derived numbers.
**Use the SSA Period Life Table** (Social Security Administration, published periodically — most recently as
of this writing the 2021 table; confirm no newer edition has been published before transcribing) — it gives,
for each single age 0-119 and each sex, `qx` (probability of death within that year, given alive at the start
of it) and the resulting life expectancy at that age. Structure to mirror `tax_brackets.json`'s own pattern:

```json
{
  "_note": "SSA Period Life Table, [year], Social Security Administration. Source: [exact URL]. qx = probability of death within one year, given alive at the start of the age shown. Cross-check against at least one independent published source before transcribing, same convention as data/tax_brackets.json.",
  "male": { "0": 0.00552, "1": 0.00037, "...": "...", "119": 1.0 },
  "female": { "0": 0.00465, "1": 0.00030, "...": "...", "119": 1.0 }
}
```

**Do not fabricate the actual `qx` values from memory** — pull the real published table when building this
(the SSA publishes it as a plain HTML/PDF table on ssa.gov); this document specifies the schema and sourcing
discipline, not the numbers themselves, exactly like `tax_brackets.json`'s own `_note` field insists on
"cross-checked against at least two independent published sources."

## Health-status adjustment — a real methodology, flagged as an approximation

There is no single universally-agreed numeric mapping from a four-option self-rated health status to a
mortality adjustment — this needs to be built as a **documented, user-overridable approximation**, not
presented as clinically precise. The standard actuarial/insurance-underwriting technique for turning a
qualitative health assessment into a mortality-table adjustment is **age rating**: look up the mortality
table not at the person's real age, but at a shifted "mortality age" that reflects their health standing
relative to the table's own population average.

```python
# modules/health.py — a starting default, explicitly flagged as adjustable, not a clinical claim.
# "Good" is the reference tier (SSA tables are population averages, closest to a "good" self-report
# in most self-rated-health literature) -- 0 years shift. Excellent/Fair/Poor shift the EFFECTIVE
# age used for table lookup, not the real age used everywhere else in the model.
DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS = {
    "Excellent": -3,
    "Good": 0,
    "Fair": 5,
    "Poor": 10,
}
```

Surface these four numbers as editable inputs on the Demographics tab (not just a hardcoded constant) so you
can override them if you have a more specific view of your own mortality risk (e.g., from a life-insurance
underwriting quote, which is a real, individually-priced version of exactly this same age-rating technique).
Label this clearly in the UI: "Approximate — adjusts which table age is used to look up your mortality risk;
override if you have a more specific estimate."

## Core functions — new `modules/health.py`

Pure functions only, per `CLAUDE.md` ground rule 4 — mirrors this codebase's existing module shape closely:

```python
def load_mortality_table(path: Path = _MORTALITY_TABLE_PATH) -> dict:
    """Reads data/mortality_table.json -> {"male": {age: qx}, "female": {age: qx}}, ints for age keys."""


def health_adjusted_age(actual_age: int, health_status: str, adjustment_years: dict) -> int:
    """Actual age + this health tier's adjustment, clamped to [0, 119] (the table's own domain)."""


def survival_curve(
    birth_date: date, as_of: date, sex: str, health_status: str,
    mortality_table: dict, adjustment_years: dict | None = None,
) -> list[dict]:
    """
    One row per age from current age through 119: {"age", "death_probability_this_year",
    "survival_probability_to_age"}. `survival_probability_to_age` is the cumulative product of
    (1 - qx) from the current age up through THIS age, each year's qx looked up at that year's own
    health_adjusted_age (age rating re-applied every year, not fixed once at the start — matches
    how real underwriting age-rating works: a 70-year-old in "Good" health is looked up at
    mortality-age 70, not "current age + fixed offset computed decades ago").
    """


def expected_lifespan_age(curve: list[dict], current_age: int) -> float:
    """Expected value of age at death: sum over the curve of death_probability_this_year * age,
    each conditioned on having survived to that age (already baked into how death_probability_this_
    year should be computed as the joint, not the raw table qx — see the function's own docstring
    for the exact conditioning, mirroring standard actuarial expected-value-of-remaining-life math)."""


def percentile_lifespan_age(curve: list[dict], percentile: float) -> int:
    """The age at which survival_probability_to_age first drops below (1 - percentile) — e.g.
    percentile=0.90 -> the age you have a 90% chance of NOT having died before. Used for the
    funding-horizon-safety framing above, distinct from expected_lifespan_age's consumption-planning
    framing."""
```

## Demographics-tab additions

Alongside the existing `health_status`/`planning_horizon_age` inputs (`ui/demographics_tab.py`):

1. New `sex_for_mortality` selectbox (see the gap noted above).
2. New editable health-adjustment-years inputs (four small number inputs, or a single expander — match
   the existing `employer_match_rate`/`employer_match_cap_pct` two-column widget pattern), defaulting to
   `DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS`.
3. New derived stat-metric row (same `st.metric` pattern as the existing "Current age"/"Years to
   retirement" row): "Expected lifespan" (`expected_lifespan_age`) and "90th-percentile lifespan"
   (`percentile_lifespan_age(curve, 0.90)`), each labeled plainly as what it is — not silently substituted for
   `planning_horizon_age`'s own existing "Years to planning horizon" metric, which stays as-is per the
   funding-safety recommendation above.
4. A one-line caption comparing the two: e.g. *"Your current planning horizon (age {planning_horizon_age})
   covers the {Xth} percentile of your projected survival curve"* — computed by inverting
   `percentile_lifespan_age` against the CONFIGURED `planning_horizon_age` rather than a fixed 90/95 — a
   genuinely useful, low-effort reassurance ("your age-100 assumption is conservative, covering 97% of
   projected outcomes") or warning ("your age-85 assumption only covers 60% — you have a real chance of
   outliving it") depending on what the user has actually set.

## Where this plugs into Module G2 — named, not built here

Once `expected_lifespan_age`/`percentile_lifespan_age` exist, `MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md`'s
Part 2 (`target_net_spending`, the dynamic total-sizing strategy) is the natural consumer: instead of assuming
a fixed remaining horizon of "years to `planning_horizon_age`" for any smoothing/guardrail math, it could use
"years to `expected_lifespan_age`" as the planning length for consumption smoothing specifically (while the
actual funding/failure check still runs against the full conservative horizon). Flagged as the integration
point, not built as part of either this document or Part 2's own current scope — a THIRD, later step, once
both G1 and G2 Part 1 are live and signed off.

## Summary of concrete next actions for Claude Code

1. Confirm the funding-horizon-vs-expected-lifespan design fork (first section above) and the
   biological-sex input framing (second section) with the user before writing code — both are real,
   user-facing decisions, not implementation details.
2. Source the actual SSA Period Life Table (current edition, both sexes, full age range) and transcribe it
   into `data/mortality_table.json` with a citation `_note` matching `tax_brackets.json`'s own convention —
   cross-check against at least one independent source before committing the numbers.
3. Add `modules/health.py` with `load_mortality_table`, `health_adjusted_age`, `survival_curve`,
   `expected_lifespan_age`, `percentile_lifespan_age` — pure functions, unit-tested against known reference
   points (e.g., SSA's own published life-expectancy-at-birth figures for each sex, as a sanity check that
   `expected_lifespan_age` at age 0 with "Good"/0-adjustment roughly matches the table's own headline number).
4. Add the four new Demographics-tab inputs/outputs described above.
5. Do NOT wire this into `project_multi_year`'s actual horizon or into any withdrawal-sizing logic yet —
   this step is G1 alone: compute and display the figures. The G2 integration is its own later, signed-off
   step, per the section above.
