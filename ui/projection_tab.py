"""
Projection tab (a.k.a. "Income & Expenses" — the name used by the optimization spec this session's
contribution-planning/chart work was built from, a Downloads-folder doc not checked into this repo;
see NEXT.md) — the "version over time" of the Tax tab: runs modules/gross_income.py's year-by-year
gross income/expense projection through modules/tax.py (via modules/projection.py's
project_multi_year adapter) and shows gross vs. net income, expenses, and profit across years, as
Plotly charts and a table, plus a year-by-year contribution-planning section.

Deliberately independent of ui/tax_tab.py's session state (its own proj_* keys, its own filing
status) — per explicit user request, the Tax tab stays a self-contained single-year QC tool and
is not touched by this module.

`current_date` is always `date.today()`, never a user input — "the model starts on today's date"
per the user's request. `birth_date`/`retirement_date` are read from the Demographics tab's
session state rather than re-asked here (same "seed from existing data" convention used
elsewhere), but are only ever used as read-only projection inputs on this tab.

**Charts are Plotly, not Altair** (PLOTLY_CHART_REDESIGN.md, 2026-08-14, a Downloads-folder spec —
`plotly>=5.22` was already in requirements.txt but unused anywhere in the app before this; this is
an adoption, not a new dependency). `grep -rl "import altair" ui/` confirmed this was the only file
using Altair — the migration is fully contained here. See `_style_chart`'s own docstring for the
color/interaction system.
"""

from __future__ import annotations

import uuid
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from modules.contributions import RECOMMENDED_CONTRIBUTION_CONFIG
from modules.demographics import date_at_age
from modules.gross_income import project_gross_income
from modules.portfolio import ACCOUNT_TYPES, portfolio_value_by_account_type
from modules.social_security import load_bend_point_table
from modules.projection import (
    adjusted_wealth_via_retirement_tax_rate,
    average_annual_field,
    average_annual_net_retirement_income,
    average_retirement_tax_rate,
    portfolio_value,
    project_multi_year,
    project_no_income_no_expense_baseline,
)
from modules.tax import (
    FILING_STATUSES,
    annual_additions_ceiling,
    check_415c_limit,
    load_bracket_table,
    load_rmd_table,
)

_FILING_STATUS_LABELS = {"single": "Single", "mfj": "Married filing jointly", "hoh": "Head of household"}

# Display <-> internal-value maps for the contribution mode grid (CONTRIBUTION_TOGGLE_REDESIGN.md
# §8, 2026-08-16) — same pattern as _FILING_STATUS_LABELS above: st.column_config.SelectboxColumn
# shows/returns whatever's literally in the DataFrame cell, so friendly labels are stored in the
# editor's own DataFrame and translated back to modules.contributions' internal mode strings only
# when reading `edited` back out.
_401K_MODE_LABELS = {"max_pretax": "Max pretax", "max_roth": "Max Roth", "custom": "Custom"}
_401K_MODE_VALUES = {v: k for k, v in _401K_MODE_LABELS.items()}
_401K_CUSTOM_TYPE_LABELS = {"pretax": "Pretax", "roth": "Roth"}
_401K_CUSTOM_TYPE_VALUES = {v: k for k, v in _401K_CUSTOM_TYPE_LABELS.items()}
_IRA_MODE_LABELS = {"maximize": "Maximize", "custom": "Custom"}
_IRA_MODE_VALUES = {v: k for k, v in _IRA_MODE_LABELS.items()}

# PLOTLY_CHART_REDESIGN.md §3 — every pairing below was run through Anthropic's internal
# data-visualization method's own validator (OKLab-based CVD simulation under protan/deutan,
# 3:1-contrast floor against the light chart surface) rather than picked by eye; see the spec's own
# quoted pass/fail results for the reasoning behind each pairing (e.g. gross/net "PASS"es CVD but
# ships with direct end-labels as required relief since the net color alone falls short of 3:1
# contrast; profit's green/red is in the CVD "floor" band and is legal only because the zero
# baseline is a second, position-based encoding, not color alone).
#
# Light mode only for now (dark-mode hexes are in the spec but explicitly lower priority there,
# "implement light mode first" — not implemented here; this app has no dark/light theme toggle at
# all yet, so there's nothing to switch between regardless).
# 2026-08-30, user request — full dark theme on this tab's charts, no toggle: every chart already
# funnels through _style_chart()/this one dict, so switching the values here is the entire change.
# The neutral/background/text roles below use PLOTLY_CHART_REDESIGN.md §3's OWN validated dark-mode
# alternates (its own table, written 2026-08-14 "for if/when this app adds a dark theme" — reused
# verbatim, not picked fresh) — the light-mode hex values are gone, not just backgrounded, per the
# user's own "fully replace, no toggle" instruction. Categorical hues below this dict
# (_PRE_RETIREMENT_STACK_COLORS, _ACCOUNT_TYPE_COLORS) are NOT part of the redesign doc's validated
# table and are left as-is — they're mid-brightness colors with reasonable contrast against the new
# near-black surface on their own, and picking new ones without the doc's own CVD validator would be
# "new colors from scratch," which the doc explicitly says to avoid wherever a validated value exists.
_PLOTLY_COLORS = {
    "gross": "#3987e5",
    "net": "#199e70",
    "expense": "#d95926",
    "profit_pos": "#008300",
    "profit_neg": "#e66767",
    "wealth_base": "#3987e5",  # same as "gross" -- the "as-planned" scenario, per the spec's own table
    "wealth_baseline": "#9085e9",
    # 2026-09-10, user request — was "discretionary" (the retirement-income chart's now-removed
    # "Discretionary income" line); repurposed, not replaced, for the new "Total net income"
    # reference line the same chart now draws instead. The underlying CVD reasoning is unchanged:
    # this violet is a third line the redesign spec's own validated pairs don't cover (its explicit
    # color table only validates 2-color pairings) -- reusing the already-validated hue (PASSES
    # against blue at CVD ΔE 13.0, distinct from the teal/yellow the stack already uses) rather than
    # picking an unvalidated new one.
    "total_net_income": "#9085e9",
    # 2026-08-31, user request — the retirement-income chart's own Social Security vs. Other
    # stacked-area split (see _retirement_income_chart). Reuses the same yellow already validated
    # and in use elsewhere on this tab (_ACCOUNT_TYPE_COLORS' "Roth 401(k)" slot, PLOTLY_CHART_
    # REDESIGN.md-era palette) rather than picking a brand-new hue from scratch — "Other (after
    # tax)" keeps the existing "net" teal it's replacing conceptually (it's still the majority,
    # variable-withdrawal component), so this is the one genuinely new color this split needs.
    "ss_income": "#eda100",
    # 2026-08-15, user request — the pre-retirement stack switched from total gross income
    # (earned + investment income) to earned income alone; this is the un-stacked reference line
    # added back on top showing the ORIGINAL total figure for context. A muted, dashed neutral
    # (not one of the six stack band hues, not the bold "gross"/earned-income blue) so it reads as
    # "additional context," not a series competing with the stack itself — same "neutral wash"
    # principle _retirement_income_chart's own tax-burden band already uses for a derived reference.
    # Unchanged in dark mode -- the redesign doc's own table keeps this exact muted tone identical
    # across both light and dark ("Secondary/muted text" row's second value, #898781).
    "gross_total": "#898781",
    "gridline": "#2c2c2a",
    "axis": "#383835",
    "surface": "#1a1a19",
    "text_primary": "#ffffff",
    "text_secondary": "#c3c2b7",
    "text_muted": "#898781",
}
_PLOTLY_FONT = "-apple-system, 'Segoe UI', Roboto, sans-serif"

# Whole-dollar display everywhere on this tab (per explicit user request) — underlying values stay
# full-precision floats throughout modules/gross_income.py, modules/tax.py, and modules/projection.py;
# only the display/formatting layer here rounds. Applies to axis tick labels, tooltips, AND the new
# end-of-line annotations (PLOTLY_CHART_REDESIGN.md §5) alike — a prior session's spec had asked for
# SI-suffix axis ticks ("$120k"); this explicit instruction supersedes that for exact whole-dollar
# ticks instead, and the redesign spec explicitly reconfirms it (quoting this exact comment) rather
# than silently reintroducing compact formatting.
_DOLLAR_TICKFORMAT = "$,.0f"

