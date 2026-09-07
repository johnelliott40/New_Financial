# Replacing the waterfall with a per-destination toggle system — spec for Claude Code

**Prepared by:** Claude (Cowork), 2026-08-16. This **replaces** the priority-order waterfall
(`modules/investing.py::contribution_waterfall`/`allocate_401k_family_remaining`) and the manual-
override/waterfall dual-branch in `modules/projection.py` (lines 481-589) with a system where every
destination has an explicit, per-year mode you control — max, or a manual amount that's always
checked against that year's real legal limit. **It does not replace or conflict with the two fixes
in `WATERFALL_STREAMLINE_REDESIGN.md`** — the guaranteed-Taxable-catch-all (that document's Part 2)
and the coasting-phase freeze (Part 3) are still exactly right and get built INTO this new system
from the start, more cleanly than they could have been retrofitted onto the old one. Only that
document's Part 1/Part 4-step-3 ("unify the two branches") is now moot — there's only one branch
left once this ships.

**A key interpretation call, made explicit up front, since it shapes everything below**: your
message describes "a toggle to have maximum 401k pre-tax contributions or maximum roth 401k
contributions or custom contributions" — I've read this as **one mutually-exclusive choice per
year** (pretax-maxed, OR Roth-maxed, OR a custom amount — not "pretax and Roth independently
adjustable, both active the same year"). If you actually wanted independent pretax/Roth controls
that can both be active in the same year, say so before this gets built — it changes the shape of
the data model in §2 below, not just a UI label.

---

## 1. The two-stage design, matching your own framing exactly

> "First we build a method to allocate what dollars should be contributed to retirement accounts
> based on earned income, then we pay expenses and use the remaining dollars to contribute to the
> accounts — first 401k, then roth, then traditional, then taxable."

This maps to two genuinely separate computations, which is also why this is a cleaner architecture
than what exists today (today's code tangles both together in one pass):

**Stage 1 — Targets** (`resolve_*_target` functions, pure, driven by earned income + IRS limits
only, no cash-flow involved): for each destination, what's the *legally allowed* amount given this
year's mode? Entirely a function of `gross_w2`, `se_earnings`, `filing_status`, `age`, `bracket_table`
— never of whether you can actually afford it.

**Stage 2 — Funding** (`fund_from_available_cash`, driven by actual liquid cash, in your stated
priority order): given the Stage-1 targets, and given what's actually left after tax and expenses,
fund Roth IRA, then Traditional IRA, then Taxable (uncapped, guaranteed) — each capped at
`min(its own target, whatever cash remains)`.

**One important asymmetry, preserved from the current (correct) codebase, not something this
redesign changes**: **401(k) is not part of Stage 2 at all.** It's a payroll deduction — money
deducted from your paycheck *before* it ever becomes liquid cash, not something funded from
after-tax profit the way Roth IRA/Traditional IRA/Taxable are (this is already how
`modules/projection.py` computes `net_income` today — "those dollars are deducted from payroll/net
profit BEFORE they're ever liquid cash," its own docstring, lines 163-169). So Stage 1's 401(k)
target is funded up to its own legal ceiling directly (limited only by compensation — you can't
defer more than you earned — never by "is there enough cash left after expenses," since there's no
such constraint on a payroll deduction). Tax is then computed *with* that real deduction applied,
*then* Roth IRA/Traditional IRA/Taxable compete for what's actually left. This is why your priority
order ("first 401k, then roth, then traditional, then taxable") is right, but 401(k) getting funded
first isn't really "first in a cash queue" — it happens before there's a cash queue to be first in
at all. Flagging this precisely so it doesn't get built as "401k also capped by remaining profit,"
which would be a real regression from what's correct today.

---

## 2. The 401(k) toggle

Three modes, one choice per year:

| Mode | What it targets |
|---|---|
| **Max pretax** | The full combined statutory max (W-2 employee deferral remaining room + SE solo-401(k) employee deferral + SE solo-401(k) employer contribution), all as **pretax** — reusing `modules.tax.compute_max_401k_contributions`'s existing, already-correct dependency-safe ordering (W-2, then SE employee, then SE employer) unchanged. |
| **Max Roth** | Same combined limit, but the **employee-side** portions (W-2 deferral + SE employee deferral) target Roth instead. **SE employer contributions are always pretax by law** — there is no Roth solo-401(k) employer contribution — so this mode maxes the employee side as Roth and separately maximizes the SE employer side as pretax (see the checkbox below). Flag this distinction in the UI copy so it isn't surprising. |
| **Custom** | A dollar amount you enter, plus a pretax/Roth selector for it (defaults to pretax). Applied first against the W-2 plan's own remaining capacity; anything beyond that rolls into SE-employee deferral capacity, reusing `allocate_401k_family_remaining`'s existing dependency order rather than inventing a new split. |

**SE employer contribution, independent of the above**: since "a custom employer contribution" isn't
a well-defined concept the way an employee election is (an employer either contributes its
statutory max or doesn't), give it its own checkbox — **"Also maximize the SE solo-401(k) employer
contribution"** — independent of whichever employee-side mode is selected, defaulting to checked
whenever there's SE income and unchecked (so, `$0`) when there isn't (matches today's behavior for
a W-2-only filer, like your own current save).

The employer-match mechanic is completely unaffected by any of this — it's still auto-computed on
top of whatever W-2-side deferral (pretax or Roth) ends up targeted, via the existing
`employer_401k_match`, unchanged.

## 3. The Roth IRA toggle

| Mode | What it targets |
|---|---|
| **Maximize** | `modules.tax.roth_ira_phase_out_max`, computed **fresh, every run**, from that year's actual `federal_agi` → MAGI. This directly fixes the earlier bug's root cause, not just its symptom: last session's "Fill Roth IRA at each year's max" button computed this **once** and froze the number — this mode recomputes it live every time, so it can never go stale again as your income assumptions change. |
| **Custom** | A dollar amount you enter, clamped to `min(entered, roth_ira_phase_out_max(...))` — exactly your own example: enter $5,000, legal limit is $3,000, you get $3,000. |

## 4. The Traditional IRA toggle

Same two modes, but its capacity is **not independent** — it shares one combined annual limit with
Roth IRA (`modules.tax.max_ira_contribution_limit`), and per your own priority order, **Roth
resolves first**:

```python
roth_ira_used = resolve_roth_ira_target(...)                         # §3, resolved first
traditional_ira_room = max_ira_contribution_limit(...) - roth_ira_used
traditional_ira_target = (
    traditional_ira_room if mode == "maximize"
    else min(custom_amount, traditional_ira_room)                    # your "system of checks"
)
```

This is the "system of checks to make sure I don't overcontribute" you asked for — Traditional's
target is *structurally* defined as whatever's left of the combined limit after Roth, so the two
can never sum past the legal cap, by construction, not by a separate validation step that could be
skipped or get out of sync.

**One pre-existing gap, unrelated to this redesign, worth a one-line flag so it isn't mistaken for
new**: this codebase doesn't model Traditional IRA deductibility (`compute_taxes` has no
Traditional-IRA-deduction parameter) — a Traditional IRA contribution here doesn't reduce AGI, same
as before this change. Not something this document fixes; just don't let it read as a new bug.

## 5. Taxable — unchanged from last session's fix

Whatever's left of available cash after Roth IRA and Traditional IRA are funded, uncapped,
guaranteed — this is exactly `WATERFALL_STREAMLINE_REDESIGN.md` Part 2.2's fix, which this new
system should implement from the start rather than retrofit:

```python
tax_advantaged_used = roth_ira_used + traditional_ira_used  # 401(k) already deducted upstream,
                                                              # not part of this sum — see §1
taxable_used = max(0.0, available_cash - tax_advantaged_used)   # always computed, never optional
```

`available_cash` must still exclude the portion of profit that's *already* auto-reinvested by
`roll_forward_portfolio` (Taxable dividends/interest pre-withdrawal) — the exact double-counting
trap `WATERFALL_STREAMLINE_REDESIGN.md` Part 2.3 documents. Reuse that fix here verbatim; don't
recompute `available_cash` as raw `profit`.

---

## 6. Proposed function shapes (`modules/investing.py`, or a new `modules/contributions.py` — this
is now substantial enough logic to deserve its own module; your call whether it's worth the split)

```python
def resolve_401k_target(mode, custom_amount, custom_type, also_maximize_se_employer,
                         tax_year, age_at_year_end, gross_w2, se_earnings, bracket_table) -> dict:
    """Stage 1. Returns {"w2_pretax_target", "w2_roth_target", "se_employee_pretax_target",
    "se_employee_roth_target", "se_employer_target"} — legally-capped, cash-flow-independent."""

def resolve_roth_ira_target(mode, custom_amount, tax_year, filing_status, age_at_year_end,
                             magi, bracket_table) -> float: ...

def resolve_traditional_ira_target(mode, custom_amount, combined_ira_limit, roth_ira_used) -> float: ...

def fund_from_available_cash(roth_ira_target, traditional_ira_target, available_cash) -> dict:
    """Stage 2. Returns {"roth_ira_used", "traditional_ira_used", "taxable_used"} — Roth first,
    then Traditional, then Taxable uncapped with whatever remains. Does NOT touch 401(k) — that's
    resolved upstream as a payroll deduction before available_cash is even computed (§1)."""
```

`contribution_waterfall`, `allocate_401k_family_remaining`, and `DEFAULT_CONTRIBUTION_PRIORITY` in
`modules/investing.py` become dead code once this ships — remove them rather than leaving an unused
second mechanism sitting alongside the new one (confirm nothing else references them first — a
quick grep across `ui/` and `tests/`).

## 7. Data model & migration

Replace `contribution_by_year`'s current six-raw-dollar-field shape with, per year:

```python
{
    "contribution_401k_mode": "max_pretax" | "max_roth" | "custom",
    "contribution_401k_custom_amount": float,        # only meaningful when mode == "custom"
    "contribution_401k_custom_type": "pretax" | "roth",
    "maximize_se_employer_401k": bool,
    "roth_ira_mode": "maximize" | "custom",
    "roth_ira_custom_amount": float,
    "traditional_ira_mode": "maximize" | "custom",
    "traditional_ira_custom_amount": float,
}
```

**Migration for existing saves** (including your own `real_portfolio_waterfall_fixed.json`):
default every existing year to `mode="custom"` with `custom_amount` set to whatever raw dollar value
was already stored — this preserves *exact* prior numeric behavior on load, least surprising. Add a
one-click "switch all years to Max/Maximize" action in the UI afterward so moving to the new
recommended default is one click, not 71 manual edits — this is exactly the kind of bulk action last
session's bug (a bulk-fill button silently freezing values) should have been in the first place, so
build it as a genuine recompute-every-run mode this time, not another one-time-stamp button.

## 8. UI shape — a real design call, not a small one

Today's grid (`ui/projection_tab.py`'s `st.data_editor`, lines 520-544) has 6 raw-dollar columns.
The new model needs mode dropdowns *and* amount fields — up to 8 columns per year, which is a lot
for one scrolling grid. Two reasonable directions, pick one:

- **(a) Keep one wide grid.** Simplest to build, most consistent with today's UX, but a lot of
  horizontal scrolling for 70+ years.
- **(b) A default mode/amount above the grid ("apply to all years"), with the grid itself only for
  exceptions.** Less scrolling, matches how most users will actually use this (one strategy for
  most of their working life, maybe a couple of exception years) — more UI work to build well.

Recommend (a) first (ships faster, directly matches "an input on the projection tab" as asked), with
(b) as a fast-follow once (a) is live and it's clear whether 70-column-wide scrolling is actually
annoying in practice.

## 9. What this does to `WATERFALL_STREAMLINE_REDESIGN.md`'s remaining open item

That document's Part 4 step 3 ("unify the manual-vs-waterfall branches") is now superseded — there's
no waterfall branch and no manual-override branch left to unify, just one resolution path per
destination. Its Part 2 (guaranteed Taxable catch-all + double-count exclusion) and Part 3 (coasting
freeze) are unchanged and still needed — build them as part of this same pass rather than as a
separate later change, since §5/§9 here depend on Part 2's exact fix.

## 10. Testing checklist

- Your own worked example, locked in as a test: Roth IRA custom `$5,000`, actual legal limit
  `$3,000` (a MAGI you construct to force phase-out) → `roth_ira_used == 3000.0`.
- Same for 401(k): custom `$50,000` pretax with a compensation/statutory ceiling of `$24,500` →
  `w2_pretax_target == 24500.0` (never the entered amount).
- Traditional + Roth combined never exceeds `max_ira_contribution_limit` across a range of
  Roth modes (maximize and custom) crossed with Traditional modes (maximize and custom).
- 401(k) funding is **never** reduced by low `available_cash` — construct a year with real 401(k)
  room but `profit` near `$0`; confirm the 401(k) target still funds in full (payroll-deduction
  semantics, §1), while Roth/Traditional/Taxable correctly shrink to whatever's left.
- Migration test: load a save with the old six-field shape, confirm it lands in `mode="custom"`
  with the exact prior dollar amounts, and that a fresh `project_multi_year` run produces identical
  numbers to before the migration (day-one correctness, not just going forward).

## Summary of concrete next actions for Claude Code

1. Confirm the interpretation call at the top of this document (mutually-exclusive 401(k)
   pretax/Roth mode, not independent controls) before building — it's the one assumption most worth
   a quick sanity check given how much of §2 depends on it.
2. Implement §6's four functions, folding in `WATERFALL_STREAMLINE_REDESIGN.md` Part 2's guaranteed-
   catch-all and Part 2.3's double-count exclusion from the start.
3. Wire `modules/projection.py` to call them in the two-stage order from §1, removing the old
   manual-override/waterfall branches (lines 436-589) entirely.
4. Implement the coasting-phase freeze from `WATERFALL_STREAMLINE_REDESIGN.md` Part 3 as part of
   this same pass.
5. Build the new per-year UI (§8, direction (a) first) and the migration (§7).
6. Remove the now-dead `contribution_waterfall`/`allocate_401k_family_remaining`/
   `DEFAULT_CONTRIBUTION_PRIORITY` from `modules/investing.py` after confirming nothing else
   references them.
7. Run the checklist in §10; update `WATERFALL_1PAGER.md` (it currently documents the old mechanism
   on purpose) and `NEXT.md` when done.
