import sqlite3
from pathlib import Path

import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, dcc, html, register_page
from dash.exceptions import PreventUpdate


register_page(__name__, path="/FI-Screener", name="Fixed Income Screener")

BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "data" / "fixed_income.db"

INDEX_CATEGORIES = ("Inflation", "Fixed Rate", "Floating Rate")
MINIMUM_MTD_VOLUME_BY_STEP = {
    0: 0,
    1: 500_000,
    2: 1_000_000,
    3: 10_000_000,
}

SCREENER_COLUMNS = [
    {
        "headerName": "Issuer",
        "field": "issuer",
        "pinned": "left",
        "lockPinned": True,
        "minWidth": 205,
        "flex": 1.5,
        "cellStyle": {"fontWeight": 700, "color": "#0f172a"},
    },
    {"headerName": "Sector", "field": "sector", "minWidth": 155, "flex": 1.15},
    {
        "headerName": "Rating",
        "field": "rating",
        "cellRenderer": "RatingBadge",
        "cellStyle": {"display": "flex", "alignItems": "center"},
        "width": 95,
    },
    {
        "headerName": "Ticker",
        "field": "ticker",
        "minWidth": 120,
        "flex": 0.9,
    },
    {"headerName": "Index", "field": "index", "width": 90},
    {
        "headerName": "Duration",
        "field": "duration",
        "width": 100,
        "type": "numericColumn",
        "valueFormatter": {
            "function": "params.value == null ? '\u2014' : d3.format('.2f')(params.value)"
        },
    },
    {
        "headerName": "Maturity Date",
        "field": "maturity_date",
        "minWidth": 125,
        "flex": 0.9,
    },
    {
        "headerName": "Reference Yield",
        "field": "reference_yield_pct",
        "width": 125,
        "type": "numericColumn",
        "valueFormatter": {
            "function": "params.value == null ? '\u2014' : d3.format('.2f')(params.value) + '%'"
        },
    },
    {
        "headerName": "Spread (bps)",
        "marryChildren": True,
        "children": [
            {
                "headerName": "Spread MTM",
                "field": "spread_mtm_bps",
                "width": 112,
                "type": "numericColumn",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : d3.format('.1f')(params.value)"
                },
            },
            {
                "headerName": "Spread Buy",
                "field": "spread_buy_bps",
                "width": 110,
                "type": "numericColumn",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : d3.format('.1f')(params.value)"
                },
            },
            {
                "headerName": "Spread Sell",
                "field": "spread_sell_bps",
                "width": 110,
                "type": "numericColumn",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : d3.format('.1f')(params.value)"
                },
            },
        ],
    },
    {
        "headerName": "Spread Change (bps)",
        "marryChildren": True,
        "children": [
            {
                "headerName": "1D Change",
                "field": "change_1d_bps",
                "width": 105,
                "type": "numericColumn",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : (params.value > 0 ? '+' : '') + d3.format('.1f')(params.value)"
                },
                "cellClassRules": {
                    "screener-move-wider": "params.value > 0",
                    "screener-move-tighter": "params.value < 0",
                    "screener-move-flat": "params.value === 0",
                },
            },
            {
                "headerName": "1W Change",
                "field": "change_1w_bps",
                "width": 105,
                "type": "numericColumn",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : (params.value > 0 ? '+' : '') + d3.format('.1f')(params.value)"
                },
                "cellClassRules": {
                    "screener-move-wider": "params.value > 0",
                    "screener-move-tighter": "params.value < 0",
                    "screener-move-flat": "params.value === 0",
                },
            },
            {
                "headerName": "1M Change",
                "field": "change_1m_bps",
                "width": 105,
                "type": "numericColumn",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : (params.value > 0 ? '+' : '') + d3.format('.1f')(params.value)"
                },
                "cellClassRules": {
                    "screener-move-wider": "params.value > 0",
                    "screener-move-tighter": "params.value < 0",
                    "screener-move-flat": "params.value === 0",
                },
            },
        ],
    },
    {
        "headerName": "Traded Volume",
        "marryChildren": True,
        "children": [
            {
                "headerName": "DLY",
                "field": "volume_dly",
                "width": 112,
                "type": "numericColumn",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : 'R$ ' + d3.format('.3s')(params.value)"
                },
            },
            {
                "headerName": "WTD",
                "field": "volume_wtd",
                "width": 112,
                "type": "numericColumn",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : 'R$ ' + d3.format('.3s')(params.value)"
                },
            },
            {
                "headerName": "MTD",
                "field": "volume_mtd",
                "width": 112,
                "type": "numericColumn",
                "sort": "desc",
                "valueFormatter": {
                    "function": "params.value == null ? '\u2014' : 'R$ ' + d3.format('.3s')(params.value)"
                },
                "cellStyle": {"fontWeight": 700, "color": "#1d4ed8"},
            },
        ],
    },
    {
        "headerName": "Last Trade Date",
        "field": "last_trade_date",
        "width": 125,
    },
]