# PLOTLY_CHART_REDESIGN.md §4 — hide the modebar at rest (it still appears on hover), no Plotly
# logo. Module-level and reused by every st.plotly_chart(...) call on this tab, per the spec's own
# "define this once" instruction.
PLOTLY_CONFIG = {"displayModeBar": "hover", "displaylogo": False}


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """`#rrggbb` -> `rgba(r,g,b,alpha)` -- Plotly fill colors need an explicit alpha channel (no
    separate "opacity" property on `fillcolor` itself the way Altair's `mark_area(opacity=...)`
    worked), so every semi-transparent fill on this tab goes through this one conversion."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _dollar_end_label(fig: go.Figure, x: float, y: float, color: str, xshift: int = 8) -> None:
    """One end-of-line annotation showing the final year's value in exact whole dollars
    (PLOTLY_CHART_REDESIGN.md §5.1) — "label the endpoint... never every point": called once per
    SERIES that's actually the point of the chart, not once per stacked band (see
    `_pre_retirement_overview_chart`, which labels only the Gross income boundary line, not all six
    bands underneath it)."""
    fig.add_annotation(
        x=x,
        y=y,
        text=f"${y:,.0f}",
        showarrow=False,
        xanchor="left",
        xshift=xshift,
        font=dict(size=13, color=color, family=_PLOTLY_FONT),
    )


def _style_chart(
    fig: go.Figure,
    title: str,
    height: int,
    y_max: float | None = None,
    allow_negative: bool = False,
    show_legend: bool = True,
    secondary_y_title: str | None = None,
    secondary_y_tickformat: str = ".0%",
) -> None:
    """
    Shared styling applied to every chart on this tab (PLOTLY_CHART_REDESIGN.md §2-§4) — the single
    place the color/interaction/layout system lives, so no chart restates it. Concretely:

    - **Unified crosshair hover** (`hovermode="x unified"` + `showspikes`): one tooltip lists every
      series at the hovered year, values leading (bold) — each trace supplies its own
      `hovertemplate` with a `<b>$%{y:,.0f}</b><extra>Series name</extra>` shape (the `<extra>` tag
      supplies the series name without Plotly's default secondary "trace box").
    - **Solid hairline gridlines everywhere** — the OLD Altair theme dashed every gridline
      (`gridDash: [2, 2]`), a real anti-pattern the redesign spec calls out explicitly: dashing
      reads as "projection" or "threshold," and routine gridlines are neither. The one legitimate
      exception (the Profit chart's zero-reference line, a REAL threshold) is added directly by that
      chart's own builder, not here.
    - **Exact whole-dollar y-axis ticks** (`_DOLLAR_TICKFORMAT`) — never compact/SI-suffix.
    - **15% headroom above the highest plotted value** (`y_max`) so end-of-line labels never crowd
      the plot's top edge, via `rangemode="tozero"` — "taller y-axis" means more room for the trend
      to unfold, not an axis range that extends past the data (that would compress the trend
      visually smaller, the opposite of the goal). `allow_negative=True` skips both the floor and
      the headroom entirely (full autorange) for a chart whose bands/lines can legitimately go
      negative (a dissaving year, a shortfall year, Profit itself) — forcing `tozero` there would
      silently CLIP those values off the bottom of the chart, which is worse than no headroom logic
      at all.
    - **Opt-in secondary (right-side) axis** (2026-09-10, user request — `_retirement_income_chart`'s
      own "Effective tax rate" line): `secondary_y_title=None` (the default) leaves every chart
      exactly as before, single dollar axis only. Passing a title adds a second, independent y-axis
      (`yaxis2`, `overlaying="y"`, `side="right"`) for a trace on a genuinely different scale (a
      percentage, not a dollar amount) to share the plot area without being squashed onto — or
      distorting — the dollar axis. No gridlines of its own (`showgrid=False`) so it doesn't add a
      second, unrelated gridline set fighting the existing dollar gridlines.
    """
    fig.update_layout(
        title=dict(text=title, font=dict(color=_PLOTLY_COLORS["text_primary"], size=16)),
        height=height,
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor=_PLOTLY_COLORS["surface"],
            bordercolor=_PLOTLY_COLORS["gridline"],
            font_size=13,
            font_family=_PLOTLY_FONT,
        ),
        plot_bgcolor=_PLOTLY_COLORS["surface"],
        paper_bgcolor=_PLOTLY_COLORS["surface"],
        font=dict(family=_PLOTLY_FONT, color=_PLOTLY_COLORS["text_secondary"]),
        # 2026-08-14, user request ("the axes do not run up against the edges so aggressively") --
        # the original l=10/b=10 left barely any room for the y-axis title ("Dollars (real)",
        # rotated) and the x-axis title ("Year") alongside their own tick labels, cramming both
        # against the plot edge. `automargin=True` below (belt-and-suspenders) lets Plotly expand
        # further still if a particular chart's tick labels (e.g. 7-figure dollar amounts) need
        # more room than this base margin already provides.
        margin=dict(r=90, t=48, l=70, b=70),
        showlegend=show_legend,
        legend=dict(orientation="h", y=-0.15, font=dict(color=_PLOTLY_COLORS["text_secondary"])),
    )
    fig.update_xaxes(
        title="Year",
        tickformat="d",
        automargin=True,
        gridcolor=_PLOTLY_COLORS["gridline"],
        gridwidth=1,
        linecolor=_PLOTLY_COLORS["axis"],
        color=_PLOTLY_COLORS["text_secondary"],
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikethickness=1,
        spikedash="solid",
        spikecolor=_PLOTLY_COLORS["axis"],
    )
    y_axis_kwargs = dict(
        title="Dollars (real)",
        tickformat=_DOLLAR_TICKFORMAT,
        automargin=True,
        gridcolor=_PLOTLY_COLORS["gridline"],
        gridwidth=1,
        linecolor=_PLOTLY_COLORS["axis"],
        color=_PLOTLY_COLORS["text_secondary"],
    )
    if not allow_negative:
        y_axis_kwargs["rangemode"] = "tozero"
        if y_max is not None and y_max > 0:
            y_axis_kwargs["range"] = [0, y_max * 1.15]
    fig.update_yaxes(**y_axis_kwargs)

    if secondary_y_title is not None:
        fig.update_layout(
            yaxis2=dict(
                title=secondary_y_title,
                tickformat=secondary_y_tickformat,
                overlaying="y",
                side="right",
                showgrid=False,
                automargin=True,
                linecolor=_PLOTLY_COLORS["axis"],
                color=_PLOTLY_COLORS["text_secondary"],
            )
        )


_PRE_RETIREMENT_STACK_COLORS = {
    "Taxes": "#d62728",
    "401k contributions": "#9467bd",
    "Traditional IRA contributions": "#8c564b",
    "Roth IRA contributions": "#2ca02c",
    "Expenses": _PLOTLY_COLORS["expense"],
    "Taxable account & other savings": "#17becf",
    # 2026-09-06, NEXT.md item B1 — the earned-income surplus NOT becoming a new Stage-2
    # contribution (replaces the old "coasting-phase freeze," see modules/projection.py's own
    # docstring). Gray — tab10's own remaining unused hue, deliberately neutral/muted rather than a
    # hue implying "growth" (green) or "cost" (red): this band is money leaving the model as spent,
    # neither invested nor a bill being paid down.
    "Discretionary spending": "#7f7f7f",
    # 2026-09-06, user request — the two new bands sitting ABOVE the Earned income boundary line,
    # between it and the Total gross income line (see _pre_retirement_overview_chart). Same "reuse
    # an already-established categorical family rather than invent new colors from scratch"
    # reasoning as the six above: these six already read as matplotlib's tab10 palette (d62728,
    # 9467bd, 8c564b, 2ca02c, 17becf, plus the custom "expense" hue) — pink and olive are tab10's
    # two remaining unused hues, picked here rather than shades of the existing six specifically so
    # the boundary between "earned income" bands and "investment income" bands reads clearly at a
    # glance, not as a subtle tint of a band already in use.
    "Reinvested dividends/interest": "#e377c2",
    "Tax on dividends/interest": "#bcbd22",
}
# Bottom-to-top stacking order (2026-08-14, at the user's request) — this list is written
# bottom-first because BOTH renderers this tab has used stack traces/marks in ADD/enumerate order,
# lowest-first-added = closest to the baseline: originally confirmed empirically against Vega-Lite
# (rendered a minimal 6-band test chart via `vl-convert-python` and inspected the resulting PNG,
# since guessing the direction wrong would be a silent, easy-to-miss mistake) and unchanged by the
# later Plotly migration (PLOTLY_CHART_REDESIGN.md) — `go.Figure.add_trace`'s own `stackgroup`
# behavior follows the identical "first trace added = bottom of the stack" convention, so this same
# list, iterated in the same order, produces the same visual result under either renderer. Top-to-
# bottom reading of the chart: Tax on dividends/interest, Reinvested dividends/interest, Taxes,
# Expenses, 401(k) contributions, Traditional IRA contributions, Roth IRA contributions,
# Discretionary spending, Taxable account & other savings. The last two entries added 2026-09-06
# (Tax/Reinvested dividends) are the ones that sit ABOVE the Earned income boundary line — see
# _pre_retirement_overview_chart's own docstring. "Discretionary spending" (also 2026-09-06) is
# placed right next to "Taxable account & other savings" -- both are the same "leftover earned
# surplus" concept, just split between what gets invested and what doesn't.
_PRE_RETIREMENT_STACK_ORDER = [
    "Taxable account & other savings",
    "Discretionary spending",
    "Roth IRA contributions",
    "Traditional IRA contributions",
    "401k contributions",
    "Expenses",
    "Taxes",
    "Reinvested dividends/interest",
    "Tax on dividends/interest",
]


def _pre_retirement_overview_chart(rows: list[dict]) -> go.Figure:
    """Pre-retirement overview: a STACKED AREA chart — Earned income drawn as a bold line marking
    the boundary between seven mutually exclusive, exhaustive bands UNDER it (accounting for every
    dollar of EARNED income: Taxes, 401(k) contributions, Traditional IRA contributions, Roth IRA
    contributions, Expenses, "Discretionary spending" (2026-09-06, see below), and "Taxable account
    & other savings") and two more bands ABOVE it (2026-09-06, see below) reaching up to a "Total
    gross income" line (dashed, muted, still overlaid as an un-stacked reference — see that trace's
    own comment for why it stays separate).

    **2026-08-15, redesigned again at the user's request**: the stack now sums to EARNED income
    (`gross_w2 + gross_se` — the same IRC §219(f)(1) "earned income" concept used elsewhere on this
    tab, e.g. the IRA-contribution earned-income gate) instead of total `gross_income` (which also
    folds in investment income —
    dividends/interest/gains). The motivating case: a $0-earned-income year with a real dividend-
    paying portfolio previously still showed a nonzero stack here — technically "exact" against
    `gross_income`, but conceptually misleading for a chart titled "where gross income goes," since
    that income was never a paycheck being allocated; `roll_forward_portfolio`'s own dedicated,
    unconditional DRIP reinvestment (see modules/projection.py's `investable`-sizing comment) already
    handles investment income entirely separately from this stack's five destinations. Now, with $0
    earned income, every band (and the boundary line) correctly reads exactly $0.

    `earned_income_tax` (modules/projection.py, new field) is the isolated tax on `gross_w2 +
    gross_se` alone — investment/break income zeroed, this row's own ACTUAL 401(k)-family
    contributions applied (those DO reduce earned-income tax) — replacing `total_tax` (which taxes
    the full, investment-income-inclusive `gross_income`) as this stack's own "Taxes" band.

    Two categories beyond the four the user originally named (Taxes, 401(k), Roth IRA, Expenses,
    Taxable) were added so the stack is provably exact, not approximate — flagged here, not
    silently guessed:

    - **Traditional IRA contributions** — a real category the waterfall can fill (when Roth IRA is
      phased out by MAGI) that would otherwise have nowhere to go in the stack.
    - **"Discretionary spending"** (2026-09-06, NEXT.md item B1) reuses `modules/projection.py`'s
      own `row["discretionary_spending"]` directly, never re-derived here — the earned-income
      surplus NOT becoming a new Stage-2 contribution this year (replaces the old "coasting-phase
      freeze," which used to force this whole chart's earned-income side to $0 during that window
      regardless of whether real income was still flowing — see that module's own docstring for the
      full "why this changed" reasoning). Subtracted out of "Taxable account & other savings" below,
      the same way `roth_ira`/`traditional_ira` already were, so the identity keeps holding exactly.
    - **"Taxable account & other savings"** is computed as the EXACT residual —
      `earned_profit - roth_ira_contribution_used - traditional_ira_contribution_used -
      discretionary_spending`, where `earned_profit = (gross_w2 + gross_se - earned_income_tax -
      contributions_401k) - gross_expense` (the earned-income mirror of `profit`, see the algebra
      below) — rather than only the waterfall's own `"taxable"` destination amount. This is
      deliberate: the waterfall's taxable figure is sized from an "investable = baseline_profit ×
      saving_fraction" estimate that can itself differ slightly from the year's REAL, post-
      contribution `profit` (a documented two-pass approximation — see `modules.investing`'s own
      module docstring), and a manual-override year has no tracked "taxable" destination at all.
      Using the residual instead guarantees the stack sums to `gross_w2 + gross_se` EXACTLY, always,
      by construction — at the cost of this band occasionally also absorbing a small amount of
      genuinely uninvested surplus rather than being 100% pure "taxable investing" (now a much
      smaller residual than before 2026-09-06, since `discretionary_spending` already captures the
      LARGEST source of that gap — the full "stopped saving" surplus — as its own honestly-labeled
      band instead). A band that goes NEGATIVE (expenses exceed what earned income alone covers —
      common whenever investment income is doing real work, e.g. a retiree-shaped $0-earned-income
      year with nonzero expenses) means earned income alone didn't cover this year's taxes/
      contributions/expenses — the gap was covered by investment income and/or asset sales, not a
      "dissaving" data-quality problem. `allow_negative` (in the shared `_style_chart` call below) is
      set whenever any band actually goes negative in the data, so that excursion below zero is
      never silently clipped off the bottom of the chart.

    The exact identity, so the seven bands are provably mutually exclusive AND exhaustive against
    EARNED income specifically:
    `taxes + 401k + traditional_ira + roth_ira + expenses + discretionary_spending +
    (earned_profit - roth_ira - traditional_ira - discretionary_spending)`
    `= taxes + 401k + expenses + earned_profit`
    `= taxes + 401k + expenses + (earned_net_income - expenses)`
    `= taxes + 401k + earned_net_income = taxes + 401k + (earned_income - taxes - 401k) = earned_income`.

    **2026-09-06, user request — investment income re-added, as two NEW bands ABOVE the Earned
    income boundary line, not folded back into the six earned-income bands above**: the same-day
    dividend-tax fix (`modules/projection.py`'s `tax_attributable_to_investment_income` — a Taxable
    holding's distribution genuinely splits into an after-tax reinvested amount and a real tax bill,
    where before this chart's own 2026-08-15 redesign that split was invisible, explained only in a
    `st.caption` in words) is now shown directly, as real dollars, instead:
    - **Reinvested dividends/interest** = `max(0, gross_investment_income -
      tax_attributable_to_investment_income)` — the actual after-tax amount that buys new Taxable-
      account shares this year (mirrors exactly what `modules.investing.roll_forward_portfolio`'s
      own reinvestment lots are scaled to, post-fix).
    - **Tax on dividends/interest** = `tax_attributable_to_investment_income` directly — the same
      isolated, marginal-tax figure the fix computes (a second `compute_taxes` call with investment
      income zeroed, the identical isolation technique `earned_income_tax`/`tax_attributable_to_ss`
      already use elsewhere), not a flat blended rate.

    Together, these two bands exactly bridge Earned income up to Total gross income WHENEVER
    `gross_ordinary_break_income` and `ss_benefit_gross` are both $0 for the year — the common
    pre-retirement case. Those two income sources are real but still NOT represented anywhere in
    this stack (a pre-existing scope boundary this pass didn't try to close): a year with nonzero
    break income or SS claimed before formal retirement will show a real, honest gap between the top
    of the 8-band stack and the dashed Total gross income line rather than a silent mismatch — not a
    bug, just an honest reflection of what this chart does and doesn't account for yet. The rare
    "tax exceeds the dividend itself" edge case (`modules/projection.py`'s own clamping note on
    `tax_attributable_to_investment_income`) can also show as the Tax band alone briefly exceeding
    `gross_investment_income` — same honest-overshoot reasoning, not clipped or hidden.

    Only ever called with pre-retirement rows (`not is_withdrawal_year`) — see the call site.
    PLOTLY_CHART_REDESIGN.md §5's "label the endpoint... never every point" rule means only the
    Earned income boundary line and the Total gross income overlay get end-of-line labels here, not
    all nine bands underneath — nine competing end-labels on a stacked chart would be clutter, the
    opposite of "at a glance.\""""
    years: list[int] = []
    band_values: dict[str, list[float]] = {category: [] for category in _PRE_RETIREMENT_STACK_ORDER}
    earned_values: list[float] = []
    gross_total_values: list[float] = []
    for r in rows:
        if r["profit"] is None or r["earned_income_tax"] is None:
            continue
        earned_income = r["gross_w2"] + r["gross_se"]
        taxes = r["earned_income_tax"]
        contributions_401k = (
            r["w2_401k_contribution_used"]
            + r["roth_401k_contribution_used"]
            + r["se_401k_employee_contribution_used"]
            + r["se_401k_employer_contribution_used"]
        )
        roth_ira = r["roth_ira_contribution_used"]
        traditional_ira = r["traditional_ira_contribution_used"]
        earned_net_income = earned_income - taxes - contributions_401k
        earned_profit = earned_net_income - r["gross_expense"]
        # 2026-09-06, NEXT.md item B1 — reuses modules/projection.py's own `discretionary_spending`
        # field directly (never re-derived locally, per that module's own "one formula, not two
        # that could drift apart" principle) rather than assuming every dollar of earned_profit
        # became a Taxable contribution — subtracted out of `taxable_and_other` below so the
        # six-band-plus-discretionary-spending identity still sums to EARNED income exactly, the
        # same way `roth_ira`/`traditional_ira` were already subtracted out before this.
        discretionary_spending = r["discretionary_spending"] or 0.0
        taxable_and_other = earned_profit - roth_ira - traditional_ira - discretionary_spending
        # 2026-09-06, user request — the two bands ABOVE the Earned income line (see this
        # function's own docstring for the exact formulas/caveats). `or 0.0` guards: both fields are
        # `None` whenever portfolio roll-forward isn't active (no universe/lots configured), the
        # same "$0 investment income" backward-compatible default every other portfolio-only field
        # on this row already uses.
        gross_investment_income = r["gross_investment_income"] or 0.0
        tax_on_investment_income = r["tax_attributable_to_investment_income"] or 0.0
        reinvested_investment_income = max(0.0, gross_investment_income - tax_on_investment_income)
        values_by_category = {
            "Taxes": taxes,
            "401k contributions": contributions_401k,
            "Traditional IRA contributions": traditional_ira,
            "Roth IRA contributions": roth_ira,
            "Expenses": r["gross_expense"],
            "Taxable account & other savings": taxable_and_other,
            "Discretionary spending": discretionary_spending,
            "Reinvested dividends/interest": reinvested_investment_income,
            "Tax on dividends/interest": tax_on_investment_income,
        }
        years.append(r["year"])
        for category in _PRE_RETIREMENT_STACK_ORDER:
            band_values[category].append(values_by_category[category])
        earned_values.append(earned_income)
        gross_total_values.append(r["gross_income"])

    fig = go.Figure()
    for category in _PRE_RETIREMENT_STACK_ORDER:
        color = _PRE_RETIREMENT_STACK_COLORS[category]
        fig.add_trace(
            go.Scatter(
                x=years,
                y=band_values[category],
                mode="lines",
                name=category,
                stackgroup="pre_retirement",
                line=dict(width=0.5, color=color),
                fillcolor=_hex_to_rgba(color, 0.85),
                hovertemplate="<b>$%{y:,.0f}</b><extra>" + category + "</extra>",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=years,
            y=earned_values,
            mode="lines+markers",
            name="Earned income",
            line=dict(width=3, color=_PLOTLY_COLORS["gross"]),
            marker=dict(size=5, color=_PLOTLY_COLORS["gross"]),
            hovertemplate="<b>$%{y:,.0f}</b><extra>Earned income</extra>",
        )
    )
    # Un-stacked overlay (2026-08-15, user request: "an additional line overlaid (not area) that
    # shows total gross income with dividends etc") — NOT part of the stackgroup, so it never adds
    # to the shaded areas underneath; the top of the 8-band stack now reaches this line exactly in
    # the common case (2026-09-06, see this function's own docstring), so this line's remaining job
    # is honesty: any visible daylight between the stack's own top and this line is real income
    # (break income, pre-retirement SS) this chart still doesn't have a band for — a signal worth
    # keeping, not redundant with the stack now that the two are usually flush.
    fig.add_trace(
        go.Scatter(
            x=years,
            y=gross_total_values,
            mode="lines",
            name="Total gross income (incl. investment income)",
            line=dict(width=2, color=_PLOTLY_COLORS["gross_total"], dash="dash"),
            hovertemplate="<b>$%{y:,.0f}</b><extra>Total gross income</extra>",
        )
    )
    if years:
        _dollar_end_label(fig, years[-1], earned_values[-1], _PLOTLY_COLORS["gross"])
        if abs(gross_total_values[-1] - earned_values[-1]) > 1e-6:
            _dollar_end_label(fig, years[-1], gross_total_values[-1], _PLOTLY_COLORS["gross_total"])

    has_negative = any(v < 0 for values in band_values.values() for v in values)
    y_max = max(gross_total_values + earned_values) if years else 0
    _style_chart(
        fig,
        title="Pre-retirement: where gross income goes",
        height=480,
        y_max=y_max,
        allow_negative=has_negative,
    )
    return fig


def _retirement_income_chart(rows: list[dict], title: str = "Total retirement income") -> go.Figure:
    """RETIREMENT_REPORTING_AUDIT.md §2.4 point 2 (2026-08-13) — the withdrawal-phase counterpart to
    the pre-retirement overview chart: total cash actually raised from the portfolio
    (`total_withdrawal_income`) vs. what's left after tax (`net_retirement_income`), same shaded-
    band-for-tax visual convention. Only ever called with withdrawal-phase rows
    (`is_withdrawal_year`) — see the call site.

    The tax-burden band (net to gross) is a DERIVED reference region, not a data series
    (PLOTLY_CHART_REDESIGN.md §6.2) — no legend entry, no hover of its own, and filled with a
    neutral muted wash rather than a hue borrowed from another series (the old Altair version reused
    the Expenses orange for this, which the redesign spec calls out as implying a relationship to
    Expenses that isn't real).

    **2026-08-31, user request**: the single "Net retirement income" line is now a two-band
    STACKED AREA — Social Security (after tax, base of the stack) and Other (after tax, on top) —
    summing to exactly the same `net_retirement_income` total as before (`modules.projection`'s own
    `ss_after_tax_income`/`other_after_tax_income`, a real split of the existing total via a
    second, SS-zeroed `compute_taxes` pass — see that module's own docstring for why this is
    correct where a flat blended rate wouldn't be). The Tax wash is otherwise unchanged — its own
    top edge is still `net_retirement_income` (now the stack's own total height), so it sits
    directly on top of the two-color stack instead of an empty line.

    **2026-09-10, user request**: `discretionary_income` (2026-08-14's own line — see git history/
    NEXT.md for that fix's write-up if it's ever needed again) is REMOVED — replaced by an explicit
    "Total net income" reference line drawn over the SS/Other stack (same un-stacked-total-over-a-
    stack convention `_pre_retirement_overview_chart`'s own "Total gross income" line already uses),
    plus a per-year "Effective tax rate" line (`retirement_taxes_paid / total_withdrawal_income`,
    same convention `modules.projection.average_retirement_tax_rate` already uses for its own
    average — a `None` gap, not a `0%`, in any year with no withdrawal income at all) on a new,
    opt-in secondary percent axis (`_style_chart`'s `secondary_y_title`)."""
    paired = [
        (
            r["year"], r["total_withdrawal_income"], r["net_retirement_income"],
            r["ss_after_tax_income"], r["other_after_tax_income"], r["retirement_taxes_paid"],
        )
        for r in rows
        if r["total_withdrawal_income"] is not None
        and r["net_retirement_income"] is not None
        and r["ss_after_tax_income"] is not None
        and r["other_after_tax_income"] is not None
        and r["retirement_taxes_paid"] is not None
    ]
    years = [p[0] for p in paired]
    gross = [p[1] for p in paired]
    net = [p[2] for p in paired]
    ss_after_tax = [p[3] for p in paired]
    other_after_tax = [p[4] for p in paired]
    # Same "a year with no withdrawal income doesn't get a rate" convention
    # `modules.projection.average_retirement_tax_rate` uses for its own average — here it's a `None`
    # gap (Plotly draws a break in the line) rather than a row excluded from an average.
    tax_rate = [(p[5] / p[1]) if p[1] else None for p in paired]

    fig = go.Figure()
    if years:
        fig.add_trace(
            go.Scatter(
                x=years + years[::-1],
                y=net + gross[::-1],
                fill="toself",
                fillcolor=_hex_to_rgba(_PLOTLY_COLORS["text_muted"], 0.12),
                line=dict(width=0),
                name="Tax",
                hoverinfo="skip",
                showlegend=False,
            )
        )
    # Social Security first (bottom of the stack, a guaranteed-income "floor" once claimed), Other
    # on top (portfolio withdrawals — the variable layer) -- go.Figure's own "first trace added =
    # bottom of the stack" stackgroup convention, same as the pre-retirement/wealth-by-account
    # stacked charts elsewhere on this tab.
    for values, name, color in (
        (ss_after_tax, "Social Security (after tax)", _PLOTLY_COLORS["ss_income"]),
        (other_after_tax, "Other (after tax)", _PLOTLY_COLORS["net"]),
    ):
        fig.add_trace(
            go.Scatter(
                x=years,
                y=values,
                mode="lines",
                stackgroup="net",
                name=name,
                line=dict(width=0.5, color=color),
                fillcolor=_hex_to_rgba(color, 0.85),
                hovertemplate="<b>$%{y:,.0f}</b><extra>" + name + "</extra>",
            )
        )
    for values, name, color in ((gross, "Gross withdrawal", _PLOTLY_COLORS["gross"]),):
        fig.add_trace(
            go.Scatter(
                x=years,
                y=values,
                mode="lines+markers",
                name=name,
                line=dict(width=2, color=color),
                marker=dict(size=5, color=color),
                hovertemplate="<b>$%{y:,.0f}</b><extra>" + name + "</extra>",
            )
        )
        if years:
            _dollar_end_label(fig, years[-1], values[-1], color)

    # "Total net income" (2026-09-10) — the un-stacked total drawn over the SS/Other stack, same
    # role/styling `_pre_retirement_overview_chart`'s own "Total gross income" reference line plays
    # there (dashed, no markers, its own end label) — REPLACES the old implicit "label the stack's
    # own top edge" call this line used to be, rather than sitting alongside a second label at the
    # same point.
    fig.add_trace(
        go.Scatter(
            x=years,
            y=net,
            mode="lines",
            name="Total net income",
            line=dict(width=2, color=_PLOTLY_COLORS["total_net_income"], dash="dash"),
            hovertemplate="<b>$%{y:,.0f}</b><extra>Total net income</extra>",
        )
    )
    if years:
        _dollar_end_label(fig, years[-1], net[-1], _PLOTLY_COLORS["total_net_income"])

    # "Effective tax rate" (2026-09-10) — a per-year context line on its own secondary percent axis;
    # no end-of-line dollar label (`_dollar_end_label` hardcodes a `$` format, and this is a percent,
    # not a dollar figure — same "no end label" treatment the Tax wash and other pure-context
    # elements on this chart already get).
    fig.add_trace(
        go.Scatter(
            x=years,
            y=tax_rate,
            mode="lines+markers",
            name="Effective tax rate",
            yaxis="y2",
            line=dict(width=2, color=_PLOTLY_COLORS["text_primary"], dash="dot"),
            marker=dict(size=4, color=_PLOTLY_COLORS["text_primary"]),
            hovertemplate="<b>%{y:.1%}</b><extra>Effective tax rate</extra>",
        )
    )

    all_values = gross + net
    has_negative = any(v < 0 for v in all_values)
    y_max = max(all_values) if all_values else 0
    _style_chart(
        fig, title=title, height=480, y_max=y_max, allow_negative=has_negative,
        secondary_y_title="Effective tax rate",
    )
    return fig


def _total_wealth_chart(
    rows: list[dict], baseline_rows: list[dict], withdrawal_start_year: int | None = None
) -> go.Figure:
    """Total portfolio value over time, spanning BOTH pre-retirement and retirement-phase years —
    the page's HERO chart (PLOTLY_CHART_REDESIGN.md §1, 2026-08-14 — promoted to the top of the page,
    directly under the Adjusted-wealth stat groups, resolving ADJUSTED_WEALTH_REDESIGN.md §5.1's own
    open question about whether it should leave the collapsed ledger expander it used to live in: it
    already had, as of this morning's redesign — this pass just makes it visually the biggest,
    tallest chart on the page to match its role). Each row's own `portfolio_value` already reflects
    that year's price roll-forward, contributions, reinvestment, dissaving, withdrawals, and
    rebalancing (see modules/projection.py's own project_multi_year) — no separate recomputation here.

    Also plots the "no income or expenses from today" baseline's own `portfolio_value` per year
    (ADJUSTED_WEALTH_REDESIGN.md §5.1) as a second series, so the two wealth trajectories — "keep
    earning and spending as planned" vs. "stop today, coast to retirement on what I already hold" —
    are visible at a glance across the whole horizon, not just readable from the stat groups above.
    `withdrawal_start_year`, if given, draws a light reference line + annotation there (§6.1) — both
    scenarios' slopes visibly change at that year, and marking it removes a moment of reader
    confusion ("why does the slope change here?")."""
    years = [r["year"] for r in rows]
    values = [r["portfolio_value"] for r in rows]
    baseline_years = [r["year"] for r in baseline_rows]
    baseline_values = [r["portfolio_value"] for r in baseline_rows]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=years,
            y=values,
            mode="lines+markers",
            name="With income & expenses (as planned)",
            line=dict(width=2, color=_PLOTLY_COLORS["wealth_base"]),
            marker=dict(size=5, color=_PLOTLY_COLORS["wealth_base"]),
            hovertemplate="<b>$%{y:,.0f}</b><extra>With income & expenses</extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=baseline_years,
            y=baseline_values,
            mode="lines+markers",
            name="No income or expenses from today",
            line=dict(width=2, color=_PLOTLY_COLORS["wealth_baseline"]),
            marker=dict(size=5, color=_PLOTLY_COLORS["wealth_baseline"]),
            hovertemplate="<b>$%{y:,.0f}</b><extra>No income or expenses</extra>",
        )
    )
    if years:
        _dollar_end_label(fig, years[-1], values[-1], _PLOTLY_COLORS["wealth_base"])
    if baseline_years:
        _dollar_end_label(fig, baseline_years[-1], baseline_values[-1], _PLOTLY_COLORS["wealth_baseline"])

    if withdrawal_start_year is not None and years and min(years) <= withdrawal_start_year <= max(years):
        fig.add_vline(x=withdrawal_start_year, line=dict(width=1, color=_PLOTLY_COLORS["axis"], dash="dot"))
        fig.add_annotation(
            x=withdrawal_start_year,
            y=1.02,
            yref="paper",
            showarrow=False,
            text="Withdrawals begin",
            font=dict(size=11, color=_PLOTLY_COLORS["text_muted"]),
        )

    all_values = [v for v in values + baseline_values if v is not None]
    y_max = max(all_values) if all_values else 0
    _style_chart(fig, title="Total wealth over time", height=600, y_max=y_max)
    return fig


# WEALTH_BY_ACCOUNT_TYPE_CHARTS.md (2026-08-22, user request) — two new stacked-area charts, one per
# scenario, splitting the hero chart's single wealth line into "where is the wealth actually held."
# Reuses seven of the eight validated categorical hues (dataviz method's own reference palette,
# adjacent-pair CVD-validated ordering for stacks/lines) -- deliberately NOT reusing any of
# _PLOTLY_COLORS' existing semantic assignments (gross/net/expense/profit/wealth_base/
# wealth_baseline), since those mean something different on other charts and would be confusing
# reused here. Order below is this palette's own validated adjacent order, not account-type
# semantics -- unrelated categorical data, so a validated-but-otherwise-arbitrary assignment is
# correct, not a placeholder.
_ACCOUNT_TYPE_COLORS = {
    "Taxable": "#2a78d6",  # slot 1, blue
    "Traditional 401(k)": "#eb6834",  # slot 2, orange
    "Traditional IRA": "#1baf7a",  # slot 3, aqua
    "Roth 401(k)": "#eda100",  # slot 4, yellow
    "Roth IRA": "#e87ba4",  # slot 5, magenta
    "HSA": "#008300",  # slot 6, green
    "Other": "#4a3aa7",  # slot 7, violet
}
# Bottom-to-top stack order -- Taxable (most liquid / most likely to be drawn down first per
# DEFAULT_DRAW_ORDER) at the base, tax-advantaged accounts above it, "Other" on top since it's the
# catch-all. First-added = bottom, same go.Figure stackgroup convention as
# _pre_retirement_overview_chart. Purely a reading-order choice.
_ACCOUNT_TYPE_STACK_ORDER = ["Taxable", "Traditional 401(k)", "Traditional IRA", "Roth 401(k)", "Roth IRA", "HSA", "Other"]
assert set(_ACCOUNT_TYPE_STACK_ORDER) == set(ACCOUNT_TYPES), "stack order must cover every account type"


def _wealth_by_account_type_chart(rows: list[dict], title: str) -> go.Figure:
    """
    Same total portfolio value as `_total_wealth_chart`, for ONE scenario's row set, split into a
    stacked area by account type -- "where is the wealth actually held," not just "how much." Each
    row's `ending_lots`/`ending_prices_by_ticker` (already computed by project_multi_year, no new
    modeling) are grouped via `modules.portfolio.portfolio_value_by_account_type`. Bands sum to
    EXACTLY that row's own `portfolio_value` by construction (see the helper's own docstring) --
    same exactness bar `_pre_retirement_overview_chart` holds itself to for its six income bands.

    Only the account types that ever hold a nonzero balance across `rows` get a trace -- an account
    type that's always $0 (e.g., no HSA configured) is dropped rather than plotted as a flat zero
    band cluttering the legend.
    """
    years = [r["year"] for r in rows]
    by_type: dict[str, list[float]] = {account_type: [] for account_type in _ACCOUNT_TYPE_STACK_ORDER}
    for r in rows:
        values_this_year = portfolio_value_by_account_type(r["ending_lots"], r["ending_prices_by_ticker"])
        for account_type in _ACCOUNT_TYPE_STACK_ORDER:
            by_type[account_type].append(values_this_year[account_type])

    active_types = [t for t in _ACCOUNT_TYPE_STACK_ORDER if any(v > 0.005 for v in by_type[t])]

    fig = go.Figure()
    for account_type in active_types:
        color = _ACCOUNT_TYPE_COLORS[account_type]
        fig.add_trace(
            go.Scatter(
                x=years,
                y=by_type[account_type],
                mode="lines",
                name=account_type,
                stackgroup="wealth_by_account_type",
                line=dict(width=0.5, color=color),
                fillcolor=_hex_to_rgba(color, 0.85),
                hovertemplate="<b>$%{y:,.0f}</b><extra>" + account_type + "</extra>",
            )
        )

    totals = [r["portfolio_value"] for r in rows]
    if years:
        _dollar_end_label(fig, years[-1], totals[-1], _PLOTLY_COLORS["text_primary"])

    y_max = max(totals) if totals else 0
    _style_chart(fig, title=title, height=550, y_max=y_max)
    return fig


def _curve_inputs(prefix: str, label: str) -> dict:
    """Four-field S-curve input block (start/end/midpoint/steepness), shared by W2/SE/expense curves."""
    st.markdown(f"**{label} growth curve**")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.number_input(
            "Starting annual rate",
            min_value=0.0,
            step=1000.0,
            format="%.2f",
            key=f"proj_{prefix}_start_value",
            help="The rate as of the start of next full calendar year (t=0) — not the current, "
            "partial year, which is set separately above.",
        )
    with c2:
        st.number_input(
            "Ending annual rate", min_value=0.0, step=1000.0, format="%.2f", key=f"proj_{prefix}_end_value"
        )
    with c3:
        st.number_input(
            "Midpoint (years)",
            min_value=0.0,
            step=0.5,
            format="%.1f",
            key=f"proj_{prefix}_midpoint_years",
            help="Years until the curve is halfway from starting to ending rate.",
        )
    with c4:
        st.number_input(
            "Steepness",
            min_value=0.0,
            step=0.1,
            format="%.2f",
            key=f"proj_{prefix}_steepness",
            help="How sharply the curve bends around the midpoint. Near 0 = straight-line growth "
            "instead of an S-curve.",
        )
    return {
        "start_value": st.session_state[f"proj_{prefix}_start_value"],
        "end_value": st.session_state[f"proj_{prefix}_end_value"],
        "midpoint_years": st.session_state[f"proj_{prefix}_midpoint_years"],
        "steepness": st.session_state[f"proj_{prefix}_steepness"],
    }


def _render_income_breaks() -> None:
    st.markdown("**Income breaks**")
    st.caption(
        "Date-ranged periods (unemployment, a stipend, sabbatical) where income is replaced by a "
        "different annualized rate. End date is exclusive."
    )

    with st.form("add_income_break_form", clear_on_submit=True):
        bc1, bc2, bc3 = st.columns(3)
        new_label = bc1.text_input("Label")
        new_start = bc2.date_input("Start date", value=date.today(), format="YYYY-MM-DD")
        new_end = bc3.date_input("End date (exclusive)", value=date.today(), format="YYYY-MM-DD")
        new_rate = st.number_input(
            "Annualized income during break",
            min_value=0.0,
            step=1000.0,
            format="%.2f",
            help="What this period's income would total if it lasted a full year — day-weighted "
            "against the actual break length.",
        )
        add_break = st.form_submit_button("Add", icon=":material/add:", width="stretch")
        if add_break:
            if not new_label.strip():
                st.error("Enter a label.")
            elif new_end <= new_start:
                st.error("End date must be after start date.")
            elif any(
                new_start < b["end_date"] and b["start_date"] < new_end for b in st.session_state.income_breaks
            ):
                st.error("This date range overlaps an existing break — adjust the dates or remove the other break first.")
            else:
                st.session_state.income_breaks.append(
                    {
                        "id": str(uuid.uuid4()),
                        "label": new_label.strip(),
                        "start_date": new_start,
                        "end_date": new_end,
                        "annualized_income_during_break": new_rate,
                    }
                )

    if not st.session_state.income_breaks:
        st.caption("No income breaks added.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Label": b["label"],
                    "Start": b["start_date"],
                    "End (exclusive)": b["end_date"],
                    "Annualized income": f"${b['annualized_income_during_break']:,.0f}",
                }
                for b in st.session_state.income_breaks
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    with st.popover("Remove an income break", icon=":material/delete:"):
        options = {b["id"]: b["label"] for b in st.session_state.income_breaks}
        remove_id = st.selectbox(
            "Break to remove", options=list(options.keys()), format_func=lambda i: options[i], key="remove_income_break_select"
        )
        if st.button("Remove", key="remove_income_break_btn", width="stretch"):
            st.session_state.income_breaks = [b for b in st.session_state.income_breaks if b["id"] != remove_id]
            st.rerun()


def _age_at_year_end(year: int, birth_date: date) -> int:
    """Same simple `year - birth_date.year` arithmetic modules/projection.py uses internally for
    each row's `age_at_year_end` — duplicated here (not imported, since that arithmetic lives
    inline in project_multi_year, not as its own function) so this tab's own computed-max/Roth-max
    previews agree with the age project_multi_year will actually use for the same year."""
    return year - birth_date.year


_FALLBACK_BRACKET_RATE_OPTIONS = [0.10, 0.12, 0.22, 0.24, 0.32, 0.35, 0.37]


def _bracket_rate_options_for(bracket_table: dict) -> list[float]:
    """MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1 (2026-08-23) — the target-bracket-rate
    selectbox's own options: every real federal ordinary-income rate this app's bracket data
    actually has, for the filing status currently selected on this tab, read from the LATEST
    configured bracket year (options are for "which rate exists at all," not a per-year lookup — the
    real per-year rate used by `ordinary_bracket_ceiling` is resolved fresh inside
    `project_multi_year` itself). Falls back to the standard 2026 federal rate schedule if
    `bracket_table` is empty or the current filing status has no data at all, so the widget never has
    zero options to render."""
    filing_status = st.session_state.proj_filing_status
    if bracket_table:
        latest_year = max(bracket_table.keys())
        federal_brackets = bracket_table[latest_year].get("federal_brackets", {}).get(filing_status)
        if federal_brackets:
            return sorted({rate for _, rate in federal_brackets})
    return _FALLBACK_BRACKET_RATE_OPTIONS


def _render_contributions(income_only_rows: list[dict], birth_date: date, bracket_table: dict) -> dict[int, dict]:
    """
    Renders the "Annual contribution inputs" section (CONTRIBUTION_TOGGLE_REDESIGN.md, 2026-08-16
    — replaces the earlier priority-order-waterfall toggle + six-raw-dollar-field manual grid
    entirely) and returns `st.session_state.contribution_by_year` itself — the per-year
    `contribution_config_by_year` dict to feed into `project_multi_year` this run. A year with no
    saved entry renders (and, once the table is drawn, persists) as
    `modules.contributions.RECOMMENDED_CONTRIBUTION_CONFIG` — max out every destination, the new
    recommended default.
    """
    st.caption(
        "Year-by-year 401(k)-family and IRA contribution instructions. Each destination has its "
        "own mode: max it out (checked against that year's real IRS limits automatically, every "
        "run — never a one-time snapshot that can go stale) or enter a custom amount (also "
        "checked against the real limit, clamped and flagged if it's over). See 'How each "
        "destination resolves' below for the exact mechanics."
    )
    st.info(
        "**Traditional IRA contributions are modeled as non-deductible** — they do not reduce "
        "your taxable income, and withdrawals would be taxed in full as ordinary income (the Form "
        "8606 pro-rata basis recovery rule is not modeled — this makes the model's tax figures "
        "conservative, i.e. an overstatement, if some of your actual contribution would be "
        "deductible). Deductibility depends on income and workplace-plan coverage. Because of this "
        "assumption, a Traditional IRA is always worse than a Roth IRA in this model — Roth IRA "
        "resolves first below, and Traditional only gets whatever's left of the combined limit.",
        icon=":material/info:",
    )
    with st.expander("How each destination resolves", icon=":material/info:"):
        st.markdown(
            "**401(k) mode** (one choice per year):\n\n"
            "- **Max pretax** — the full combined §402(g) limit (W-2 employee deferral room + SE "
            "solo 401(k) employee deferral room), entirely pretax.\n"
            "- **Max Roth** — the same combined limit, but the employee-side portions (W-2 + SE "
            "employee) target Roth instead. SE employer contributions are always pretax by law — "
            "there's no Roth solo-401(k) employer contribution.\n"
            "- **Custom** — a single dollar amount, plus a Pretax/Roth selector, applied first "
            "against the W-2 plan's own remaining capacity; anything beyond that rolls into "
            "SE-employee deferral capacity.\n\n"
            "**'Also maximize the SE solo 401(k) employer contribution'** is its own checkbox, "
            "independent of the 401(k) mode above — a real employer either contributes its "
            "statutory max or doesn't; there's no partial custom amount for it.\n\n"
            "**401(k) is a payroll deduction** — resolved directly from the mode above and never "
            "gated on whether there's cash left after expenses, unlike Roth IRA/Traditional IRA/"
            "Taxable below, which compete for what's actually left after tax and spending.\n\n"
            "**Roth IRA / Traditional IRA mode**: 'Maximize' computes that year's real legal limit "
            "fresh, every run (the MAGI phase-out AND the combined §219(b) limit, correctly "
            "reduced by whatever Roth already used) — never a frozen snapshot. 'Custom' clamps "
            "your entered amount to that same real limit. Roth resolves first; Traditional only "
            "gets whatever's left of the combined limit.\n\n"
            "**Taxable** always receives whatever's left of your actual after-tax, after-expense "
            "cash once Roth IRA and Traditional IRA have taken their share — uncapped, guaranteed, "
            "never an optional side-channel that could silently drop a dollar."
        )

    st.markdown("**Employer 401(k) match**")
    st.caption(
        "Flat plan-design settings (not year-by-year — a real employer's match formula doesn't "
        "typically change year to year). Both default to 0.0: with no employer match, this "
        "produces exactly $0 every year, never an invented number. Employer money — never comes "
        "out of profit, never consumes the §402(g) employee-deferral pool above, but DOES count "
        "toward the W-2 plan's own separate §415(c) annual-additions ceiling (checked below, "
        "independently of the SE solo plan's own ceiling)."
    )
    em1, em2 = st.columns(2)
    with em1:
        st.number_input(
            "Match rate (per employee $ deferred)",
            min_value=0.0,
            max_value=2.0,
            step=0.05,
            format="%.2f",
            key="employer_match_rate",
            help="E.g. 0.50 = employer contributes 50 cents per dollar you defer, within the cap below.",
        )
    with em2:
        st.number_input(
            "Match cap (% of W-2 gross pay)",
            min_value=0.0,
            max_value=1.0,
            step=0.01,
            format="%.2f",
            key="employer_match_cap_pct",
            help="E.g. 0.06 = only the first 6% of pay deferred is eligible for matching.",
        )

    st.selectbox(
        "Sale method for dissaving / withdrawals",
        options=["hifo", "fifo", "average_cost"],
        format_func=lambda m: {
            "hifo": "Highest basis first (HIFO — minimizes realized gain, recommended)",
            "fifo": "Oldest lots first (FIFO)",
            "average_cost": "Average cost per ticker",
        }[m],
        key="sale_method",
        help="MODEL_WIRING.md §3.3/§5.2 — which tax lots get sold first whenever a year needs to "
        "raise cash by selling assets (dissaving — see the results table below). Applies to every "
        "account and every year; there is no per-year override yet.",
    )

    reb_col1, reb_col2 = st.columns([2, 1])
    with reb_col1:
        st.checkbox(
            "Rebalance tax-advantaged accounts annually (recommended)",
            key="rebalance",
            help="MODEL_WIRING.md §7 — once a year, corrects each Traditional/Roth 401(k)/IRA and "
            "HSA back to its target allocation (Portfolio tab) via net-zero-cash trades. No tax "
            "consequence at all inside these accounts, so Total tax/Profit are never affected. "
            "Taxable accounts are NEVER rebalanced by selling (that would realize gains for no "
            "benefit) — Taxable drift is corrected only by directing new contributions, same as "
            "before.",
        )
    with reb_col2:
        st.number_input(
            "Rebalance band",
            min_value=0.0,
            max_value=0.50,
            step=0.01,
            format="%.2f",
            key="rebalance_band",
            help="A ticker within this much of its target weight (e.g. 0.05 = 5 percentage points) "
            "is left untouched that year. 0.00 (default) always corrects to the exact target.",
            disabled=not st.session_state.rebalance,
        )

    _WITHDRAWAL_STRATEGY_LABELS = {
        "flat_percentage": "Flat percentage rule",
        "target_net_spending": "Dynamic — close the funding gap",
    }
    st.selectbox(
        "Retirement withdrawal strategy",
        options=list(_WITHDRAWAL_STRATEGY_LABELS.keys()),
        format_func=lambda s: _WITHDRAWAL_STRATEGY_LABELS[s],
        key="withdrawal_strategy",
        help="MODEL_WIRING.md §2.3 / MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 2 — **Flat "
        "percentage rule**: each year draws a fixed % of the portfolio's start-of-year value, "
        "deliberately independent of that year's actual expenses (funding_gap reports the mismatch "
        "rather than reconciling it). **Dynamic — close the funding gap**: sizes the total draw "
        "so `net_retirement_income` (after tax) lands on that year's actual spending need instead, "
        "via fixed-point iteration (capped at 20 passes/year; a rare non-convergence — e.g. the "
        "portfolio genuinely can't fund the need — shows up as a warning below, not a silent wrong "
        "answer). Either way, the bracket-aware account split and RMD floor below still apply "
        "identically to whatever total is drawn.",
    )
    st.number_input(
        "Retirement withdrawal rate (flat-percentage rule)",
        min_value=0.0,
        max_value=0.20,
        step=0.005,
        format="%.3f",
        key="withdrawal_rate",
        help="Only used by the flat-percentage strategy above (default 4.0%, the spec's own stated "
        "default) — ignored by the dynamic strategy, which sizes its own draw every year instead.",
        disabled=st.session_state.withdrawal_strategy != "flat_percentage",
    )

    bracket_rate_options = _bracket_rate_options_for(bracket_table)
    st.selectbox(
        "Target ordinary-income tax bracket for retirement withdrawals",
        options=bracket_rate_options,
        format_func=lambda r: f"{r:.0%}",
        key="target_bracket_rate",
        help="MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1 — each retirement year, Traditional "
        "401(k)/IRA sales are capped at the top of this bracket (after already-locked-in ordinary "
        "income for the year) rather than drawn first-and-fully like every other account; Taxable "
        "picks up the rest (its gain gets a real shot at the low/0% LTCG bracket precisely because "
        "ordinary income was capped, not left to climb), and Roth absorbs whatever's still needed, "
        "preserved for last since it's the only account that keeps compounding completely tax-free "
        "the longer it's left alone. 22% (default) is a comfortable middle bracket for most "
        "retirees — raise it to draw more Traditional money now at a higher rate; lower it to lean "
        "harder on Taxable/Roth instead.",
    )

    manual_by_year = st.session_state.contribution_by_year
    editor_records = []
    for r in income_only_rows:
        year = r["year"]
        stored = manual_by_year.get(year) or RECOMMENDED_CONTRIBUTION_CONFIG
        editor_records.append(
            {
                "year": year,
                "gross_w2_ref": r["gross_w2"],
                "gross_se_ref": r["gross_se"],
                "401k_mode": _401K_MODE_LABELS[stored.get("contribution_401k_mode", "custom")],
                "401k_custom_amount": stored.get("contribution_401k_custom_amount", 0.0),
                "401k_custom_type": _401K_CUSTOM_TYPE_LABELS[stored.get("contribution_401k_custom_type", "pretax")],
                "maximize_se_employer": bool(stored.get("maximize_se_employer_401k", False)),
                "roth_ira_mode": _IRA_MODE_LABELS[stored.get("roth_ira_mode", "custom")],
                "roth_ira_custom_amount": stored.get("roth_ira_custom_amount", 0.0),
                "traditional_ira_mode": _IRA_MODE_LABELS[stored.get("traditional_ira_mode", "custom")],
                "traditional_ira_custom_amount": stored.get("traditional_ira_custom_amount", 0.0),
            }
        )

    edited = st.data_editor(
        pd.DataFrame(editor_records),
        # Versioned key (same pattern as ui/portfolio_tab.py's accounts/holdings editors) — forces
        # a full remount on Load, so a stale cached edit-diff from a previous save/session can never
        # get silently reapplied on top of freshly loaded contribution_by_year values.
        key=f"contribution_editor_v{st.session_state.form_version}",
        num_rows="fixed",
        hide_index=True,
        disabled=["year", "gross_w2_ref", "gross_se_ref"],
        column_config={
            "year": st.column_config.NumberColumn("Year", format="%d", pinned=True),
            "gross_w2_ref": st.column_config.NumberColumn("Gross W-2 (ref.)", format="$%.0f"),
            "gross_se_ref": st.column_config.NumberColumn("Gross SE (ref.)", format="$%.0f"),
            "401k_mode": st.column_config.SelectboxColumn("401(k) mode", options=list(_401K_MODE_LABELS.values())),
            "401k_custom_amount": st.column_config.NumberColumn("401(k) custom $", format="$%.0f", min_value=0.0),
            "401k_custom_type": st.column_config.SelectboxColumn(
                "401(k) custom type", options=list(_401K_CUSTOM_TYPE_LABELS.values())
            ),
            "maximize_se_employer": st.column_config.CheckboxColumn("Max SE employer?"),
            "roth_ira_mode": st.column_config.SelectboxColumn("Roth IRA mode", options=list(_IRA_MODE_LABELS.values())),
            "roth_ira_custom_amount": st.column_config.NumberColumn("Roth IRA custom $", format="$%.0f", min_value=0.0),
            "traditional_ira_mode": st.column_config.SelectboxColumn(
                "Traditional IRA mode", options=list(_IRA_MODE_LABELS.values())
            ),
            "traditional_ira_custom_amount": st.column_config.NumberColumn(
                "Traditional IRA custom $", format="$%.0f", min_value=0.0
            ),
        },
        width="stretch",
    )

    new_by_year: dict[int, dict] = {}
    for _, edited_row in edited.iterrows():
        year = int(edited_row["year"])
        new_by_year[year] = {
            "contribution_401k_mode": _401K_MODE_VALUES[edited_row["401k_mode"]],
            "contribution_401k_custom_amount": float(edited_row["401k_custom_amount"]),
            "contribution_401k_custom_type": _401K_CUSTOM_TYPE_VALUES[edited_row["401k_custom_type"]],
            "maximize_se_employer_401k": bool(edited_row["maximize_se_employer"]),
            "roth_ira_mode": _IRA_MODE_VALUES[edited_row["roth_ira_mode"]],
            "roth_ira_custom_amount": float(edited_row["roth_ira_custom_amount"]),
            "traditional_ira_mode": _IRA_MODE_VALUES[edited_row["traditional_ira_mode"]],
            "traditional_ira_custom_amount": float(edited_row["traditional_ira_custom_amount"]),
        }
    st.session_state.contribution_by_year = new_by_year

    # Bulk "reset to recommended" action (CONTRIBUTION_TOGGLE_REDESIGN.md §7's own explicit
    # callout: a genuine recompute-every-run MODE, not another one-time-stamp dollar figure like
    # the earlier "Fill Roth IRA at each year's max" button that caused CONTRIBUTION_WATERFALL_
    # BUG.md in the first place — this can never go stale, because a mode is re-resolved against
    # real income/limits on every single run, not frozen at click time).
    if st.button(
        "Reset all years to the recommended default (max everything)",
        icon=":material/restart_alt:",
        help="Sets every projected year's mode to max pretax 401(k) + max SE employer + maximize "
        "both IRAs. Unlike the old 'Fill Roth IRA' button, this sets a MODE, not a frozen dollar "
        "amount — it recomputes correctly against your real income every run.",
    ):
        st.session_state.contribution_by_year = {
            r["year"]: dict(RECOMMENDED_CONTRIBUTION_CONFIG) for r in income_only_rows
        }
        st.rerun()

    return st.session_state.contribution_by_year


def _w2_415c_warning_for_row(r: dict, birth_date: date, bracket_table: dict) -> str | None:
    """
    MODEL_WIRING.md §3.1/§4.2 (2026-08-10) — the W-2 401(k) plan's OWN separate §415(c)
    annual-additions ceiling: W-2-side employee deferral (pretax + Roth) plus the employer match,
    checked independently of the SE solo plan's own ceiling (already covered by
    modules.contributions' own `resolve_401k_target`, via its `over_cap_warning`, folded into
    `row["contribution_warnings"]`). Not produced by modules/projection.py itself — employer match
    isn't fed into the tax calculation at all (it never affects AGI — see employer_401k_match's own
    docstring) — so computed here, reading the row's own ALREADY-RESOLVED `_used`/
    `employer_401k_match` fields directly rather than re-deriving anything from
    `contribution_by_year` (2026-08-16, CONTRIBUTION_TOGGLE_REDESIGN.md — simpler than the prior
    version, which needed a separate `effective_contributions_by_year` side-channel dict just for
    this one check).
    """
    if not r["tax_available"]:
        return None
    employer_match = r.get("employer_401k_match", 0.0)
    if not employer_match:
        return None  # inert when there's no employer match at all — nothing to check
    w2_side_deferral = r["w2_401k_contribution_used"] + r["roth_401k_contribution_used"]
    age = _age_at_year_end(r["year"], birth_date)
    ceiling = annual_additions_ceiling(r["bracket_year_used"], age, bracket_table)
    return check_415c_limit(w2_side_deferral, employer_match, ceiling, plan_label="W-2 401(k)")


def _average_tax_rate_on_401k_during_retirement(rows: list[dict]) -> float:
    """
    Display-only approximation (2026-08-14) for `_blended_profit_by_year` below: the weighted-
    average FEDERAL MARGINAL rate applied to Traditional 401(k)/IRA withdrawals during the
    retirement phase of THIS SAME projection, weighted by each year's own
    `withdrawal_taxable_retirement_distribution`. Never fed back into any tax computation — used
    only to discount pre-retirement 401(k) contributions to their after-eventual-tax equivalent.

    A clearly-labeled proxy, not a rigorous decomposition: isolating exactly which slice of a
    combined federal+CA tax bill is "attributable to" one income stream among several (dividends,
    LTCG, other withdrawals) isn't possible without a marginal what-if recomputation, out of scope
    for a display-only figure. Federal MARGINAL rate specifically (not CA, which has no equivalent
    field exposed today, and not NIIT/Additional Medicare) — marginal is the economically correct
    rate for "what does the next/last dollar of ordinary income cost," and a Traditional 401(k)/IRA
    distribution is exactly that: ordinary income stacking on top of whatever else that year has.

    Falls back to `0.0` (no discount at all) when there's no retirement-phase distribution data to
    compute an average from — a portfolio-inactive projection, or one that never actually draws down
    a Traditional account.
    """
    total_distribution = 0.0
    weighted_rate_sum = 0.0
    for r in rows:
        distribution = r.get("withdrawal_taxable_retirement_distribution") or 0.0
        marginal_rate = r.get("marginal_federal_rate")
        if distribution <= 0 or marginal_rate is None:
            continue
        total_distribution += distribution
        weighted_rate_sum += distribution * marginal_rate
    return weighted_rate_sum / total_distribution if total_distribution > 0 else 0.0


def _blended_profit_by_year(rows: list[dict]) -> dict[int, float]:
    """
    Display-only composite figure (2026-08-14, user request), for each pre-retirement year:
    Traditional 401(k)-family contributions + Roth 401(k) contributions + Roth IRA contributions +
    "other profit" collapsed into ONE number, all expressed in comparable after-tax-equivalent
    dollars — a rough "true economic benefit this year" figure. FOR DISPLAY ONLY: not fed into any
    other calculation, not a new canonical `project_multi_year` row field.

    The math: `profit` already correctly EXCLUDES all 401(k)-family contributions (subtracted as
    payroll-style money that never became liquid cash — see `project_multi_year`'s own docstring)
    but already INCLUDES Roth IRA contributions at full face value (funded FROM `profit`'s own
    already-liquid, already-taxed cash, not a further reduction to it). So "combines 401(k)
    contributions, Roth IRA contributions, and other profit" reduces to: take `profit` as-is (which
    already has the Roth IRA piece baked in correctly) and add back the 401(k)-family money that was
    subtracted out of it — but NOT at face value. Traditional/pretax 401(k) dollars (W-2 pretax + SE
    employee + SE employer — this model assumes the SE side is always pretax, no Roth-vs-traditional
    split there) will be taxed as ordinary income when eventually withdrawn, so they're added back
    at their DISCOUNTED, after-that-eventual-tax value:
    `traditional_401k * (1 - avg_tax_rate_on_401k_during_retirement)`. Roth 401(k) dollars are
    already after-tax today (unlike Traditional, W-2 wages aren't reduced for a Roth deferral — see
    `compute_taxes`'s own docstring) — added back at full face value, no discount.
    """
    avg_rate = _average_tax_rate_on_401k_during_retirement(rows)
    blended: dict[int, float] = {}
    for r in rows:
        if r["profit"] is None:
            continue
        traditional_401k = (
            r["w2_401k_contribution_used"]
            + r["se_401k_employee_contribution_used"]
            + r["se_401k_employer_contribution_used"]
        )
        roth_401k = r["roth_401k_contribution_used"]
        blended[r["year"]] = r["profit"] + traditional_401k * (1.0 - avg_rate) + roth_401k
    return blended


def render() -> None:
    st.header("Projection")
    st.caption(
        "Multi-year version of the Tax tab: projects gross W2/SE income and expenses from today "
        "through retirement (modules/gross_income.py) and runs each year through the same tax "
        "engine (modules/tax.py) to show net income over time. Independent of the Tax tab's own "
        "inputs — nothing here reads or writes tax_* session state."
    )

    current_date = date.today()
    birth_date = st.session_state.birth_date
    retirement_date = st.session_state.retirement_date
    # MODEL_WIRING.md §1.2 (2026-08-10): the projection runs through this planning horizon, not
    # just through retirement_date — retirement_date now only stops W-2/SE income (see
    # modules.gross_income.project_gross_income), it's no longer the end of the projection.
    horizon_date = date_at_age(birth_date, st.session_state.planning_horizon_age)

    c1, c2, c3 = st.columns(3)
    c1.metric("Model start date", current_date.isoformat())
    c2.metric("Retirement date", retirement_date.isoformat(), help="Set on the Demographics tab.")
    c3.metric(
        "Planning horizon",
        horizon_date.isoformat(),
        help=f"Age {st.session_state.planning_horizon_age} — a planning horizon, not a "
        "life-expectancy estimate. Set on the Demographics tab.",
    )
    st.selectbox(
        "Filing status",
        options=FILING_STATUSES,
        format_func=lambda s: _FILING_STATUS_LABELS[s],
        key="proj_filing_status",
    )

    with st.expander("Wages", expanded=True):
        st.caption(f"Current year ({current_date.year}) actual/estimate split — already-earned amounts are not day-weighted.")
        w1, w2 = st.columns(2)
        with w1:
            st.number_input(
                "W-2 already earned this year", min_value=0.0, step=1000.0, format="%.2f", key="proj_already_earned_w2"
            )
            st.number_input(
                "SE already earned this year", min_value=0.0, step=1000.0, format="%.2f", key="proj_already_earned_se"
            )
        with w2:
            st.number_input(
                "W-2 yet to earn this year", min_value=0.0, step=1000.0, format="%.2f", key="proj_yet_to_earn_w2"
            )
            st.number_input(
                "SE yet to earn this year", min_value=0.0, step=1000.0, format="%.2f", key="proj_yet_to_earn_se"
            )
        st.divider()
        w2_curve = _curve_inputs("w2", "W-2")
        st.divider()
        se_curve = _curve_inputs("se", "SE")

    with st.expander("Expenses"):
        st.caption(f"Current year ({current_date.year}) actual/estimate split, mirroring Wages above.")
        e1, e2 = st.columns(2)
        with e1:
            st.number_input(
                "Already incurred this year",
                min_value=0.0,
                step=1000.0,
                format="%.2f",
                key="proj_already_incurred_expense",
            )
        with e2:
            st.number_input(
                "Yet to incur this year", min_value=0.0, step=1000.0, format="%.2f", key="proj_yet_to_incur_expense"
            )
        st.divider()
        expense_curve = _curve_inputs("expense", "Expense")

    with st.expander("Income breaks"):
        _render_income_breaks()

    income_inputs = {
        "current_year_already_earned_w2": st.session_state.proj_already_earned_w2,
        "current_year_already_earned_se": st.session_state.proj_already_earned_se,
        "current_year_yet_to_earn_w2": st.session_state.proj_yet_to_earn_w2,
        "current_year_yet_to_earn_se": st.session_state.proj_yet_to_earn_se,
        "w2_curve": w2_curve,
        "se_curve": se_curve,
        "breaks": st.session_state.income_breaks,
    }
    expense_inputs = {
        "current_year_already_incurred_expense": st.session_state.proj_already_incurred_expense,
        "current_year_yet_to_incur_expense": st.session_state.proj_yet_to_incur_expense,
        "expense_curve": expense_curve,
    }
    bracket_table = load_bracket_table()
    rmd_table = load_rmd_table()
    # 2026-09-06, NEXT.md item B3 -- the Retirement Earnings Test's own exempt-amount table, same
    # file/loader Social Security already uses for bend points/QC thresholds.
    ss_bend_point_table = load_bend_point_table()

    with st.expander("Annual contribution inputs"):
        income_only_rows = project_gross_income(income_inputs, current_date, horizon_date, retirement_date)
        contribution_config_by_year = _render_contributions(income_only_rows, birth_date, bracket_table)

    st.subheader("Results")

    # MODEL_WIRING.md §2's four phase dates -- retirement_date already a separate param;
    # phase_flags_for_year (via project_multi_year) needs the other three too.
    phase_dates = {
        "savings_stop_date": st.session_state.savings_stop_date,
        "withdrawal_start_date": st.session_state.withdrawal_start_date,
        "ss_claim_date": st.session_state.ss_claim_date,
    }

    # Today's real portfolio holdings (Portfolio tab), converted into project_multi_year's starting
    # tax-lot ledger for the Step 3 roll-forward (MODEL_WIRING.md §4, 2026-08-10). Each account's
    # positions carry no per-purchase lot history today (see ui/portfolio_tab.py's own bridge
    # comment) — one blended lot per (account, ticker), dated this year, is the documented
    # simplification; it doesn't affect price roll-forward or dividend income at all, only a future
    # cost-basis/gain feature would ever care about `year_acquired` on an EXISTING holding.
    account_type_by_name = {a["name"]: a["type"] for a in st.session_state.accounts}
    initial_lots = [
        {
            "ticker": p["ticker"],
            "account_type": account_type_by_name.get(p["account"], "Other"),
            "shares": p["shares"],
            "basis_per_share": p["cost_basis_per_share"],
            "year_acquired": current_date.year,
        }
        for p in st.session_state.portfolio_all_positions
    ]

    try:
        rows = project_multi_year(
            income_inputs,
            expense_inputs,
            current_date=current_date,
            horizon_date=horizon_date,
            retirement_date=retirement_date,
            birth_date=birth_date,
            filing_status=st.session_state.proj_filing_status,
            bracket_table=bracket_table,
            contribution_config_by_year=contribution_config_by_year,
            phase_dates=phase_dates,
            employer_match_rate=st.session_state.employer_match_rate,
            employer_match_cap_pct=st.session_state.employer_match_cap_pct,
            universe=st.session_state.portfolio_universe,
            initial_lots=initial_lots,
            initial_prices_by_ticker=st.session_state.resolved_ticker_prices,
            inflation_rate=st.session_state.inflation_rate,
            target_allocations=st.session_state.target_allocations,
            sale_method=st.session_state.sale_method,
            rebalance=st.session_state.rebalance,
            rebalance_band=st.session_state.rebalance_band,
            withdrawal_strategy=st.session_state.withdrawal_strategy,
            withdrawal_strategy_params={
                "rate": st.session_state.withdrawal_rate,
                "target_bracket_rate": st.session_state.target_bracket_rate,
            },
            rmd_table=rmd_table,
            # Module F (2026-08-31) -- cross-tab bridge from the Social Security tab, which
            # renders earlier in app.py's own tab order so this is fresh every rerun, not
            # stale-by-one. Defaults to 0.0 (state.py) until that tab has rendered at least once.
            ss_annual_benefit=st.session_state.computed_ss_annual_benefit,
            ss_bend_point_table=ss_bend_point_table,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    if not rows:
        st.caption("No projected years — check the planning horizon (age) on the Demographics tab.")
        return

    filing_status = st.session_state.proj_filing_status

    # `contribution_warnings` (CONTRIBUTION_TOGGLE_REDESIGN.md, 2026-08-16) carries whatever
    # per-row warnings modules/projection.py itself decides are worth surfacing — no UI-layer
    # re-derivation needed for those. As of 2026-08-30 (user request), that no longer includes the
    # 401(k)-family §402(g) over-cap or the Roth/Traditional IRA over-legal-limit notices: both were
    # confirmed to only ever narrate an automatic correction the model already applies regardless
    # (the contribution amount used in the projection is clamped either way) — see the removal
    # sites in modules/projection.py for the full reasoning. Genuinely uncorrected issues remain:
    # the dynamic-withdrawal-sizing non-convergence warning (also `contribution_warnings`, reused),
    # plus the W-2 plan's own §415(c)-including-employer-match check just below, which still needs
    # a UI-side computation since employer match never reaches modules/projection.py's own tax
    # calculation at all.
    w2_415c_warning_by_year = {r["year"]: _w2_415c_warning_for_row(r, birth_date, bracket_table) for r in rows}
    all_warnings_by_year = {
        r["year"]: (
            list(r.get("contribution_warnings", []))
            + ([w2_415c_warning_by_year[r["year"]]] if w2_415c_warning_by_year[r["year"]] else [])
        )
        for r in rows
    }
    total_warning_count = sum(len(w) for w in all_warnings_by_year.values())
    if total_warning_count:
        years_with_warnings = [y for y, w in all_warnings_by_year.items() if w]
        with st.expander(
            f":material/warning: {total_warning_count} contribution limit warning(s) across "
            f"{len(years_with_warnings)} year(s)",
            icon=":material/warning:",
        ):
            for year in years_with_warnings:
                for w in all_warnings_by_year[year]:
                    st.warning(f"**{year}:** {w}")

    # RETIREMENT_REPORTING_AUDIT.md §1.2 point 2 (2026-08-13): raise the visibility of a real,
    # already-partially-documented gap -- modules.investing.create_lots silently skips converting a
    # year's contribution dollars into actual lots whenever that destination account type has no
    # configured target allocation (empty or all-zero ticker weights). The dollar amount itself is
    # still correctly computed and shown in the destination table below; it just never becomes a
    # portfolio holding. Per the audit's own explicit direction (confirmed with the user,
    # 2026-08-13): the waterfall's own routing behavior is UNCHANGED here — this is a visibility fix
    # only, not a modules/ behavior change.
    target_allocations = st.session_state.target_allocations
    _destination_account_amount_fns = [
        (
            "Traditional 401(k)",
            lambda r: (
                r["w2_401k_contribution_used"]
                + r["se_401k_employee_contribution_used"]
                + r["se_401k_employer_contribution_used"]
                + r["employer_401k_match"]
            ),
        ),
        ("Roth 401(k)", lambda r: r["roth_401k_contribution_used"]),
        ("Roth IRA", lambda r: r["roth_ira_contribution_used"]),
        ("Traditional IRA", lambda r: r["traditional_ira_contribution_used"]),
        ("Taxable", lambda r: (r["contribution_destinations"] or {}).get("taxable", 0.0)),
    ]
    unconverted_totals: dict[str, float] = {}
    for r in rows:
        if not r["tax_available"]:
            continue
        for account_type, amount_fn in _destination_account_amount_fns:
            weights = target_allocations.get(account_type, {})
            if any(w > 0 for w in weights.values()):
                continue  # a real target allocation exists -- these dollars DO become holdings
            amount = amount_fn(r)
            if amount > 0:
                unconverted_totals[account_type] = unconverted_totals.get(account_type, 0.0) + amount
    if unconverted_totals:
        breakdown = "; ".join(
            f"{acct}: ${amt:,.0f}" for acct, amt in sorted(unconverted_totals.items(), key=lambda kv: -kv[1])
        )
        st.warning(
            "**Contribution dollars are not becoming portfolio holdings.** "
            f"{breakdown} was routed there across this projection, "
            "but no ticker weights are configured for that account type — this money is NOT "
            "reflected in any portfolio holdings, the ledger below, or Total wealth. Set target "
            "allocation weights for these account types under **Target allocation by account "
            "type** on the Portfolio tab to convert them into actual holdings.",
            icon=":material/warning:",
        )

    last_configured_bracket_year = max(bracket_table.keys())
    if any(r["year"] > last_configured_bracket_year for r in rows):
        st.caption(
            f"Tax brackets are only explicitly configured through {last_configured_bracket_year} — "
            "later years hold those brackets flat in real (today's-dollar) terms, per this model's "
            "real-dollar convention. See each such row's Notes."
        )

    discount_rate = st.session_state.portfolio_blended_real_return

    # ADJUSTED_WEALTH_REDESIGN.md §2 (2026-08-14, Claude Cowork read-only audit + user request) --
    # the "what if no more income is earned and no more expenses are incurred, starting today"
    # baseline: the SAME portfolio, tax law, and withdrawal mechanics as the base case above, but
    # income/expenses zeroed and retirement_date/savings_stop_date pulled to current_date (see
    # project_no_income_no_expense_baseline's own docstring for exactly what changes and why).
    # Supersedes this morning's earlier "Adjusted wealth (net of future taxes)" per-row table
    # columns/chart line -- a different design this spec's author, reviewing independently, judged
    # conceptually cleaner (a real what-if PROJECTION rather than a per-row marginal-tax netting) --
    # see NEXT.md for the full before/after. Computed once here and reused below for the Adjusted
    # wealth metric, both stat groups, and the wealth chart's second series.
    baseline_rows = project_no_income_no_expense_baseline(
        current_date=current_date,
        horizon_date=horizon_date,
        birth_date=birth_date,
        filing_status=filing_status,
        bracket_table=bracket_table,
        withdrawal_start_date=st.session_state.withdrawal_start_date,
        ss_claim_date=st.session_state.ss_claim_date,
        universe=st.session_state.portfolio_universe,
        initial_lots=initial_lots,
        initial_prices_by_ticker=st.session_state.resolved_ticker_prices,
        inflation_rate=st.session_state.inflation_rate,
        target_allocations=target_allocations,
        sale_method=st.session_state.sale_method,
        rebalance=st.session_state.rebalance,
        rebalance_band=st.session_state.rebalance_band,
        # Deliberately ALWAYS "flat_percentage" here, regardless of st.session_state.
        # withdrawal_strategy (the real scenario's own choice, just above) — this baseline's
        # spending_need is hardcoded to $0 throughout (see project_no_income_no_expense_baseline's
        # own docstring), so "target_net_spending" (close the gap to spending_need) is degenerate
        # in this context: with $0 to spend, it doesn't sit still, it sells just enough to cover
        # the TAX on its own sale, converging on a self-referential number that has nothing to do
        # with real future 401(k) distribution income — exactly the "changes based on inputs that
        # shouldn't matter" symptom this is fixing. "flat_percentage" (a fixed % of the portfolio,
        # the same "assume a modest retirement drawdown" role the ORIGINAL Adjusted-wealth design
        # always used) stays well-defined at $0 spending_need and is the only strategy this metric
        # should ever use — rate/target_bracket_rate below still mirror the user's real inputs,
        # since THOSE are legitimate "how would 401(k) distributions be taxed" assumptions, not
        # future income.
        withdrawal_strategy="flat_percentage",
        withdrawal_strategy_params={
            "rate": st.session_state.withdrawal_rate,
            "target_bracket_rate": st.session_state.target_bracket_rate,
        },
        rmd_table=rmd_table,
        # Module F (2026-08-31, fixed same day as a real bug — see NEXT.md): a SEPARATE cross-tab
        # bridge from the real scenario's own above. This baseline's "no more income from today"
        # premise means only ALREADY-EARNED income (historical years + this year's own
        # already-earned piece) could have generated Social Security credit — computed
        # independently in ui/social_security_tab.py, never the real scenario's own (larger) AIME.
        ss_annual_benefit=st.session_state.computed_ss_annual_benefit_baseline,
        # 2026-09-06, NEXT.md item B3 -- forwarded for symmetry with the real scenario's own call
        # above; always a no-op here since gross_w2/gross_se are unconditionally $0 in this
        # baseline (see project_no_income_no_expense_baseline's own docstring).
        ss_bend_point_table=ss_bend_point_table,
    )

    # ADJUSTED_WEALTH_REDESIGN.md §4-5 -- two more stats, per scenario, side by side so "keep
    # earning & spending as planned" and "stop today" are directly comparable. Hoisted above the
    # "Adjusted wealth" metric below (2026-08-30) since its new calculation reuses this same
    # baseline-scenario figure rather than re-deriving it.
    def _wealth_at_retirement(scenario_rows: list[dict]) -> float | None:
        return next(
            (r["portfolio_value_at_start_of_year"] for r in scenario_rows if r["is_withdrawal_year"]),
            None,
        )

    # Today's actual mark-to-market value -- NOT baseline_rows[0]["portfolio_value"], which is
    # already the END of year 0 after that year's own growth/dividends/tax (ADJUSTED_WEALTH_
    # REDESIGN.md §3.2's own emphasis: net today's real holdings against a figure computed from
    # the SAME baseline scenario, not one that's already one step into the future).
    today_portfolio_value = portfolio_value(initial_lots, st.session_state.resolved_ticker_prices)
    # 2026-08-30, user request: replaced the original "NPV of every future year's tax bill"
    # calculation with a simpler three-step recipe — average effective retirement tax rate ×
    # portfolio value at retirement, discounted back to today — see
    # modules.projection.adjusted_wealth_via_retirement_tax_rate's own docstring for the full
    # reasoning and the exact three steps. Uses the SAME baseline_rows as the two "stop today"
    # stats below (Wealth at retirement / Average annual net retirement income), so all three
    # numbers on this page describing "what if I stopped everything today" are now mutually
    # consistent, computed from one baseline run.
    adjusted_wealth_result = adjusted_wealth_via_retirement_tax_rate(baseline_rows, discount_rate)
    adjusted_wealth_today = adjusted_wealth_result["adjusted_wealth"] if adjusted_wealth_result else None
    st.metric(
        "Adjusted wealth (today, net of future taxes)",
        f"${adjusted_wealth_today:,.0f}" if adjusted_wealth_today is not None else "—",
        help=(
            f"This scenario's own average effective retirement tax rate "
            f"({adjusted_wealth_result['average_tax_rate']:.1%} — retirement_taxes_paid / gross "
            "cash drawn, averaged across every withdrawal-phase year of the 'if I stopped earning/"
            "spending today' baseline below) applied to that SAME baseline's portfolio value AT "
            f"retirement (${adjusted_wealth_result['wealth_at_retirement']:,.0f}), then discounted "
            f"back to today at {discount_rate:.2%} (the Portfolio tab's blended real expected "
            f"return) over {adjusted_wealth_result['years_to_retirement']} years. This isolates "
            "what your CURRENT holdings are worth after their own future retirement tax bill, "
            "independent of any assumption about your future income or spending — it is not a "
            "forecast of your total future wealth (see the two 'wealth at retirement' figures "
            "below for that)."
            if adjusted_wealth_result is not None
            else "No computable figure — either this scenario never reaches its own withdrawal "
            "phase within the planning horizon, or nothing is ever drawn in a withdrawal-phase "
            "year, in the 'if I stopped earning/spending today' baseline below."
        ),
    )
    if discount_rate == 0.0:
        st.caption(
            "Discount rate is currently 0% — either no portfolio holdings are set on the Portfolio "
            "tab yet (an empty portfolio has no blended return to discount against), or the "
            "blended real return there genuinely computes to 0%."
        )

    stat_col1, stat_col2 = st.columns(2)
    with stat_col1:
        st.markdown("**If income & spending continue as planned**")
        wealth_at_retirement = _wealth_at_retirement(rows)
        st.metric(
            "Wealth at retirement",
            f"${wealth_at_retirement:,.0f}" if wealth_at_retirement is not None else "—",
            help="Portfolio value at the start of the first year withdrawals begin.",
        )
        avg_net_retirement_income = average_annual_net_retirement_income(rows)
        st.metric(
            "Avg. annual net retirement income",
            f"${avg_net_retirement_income:,.0f}" if avg_net_retirement_income is not None else "—",
            help="Plain average of Net income (Retirement income table below) across every "
            "withdrawal-phase year through the planning horizon — not discounted.",
        )
        # 2026-08-31, user request — the retirement-income chart's own SS/Other split
        # (modules.projection's ss_after_tax_income/other_after_tax_income), averaged the same
        # "plain mean across withdrawal-phase years" way as the net-income figure just above.
        avg_ss_after_tax = average_annual_field(rows, "ss_after_tax_income")
        st.metric(
            "SS contribution to net income",
            f"${avg_ss_after_tax:,.0f}" if avg_ss_after_tax is not None else "—",
            help="Average annual Social Security benefit, AFTER its own marginal tax cost — the "
            "same after-tax split shown in the retirement-income chart's stacked area below "
            "(Social Security tab's own computed benefit). $0 if no benefit is claimed.",
        )
        avg_other_after_tax = average_annual_field(rows, "other_after_tax_income")
        st.metric(
            "Net income without SS",
            f"${avg_other_after_tax:,.0f}" if avg_other_after_tax is not None else "—",
            help="Avg. annual net retirement income MINUS the SS contribution above — what your "
            "average net retirement income would be from portfolio withdrawals alone, holding "
            "every other income source this same year fixed. Sums with the row above back to "
            "'Avg. annual net retirement income.'",
        )
        # 2026-09-10, user request.
        avg_tax_rate_retirement = average_retirement_tax_rate(rows)
        st.metric(
            "Average tax rate during retirement",
            f"{avg_tax_rate_retirement:.1%}" if avg_tax_rate_retirement is not None else "—",
            help="Plain average, across every withdrawal-phase year, of that year's own effective "
            "tax rate (Taxes paid / Gross withdrawal — see the Retirement income table below). Not "
            "a marginal rate, and not the same figure used to discount 401(k) contributions in the "
            "Pre-retirement wealth table above (that one is federal-marginal-rate-weighted; this "
            "one is the plain effective-rate average already used by the Adjusted wealth metric "
            "above).",
        )
    with stat_col2:
        st.markdown("**If you stopped earning & spending today**")
        baseline_wealth_at_retirement = _wealth_at_retirement(baseline_rows)
        st.metric(
            "Wealth at retirement",
            f"${baseline_wealth_at_retirement:,.0f}" if baseline_wealth_at_retirement is not None else "—",
            help="Same figure, computed on the no-income/no-expense baseline above.",
        )
        baseline_avg_net_retirement_income = average_annual_net_retirement_income(baseline_rows)
        st.metric(
            "Avg. annual net retirement income",
            f"${baseline_avg_net_retirement_income:,.0f}" if baseline_avg_net_retirement_income is not None else "—",
            help="Same figure, computed on the no-income/no-expense baseline above.",
        )
        baseline_avg_ss_after_tax = average_annual_field(baseline_rows, "ss_after_tax_income")
        st.metric(
            "SS contribution to net income",
            f"${baseline_avg_ss_after_tax:,.0f}" if baseline_avg_ss_after_tax is not None else "—",
            help="Same figure, computed on the no-income/no-expense baseline above — uses that "
            "baseline's OWN, separately-computed Social Security benefit (Social Security tab's "
            "\"no further earnings\" figure), never the real scenario's.",
        )
        baseline_avg_other_after_tax = average_annual_field(baseline_rows, "other_after_tax_income")
        st.metric(
            "Net income without SS",
            f"${baseline_avg_other_after_tax:,.0f}" if baseline_avg_other_after_tax is not None else "—",
            help="Same figure, computed on the no-income/no-expense baseline above.",
        )
        # 2026-09-10, user request — reuses `adjusted_wealth_result["average_tax_rate"]` (computed
        # earlier in this function via `adjusted_wealth_via_retirement_tax_rate(baseline_rows,
        # discount_rate)`) rather than a second, redundant `average_retirement_tax_rate(baseline_
        # rows)` call — both are the identical computation on the identical rows; reusing the
        # existing value avoids a redundant pass over `baseline_rows` and keeps this metric visibly
        # tied to the exact number already feeding the "Adjusted wealth" metric's own help text
        # above, rather than two separately-computed figures that must coincidentally agree.
        baseline_avg_tax_rate_retirement = (
            adjusted_wealth_result["average_tax_rate"] if adjusted_wealth_result is not None else None
        )
        st.metric(
            "Average tax rate during retirement",
            f"{baseline_avg_tax_rate_retirement:.1%}" if baseline_avg_tax_rate_retirement is not None else "—",
            help="Same figure, computed on the no-income/no-expense baseline above — this is the "
            "exact rate already feeding the 'Adjusted wealth (today, net of future taxes)' "
            "metric's own calculation further up the page.",
        )

    # RETIREMENT_REPORTING_AUDIT.md §2.4 point 1 (2026-08-13, confirmed with the user): gross_income/
    # net_income mean something different once withdrawing (only the taxable slice of that year's
    # withdrawal, not real spendable cash — §2.2's confirmed defect), so the pre-retirement overview
    # stops at the withdrawal boundary rather than plotting misleading values past it — the more
    # literal reading of "discontinue the lines."
    pre_retirement_rows = [r for r in rows if not r["is_withdrawal_year"]]
    withdrawal_phase_rows = [r for r in rows if r["is_withdrawal_year"]]
    baseline_withdrawal_phase_rows = [r for r in baseline_rows if r["is_withdrawal_year"]]
    total_wealth_rows = [r for r in rows if r["tax_available"] and r["portfolio_value"] is not None]
    baseline_wealth_rows = [r for r in baseline_rows if r["tax_available"] and r["portfolio_value"] is not None]

    if any(not r["tax_available"] for r in rows):
        st.caption(
            "No federal/CA tax bracket data is configured at all — dollar figures in both sections "
            "below are blank rather than guessed for those years. See each row's Notes and "
            "data/tax_brackets.json."
        )

    # 2026-08-30, user request — the page's chart/table content reorganized into two collapsible
    # sections (both default-expanded — this IS the Results section's main content, not secondary
    # detail like the ledger/dissaving/rebalancing expanders further below).
    with st.expander("Projected Wealth", expanded=True, icon=":material/trending_up:"):
        # PLOTLY_CHART_REDESIGN.md §1-§2 (2026-08-14) — Total wealth is this section's HERO chart,
        # full container width. One-line, data-driven caption ABOVE it (§5.2) — the plain-language
        # takeaway, faster to read than the chart itself — computed from the exact same rows
        # feeding that chart, no new modeling.
        if total_wealth_rows:
            # Dollar signs escaped as \$ (2026-08-14, a real rendering bug found and fixed while
            # building this exact caption -- Streamlit's markdown renderer treats a PAIR of literal
            # $ characters as inline LaTeX math delimiters, which silently mangled the very first
            # version of this caption into garbled text; every dollar amount in every caption on
            # this tab needs the same escape, not just the ones that happen to render two $ in one
            # call).
            st.caption(
                f"Grows from **\\${today_portfolio_value:,.0f}** today to a projected "
                f"**\\${total_wealth_rows[-1]['portfolio_value']:,.0f}** by {total_wealth_rows[-1]['year']}, "
                "assuming income and spending continue as planned."
            )
            st.plotly_chart(
                _total_wealth_chart(
                    total_wealth_rows, baseline_wealth_rows, st.session_state.withdrawal_start_date.year
                ),
                theme=None,
                config=PLOTLY_CONFIG,
                width="stretch",
            )

        # WEALTH_BY_ACCOUNT_TYPE_CHARTS.md (2026-08-22, user request) — additive: the hero chart
        # above is untouched, these two split each of its own two lines into a stacked-area
        # breakdown by account type ("where is the wealth actually held"), reusing the exact same
        # row sets, no new project_multi_year/baseline call. Side by side (2026-08-30, user
        # request), "as planned" first (matching the hero chart's own trace order) then the
        # no-income/no-expense baseline.
        wealth_col1, wealth_col2 = st.columns(2)
        with wealth_col1:
            if total_wealth_rows:
                st.plotly_chart(
                    _wealth_by_account_type_chart(
                        total_wealth_rows, "Wealth by account type — with income & expenses (as planned)"
                    ),
                    theme=None,
                    config=PLOTLY_CONFIG,
                    width="stretch",
                )
        with wealth_col2:
            if baseline_wealth_rows:
                st.plotly_chart(
                    _wealth_by_account_type_chart(
                        baseline_wealth_rows, "Wealth by account type — no income or expenses from today"
                    ),
                    theme=None,
                    config=PLOTLY_CONFIG,
                    width="stretch",
                )

        # 2026-08-30, user request: moved up from further down the page, into this section.
        st.markdown("**Pre-retirement wealth**")
        pre_retirement_table_rows = [r for r in rows if not r["is_withdrawal_year"]]
        # Pass the FULL row list (both phases), not just the pre-retirement subset -- the average
        # discount rate is computed from RETIREMENT-phase distribution data (see
        # _average_tax_rate_on_401k_during_retirement), which by definition isn't in
        # pre_retirement_table_rows; the function itself only ever produces entries for years with
        # a real `profit` figure, so passing every row here is safe.
        blended_profit_by_year = _blended_profit_by_year(rows)
        pre_retirement_df = pd.DataFrame(
            [
                {
                    "Year": r["year"],
                    "Total wealth": r["portfolio_value"],
                    "Gross income": r["gross_income"],
                    "Net income": r["net_income"],
                    "Expenses": r["gross_expense"],
                    "Taxes": r["total_tax"],
                    "401k contributions": (
                        r["w2_401k_contribution_used"]
                        + r["roth_401k_contribution_used"]
                        + r["se_401k_employee_contribution_used"]
                        + r["se_401k_employer_contribution_used"]
                    ),
                    "Roth IRA contributions": r["roth_ira_contribution_used"],
                    "Profit": r["profit"],
                    "Blended profit": blended_profit_by_year.get(r["year"]),
                    "Dissaving proceeds": r["dissaving_proceeds"] or 0.0,
                    "Unfunded shortfall": r["dissaving_unfunded_shortfall"] or 0.0,
                    "Notes": " ".join(list(r["notes"]) + all_warnings_by_year[r["year"]]) or "—",
                }
                for r in pre_retirement_table_rows
            ]
        )
        pre_retirement_dollar_cols = [
            "Total wealth", "Gross income", "Net income", "Expenses", "Taxes",
            "401k contributions", "Roth IRA contributions", "Profit", "Blended profit",
            "Dissaving proceeds", "Unfunded shortfall",
        ]
        st.dataframe(
            pre_retirement_df,
            column_config={c: st.column_config.NumberColumn(c, format="$%.0f") for c in pre_retirement_dollar_cols},
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "**Blended profit** (display only, not used elsewhere) collapses 401(k) contributions, "
            "Roth IRA contributions, and the rest of Profit into one after-tax-equivalent figure — "
            "401(k) dollars are discounted by the weighted-average federal marginal rate this same "
            "projection's own Traditional 401(k)/IRA withdrawals experience during retirement (an "
            "approximation, not a new tax computation; see this tab's own code comments for the "
            "exact method), since that money will be taxed as ordinary income when eventually "
            "withdrawn — Roth 401(k)/IRA dollars are already after-tax and count at full face value."
        )
        if any((r["dissaving_proceeds"] or 0.0) > 0 for r in pre_retirement_table_rows):
            st.caption(
                "**Dissaving proceeds** shows years where expenses exceeded after-tax income and "
                "assets were sold to cover the shortfall (MODEL_WIRING.md §5.1) — Net income/Profit "
                "above already reflect the tax on whatever was realized. **Unfunded shortfall** is "
                "nonzero only if the entire draw order ran out of assets before covering the year's "
                "need — never silently absorbed."
            )
        if any((r["dissaving_unfunded_shortfall"] or 0.0) > 0 for r in pre_retirement_table_rows):
            st.warning(
                "One or more pre-retirement years could not fully fund their shortfall by selling "
                "assets — see 'Unfunded shortfall' above. This model does not borrow or go negative "
                "on the portfolio; an unfunded year simply means the plan runs out of money."
            )

    with st.expander("Income Projections", expanded=True, icon=":material/payments:"):
        # 2026-08-30, user request: now the LEAD chart in this section, replacing
        # _earned_income_vs_expenses_chart (removed entirely, not relocated — this stacked
        # breakdown already shows the same Expenses figure, just folded into the fuller
        # allocation). One wide chart per row (2026-08-14, at the user's request) rather than a
        # multi-column layout — gross/net/expenses/profit are all pre-retirement-only concepts
        # consolidated onto this one chart, so there's nothing left to put in other columns.
        if pre_retirement_rows:
            first_earned = pre_retirement_rows[0]["gross_w2"] + pre_retirement_rows[0]["gross_se"]
            last_earned = pre_retirement_rows[-1]["gross_w2"] + pre_retirement_rows[-1]["gross_se"]
            # 2026-09-06, user request — investment income (dividends/interest) is now two real
            # bands ABOVE the Earned income line in the chart itself (Reinvested dividends/interest,
            # Tax on dividends/interest — see _pre_retirement_overview_chart's own docstring for the
            # exact formulas), replacing the standalone caption that used to explain this fix in
            # words alone. This caption's own job shrinks to just the earned-income half of the
            # picture, same as before 2026-09-06.
            st.caption(
                f"Earned income grows from **\\${first_earned:,.0f}** in {pre_retirement_rows[0]['year']} "
                f"to **\\${last_earned:,.0f}** by {pre_retirement_rows[-1]['year']}, split across taxes, "
                "contributions, expenses, discretionary spending, and taxable savings below the bold "
                "Earned income line — dividends/interest (reinvested, and the tax on them) are the two "
                "bands ABOVE it, up to the dashed Total gross income line. **Discretionary spending** "
                "(2026-09-06) is the earned surplus left over once saving has stopped (Demographics "
                "tab's Savings stop date) but real income hasn't — money genuinely spent, not invested."
            )
            st.plotly_chart(_pre_retirement_overview_chart(pre_retirement_rows), theme=None, config=PLOTLY_CONFIG, width="stretch")

        # RETIREMENT_REPORTING_AUDIT.md §2.4 point 2 — the withdrawal-phase counterpart, shown only
        # once there's at least one withdrawal-phase year with usable figures (each chart already
        # filters out any row missing a field, but skip rendering an empty chart entirely). Side by
        # side (2026-08-30, user request): "as planned" (the real scenario) and the SAME chart for
        # the no-income/no-expense baseline, so "keep earning & spending" vs. "stop today" are
        # comparable for retirement income the same way the wealth charts above already are.
        income_col1, income_col2 = st.columns(2)
        with income_col1:
            if any(r["total_withdrawal_income"] is not None for r in withdrawal_phase_rows):
                avg_net_for_caption = average_annual_net_retirement_income(withdrawal_phase_rows)
                st.caption(
                    f"Averages **\\${avg_net_for_caption:,.0f}** a year in net retirement income across "
                    f"{withdrawal_phase_rows[0]['year']}–{withdrawal_phase_rows[-1]['year']}."
                    if avg_net_for_caption is not None
                    else "No computable retirement-income years yet."
                )
                st.plotly_chart(
                    _retirement_income_chart(withdrawal_phase_rows, title="Total retirement income (as planned)"),
                    theme=None, config=PLOTLY_CONFIG, width="stretch",
                )
        with income_col2:
            if any(r["total_withdrawal_income"] is not None for r in baseline_withdrawal_phase_rows):
                baseline_avg_net_for_caption = average_annual_net_retirement_income(baseline_withdrawal_phase_rows)
                st.caption(
                    f"Averages **\\${baseline_avg_net_for_caption:,.0f}** a year, on the no-income/"
                    "no-expense baseline."
                    if baseline_avg_net_for_caption is not None
                    else "No computable retirement-income years yet, on the baseline."
                )
                st.plotly_chart(
                    _retirement_income_chart(
                        baseline_withdrawal_phase_rows, title="Total retirement income (no income or expenses)"
                    ),
                    theme=None, config=PLOTLY_CONFIG, width="stretch",
                )

        # RETIREMENT_REPORTING_AUDIT.md §2.4 points 3-5 (2026-08-13, confirmed with the user:
        # replace the old single combined table entirely). Split at the SAME `is_withdrawal_year`
        # boundary as the charts above — gross_income/net_income mean genuinely different things on
        # each side (§2.2's confirmed defect), so one table with one set of column meanings across
        # the whole horizon would keep reproducing the exact conflation this redesign exists to
        # fix. 2026-08-30, user request: moved up from further down the page, into this section.
        if withdrawal_phase_rows:
            st.markdown("**Retirement income**")
            st.caption(
                "'Gross withdrawal' is the TOTAL cash actually raised from the portfolio this year — "
                "across every account type, including Roth principal and return-of-basis, which never "
                "show up in ordinary gross/net income (RETIREMENT_REPORTING_AUDIT.md §2.2's confirmed "
                "defect: those fields alone understate real spendable retirement income). 'Withdrawal "
                "target' (the flat-percentage rule's own output) and 'Funding gap (pre-tax)' (target "
                "minus that year's actual expenses, BEFORE this year's own withdrawal tax) are reported "
                "honestly, never silently reconciled with each other — a negative gap is a real "
                "shortfall the strategy itself doesn't close. **'Discretionary income'** is the "
                "different, AFTER-TAX figure (Net income minus expenses) — money genuinely left over "
                "once both tax and need are accounted for, so unlike Funding gap it can never exceed "
                "Net income (2026-08-14 fix — Funding gap legitimately CAN exceed Net income in a year "
                "with a large realized-gain tax bill, which is correct for a pre-tax diagnostic but was "
                "wrong to treat as \"discretionary income\"; see the withdrawal rate input above). "
                "**'RMD'** is that year's Required Minimum Distribution once age-eligible (IRS Pub "
                "590-B Table III) — a floor on the Traditional target above, never a ceiling. **'RMD "
                "forced excess'** is whatever it forced beyond that year's actual withdrawal target: "
                "still sold and still taxed as ordinary income (see Taxes paid), but reinvested into "
                "Taxable rather than spent, so it's already excluded from Gross withdrawal/Net income "
                "above — money you were legally required to move, not money you needed to spend. "
                "**'Iterations'** (Dynamic strategy only, blank otherwise) is how many fixed-point "
                "passes it took to close Funding gap onto that year's actual tax bill (MODULE_G2_STEP7_"
                "BRACKET_AWARE_WITHDRAWALS.md Part 2) — 20 with no visible convergence elsewhere on "
                "this page means it hit the iteration cap without converging, worth a closer look."
            )
            retirement_income_df = pd.DataFrame(
                [
                    {
                        "Year": r["year"],
                        "Total wealth": r["portfolio_value"],
                        "Gross withdrawal": r["total_withdrawal_income"] or 0.0,
                        "Net income": r["net_retirement_income"] or 0.0,
                        "Taxes paid": r["retirement_taxes_paid"] or 0.0,
                        "Discretionary income": r["discretionary_income"] or 0.0,
                        "Withdrawal target": r["withdrawal_target"] or 0.0,
                        "Funding gap (pre-tax)": r["funding_gap"] or 0.0,
                        "Unfunded shortfall": r["withdrawal_unfunded_shortfall"] or 0.0,
                        "RMD": r["rmd_amount"] or 0.0,
                        "RMD forced excess": r["rmd_forced_excess"] or 0.0,
                        "Iterations": r["withdrawal_iteration_count"] if r["withdrawal_iteration_count"] is not None else "—",
                        "Notes": " ".join(list(r["notes"]) + all_warnings_by_year[r["year"]]) or "—",
                    }
                    for r in withdrawal_phase_rows
                ]
            )
            retirement_income_dollar_cols = [
                "Total wealth", "Gross withdrawal", "Net income", "Taxes paid",
                "Discretionary income", "Withdrawal target", "Funding gap (pre-tax)", "Unfunded shortfall",
                "RMD", "RMD forced excess",
            ]
            st.dataframe(
                retirement_income_df,
                column_config={c: st.column_config.NumberColumn(c, format="$%.0f") for c in retirement_income_dollar_cols},
                width="stretch",
                hide_index=True,
            )
            if any((r["withdrawal_unfunded_shortfall"] or 0.0) > 0 for r in withdrawal_phase_rows):
                st.warning(
                    "One or more retirement years could not fully fund the withdrawal target by "
                    "selling assets — see 'Unfunded shortfall' above. This model does not borrow or go "
                    "negative on the portfolio; an unfunded year simply means the plan runs out of money."
                )

    with st.expander("Contribution destinations & portfolio ledger (Module D Steps 2-6 — MODEL_WIRING.md §2.3, §3-§7)"):
        st.caption(
            "Where each year's 401(k)-family/IRA/employer-match dollars actually landed (per that "
            "year's own mode — see 'Annual contribution inputs' above), and the resulting "
            "portfolio ledger: today's real holdings (Portfolio tab) PLUS every year's new "
            "contribution lots and reinvested dividends/interest, each rolling forward at its OWN "
            "asset-class return net of its OWN expense ratio (never a blended rate — MODEL_WIRING.md "
            "§4.1). A ticker with no target allocation configured for its account type (Portfolio "
            "tab) produces no new lots from that year's contribution dollars — the dollars still "
            "show up in the destination table below, just unconverted to shares."
        )

        destination_rows = []
        for r in rows:
            if not r["tax_available"]:
                continue
            taxable_used = (r["contribution_destinations"] or {}).get("taxable", 0.0)
            uninvested = (r["contribution_destinations"] or {}).get("uninvested_surplus", 0.0)
            destination_rows.append(
                {
                    "Year": r["year"],
                    "Traditional 401(k) (incl. match)": (
                        r["w2_401k_contribution_used"]
                        + r["se_401k_employee_contribution_used"]
                        + r["se_401k_employer_contribution_used"]
                        + r["employer_401k_match"]
                    ),
                    "Roth 401(k)": r["roth_401k_contribution_used"],
                    "Roth IRA": r["roth_ira_contribution_used"],
                    "Traditional IRA": r["traditional_ira_contribution_used"],
                    "Taxable": taxable_used,
                    "Employer match (of the 401(k) total)": r["employer_401k_match"],
                    "Uninvested surplus": uninvested,
                }
            )

        if destination_rows:
            destination_df = pd.DataFrame(destination_rows)
            dollar_dest_cols = [c for c in destination_df.columns if c != "Year"]
            st.dataframe(
                destination_df,
                column_config={c: st.column_config.NumberColumn(c, format="$%.0f") for c in dollar_dest_cols},
                width="stretch",
                hide_index=True,
            )

        # Dissaving breakdown (Step 4, MODEL_WIRING.md §5.1-§5.2) -- only years where a sale
        # actually happened, so this table is empty/absent for a plan that never runs a shortfall.
        dissaving_rows = [
            {
                "Year": r["year"],
                "Proceeds": r["dissaving_proceeds"],
                "Long-term gain (Taxable)": r["dissaving_long_term_gain"],
                "Short-term gain (Taxable)": r["dissaving_short_term_gain"],
                "Retirement withdrawal (Traditional)": r["dissaving_retirement_withdrawal"],
                "Unfunded shortfall": r["dissaving_unfunded_shortfall"],
            }
            for r in rows
            if r["tax_available"] and (r["dissaving_proceeds"] or 0.0) > 0
        ]
        if dissaving_rows:
            st.markdown("**Dissaving — years assets were sold to cover a shortfall**")
            st.caption(
                "Realized gain/withdrawal amounts here are already reflected in that year's Total "
                "tax/Net income/Profit in the main table above — this just breaks out where the sale "
                "proceeds came from and how they were taxed by account type (§5.2)."
            )
            dissaving_df = pd.DataFrame(dissaving_rows)
            st.dataframe(
                dissaving_df,
                column_config={
                    c: st.column_config.NumberColumn(c, format="$%.0f") for c in dissaving_df.columns if c != "Year"
                },
                width="stretch",
                hide_index=True,
            )

        # The former "Retirement withdrawals — flat-percentage rule" mechanics breakdown (target vs.
        # spending need, dividends vs. sale proceeds, gain by type) lived here through Step 6 —
        # removed per RETIREMENT_REPORTING_AUDIT.md §2.4 point 4 (confirmed with the user,
        # 2026-08-13) as redundant with the new top-level "Retirement income" table above, which now
        # covers Withdrawal target/Funding gap/Unfunded shortfall directly rather than burying them
        # in a collapsed expander.

        # Rebalancing residual drift (Step 5, MODEL_WIRING.md §7) -- only shown when rebalancing is
        # on and at least one account/year has real leftover drift (an unpriced ticker or one
        # inside the rebalance band); the common case is an empty table, which is itself the point
        # -- silence here means the plan is at its exact target every year.
        drift_rows = [
            {"Year": r["year"], "Account type": account_type, "Residual drift (%)": drift * 100.0}
            for r in rows
            if r["tax_available"] and r["rebalance_residual_drift"]
            for account_type, drift in r["rebalance_residual_drift"].items()
            if drift > 0.001
        ]
        if drift_rows:
            st.markdown("**Rebalancing residual drift**")
            st.caption(
                "Accounts/years where rebalancing could NOT fully correct to the target weight — "
                "either a held ticker has no known price, or it's protected by the rebalance band "
                "above (MODEL_WIRING.md §7: 'measured, not assumed away')."
            )
            drift_df = pd.DataFrame(drift_rows)
            st.dataframe(
                drift_df,
                column_config={"Residual drift (%)": st.column_config.NumberColumn("Residual drift (%)", format="%.1f%%")},
                width="stretch",
                hide_index=True,
            )

        # The real, evolving ledger (Step 3) -- each row's own ending_lots/portfolio_value already
        # reflect that year's price roll-forward, reinvestment, and new contribution purchases; no
        # separate recomputation needed here (see modules/projection.py's project_multi_year). The
        # "Portfolio value over time" chart that used to live here moved to the main chart section
        # (2026-08-14) — see _total_wealth_chart — so it's visible without expanding anything; only
        # the end-of-projection share/value breakdown table stays here as supporting detail.
        portfolio_rows = [r for r in rows if r["tax_available"] and r["ending_lots"] is not None]
        if portfolio_rows:
            final_lots = portfolio_rows[-1]["ending_lots"]
            final_prices = portfolio_rows[-1]["ending_prices_by_ticker"]
            shares_by_account_ticker: dict[tuple[str, str], float] = {}
            for lot in final_lots:
                key = (lot["account_type"], lot["ticker"])
                shares_by_account_ticker[key] = shares_by_account_ticker.get(key, 0.0) + lot["shares"]
            if shares_by_account_ticker:
                st.markdown(f"**Total shares as of {portfolio_rows[-1]['year']} (end of projection)**")
                lot_summary_df = pd.DataFrame(
                    [
                        {
                            "Account type": account_type,
                            "Ticker": ticker,
                            "Total shares": shares,
                            "Value at that year's price": shares * final_prices.get(ticker, 0.0),
                        }
                        for (account_type, ticker), shares in sorted(shares_by_account_ticker.items())
                    ]
                )
                st.dataframe(
                    lot_summary_df,
                    column_config={
                        "Total shares": st.column_config.NumberColumn("Total shares", format="%.2f"),
                        "Value at that year's price": st.column_config.NumberColumn(
                            "Value at that year's price", format="$%.0f"
                        ),
                    },
                    width="stretch",
                    hide_index=True,
                )
