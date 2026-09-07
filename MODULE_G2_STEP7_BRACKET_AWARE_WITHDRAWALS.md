# Step 7 (Module G2) — bracket-aware withdrawal fill + RMDs, ready to implement

**Prepared by:** Claude (Cowork), 2026-08-23, against the live codebase (`modules/investing.py` 36,635 bytes,
`modules/projection.py` 75,671 bytes, `modules/tax.py` 44,311 bytes, `data/tax_brackets.json` 6,796 bytes —
all current as of this write). Read `MODEL_WIRING.md` §5.3/§9, `NEXT.md`'s own Step 7 framing (lines 860-885,
2807-2822), `modules/investing.py`'s `sell_lots`/`draw_order_fill`/`DEFAULT_DRAW_ORDER`, and
`modules/projection.py`'s full retirement-withdrawal block (lines 822-908) directly before writing this —
this is a concrete build spec for the step MODEL_WIRING.md §9 already approved and gated as "Step 7," not a
new architectural proposal.

## Why this document exists right now

You asked whether the draw order should pull from Roth first instead of last, to lower taxes. The honest
answer turned out to be: **neither fixed order is actually the tax-minimizing one** — a fixed order can only
ever be a placeholder, which is exactly why this codebase's own design docs (`MODEL_WIRING.md` §5.3, written
back on 2026-08-10) already planned a two-stage draw order and marked Stage 2 — this document — as the real
answer. Building it now directly resolves the original question, correctly, instead of just picking a
different fixed order.

## The tax mechanics that make a FIXED order wrong, either direction

- A **Traditional** 401(k)/IRA sale is 100% ordinary income, no matter when it happens in your retirement.
  There's no benefit to "saving" that tax treatment for later — it's the same regardless of order.
- A **Taxable** sale is only taxed on the gain, at LTCG rates (0%/15%/20%, stacked *on top of* your ordinary
  income for the year — see `_stacked_bracket_tax` in `modules/tax.py`). The lower your ordinary income is
  in a given year, the more of that gain lands in the 0% LTCG bracket.
- A **Roth** sale is untaxed, full stop, in any year.

That last point is exactly your original intuition, and it's correct in isolation — pulling Roth first does
minimize *that year's* tax. Where it goes wrong is what it costs you: Roth is the only account where the
balance you *don't* withdraw keeps compounding completely tax-free, forever, with no RMDs ever forcing a
distribution. Spending it down first burns the one asset that gets more valuable the longer it's left alone,
and pushes later years — which may have less flexibility (RMDs forcing Traditional distributions regardless,
Social Security added to the mix) — onto Taxable/Traditional instead.