def _empty_snapshot():
    return pd.DataFrame(
        columns=[
            "issuer",
            "sector",
            "rating",
            "ticker",
            "index",
            "index_category",
            "asset_count",
            "duration",
            "maturity_date",
            "reference_yield_pct",
            "spread_mtm_bps",
            "spread_buy_bps",
            "spread_sell_bps",
            "change_1d_bps",
            "change_1w_bps",
            "change_1m_bps",
            "volume_dly",
            "volume_wtd",
            "volume_mtd",
            "last_trade_date",
        ]
    )


def _load_screener_source():
    if not DATABASE_PATH.exists():
        return pd.DataFrame()

    market_query = """
        SELECT
            DT_REF,
            ID_SECURITY,
            NM_SECURITY,
            VL_SPREAD_BID,
            VL_SPREAD_ASK,
            VL_SPREAD_REF,
            VL_BASE_YIELD,
            VL_DURATION,
            VL_TRADED,
            DT_INPUT
        FROM FIXED_INCOME_MARKET_DATA
    """
    classification_query = """
        SELECT
            ID_SECURITY,
            NM_SECURITY,
            NM_ISSUER,
            NM_INDEX,
            NM_RATING,
            NM_SECTOR,
            DT_MATURITY
        FROM FIXED_INCOME_SECURITY_CLASSIFICATION
    """

    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            market_df = pd.read_sql(market_query, connection)
            classification_df = pd.read_sql(classification_query, connection)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()

    if market_df.empty:
        return market_df

    market_df["DT_REF"] = pd.to_datetime(market_df["DT_REF"], errors="coerce")
    market_df["DT_INPUT"] = pd.to_datetime(market_df["DT_INPUT"], errors="coerce")
    market_df = market_df.dropna(subset=["DT_REF", "NM_SECURITY"]).copy()
    for column_name in [
        "VL_SPREAD_BID",
        "VL_SPREAD_ASK",
        "VL_SPREAD_REF",
        "VL_BASE_YIELD",
        "VL_DURATION",
        "VL_TRADED",
    ]:
        market_df[column_name] = pd.to_numeric(
            market_df[column_name], errors="coerce"
        )

    if classification_df.empty:
        for column_name in ["NM_ISSUER", "NM_INDEX", "NM_RATING", "NM_SECTOR"]:
            market_df[column_name] = ""
        market_df["DT_MATURITY"] = pd.NaT
        return market_df

    classification_df["DT_MATURITY"] = pd.to_datetime(
        classification_df["DT_MATURITY"], errors="coerce"
    )
    classification_df = classification_df.drop_duplicates(
        subset=["NM_SECURITY"], keep="last"
    )
    classification_df = classification_df[
        [
            "NM_SECURITY",
            "NM_ISSUER",
            "NM_INDEX",
            "NM_RATING",
            "NM_SECTOR",
            "DT_MATURITY",
        ]
    ]
    return market_df.merge(classification_df, on="NM_SECURITY", how="left")


