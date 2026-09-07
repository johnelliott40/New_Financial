# Contribution waterfall bug — root cause found and confirmed against your real save file

**Prepared by:** Claude (Cowork), 2026-08-16. This is a confirmed bug, not a hypothesis — reproduced
directly against `saved_states/real_portfolio.json` with the actual `project_multi_year` code, with
before/after numbers below. This supersedes the "most likely explanation" guesswork in
`RETIREMENT_REPORTING_AUDIT.md` Part 1 for this specific save file — that document's "check whether
target allocations are configured" theory does **not** apply here (your save has real target
allocations configured for Taxable/Traditional 401(k)/Roth IRA/Traditional IRA — that's not the
problem). This is a different, more fundamental bug.

## The root cause, in one sentence

**Every year in your save has a `contribution_by_year` entry (all 71 of them, 2026-2096), and the
moment ANY field in a year's entry is nonzero, `modules/projection.py` treats that year as a full
manual override and permanently disables the automatic waterfall for it — even though you have
`use_contribution_waterfall: true` set.** Your save has a nonzero `roth_ira_contribution` in most
years (almost certainly written by the "Fill Roth IRA at each year's income-phased maximum" button
on the Income & Expenses tab), which is enough to trip this for every one of those years.

## Exactly where, in the code

`modules/projection.py` lines 445-455:

```python
has_manual_override = year_contributions is not None and any(
    year_contributions.get(k, 0.0)
    for k in (
        "w2_401k_contribution", "roth_401k_contribution",
        "se_401k_employee_contribution", "se_401k_employer_contribution",
        "traditional_ira_contribution", "roth_ira_contribution",
    )
)
```

This checks **all six** contribution fields together. If *any one* of them is nonzero — even just
`roth_ira_contribution` — the whole year is flagged as manually overridden.

Then, `project_multi_year`'s `if has_manual_override: ... elif use_contribution_waterfall: ...`
(lines 481, 524) is a plain `if`/`elif` — **the waterfall branch is structurally unreachable for
any year with `has_manual_override=True`**, no matter what `use_contribution_waterfall` is set to.
In the manual branch, your 401(k) contribution comes straight from
`year_contributions.get("w2_401k_contribution", 0.0)` (line 485) — which is `0.0` in your save, for
every year — so `w2_401k_used` is pinned at `$0` regardless of how high `gross_w2` climbs.

**A second, independent consequence of the same bug**: `waterfall_allocations` is initialized to
`None` (line 438) and is **never set** anywhere inside the manual-override branch (lines 481-522).
Later, the dollars actually routed into your Taxable account are computed as:

```python
"Taxable": (waterfall_allocations or {}).get("taxable", 0.0),   # line 789
```

Since `waterfall_allocations` stays `None` for every manually-overridden year, this is `0.0` —
**always** — regardless of your income or profit that year. This is why Taxable holdings aren't
growing from contributions: in every year your save currently treats as "manual," there is no
mechanism at all that can route money there.

## Reproduced against your actual numbers

I ran `project_multi_year` twice — once with your save's real `contribution_by_year` exactly as
stored, once with it cleared to `{}` — everything else identical (your real W-2 curve $100k→$150k,
your real filing status, dates, employer match settings):

| Year | Gross W-2 | **W-2 401(k) — with your saved overrides** | **W-2 401(k) — waterfall running freely** |
|---|---|---|---|
| 2027 | $100,000 | **$0** | **$24,500** |
| 2028 | $102,345 | **$0** | **$24,500** |
| 2029 | $105,766 | **$0** | **$24,500** |
| 2030 | $110,447 | **$0** | **$24,500** |
| 2031 | $116,322 | **$0** | **$24,500** |