**The bracket-aware fill resolves this correctly, per-year, instead of committing to any single fixed order:**
draw Traditional first, but only up to a chosen ordinary-bracket ceiling (cheap, and it's 100% ordinary
either way, so there's no cost to taking it early); then Taxable, whose LTCG gets a shot at the low/0%
bracket precisely because ordinary income was capped, not left to climb; then Roth last, so it keeps
compounding tax-free for as long as possible and only gets tapped once the cheaper sources are used up. This
is is genuinely what "minimized taxes over the horizon" means here — not a rule you fix once, but a rule
that reads each year's own tax situation.

## Scope: two parts, buildable and signable-off separately

**Part 1 — bracket-aware SPLIT of an already-sized withdrawal** (this is the part that directly delivers
"minimize taxes" and is safe to build now). **Part 2 — a new dynamic TOTAL-sizing strategy** that also closes
`funding_gap` via fixed-point iteration (a bigger, genuinely iterative feature MODEL_WIRING.md's own text
flags as "meaningfully larger than any single step so far" — buildable as a signed-off follow-on once Part 1
is live and tested, not required to answer your original question). Keeping them separate means Part 1 ships
fast and low-risk; Part 2 is real, but shouldn't block Part 1.

---

## Part 1 — bracket-aware split (the tax-minimization fix)

### What stays exactly the same

`annual_withdrawal_target` (today's flat-4%-of-portfolio dispatch) still computes the year's TOTAL nominal
withdrawal target, exactly as it does now — Part 1 does not touch total sizing at all. Dividend netting
(`dividends_available`, `sale_amount_needed`) stays exactly as-is too. The only thing that changes is **which
accounts the `sale_amount_needed` dollars come from** — replacing the current `draw_order_fill` call (fixed
`DEFAULT_DRAW_ORDER`) with the new function below.

### New function — `modules/investing.py`: `bracket_aware_draw`

Reuses `draw_order_fill` three times, chained — no new lot-selling logic needed, since `draw_order_fill`
already accepts a restricted `draw_order` list and a specific `dollars_needed` target per call, and its own
`remaining_lots` is exactly what the next call needs as input:

```python
def bracket_aware_draw(
    traditional_target: float,
    taxable_target: float,
    roth_target: float,
    lots: list[dict],
    prices_by_ticker: dict[str, float],
    current_year: int,
    sale_method: str = "hifo",
) -> dict:
    """
    MODEL_WIRING.md §5.3 Stage 2 — sells up to `traditional_target` from Traditional 401(k)/IRA,
    then up to `taxable_target` from Taxable, then up to `roth_target` from Roth 401(k)/IRA — three
    INDEPENDENT dollar targets (the caller has already decided the split; this function only
    executes it), chained through draw_order_fill so each stage's remaining_lots feeds the next.
    HSA/Other are never touched, same as DEFAULT_DRAW_ORDER.

    Returns the same shape as draw_order_fill, plus a per-bucket shortfall breakdown:
    {
        "sales_by_account_type": {...},        # merged across all three stages
        "remaining_lots": [...],
        "total_proceeds": float,
        "unfunded_shortfall": float,            # sum of all three stages' own shortfalls
        "unfunded_shortfall_by_bucket": {"traditional": float, "taxable": float, "roth": float},
    }
    """
    traditional = draw_order_fill(
        traditional_target, lots, prices_by_ticker, current_year,
        draw_order=["Traditional 401(k)", "Traditional IRA"], sale_method=sale_method,
    )
    taxable = draw_order_fill(
        taxable_target, traditional["remaining_lots"], prices_by_ticker, current_year,
        draw_order=["Taxable"], sale_method=sale_method,
    )
    roth = draw_order_fill(
        roth_target, taxable["remaining_lots"], prices_by_ticker, current_year,
        draw_order=["Roth 401(k)", "Roth IRA"], sale_method=sale_method,
    )
    return {
        "sales_by_account_type": {
            **traditional["sales_by_account_type"],
            **taxable["sales_by_account_type"],
            **roth["sales_by_account_type"],
        },
        "remaining_lots": roth["remaining_lots"],
        "total_proceeds": traditional["total_proceeds"] + taxable["total_proceeds"] + roth["total_proceeds"],
        "unfunded_shortfall": (
            traditional["unfunded_shortfall"] + taxable["unfunded_shortfall"] + roth["unfunded_shortfall"]
        ),
        "unfunded_shortfall_by_bucket": {
            "traditional": traditional["unfunded_shortfall"],
            "taxable": taxable["unfunded_shortfall"],
            "roth": roth["unfunded_shortfall"],
        },
    }
```

### New helper — `modules/tax.py`: `ordinary_bracket_ceiling`

```python
def ordinary_bracket_ceiling(brackets: list[list[float]], target_rate: float) -> float:
    """
    The taxable-income threshold at which `target_rate`'s bracket ENDS — the lower bound of the
    next bracket up. E.g. for federal single 2025 brackets, target_rate=0.22 -> $103,350 (the
    22%-bracket ceiling, where 24% begins). Returns float('inf') if target_rate is the top bracket.
    Raises ValueError if target_rate isn't one of `brackets`' own rates — a typo/mismatched-year
    bug should fail loudly here, not silently return an unrelated boundary.
    """
    for i, (lower, rate) in enumerate(brackets):
        if abs(rate - target_rate) < 1e-9:
            return brackets[i + 1][0] if i + 1 < len(brackets) else float("inf")
    raise ValueError(f"target_rate {target_rate!r} is not one of this bracket table's own rates: {brackets}")
```

### The per-year split algorithm — `modules/projection.py`, replacing the Step 6 sale call

Where today's code (line ~841) calls `draw_order_fill(sale_amount_needed, ...)` with the fixed default order,
Part 1 instead sizes the three buckets first, then calls `bracket_aware_draw`:

```python
# 1. Non-discretionary ordinary income already locked in for this year, independent of the
#    withdrawal decision itself: taxable dividends/interest already received, ordinary break
#    income (pension proxy today), and SS's taxable portion (0.0 until Module F exists — see the
#    "Known gap" note below, NOT silently omitted).
other_ordinary_income = taxable_ordinary + taxable_interest + gross_break + ss_taxable_amount_placeholder

# 2. How much Traditional headroom is left before the target bracket's own ceiling — never
#    negative (already-locked-in income may already exceed the ceiling, e.g. a high-break-income
#    year; that's a real, reportable case, not a bug).
target_rate = withdrawal_strategy_params.get("target_bracket_rate", 0.22) if withdrawal_strategy_params else 0.22
bracket_year_table = bracket_table[_bracket_year_for(year, bracket_table)]
ceiling = ordinary_bracket_ceiling(bracket_year_table["federal_brackets"][filing_status], target_rate)
traditional_headroom = max(0.0, ceiling - other_ordinary_income)

# 3. Split sale_amount_needed across the three buckets, Traditional first (capped at headroom AND
#    at what's actually left in Traditional — see the "capacity cap" note below), then Taxable,
#    then Roth for whatever's left.
traditional_target = min(sale_amount_needed, traditional_headroom)
remaining_after_traditional = sale_amount_needed - traditional_target
taxable_target = remaining_after_traditional  # Taxable is uncapped by bracket logic in Part 1 —
                                               # see "Known simplification" below
roth_target = 0.0  # only used if Taxable can't cover it — see the capacity-cap note

withdrawal_sale = bracket_aware_draw(
    traditional_target, taxable_target, roth_target, current_lots, current_prices, year,
    sale_method=sale_method,
)
```

**Known simplification, flagged not hidden**: Part 1's Taxable target is "whatever's left after Traditional,"
not itself bracket-limited against the LTCG 0%/15%/20% thresholds — a literal LTCG-bracket-aware Taxable
split (only realizing 0%-eligible gains, deferring the rest) is a natural Part 1.5 extension using the same
`ordinary_bracket_ceiling`-style helper against `ltcg_brackets` instead of `federal_brackets`, stacked on top
of `other_ordinary_income + traditional_target` (mirroring `_stacked_bracket_tax`'s own floor mechanic) —
worth doing, but scoped out here to keep Part 1 shippable; flag before deciding whether to fold it in now or
as an immediate follow-on.

**Capacity cap, needed regardless of the above**: `traditional_target`/`taxable_target` must each also be
capped at that bucket's OWN current portfolio value (can't target more than what's actually held) — read the
per-bucket value via the same `portfolio_value_by_account_type` helper already built for
`WEALTH_BY_ACCOUNT_TYPE_CHARTS.md`, and roll any shortfall down into the next bucket in the chain (Traditional
shortfall → added to `taxable_target`; Taxable shortfall → added to `roth_target`) BEFORE calling
`bracket_aware_draw`, so a bucket that's already exhausted doesn't just report an unfunded shortfall when the
next bucket down could have covered it. `bracket_aware_draw`'s own `unfunded_shortfall_by_bucket` is then the
true "even after cascading, we came up short" signal — keep reporting it, per §6's "never silently dropped"
rule, exactly like `withdrawal_unfunded_shortfall` already is today.

### RMDs

```python
def rmd_start_age(birth_year: int) -> int:
    """SECURE 2.0's phased RMD age: 73 for anyone born 1951-1959, 75 for 1960 and later."""
    return 75 if birth_year >= 1960 else 73


def rmd_divisor(age: int, table: dict) -> float:
    """IRS Pub 590-B Table III (Uniform Lifetime Table) divisor for `age`. Ages past the table's
    last entry reuse that last divisor (the IRS table itself bottoms out and holds flat past ~120)
    rather than raising — an RMD must always be computable for any age the model might reach."""
    divisors = table["uniform_lifetime_table"]
    if str(age) in divisors:
        return divisors[str(age)]
    last_age = max(int(a) for a in divisors)
    return divisors[str(last_age)] if age > last_age else _raise_below_table(age, divisors)
```

New `data/rmd_table.json` — the IRS Uniform Lifetime Table, source-cited exactly like `tax_brackets.json`'s
own `_note` field (IRS Pub 590-B Table III, current as of the 2022 update; confirm no newer revision before
transcribing). `modules/tax.py`'s `load_bracket_table` pattern (one I/O boundary, pure function consumes the
already-loaded dict) should be mirrored with a new `load_rmd_table`.

Wiring, right after `bracket_aware_draw` runs (or before, to set the floor — either order is fine since the
capacity-cap logic above already handles bucket exhaustion): if `age_at_year_end >= rmd_start_age(birth_year)`,
compute `rmd_amount = traditional_balance_at_start_of_year / rmd_divisor(age_at_year_end, rmd_table)`. If
`rmd_amount > traditional_target` actually sold, the difference is a **forced extra Traditional sale**, taxed
as ordinary income like any other Traditional distribution, but — per MODEL_WIRING.md's own instruction —
**not consumed**: the proceeds beyond what `sale_amount_needed` required get reinvested into Taxable via
`create_lots`, exactly like a contribution, preserving the ledger identity (§6) rather than vanishing or
inflating `net_retirement_income` with cash that was never actually needed for spending. Report the RMD
amount and whether it forced an above-target sale as new row fields (`rmd_amount`, `rmd_forced_excess`) —
same "report, don't silently absorb" convention as `funding_gap`/`discretionary_income`.

### Fixed-point iteration — is it needed for Part 1?

**No**, and that's worth stating plainly rather than building unneeded complexity: Part 1's split is a direct
computation (bracket ceiling minus already-known income, cascaded), not a circular one — `other_ordinary_
income` doesn't depend on this year's withdrawal split at all. The genuine circularity MODEL_WIRING.md's text
warns about only shows up in Part 2 (closing `funding_gap` — see below), where the TOTAL to draw depends on
the tax owed on that same draw. Keep Part 1 iteration-free; don't add a solver loop that has nothing to
converge on.

### Known gap, honestly carried forward

`ss_taxable_amount_placeholder` in step 1 above is `0.0` until Module F (Social Security) exists — same gap
`taxable_retirement_withdrawal`'s SS-benefit parameter already carries everywhere else in this codebase today.
Wire the parameter through now (don't hardcode `0.0` inline three call-sites deep) so Module F's eventual
landing is a one-line change, not a re-thread.

---

## Part 2 — closing `funding_gap` (dynamic total-sizing), a signed-off follow-on

A NEW `WITHDRAWAL_STRATEGIES` entry — e.g. `"target_net_spending"` — added to the existing dispatch in
`annual_withdrawal_target` (already designed for this: its own docstring says new strategies are "a new
branch here later without a signature rewrite"). Goal: size the TOTAL draw (not just its split) so that
`net_retirement_income` (already a defined field — `total_withdrawal_income - retirement_taxes_paid`) lands
at `spending_need` exactly, rather than reporting a `funding_gap` that never closes.

**The iteration** (per MODEL_WIRING.md §5.3's own explicit instruction — quoted, not reinterpreted): start
from a naive guess (e.g. `spending_need` itself, or last iteration's answer), run the full Part 1 split +
`compute_taxes` pass, compare `net_retirement_income` against `spending_need`, adjust the guess, repeat until
the draw changes by < $1 — capped at ~20 iterations, with the iteration count recorded on the row
(`withdrawal_iteration_count`) and non-convergence surfaced as a warning, never silently accepted as "close
enough" past the cap.

Flag before starting: this is real, self-contained work — a new strategy branch, a new convergence loop, new
test coverage for the solver's own correctness (does it actually converge? does it converge to the *right*
number, verified against a hand-computed fixture?) — sign off on Part 1 first, confirm the bracket-aware
split is producing sensible numbers against your real save file, then scope Part 2 as its own step.

---

## Summary of concrete next actions for Claude Code

1. Confirm Part 1's scope with the user (this document) before starting — per `CLAUDE.md` ground rule 2, one
   module/step at a time.
2. Add `data/rmd_table.json` (IRS Uniform Lifetime Table, source-cited) and `load_rmd_table` in
   `modules/tax.py`.
3. Add `ordinary_bracket_ceiling`, `rmd_start_age`, `rmd_divisor` to `modules/tax.py`.
4. Add `bracket_aware_draw` to `modules/investing.py`.
5. Wire the capacity-cap cascade + bracket split + RMD floor into `modules/projection.py`'s Step 6 block,
   replacing the current `draw_order_fill` call — add `rmd_amount`/`rmd_forced_excess` to the row schema.
6. Add a `target_bracket_rate` input to the Projection tab (`st.selectbox`, options like the existing bracket
   rates for the user's filing status, default `0.22`) alongside the existing `sale_method`/`rebalance_band`
   controls, same widget conventions.
7. Tests: `ordinary_bracket_ceiling` against the known 2025 single-filer boundaries; `rmd_divisor`/
   `rmd_start_age` against IRS published values for a few ages/birth years; `bracket_aware_draw` against a
   synthetic three-account portfolio (confirm Traditional is capped at the bracket ceiling, Roth is only
   touched once Taxable is exhausted); an end-to-end `project_multi_year` run confirming total realized tax
   over a full retirement horizon is lower under the new split than under `DEFAULT_DRAW_ORDER`'s old
   Taxable-first-fixed order, for at least one representative scenario.
8. Run against your real save file and confirm the split "looks right" by eye for a few early retirement
   years before treating this as done — same verification bar every prior spec in this project has used.
9. Part 2 (`target_net_spending`) stays a separate, later sign-off — not started as part of this step.