def _latest_security_rows(source_df, as_of, strict_before=False):
    if source_df.empty:
        return source_df.copy()
    if strict_before:
        eligible = source_df.loc[source_df["DT_REF"] < as_of].copy()
    else:
        eligible = source_df.loc[source_df["DT_REF"] <= as_of].copy()
    if eligible.empty:
        return eligible
    return (
        eligible.sort_values(["DT_REF", "DT_INPUT"])
        .drop_duplicates(subset=["NM_SECURITY"], keep="last")
        .copy()
    )


def _weighted_average(frame, value_column):
    values = pd.to_numeric(frame[value_column], errors="coerce")
    weights = pd.to_numeric(frame["VL_TRADED"], errors="coerce").fillna(0.0)
    valid = values.notna()
    if not valid.any():
        return None
    positive_weight = valid & weights.gt(0)
    if positive_weight.any():
        return float(
            (values.loc[positive_weight] * weights.loc[positive_weight]).sum()
            / weights.loc[positive_weight].sum()
        )
    return float(values.loc[valid].mean())


def _representative_label(values, fallback="--"):
    cleaned = [
        str(value).strip()
        for value in values
        if pd.notna(value) and str(value).strip()
    ]
    if not cleaned:
        return fallback
    counts = pd.Series(cleaned).value_counts()
    return str(counts.index[0])


def _index_category(index_name):
    normalized = str(index_name or "").strip().upper().replace("-", "")
    if any(token in normalized for token in ("IPCA", "IGPM", "INFLATION")):
        return "Inflation"
    if any(token in normalized for token in ("PRE", "FIXED")):
        return "Fixed Rate"
    if any(token in normalized for token in ("CDI", "SELIC", "FLOAT")):
        return "Floating Rate"
    return "--"


def _format_maturity_dates(values):
    maturity_dates = pd.to_datetime(pd.Series(values), errors="coerce").dropna()
    if maturity_dates.empty:
        return "--"
    unique_dates = sorted(maturity_dates.dt.normalize().unique())
    return ", ".join(pd.Timestamp(value).strftime("%d %b %Y") for value in unique_dates)


def _issuer_reference_snapshot(source_df, as_of, strict_before=False):
    latest_df = _latest_security_rows(source_df, as_of, strict_before)
    if latest_df.empty:
        return {}
    snapshot = {}
    for issuer, issuer_df in latest_df.groupby("issuer", dropna=False):
        snapshot[issuer] = _weighted_average(issuer_df, "VL_SPREAD_REF")
    return snapshot


