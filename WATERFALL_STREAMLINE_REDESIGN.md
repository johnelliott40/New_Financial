# Streamlining the contribution engine — spec for Claude Code

**Prepared by:** Claude (Cowork), 2026-08-16. Covers three related requests: (1) simplify the
"patchwork" contribution waterfall without losing behavioral fidelity, (2) guarantee that 100% of
unspent income always gets invested somewhere (taxable at minimum), and (3) redefine the "coasting"
phase between `savings_stop_date` and `withdrawal_start_date` as a true freeze — no income, no
expenses, no new contributions, portfolio growth from dividend/interest reinvestment only — reverting
to today's exact withdrawal-phase behavior at `withdrawal_start_date`. **Resolved with the user
before writing this**: during coasting, income and expenses are frozen entirely (not tracked, not
partially invested) — this is what makes requests #2 and #3 consistent rather than contradictory;
#2's "invest everything" guarantee applies to the accumulation phase (before `savings_stop_date`),
#3's "zero accumulation" is coasting's own, deliberately different rule.

---

## Part 1 — Why it feels like patchwork (and it is)

Concrete, code-cited reasons, all in `modules/projection.py`:

1. **Two entirely separate code paths** for "how much went to each destination this year" — the
   manual-override branch (lines 481-522) and the waterfall branch (lines 524-589) — each
   re-deriving `w2_401k_used`/`roth_401k_used`/`se_employee_used`/`se_employer_used`/
   `traditional_ira_used`/`roth_ira_used`/`employer_match` independently, by different logic. A
   fix to one (like last session's manual-override fix) doesn't automatically apply to the other.
2. **`waterfall_allocations` is an optional side-channel**, `None` unless the waterfall branch
   specifically ran (line 438), and the ONE place that reads "how much went to Taxable"
   (`(waterfall_allocations or {}).get("taxable", 0.0)`, line 789) silently falls back to `$0`
   whenever it's absent — which is every code path except the waterfall branch. This is the exact
   mechanism behind both bugs found so far (manual-override years, and — see Part 2 below — every
   other path too).
3. **A bolted-on sub-waterfall**: `allocate_401k_family_remaining` (in `modules/investing.py`) is a
   separate 3-way split (W-2 remainder / SE employee / SE employer) that exists outside the main
   `contribution_waterfall` priority-list mechanism, reusing `modules.tax.compute_max_401k_contributions`'s
   own internal ordering. It works, but it means "the waterfall" is actually two different pieces of
   logic wired together, not one mechanism.
4. **No unconditional catch-all.** Nothing in this codebase currently *guarantees* that leftover
   money lands somewhere — Taxable getting funded is a side effect of the waterfall branch
   specifically running and reaching its last step, not a structural guarantee.

## Part 2 — The "unspent money disappears" bug, and a double-counting trap the fix must avoid

### 2.1 The bug, generalized

Last session found this for manually-overridden years specifically. It's actually broader: **any
code path other than the waterfall branch running to completion** leaves `waterfall_allocations =
None`, so Taxable gets `$0` regardless of actual leftover profit. That includes:
- Every manually-overridden year (found last session).
- Every year where `use_contribution_waterfall=False` altogether — the `else` branch (lines
  590-594) sets every contribution to `$0` and never touches `waterfall_allocations` either, so
  **100% of that year's profit is untracked**, not just the tax-advantaged portion.
- The `if investable <= 0:` short-circuit inside the waterfall branch itself (lines 535-540) is
  fine — it correctly sets `waterfall_allocations = {"uninvested_surplus": 0.0}`, i.e. explicitly
  reports zero, which is correct when there's genuinely nothing to invest.

### 2.2 The fix: an unconditional final step, not a side-channel dict

Restructure so "how much reaches Taxable" is **always computed, once, at the end of the per-year
contribution logic**, regardless of which path (manual entry, waterfall, or neither) decided the
tax-advantaged amounts:

```python
# After w2_401k_used, roth_401k_used, se_employee_used, se_employer_used, traditional_ira_used,
# roth_ira_used are resolved by WHATEVER path decided them (manual, waterfall, or $0 if
# use_contribution_waterfall is off and nothing was entered) —
tax_advantaged_used = (
    w2_401k_used + roth_401k_used + se_employee_used + se_employer_used
    + traditional_ira_used + roth_ira_used
)
taxable_used = max(0.0, investable - tax_advantaged_used)   # ALWAYS computed — no dict, no None
uninvested_surplus = max(0.0, investable - tax_advantaged_used - taxable_used)  # ~always 0.0 now,
    # since Taxable is uncapped; kept as an explicit reportable value per §6's own "never silently
    # drop a dollar" principle, not removed
```

This makes `taxable_used` a first-class local, computed the same way every single time, instead of
a `.get("taxable", 0.0)` lookup into a dict that may or may not exist. It's a small change with a
large effect: it closes the manual-override gap, the waterfall-off gap, and any future gap of the
same shape, all at once, structurally — a new code path added later can't silently reintroduce this
bug the way it did before, because there's no longer an optional dict to forget to populate.

**This requires `investable` to be defined and computed on every path, not just inside the waterfall
branch** — see Part 3 for the redefinition needed anyway (to avoid double-counting, below), which
naturally makes `investable` available everywhere it's needed.

### 2.3 A double-counting trap this fix must not introduce

Before this fix, `uninvested_surplus`/the missing-Taxable-money bug was *accidentally* absorbing a
real, separate, pre-existing problem: **Taxable-account dividends/interest are already
auto-reinvested by `modules/investing.py::roll_forward_portfolio`** (its own §4.3 reinvestment
rule — happens unconditionally pre-withdrawal, independent of the contribution waterfall
entirely, lines 792 in `projection.py`: `reinvestment_lots` are added to the ledger regardless of
what the waterfall computes). That same dividend/interest cash **also** flows into `gross_income` →
`profit` → today's `investable = max(0.0, baseline_profit) * saving_fraction`.

**Once Taxable becomes a guaranteed catch-all for 100% of `investable`, this becomes a real,
visible double-count**: the after-tax portion of Taxable dividend/interest income would get
invested a *second* time — once automatically (the existing, correct mechanism), once more via the
new guaranteed catch-all treating it as ordinary "profit" to place. Today this mostly goes
unnoticed because the missing-Taxable-money bug was silently swallowing most of it anyway (two
bugs partially masking each other). Fixing §2.2 alone, without addressing this, would trade an
under-counting bug for an over-counting one.

**The fix**: exclude the row's own `gross_investment_income` (the field `modules/projection.py`
already computes — Taxable qualified/ordinary dividends + interest, the exact amount
`roll_forward_portfolio` is already reinvesting) from what feeds `investable`:

```python
investable = max(0.0, baseline_profit - baseline_gross_investment_income)
```

using the same `$0`-contribution baseline pass the code already runs for `baseline_tax`/
`baseline_profit` (lines 530-532) — `baseline_gross_investment_income` is just `gross_investment_income`
for that row, already computed earlier in the loop (line 359), reused, not recomputed. This is a
conservative approximation (it subtracts the *pre-tax* dividend amount, slightly under- rather than
over-counting `investable` by the tax already paid on it) — flagged as exactly that kind of
documented, deliberate simplification this codebase already uses elsewhere (e.g. the dissaving/
withdrawal one-pass tax approximation), not a precision gap worth chasing further.

**Only Taxable-account income needs excluding** — Traditional/Roth 401(k)/IRA/HSA distributions
never reach `gross_income` at all (only the `"Taxable"` bucket of `roll_result["distribution_by_account_and_type"]`
is ever read out, line 352), so there's no equivalent double-count risk for tax-advantaged accounts.

## Part 3 — The coasting-phase freeze

### 3.1 Precise behavior

From `savings_stop_date` through `withdrawal_start_date` (exclusive of the day withdrawals start,
matching every other phase-boundary convention already in `phase_flags_for_year`): **no W-2/SE/break
income, no expenses, no contributions of any kind.** The portfolio's only growth mechanism during
this window is `roll_forward_portfolio`'s existing, untouched dividend/interest-reinvestment
logic. At `withdrawal_start_date`, the model reverts to exactly today's withdrawal-phase behavior —
no change requested or needed there.

### 3.2 Where to implement it — at the point of consumption, not inside the projector modules

`modules/gross_income.py::project_gross_income`/`project_expenses` should **not** need to know about
`savings_stop_date` at all — keep that module scoped to "earning/spending windows," as it already is
(its own docstrings are explicit that expenses have no retirement-linked gating by design). Instead,
apply the freeze in `modules/projection.py`'s per-year loop, where `phase_flags_for_year`'s fractions
are already computed (lines 339-341):

```python
is_coasting = saving_fraction <= 0.0 and withdrawing_fraction <= 0.0
if is_coasting:
    gross_w2 = gross_se = gross_break = 0.0
    gross_expense = 0.0
```

Applied immediately after pulling `gross_w2`/`gross_se`/`gross_break`/`gross_expense` from
`income_rows`/`expense_rows` (around lines 325-328), before anything downstream (`gross_income`,
`_tax_for`, the contribution engine) ever sees them. Everything else — investment income, tax on
investment income, the (now-fixed) contribution engine naturally computing `investable ≈ $0` since
both `baseline_profit` and `gross_expense` are `$0` — falls out correctly with no further
special-casing. `saving_fraction`/`withdrawing_fraction` being independently `0`/`0` is already
exactly "coasting" by the existing phase model — no new state needed, just this one gate.

### 3.3 `retirement_date` vs. `savings_stop_date` — needs a decision

