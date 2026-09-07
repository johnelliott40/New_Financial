"""Save/load/delete named snapshots (sidebar)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from modules import save_state as save_state_io
from modules.demographics import HEALTH_STATUS_OPTIONS, date_at_age
from modules.expense_ratio_overrides import load_overrides as load_legacy_expense_ratios

# Every plain (non-portfolio) Tax-tab input, session key -> save-payload key. Age at year-end and
# tax year select are included too — they're freely overridable per-field widgets (see
# ui/tax_tab.py), same as every other value here, not derived-and-therefore-skippable.
_TAX_FIELD_KEYS = {
    "tax_filing_status": "filing_status",
    "tax_year_select": "year_select",
    "tax_deferral_priority": "deferral_priority",
    "tax_age_at_year_end": "age_at_year_end",
    "tax_w2_gross": "w2_gross",
    "tax_pretax_401k": "pretax_401k",
    "tax_roth_401k": "roth_401k",
    "tax_pretax_health_dental": "pretax_health_dental",
    "tax_se_net_profit": "se_net_profit",
    "tax_se_solo_employee_deferral": "se_solo_employee_deferral",
    "tax_se_solo_employer_contribution": "se_solo_employer_contribution",
    "tax_retirement_withdrawal": "retirement_withdrawal",
    "tax_ltcg": "ltcg",
    "tax_qualified_dividends": "qualified_dividends",
    "tax_ordinary_dividends": "ordinary_dividends",
    "tax_interest_income": "interest_income",
    "tax_ss_benefit_gross": "ss_benefit_gross",
    "tax_traditional_ira_contribution": "traditional_ira_contribution",
    "tax_roth_ira_contribution": "roth_ira_contribution",
}

# Every plain (non-income-breaks) Projection-tab input, same key-mapping shape as above.
# income_breaks is handled separately (its dates need JSON-safe serialization).
_PROJECTION_FIELD_KEYS = {
    "proj_filing_status": "filing_status",
    "proj_already_earned_w2": "already_earned_w2",
    "proj_yet_to_earn_w2": "yet_to_earn_w2",
    "proj_w2_start_value": "w2_start_value",
    "proj_w2_end_value": "w2_end_value",
    "proj_w2_midpoint_years": "w2_midpoint_years",
    "proj_w2_steepness": "w2_steepness",
    "proj_already_earned_se": "already_earned_se",
    "proj_yet_to_earn_se": "yet_to_earn_se",
    "proj_se_start_value": "se_start_value",
    "proj_se_end_value": "se_end_value",
    "proj_se_midpoint_years": "se_midpoint_years",
    "proj_se_steepness": "se_steepness",
    "proj_already_incurred_expense": "already_incurred_expense",
    "proj_yet_to_incur_expense": "yet_to_incur_expense",
    "proj_expense_start_value": "expense_start_value",
    "proj_expense_end_value": "expense_end_value",
    "proj_expense_midpoint_years": "expense_midpoint_years",
    "proj_expense_steepness": "expense_steepness",
    # Employer 401(k) match (MODEL_WIRING.md §3.1/§3.2, 2026-08-10) — flat plan-design settings,
    # not year-by-year (see state.py).
    "employer_match_rate": "employer_match_rate",
    "employer_match_cap_pct": "employer_match_cap_pct",
    # MODEL_WIRING.md §3.3/§5.2 (Step 4, 2026-08-10) — which tax lots get sold first for dissaving.
    "sale_method": "sale_method",
    # MODEL_WIRING.md §7 (Step 5, 2026-08-10) — annual rebalancing of tax-advantaged accounts.
    "rebalance": "rebalance",
    "rebalance_band": "rebalance_band",
    # MODEL_WIRING.md §2.3 (Step 6, 2026-08-10) — retirement withdrawal strategy selection + the
    # flat-percentage rule's own rate. MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 2
    # (2026-08-29) added "target_net_spending" as a second valid value.
    "withdrawal_strategy": "withdrawal_strategy",
    "withdrawal_rate": "withdrawal_rate",
    # MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1 (Module G2 Step 7, 2026-08-23) — the
    # ordinary-income bracket ceiling Traditional sales are capped at during retirement withdrawals.
    "target_bracket_rate": "target_bracket_rate",
}


def _gather_tax() -> dict:
    return {save_key: st.session_state[session_key] for session_key, save_key in _TAX_FIELD_KEYS.items()}


def _gather_projection() -> dict:
    payload = {save_key: st.session_state[session_key] for session_key, save_key in _PROJECTION_FIELD_KEYS.items()}
    payload["income_breaks"] = [
        {**b, "start_date": b["start_date"].isoformat(), "end_date": b["end_date"].isoformat()}
        for b in st.session_state.income_breaks
    ]
    # contribution_by_year is keyed by int year — JSON always stringifies dict keys on write, so
    # _restore_projection converts them back to int on the way in (see below). Entries are always
    # written in the CURRENT (mode-based) shape — the UI never writes the old six-raw-dollar-field
    # shape once loaded, even for a migrated save (see _migrate_contribution_entry below).
    payload["contribution_by_year"] = {str(year): entry for year, entry in st.session_state.contribution_by_year.items()}
    return payload


def _migrate_contribution_entry(entry: dict) -> dict:
    """
    CONTRIBUTION_TOGGLE_REDESIGN.md §7 (2026-08-16) — a `contribution_by_year` entry written before
    this redesign has six independent raw-dollar fields (`w2_401k_contribution`,
    `roth_401k_contribution`, `se_401k_employee_contribution`, `se_401k_employer_contribution`,
    `traditional_ira_contribution`, `roth_ira_contribution`); the current shape is mode-based (see
    `modules/contributions.py`'s own module docstring). Detected by the ABSENCE of
    `"contribution_401k_mode"` — a genuinely new-shape entry always has that key, an old one never
    does — so this is idempotent: re-running it on an already-migrated entry is a no-op (returned
    unchanged).

    Traditional/Roth IRA migrate EXACTLY — both were already single custom dollar amounts, no
    ambiguity. The 401(k)-family migration is a real, DOCUMENTED approximation, not exact
    (flagged, not silently guessed — CLAUDE.md ground rule 7): the new custom mode is ONE combined
    amount + a single pretax-or-Roth type (per the user's own confirmed "mutually exclusive"
    interpretation), so an old entry with BOTH `w2_401k_contribution` AND
    `roth_401k_contribution` nonzero in the same year can't migrate exactly — whichever of the two
    is LARGER wins the type, and the amounts are summed into one combined custom figure (the W-2-
    then-SE-employee-overflow split `resolve_401k_target` applies on top may not exactly reproduce
    the old entry's own w2-vs-se_401k_employee split either, if the two don't already fall along
    that same "W-2 first" ordering — a real, bounded fidelity loss inherent to collapsing five old
    independent dollar fields into two new ones). `se_401k_employer_contribution` (old) has no
    custom-dollar equivalent at all in the new system (§2's own explicit design: an employer either
    maxes its statutory contribution or doesn't) — migrates to `maximize_se_employer_401k=True`
    whenever the old entry had ANY nonzero employer amount, `False` otherwise.
    """
    if "contribution_401k_mode" in entry:
        return entry  # already the new shape -- no-op

    w2_pretax = entry.get("w2_401k_contribution", 0.0) or 0.0
    w2_roth = entry.get("roth_401k_contribution", 0.0) or 0.0
    se_employee = entry.get("se_401k_employee_contribution", 0.0) or 0.0
    se_employer = entry.get("se_401k_employer_contribution", 0.0) or 0.0

    if w2_roth > w2_pretax:
        custom_type = "roth"
        custom_amount = w2_roth + w2_pretax + se_employee
    else:
        custom_type = "pretax"
        custom_amount = w2_pretax + w2_roth + se_employee

    return {
        "contribution_401k_mode": "custom",
        "contribution_401k_custom_amount": custom_amount,
        "contribution_401k_custom_type": custom_type,
        "maximize_se_employer_401k": se_employer > 0,
        "roth_ira_mode": "custom",
        "roth_ira_custom_amount": entry.get("roth_ira_contribution", 0.0) or 0.0,
        "traditional_ira_mode": "custom",
        "traditional_ira_custom_amount": entry.get("traditional_ira_contribution", 0.0) or 0.0,
    }


def _restore_tax(data: dict) -> None:
    tax = data.get("tax", {})
    for session_key, save_key in _TAX_FIELD_KEYS.items():
        if save_key in tax:
            st.session_state[session_key] = tax[save_key]


def _restore_projection(data: dict) -> None:
    projection = data.get("projection", {})
    for session_key, save_key in _PROJECTION_FIELD_KEYS.items():
        if save_key in projection:
            st.session_state[session_key] = projection[save_key]
    if "income_breaks" in projection:
        st.session_state.income_breaks = [
            {**b, "start_date": date.fromisoformat(b["start_date"]), "end_date": date.fromisoformat(b["end_date"])}
            for b in projection["income_breaks"]
        ]
    if "contribution_by_year" in projection:
        st.session_state.contribution_by_year = {
            int(year): _migrate_contribution_entry(entry)
            for year, entry in projection["contribution_by_year"].items()
        }

# One-time backward-compat shim: before this session's rework, ticker -> asset-class came from
# the now-retired data/etf_universe.json, shared by everyone, rather than from each user's own
# ticker_universe. A save written before this change has no ticker_universe key at all. Without
# this table, loading such a save would show correct accounts/holdings but blow up on any
# calculation, since none of its tickers exist in the (now-empty) universe. Values copied from
# that file's last content before deletion. CASH_USD/CASH_GBP no longer resolve via a live lookup
# under the new design (that per-ticker price_mode special-casing was dropped per
# PROJECT_PLAN.md's Step 1 "drop non-USD support" resolution for audit finding 14) — they'll
# prompt for a manual price on load instead of failing outright. GBP_INT itself was dropped as an
# asset class entirely, so CASH_GBP folds into USD_CASH as the closest remaining bucket.
_LEGACY_TICKER_ASSET_CLASSES = {
    "CASH_USD": "USD_CASH",
    "CASH_GBP": "USD_CASH",
    "VXUS": "EXUS",
    "AVNM": "EXUSV",
    "VT": "TOT",
    "AVGE": "TOTV",
    "VTI": "US",
    "AVUS": "USV",
    "VGLT": "LTT",
    "VOO": "US",
    "VEA": "DM",
    "VWO": "EM",
    "AVUV": "USSCV",
    "AVDV": "DMSCV",
    "DFSTX": "USSCV",
    "FXAIX": "US",
    "FSKAX": "US",
    "FPADX": "EM",
    "FSPSX": "DM",
    "SCHE": "DM",
    "VGIT": "MTT",
    "VTTSX": "US",
    "SGOV": "STT",
}


def _gather_positions_by_account() -> dict[str, list[dict]]:
    """Reads the current (input-only) holdings out of each account's session-state DataFrame, for saving."""
    result: dict[str, list[dict]] = {}
    for account in st.session_state.accounts:
        name = account["name"]
        df = st.session_state.get(f"positions_df_{name}")
        if df is None or "ticker" not in df.columns:
            result[name] = []
            continue
        cleaned = df.dropna(subset=["ticker"])
        result[name] = [
            {
                "ticker": row["ticker"],
                "shares": float(row["shares"]) if pd.notna(row["shares"]) else 0.0,
                "cost_basis_per_share": float(row["cost_basis_per_share"]) if pd.notna(row["cost_basis_per_share"]) else 0.0,
            }
            for row in cleaned.to_dict("records")
        ]
    return result


def _clear_holdings_widget_state() -> None:
    """Wipes per-account/per-ticker widget state before a Load, so nothing from before bleeds through."""
    prefixes = (
        "positions_df_",
        "manual_price_",
        "ticker_er_",
        "ticker_dr_",
        "ticker_dt_",
        "ticker_ac_",
        "ticker_it_",
        "new_ticker_",
        "new_shares_",
        "new_cost_basis_",
        "remove_ticker_select_",
        "account_expander_",
        "account_name_",
        "account_type_",
        "class_return_",
        # 2026-09-06, NEXT.md item A — the target-allocation add-by-dropdown redesign's own
        # per-ticker weight widgets (both the Standard list and every Alternative override list
        # share this one prefix, ticker-suffixed).
        "target_allocation_weight_",
    )
    for key in list(st.session_state.keys()):
        if key.startswith(prefixes):
            del st.session_state[key]


def _migrate_ticker_universe(data: dict) -> dict:
    """
    Returns the save's own ticker_universe, upgraded to the current per-ticker shape
    ({ticker: {"asset_class", "expense_ratio", "dividend_rate", "income_type"}}), reconstructing
    it from scratch for pre-Module-2 saves that have no ticker_universe key at all.

    Three save generations need upgrading, all handled here:
    - Pre-Module-2 saves: no ticker_universe key at all — rebuilt from the hardcoded legacy table.
    - Saves from the brief window between Module 2 and the 2026-08-08 amendment that removed the
      Yahoo Finance expense-ratio lookup: ticker_universe values were a bare asset-class code
      string, no expense ratio/dividend info. Expense ratio for these is recovered from the save's
      own manual_quotes (its old "live value or override" slot for that ticker), then the legacy
      data/expense_ratio_overrides.json file, in that order — never defaulted to 0.0 while a real
      number is available. Dividend rate/type have no historical source, so they default to
      0.0 / "qualified" and need a one-time manual fix in the Ticker details expander.
    - Saves from between the 2026-08-08 amendment and the 2026-08-10 `income_type` rename
      (MODEL_WIRING.md §4.2): the per-ticker dict is already in the current shape except the key
      is the old `dividend_type`, not `income_type` — renamed in place, value unchanged (still
      "qualified"/"ordinary", the only two values that existed before this rename).
    """
    manual_quotes = data.get("manual_quotes", {})
    legacy_expense_ratios = load_legacy_expense_ratios()

    def _expense_ratio_for(ticker: str) -> float:
        from_manual_quotes = manual_quotes.get(ticker, {}).get("expense_ratio")
        if from_manual_quotes is not None:
            return from_manual_quotes
        return legacy_expense_ratios.get(ticker, 0.0)

    raw = data.get("ticker_universe")
    if raw:
        upgraded = {}
        for ticker, entry in raw.items():
            if isinstance(entry, str):  # old shape: bare asset-class code
                upgraded[ticker] = {
                    "asset_class": entry,
                    "expense_ratio": _expense_ratio_for(ticker),
                    "dividend_rate": 0.0,
                    "income_type": "qualified",
                }
            elif "income_type" not in entry and "dividend_type" in entry:
                upgraded[ticker] = {**entry, "income_type": entry["dividend_type"]}
                del upgraded[ticker]["dividend_type"]
            else:
                upgraded[ticker] = entry
        return upgraded

    used_tickers = {p["ticker"] for rows in data.get("positions", {}).values() for p in rows}
    return {
        t: {
            "asset_class": _LEGACY_TICKER_ASSET_CLASSES[t],
            "expense_ratio": _expense_ratio_for(t),
            "dividend_rate": 0.0,
            "income_type": "qualified",
        }
        for t in used_tickers
        if t in _LEGACY_TICKER_ASSET_CLASSES
    }


def _save_as(display_name: str) -> str:
    """
    Gathers every tab's current input — not just Portfolio's — and writes it under `display_name`,
    overwriting any existing save with the same slug (see modules.save_state.save_state). Shared by
    both the quick "Save" button (reuses the currently-open file's own display_name) and "Save as"
    (a name the user just typed) — same gather logic either way, only the name differs.
    """
    filename = save_state_io.save_state(
        display_name=display_name,
        macro={
            "inflation_rate": st.session_state.inflation_rate,
        },
        demographics={
            "birth_date": st.session_state.birth_date.isoformat(),
            "retirement_date": st.session_state.retirement_date.isoformat(),
            "health_status": st.session_state.health_status,
            # savings_stop_date/withdrawal_start_date/ss_claim_date/planning_horizon_age: the four
            # MODEL_WIRING.md §2 phase-date fields (2026-08-10) — saving_stop_age (an int age) is
            # retired in favor of savings_stop_date (a date); see _migrate_demographics below for
            # the one-time upgrade path for saves written before this change.
            "savings_stop_date": st.session_state.savings_stop_date.isoformat(),
            "withdrawal_start_date": st.session_state.withdrawal_start_date.isoformat(),
            "ss_claim_date": st.session_state.ss_claim_date.isoformat(),
            "planning_horizon_age": st.session_state.planning_horizon_age,
            # Module G1 (2026-08-30) — sex + the four editable health-age-rating years.
            "sex_for_mortality": st.session_state.sex_for_mortality,
            "health_age_adjustment_years": {
                tier: st.session_state[f"health_adjustment_{tier.lower()}"] for tier in HEALTH_STATUS_OPTIONS
            },
        },
        accounts=st.session_state.accounts,
        positions=_gather_positions_by_account(),
        manual_quotes=st.session_state.manual_quotes,
        ticker_universe=st.session_state.ticker_universe,
        asset_class_returns=st.session_state.asset_class_returns,
        tax=_gather_tax(),
        projection=_gather_projection(),
        target_allocations=st.session_state.target_allocations,
        target_allocation_standard=st.session_state.target_allocation_standard,
        target_allocation_overrides=st.session_state.target_allocation_overrides,
        custom_asset_classes=st.session_state.custom_asset_classes,
        removed_canonical_asset_classes=sorted(st.session_state.removed_canonical_asset_classes),
        social_security={
            # JSON object keys are always strings -- converted back to int on load below, same
            # "stringified dict keys round-trip through int() on load" pattern used nowhere else
            # yet in this file only because no other personal dict here happens to be int-keyed.
            "historical_ss_earnings": {
                str(year): dollars for year, dollars in st.session_state.historical_ss_earnings.items()
            },
        },
    )
    st.session_state.current_save_display_name = display_name
    st.session_state.current_save_filename = filename
    return filename


def render() -> None:
    with st.sidebar:
        st.header("Saved states")

        current_name = st.session_state.current_save_display_name
        if current_name:
            st.caption(f"Currently open: **{current_name}**")
            if st.button(
                "Save",
                icon=":material/save:",
                width="stretch",
                help="Overwrite the currently open file with every tab's current inputs.",
            ):
                _save_as(current_name)
                st.success(f"Saved '{current_name}'")
        else:
            st.caption("No file currently open — use “Save as” below to create one.")

        with st.popover("Save as…", icon=":material/save_as:", width="stretch"):
            st.text_input("Save name", key="save_name_input")
            if st.button("Save as new file", icon=":material/save_as:", width="stretch"):
                name = st.session_state.save_name_input.strip()
                if not name:
                    st.error("Enter a name before saving.")
                else:
                    filename = _save_as(name)
                    # This sets current_save_display_name, which the "Currently open" caption
                    # above already rendered (stale) earlier in this same script pass -- force a
                    # fresh rerun so it reflects the new file immediately, same as Load below does.
                    st.success(f"Saved as {filename}")
                    st.rerun()

        st.divider()

        saves = save_state_io.list_saves()
        if not saves:
            st.caption("No saved states yet.")
        else:
            save_options = {f"{s['display_name']} ({s['saved_at']})": s["filename"] for s in saves}
            selected_label = st.selectbox("Load a saved state", options=list(save_options.keys()), key="load_select")
            selected_filename = save_options[selected_label]

            with st.container(horizontal=True):
                load_clicked = st.button("Load", icon=":material/folder_open:")
                delete_clicked = st.button("Delete", icon=":material/delete:")

            if load_clicked:
                data = save_state_io.load_state(selected_filename)
                _clear_holdings_widget_state()

                macro = data["macro"]
                st.session_state.inflation_rate = macro["inflation_rate"]
                # pretax_tax_rate/capital_gains_rate removed 2026-08-30 — an older save's macro
                # dict may still carry them; simply ignored on load, same as any other retired key.

                demographics = data.get("demographics", {})
                if "birth_date" in demographics:
                    st.session_state.birth_date = date.fromisoformat(demographics["birth_date"])
                if "retirement_date" in demographics:
                    st.session_state.retirement_date = date.fromisoformat(demographics["retirement_date"])
                elif "retirement_date" in macro:  # pre-Module-2 save: retirement_date lived under "macro"
                    st.session_state.retirement_date = date.fromisoformat(macro["retirement_date"])
                st.session_state.health_status = demographics.get("health_status", st.session_state.health_status)

                # MODEL_WIRING.md §2 phase dates (2026-08-10). savings_stop_date replaces the old
                # saving_stop_age (an int age, display-only — nothing ever consumed it). A save
                # written before this change has no savings_stop_date at all; migrate it from the
                # legacy age field (relative to the just-restored birth_date) rather than silently
                # defaulting to "today," so a real prior setting isn't lost. withdrawal_start_date
                # and ss_claim_date have no legacy source at all — default to retirement_date /
                # age 67 respectively, same as a fresh session (state.py).
                if "savings_stop_date" in demographics:
                    st.session_state.savings_stop_date = date.fromisoformat(demographics["savings_stop_date"])
                elif "saving_stop_age" in demographics:
                    st.session_state.savings_stop_date = date_at_age(
                        st.session_state.birth_date, demographics["saving_stop_age"]
                    )
                else:
                    st.session_state.savings_stop_date = st.session_state.retirement_date
                if "withdrawal_start_date" in demographics:
                    st.session_state.withdrawal_start_date = date.fromisoformat(demographics["withdrawal_start_date"])
                else:
                    st.session_state.withdrawal_start_date = st.session_state.retirement_date
                if "ss_claim_date" in demographics:
                    st.session_state.ss_claim_date = date.fromisoformat(demographics["ss_claim_date"])
                else:
                    st.session_state.ss_claim_date = date_at_age(st.session_state.birth_date, 67)
                st.session_state.planning_horizon_age = demographics.get(
                    "planning_horizon_age", st.session_state.planning_horizon_age
                )

                # Module G1 (2026-08-30). A save written before this feature has neither key —
                # .get() with the current session-state value as default leaves state.py's own
                # fresh-session default in place rather than crashing or guessing.
                st.session_state.sex_for_mortality = demographics.get(
                    "sex_for_mortality", st.session_state.sex_for_mortality
                )
                saved_adjustment_years = demographics.get("health_age_adjustment_years", {})
                for tier in HEALTH_STATUS_OPTIONS:
                    key = f"health_adjustment_{tier.lower()}"
                    st.session_state[key] = saved_adjustment_years.get(tier, st.session_state[key])

                st.session_state.accounts = data["accounts"]
                # accounts_df (a separate data_editor-backed DataFrame mirror) was retired along
                # with the dedicated "Accounts" sub-section (2026-09-06 reorg, NEXT.md item 2) --
                # st.session_state.accounts above is the only source of truth now.
                for account in data["accounts"]:
                    rows = data["positions"].get(account["name"], [])
                    st.session_state[f"positions_df_{account['name']}"] = pd.DataFrame(
                        rows, columns=["ticker", "shares", "cost_basis_per_share"]
                    )

                st.session_state.manual_quotes = data["manual_quotes"]
                st.session_state.ticker_universe = _migrate_ticker_universe(data)
                if data.get("asset_class_returns"):
                    st.session_state.asset_class_returns.update(data["asset_class_returns"])
                _restore_tax(data)
                _restore_projection(data)
                # target_allocation_standard/target_allocation_overrides (2026-09-06 redesign,
                # NEXT.md item 3) -- a save written under the new UI has both keys directly. A save
                # written before it (MODEL_WIRING.md §3.1, 2026-08-10) has only the flat
                # target_allocations dict -- migrated here into target_allocation_overrides for
                # every account type it already has a nonzero weight dict for, Standard left empty,
                # reproducing the EXACT SAME effective target_allocations immediately after the
                # upgrade rather than losing the standard/override distinction on reload. Either
                # way, target_allocations itself (the shape every consumer reads) is left for
                # ui/portfolio_tab.py's own render() to re-derive fresh this same rerun -- not
                # recomputed here, since Portfolio always renders before anything reads it.
                if "target_allocation_standard" in data or "target_allocation_overrides" in data:
                    st.session_state.target_allocation_standard = data.get("target_allocation_standard", {})
                    st.session_state.target_allocation_overrides = data.get("target_allocation_overrides", {})
                else:
                    legacy_target_allocations = data.get("target_allocations", {})
                    st.session_state.target_allocation_standard = {}
                    st.session_state.target_allocation_overrides = {
                        account_type: weights for account_type, weights in legacy_target_allocations.items() if weights
                    }
                # custom_asset_classes (2026-08-14, user request) -- same absent-in-older-saves
                # pattern; a ticker referencing a custom class is restored by _migrate_ticker_universe
                # above regardless, since ticker_universe entries only ever store a bare code string.
                st.session_state.custom_asset_classes = data.get("custom_asset_classes", {})
                # removed_canonical_asset_classes (2026-08-14, user request) -- stored as a plain
                # list (JSON has no set type); same absent-in-older-saves pattern as above.
                st.session_state.removed_canonical_asset_classes = set(data.get("removed_canonical_asset_classes", []))
                # Module F (2026-08-31) -- same absent-in-older-saves pattern; year keys stored as
                # JSON strings, converted back to int here (the shape ui/social_security_tab.py's
                # own st.session_state.historical_ss_earnings actually uses).
                social_security = data.get("social_security", {})
                st.session_state.historical_ss_earnings = {
                    int(year): dollars
                    for year, dollars in social_security.get("historical_ss_earnings", {}).items()
                }
                st.session_state.form_version += 1  # force the accounts data_editor to remount with the loaded data

                st.session_state.current_save_display_name = data["display_name"]
                st.session_state.current_save_filename = selected_filename

                st.success(f"Loaded '{data['display_name']}'")
                st.rerun()

            if delete_clicked:
                st.session_state.pending_delete = selected_filename

            if st.session_state.get("pending_delete") == selected_filename:
                st.warning(f"Delete '{selected_label}'? This can't be undone.")
                with st.container(horizontal=True):
                    if st.button("Confirm delete"):
                        save_state_io.delete_state(selected_filename)
                        st.session_state.pending_delete = None
                        if st.session_state.current_save_filename == selected_filename:
                            # The file backing the quick "Save" button no longer exists.
                            st.session_state.current_save_display_name = None
                            st.session_state.current_save_filename = None
                        st.success("Deleted.")
                        st.rerun()
                    if st.button("Cancel"):
                        st.session_state.pending_delete = None
                        st.rerun()