def build_issuer_snapshot():
    source_df = _load_screener_source()
    if source_df.empty:
        return _empty_snapshot(), None

    source_df["NM_SECURITY"] = (
        source_df["NM_SECURITY"].fillna("").astype(str).str.strip()
    )
    source_df["issuer"] = source_df["NM_ISSUER"].fillna("").astype(str).str.strip()
    source_df.loc[source_df["issuer"].eq(""), "issuer"] = source_df["NM_SECURITY"]
    source_df["VL_TRADED"] = source_df["VL_TRADED"].fillna(0.0)

    reference_date = source_df["DT_REF"].max().normalize()
    latest_df = _latest_security_rows(source_df, reference_date)
    if latest_df.empty:
        return _empty_snapshot(), reference_date

    prior_day_reference = _issuer_reference_snapshot(
        source_df, reference_date, strict_before=True
    )
    prior_week_reference = _issuer_reference_snapshot(
        source_df, reference_date - pd.DateOffset(weeks=1)
    )
    prior_month_reference = _issuer_reference_snapshot(
        source_df, reference_date - pd.DateOffset(months=1)
    )

    week_start = reference_date - pd.Timedelta(days=reference_date.weekday())
    month_start = reference_date.replace(day=1)
    daily_df = source_df.loc[source_df["DT_REF"].eq(reference_date)]
    weekly_df = source_df.loc[source_df["DT_REF"].between(week_start, reference_date)]
    monthly_df = source_df.loc[
        source_df["DT_REF"].between(month_start, reference_date)
    ]

    def volume_by_issuer(frame):
        if frame.empty:
            return {}
        return frame.groupby("issuer")["VL_TRADED"].sum(min_count=1).to_dict()

    daily_volume = volume_by_issuer(daily_df)
    weekly_volume = volume_by_issuer(weekly_df)
    monthly_volume = volume_by_issuer(monthly_df)
    last_trade_dates = (
        source_df.loc[source_df["VL_TRADED"].gt(0)]
        .groupby("issuer")["DT_REF"]
        .max()
        .to_dict()
    )

    rows = []
    for issuer, issuer_df in latest_df.groupby("issuer", sort=True):
        bid = _weighted_average(issuer_df, "VL_SPREAD_BID")
        ask = _weighted_average(issuer_df, "VL_SPREAD_ASK")
        reference = _weighted_average(issuer_df, "VL_SPREAD_REF")
        reference_yield = _weighted_average(issuer_df, "VL_BASE_YIELD")
        duration = _weighted_average(issuer_df, "VL_DURATION")
        prior_day = prior_day_reference.get(issuer)
        prior_week = prior_week_reference.get(issuer)
        prior_month = prior_month_reference.get(issuer)
        codes = sorted(
            code
            for code in issuer_df["NM_SECURITY"].dropna().astype(str).str.strip().unique()
            if code
        )
        last_trade = last_trade_dates.get(issuer)
        index_name = _representative_label(issuer_df["NM_INDEX"])

        rows.append(
            {
                "issuer": issuer,
                "sector": _representative_label(issuer_df["NM_SECTOR"]),
                "rating": _representative_label(
                    issuer_df["NM_RATING"], fallback="Unrated"
                ),
                "index": index_name,
                "index_category": _index_category(index_name),
                "asset_count": len(codes),
                "ticker": ", ".join(codes),
                "duration": duration,
                "maturity_date": _format_maturity_dates(
                    issuer_df["DT_MATURITY"]
                ),
                "reference_yield_pct": (
                    reference_yield * 100 if reference_yield is not None else None
                ),
                "spread_mtm_bps": (
                    reference * 10_000 if reference is not None else None
                ),
                "spread_buy_bps": bid * 10_000 if bid is not None else None,
                "spread_sell_bps": ask * 10_000 if ask is not None else None,
                "change_1d_bps": (
                    (reference - prior_day) * 10_000
                    if reference is not None and prior_day is not None
                    else None
                ),
                "change_1w_bps": (
                    (reference - prior_week) * 10_000
                    if reference is not None and prior_week is not None
                    else None
                ),
                "change_1m_bps": (
                    (reference - prior_month) * 10_000
                    if reference is not None and prior_month is not None
                    else None
                ),
                "volume_dly": float(daily_volume.get(issuer, 0.0) or 0.0),
                "volume_wtd": float(weekly_volume.get(issuer, 0.0) or 0.0),
                "volume_mtd": float(monthly_volume.get(issuer, 0.0) or 0.0),
                "last_trade_date": (
                    pd.Timestamp(last_trade).strftime("%d %b %Y")
                    if last_trade is not None and pd.notna(last_trade)
                    else "--"
                ),
            }
        )

    snapshot_df = pd.DataFrame(rows)
    if snapshot_df.empty:
        return _empty_snapshot(), reference_date
    snapshot_df = snapshot_df.sort_values(
        ["volume_mtd", "issuer"], ascending=[False, True]
    ).reset_index(drop=True)
    return snapshot_df, reference_date


