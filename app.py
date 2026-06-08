"""GEIP Global Energy Dashboard.

Reads data/demo_facts.parquet. Uses only owid_energy historical rows for the
main view. With the "Forward view" toggle, extends charts with EIA IEO 2023
Reference-scenario projections (2025–2050), clearly distinguished from history.

Layout:
  Sidebar  — Geography selector + Forward view toggle
  1. Growth vs Scale bubble   — 5-yr historical CAGR (+ IEO 2035 hollow overlay)
  2. Electricity mix donut    — latest year, % labels
  3. Stacked area over time   — 1985–present (+ IEO projection shading to 2050)
  4. Reconciliation caption

Run:
    streamlit run app.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_PARQUET = Path(__file__).parent / "data" / "demo_facts.parquet"

_COLORS: dict[str, str] = {
    "coal":            "#3d3d3d",
    "gas":             "#c9a048",
    "oil":             "#8b3a3a",
    "nuclear":         "#e8c914",
    "hydro":           "#1a6faf",
    "wind":            "#5ba5e0",
    "solar":           "#f5a623",
    "other_renewable": "#57a65a",
}

_LABELS: dict[str, str] = {
    "coal":            "Coal",
    "gas":             "Gas",
    "oil":             "Oil",
    "nuclear":         "Nuclear",
    "hydro":           "Hydro",
    "wind":            "Wind",
    "solar":           "Solar",
    "other_renewable": "Other renewables",
}

_STACK_ORDER = ["coal", "oil", "gas", "nuclear", "hydro", "other_renewable", "wind", "solar"]

_PRIORITY_GEOS = ["World", "China", "United States", "India", "European Union (27)"]

_CAP_AGGREGATES = {"World", "EU", "G20", "G7", "OECD"}
_MIN_CAP_GW = 0.1

# IEO geography names → OWID dropdown names.
# Only exact single-country / "Total World" matches are mapped; IEO broad
# regions (Americas, Western Europe, …) have no 1:1 OWID counterpart.
_IEO_TO_OWID: dict[str, str] = {
    "Total World":           "World",
    "United States":         "United States",
    "China":                 "China",
    "India":                 "India",
    "Japan":                 "Japan",
    "South Korea":           "South Korea",
    "Canada":                "Canada",
    "Mexico":                "Mexico",
    "Brazil":                "Brazil",
    "Russia":                "Russia",
}
_OWID_TO_IEO = {v: k for k, v in _IEO_TO_OWID.items()}

# IEO projection target year for the forward-view bubble overlay.
_PROJ_TARGET_YEAR = 2035

_CHART_CONFIG: dict = {"responsive": True, "displayModeBar": False}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rgba(hex_color: str, alpha: float) -> str:
    """Convert a #rrggbb hex color to an rgba() string for Plotly fills."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

@st.cache_data
def _load() -> pd.DataFrame:
    df = pd.read_parquet(_PARQUET)
    owid = df[
        (df["source_id"] == "owid_energy") &
        (~df["is_projection"]) &
        (df["metric_family"] == "electricity")
    ].copy()
    owid["year"] = pd.to_datetime(owid["period"]).dt.year
    return owid[owid["year"] >= 1986]


@st.cache_data
def _load_projections() -> pd.DataFrame:
    """IEO 2023 Reference-scenario electricity facts, remapped to OWID geography names."""
    df = pd.read_parquet(_PARQUET)
    ieo = df[
        (df["source_id"] == "eia_ieo") &
        (df["is_projection"]) &
        (df["scenario"] == "reference") &
        (df["metric_family"] == "electricity")
    ].copy()
    if ieo.empty:
        return ieo
    ieo["geography"] = ieo["geography"].map(_IEO_TO_OWID)
    ieo = ieo.dropna(subset=["geography"])
    ieo["year"] = pd.to_datetime(ieo["period"]).dt.year
    return ieo


@st.cache_data
def _load_capacity() -> pd.DataFrame:
    """Ember installed capacity (wind + solar, GW, monthly)."""
    df = pd.read_parquet(_PARQUET)
    cap = df[
        (df["source_id"] == "ember") &
        (df["metric_family"] == "capacity") &
        (df["energy_type"].isin(["wind", "solar"])) &
        (~df["is_projection"])
    ].copy()
    if not cap.empty:
        cap["period_dt"] = pd.to_datetime(cap["period"])
    return cap


