"""
Macro tab (2026-09-06 reorg — see NEXT.md item 1): macro inputs, asset classes, and the ETF
universe, moved out of the Portfolio tab. These sections have zero dependency on
accounts/holdings — they only depend on each other and on data/asset_classes.json — so they're
split out here to simplify the Portfolio tab down to accounts/holdings/target allocation/summary.

**Rendered BEFORE the Portfolio tab in app.py's `st.tabs([...])` call — not optional.** Streamlit
renders every tab's body every rerun regardless of which is visually active (documented repeatedly
elsewhere in this app, e.g. why Social Security renders before Projection), so Portfolio's
account-holdings section (which needs a fresh ETF universe/prices) would read a stale-by-one-rerun
universe otherwise. Portfolio's own render() reads this tab's published
`st.session_state.etf_universe` / `resolved_ticker_prices` / `resolved_prices_full` instead of
building them locally — see this tab's own render() for those cross-tab bridges.

UI only; calculation logic lives in modules/portfolio.py, live market data lookups in
modules/market_data.py via ui/quotes.py.
"""

from __future__ import annotations

import streamlit as st

from modules.portfolio import build_universe, load_asset_classes
from ui.quotes import render_price_override, resolve_all_prices

# MODEL_WIRING.md §4.2 (2026-08-10): gained "Interest" — cash/money-market/short-duration-bond
# tickers (SPAXX, SGOV, etc.) distribute interest, not dividends; kept distinct from "Ordinary"
# since interest and non-qualified dividends aren't always taxed identically (e.g. Treasury
# interest is state-tax-exempt). Not yet consumed by any calculation — see modules/portfolio.py.
_INCOME_TYPES = ["Qualified", "Ordinary", "Interest"]


def _render_macro_inputs() -> None:
    # 2026-08-30, user request: "Tax rate for pre-tax retirement accounts" and "Capital gains tax
    # rate" removed — they only ever fed the display-only "Liquidation value (est.)" estimate
    # (also removed), never modules/projection.py's/modules/tax.py's real bracket-based tax engine.
    st.number_input("Inflation rate", min_value=0.0, max_value=0.20, step=0.001, format="%.3f", key="inflation_rate")


def _accounts_holding_ticker(ticker: str) -> list[str]:
    """
    Every account name with at least one holding row for `ticker`, in account order. Used by the
    ETF universe's "Remove a ticker" popover so removal is uniform for every ticker — cash-
    equivalent funds (SPAXX, SGOV, or any other) included — rather than only tickers with zero
    holdings being removable in one step. See NEXT.md for the full writeup.
    """
    holding_accounts = []
    for account in st.session_state.accounts:
        df = st.session_state.get(f"positions_df_{account['name']}")
        if df is not None and "ticker" in df.columns and ticker in df["ticker"].dropna().tolist():
            holding_accounts.append(account["name"])
    return holding_accounts


def _tickers_using_asset_class(code: str) -> list[str]:
    """Every ticker in the universe currently tagged with asset class `code`, sorted — same
    "show what's affected before letting the user remove it" spirit as
    `_accounts_holding_ticker`'s own check, used by the custom-asset-class removal popover below."""
    return sorted(t for t, entry in st.session_state.ticker_universe.items() if entry["asset_class"] == code)


