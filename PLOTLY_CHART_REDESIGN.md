# Charts → Plotly redesign — spec for Claude Code

**Prepared by:** Claude (Cowork), 2026-08-14, from a read-only review of `ui/projection_tab.py`
(the only file in this codebase using charts today) plus Anthropic's internal data-visualization
method (form → color → marks → interaction, six computable color checks instead of eyeballing).
Every color pairing recommended below was run through that method's validator
(`validate_palette.js`, OKLab-based CVD simulation) — exact results are quoted inline so nothing
here is a guess.

**`plotly>=5.22` is already in `requirements.txt`** but unused anywhere in the app — this is an
adoption, not a new dependency. `altair` is used **only** in `ui/projection_tab.py` (confirmed:
`grep -rl "import altair" ui/` returns one file) — once this migration is done, remove the
`import altair as alt` line, `_financial_model_theme`/`register_altair_theme`, and (after
confirming no other near-term work needs it) the `altair` line from `requirements.txt`.

**Relationship to the other two specs in this folder:** `RETIREMENT_REPORTING_AUDIT.md` changes
*which rows* feed the gross/net chart (cut at the withdrawal boundary) and adds a new "Total
retirement income" chart; `ADJUSTED_WEALTH_REDESIGN.md` adds a second series to the portfolio-value
chart and three new headline stats. This document is orthogonal to both — it's how every chart
(current and newly-specified) should be *built and styled*, regardless of which document's data
changes land first. Implement the Plotly patterns below against whatever fields currently exist;
swap in the new fields as the other two specs land. If none of that work has happened yet, this
document still fully applies to the four charts that exist today.

---

## 1. The chart inventory (current + newly-specified, all in scope)

| # | Chart | Status | Source rows |
|---|---|---|---|
| 1 | **Total wealth over time** | Exists today (`ui/projection_tab.py` lines 1103-1114) as one buried, single-series line inside a collapsed expander. Becomes 2 series per `ADJUSTED_WEALTH_REDESIGN.md` §5.1. | `portfolio_value` per year, both scenarios |
| 2 | **Gross vs. net income** | Exists today (`_gross_net_chart`, lines 104-141), full horizon. Becomes pre-retirement-only per `RETIREMENT_REPORTING_AUDIT.md` §2.4. | `gross_income`, `net_income` |
| 3 | **Total retirement income** | New, from `RETIREMENT_REPORTING_AUDIT.md` §2.4 point 2 | `total_withdrawal_income`, `net_retirement_income` |
| 4 | **Expenses** | Exists today (`_expense_chart`, lines 144-157) | `gross_expense` |
| 5 | **Profit** | Exists today (`_profit_chart`, lines 160-177) | `profit` |