@st.cache_data
def _load_ember_gen() -> pd.DataFrame:
    """Ember electricity generation (wind + solar, TWh, monthly)."""
    df = pd.read_parquet(_PARQUET)
    gen = df[
        (df["source_id"] == "ember") &
        (df["metric_family"] == "electricity") &
        (df["energy_type"].isin(["wind", "solar"])) &
        (~df["is_projection"])
    ].copy()
    if not gen.empty:
        gen["period_dt"] = pd.to_datetime(gen["period"])
        gen["year"] = gen["period_dt"].dt.year
    return gen


def _geo_options(df: pd.DataFrame) -> list[str]:
    present = set(df["geography"].unique())
    priority = [g for g in _PRIORITY_GEOS if g in present]
    rest = sorted(present - set(_PRIORITY_GEOS))
    return priority + rest


def _cap_geo_options() -> list[str]:
    cap = _load_capacity()
    if cap.empty:
        return []
    present = set(cap["geography"].unique())
    aggs = sorted(present & _CAP_AGGREGATES)
    countries = sorted(present - _CAP_AGGREGATES)
    return aggs + countries


def _format_cap_geo(geo: str) -> str:
    return f"◆ {geo}" if geo in _CAP_AGGREGATES else geo


# ---------------------------------------------------------------------------
# Chart 1 — Growth vs Scale bubble
# ---------------------------------------------------------------------------

def _bubble(
    df: pd.DataFrame,
    latest_year: int,
    proj_df: pd.DataFrame | None = None,
) -> go.Figure:
    prior_year = latest_year - 5
    latest = df[df["year"] == latest_year].set_index("energy_type")["value"].to_dict()
    prior  = df[df["year"] == prior_year].set_index("energy_type")["value"].to_dict()

    labels, x_cagr, y_twh, colors = [], [], [], []
    for et in sorted(latest):
        current = latest[et]
        p = prior.get(et)
        if not p or p <= 0:
            continue
        cagr = ((current / p) ** 0.2 - 1) * 100
        labels.append(_LABELS.get(et, et))
        x_cagr.append(round(cagr, 2))
        y_twh.append(round(current, 1))
        colors.append(_COLORS.get(et, "#aaaaaa"))

    if not y_twh:
        return _empty_fig("Not enough data for this geography")

    # Projected values for bubble overlay (hollow circles).
    proj_labels, proj_x, proj_y, proj_colors = [], [], [], []
    if proj_df is not None and not proj_df.empty:
        proj_vals = (
            proj_df[proj_df["year"] == _PROJ_TARGET_YEAR]
            .set_index("energy_type")["value"]
            .to_dict()
        )
        n_years = _PROJ_TARGET_YEAR - latest_year
        if n_years > 0:
            for et in sorted(proj_vals):
                curr = latest.get(et)
                pv = proj_vals[et]
                if not curr or curr <= 0:
                    continue
                cagr = ((pv / curr) ** (1 / n_years) - 1) * 100
                proj_labels.append(_LABELS.get(et, et))
                proj_x.append(round(cagr, 2))
                proj_y.append(round(pv, 1))
                proj_colors.append(_COLORS.get(et, "#aaaaaa"))

    # Shared sizeref so historical and projected bubbles are comparable in area.
    all_twh = y_twh + proj_y
    sizeref = 2 * max(all_twh) / (80 ** 2)

    fig = go.Figure()

    # Historical (solid fill)
    fig.add_trace(go.Scatter(
        x=x_cagr, y=y_twh,
        mode="markers+text",
        text=labels,
        textposition="top center",
        textfont=dict(size=10, color="#333333"),
        marker=dict(
            size=y_twh, sizemode="area", sizeref=sizeref,
            color=colors, opacity=0.82,
            line=dict(width=1.5, color="white"),
        ),
        hovertemplate=(
            "<b>%{text}</b><br>"
            f"Generation ({latest_year}): %{{y:,.0f}} TWh<br>"
            "5-yr CAGR: %{x:+.1f}%<extra></extra>"
        ),
        showlegend=False,
    ))

    # Projected (hollow overlay)
    if proj_y:
        fig.add_trace(go.Scatter(
            x=proj_x, y=proj_y,
            mode="markers",
            marker=dict(
                size=proj_y, sizemode="area", sizeref=sizeref,
                color=[_rgba(c, 0.0) for c in proj_colors],
                line=dict(width=2.5, color=proj_colors),
            ),
            hovertemplate=(
                "<b>%{text}</b> · IEO Reference<br>"
                f"Projected ({_PROJ_TARGET_YEAR}): %{{y:,.0f}} TWh<br>"
                f"CAGR {latest_year}→{_PROJ_TARGET_YEAR}: %{{x:+.1f}}%<extra></extra>"
            ),
            text=proj_labels,
            showlegend=False,
        ))

    fig.add_vline(
        x=0, line_width=1, line_dash="dot", line_color="#bbbbbb",
        annotation_text="0% growth",
        annotation_position="top right",
        annotation_font=dict(size=10, color="#999999"),
    )
    fig.update_layout(
        xaxis=dict(title="CAGR (%)", ticksuffix="%",
                   showgrid=True, gridcolor="#ebebeb", zeroline=False),
        yaxis=dict(title="Generation (TWh)", tickformat=",",
                   showgrid=True, gridcolor="#ebebeb"),
        hovermode="closest",
        autosize=True,
        margin=dict(l=10, r=10, t=40, b=40),
        height=420,
        plot_bgcolor="#f9f9f9",
        paper_bgcolor="#ffffff",
    )
    return fig