def _render_asset_classes(canonical: dict) -> dict:
    """
    Every asset class active in this session — built-in (data/asset_classes.json) plus any
    user-added custom ones — shown, edited, added, and removed all in ONE place (2026-08-14, user
    request, three parts: "asset class returns... should be in the asset class section," "not all
    of the asset classes are displayed," and "addable and removable, the same as an ETF would be").

    Built-in classes are removable the SAME way custom ones are (recorded in
    `st.session_state.removed_canonical_asset_classes`, a set of codes; re-adding that same code
    afterward creates a fresh custom entry, no memory of the old one). `data/asset_classes.json`
    itself is never touched either way — "removed" only ever means "excluded from THIS session's
    own active list," same personal-data boundary `custom_asset_classes` already draws.

    Returns the merged ACTIVE dict (`{code: {"label": str, "nominal_return": float}}`, the exact
    shape `load_asset_classes()` itself returns) for the caller (`render()`) to pass downstream to
    every other section exactly as before.
    """
    active_canonical = {
        code: info for code, info in canonical.items() if code not in st.session_state.removed_canonical_asset_classes
    }
    # Read before the form below so the "already active" validation check reflects the set as of
    # the START of this rerun -- recomputed again right after the form so a class added THIS pass
    # shows up immediately in the list/dropdown/popover below without needing an extra st.rerun().
    active = {**active_canonical, **st.session_state.custom_asset_classes}

    st.caption(
        "Every asset class selectable when adding a ticker in ETF universe below, and its expected "
        'return. Add a class beyond the built-in list (e.g. a regional or factor tilt like "DMSCV" '
        "for Developed Markets Small-Cap Value) the same way you'd add a ticker."
    )
    with st.form("add_asset_class_form", clear_on_submit=True):
        ac1, ac2, ac3 = st.columns([1, 2, 1])
        new_code = ac1.text_input("Code", help="Short identifier, e.g. DMSCV — not a tradable ticker itself.")
        new_label = ac2.text_input("Label", help="Descriptive name, e.g. Developed Markets Small-Cap Value.")
        new_return = ac3.number_input(
            "Default nominal return",
            min_value=-0.10,
            max_value=0.30,
            step=0.001,
            format="%.4f",
            help="Starting expected-return assumption — editable per class afterward, same as any "
            "built-in class, in the list below.",
        )
        add_class = st.form_submit_button("Add", icon=":material/add:", width="stretch")
        if add_class:
            code = new_code.strip().upper()
            label = new_label.strip()
            if not code:
                st.error("Enter a code.")
            elif not label:
                st.error("Enter a label.")
            elif code in active:
                st.error(f"{code} is already an active asset class ({active[code]['label']}).")
            else:
                st.session_state.custom_asset_classes[code] = {"label": label, "nominal_return": new_return}

    # Recomputed (not just reused) so a class added by the form above is reflected immediately.
    active = {**active_canonical, **st.session_state.custom_asset_classes}

    if not active:
        st.caption("No asset classes available — add one above.")
        return active

    st.markdown("**Asset class returns**")
    st.caption(
        "Editable per class — applies to every ticker tagged with that class. Built-in defaults "
        "come from data/asset_classes.json; edits here (and any custom class) are personal "
        "(session/saved-state only)."
    )
    hc1, hc2, hc3 = st.columns([2, 2, 1])
    hc1.caption("Class")
    hc2.caption("Source")
    hc3.caption("Nominal return")
    for code in sorted(active.keys()):
        info = active[code]
        return_key = f"class_return_{code}"
        if return_key not in st.session_state:
            st.session_state[return_key] = st.session_state.asset_class_returns.get(code, info["nominal_return"])
        rc1, rc2, rc3 = st.columns([2, 2, 1], vertical_alignment="center")
        rc1.markdown(f"{code} — {info['label']}")
        rc2.caption("Custom" if code in st.session_state.custom_asset_classes else "Built-in")
        rc3.number_input(
            f"{code} nominal return",
            min_value=-0.10,
            max_value=0.30,
            step=0.001,
            format="%.4f",
            key=return_key,
            label_visibility="collapsed",
        )
        st.session_state.asset_class_returns[code] = st.session_state[return_key]

    with st.popover("Remove an asset class", icon=":material/delete:"):
        remove_code = st.selectbox(
            "Class to remove",
            options=sorted(active.keys()),
            format_func=lambda c: f"{c} — {active[c]['label']}",
            key="remove_asset_class_select",
        )
        tickers_using_it = _tickers_using_asset_class(remove_code)
        if tickers_using_it:
            st.warning(
                f"**{remove_code}** is currently used by: {', '.join(tickers_using_it)}. Reassign or "
                "remove those tickers (ETF universe, below) before removing this class — unlike "
                "removing a ticker, removing an in-use asset class has nowhere sensible to cascade to."
            )
        if st.button("Remove", key="remove_asset_class_btn", width="stretch", disabled=bool(tickers_using_it)):
            if remove_code in st.session_state.custom_asset_classes:
                del st.session_state.custom_asset_classes[remove_code]
            else:
                st.session_state.removed_canonical_asset_classes.add(remove_code)
            st.session_state.asset_class_returns.pop(remove_code, None)
            st.session_state.pop(f"class_return_{remove_code}", None)
            st.rerun()

    return active


