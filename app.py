"""
Entry point: page config, session-state init, sidebar (save/load), tab layout. All actual
rendering lives in ui/ — see PROJECT_PLAN.md's architecture principles ("UI is a thin render
layer over pure functions").
"""

import streamlit as st

from state import init_session_state
from ui import demographics_tab, macro_tab, portfolio_tab, projection_tab, sidebar, social_security_tab, tax_tab

st.set_page_config(page_title="Financial model", layout="wide")
init_session_state()
sidebar.render()

st.title("Financial model")
st.caption("All figures in real (inflation-adjusted) dollars unless labeled otherwise.")

# Macro renders BEFORE Portfolio (2026-09-06 reorg, NEXT.md item 1) so its own cross-tab bridges
# (st.session_state.etf_universe/resolved_ticker_prices/resolved_prices_full) are fresh every
# rerun, not stale-by-one — Streamlit renders every tab's body every rerun regardless of which is
# visually active. Social Security renders BEFORE Projection for the identical reason (Module F,
# 2026-08-31, its own computed-benefit bridge) — same pattern, applied here for the newer tab.
tab_macro, tab_portfolio, tab_demographics, tab_tax, tab_social_security, tab_projection = st.tabs(
    ["Macro", "Portfolio", "Demographics", "Tax", "Social Security", "Projection"]
)
with tab_macro:
    macro_tab.render()
with tab_portfolio:
    portfolio_tab.render()
with tab_demographics:
    demographics_tab.render()
with tab_tax:
    tax_tab.render()
with tab_social_security:
    social_security_tab.render()
with tab_projection:
    projection_tab.render()
