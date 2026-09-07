# CLAUDE.md — Project Instructions

## What this is

A ground-up personal financial planning / retirement model, built incrementally, module by module, with a Streamlit UI. It must work for **any adult in the United States** — no hardcoded personal data, no assumptions specific to one person's income, accounts, or life stage. Every input is user-supplied through the UI or a config object; defaults should be reasonable national averages, not anyone's actual numbers.

Inspiration (methodology only, not code or structure) comes from a prior retirement-calculator Excel workbook. Do not port its structure, hardcoded tables, or spreadsheet-style logic wholesale — re-derive the calculations cleanly in Python, and re-derive the UI cleanly in Streamlit. Do not copy from any previous Streamlit financial project either — this is a fresh design.

## Ground rules

1. **Real dollars throughout.** Every dollar figure in every module is inflation-adjusted (today's-dollar terms) unless explicitly labeled nominal for a specific intermediate calculation (e.g., tax bracket thresholds may need nominal-year lookups before being deflated back). State clearly in code/comments when a value is nominal vs. real.
2. **One module at a time.** Do not build ahead. Each module is proposed, implemented, tested, and reviewed by the user before moving to the next. Check `NEXT.md` at the start of every session to see what's approved to build next.
3. **General-purpose, not personal.** No hardcoded ages, salaries, account balances, or filing status. All such values are inputs with sensible defaults.
4. **Separation of concerns.** Calculation logic lives in `modules/*.py` as pure functions (no Streamlit calls, no I/O side effects). UI code lives in `app.py` and `pages/`. This keeps modules independently testable.
5. **Testable.** Every module gets unit tests in `tests/` before being considered done. Use `pytest`.
6. **Config-driven constants.** Tax brackets, FICA rates, AIME bend points, ETF expense ratios, etc. live in `data/` as structured files (JSON/CSV/YAML), not hardcoded inline, so they can be updated for future tax years without touching logic code.
7. **No silent scope creep.** If a module's requirements are ambiguous, ask the user rather than guessing.

## Module map (see PROJECT_PLAN.md for full detail and build order)

- Module 1 — Portfolio (account locations, ETF universe, expense ratios, returns, cost basis)
- Module 2 — Demographics (age, retirement date, months-to-retirement, health)
- Module 3 — Macro assumptions (inflation, etc.)
- Module B — Income evolution (gross/net, S-curve, gaps, federal/CA tax, FICA, SE income)
- Module C — Expenses evolution (S-curve or flat)
- Module D — Saving & investing (contribution allocation, rebalancing, cost-basis tracking, dissaving)
- Module E — Deterministic wealth calculation (static and with contributions/rebalancing)
- Module F — Historical income record, Social Security / AIME / bend points
- Module G1 — Health index / projected lifespan and retirement funding needs
- Module G2 — Retirement withdrawal strategies (4% rule, dynamic, tax on withdrawals)

## Tech stack

- Python 3.10, `.venv` at project root
- Streamlit for UI
- pandas / numpy for calculation engine
- plotly for charts
- pytest for tests

## Working agreement with the user

- Build incrementally, module by module, with a working demo/review checkpoint after each.
- Update `NEXT.md` after every session to reflect current state and the next approved step.
- Do not jump ahead to later modules without explicit go-ahead, even if the implementation seems obvious.