def _render_etf_universe(asset_class_defaults: dict) -> dict[str, dict]:
    """
    2026-09-06 reorg (NEXT.md item 1) — merges the old separate "ETF universe" (add-ticker form +
    a READ-ONLY overview table) and "Ticker details" (a separate per-ticker editable grid for
    expense ratio/dividend rate/income type only) into ONE section: one add-form, one editable
    list. The add-form itself is unchanged. The one real capability change, flagged per the
    reorg's own "don't expand inputs" constraint: **asset class becomes editable in place** here
    (previously it could only be set at ticker-creation time — changing it required removing and
    re-adding the ticker). This isn't a new input; `asset_class` is the same existing
    `ticker_universe[ticker]` field, now editable everywhere the other three fields already were.

    Returns the resolved `{ticker: {"price": float, "error": str | None}}` dict, same shape the
    old builder returned, for `render()`'s own cross-tab price bridges below.
    """
    st.caption(
        "Type in every ticker you might hold: its asset class, expense ratio, dividend rate, and "
        "whether its dividends are qualified or ordinary. This becomes the option list for the "
        "holdings on the Portfolio tab — a ticker can't be held until it's added here. Only price "
        "is looked up live, from Yahoo Finance; every other field is entered directly and editable "
        "in place any time, not pulled from any live source."
    )

    class_options = {code: info["label"] for code, info in asset_class_defaults.items()}

    with st.form("add_ticker_form", clear_on_submit=True):
        tc1, tc2 = st.columns(2)
        new_ticker = tc1.text_input("Ticker")
        new_class = tc2.selectbox(
            "Asset class",
            options=list(class_options.keys()),
            format_func=lambda c: f"{c} — {class_options[c]}",
        )
        fc1, fc2, fc3, fc4 = st.columns(4)
        new_expense_ratio = fc1.number_input(
            "Expense ratio",
            min_value=0.0,
            max_value=0.05,
            step=0.0001,
            format="%.4f",
            help="The fund's annual expense ratio, e.g. 0.0003 for 0.03%.",
        )
        new_dividend_rate = fc2.number_input(
            "Dividend rate",
            min_value=0.0,
            max_value=0.20,
            step=0.0001,
            format="%.4f",
            help="Annual dividend yield, e.g. 0.013 for 1.3%.",
        )
        new_income_type = fc3.selectbox(
            "Income type",
            options=_INCOME_TYPES,
            help="Qualified/Ordinary dividends, or Interest for cash, money-market, and "
            "short-duration-bond tickers (e.g. SPAXX, SGOV).",
        )
        add_ticker = fc4.form_submit_button("Add", icon=":material/add:", width="stretch")
        if add_ticker:
            ticker = new_ticker.strip().upper()
            if not ticker:
                st.error("Enter a ticker.")
            elif ticker in st.session_state.ticker_universe:
                st.error(f"{ticker} is already in the universe.")
            else:
                st.session_state.ticker_universe[ticker] = {
                    "asset_class": new_class,
                    "expense_ratio": new_expense_ratio,
                    "dividend_rate": new_dividend_rate,
                    "income_type": new_income_type.lower(),
                }

    if not st.session_state.ticker_universe:
        st.caption("No tickers yet — add one above.")
        return {}

    tickers_in_use = sorted(st.session_state.ticker_universe.keys())
    resolved = resolve_all_prices(tickers_in_use)

    st.markdown("**Tickers**")
    st.caption(
        "Edit any field in place — asset class, expense ratio, dividend rate, or income type — "
        "without removing and re-adding the ticker. Price is read-only (live, from Yahoo Finance)."
    )
    hc1, hc2, hc3, hc4, hc5, hc6 = st.columns([1, 2, 1, 1, 1, 1])
    for col, label in zip((hc1, hc2, hc3, hc4, hc5, hc6), ("Ticker", "Asset class", "Expense ratio", "Dividend rate", "Income type", "Price")):
        col.caption(label)

    for ticker in tickers_in_use:
        entry = st.session_state.ticker_universe[ticker]
        q = resolved[ticker]
        if q["error"]:
            st.warning(f"Live price unavailable for **{ticker}**: {q['error']}. Enter it manually:")
            render_price_override(ticker)
            q["price"] = st.session_state.get(f"manual_price_{ticker}", 0.0)

        dc1, dc2, dc3, dc4, dc5, dc6 = st.columns([1, 2, 1, 1, 1, 1], vertical_alignment="center")
        dc1.markdown(f"`{ticker}`")

        ac_key = f"ticker_ac_{ticker}"
        if ac_key not in st.session_state:
            st.session_state[ac_key] = entry["asset_class"]
        dc2.selectbox(
            f"{ticker} asset class",
            options=list(class_options.keys()),
            format_func=lambda c: f"{c} — {class_options.get(c, c)}",
            key=ac_key,
            label_visibility="collapsed",
        )
        entry["asset_class"] = st.session_state[ac_key]

        er_key = f"ticker_er_{ticker}"
        if er_key not in st.session_state:
            st.session_state[er_key] = entry["expense_ratio"]
        dc3.number_input(
            f"{ticker} expense ratio",
            min_value=0.0,
            max_value=0.05,
            step=0.0001,
            format="%.4f",
            key=er_key,
            label_visibility="collapsed",
        )
        entry["expense_ratio"] = st.session_state[er_key]

        dr_key = f"ticker_dr_{ticker}"
        if dr_key not in st.session_state:
            st.session_state[dr_key] = entry["dividend_rate"]
        dc4.number_input(
            f"{ticker} dividend rate",
            min_value=0.0,
            max_value=0.20,
            step=0.0001,
            format="%.4f",
            key=dr_key,
            label_visibility="collapsed",
        )
        entry["dividend_rate"] = st.session_state[dr_key]

        it_key = f"ticker_it_{ticker}"
        if it_key not in st.session_state:
            st.session_state[it_key] = entry["income_type"].capitalize()
        dc5.selectbox(
            f"{ticker} income type",
            options=_INCOME_TYPES,
            key=it_key,
            label_visibility="collapsed",
        )
        entry["income_type"] = st.session_state[it_key].lower()

        dc6.markdown(f"${q['price']:.2f}")

    # Every ticker is selectable here, uniformly — cash-equivalent funds (SPAXX, SGOV, or any
    # other) included, not just ones with zero holdings. A ticker held somewhere shows exactly
    # where and cascades that deletion on removal instead of blocking it — see
    # _accounts_holding_ticker's own docstring for why.
    with st.popover("Remove a ticker", icon=":material/delete:"):
        remove_ticker = st.selectbox("Ticker to remove", options=tickers_in_use, key="remove_ticker_from_universe")
        holding_accounts = _accounts_holding_ticker(remove_ticker)
        if holding_accounts:
            st.warning(
                f"**{remove_ticker}** is currently held in: {', '.join(holding_accounts)}. Removing "
                "it here also deletes those holdings (shares and cost basis) — this applies the "
                "same way to every ticker, not just ones with no holdings."
            )
        if st.button("Remove", key="remove_ticker_from_universe_btn", width="stretch"):
            for account_name in holding_accounts:
                state_key = f"positions_df_{account_name}"
                df = st.session_state[state_key]
                st.session_state[state_key] = df[df["ticker"] != remove_ticker].reset_index(drop=True)
            del st.session_state.ticker_universe[remove_ticker]
            if holding_accounts:
                # Force every holdings/account data_editor to remount from the now-updated
                # positions_df_* state, same mechanism _clear_holdings_widget_state's callers rely
                # on elsewhere (ui/sidebar.py) — otherwise a stale cached edit-diff tied to the old
                # data could reintroduce the just-removed ticker on the next rerun.
                st.session_state.form_version += 1
            st.rerun()

    return resolved