**Chart #1 is promoted from a collapsed expander to the top of the page, directly under the stat
metrics from `ADJUSTED_WEALTH_REDESIGN.md`.** This resolves that document's own open question
(§5.1's "should this move out of the expander") — given this request is specifically about making
charts bigger and easier to read at a glance, leaving the most important one collapsed and small
contradicts that goal. It becomes the page's headline/hero chart.

---

## 2. Layout: one chart per row, not `st.columns(3)`

Replace the current 3-across layout (`ui/projection_tab.py` lines 880-886, `chart_col1/2/3 =
st.columns(3, ...)`) with a plain vertical stack, full container width, in this order:

1. Total wealth over time (hero — tallest)
2. Gross vs. net income
3. Total retirement income
4. Expenses
5. Profit

```python
st.plotly_chart(wealth_fig, use_container_width=True, config=PLOTLY_CONFIG)
st.plotly_chart(gross_net_fig, use_container_width=True, config=PLOTLY_CONFIG)
st.plotly_chart(retirement_income_fig, use_container_width=True, config=PLOTLY_CONFIG)
st.plotly_chart(expense_fig, use_container_width=True, config=PLOTLY_CONFIG)
st.plotly_chart(profit_fig, use_container_width=True, config=PLOTLY_CONFIG)
```

(Check the installed Streamlit version's current preferred kwarg — `requirements.txt` pins
`streamlit>=1.38`; `use_container_width` is the long-stable API, some newer versions also accept
`width="stretch"` the same way `st.dataframe` already uses elsewhere in this codebase. Either is
fine — pick whichever the installed version's own deprecation warnings (if any) point to.)

**Height and width — "much taller y-axes," concretely:** the current charts are 280px tall
(`_gross_net_chart` etc., `.properties(height=280, ...)`). Set:

- Chart #1 (Total wealth — hero): **`height=600`**
- Charts #2-#5: **`height=480`**
- All: full container width (no fixed pixel width — let Streamlit's column/page width drive it)

"Taller y-axis" should mean **more vertical room for the trend to unfold**, not a y-axis range that
extends past the data (that would compress the actual trend smaller, the opposite of readable).
Concretely: `rangemode="tozero"` on every magnitude chart (wealth, gross/net, expenses — dollar
figures should never start their axis above $0, or the visual slope lies about the rate of change)
plus explicit headroom above the max value so line markers/end-labels never crowd the plot's top
edge:

```python
y_max = max(v for v in values if v is not None)
fig.update_yaxes(rangemode="tozero", range=[0, y_max * 1.15])
```

`Profit` (and `funding_gap`, if that chart is ever added) can go negative — let Plotly autorange
that one symmetrically rather than forcing `tozero`.

---

## 3. Color — computed, not eyeballed

Every categorical color below was run through the validated palette's own checker
(OKLab ΔE under simulated protanopia/deuteranopia, plus a normal-vision floor and a 3:1 contrast
check against a `#fcfcfb` light surface). Quoting the actual results so nothing here is asserted
without evidence:

| Pairing (chart) | Result |
|---|---|
| Blue `#2a78d6` + Aqua `#1baf7a` (Gross/Net) | **PASS** — CVD ΔE 23.1 (protan), normal-vision ΔE 24.0. Aqua alone is 2.74:1 contrast on the light surface (below the 3:1 target) — **ships with direct end-labels as the required relief**, not color alone. |
| Blue `#2a78d6` + Violet `#4a3aa7` (Wealth: base vs. no-income/no-expense) | **PASS**, cleanest pairing tested — CVD ΔE 13.0 (deutan), normal-vision ΔE 16.3, **both colors clear 3:1 contrast** (no relief needed). |
| Green `#008300` + Red `#e34948` (Profit: positive/negative) | CVD ΔE 7.2 (protan) — in the "floor" band (6-8), **legal only with secondary encoding**. This chart already has one: the zero baseline makes positive/negative unambiguous from *position* alone (area above vs. below the line), independent of hue — color here is a reinforcing cue, not the only signal. Keep green/red (matches the near-universal "green=gain, red=loss" finance convention) but make sure the zero-line and a signed value label are always present, never rely on the fill color by itself. |

Final assignments (light mode; dark-mode hex alternates in parentheses, for if/when this app adds a
dark theme — lower priority, implement light mode first):

| Role | Hex (light) | Hex (dark) |
|---|---|---|
| Gross (income or withdrawal) | `#2a78d6` | `#3987e5` |
| Net (income, withdrawal, or the "after" line generally) | `#1baf7a` | `#199e70` |
| Expenses | `#eb6834` | `#d95926` |
| Profit — positive | `#008300` | `#008300` |
| Profit — negative | `#e34948` | `#e66767` |
| Wealth — base case (with income & expenses) | `#2a78d6` (same as Gross — it's the "as-planned" scenario) | `#3987e5` |
| Wealth — no income/no expense baseline | `#4a3aa7` | `#9085e9` |
| Gridlines (hairline, solid) | `#e1e0d9` | `#2c2c2a` |
| Axis/baseline | `#c3c2b7` | `#383835` |
| Chart surface | `#fcfcfb` | `#1a1a19` |
| Primary text (title) | `#0b0b0b` | `#ffffff` |
| Secondary/muted text (axis labels, captions) | `#52514e` / `#898781` | `#c3c2b7` / `#898781` |

**Fix while migrating**: the current Altair theme sets `gridDash: [2, 2]` (dashed gridlines) on
*every* axis — this is a real anti-pattern (dashing reads as "projection" or "threshold," and
routine gridlines aren't either). Ship **solid** hairline gridlines everywhere except the profit
chart's zero-reference line, which is legitimately a threshold (positive vs. negative), not a
routine gridline — keep that one dashed on purpose, and only that one.

---

## 4. Interaction — Plotly's unified hover is a near-perfect fit for the method's spec

The skill's own interaction spec (crosshair finds X, one tooltip lists every series, values lead
labels) is close to Plotly's **`hovermode="x unified"`** out of the box:

```python
fig.update_layout(
    hovermode="x unified",
    hoverlabel=dict(bgcolor="#fcfcfb", bordercolor="#e1e0d9", font_size=13,
                     font_family="-apple-system, 'Segoe UI', Roboto, sans-serif"),
)
fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor",
                  spikethickness=1, spikedash="solid", spikecolor="#c3c2b7")
```

Per-trace hover template, value-first (bold), series name secondary — set on every `go.Scatter`:

```python
hovertemplate="<b>$%{y:,.0f}</b><extra>Gross income</extra>"
```

(The `<extra>...</extra>` tag is what supplies the series name in unified-hover mode without also
drawing Plotly's default secondary "trace box" — cleaner than the default.)

**Modebar**: keep it, but don't let it float in the way — `config=dict(displayModeBar="hover",
displaylogo=False)`. Users can still zoom/pan/download-as-PNG on hover over the chart, but it's not
visually present at rest. Define this once as a module-level `PLOTLY_CONFIG` dict, reused by every
chart on the tab.

---

## 5. Direct labels — the "at a glance" request, concretely

The skill's rule: label the endpoint or the one series that's the point, never every point. Two
things this buys you, per the user's explicit ask for charts that "deliver the key points
quickly":

1. **An end-of-line annotation on every series**, showing the final year's value, so the headline
   number is visible without hovering at all:

```python
fig.add_annotation(
    x=rows[-1]["year"], y=rows[-1]["portfolio_value"],
    text=f"${rows[-1]['portfolio_value']:,.0f}",
    showarrow=False, xanchor="left", xshift=8,
    font=dict(size=13, color="#0b0b0b", family="-apple-system, 'Segoe UI', Roboto, sans-serif"),
)
```

Do this for every series on every chart (2 annotations on the 2-series charts, 1 on the
single-series ones). Keep the axis format for these labels **exact whole dollars**
(`$1,234,567`, not `$1.2M`) — this repo has an explicit, already-documented convention for that
(`ui/projection_tab.py`'s `_DOLLAR_AXIS_FORMAT` comment: *"a prior session's spec had asked for
SI-suffix axis ticks (`$120k`); this explicit instruction supersedes that for exact whole-dollar
ticks instead"*) — **don't reintroduce compact/SI formatting anywhere, including these new
annotations**, without checking with the user first; it was a deliberate prior decision, not an
oversight.

2. **A one-line, data-driven caption above each chart**, stating the headline takeaway in plain
   language — this is what actually delivers "the key point at a glance," faster than reading the
   chart at all:

```python
st.caption(
    f"Grows from **${today_portfolio_value:,.0f}** today to a projected "
    f"**${rows[-1]['portfolio_value']:,.0f}** by {rows[-1]['year']}, assuming income and spending "
    "continue as planned."
)
st.plotly_chart(wealth_fig, ...)
```

Compute these from the same row data feeding the chart — no new modeling, just a plain-language
restatement of the endpoint(s) already being plotted.

---

## 6. Per-chart build notes

### 6.1 Total wealth over time (hero chart)

Two `go.Scatter` traces (`mode="lines"`, `line=dict(width=2, color=...)`), one per scenario, both
on the SAME y-axis (dollars) — **never** a second y-axis; if a future addition needs a
differently-scaled series, that's a separate chart or small multiples, not a dual axis (see
`RETIREMENT_REPORTING_AUDIT.md`'s own review — no dual-axis chart exists in this app today, keep it
that way). End-of-line annotations on both series (§5). Consider a light vertical reference line +
annotation at `withdrawal_start_date`'s year, labeled "Withdrawals begin" — both scenarios pivot
behavior there, and marking it removes a moment of reader confusion ("why does the slope change
here?").

```python
fig = go.Figure()
fig.add_trace(go.Scatter(
    x=[r["year"] for r in base_rows], y=[r["portfolio_value"] for r in base_rows],
    mode="lines", name="With income & expenses (as planned)",
    line=dict(width=2, color="#2a78d6"),
    hovertemplate="<b>$%{y:,.0f}</b><extra>With income & expenses</extra>",
))
fig.add_trace(go.Scatter(
    x=[r["year"] for r in baseline_rows], y=[r["portfolio_value"] for r in baseline_rows],
    mode="lines", name="No income or expenses from today",
    line=dict(width=2, color="#4a3aa7"),
    hovertemplate="<b>$%{y:,.0f}</b><extra>No income or expenses</extra>",
))
fig.add_vline(x=withdrawal_start_date.year, line=dict(width=1, color="#c3c2b7", dash="dot"))
fig.add_annotation(x=withdrawal_start_date.year, y=1.02, yref="paper", showarrow=False,
                    text="Withdrawals begin", font=dict(size=11, color="#898781"))
fig.update_layout(height=600, hovermode="x unified", legend=dict(orientation="h", y=-0.15))
fig.update_yaxes(title="Dollars (real)", tickformat="$,.0f", rangemode="tozero",
                  range=[0, max_wealth * 1.15], gridcolor="#e1e0d9", gridwidth=1)
fig.update_xaxes(title="Year", tickformat="d", gridcolor="#e1e0d9", gridwidth=1)
```

### 6.2 Gross vs. net income

Two lines (Gross = blue, Net = aqua) plus the existing shaded "tax burden" band between them. The
band is a **derived reference region, not a third series** — don't give it its own hue or legend
entry (reusing "Expenses" orange for it, as the current Altair version does, is a leftover from
before this document and should stop — it implies a relationship to expenses that isn't real). Fill
it with a neutral, muted wash instead, and label it directly rather than via the legend:

```python
fig.add_trace(go.Scatter(
    x=years + years[::-1], y=net_vals + gross_vals[::-1],
    fill="toself", fillcolor="rgba(137,135,129,0.12)", line=dict(width=0),
    name="Tax", hoverinfo="skip", showlegend=False,
))
# then the Gross (blue) and Net (aqua) lines on top, each with hovertemplate + end-label as above
```

Rows: only pre-retirement (`year < withdrawal_start_date.year`, per
`RETIREMENT_REPORTING_AUDIT.md` §2.4 point 1) once that spec lands — use the full `rows` set until
it does.

### 6.3 Total retirement income (new chart)

Same Gross/Net color mapping and construction pattern as §6.2 (blue = `total_withdrawal_income`,
aqua = `net_retirement_income`), no tax-burden band needed here since
`RETIREMENT_REPORTING_AUDIT.md` already defines `retirement_taxes_paid` as its own reportable
figure — a shaded gap is optional polish, not required. Only renders once that spec's new fields
exist; until then this chart doesn't exist yet (nothing to build against).

### 6.4 Expenses

Single line or a light-opacity area (`fill="tozeroy"`, ~10% opacity per the mark spec), orange, no
legend box needed (single series — the chart title already says what's plotted, per
marks-and-anatomy's "a single series needs no legend box" rule). One end-label, one caption.

### 6.5 Profit

Filled area, `fill="tozeroy"`, split by sign (`green` above zero, `red` below) — Plotly doesn't do
conditional per-point fill color natively in one trace; build it as **two traces sharing the same
x-domain**, one clipped to positive values (`None` where negative) and one clipped to negative
values (`None` where positive), each with its own fill/color, both starting at 0:

```python
pos = [p if p is not None and p >= 0 else None for p in profits]
neg = [p if p is not None and p < 0 else None for p in profits]
fig.add_trace(go.Scatter(x=years, y=pos, fill="tozeroy", fillcolor="rgba(0,131,0,0.25)",
                          line=dict(width=2, color="#008300"), name="Profit", showlegend=False))
fig.add_trace(go.Scatter(x=years, y=neg, fill="tozeroy", fillcolor="rgba(227,73,72,0.25)",
                          line=dict(width=2, color="#e34948"), showlegend=False))
fig.add_hline(y=0, line=dict(width=1, color="#999999", dash="dash"))  # intentionally dashed — a
    # real threshold, not a routine gridline; see §3's callout
```

No legend needed (position above/below zero already carries the identity; a legend swatch for
"positive"/"negative" would be redundant with the axis itself). Signed value in the hover template
and end-label (`+$16,745` / `-$11,868`) so the number itself carries the sign redundantly with color
and position — the "secondary encoding" this pairing's CVD floor-band result requires (§3).

---

## 7. Accessibility / robustness checklist

- **Table view**: every chart already has a full data table below it on this tab (`table_df` /
  the new per-scenario tables from the other two specs) — keep those; they're each chart's
  required WCAG-clean twin. Don't let the chart be the only way to read a value.
- **Legend**: present for every 2-series chart (wealth, gross/net, retirement income), absent for
  the two single-series charts (expenses, profit — a legend box with one swatch would just restate
  the title).
- **Empty/None guards**: several fields are `None` pre-portfolio-activation or pre-tax-availability
  (`gross_income`, `profit`, etc. — see `modules/projection.py`'s own row-schema notes). Filter
  `None` out of each trace's `y` list before plotting (Plotly will otherwise render a gap, which is
  usually fine, but the end-label/annotation logic in §5 needs a non-`None` last value — guard for
  "no computable rows at all" and fall back to `st.caption("No projected years — check tax bracket
  data and planning horizon.")`, matching this tab's existing empty-state pattern (line 788-790).
- **Keyboard/focus parity**: Plotly's built-in hover already responds to touch and works reasonably
  with keyboard focus in modern browsers; no extra work needed here beyond what `hovermode="x
  unified"` already provides.

---

## 8. Testing / verification checklist

- Visual: run the app, open the Projection tab, confirm all five charts render full-width, one per
  row, at the specified heights, with solid (not dashed) routine gridlines and a dashed zero-line
  only on the Profit chart.
- Confirm end-of-line annotations don't clip against the right edge of the plot (Plotly's default
  margin may need `fig.update_layout(margin=dict(r=80))` or similar to leave room for the
  `xanchor="left", xshift=8` annotations in §5 — check visually, adjust the right margin if labels
  crowd or overflow).
- Confirm the y-axis headroom (`range=[0, max*1.15]`) doesn't clip the highest end-label on any
  chart — if it does, increase the multiplier.
- Confirm dollar formatting stays exact (`$1,234,567`), not compact, on every axis, tooltip, and
  annotation — this is an explicit prior decision in this codebase, not a style choice to revisit
  here.
- Confirm the tax-burden band (§6.2) has no legend entry and doesn't visually read as a third
  data series.

---

## Summary of concrete next actions for Claude Code

1. Add a module-level `PLOTLY_CONFIG` dict and the color/format constants from §3 to
   `ui/projection_tab.py` (or a small new `ui/_chart_style.py` shared module, if other tabs are
   ever expected to chart anything — Claude Code's call, keep it simple if not).
2. Rewrite `_gross_net_chart`, `_expense_chart`, `_profit_chart`, and the buried portfolio-value
   chart as Plotly `go.Figure` builders per §6.1-§6.2, §6.4-§6.5, using whatever row fields exist
   today (swap in the retirement-boundary/new-field changes from the other two specs as they land).
3. Add the new Total retirement income chart (§6.3) once `RETIREMENT_REPORTING_AUDIT.md`'s fields
   exist.
4. Replace `st.columns(3)` with the vertical one-per-row layout in §2, promote the wealth chart out
   of its expander to the top of the page (§1).
5. Add the one-line data-driven captions (§5) above each chart.
6. Remove `import altair as alt`, `_financial_model_theme`, `register_altair_theme`, and (after
   confirming nothing else needs it) `altair` from `requirements.txt`.
7. Run the checklist in §8; update `NEXT.md` when done, same working agreement as before.
