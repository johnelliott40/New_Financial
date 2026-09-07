"""
Shared Streamlit session-state initialization.

UI/session concern, not calculation logic — deliberately kept outside modules/ per CLAUDE.md's
separation of concerns (ground rule 4) and PROJECT_PLAN.md's architecture principles.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from modules.demographics import DEFAULT_PLANNING_HORIZON_AGE, HEALTH_STATUS_OPTIONS, date_at_age
from modules.health import DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS
from modules.portfolio import load_asset_classes
from modules.tax import load_bracket_table


def init_session_state() -> None:
    # Each input's session-state key is initialized here, once, and never touched again outside
    # of an explicit user action (e.g. Load) — the widgets bound to these keys are declared with
    # `key=` only (no `value=`), which is the documented-safe pattern for plain widgets. Passing
    # both `value=` and `key=` — or re-deriving `value=` from a session-state dict that's only
    # updated *after* the widget is declared — makes Streamlit fight its own persisted state on
    # rerun, so every edit needs to be entered twice before it sticks.

    # ---- Macro (Portfolio tab) ----
    # `pretax_tax_rate`/`capital_gains_rate` removed 2026-08-30, user request — they only ever fed
    # the display-only "Liquidation value (est.)" estimate (also removed), never the real
    # bracket-based tax engine in modules/projection.py/modules/tax.py.
    if "inflation_rate" not in st.session_state:
        st.session_state.inflation_rate = 0.025

    # ---- Demographics (Module 2, extended by MODEL_WIRING.md §2's four phase dates, 2026-08-10) ----
    if "birth_date" not in st.session_state:
        st.session_state.birth_date = date(1990, 1, 1)
    if "retirement_date" not in st.session_state:
        st.session_state.retirement_date = date(date.today().year + 30, date.today().month, date.today().day)
    if "health_status" not in st.session_state:
        st.session_state.health_status = HEALTH_STATUS_OPTIONS[1]  # "Good"
    # `saving_stop_age` (an age, display-only — nothing ever consumed it, per MODEL_WIRING.md §2.2)
    # is retired in favor of `savings_stop_date` below, one of the spec's four independent phase
    # dates that the future contribution engine (Module D) actually reads. Old saves carrying
    # `saving_stop_age` are migrated to `savings_stop_date` on load — see ui/sidebar.py.
    if "savings_stop_date" not in st.session_state:
        # Default confirmed with the user (2026-08-10): same as retirement_date's own default —
        # most people stop saving when they stop earning. Seeded once here, from
        # retirement_date's value AT THIS POINT, not a live reference to it — freely and
        # independently editable afterward, same "seed once" pattern used elsewhere in this file
        # (e.g. tax_age_at_year_end).
        st.session_state.savings_stop_date = st.session_state.retirement_date
    if "withdrawal_start_date" not in st.session_state:
        # Default confirmed with the user (2026-08-10): same as retirement_date's own default.
        st.session_state.withdrawal_start_date = st.session_state.retirement_date
    if "ss_claim_date" not in st.session_state:
        # Default confirmed with the user (2026-08-10): age 67 (current full retirement age for
        # most birth years) from birth_date — independent of the other three phase dates.
        st.session_state.ss_claim_date = date_at_age(st.session_state.birth_date, 67)
    if "planning_horizon_age" not in st.session_state:
        # MODEL_WIRING.md §1.2 — the projection runs to this age. A planning horizon, not a
        # life-expectancy estimate — Module G1 below adds a SEPARATE, informational expected/
        # percentile-lifespan figure rather than replacing this one (confirmed with the user,
        # 2026-08-30 — see modules/health.py's own top-of-file docstring for the full reasoning).
        st.session_state.planning_horizon_age = DEFAULT_PLANNING_HORIZON_AGE

    # ---- Module G1 (health index / expected lifespan, MODULE_G1_HEALTH_LIFESPAN.md, 2026-08-30) ----
    # A new required input this module's mortality-table lookup needs — SSA Period Life Tables are
    # published separately by sex (see modules/health.py's own data-source docstring for why a
    # blended "unisex" table would be a real accuracy loss, not a rounding difference). Defaults to
    # "Female" arbitrarily (no other input on this tab defaults to a specific option this way, but
    # a selectbox needs SOME starting value) — never silently assumed without being shown/editable.
    if "sex_for_mortality" not in st.session_state:
        st.session_state.sex_for_mortality = "Female"
    # Four flat scalar keys, not one dict — Streamlit widgets bind to one scalar session-state
    # value each (same reason employer_match_rate/employer_match_cap_pct above are two separate
    # keys, not one dict); ui/demographics_tab.py assembles them into the
    # {"Excellent": ..., ...} shape modules.health's own functions expect, at the point of use.
    for _tier, _default in DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS.items():
        _key = f"health_adjustment_{_tier.lower()}"
        if _key not in st.session_state:
            st.session_state[_key] = _default

    # ---- Module F (Social Security AIME/bend points, NEXT.md's own queued 2026-08-31 spec) ----
    # Personal data (a real earnings history), not shared config — same category as
    # ticker_universe/target_allocations, never written to data/. {year: dollars}, one blended
    # W-2+SE figure per historical year — see ui/social_security_tab.py's own docstring.
    if "historical_ss_earnings" not in st.session_state:
        st.session_state.historical_ss_earnings = {}
    # Cross-tab bridge (same "renders earlier in app.py's tab order, fresh every rerun" pattern as
    # portfolio_universe/resolved_ticker_prices) — the Social Security tab's own final computed
    # annual benefit, read by the Projection tab as project_multi_year's new ss_annual_benefit
    # parameter. Defaults to 0.0 (no benefit) until that tab has rendered at least once.
    if "computed_ss_annual_benefit" not in st.session_state:
        st.session_state.computed_ss_annual_benefit = 0.0
    # Same bridge, for the no-income/no-expense baseline scenario specifically (2026-08-31, a real
    # bug fix — that scenario's own "I stop earning entirely today" premise means only ALREADY-
    # EARNED income can have generated Social Security credit, a smaller AIME than the real
    # scenario's; see ui/social_security_tab.py's own comment at the point of computation).
    if "computed_ss_annual_benefit_baseline" not in st.session_state:
        st.session_state.computed_ss_annual_benefit_baseline = 0.0

    # ---- ETF universe (personal: ticker -> asset class, and any in-session return edits) ----
    if "ticker_universe" not in st.session_state:
        st.session_state.ticker_universe = {}  # {ticker: asset_class_code}
    if "asset_class_returns" not in st.session_state:
        # Starts equal to data/asset_classes.json's defaults; editable per class in the UI from
        # there on. Session-only unless saved — see PROJECT_PLAN.md Step 1.
        st.session_state.asset_class_returns = {
            code: info["nominal_return"] for code, info in load_asset_classes().items()
        }
    if "custom_asset_classes" not in st.session_state:
        # User-added asset classes (2026-08-14, user request) — {code: {"label": str,
        # "nominal_return": float}}, the exact same shape load_asset_classes() itself returns, so
        # ui/portfolio_tab.py can merge the two with a plain {**canonical, **custom} and every
        # downstream consumer (the ETF universe builder's "Asset class" dropdown, the per-class
        # return editor, build_universe) needs no special-casing. Personal data, same category as
        # ticker_universe/asset_class_returns above — session/saved-state only, never written back
        # to data/asset_classes.json itself (that file stays the shared, developer-edited canon).
        st.session_state.custom_asset_classes = {}
    if "removed_canonical_asset_classes" not in st.session_state:
        # Built-in (data/asset_classes.json) classes the user removed from THEIR OWN active set
        # (2026-08-14, user request: asset classes should be "addable and removable, the same as an
        # ETF would be") — a set of codes, checked wherever the canonical dict is merged into the
        # session's active `asset_class_defaults` (ui/portfolio_tab.py's `_render_asset_classes`).
        # data/asset_classes.json itself is never touched — "removed" only means "excluded from this
        # session's own active list," same personal-data boundary custom_asset_classes draws.
        st.session_state.removed_canonical_asset_classes = set()

    # ---- Target allocation by account type (MODEL_WIRING.md §3.1, 2026-08-10) ----
    # {account_type: {ticker: weight}} — how new contributions should be invested, consumed by the
    # Income & Expenses tab's contribution waterfall + lot creation (modules/investing.py). Not
    # per-account (there can be many accounts of the same type) — a deliberate simplification per
    # the spec's own framing, replaced later by §8's derived/optimized allocation.
    if "target_allocations" not in st.session_state:
        st.session_state.target_allocations = {}
    # ---- Target allocation UI redesign (2026-09-06, see NEXT.md item 3) ----
    # `target_allocations` above stays the exact downstream shape every consumer reads
    # (modules/investing.py via ui/projection_tab.py) — these two are the new SOURCE the Portfolio
    # tab derives it from every rerun: one Standard {ticker: weight} list applied to every account
    # type, plus only the account types explicitly overridden here diverging from it.
    if "target_allocation_standard" not in st.session_state:
        st.session_state.target_allocation_standard = {}
    if "target_allocation_overrides" not in st.session_state:
        st.session_state.target_allocation_overrides = {}

    # ---- Portfolio-derived value read by the Projection tab (NPV discount rate) ----
    # Recomputed every rerun by ui/portfolio_tab.py's _render_portfolio_summary (that tab always
    # renders before Projection, regardless of which is visually active — see app.py's tab order),
    # never independently persisted/loaded. Starts at 0.0 (no holdings yet), matching
    # modules.portfolio.summarize_holdings' own empty-portfolio value.
    if "portfolio_blended_real_return" not in st.session_state:
        st.session_state.portfolio_blended_real_return = 0.0
    if "resolved_ticker_prices" not in st.session_state:
        # {ticker: price} — same cross-tab-bridge pattern as portfolio_blended_real_return just
        # above, read by the Projection tab's contribution-waterfall lot creation.
        st.session_state.resolved_ticker_prices = {}
    # ---- Macro tab -> Portfolio tab bridges (2026-09-06 reorg, NEXT.md item 1) ----
    # Published every rerun by ui/macro_tab.py, which always renders before Portfolio (app.py) —
    # same "always fresh, never stale-by-one" pattern as resolved_ticker_prices above. Empty
    # defaults here only matter before Macro has ever rendered once (never happens in practice,
    # same caveat portfolio_universe below already carries).
    if "etf_universe" not in st.session_state:
        st.session_state.etf_universe = {"asset_classes": {}, "etfs": {}}
    if "resolved_prices_full" not in st.session_state:
        st.session_state.resolved_prices_full = {}  # {ticker: {"price": float, "error": str | None}}
    if "portfolio_universe" not in st.session_state:
        # modules.portfolio.build_universe's own shape — cross-tab bridge (MODEL_WIRING.md §4, Step
        # 3, 2026-08-10) for the Projection tab's portfolio roll-forward. Empty asset_classes/etfs
        # is a safe empty-portfolio default (roll_forward_portfolio treats every lot as "unknown
        # ticker" and skips it, same as a lot with no matching price).
        st.session_state.portfolio_universe = {"asset_classes": {}, "etfs": {}}
    if "portfolio_all_positions" not in st.session_state:
        # [{"account", "ticker", "shares", "cost_basis_per_share", "price"}] — same bridge, read
        # alongside st.session_state.accounts to build project_multi_year's initial_lots.
        st.session_state.portfolio_all_positions = []

    # ---- Dissaving / sales (MODEL_WIRING.md §5, Step 4, 2026-08-10) ----
    # sale_method: which tax lots get sold first when raising cash (modules.investing.sell_lots).
    # "hifo" (specific identification, highest-basis-first) is the spec's own default — minimizes
    # realized gain. draw_order deliberately has NO session-state key / UI editor yet — project_
    # multi_year falls back to modules.investing.DEFAULT_DRAW_ORDER whenever None is passed, which
    # is what every call site does today.
    if "sale_method" not in st.session_state:
        st.session_state.sale_method = "hifo"

    # ---- Rebalancing (MODEL_WIRING.md §7, Step 5, 2026-08-10) ----
    # Tax-advantaged accounts only, net-zero-cash/zero-tax-consequence trades -- default True (the
    # UI's own recommended default; project_multi_year itself still defaults `rebalance=False` for
    # backward compatibility). rebalance_band=0.0 means always correct to the exact target.
    if "rebalance" not in st.session_state:
        st.session_state.rebalance = True
    if "rebalance_band" not in st.session_state:
        st.session_state.rebalance_band = 0.0

    # ---- Retirement withdrawal (MODEL_WIRING.md §2.3, Step 6, 2026-08-10) ----
    # "flat_percentage" (the spec's own original default) draws a flat % of portfolio value every
    # year, deliberately never reconciled against that year's actual spending need. Applies from
    # withdrawal_start_date (Demographics tab).
    if "withdrawal_strategy" not in st.session_state:
        st.session_state.withdrawal_strategy = "flat_percentage"
    if "withdrawal_rate" not in st.session_state:
        st.session_state.withdrawal_rate = 0.04

    # ---- Bracket-aware withdrawal split (MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1,
    # Module G2 Step 7, 2026-08-23) ----
    # The ordinary-income bracket ceiling Traditional 401(k)/IRA sales are capped at each retirement
    # year, before Taxable/Roth pick up the rest. 0.22 (the 22% bracket) is the spec's own stated
    # default -- a comfortable middle bracket for most retirees, cheap ordinary income taken now
    # rather than forced into a higher bracket later or left to a Roth balance that would otherwise
    # keep compounding tax-free.
    if "target_bracket_rate" not in st.session_state:
        st.session_state.target_bracket_rate = 0.22

    # ---- Accounts / holdings ----
    if "accounts" not in st.session_state:
        st.session_state.accounts = []  # [{name, type}]
    # Note: there is deliberately no separate st.session_state.positions mirror here (audit
    # finding 12) — each account's own positions_df_<name> (created lazily in ui/portfolio_tab.py)
    # is the only source of truth for its holdings; the flat position list used for calculations
    # is rebuilt fresh from those every render, never stored back into session state.

    if "manual_quotes" not in st.session_state:
        # {ticker: {price}} — manual price fallback when a live Yahoo Finance lookup fails. Expense
        # ratio and dividend rate/type are not "quotes" at all anymore (Module 1 amendment,
        # 2026-08-08) — they're user-entered directly on each ticker's ticker_universe entry.
        st.session_state.manual_quotes = {}

    # ---- Tax (QC tab — see PROJECT_PLAN.md Step 4) ----
    # A single plain input per modules.tax.compute_taxes income source. Not part of any saved
    # state yet (see ui/tax_tab.py) — this tab is a manual single-year QC surface, not persisted
    # user data, so it intentionally resets each session.
    if "tax_filing_status" not in st.session_state:
        st.session_state.tax_filing_status = "single"
    if "tax_year_select" not in st.session_state:
        st.session_state.tax_year_select = max(load_bracket_table().keys())
    if "tax_deferral_priority" not in st.session_state:
        st.session_state.tax_deferral_priority = "w2-first"
    if "tax_age_at_year_end" not in st.session_state:
        # Seeded once from Demographics' birth_date + the default tax year above — the widget is
        # still freely overridable afterward (key-only, no value= — see the note at the top of this
        # function), this just avoids making the user re-enter an age already on file elsewhere.
        st.session_state.tax_age_at_year_end = st.session_state.tax_year_select - st.session_state.birth_date.year
    for _key in (
        "tax_w2_gross",
        "tax_pretax_401k",
        "tax_roth_401k",
        "tax_pretax_health_dental",
        "tax_se_net_profit",
        "tax_se_solo_employee_deferral",
        "tax_se_solo_employer_contribution",
        "tax_retirement_withdrawal",
        "tax_ltcg",
        "tax_qualified_dividends",
        "tax_ordinary_dividends",
        "tax_interest_income",
        "tax_ss_benefit_gross",
        "tax_traditional_ira_contribution",
        "tax_roth_ira_contribution",
    ):
        if _key not in st.session_state:
            st.session_state[_key] = 0.0

    # ---- Income & expense projection (multi-year tab — see PROJECT_PLAN.md) ----
    # Deliberately separate from the Tax tab's own session state (tax_filing_status etc.) — the
    # Tax tab stays a self-contained single-year tool, per explicit user request; this tab has its
    # own independent filing status and income/expense/break inputs, not shared with it.
    if "proj_filing_status" not in st.session_state:
        st.session_state.proj_filing_status = "single"
    for _key in (
        "proj_already_earned_w2",
        "proj_yet_to_earn_w2",
        "proj_w2_start_value",
        "proj_w2_end_value",
        "proj_already_earned_se",
        "proj_yet_to_earn_se",
        "proj_se_start_value",
        "proj_se_end_value",
        "proj_already_incurred_expense",
        "proj_yet_to_incur_expense",
        "proj_expense_start_value",
        "proj_expense_end_value",
    ):
        if _key not in st.session_state:
            st.session_state[_key] = 0.0
    for _key in ("proj_w2_midpoint_years", "proj_se_midpoint_years", "proj_expense_midpoint_years"):
        if _key not in st.session_state:
            st.session_state[_key] = 5.0
    for _key in ("proj_w2_steepness", "proj_se_steepness", "proj_expense_steepness"):
        if _key not in st.session_state:
            st.session_state[_key] = 0.5
    if "income_breaks" not in st.session_state:
        st.session_state.income_breaks = []  # [{"id","label","start_date","end_date","annualized_income_during_break"}]

    # ---- Employer 401(k) match (MODEL_WIRING.md §3.1/§3.2, 2026-08-10) ----
    # Flat plan-design settings, not year-by-year like contribution_by_year below — a real
    # employer's match formula doesn't typically change year to year, unlike planned $ amounts.
    # Both default to 0.0 so a user with no employer match sees the feature as provably inert
    # (see modules.tax.employer_401k_match's own test coverage) rather than an invented number.
    if "employer_match_rate" not in st.session_state:
        st.session_state.employer_match_rate = 0.0
    if "employer_match_cap_pct" not in st.session_state:
        st.session_state.employer_match_cap_pct = 0.0

    # ---- Annual contribution inputs — the per-destination mode system (CONTRIBUTION_TOGGLE_
    # REDESIGN.md, 2026-08-16; replaces the earlier priority-order-waterfall toggle +
    # six-raw-dollar-field manual grid entirely, see NEXT.md) ----
    # Keyed by calendar year rather than stored as a flat list, so a year's entered config survives
    # even if the projected year range temporarily shrinks (e.g. the retirement date moves earlier
    # then later again) — same "don't lose data the user typed" principle as everywhere else in this
    # app. A year missing from this dict is read by `ui/projection_tab.py` as
    # `modules.contributions.RECOMMENDED_CONTRIBUTION_CONFIG` (max out every destination — the new
    # recommended default, replacing the old `use_contribution_waterfall=True` UI default) for
    # DISPLAY and for what `project_multi_year` actually computes; `modules.contributions.
    # DEFAULT_CONTRIBUTION_CONFIG` ($0 everywhere) is a SEPARATE, function-level fallback used only
    # when this whole parameter is omitted entirely (every non-UI caller/test) — the UI never
    # relies on that lower default once a save is open, so a fresh year always shows "maxed," not
    # blank.
    if "contribution_by_year" not in st.session_state:
        st.session_state.contribution_by_year = {}
        # {year: {"contribution_401k_mode", "contribution_401k_custom_amount",
        #         "contribution_401k_custom_type", "maximize_se_employer_401k", "roth_ira_mode",
        #         "roth_ira_custom_amount", "traditional_ira_mode", "traditional_ira_custom_amount"}}
        # — see modules/contributions.py's own module docstring for the full field meanings.

    # ---- Currently-open save file (ui/sidebar.py's one-click "Save" button) ----
    # None until the user either loads an existing save or does a "Save as" — tracks the exact
    # display_name/filename so a quick re-save overwrites that same file (same slug) instead of
    # requiring the name to be retyped exactly, or risking a second, near-duplicate file.
    if "current_save_display_name" not in st.session_state:
        st.session_state.current_save_display_name = None
    if "current_save_filename" not in st.session_state:
        st.session_state.current_save_filename = None

    # ---- Widget-remount version counters (see ui/portfolio_tab.py and ui/sidebar.py for why) ----
    if "form_version" not in st.session_state:
        st.session_state.form_version = 0
    if "all_accounts_expanded" not in st.session_state:
        st.session_state.all_accounts_expanded = True
    if "expander_version" not in st.session_state:
        st.session_state.expander_version = 0