Roth IRA shows `$7,500` in both runs for these years — not because it's working correctly, but
because that's the exact frozen number the "Fill Roth IRA" button wrote in once; it isn't being
recomputed against your current income assumptions on any later run, which is very likely also
behind your second symptom ("Roth IRA contributions not occurring despite income levels that make
sense") — for years further out where your income has grown past the Roth phase-out threshold, the
frozen number was never revisited and may no longer reflect real eligibility either way (too high
in some years, incorrectly `$0` in others, depending on what the button computed the one time it
ran).

## The fix — for Claude Code

This needs a real code decision, not a one-line patch — flagging the trade-offs rather than picking
one, per this repo's own "ask rather than guess" convention:

1. **Decouple the override flag per contribution family, not per year as a whole.** Right now one
   boolean covers all six fields. The Income & Expenses tab's grid always round-trips *every* field
   for *every* row (confirmed: `ui/projection_tab.py` lines 548-559 unconditionally writes
   `roth_401k_contribution`/`traditional_ira_contribution`/`roth_ira_contribution` — and, when
   `use_computed_max_401k` is off, the three 401(k)-family fields too — back into
   `st.session_state.contribution_by_year` on every rerun, for every displayed year), so **any**
   grid interaction, including a button meant to touch only one column, permanently seeds every
   other field at whatever it last held. The clean fix: split `has_manual_override` into
   independent checks — e.g. `has_401k_override` (the four 401(k)-family fields),
   `has_traditional_ira_override`, `has_roth_ira_override` — and let each destination fall through
   to the waterfall independently when its own fields are all zero, rather than one field's entry
   blocking the other two families.
2. **Fix the Taxable-never-funded gap independently of #1.** Even with #1 fixed, a year with a
   *genuine* 401(k) override (user deliberately hand-entering a number) still has
   `waterfall_allocations = None`, so Taxable gets nothing. Decide: should a manually-overridden
   year still run the waterfall for whatever wasn't manually specified (i.e., manual entries become
   floors/inputs into the SAME waterfall run, not a total bypass of it)? That's the more correct
   design and matches the docstring's own stated intent ("the manual grid becomes a
   reference/override for years you want to hand-tune" — "override," not "replace entirely"), but
   it's a real behavior change worth confirming before building.
3. **Make the "Fill Roth IRA at each year's max" button not silently disable the waterfall.**
   Independent of #1/#2 (defense in depth): either write its output to a separate, clearly-scoped
   field that `has_manual_override` doesn't check, or show an explicit confirmation before running
   it ("This will disable automatic 401(k)/Taxable contributions for every year — continue?"), or —
   simplest — recompute and re-apply it automatically on every run rather than freezing a one-time
   snapshot, so it stays correct as income assumptions change.
4. **Add a visible warning in the UI, regardless of #1-#3.** When `use_contribution_waterfall=True`
   and one or more years still have `has_manual_override=True`, say so plainly (a banner, not a
   buried caption) — count of years affected, and that 401(k)/Taxable won't be auto-computed for
   them. This would have caught this immediately instead of it silently compounding across 71
   years.

## What I did right now, without waiting on a code fix

I can't run the Streamlit app itself, but the bug lives entirely in *data* (your saved
`contribution_by_year`), not in a place I can't reach — so I cleared it and saved a corrected copy:
**`saved_states/real_portfolio_waterfall_fixed.json`**, identical to your real save in every other
respect (accounts, positions, target allocations, demographics, macro assumptions), with
`contribution_by_year` reset to `{}`. Your original `real_portfolio.json` is untouched. Load the
`_waterfall_fixed` save in the app and the waterfall should immediately start filling 401(k)/Taxable
correctly, based on the numbers above — worth confirming in the running app before treating this as
fully resolved, since I verified this against the pure `modules/projection.py` function directly,
not the full Streamlit session state plumbing around it.

**One caveat**: this clears every year's override, including any Roth/Traditional IRA amounts you
*did* mean to hand-enter deliberately (as opposed to ones the "Fill Roth IRA" button wrote). If you
had specific years you actually wanted overridden, re-enter just those after loading the fixed save
— I had no way to distinguish "button-written" from "deliberately hand-entered" from the data alone,
so I cleared all of it rather than guess which was which.

## Summary of concrete next actions for Claude Code

1. Read this document alongside `modules/projection.py` lines 436-522 and 780-793, and
   `ui/projection_tab.py` lines 445-560 and 818-841 (the "Fill Roth IRA" button).
2. Decide, with the user, between the design options in "The fix" §1-§3 above — this is a real
   behavior change, not a one-line bug fix, so get sign-off before building.
3. Implement the visible-warning fix (§4) regardless — it's low-risk and would have surfaced this
   immediately.
4. Add a regression test to `tests/test_projection.py`: a year with only `roth_ira_contribution`
   set (all other fields `0.0`) should still let the waterfall fund 401(k)/Taxable for that year
   once the fix lands — this exact scenario, locked in.
5. Confirm in the running app that `saved_states/real_portfolio_waterfall_fixed.json` now shows
   401(k)/Taxable contributions growing correctly across the projected years.