# ---------------------------------------------------------------------------
# Chart 2 — Latest-year donut
# ---------------------------------------------------------------------------

def _donut(df: pd.DataFrame, year: int) -> go.Figure:
    sub = df[df["year"] == year].sort_values("value", ascending=False)
    total_twh = sub["value"].sum()
    centre = (
        f"<b>{year}</b><br>{total_twh / 1_000:.1f} PWh"
        if total_twh >= 1_000 else
        f"<b>{year}</b><br>{total_twh:,.0f} TWh"
    )
    fig = go.Figure(go.Pie(
        labels=[_LABELS.get(et, et) for et in sub["energy_type"]],
        values=sub["value"].tolist(),
        hole=0.52,
        marker_colors=[_COLORS.get(et, "#aaaaaa") for et in sub["energy_type"]],
        texttemplate="%{label}<br><b>%{percent:.1%}</b>",
        textposition="outside",
        textfont_size=11,
        hovertemplate="<b>%{label}</b><br>%{value:,.0f} TWh · %{percent:.1%}<extra></extra>",
        sort=False,
        direction="clockwise",
    ))
    fig.update_layout(
        annotations=[{"text": centre, "x": 0.5, "y": 0.5,
                       "font": {"size": 20}, "showarrow": False}],
        showlegend=False,
        autosize=True,
        margin=dict(l=20, r=20, t=40, b=20),
        height=480,
        paper_bgcolor="#ffffff",
    )
    return fig


# ---------------------------------------------------------------------------
# Chart 3 — Stacked area over time
# ---------------------------------------------------------------------------

def _stacked_area(
    df: pd.DataFrame,
    proj_df: pd.DataFrame | None = None,
) -> go.Figure:
    year_counts = df.groupby("year")["energy_type"].nunique()
    valid_years = year_counts[year_counts >= 3].index
    if valid_years.empty:
        return _empty_fig("Not enough data for this geography")
    start_year = int(valid_years.min())

    pivot = (
        df[df["year"] >= start_year]
        .pivot_table(index="year", columns="energy_type", values="value", aggfunc="sum")
        .fillna(0)
    )
    last_hist_year = int(pivot.index.max())

    fig = go.Figure()

    # Historical stacked area
    for et in _STACK_ORDER:
        if et not in pivot.columns:
            continue
        color = _COLORS.get(et, "#aaaaaa")
        fig.add_trace(go.Scatter(
            x=pivot.index, y=pivot[et].round(1),
            name=_LABELS.get(et, et),
            mode="lines", stackgroup="one",
            line=dict(width=0, color=color),
            fillcolor=color,
            hovertemplate=f"<b>{_LABELS.get(et, et)}</b>  %{{y:,.0f}} TWh<extra></extra>",
        ))

    # Projection extension
    if proj_df is not None and not proj_df.empty:
        proj_pivot = (
            proj_df[proj_df["year"] >= last_hist_year]
            .pivot_table(index="year", columns="energy_type", values="value", aggfunc="sum")
            .fillna(0)
        )
        if not proj_pivot.empty:
            proj_max_year = int(proj_pivot.index.max())

            # Light shading over the projection period
            fig.add_vrect(
                x0=last_hist_year + 0.5, x1=proj_max_year + 0.5,
                fillcolor="#eeeeee", opacity=0.55,
                layer="below", line_width=0,
            )

            # Dashed stacked area continuation (lighter fills, dotted boundary)
            for et in _STACK_ORDER:
                if et not in proj_pivot.columns:
                    continue
                color = _COLORS.get(et, "#aaaaaa")
                fig.add_trace(go.Scatter(
                    x=proj_pivot.index, y=proj_pivot[et].round(1),
                    name=_LABELS.get(et, et),
                    mode="lines", stackgroup="proj",
                    line=dict(width=1, dash="dot", color=color),
                    fillcolor=_rgba(color, 0.40),
                    showlegend=False,
                    hovertemplate=(
                        f"<b>{_LABELS.get(et, et)}</b> (IEO Reference)"
                        "  %{y:,.0f} TWh<extra></extra>"
                    ),
                ))

            # "Not a forecast" annotation centred in the shaded zone
            fig.add_annotation(
                x=(last_hist_year + proj_max_year) / 2,
                y=0.97, yref="paper",
                text="EIA projection — not GEIP forecast",
                showarrow=False,
                font=dict(size=10, color="#777777"),
                bgcolor="rgba(255,255,255,0.75)",
                borderpad=3,
            )

    fig.update_layout(
        xaxis=dict(title=None, showgrid=False),
        yaxis=dict(title="TWh", showgrid=True, gridcolor="#e4e4e4", tickformat=","),
        hovermode="x unified",
        legend=dict(orientation="h", x=0, y=-0.15,
                    xanchor="left", yanchor="top", font_size=12),
        autosize=True,
        margin=dict(l=40, r=10, t=10, b=80),
        height=520,
        plot_bgcolor="#f9f9f9",
        paper_bgcolor="#ffffff",
    )
    return fig