def _filter_snapshot(
    snapshot_df,
    search_text=None,
    sectors=None,
    index_category_value="Inflation",
    minimum_mtd=0,
):
    if snapshot_df.empty:
        return snapshot_df.copy()
    filtered_df = snapshot_df.copy()
    if search_text:
        needle = str(search_text).strip().casefold()
        haystack = (
            filtered_df[["issuer", "ticker", "sector"]]
            .fillna("")
            .astype(str)
            .agg(" ".join, axis=1)
            .str.casefold()
        )
        filtered_df = filtered_df.loc[haystack.str.contains(needle, regex=False)]
    if sectors:
        filtered_df = filtered_df.loc[filtered_df["sector"].isin(sectors)]
    selected_category = str(index_category_value or "Inflation")
    if selected_category not in INDEX_CATEGORIES:
        selected_category = "Inflation"
    filtered_df = filtered_df.loc[
        filtered_df["index_category"].eq(selected_category)
    ]
    minimum_mtd = MINIMUM_MTD_VOLUME_BY_STEP.get(int(minimum_mtd or 0), 0)
    filtered_df = filtered_df.loc[filtered_df["volume_mtd"].ge(minimum_mtd)]
    return filtered_df.reset_index(drop=True)


def _empty_figure(message):
    figure = go.Figure()
    figure.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"size": 13, "color": "#64748b"},
    )
    figure.update_layout(
        height=300,
        margin={"l": 20, "r": 20, "t": 10, "b": 20},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return figure


def build_volume_figure(snapshot_df):
    if snapshot_df.empty:
        return _empty_figure("No issuers match the selected filters")
    chart_df = snapshot_df.nlargest(10, "volume_mtd").sort_values("volume_mtd")
    figure = go.Figure(
        go.Bar(
            x=chart_df["volume_mtd"],
            y=chart_df["issuer"],
            orientation="h",
            marker={"color": "#2563eb", "cornerradius": 4},
            customdata=chart_df[["volume_wtd", "volume_dly"]],
            hovertemplate=(
                "<b>%{y}</b><br>MTD: R$ %{x:,.0f}"
                "<br>WTD: R$ %{customdata[0]:,.0f}"
                "<br>DLY: R$ %{customdata[1]:,.0f}<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        height=300,
        margin={"l": 18, "r": 18, "t": 8, "b": 32},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        showlegend=False,
        font={"family": "Arial, sans-serif", "color": "#334155", "size": 11},
        hoverlabel={"bgcolor": "#0f172a", "font_color": "#ffffff"},
        bargap=0.38,
    )
    figure.update_xaxes(
        showgrid=True,
        gridcolor="#eef2f7",
        zeroline=False,
        tickprefix="R$ ",
        tickformat="~s",
        title=None,
    )
    figure.update_yaxes(showgrid=False, title=None, automargin=True)
    return figure


def build_movement_figure(snapshot_df):
    chart_df = snapshot_df.dropna(subset=["change_1m_bps"]).copy()
    if chart_df.empty:
        return _empty_figure("One-month comparison is not available")
    chart_df = chart_df.reindex(
        chart_df["change_1m_bps"].abs().sort_values(ascending=False).index
    ).head(10)
    chart_df = chart_df.sort_values("change_1m_bps")
    colors = [
        "#16a34a" if value < -0.05 else "#dc2626" if value > 0.05 else "#94a3b8"
        for value in chart_df["change_1m_bps"]
    ]
    labels = [
        f"{value:+.1f}" if abs(value) >= 0.05 else "0.0"
        for value in chart_df["change_1m_bps"]
    ]
    figure = go.Figure(
        go.Bar(
            x=chart_df["change_1m_bps"],
            y=chart_df["issuer"],
            orientation="h",
            marker={"color": colors, "cornerradius": 4},
            text=labels,
            textposition="outside",
            cliponaxis=False,
            hovertemplate="<b>%{y}</b><br>1M move: %{x:+.1f} bps<extra></extra>",
        )
    )
    figure.update_layout(
        height=300,
        margin={"l": 18, "r": 40, "t": 8, "b": 32},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        showlegend=False,
        font={"family": "Arial, sans-serif", "color": "#334155", "size": 11},
        hoverlabel={"bgcolor": "#0f172a", "font_color": "#ffffff"},
        bargap=0.38,
    )
    figure.update_xaxes(
        showgrid=True,
        gridcolor="#eef2f7",
        zeroline=True,
        zerolinecolor="#94a3b8",
        zerolinewidth=1,
        ticksuffix=" bps",
        title=None,
    )
    figure.update_yaxes(showgrid=False, title=None, automargin=True)
    return figure


def _options(values):
    return [
        {"label": value, "value": value}
        for value in sorted({str(value) for value in values if value and value != "--"})
    ]


def _filter_options():
    snapshot_df, _ = build_issuer_snapshot()
    if snapshot_df.empty:
        return []
    return _options(snapshot_df["sector"])


SECTOR_OPTIONS = _filter_options()

layout = dbc.Container(
    fluid=True,
    className="page-container screener-page",
    children=[
        dcc.Interval(id="screener-initial-load", interval=250, max_intervals=1),
        dcc.Download(id="screener-download"),
        html.Div(
            className="screener-hero",
            children=[
                html.Div(
                    children=[
                        html.Div("FIXED INCOME", className="screener-eyebrow"),
                        html.H1("Issuer Screener", className="screener-title"),
                        html.P(
                            "Compare spread levels, momentum, and trading activity across issuers in one view.",
                            className="screener-subtitle",
                        ),
                    ]
                ),
                html.Div(
                    className="screener-hero-meta",
                    children=[
                        html.Div(
                            id="screener-reference-date",
                            className="screener-reference-date",
                        ),
                        html.Div(
                            className="screener-actions",
                            children=[
                                html.Button(
                                    "Refresh",
                                    id="screener-refresh",
                                    className="screener-button secondary",
                                    type="button",
                                ),
                                html.Button(
                                    "Export CSV",
                                    id="screener-export",
                                    className="screener-button primary",
                                    type="button",
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            className="screener-filter-panel",
            children=[
                html.Div(
                    className="screener-filter screener-search-filter",
                    children=[
                        html.Label("Search", htmlFor="screener-search"),
                        dmc.TextInput(
                            id="screener-search",
                            value="",
                            debounce=True,
                            placeholder="Issuer, security, or sector",
                            h=38,
                            radius="md",
                            className="screener-mantine-input",
                        ),
                    ],
                ),
                html.Div(
                    className="screener-filter",
                    children=[
                        html.Label("Sector"),
                        dmc.MultiSelect(
                            id="screener-sector",
                            data=SECTOR_OPTIONS,
                            value=[],
                            placeholder="All sectors",
                            searchable=True,
                            clearable=True,
                            h=38,
                            radius="md",
                            className="screener-mantine-input",
                        ),
                    ],
                ),
                html.Div(
                    className="screener-filter screener-index-control-filter",
                    children=[
                        html.Label("Index"),
                        dmc.SegmentedControl(
                            id="screener-index-category",
                            data=list(INDEX_CATEGORIES),
                            value="Inflation",
                            fullWidth=True,
                            color="gray",
                            size="sm",
                            radius="md",
                            h=38,
                            transitionDuration=150,
                            withItemsBorders=False,
                            className="screener-index-segmented",
                        ),
                    ],
                ),
                html.Div(
                    className="screener-filter screener-volume-slider-filter",
                    children=[
                        html.Label("Minimum MTD volume"),
                        html.Div(
                            className="screener-volume-slider-shell",
                            children=dmc.Slider(
                                id="screener-min-volume",
                                min=0,
                                max=3,
                                step=1,
                                value=0,
                                marks=[
                                    {"value": 0, "label": "0"},
                                    {"value": 1, "label": "500K"},
                                    {"value": 2, "label": "1MM"},
                                    {"value": 3, "label": "10MM"},
                                ],
                                restrictToMarks=True,
                                label=None,
                                updatemode="mouseup",
                                color="blue",
                                size="sm",
                                thumbSize=16,
                                className="screener-volume-slider",
                            ),
                        ),
                    ],
                ),
                dmc.Button(
                    "Clear filters",
                    id="screener-reset",
                    variant="subtle",
                    color="gray",
                    radius="md",
                    h=38,
                    className="screener-reset-button",
                ),
            ],
        ),
        html.Div(
            className="screener-table-card",
            children=[
                html.Div(
                    className="screener-section-header",
                    children=[
                        html.Div(
                            children=[
                                html.H2("Issuer Comparison", className="screener-section-title"),
                                html.Div(
                                    "Volume-weighted reference yield and spreads using each security's latest observation.",
                                    className="screener-section-subtitle",
                                ),
                            ]
                        ),
                        html.Div(
                            id="screener-row-count", className="screener-row-count"
                        ),
                    ],
                ),
                dcc.Loading(
                    type="dot",
                    color="#2563eb",
                    children=dag.AgGrid(
                        id="issuer-screener-grid",
                        rowData=[],
                        columnDefs=SCREENER_COLUMNS,
                        defaultColDef={
                            "sortable": True,
                            "filter": True,
                            "resizable": True,
                            "suppressMovable": True,
                        },
                        dashGridOptions={
                            "animateRows": False,
                            "tooltipShowDelay": 250,
                            "ensureDomOrder": True,
                            "rowHeight": 46,
                            "headerHeight": 38,
                            "groupHeaderHeight": 34,
                        },
                        className="ag-theme-alpine ag-grid screener-grid",
                        style={"height": "510px", "width": "100%"},
                    ),
                ),
                html.Div(
                    className="screener-table-note",
                    children=[
                        html.Span("1D, 1W, and 1M changes compare Spread MTM with the closest available prior observation."),
                        html.Span("Negative movement indicates spread tightening; positive movement indicates widening."),
                    ],
                ),
            ],
        ),
        html.Div(
            className="screener-analysis-grid",
            children=[
                html.Div(
                    className="screener-analysis-card",
                    children=[
                        html.Div(
                            className="screener-chart-header",
                            children=[
                                html.Div(
                                    "MTD Liquidity Leaders",
                                    className="screener-chart-title",
                                ),
                                html.Div(
                                    "Top issuers by accumulated traded volume",
                                    className="screener-chart-subtitle",
                                ),
                            ],
                        ),
                        dcc.Graph(
                            id="screener-volume-chart",
                            figure=_empty_figure("Loading market data…"),
                            config={"displayModeBar": False, "responsive": True},
                            className="screener-chart",
                        ),
                    ],
                ),
                html.Div(
                    className="screener-analysis-card",
                    children=[
                        html.Div(
                            className="screener-chart-header",
                            children=[
                                html.Div(
                                    "One-Month Spread Movers",
                                    className="screener-chart-title",
                                ),
                                html.Div(
                                    "Reference spread change in basis points",
                                    className="screener-chart-subtitle",
                                ),
                            ],
                        ),
                        dcc.Graph(
                            id="screener-movement-chart",
                            figure=_empty_figure("Loading market data…"),
                            config={"displayModeBar": False, "responsive": True},
                            className="screener-chart",
                        ),
                    ],
                ),
            ],
        ),
    ],
)


@callback(
    Output("screener-sector", "data"),
    Input("screener-initial-load", "n_intervals"),
    Input("screener-refresh", "n_clicks"),
)
def refresh_screener_filter_options(_initial_load, _refresh_clicks):
    return _filter_options()


@callback(
    Output("screener-reference-date", "children"),
    Output("screener-row-count", "children"),
    Output("issuer-screener-grid", "rowData"),
    Output("screener-volume-chart", "figure"),
    Output("screener-movement-chart", "figure"),
    Input("screener-initial-load", "n_intervals"),
    Input("screener-refresh", "n_clicks"),
    Input("screener-search", "value"),
    Input("screener-sector", "value"),
    Input("screener-index-category", "value"),
    Input("screener-min-volume", "value"),
)
def refresh_screener(
    _initial_load,
    _refresh_clicks,
    search_text,
    sectors,
    index_category_value,
    minimum_mtd,
):
    snapshot_df, reference_date = build_issuer_snapshot()
    filtered_df = _filter_snapshot(
        snapshot_df,
        search_text,
        sectors,
        index_category_value,
        minimum_mtd,
    )

    if reference_date is None:
        reference_label = "No market data available"
    else:
        reference_label = f"Data through {reference_date.strftime('%d %b %Y')}"

    total_issuers = len(snapshot_df)
    visible_issuers = len(filtered_df)
    row_count = f"Showing {visible_issuers} of {total_issuers} issuers"

    serializable_df = filtered_df.where(pd.notna(filtered_df), None)
    return (
        reference_label,
        row_count,
        serializable_df.to_dict("records"),
        build_volume_figure(filtered_df),
        build_movement_figure(filtered_df),
    )


@callback(
    Output("screener-search", "value"),
    Output("screener-sector", "value"),
    Output("screener-index-category", "value"),
    Output("screener-min-volume", "value"),
    Input("screener-reset", "n_clicks"),
    prevent_initial_call=True,
)
def reset_screener_filters(_clicks):
    return "", [], "Inflation", 0


@callback(
    Output("screener-download", "data"),
    Input("screener-export", "n_clicks"),
    State("screener-search", "value"),
    State("screener-sector", "value"),
    State("screener-index-category", "value"),
    State("screener-min-volume", "value"),
    prevent_initial_call=True,
)
def export_screener_csv(
    clicks,
    search_text,
    sectors,
    index_category_value,
    minimum_mtd,
):
    if not clicks:
        raise PreventUpdate
    snapshot_df, reference_date = build_issuer_snapshot()
    export_df = _filter_snapshot(
        snapshot_df,
        search_text,
        sectors,
        index_category_value,
        minimum_mtd,
    ).rename(
        columns={
            "issuer": "Issuer",
            "sector": "Sector",
            "rating": "Rating",
            "ticker": "Ticker",
            "index": "Index",
            "duration": "Duration",
            "maturity_date": "Maturity Date",
            "reference_yield_pct": "Reference Yield (%)",
            "spread_mtm_bps": "Spread MTM (bps)",
            "spread_buy_bps": "Spread Buy (bps)",
            "spread_sell_bps": "Spread Sell (bps)",
            "change_1d_bps": "1D Change (bps)",
            "change_1w_bps": "1W Change (bps)",
            "change_1m_bps": "1M Change (bps)",
            "volume_dly": "DLY Volume (R$)",
            "volume_wtd": "WTD Volume (R$)",
            "volume_mtd": "MTD Volume (R$)",
            "last_trade_date": "Last Trade Date",
        }
    )
    export_df = export_df[
        [
            "Issuer",
            "Sector",
            "Rating",
            "Ticker",
            "Index",
            "Duration",
            "Maturity Date",
            "Reference Yield (%)",
            "Spread MTM (bps)",
            "Spread Buy (bps)",
            "Spread Sell (bps)",
            "1D Change (bps)",
            "1W Change (bps)",
            "1M Change (bps)",
            "DLY Volume (R$)",
            "WTD Volume (R$)",
            "MTD Volume (R$)",
            "Last Trade Date",
        ]
    ]
    date_suffix = (
        reference_date.strftime("%Y-%m-%d") if reference_date is not None else "latest"
    )
    return dcc.send_data_frame(
        export_df.to_csv,
        f"fixed_income_issuer_screener_{date_suffix}.csv",
        index=False,
    )