Today, `retirement_date` (not `savings_stop_date`) is what stops `gross_w2`/`gross_se` inside
`project_gross_income`. With this change, `savings_stop_date` becomes the *effective* stop for
income too, whenever it's earlier than `retirement_date` — which it is in every case seen so far
(the user's own save: `savings_stop_date=2056`, `retirement_date=2061`). Recommend gating on
`min(retirement_date, savings_stop_date)`'s year, effectively: the freeze in §3.2 already achieves
this without touching `retirement_date` at all (it's a separate, additive override on top of
whatever `project_gross_income` already computed), so **no change to `project_gross_income`'s own
`retirement_date` handling is needed** — the coasting freeze simply zeroes out whatever it produced,
for any year where `saving_fraction<=0`. Worth confirming with the user whether `retirement_date`
should still mean something distinct once this ships (e.g. a health/eligibility marker for a future
module), or whether it becomes effectively redundant with `savings_stop_date` for everyone whose
plan has `savings_stop_date <= retirement_date` (the normal case) — not a blocking question, just
flag it so `retirement_date`'s role doesn't quietly become dead weight without anyone noticing.

### 3.4 The transition year needs explicit handling — don't guess silently

`saving_fraction` is day-weighted for the year `savings_stop_date` falls in (e.g. `0.62` if it's
62% of the way through the year) — a straight `<= 0.0` gate means the **entire** transition year
still earns/spends normally, and the freeze only fully applies starting the next full year. Two
reasonable options, pick one and document the choice rather than leaving it implicit:

- **(a) Simplest — freeze only takes effect the first FULL year after `savings_stop_date`.** The
  transition year behaves exactly as it does today (full income/expenses, `saving_fraction`-scaled
  `investable`, now correctly caught by Taxable per Part 2). Minimal new edge-case surface.
- **(b) More precise — pro-rate the transition year itself**, splitting it into an
  earning/spending portion (before `savings_stop_date`) and a frozen portion (after), mirroring how
  `project_gross_income` already day-weights the `retirement_date` transition internally. More
  correct, more code, more tests — a real, separate day-weighting problem, not a one-line change on
  top of §3.2's gate.

Recommend (a) for a first pass — it's a small, contained, easily-tested piece of behavior, and (b)
can follow once (a) is verified against a real save file (the user's own is a good test case: a
5-year coasting window, 2056-2061).

## Part 4 — Suggested build order (ship incrementally, not as one giant change)

1. **Part 2.2 + 2.3 together** (the guaranteed-Taxable-catchall, with the double-count exclusion) —
   self-contained, high value, fixes three symptoms across two separate sessions now.
   Regression tests: (a) a manually-overridden year with only Roth IRA set still funds Taxable with
   the remainder; (b) `use_contribution_waterfall=False` with real profit still funds Taxable; (c) a
   year with meaningful Taxable dividend income does **not** show inflated Taxable growth from
   double-reinvestment — assert `portfolio_value` growth matches hand-computed expectations for a
   synthetic single-ticker, single-account scenario.
2. **Part 3** (coasting freeze) — depends on nothing in step 1 beyond it being correct first (a
   coasting year with $0 income/expense will trivially route ~$0 through the now-fixed engine, so
   testing order matters less than it might seem, but shipping 1 first keeps each change reviewable
   independently). Regression test: using the user's own save shape (`savings_stop_date` 5 years
   before `withdrawal_start_date`), confirm `gross_w2`/`gross_expense` are exactly `$0` for every
   coasting year, `portfolio_value` still grows from dividend reinvestment alone, and the first
   withdrawal-phase year behaves identically to today's (unchanged) withdrawal logic.
3. **The two-branch unification** (Part 1, points 1-2 — manual vs. waterfall as one pipeline rather
   than two) is the larger, riskier refactor of the three. It's real architectural debt worth
   fixing, but it's not blocking either of the other two — recommend doing it as its own follow-up
   once 1 and 2 are shipped and verified, not bundled into the same change. Sketch of the target
   shape, for when that work starts: resolve each of the six destinations independently — "use the
   manually-entered amount if this specific field was explicitly set this year, else use the
   waterfall's computed amount for this slot" — rather than one whole-year boolean choosing between
   two entirely separate code blocks. This also fixes the earlier bug's root cause structurally
   (a manual entry in one field can no longer even theoretically suppress another field's
   auto-computed amount, because there's only one computation path left, not two to keep in sync).

## Summary of concrete next actions for Claude Code

1. Implement Part 2.2 + 2.3 (guaranteed Taxable catch-all, double-count-safe `investable`), with the
   three regression tests listed in Part 4 step 1.
2. Implement Part 3 (coasting freeze) using approach (a) from §3.4, with the regression test in Part
   4 step 2. Confirm with the user whether `retirement_date` needs a documented role update (§3.3).
3. Verify both against the user's real save file (`saved_states/real_portfolio_waterfall_fixed.json`
   or a fresh save) end to end in the running app before considering this done.
4. Treat the two-branch unification (Part 4 step 3) as a separate, later piece of work — get
   explicit sign-off before starting it, per this repo's own working agreement.
5. Update `WATERFALL_1PAGER.md` once this ships — it currently documents pre-redesign behavior on
   purpose; regenerate or hand-edit it to match the new architecture rather than leaving it stale.
6. Update `NEXT.md` when done, same working agreement as before.