# ---------------------------------------------------------------------------
# Chart 4 — Renewable capacity trend
# ---------------------------------------------------------------------------

def _cap_trend(cap_df: pd.DataFrame) -> go.Figure:
    """Monthly stacked area of wind + solar installed capacity (GW)."""
    sub = cap_df[cap_df["value"] >= _MIN_CAP_GW].copy()
    if sub.empty:
        return _empty_fig("No capacity data for this geography")

    pivot = (
        sub.pivot_table(index="period_dt", columns="energy_type", values="value", aggfunc="sum")
        .fillna(0)
        .sort_index()
    )

    fig = go.Figure()
    for et in ["wind", "solar"]:
        if et not in pivot.columns:
            continue
        color = _COLORS[et]
        fig.add_trace(go.Scatter(
            x=pivot.index, y=pivot[et].round(2),
            name=_LABELS[et],
            mode="lines", stackgroup="cap",
            line=dict(width=0, color=color),
            fillcolor=color,
            hovertemplate=f"<b>{_LABELS[et]}</b>  %{{y:,.1f}} GW<extra></extra>",
        ))

    fig.update_layout(
        xaxis=dict(title=None, showgrid=False),
        yaxis=dict(title="GW", showgrid=True, gridcolor="#e4e4e4", tickformat=","),
        hovermode="x unified",
        legend=dict(orientation="h", x=0, y=1.08, xanchor="left", font_size=13),
        autosize=True,
        margin=dict(l=40, r=10, t=40, b=10),
        height=360,
        plot_bgcolor="#f9f9f9",
        paper_bgcolor="#ffffff",
    )
    return fig


