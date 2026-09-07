"""
Portfolio tab (Module 1): accounts, holdings, target allocation, summary.

2026-09-06 reorg (NEXT.md item 1): macro inputs / asset classes / ETF universe / ticker details
moved out to ui/macro_tab.py (rendered before this tab — see that module's own docstring). This
tab now reads that tab's published `st.session_state.etf_universe` / `resolved_ticker_prices` /
`resolved_prices_full` instead of building them locally.

UI only; calculation logic lives in modules/portfolio.py, live market data lookups in
modules/market_data.py via ui/quotes.py. See PROJECT_PLAN.md Step 1.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from modules.portfolio import (
    ACCOUNT_TYPES,
    etf_lookup,
    expense_adjusted_return,
    group_positions_by_account,
    holding_gross_value,
    portfolio_summary,
    real_return,
    target_allocation_blended_return,
    target_allocation_blended_weights,
)


def _render_account_holdings(
    universe: dict,
    resolved_prices: dict[str, dict],
    tickers: list[str],
    inflation_rate: float,
) -> list[dict]:
    """
    Accounts + their holdings, together (2026-09-06 reorg, NEXT.md item 2 — folds the old separate
    "Accounts" section's add/rename/remove controls directly into this one, instead of a shared
    grid elsewhere). `st.session_state.accounts` stays the exact same `[{name, type}]` shape/
    meaning — only WHERE it's added/renamed/removed moves, next to the account's own holdings.
    """
    st.header("Account holdings")
    st.caption(
        "Add an account, then its holdings: the ETFs you hold, shares, and cost basis per share. "
        "Ticker options come from the Macro tab's ETF universe — price is looked up there live; "
        "asset class, expense ratio, and dividend info are whatever you entered there."
    )

    all_positions: list[dict] = []

    with st.container(horizontal=True):
        if st.button("Expand all", icon=":material/unfold_more:"):
            st.session_state.all_accounts_expanded = True
            st.session_state.expander_version += 1
        if st.button("Collapse all", icon=":material/unfold_less:"):
            st.session_state.all_accounts_expanded = False
            st.session_state.expander_version += 1

    # New accounts are added through this form (one atomic submission), never through a grid's own
    # "Add row" button — editing a cell in a row a data_editor itself just added is a known
    # reliability gap (a first edit can silently fail to stick); adding a fully-formed row up front
    # sidesteps it. `st.session_state.accounts` is mutated in place (`.append`), so the loop below
    # (which reads it fresh, not a stale copy) already reflects a just-added account this same
    # rerun — no `st.rerun()` needed.
    with st.form("add_account_form", clear_on_submit=True):
        fc1, fc2, fc3 = st.columns([2, 2, 1])
        new_account_name = fc1.text_input("Account name")
        new_account_type = fc2.selectbox("Account type", options=ACCOUNT_TYPES)
        add_account = fc3.form_submit_button("Add account", icon=":material/add:", width="stretch")
        if add_account:
            stripped_name = new_account_name.strip()
            if not stripped_name:
                st.error("Enter an account name.")
            elif any(a["name"] == stripped_name for a in st.session_state.accounts):
                st.error(f"An account named '{stripped_name}' already exists.")
            else:
                st.session_state.accounts.append({"name": stripped_name, "type": new_account_type})

    accounts = st.session_state.accounts
    if not accounts:
        st.info("Add an account above to get started.")
        return all_positions
    if not tickers:
        st.info("Add at least one ticker in the Macro tab's ETF universe section first.")
        return all_positions

    for account in accounts:
        name = account["name"]
        account_type = account["type"]
        state_key = f"positions_df_{name}"

        with st.expander(
            f"{name} — {account_type}",
            expanded=st.session_state.all_accounts_expanded,
            key=f"account_expander_{name}_v{st.session_state.expander_version}",
        ):
            # Rename / retype this account in place (2026-09-06 reorg — was a separate accounts_df
            # grid elsewhere; same two fields, same underlying st.session_state.accounts update, no
            # new migration logic added for a renamed account's own positions_df_{name}/expander
            # keys — matches the exact same behavior the old grid had, not a fix, per the reorg's
            # own "cosmetic only" constraint).
            rc1, rc2, rc3 = st.columns([2, 2, 1])
            name_key = f"account_name_{name}"
            if name_key not in st.session_state:
                st.session_state[name_key] = name
            rc1.text_input("Account name", key=name_key, label_visibility="collapsed")
            type_key = f"account_type_{name}"
            if type_key not in st.session_state:
                st.session_state[type_key] = account_type
            rc2.selectbox("Account type", options=ACCOUNT_TYPES, key=type_key, label_visibility="collapsed")
            account["type"] = st.session_state[type_key]
            renamed = st.session_state[name_key].strip()
            if renamed and renamed != account["name"]:
                account["name"] = renamed

            with rc3.popover("Remove", icon=":material/delete:", width="stretch"):
                st.caption(f"Remove **{name}** and all its holdings? This can't be undone.")
                if st.button("Confirm remove", key=f"remove_account_btn_{name}", width="stretch"):
                    st.session_state.accounts = [a for a in st.session_state.accounts if a["name"] != name]
                    st.session_state.pop(state_key, None)
                    st.session_state.pop(f"account_expander_{name}_v{st.session_state.expander_version}", None)
                    st.session_state.pop(name_key, None)
                    st.session_state.pop(type_key, None)
                    st.rerun()

            if state_key not in st.session_state:
                st.session_state[state_key] = pd.DataFrame(columns=["ticker", "shares", "cost_basis_per_share"])

            # New holdings are added through this form (one atomic submission per row), never
            # through a grid's own "Add row" button — same reasoning as the account form above.
            with st.form(f"add_holding_form_{name}", clear_on_submit=True):
                hc1, hc2, hc3, hc4 = st.columns([2, 1, 1, 1])
                new_ticker = hc1.selectbox("Ticker", options=tickers, key=f"new_ticker_{name}")
                new_shares = hc2.number_input("Shares", min_value=0.0, step=1.0, format="%.4f", key=f"new_shares_{name}")
                new_cost_basis = hc3.number_input(
                    "Cost basis ($/sh)", min_value=0.0, step=0.01, format="%.2f", key=f"new_cost_basis_{name}"
                )
                add_holding = hc4.form_submit_button("Add", icon=":material/add:", width="stretch")
                if add_holding:
                    new_row = pd.DataFrame(
                        [{"ticker": new_ticker, "shares": new_shares, "cost_basis_per_share": new_cost_basis}]
                    )
                    st.session_state[state_key] = pd.concat([st.session_state[state_key], new_row], ignore_index=True)

            # Editing existing rows happens in this grid; adding a new row does not (that's the
            # form above) — data_editor's own "Add row" is a known reliability gap (a cell in a
            # row the grid itself just added can silently fail to take the first edit), so new
            # rows only ever arrive pre-filled via the form. num_rows="fixed" keeps the grid from
            # offering its own Add/Delete controls at all, which also sidesteps that gap entirely.
            if len(st.session_state[state_key]):
                edited_holdings = st.data_editor(
                    st.session_state[state_key],
                    column_config={
                        "ticker": st.column_config.SelectboxColumn("Ticker", options=tickers, required=True),
                        "shares": st.column_config.NumberColumn(
                            "Shares", min_value=0.0, step=0.0001, format="%.4f", required=True
                        ),
                        "cost_basis_per_share": st.column_config.NumberColumn(
                            "Cost basis ($/sh)", min_value=0.0, step=0.01, format="$%.2f", required=True
                        ),
                    },
                    num_rows="fixed",
                    width="stretch",
                    key=f"holdings_editor_{name}_v{st.session_state.form_version}",
                )
                st.session_state[state_key] = edited_holdings

            cleaned = st.session_state[state_key]

            if len(cleaned):
                with st.popover("Remove a holding", icon=":material/delete:"):
                    remove_ticker = st.selectbox(
                        "Holding to remove", options=cleaned["ticker"].tolist(), key=f"remove_ticker_select_{name}"
                    )
                    if st.button("Remove", key=f"remove_ticker_btn_{name}", width="stretch"):
                        st.session_state[state_key] = st.session_state[state_key][
                            st.session_state[state_key]["ticker"] != remove_ticker
                        ].reset_index(drop=True)
                        st.rerun()

            display_rows = []
            for row in cleaned.to_dict("records"):
                ticker = row["ticker"]
                if ticker not in resolved_prices:
                    st.warning(f"**{ticker}** is no longer in the ETF universe — remove and re-add this holding.")
                    continue

                price = resolved_prices[ticker]["price"]

                info = etf_lookup(ticker, universe)
                nominal = info["nominal_return"]
                expense_ratio = info["expense_ratio"]
                real = real_return(expense_adjusted_return(nominal, expense_ratio), inflation_rate)
                gross_value = holding_gross_value(row["shares"], price)

                display_rows.append(
                    {
                        "Ticker": ticker,
                        "Asset class": info["asset_class_label"],
                        "Shares": row["shares"],
                        "Cost basis ($/sh)": row["cost_basis_per_share"],
                        "Price ($)": price,
                        "Expense ratio": expense_ratio,
                        "Dividend rate": info["dividend_rate"],
                        "Income type": info["income_type"].capitalize(),
                        "Nominal return": nominal,
                        "Real return": real,
                        "Gross value ($)": gross_value,
                    }
                )
                all_positions.append(
                    {
                        "account": name,
                        "ticker": ticker,
                        "shares": row["shares"],
                        "cost_basis_per_share": row["cost_basis_per_share"],
                        "price": price,
                    }
                )

            if display_rows:
                st.dataframe(
                    pd.DataFrame(display_rows).style.format(
                        {
                            "Shares": "{:.4f}",
                            "Cost basis ($/sh)": "${:.2f}",
                            "Price ($)": "${:.2f}",
                            "Expense ratio": "{:.2%}",
                            "Dividend rate": "{:.2%}",
                            "Nominal return": "{:.2%}",
                            "Real return": "{:.2%}",
                            "Gross value ($)": "${:,.2f}",
                        }
                    ),
                    width="stretch",
                )
            else:
                st.caption("No holdings in this account yet — add one above.")

    return all_positions


def _render_portfolio_summary(
    accounts: list[dict],
    all_positions: list[dict],
    universe: dict,
    inflation_rate: float,
) -> None:
    st.header("Portfolio summary")

    if not all_positions:
        st.info("Add holdings above to see portfolio totals.")
        # No holdings -> no meaningful blended return; matches summarize_holdings' own 0.0 for an
        # empty portfolio. The Projection tab's NPV calculation reads this every rerun (portfolio_tab
        # always renders before it, regardless of which tab is visually active — see ui/sidebar.py's
        # tax/projection gather functions for the same "runs every rerun" reasoning applied there).
        st.session_state.portfolio_blended_real_return = 0.0
        return

    accounts_for_calc = group_positions_by_account(accounts, all_positions)
    summary = portfolio_summary(accounts_for_calc, universe, inflation_rate)
    overall = summary["overall"]
    st.session_state.portfolio_blended_real_return = overall["blended_real_return"]

    account_types = {a["name"]: a["type"] for a in accounts}
    value_by_account_type: dict[str, float] = {}
    for name, s in summary["by_account"].items():
        acct_type = account_types.get(name, "")
        value_by_account_type[acct_type] = value_by_account_type.get(acct_type, 0.0) + s["total_gross_value"]
    target_summary = target_allocation_blended_return(
        st.session_state.target_allocations, value_by_account_type, universe, inflation_rate
    )

    c1, c2 = st.columns(2)
    c1.metric("Gross portfolio value", f"${overall['total_gross_value']:,.0f}")
    c2.metric("Blended expense ratio", f"{overall['blended_expense_ratio']:.2%}")

    c4, c5, c6 = st.columns(3)
    c4.metric(
        "Current Portfolio Blended Nominal Return",
        f"{overall['blended_nominal_return']:.2%}",
        help="The raw asset-class return assumption, gross of fees — a market benchmark figure.",
    )
    c5.metric(
        "Current Portfolio Blended Real Return",
        f"{overall['blended_real_return']:.2%}",
        help="Net of both the expense ratio (geometric: (1+nominal)/(1+ER) - 1) and inflation "
        "(Fisher equation), chained — what you actually keep, in today's dollars. A snapshot of "
        "TODAY's actual holdings — if you rebalance toward a different target allocation (right), "
        "this figure won't reflect that until you actually hold it.",
    )
    if target_summary["covered_value"] > 0:
        coverage_note = (
            ""
            if target_summary["covered_value"] >= target_summary["total_value"] - 1e-6
            else f" (covers ${target_summary['covered_value']:,.0f} of "
            f"${target_summary['total_value']:,.0f} — some account types have no target "
            "allocation configured yet)"
        )
        c6.metric(
            "Target Portfolio Blended Real Return",
            f"{target_summary['blended_real_return']:.2%}",
            help="2026-08-15, user request — 'if today's dollars, split by account type exactly as "
            "they are now, were each held at that account type's OWN target allocation (below) "
            "instead of whatever's actually held, what would the blended return be.' This is the "
            "figure that actually governs a rebalanced portfolio's long-run trajectory (see "
            "'Rebalance tax-advantaged accounts annually' on the Projection tab) — it can differ "
            f"substantially from 'Current Portfolio Blended Real Return' to its left{coverage_note}.",
        )
    else:
        c6.metric(
            "Target Portfolio Blended Real Return",
            "—",
            help="No account type both holds money and has a target allocation configured yet — "
            "see 'Target allocation by account type' below.",
        )

    st.subheader("By account")
    account_rows = [
        {
            "Account": name,
            "Type": account_types.get(name, ""),
            "Gross value": s["total_gross_value"],
            "Nominal return": s["blended_nominal_return"],
            "Real return": s["blended_real_return"],
            "Expense ratio": s["blended_expense_ratio"],
        }
        for name, s in summary["by_account"].items()
    ]
    account_df = pd.DataFrame(account_rows).sort_values("Gross value", ascending=False)
    st.dataframe(
        account_df.style.format(
            {
                "Gross value": "${:,.0f}",
                "Nominal return": "{:.2%}",
                "Real return": "{:.2%}",
                "Expense ratio": "{:.2%}",
            }
        ),
        width="stretch",
    )

    st.subheader("Value by asset class")
    if not overall["value_by_asset_class"]:
        st.caption("No priced holdings yet.")
    else:
        total_value = overall["total_gross_value"]
        asset_class_df = pd.DataFrame(
            [
                {
                    "Asset class": universe["asset_classes"][code]["label"],
                    "Value": value,
                    "% of portfolio": value / total_value if total_value else 0.0,
                }
                for code, value in overall["value_by_asset_class"].items()
            ]
        ).sort_values("Value", ascending=False)
        st.dataframe(
            asset_class_df.style.format({"Value": "${:,.0f}", "% of portfolio": "{:.1%}"}),
            width="stretch",
            hide_index=True,
        )

    # 2026-08-30, user request: "at the bottom of the Target allocation by account type expander,
    # add a report of the blended target allocation across all accounts." Placed HERE instead —
    # right after "Value by asset class" above, its natural forward-looking counterpart — rather
    # than literally inside that expander: that expander renders BEFORE this function in render()'s
    # own order, before `all_positions`/`value_by_account_type` exist for THIS rerun, so computing
    # it there would show last rerun's (or, immediately after a Load, the PRE-load) portfolio's
    # blend — actively misleading, not just stale-by-one-render like this file's other deliberate
    # cross-TAB bridges. This function already has a freshly computed `value_by_account_type` and
    # `universe` in scope, so the same "if today's dollars, split by account TYPE exactly as they
    # are now, were each held at that account type's OWN target allocation instead" question
    # `target_summary` above already answers for RETURN is answered here for asset-class WEIGHT
    # instead (e.g. "62% US stock / 24% international / 14% bonds").
    st.subheader("Target blended allocation by asset class")
    weights_blend = target_allocation_blended_weights(st.session_state.target_allocations, value_by_account_type, universe)
    if not weights_blend["weights_by_asset_class"]:
        st.caption(
            "No account type both holds money and has a target allocation configured yet — see "
            "'Target allocation by account type' below."
        )
    else:
        coverage_note = (
            ""
            if weights_blend["covered_value"] >= weights_blend["total_value"] - 1e-6
            else f" — covers ${weights_blend['covered_value']:,.0f} of ${weights_blend['total_value']:,.0f} "
            "(some account types have no target allocation configured yet)"
        )
        st.caption(f"Weighted by each account type's current holdings value{coverage_note}.")
        weights_df = pd.DataFrame(
            [
                {"Asset class": universe["asset_classes"].get(code, {}).get("label", code), "Weight": weight}
                for code, weight in weights_blend["weights_by_asset_class"].items()
            ]
        ).sort_values("Weight", ascending=False)
        st.dataframe(
            weights_df.style.format({"Weight": "{:.1%}"}),
            width="stretch",
            hide_index=True,
        )


def _render_weight_editor(tickers: list[str], weights: dict[str, float], widget_key: str) -> dict[str, float]:
    """
    Shared ticker/weight list for both the Standard allocation and each Alternative override below.

    **2026-09-06 redesign (NEXT.md item A)**: replaces the old grid prepopulated with EVERY ticker
    in the universe (weight editable, `0` meaning "not included") with the same "pick from a
    dropdown, enter a value, Add" pattern already used everywhere else on this tab (the ETF
    universe's own add-ticker form, the add-account form, the add-holding form) — same component,
    reused, not a new design. No change to the underlying `{ticker: weight}` shape returned — only
    the ADD interaction changes; each already-added ticker's weight stays editable in place, and a
    dedicated "Remove" button (not a weight of `0`) is what actually takes it out of the list.
    """
    current_weights = dict(weights)

    tickers_not_yet_added = [t for t in tickers if t not in current_weights]
    if tickers_not_yet_added:
        with st.form(f"{widget_key}_add_form", clear_on_submit=True):
            ac1, ac2, ac3 = st.columns([2, 1, 1])
            new_ticker = ac1.selectbox("Ticker", options=tickers_not_yet_added, key=f"{widget_key}_add_ticker")
            new_weight = ac2.number_input(
                "Weight", min_value=0.0, max_value=1.0, step=0.05, format="%.2f", key=f"{widget_key}_add_weight"
            )
            add = ac3.form_submit_button("Add", icon=":material/add:", width="stretch")
            if add:
                current_weights[new_ticker] = new_weight

    if not current_weights:
        st.caption("No tickers added yet — add one above.")
        return current_weights

    hc1, hc2, hc3 = st.columns([2, 1, 1])
    hc1.caption("Ticker")
    hc2.caption("Weight")
    for ticker in sorted(current_weights.keys()):
        rc1, rc2, rc3 = st.columns([2, 1, 1], vertical_alignment="center")
        rc1.markdown(f"`{ticker}`")
        weight_key = f"target_allocation_weight_{widget_key}_{ticker}"
        if weight_key not in st.session_state:
            st.session_state[weight_key] = current_weights[ticker]
        rc2.number_input(
            f"{ticker} weight",
            min_value=0.0,
            max_value=1.0,
            step=0.05,
            format="%.2f",
            key=weight_key,
            label_visibility="collapsed",
        )
        current_weights[ticker] = st.session_state[weight_key]
        if rc3.button("Remove", key=f"target_allocation_remove_{widget_key}_{ticker}", icon=":material/delete:"):
            del current_weights[ticker]
            st.session_state.pop(weight_key, None)
            st.rerun()

    total = sum(current_weights.values())
    if current_weights and abs(total - 1.0) > 0.01:
        st.warning(
            f"Weights sum to {total:.0%}, not 100% — contributions will still be split "
            "proportionally by these weights, but double-check this is intentional."
        )
    return current_weights


def _render_target_allocation(tickers: list[str]) -> None:
    """
    MODEL_WIRING.md §3.1 (2026-08-10) — `{account_type: {ticker: weight}}`, how new contributions
    should be invested, BY ACCOUNT TYPE (not individual account — a deliberate simplification per
    the spec's own framing; there can be many accounts of the same type). Consumed by the Income &
    Expenses tab's contribution waterfall + lot creation (`modules/investing.py`). §8 (not built)
    later replaces this manual input with a derived/optimized allocation — the data shape here is
    chosen so that substitution is a change of *source*, not of *interface*.

    **2026-09-06 redesign (NEXT.md item 3)**: replaces the old "pick one of 7 account types from a
    dropdown, edit its own grid, repeat 7 times" flow with **Standard allocation** (one ticker/
    weight list applied to every account type with no override) + **Alternative allocation by
    account type** (only the account types explicitly added here diverge). The two new session-
    state sources of truth — `target_allocation_standard: {ticker: weight}` and
    `target_allocation_overrides: {account_type: {ticker: weight}}` — are assembled into the EXACT
    SAME downstream shape every consumer already reads, every rerun, at the end of this function:
    `target_allocations = {at: target_allocation_overrides.get(at, target_allocation_standard) for
    at in ACCOUNT_TYPES}`. `modules/investing.py` and every other consumer needs ZERO changes.

    **2026-09-06, NEXT.md item A** (amends the redesign above): both the Standard list and every
    Alternative override's own list use the "pick from a dropdown, enter a value, Add" interaction
    (`_render_weight_editor` below) instead of a grid prepopulated with every ticker in the
    universe — the underlying `{ticker: weight}` shape is unchanged, only how a ticker gets added.
    """
    st.caption(
        "How new contributions should be invested. The Standard allocation applies to every "
        "account type below unless you add an Alternative override for it. Weights are normalized "
        "automatically (don't need to sum to exactly 1.0), but a mismatched total is flagged below "
        "so it's never silently renormalized without you seeing it."
    )
    st.info(
        "**Traditional IRA contributions are modeled as non-deductible** — they never reduce "
        "taxable income in this model, so a Traditional IRA is always worse than a Roth IRA under "
        "this model's assumptions, and the contribution waterfall always prefers Roth IRA (see the "
        "Income & Expenses tab for the full explanation).",
        icon=":material/info:",
    )
    if not tickers:
        st.caption("Add at least one ticker in the Macro tab's ETF universe section first.")
        st.session_state.target_allocations = {at: {} for at in ACCOUNT_TYPES}
        return

    st.markdown("**Standard allocation**")
    st.caption("Applies to every account type that has no override of its own, below.")
    st.session_state.target_allocation_standard = _render_weight_editor(
        tickers, st.session_state.target_allocation_standard, "target_allocation_standard_editor"
    )

    st.markdown("**Alternative allocation by account type**")
    overridable_types = [at for at in ACCOUNT_TYPES if at not in st.session_state.target_allocation_overrides]
    if overridable_types:
        with st.form("add_allocation_override_form", clear_on_submit=True):
            oc1, oc2 = st.columns([3, 1])
            new_override_type = oc1.selectbox("Add an override for account type", options=overridable_types)
            add_override = oc2.form_submit_button("Add override", icon=":material/add:", width="stretch")
            if add_override:
                st.session_state.target_allocation_overrides[new_override_type] = {}

    if not st.session_state.target_allocation_overrides:
        st.caption("No overrides yet — every account type uses the Standard allocation above.")
    for account_type in list(st.session_state.target_allocation_overrides.keys()):
        with st.container(border=True):
            hc1, hc2 = st.columns([4, 1])
            hc1.markdown(f"*{account_type}*")
            if hc2.button("Remove override", key=f"remove_allocation_override_{account_type}", icon=":material/delete:"):
                # Clear this override's own per-ticker weight widgets too, not just the dict entry
                # — otherwise re-adding an override for the SAME account type later in this same
                # session would read back a stale weight for any ticker it happens to share with
                # what was just removed (the widget's own `if key not in st.session_state` init-
                # once guard would see the OLD value still sitting there and skip re-initializing).
                widget_key = f"target_allocation_override_editor_{account_type}"
                for ticker in st.session_state.target_allocation_overrides[account_type]:
                    st.session_state.pop(f"target_allocation_weight_{widget_key}_{ticker}", None)
                del st.session_state.target_allocation_overrides[account_type]
                st.rerun()
            st.session_state.target_allocation_overrides[account_type] = _render_weight_editor(
                tickers,
                st.session_state.target_allocation_overrides[account_type],
                f"target_allocation_override_editor_{account_type}",
            )

    st.session_state.target_allocations = {
        at: st.session_state.target_allocation_overrides.get(at, st.session_state.target_allocation_standard)
        for at in ACCOUNT_TYPES
    }


def render() -> None:
    # Cross-tab bridges published by ui/macro_tab.py, which always renders before this tab (see
    # that module's own docstring for why the order is a hard requirement, not a preference).
    universe = st.session_state.etf_universe
    resolved_prices = st.session_state.resolved_prices_full
    tickers = sorted(st.session_state.ticker_universe.keys())
    inflation_rate = st.session_state.inflation_rate

    all_positions = _render_account_holdings(universe, resolved_prices, tickers, inflation_rate)
    accounts = st.session_state.accounts

    with st.expander("Target allocation by account type", icon=":material/pie_chart:"):
        _render_target_allocation(tickers)

    # Cross-tab bridge for the Income & Expenses tab's portfolio roll-forward (MODEL_WIRING.md §4,
    # Step 3, 2026-08-10) — same reliably-fresh pattern as the Macro tab's own bridges above (this
    # tab always renders before Projection). `universe` carries each ticker's own nominal_return/
    # expense_ratio/dividend_rate/income_type; `all_positions` + `accounts` (already in session
    # state) let the Projection tab build one starting tax lot per (account, ticker) — there is no
    # per-purchase lot history on this tab today, only one blended shares/cost-basis row per
    # (account, ticker), so each starting lot's `year_acquired` is today's year, a documented
    # simplification (existing holdings aren't broken into their real historical purchase lots;
    # only NEW lots created going forward, from Step 3 onward, track their own true year).
    st.session_state.portfolio_universe = universe
    st.session_state.portfolio_all_positions = all_positions

    _render_portfolio_summary(accounts, all_positions, universe, inflation_rate)