def render() -> None:
    with st.expander("Macro inputs", icon=":material/tune:"):
        _render_macro_inputs()

    asset_class_canonical = load_asset_classes()
    with st.expander("Asset classes", icon=":material/category:"):
        # Returns the merged ACTIVE dict (built-in minus removed, plus custom) -- the single
        # `asset_class_defaults` every section below consumes, computed and rendered in one place.
        asset_class_defaults = _render_asset_classes(asset_class_canonical)

    with st.expander("ETF universe", icon=":material/list_alt:"):
        resolved_prices = _render_etf_universe(asset_class_defaults)

    asset_classes_resolved = {
        code: {
            "label": info["label"],
            "nominal_return": st.session_state.asset_class_returns.get(code, info["nominal_return"]),
        }
        for code, info in asset_class_defaults.items()
    }
    universe = build_universe(asset_classes_resolved, st.session_state.ticker_universe)

    # Cross-tab bridges (2026-09-06 reorg) — Portfolio's own render() now reads these instead of
    # building the universe/prices locally, same reliably-fresh-every-rerun pattern already used
    # for the Portfolio -> Projection bridges (this tab renders before Portfolio — see app.py).
    st.session_state.resolved_ticker_prices = {t: v["price"] for t, v in resolved_prices.items() if not v["error"]}
    st.session_state.etf_universe = universe
    # A SECOND bridge, beyond the two literally named in NEXT.md's own spec: Portfolio's account-
    # holdings table needs the FULL {ticker: {"price", "error"}} dict, not just the filtered flat
    # one above — a ticker priced via manual override still carries a nonzero "error" (resolve_all_
    # prices never clears it), so the filtered dict silently excludes it. Before this reorg, the
    # holdings table read this exact full dict as a same-function local; without this second bridge,
    # a manually-priced holding would wrongly show "no longer in the ETF universe" — a real behavior
    # regression the reorg's own "no change to the underlying modeling/inputs" constraint forbids.
    st.session_state.resolved_prices_full = resolved_prices