def _cap_factors(cap_df: pd.DataFrame, gen_df: pd.DataFrame) -> dict[str, float]:
    """
    Returns {energy_type: capacity_factor_%} for the latest year that has both
    capacity and generation data.

    CF = (annual_gen_TWh × 1000 / 8760) / annual_avg_cap_GW × 100
    """
    if cap_df.empty or gen_df.empty:
        return {}

    cap_df = cap_df.copy()
    cap_df["year"] = cap_df["period_dt"].dt.year

    common_years = set(cap_df["year"].unique()) & set(gen_df["year"].unique())
    if not common_years:
        return {}

    year = max(common_years)
    cap_y = cap_df[cap_df["year"] == year]
    gen_y = gen_df[gen_df["year"] == year]

    result: dict[str, float] = {}
    for et in ("wind", "solar"):
        cap_et = cap_y[cap_y["energy_type"] == et]
        gen_et = gen_y[gen_y["energy_type"] == et]
        if cap_et.empty or gen_et.empty:
            continue
        avg_cap_gw = cap_et["value"].mean()
        if avg_cap_gw < _MIN_CAP_GW:
            continue
        # monthly gen values in TWh; sum → annual total; convert to average GW
        annual_gen_twh = gen_et["value"].sum()
        avg_gen_gw = annual_gen_twh * 1000 / 8760
        result[et] = round(avg_gen_gw / avg_cap_gw * 100, 1)

    return result


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _empty_fig(message: str) -> go.Figure:
    return go.Figure().update_layout(
        height=200,
        paper_bgcolor="#ffffff",
        annotations=[{"text": message, "showarrow": False,
                       "x": 0.5, "y": 0.5, "xref": "paper", "yref": "paper",
                       "font": {"size": 14, "color": "#888888"}}],
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="GEIP — Global Energy",
        page_icon="⚡",
        layout="wide",
    )

    # --- Sidebar controls ---
    with st.sidebar:
        st.header("Controls")
        geo = st.selectbox("Geography", options=_geo_options(_load()), index=0)

        st.divider()
        forward = st.toggle("Forward view (IEO 2023 projections)", value=False)
        if forward:
            st.caption(
                f"Hollow bubbles and shaded area show EIA IEO 2023 "
                f"Reference-scenario projections to 2050. "
                f"Bubble overlay compares current → {_PROJ_TARGET_YEAR}."
            )
            if geo not in _OWID_TO_IEO:
                st.warning(
                    f"IEO covers broad world regions, not all countries. "
                    f"No IEO projection data available for '{geo}'."
                )

        st.divider()
        cap_geos = _cap_geo_options()
        if cap_geos:
            default_cap = cap_geos.index("World") if "World" in cap_geos else 0
            cap_geo = st.selectbox(
                "Capacity geography",
                options=cap_geos,
                index=default_cap,
                format_func=_format_cap_geo,
            )
            if cap_geo in _CAP_AGGREGATES:
                st.caption("◆ = aggregate region. Compare only with other aggregates.")
        else:
            cap_geo = None
            st.caption("No Ember capacity data available.")

    hist_df = _load()
    geo_df = hist_df[hist_df["geography"] == geo]

    tab_dash, tab_arch = st.tabs(["Dashboard", "Architecture"])

    with tab_dash:
        if geo_df.empty:
            st.title("Global Electricity Mix")
            st.warning(f"No OWID electricity data found for '{geo}'.")
        else:
            latest_year = int(geo_df["year"].max())

            # Load projection data for this geography (empty df if no IEO coverage)
            proj_geo_df: pd.DataFrame | None = None
            if forward:
                all_proj = _load_projections()
                if not all_proj.empty and geo in _OWID_TO_IEO:
                    proj_geo_df = all_proj[all_proj["geography"] == geo]
                    if proj_geo_df.empty:
                        proj_geo_df = None

            st.title("Global Electricity Mix")

            st.divider()

            # 1. Bubble
            bubble_title = f"Growth vs scale · {latest_year - 5}–{latest_year}"
            if forward and proj_geo_df is not None:
                bubble_title += f" + IEO to {_PROJ_TARGET_YEAR}"
            st.subheader(bubble_title)
            st.caption(
                "Bubble size and y-axis both show current generation (TWh). "
                "Right = fastest growing · Up = largest output."
                + (f"  Hollow circles = IEO Reference {_PROJ_TARGET_YEAR}." if forward and proj_geo_df is not None else "")
            )
            st.plotly_chart(_bubble(geo_df, latest_year, proj_geo_df), use_container_width=True, config=_CHART_CONFIG)

            st.divider()

            # 2. Donut
            st.subheader(f"Electricity mix · {geo} · {latest_year}")
            st.plotly_chart(_donut(geo_df, latest_year), use_container_width=True, config=_CHART_CONFIG)

            st.divider()

            # 3. Stacked area
            area_title = f"Generation by source · {geo}"
            if forward and proj_geo_df is not None:
                area_title += " + IEO to 2050"
            st.subheader(area_title)
            st.plotly_chart(_stacked_area(geo_df, proj_geo_df), use_container_width=True, config=_CHART_CONFIG)

            # 4. Renewable Capacity
            st.divider()
            st.subheader("Wind & Solar — the bulk of new power-sector investment")
            st.caption(
                "Ember capacity data · wind & solar only · monthly, 2016–2026 · 30 geographies. "
                "◆ marks aggregate regions (World, EU, G20, G7, OECD) — "
                "compare only within the same tier."
            )

            if cap_geo:
                all_cap = _load_capacity()
                all_gen = _load_ember_gen()
                cap_df = all_cap[all_cap["geography"] == cap_geo]
                gen_df = all_gen[all_gen["geography"] == cap_geo]

                if cap_df.empty:
                    st.info(f"No capacity data for '{_format_cap_geo(cap_geo)}'.")
                else:
                    st.plotly_chart(_cap_trend(cap_df), use_container_width=True, config=_CHART_CONFIG)

                    cf = _cap_factors(cap_df, gen_df)
                    if cf:
                        cf_year = int(cap_df["period_dt"].dt.year.max())
                        for et, pct in cf.items():
                            st.metric(
                                label=f"{_LABELS.get(et, et)} capacity factor",
                                value=f"{pct:.1f}%",
                            )
                        st.caption(
                            f"Capacity factor = annual generation ÷ (installed GW × 8,760 h). "
                            f"Latest year with both capacity and generation data: {cf_year}."
                        )
            else:
                st.info("Select a capacity geography in the sidebar.")

            # 5. Caption
            st.caption(
                "Source: Our World in Data energy dataset (reconciliation spine). "
                "Cross-checked against EIA International and Ember: 95.9% of 80,059 "
                "overlapping series agree within ±5% (GEIP reconciliation run, June 2026)."
            )

    with tab_arch:
        st.graphviz_chart("""
digraph geip {
    rankdir=TB
    node [fontname="sans-serif" fontsize=11 style=filled fillcolor="#f0f4f8" color="#9aacbd"]
    edge [color="#6b7f93"]

    owid  [label="Our World in Data\n(spine)" fillcolor="#d4edda" color="#3a7d44"]
    eia   [label="U.S. EIA"]
    ember [label="Ember"]

    owid_c  [label="Connector\n(fetch→normalize→validate)"]
    eia_c   [label="Connector\n(fetch→normalize→validate)"]
    ember_c [label="Connector\n(fetch→normalize→validate)"]

    schema [label="Common Schema\n(vintage-aware fact store)" shape=cylinder]
    recon  [label="Reconciliation Engine\n(compares sources,\nflags discrepancies)"]
    cache  [label="Cached Snapshot\n(parquet)" shape=note]
    dash   [label="Dashboard\n(this app)" shape=box fillcolor="#cce5ff" color="#0066cc"]

    owid  -> owid_c
    eia   -> eia_c
    ember -> ember_c

    owid_c  -> schema
    eia_c   -> schema
    ember_c -> schema

    schema -> recon -> cache -> dash
}
""")
        st.markdown("""
## How this tool is built

### Data sources

GEIP draws from three free, authoritative public sources.

**Our World in Data (OWID)** is the harmonized historical backbone. Its
electricity dataset aggregates and cross-checks country-level figures from
primary statistical agencies worldwide. Every chart is anchored to OWID, and
it serves as the canonical reference against which all other sources are
compared.

**The U.S. Energy Information Administration (EIA)** supplies international
energy data and long-range projections. The EIA's International Energy Outlook
provides the forward-view scenarios shown when the "Forward view" toggle is on.

**Ember** publishes near-real-time global power generation and installed
capacity figures — monthly wind and solar data for roughly 30 geographies —
enabling the capacity-factor analysis in the dashboard.

---

### Pipeline

Each source has a dedicated connector that fetches, normalizes, and validates
the data before it enters the system. The connectors share a common interface,
so new sources can be added without touching existing logic.

Every data point is stored as a self-describing record that carries its source,
geography, energy type, metric, unit, and publication vintage. This means every
number on screen is fully traceable to its origin — nothing is computed without
a clear audit trail.

---

### Reconciliation

Where sources overlap on the same country, year, and energy type, a
reconciliation engine compares them series by series. Discrepancies above a
threshold are flagged rather than resolved silently — the system does not pick a
winner and hide the disagreement.

A magnitude floor prevents the engine from raising alarms about percentage swings
on near-zero values (for example, a small country's solar output doubling from
0.01 TWh to 0.02 TWh is a 100% change that carries no practical significance).
OWID is always the spine; EIA and Ember are compared against it, never the
reverse.

---

### Key design principles

- **Units before arithmetic.** Values are normalized to consistent units before
  any calculation. Primary energy and electricity are tracked in separate
  families and never combined.
- **Projections stay separate.** Every projected data point is flagged.
  Projections never appear inside historical totals or aggregates.
- **Append-only, vintage-aware storage.** When a source revises a number, the
  system creates a new record rather than overwriting the old one. The full
  revision history is preserved.

---

### Current state

The app reads from a recent static snapshot of the pipeline's output for fast,
reliable loading. The full pipeline — live connectors, automated ingestion, and
the reconciliation engine — exists and runs locally. The deployed version uses a
cached extract of that output, updated periodically as sources publish new data.
        """)


if __name__ == "__main__":
    main()
