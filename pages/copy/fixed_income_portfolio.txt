import json
import math
import sqlite3
from pathlib import Path

from dash import (
    ClientsideFunction,
    Input,
    Output,
    callback,
    clientside_callback,
    dcc,
    html,
    register_page,
)
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd

register_page(__name__, path="/portfolio", name="Fixed Income Portfolio")

BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "data" / "fixed_income.db"

SNAPSHOT_COLUMNS = [
    "DT_REF",
    "ID_SECURITY",
    "ID_CLIENT",
    "NM_SECURITY",
    "VL_EXPOSURE",
    "QTY",
    "NM_ISSUER",
    "NM_INDEX",
    "DT_MATURITY",
    "NM_INSTRUMENT_TYPE",
    "NM_RATING",
    "NM_SECTOR",
    "VL_DURATION",
    "VL_BASE_YIELD",
    "VL_SPREAD_REF",
    "VL_COMPOUNDED_YIELD",
    "bucket",
]

PORTFOLIO_TABLE_QUERIES = {
    "portfolio_position": """
        SELECT
            DT_REF,
            ID_SECURITY,
            ID_CLIENT,
            NM_SECURITY,
            VL_EXPOSURE,
            QTY
        FROM FIXED_INCOME_PORTFOLIO_POSITION
    """,
    "security_classification": """
        SELECT
            ID_SECURITY,
            NM_SECURITY,
            NM_ISSUER,
            NM_INDEX,
            DT_MATURITY,
            NM_INSTRUMENT_TYPE,
            NM_RATING,
            NM_SECTOR
        FROM FIXED_INCOME_SECURITY_CLASSIFICATION
    """,
    "market_data": """
        SELECT
            DT_REF,
            ID_SECURITY,
            NM_SECURITY,
            VL_DURATION,
            VL_BASE_YIELD,
            VL_SPREAD_REF
        FROM FIXED_INCOME_MARKET_DATA
    """,
    "schedule": """
        SELECT
            DT_REF,
            DT_PMT,
            ID_SECURITY,
            NM_SECURITY,
            TP_PMT,
            VL_PMT,
            DT_INPUT,
            USR_INPUT
        FROM FIXED_INCOME_SCHEDULE
    """,
}


def _empty_snapshot_df():
    return pd.DataFrame(columns=SNAPSHOT_COLUMNS)


DURATION_RATING_COLORS = {
    "AAA": "#10b981",
    "AA+": "#22c55e",
    "AA": "#84cc16",
    "AA-": "#eab308",
    "A+": "#f59e0b",
    "A": "#f97316",
    "A-": "#ef4444",
    "BBB+": "#dc2626",
    "BBB": "#b91c1c",
    "Unrated": "#9ca3af",
}

INSTRUMENT_GROUP_COLORS = {
    "Sovereign Bonds": "#5b8def",
    "Banking Instruments": "#f2554a",
    "Corporate Bonds": "#47c27f",
}

INSTRUMENT_TILE_COLORS = {
    "NTN-B (IPCA+)": "#5b86df",
    "LFT (Selic)": "#f2554a",
    "LTN (Prefixado)": "#49bf80",
    "NTN-F (Prefixado)": "#f4a31a",
    "NTN-C (IGPM)": "#8a63e5",
    "CDB": "#8b63e5",
    "LCI": "#49b4c7",
    "LCA": "#e45196",
    "LF": "#4bbdaa",
    "Debentures (IPCA+)": "#ff7a1a",
    "Debentures (CDI+)": "#f59e0b",
    "Debentures (%CDI)": "#8b63e5",
    "Debentures (IGPM)": "#d946ef",
    "Debentures (Prefixado)": "#f97316",
    "CRI": "#6c6fe3",
    "CRA": "#5b86df",
}


ISSUER_COLUMN_DEFS = [
    {"headerName": "Issuer", "field": "issuer", "flex": 2.3, "minWidth": 240},
    {"headerName": "Sector", "field": "sector", "flex": 1.0, "minWidth": 110},
    {"headerName": "Asset Code", "field": "asset_code", "flex": 0.8, "minWidth": 110},
    {
        "headerName": "Instrument Type",
        "field": "instrument_type",
        "flex": 0.8,
        "minWidth": 100,
    },
    {
        "headerName": "Bond Type",
        "field": "bond_type",
        "cellRenderer": "BondTypeBadge",
        "cellStyle": {"display": "flex", "alignItems": "center"},
        "flex": 0.6,
        "minWidth": 70,
    },
    {
        "headerName": "Rating",
        "field": "rating",
        "cellRenderer": "RatingBadge",
        "cellStyle": {"display": "flex", "alignItems": "center"},
        "flex": 0.6,
        "minWidth": 70,
    },
    {
        "headerName": "Yield",
        "field": "yield",
        "cellStyle": {"textAlign": "right"},
        "headerClass": "ag-right-aligned-header",
        "flex": 0.7,
        "minWidth": 80,
    },
    {
        "headerName": "Duration",
        "field": "duration",
        "cellStyle": {"textAlign": "right"},
        "headerClass": "ag-right-aligned-header",
        "flex": 0.7,
        "minWidth": 80,
    },
    {
        "headerName": "Exposure",
        "field": "exposure",
        "cellStyle": {"textAlign": "right", "fontWeight": 700},
        "headerClass": "ag-right-aligned-header",
        "flex": 1.0,
        "minWidth": 100,
    },
]

CASHFLOW_COLUMN_DEFS = [
    {"headerName": "Date", "field": "date", "flex": 0.8, "minWidth": 78},
    {
        "headerName": "Event Type",
        "field": "event_type",
        "cellRenderer": "CashflowEventBadge",
        "cellStyle": {"display": "flex", "alignItems": "center"},
        "flex": 0.9,
        "minWidth": 105,
    },
    {"headerName": "Asset Code", "field": "asset_code", "flex": 1.0, "minWidth": 110},
    {"headerName": "Issuer", "field": "issuer", "flex": 2.0, "minWidth": 220},
    {"headerName": "Instrument", "field": "instrument", "flex": 1.0, "minWidth": 120},
    {
        "headerName": "Clients",
        "field": "clients",
        "cellStyle": {
            "display": "flex",
            "alignItems": "center",
            "justifyContent": "flex-end",
            "textAlign": "right",
        },
        "headerClass": "ag-right-aligned-header",
        "flex": 0.8,
        "minWidth": 90,
    },
    {
        "headerName": "Principal",
        "field": "principal",
        "cellStyle": {
            "display": "flex",
            "alignItems": "center",
            "justifyContent": "flex-end",
            "textAlign": "right",
        },
        "headerClass": "ag-right-aligned-header",
        "flex": 1.1,
        "minWidth": 130,
    },
    {
        "headerName": "Interest",
        "field": "interest",
        "cellStyle": {
            "display": "flex",
            "alignItems": "center",
            "justifyContent": "flex-end",
            "textAlign": "right",
        },
        "headerClass": "ag-right-aligned-header",
        "flex": 1.1,
        "minWidth": 130,
    },
    {
        "headerName": "Total Cashflow",
        "field": "total_cashflow",
        "cellStyle": {
            "display": "flex",
            "alignItems": "center",
            "justifyContent": "flex-end",
            "textAlign": "right",
            "fontWeight": 700,
        },
        "headerClass": "ag-right-aligned-header",
        "flex": 1.25,
        "minWidth": 150,
    },
]

def stat_card(title, value, icon_text, color_class):
    return html.Div(
        className=f"portfolio-stat-card {color_class}",
        children=[
            html.Div(
                className="portfolio-stat-header",
                children=[
                    html.Div(title, className="portfolio-stat-title"),
                    html.Div(icon_text, className="portfolio-stat-icon"),
                ],
            ),
            html.Div(value, className="portfolio-stat-value"),
        ],
    )


def category_card(title, subtitle, exposure, avg_yield, duration, accent_class):
    return html.Div(
        className=f"portfolio-category-card {accent_class}",
        children=[
            html.Div(title, className="portfolio-category-title"),
            html.Div(subtitle, className="portfolio-category-subtitle"),
            html.Div("Exposure", className="portfolio-label"),
            html.Div(exposure, className="portfolio-exposure"),
            html.Div(
                className="portfolio-metrics",
                children=[
                    html.Div(
                        [
                            html.Div("Avg Yield", className="portfolio-label"),
                            html.Div(avg_yield, className="portfolio-metric-value"),
                        ],
                    ),
                    html.Div(
                        [
                            html.Div("Avg Duration", className="portfolio-label"),
                            html.Div(duration, className="portfolio-metric-value"),
                        ],
                    ),
                ],
            ),
        ],
    )


def _format_brl_string(value):
    if value is None or pd.isna(value):
        return "R$ 0.00"
    amount = float(value)
    absolute = abs(amount)
    if absolute >= 1_000_000_000:
        return f"R$ {amount / 1_000_000_000:.0f}B"
    if absolute >= 1_000_000:
        return f"R$ {amount / 1_000_000:.1f}mm"
    if absolute >= 1_000:
        return f"R$ {amount / 1_000:.1f}k"
    return f"R$ {amount:.1f}"


def _format_pct(value):
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):.2f}%"


def _format_years(value):
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):.1f} yrs"


def _bucket_from_index(index_name):
    index_text = str(index_name or "").upper()
    if "IPCA" in index_text or "IGPM" in index_text or "IGP-M" in index_text:
        return "inflation"
    if "CDI" in index_text or "SELIC" in index_text:
        return "floating"
    if "PRE" in index_text or "PREFIX" in index_text:
        return "fixed"
    return "fixed"


def _normalize_index_display(index_name):
    normalized = str(index_name or "").strip().upper()
    if normalized in {"PRE", "PREFIXADO"}:
        return "Prefixado"
    if normalized in {"SELIC"}:
        return "Selic"
    if normalized == "IPCA":
        return "IPCA+"
    if normalized == "IGPM":
        return "IGPM"
    if normalized == "%CDI":
        return "%CDI"
    if normalized == "CDI+":
        return "CDI+"
    return str(index_name or "").strip() or "--"


def _instrument_group(instrument_type):
    normalized = str(instrument_type or "").strip().upper()
    if normalized in {"LTN", "LFT", "NTN-B", "NTN-F", "NTN-C"}:
        return "Treasury Securities"
    if normalized in {"CDB", "LCI", "LCA", "LF"}:
        return "Bank Deposits"
    return "Credit Instruments"


def _instrument_display_label(instrument_type, index_name):
    normalized = str(instrument_type or "").strip().upper()
    index_display = _normalize_index_display(index_name)
    if normalized == "LFT":
        return "LFT (Selic)"
    if normalized in {"LTN", "NTN-F"}:
        return f"{normalized} (Prefixado)"
    if normalized == "NTN-B":
        return "NTN-B (IPCA+)"
    if normalized == "NTN-C":
        return "NTN-C (IGPM)"
    if normalized == "DEBENTURE":
        return f"Debentures ({index_display})"
    return str(instrument_type or "--").strip() or "--"


def _weighted_average(frame, value_col, weight_col):
    valid = frame[weight_col].notna() & (frame[weight_col] > 0) & frame[value_col].notna()
    if not valid.any():
        return None
    values = frame.loc[valid, value_col]
    weights = frame.loc[valid, weight_col]
    return (values * weights).sum() / weights.sum()


def _compound_yield(base_yield, spread_ref, index_name=None):
    if base_yield is None or pd.isna(base_yield):
        return None
    if spread_ref is None or pd.isna(spread_ref):
        return float(base_yield)
    index_text = str(index_name or "").upper()
    if "%CDI" in index_text:
        return float(base_yield) + float(spread_ref)
    return (((1 + float(base_yield) / 100.0) * (1 + float(spread_ref) / 100.0)) - 1) * 100.0


def _axis_max(values, min_default, tick):
    clean_values = pd.Series(values).dropna()
    if clean_values.empty:
        return min_default
    target = clean_values.max() * 1.15
    return max(min_default, tick * math.ceil(target / tick))


def load_portfolio_tables():
    empty_tables = {
        table_name: pd.DataFrame() for table_name in PORTFOLIO_TABLE_QUERIES
    }
    if not DATABASE_PATH.exists():
        return empty_tables

    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            tables = {
                table_name: pd.read_sql(query, conn)
                for table_name, query in PORTFOLIO_TABLE_QUERIES.items()
            }
    except Exception:
        return empty_tables

    date_columns = {
        "portfolio_position": ["DT_REF"],
        "security_classification": ["DT_MATURITY", "DT_INPUT"],
        "market_data": ["DT_REF"],
        "schedule": ["DT_REF", "DT_PMT", "DT_INPUT"],
    }
    numeric_columns = {
        "portfolio_position": ["ID_SECURITY", "ID_CLIENT", "VL_EXPOSURE", "QTY"],
        "security_classification": ["ID_SECURITY"],
        "market_data": ["ID_SECURITY", "VL_DURATION", "VL_BASE_YIELD", "VL_SPREAD_REF"],
        "schedule": ["ID_SECURITY", "VL_PMT"],
    }
    string_columns = {
        "portfolio_position": ["NM_SECURITY", "USR_INPUT"],
        "security_classification": [
            "NM_SECURITY",
            "NM_ISSUER",
            "NM_INDEX",
            "NM_INSTRUMENT_TYPE",
            "NM_RATING",
            "NM_SECTOR",
            "USR_INPUT",
        ],
        "market_data": ["NM_SECURITY", "USR_INPUT"],
        "schedule": ["NM_SECURITY", "TP_PMT", "USR_INPUT"],
    }

    for table_name, df in tables.items():
        if df.empty:
            continue
        for column_name in date_columns.get(table_name, []):
            if column_name in df.columns:
                df[column_name] = pd.to_datetime(df[column_name], errors="coerce")
        for column_name in numeric_columns.get(table_name, []):
            if column_name in df.columns:
                df[column_name] = pd.to_numeric(df[column_name], errors="coerce")
        for column_name in string_columns.get(table_name, []):
            if column_name in df.columns:
                df[column_name] = df[column_name].where(df[column_name].notna(), "").astype(str).str.strip()

    return tables


def build_portfolio_snapshot(tables):
    portfolio_df = tables["portfolio_position"].copy()
    classification_df = tables["security_classification"].copy()
    market_df = tables["market_data"].copy()
    if portfolio_df.empty:
        return _empty_snapshot_df(), 0.0, 0, 0

    portfolio_df = portfolio_df.dropna(subset=["DT_REF", "NM_SECURITY"]).copy()
    portfolio_df = portfolio_df[portfolio_df["NM_SECURITY"] != ""]
    if portfolio_df.empty:
        return _empty_snapshot_df(), 0.0, 0, 0

    latest_ref = portfolio_df["DT_REF"].max()
    latest_positions = portfolio_df.loc[portfolio_df["DT_REF"] == latest_ref].copy()
    latest_positions = latest_positions.dropna(subset=["VL_EXPOSURE"])
    if latest_positions.empty:
        return _empty_snapshot_df(), 0.0, 0, 0

    total_exposure = latest_positions["VL_EXPOSURE"].sum()
    positions_count = int(latest_positions["ID_SECURITY"].nunique())
    clients_count = int(latest_positions["ID_CLIENT"].dropna().nunique())

    merged = latest_positions.merge(
        classification_df,
        how="left",
        on=["ID_SECURITY", "NM_SECURITY"],
    )

    market_df = market_df.dropna(subset=["DT_REF", "NM_SECURITY"]).copy()
    market_df = market_df.loc[market_df["DT_REF"] <= latest_ref].copy()

    if not market_df.empty:
        latest_market = (
            market_df.sort_values(["NM_SECURITY", "DT_REF"])
            .groupby("NM_SECURITY", as_index=False)
            .tail(1)[["NM_SECURITY", "VL_DURATION", "VL_BASE_YIELD", "VL_SPREAD_REF"]]
        )
        merged = merged.merge(latest_market, on="NM_SECURITY", how="left")
    else:
        merged["VL_DURATION"] = None
        merged["VL_BASE_YIELD"] = None
        merged["VL_SPREAD_REF"] = None

    merged["NM_INDEX"] = merged["NM_INDEX"].fillna("")
    merged["NM_RATING"] = (
        merged["NM_RATING"].replace(r"^\s*$", pd.NA, regex=True).fillna("Unrated")
    )
    merged["NM_ISSUER"] = merged["NM_ISSUER"].fillna("--")
    merged["NM_SECTOR"] = merged["NM_SECTOR"].fillna("-")
    merged["NM_INSTRUMENT_TYPE"] = merged["NM_INSTRUMENT_TYPE"].fillna("--")
    merged["VL_COMPOUNDED_YIELD"] = merged.apply(
        lambda row: _compound_yield(
            row.get("VL_BASE_YIELD"),
            row.get("VL_SPREAD_REF"),
            row.get("NM_INDEX"),
        ),
        axis=1,
    )
    merged["bucket"] = merged["NM_INDEX"].apply(_bucket_from_index)

    for column_name in SNAPSHOT_COLUMNS:
        if column_name not in merged.columns:
            merged[column_name] = None

    return merged, float(total_exposure), positions_count, clients_count


def build_exposure_by_issuer_data(snapshot_df):
    required_cols = {"NM_ISSUER", "VL_EXPOSURE", "NM_RATING"}
    if snapshot_df.empty or not required_cols.issubset(set(snapshot_df.columns)):
        return []

    work_df = snapshot_df.copy()
    work_df["NM_RATING"] = (
        work_df["NM_RATING"].replace(r"^\s*$", pd.NA, regex=True).fillna("Unrated")
    )

    issuer_exposure = (
        work_df.groupby("NM_ISSUER", as_index=False)["VL_EXPOSURE"]
        .sum()
        .sort_values("VL_EXPOSURE", ascending=False)
    )

    issuer_rating = (
        work_df.sort_values(["NM_ISSUER", "VL_EXPOSURE"], ascending=[True, False])
        .drop_duplicates(subset=["NM_ISSUER"], keep="first")[["NM_ISSUER", "NM_RATING"]]
        .rename(columns={"NM_RATING": "rating"})
    )

    issuer_df = issuer_exposure.merge(issuer_rating, on="NM_ISSUER", how="left").head(10)
    issuer_df["exposure_display"] = issuer_df["VL_EXPOSURE"].apply(_format_brl_string)

    return issuer_df.rename(columns={"NM_ISSUER": "issuer", "VL_EXPOSURE": "exposure"})[
        ["issuer", "exposure", "rating", "exposure_display"]
    ].to_dict("records")


def build_exposure_chart_options(snapshot_df):
    exposure_data = build_exposure_by_issuer_data(snapshot_df)
    categories = [row.get("issuer") for row in exposure_data]
    values = []
    for row in exposure_data:
        rating = row.get("rating") or "Unrated"
        values.append(
            {
                "y": float(row.get("exposure") or 0.0),
                "issuer": row.get("issuer") or "--",
                "rating": rating,
                "exposure_display": row.get("exposure_display") or _format_brl_string(0.0),
                "color": DURATION_RATING_COLORS.get(rating, "#9ca3af"),
            }
        )

    return {
        "chart": {
            "type": "bar",
            "backgroundColor": "transparent",
            "height": 320,
            "spacingLeft": 10,
            "spacingRight": 10,
            "spacingTop": 28,
        },
        "title": {"text": None},
        "credits": {"enabled": False},
        "legend": {"enabled": False},
        "xAxis": {
            "categories": categories,
            "lineColor": "#e2e8f0",
            "tickColor": "#e2e8f0",
            "labels": {"style": {"color": "#475569", "fontSize": "12px"}},
        },
        "yAxis": {
            "title": {"text": None},
            "gridLineColor": "#e2e8f0",
            "tickAmount": 6,
            "labels": {"style": {"color": "#475569", "fontSize": "12px"}},
        },
        "tooltip": {
            "outside": False,
            "shared": False,
            "followPointer": False,
            "distance": 16,
            "backgroundColor": "#ffffff",
            "borderColor": "#e2e8f0",
            "borderRadius": 8,
            "shadow": True,
            "pointFormat": (
                "<b>{point.issuer}</b><br/>"
                "Rating: {point.rating}<br/>"
                "Exposure: {point.exposure_display}"
            ),
        },
        "plotOptions": {
            "series": {
                "borderRadius": 6,
                "pointPadding": 0.1,
                "groupPadding": 0.12,
                "dataLabels": {"enabled": False},
                "animation": {"duration": 700},
            }
        },
        "series": [{"name": "Exposure", "data": values, "colorByPoint": True}],
    }


def build_issuer_grid_data(snapshot_df):
    required_cols = {
        "NM_ISSUER",
        "NM_SECTOR",
        "NM_SECURITY",
        "NM_INSTRUMENT_TYPE",
        "NM_INDEX",
        "NM_RATING",
        "VL_DURATION",
        "VL_EXPOSURE",
    }
    if snapshot_df.empty or not required_cols.issubset(set(snapshot_df.columns)):
        return []

    grid_df = snapshot_df.copy()
    grid_df["issuer"] = grid_df["NM_ISSUER"].fillna("--")
    grid_df["sector"] = grid_df["NM_SECTOR"].fillna("-")
    grid_df["security_name"] = grid_df["NM_SECURITY"].fillna("--")
    grid_df["asset_code"] = grid_df["NM_SECURITY"].fillna("--")
    grid_df["instrument_type"] = grid_df["NM_INSTRUMENT_TYPE"].fillna("--")
    grid_df["bond_type"] = grid_df["NM_INDEX"].replace("", "--").fillna("--")
    grid_df["rating"] = (
        grid_df["NM_RATING"].replace(r"^\s*$", pd.NA, regex=True).fillna("Unrated")
    )
    yield_source = (
        "VL_COMPOUNDED_YIELD"
        if "VL_COMPOUNDED_YIELD" in grid_df.columns
        else "VL_BASE_YIELD"
    )
    grid_df["yield"] = grid_df[yield_source].apply(_format_pct)
    grid_df["duration"] = grid_df["VL_DURATION"].apply(_format_years)
    grid_df["exposure"] = grid_df["VL_EXPOSURE"].apply(_format_brl_string)
    grid_df = grid_df.sort_values("VL_EXPOSURE", ascending=False)

    return grid_df[
        [
            "issuer",
            "sector",
            "security_name",
            "asset_code",
            "instrument_type",
            "bond_type",
            "rating",
            "yield",
            "duration",
            "exposure",
        ]
    ].to_dict("records")


def _build_duration_chart_options(points, x_max, y_max, x_tick, y_tick):
    series_data = []
    for point in points:
        series_data.append(
            {
                "name": point["name"],
                "security": point["name"],
                "x": point["x"],
                "y": point["y"],
                "z": point["z"],
                "rating": point["rating"],
                "exposure_display": point.get("exposure_display", _format_brl_string(0.0)),
                "color": DURATION_RATING_COLORS.get(point["rating"], "#9ca3af"),
            }
        )

    return {
        "chart": {
            "type": "bubble",
            "backgroundColor": "transparent",
            "height": 340,
            "spacingLeft": 8,
            "spacingRight": 8,
            "spacingTop": 28,
            "spacingBottom": 6,
        },
        "title": {"text": None},
        "credits": {"enabled": False},
        "legend": {"enabled": False},
        "xAxis": {
            "min": 0,
            "max": x_max,
            "tickInterval": x_tick,
            "title": {
                "text": "Effective Duration (years)",
                "style": {"color": "#6b7280", "fontSize": "12px"},
            },
            "gridLineColor": "#e2e8f0",
            "gridLineDashStyle": "ShortDash",
            "labels": {
                "style": {"color": "#64748b", "fontSize": "11px"},
            },
            "lineColor": "#94a3b8",
            "tickColor": "#94a3b8",
        },
        "yAxis": {
            "min": 0,
            "max": y_max,
            "tickInterval": y_tick,
            "title": {
                "text": "Yield (%)",
                "style": {"color": "#6b7280", "fontSize": "12px"},
            },
            "gridLineColor": "#e2e8f0",
            "gridLineDashStyle": "ShortDash",
            "labels": {
                "format": "{value}%",
                "style": {"color": "#64748b", "fontSize": "11px"},
            },
        },
        "tooltip": {
            "useHTML": True,
            "shared": False,
            "followPointer": True,
            "outside": False,
            "distance": 1,
            "backgroundColor": "#ffffff",
            "borderColor": "#e2e8f0",
            "borderRadius": 8,
            "shadow": True,
            "pointFormat": (
                "<b>{point.security}</b><br/>"
                "Rating: {point.rating}<br/>"
                "Duration: {point.x:.2f} yrs<br/>"
                "Yield: {point.y:.2f}%<br/>"
                "Exposure: {point.exposure_display}"
            ),
        },
        "plotOptions": {
            "bubble": {
                "minSize": 18,
                "maxSize": 70,
                "stickyTracking": True,
                "marker": {"lineColor": "rgba(15,23,42,0.12)", "lineWidth": 0.5},
                "states": {"hover": {"enabled": True, "halo": {"size": 6}}},
            },
            "series": {"animation": {"duration": 700}},
        },
        "series": [{"name": "Securities", "turboThreshold": 0, "data": series_data}],
    }


def build_duration_chart_options(snapshot_df):
    defaults = {
        "fixed": {"x_max": 8, "y_max": 16, "x_tick": 2, "y_tick": 4},
        "floating": {"x_max": 3, "y_max": 16, "x_tick": 0.75, "y_tick": 4},
        "inflation": {"x_max": 16, "y_max": 12, "x_tick": 4, "y_tick": 3},
    }
    required_cols = {
        "bucket",
        "NM_INDEX",
        "VL_DURATION",
        "VL_COMPOUNDED_YIELD",
        "VL_EXPOSURE",
        "NM_RATING",
        "NM_SECURITY",
    }
    if snapshot_df.empty or not required_cols.issubset(set(snapshot_df.columns)):
        return {
            bucket: _build_duration_chart_options(
                [],
                x_max=cfg["x_max"],
                y_max=cfg["y_max"],
                x_tick=cfg["x_tick"],
                y_tick=cfg["y_tick"],
            )
            for bucket, cfg in defaults.items()
        }

    options = {}
    for bucket, cfg in defaults.items():
        bucket_df = snapshot_df.loc[snapshot_df["bucket"] == bucket].copy()
        bucket_df = bucket_df.dropna(
            subset=["VL_DURATION", "VL_COMPOUNDED_YIELD", "VL_EXPOSURE"]
        )
        bucket_df = bucket_df.loc[bucket_df["VL_EXPOSURE"] > 0]

        if bucket_df.empty:
            options[bucket] = _build_duration_chart_options(
                [],
                x_max=cfg["x_max"],
                y_max=cfg["y_max"],
                x_tick=cfg["x_tick"],
                y_tick=cfg["y_tick"],
            )
            continue

        # Portfolio positions are stored at client level; chart bubbles should
        # represent one point per security with exposure summed across clients.
        bucket_df = (
            bucket_df.groupby(
                ["NM_SECURITY", "NM_INDEX", "NM_RATING"],
                as_index=False,
            )
            .agg(
                VL_DURATION=("VL_DURATION", "first"),
                VL_COMPOUNDED_YIELD=("VL_COMPOUNDED_YIELD", "first"),
                VL_EXPOSURE=("VL_EXPOSURE", "sum"),
            )
        )

        chart_df = pd.DataFrame(
            {
                "name": bucket_df["NM_SECURITY"],
                "index_name": bucket_df["NM_INDEX"].astype(str).str.upper(),
                "rating": bucket_df["NM_RATING"]
                .replace(r"^\s*$", pd.NA, regex=True)
                .fillna("Unrated"),
                "x": bucket_df["VL_DURATION"],
                "y": bucket_df["VL_COMPOUNDED_YIELD"],
                "exposure_raw": bucket_df["VL_EXPOSURE"],
            }
        ).dropna(subset=["x", "y", "exposure_raw"])
        chart_df = chart_df.loc[chart_df["exposure_raw"] > 0]
        chart_df = chart_df.loc[chart_df["y"] >= 0]

        if chart_df.empty:
            options[bucket] = _build_duration_chart_options(
                [],
                x_max=cfg["x_max"],
                y_max=cfg["y_max"],
                x_tick=cfg["x_tick"],
                y_tick=cfg["y_tick"],
            )
            continue

        chart_df["z"] = chart_df["exposure_raw"] / 1_000_000
        chart_df["exposure_display"] = chart_df["exposure_raw"].apply(_format_brl_string)
        points = chart_df.to_dict("records")
        options[bucket] = _build_duration_chart_options(
            points,
            x_max=_axis_max(chart_df["x"], cfg["x_max"], cfg["x_tick"]),
            y_max=_axis_max(chart_df["y"], cfg["y_max"], cfg["y_tick"]),
            x_tick=cfg["x_tick"],
            y_tick=cfg["y_tick"],
        )

    return options


def build_instrument_type_chart(snapshot_df):
    required_cols = {"NM_INSTRUMENT_TYPE", "NM_INDEX", "VL_EXPOSURE"}
    empty_options = {
        "chart": {
            "type": "treemap",
            "backgroundColor": "transparent",
            "height": 560,
            "spacingLeft": 0,
            "spacingRight": 0,
            "spacingTop": 16,
            "spacingBottom": 8,
        },
        "title": {"text": None},
        "credits": {"enabled": False},
        "tooltip": {
            "useHTML": True,
            "backgroundColor": "#ffffff",
            "borderColor": "#e2e8f0",
            "borderRadius": 8,
            "shadow": True,
            "pointFormat": "<b>{point.name}</b><br/>Exposure: {point.exposure_display}",
        },
        "series": [],
    }
    if snapshot_df.empty or not required_cols.issubset(set(snapshot_df.columns)):
        return empty_options, []

    work_df = snapshot_df.copy()
    work_df["instrument_group"] = work_df["NM_INSTRUMENT_TYPE"].apply(_instrument_group)
    work_df["instrument_label"] = work_df.apply(
        lambda row: _instrument_display_label(
            row.get("NM_INSTRUMENT_TYPE"),
            row.get("NM_INDEX"),
        ),
        axis=1,
    )
    work_df = work_df.dropna(subset=["VL_EXPOSURE"])
    work_df = work_df.loc[work_df["VL_EXPOSURE"] > 0]

    if work_df.empty:
        return empty_options, []

    label_df = (
        work_df.groupby(["instrument_group", "instrument_label"], as_index=False)["VL_EXPOSURE"]
        .sum()
        .sort_values("VL_EXPOSURE", ascending=False)
    )
    group_df = (
        label_df.groupby("instrument_group", as_index=False)["VL_EXPOSURE"]
        .sum()
        .sort_values("VL_EXPOSURE", ascending=False)
    )

    treemap_data = []
    for _, group_row in group_df.iterrows():
        group_name = group_row["instrument_group"]
        group_id = f"group-{group_name.lower().replace(' ', '-')}"
        treemap_data.append(
            {
                "id": group_id,
                "name": group_name,
                "color": INSTRUMENT_GROUP_COLORS.get(group_name, "#94a3b8"),
            }
        )

        group_labels = label_df.loc[label_df["instrument_group"] == group_name]
        for _, label_row in group_labels.iterrows():
            label_name = label_row["instrument_label"]
            exposure_value = float(label_row["VL_EXPOSURE"])
            treemap_data.append(
                {
                    "name": label_name,
                    "parent": group_id,
                    "value": exposure_value,
                    "color": INSTRUMENT_TILE_COLORS.get(
                        label_name,
                        INSTRUMENT_GROUP_COLORS.get(group_name, "#94a3b8"),
                    ),
                    "exposure_display": _format_brl_string(exposure_value),
                }
            )

    legend_children = []
    for _, group_row in group_df.iterrows():
        group_name = group_row["instrument_group"]
        legend_children.append(
            html.Div(
                className="portfolio-instrument-legend-item",
                children=[
                    html.Span(
                        className="portfolio-instrument-legend-dot",
                        style={
                            "backgroundColor": INSTRUMENT_GROUP_COLORS.get(
                                group_name, "#94a3b8"
                            )
                        },
                    ),
                    html.Div(
                        className="portfolio-instrument-legend-text",
                        children=[
                            html.Div(
                                group_name,
                                className="portfolio-instrument-legend-title",
                            ),
                            html.Div(
                                _format_brl_string(group_row["VL_EXPOSURE"]),
                                className="portfolio-instrument-legend-value",
                            ),
                        ],
                    ),
                ],
            )
        )

    options = {
        "chart": {
            "type": "treemap",
            "backgroundColor": "transparent",
            "height": 560,
            "spacingLeft": 0,
            "spacingRight": 0,
            "spacingTop": 16,
            "spacingBottom": 8,
        },
        "title": {"text": None},
        "credits": {"enabled": False},
        "legend": {"enabled": False},
        "tooltip": {
            "useHTML": True,
            "backgroundColor": "#ffffff",
            "borderColor": "#e2e8f0",
            "borderRadius": 8,
            "shadow": True,
            "pointFormat": "<b>{point.name}</b><br/>Exposure: {point.exposure_display}",
        },
        "plotOptions": {
            "series": {
                "animation": {"duration": 700},
                "borderColor": "#ffffff",
                "borderWidth": 2,
            }
        },
        "series": [
            {
                "type": "treemap",
                "layoutAlgorithm": "squarified",
                "allowTraversingTree": False,
                "alternateStartingDirection": True,
                "levelIsConstant": False,
                "dataLabels": {"enabled": False},
                "levels": [
                    {
                        "level": 1,
                        "dataLabels": {"enabled": False},
                        "borderWidth": 0,
                    },
                    {
                        "level": 2,
                        "dataLabels": {
                            "enabled": True,
                            "useHTML": True,
                            "allowOverlap": True,
                            "format": (
                                "<div style=\"text-align:center;color:#ffffff;\">"
                                "<div style=\"font-size:16px;font-weight:700;line-height:1.2;\">{point.name}</div>"
                                "<div style=\"font-size:14px;font-weight:700;line-height:1.4;margin-top:6px;\">{point.exposure_display}</div>"
                                "</div>"
                            ),
                            "style": {"textOutline": "none"},
                        },
                    },
                ],
                "data": treemap_data,
            }
        ],
    }
    return options, legend_children


def build_projected_schedule_df(snapshot_df, schedule_df):
    schedule_df = schedule_df.copy()
    if schedule_df.empty or snapshot_df.empty:
        return pd.DataFrame(), pd.NaT

    schedule_df = schedule_df.dropna(
        subset=["DT_REF", "DT_PMT", "VL_PMT", "ID_SECURITY", "NM_SECURITY"]
    )
    if schedule_df.empty:
        return pd.DataFrame(), pd.NaT

    latest_schedule_ref = schedule_df["DT_REF"].max().normalize()
    schedule_df = schedule_df.loc[
        (schedule_df["DT_REF"].dt.normalize() == latest_schedule_ref)
        & (schedule_df["DT_PMT"] >= latest_schedule_ref)
    ].copy()
    if schedule_df.empty:
        return pd.DataFrame(), latest_schedule_ref

    classification_cols = [
        "ID_SECURITY",
        "NM_SECURITY",
        "NM_ISSUER",
        "NM_INSTRUMENT_TYPE",
        "NM_INDEX",
        "DT_MATURITY",
    ]
    classification_df = snapshot_df[classification_cols].drop_duplicates().copy()

    position_agg_df = (
        snapshot_df.groupby(["ID_SECURITY", "NM_SECURITY"], as_index=False).agg(
            clients=("ID_CLIENT", "nunique"),
            total_qty=("QTY", "sum"),
        )
    )

    projected_df = schedule_df.merge(
        classification_df,
        on=["ID_SECURITY", "NM_SECURITY"],
        how="left",
    ).merge(
        position_agg_df,
        on=["ID_SECURITY", "NM_SECURITY"],
        how="left",
    )
    if projected_df.empty:
        return projected_df, latest_schedule_ref

    projected_df["event_type"] = projected_df["TP_PMT"].where(
        projected_df["TP_PMT"].isin(["Principal", "Interest"]),
        projected_df["TP_PMT"].astype(str).str.title(),
    )
    projected_df["clients"] = projected_df["clients"].fillna(0)
    projected_df["total_qty"] = projected_df["total_qty"].fillna(0)
    projected_df["principal"] = (projected_df["VL_PMT"] * projected_df["total_qty"]).where(
        projected_df["TP_PMT"].eq("Principal"), 0.0
    )
    projected_df["interest"] = (projected_df["VL_PMT"] * projected_df["total_qty"]).where(
        projected_df["TP_PMT"].eq("Interest"), 0.0
    )
    projected_df["total_cashflow"] = projected_df["principal"] + projected_df["interest"]
    return projected_df, latest_schedule_ref


def build_cashflow_section(snapshot_df, schedule_df):
    projected_df, latest_schedule_ref = build_projected_schedule_df(snapshot_df, schedule_df)
    if projected_df.empty or pd.isna(latest_schedule_ref):
        return html.Div(
            className="portfolio-cashflow-section",
            children=[
                html.Div("Cash Flows & Liquidity", className="portfolio-section-title"),
                html.Div(
                    className="portfolio-cashflow-card",
                    children=[
                        html.Div(
                            "No upcoming cash flow events available",
                            className="portfolio-chart-subtitle",
                        )
                    ],
                ),
            ],
        )

    horizon = latest_schedule_ref + pd.Timedelta(days=7)
    merged_df = projected_df.loc[
        (projected_df["DT_PMT"] >= latest_schedule_ref)
        & (projected_df["DT_PMT"] <= horizon)
    ].copy()

    if merged_df.empty:
        return html.Div(
            className="portfolio-cashflow-section",
            children=[
                html.Div("Cash Flows & Liquidity", className="portfolio-section-title"),
                html.Div(
                    className="portfolio-cashflow-card",
                    children=[
                        html.Div(
                            "No upcoming cash flow events available",
                            className="portfolio-chart-subtitle",
                        )
                    ],
                ),
            ],
        )

    grouped_df = (
        merged_df.groupby(
            [
                "DT_PMT",
                "ID_SECURITY",
                "NM_SECURITY",
                "NM_ISSUER",
                "NM_INSTRUMENT_TYPE",
                "NM_INDEX",
                "DT_MATURITY",
                "clients",
                "event_type",
            ],
            as_index=False,
        )[["principal", "interest"]]
        .sum()
        .sort_values(["DT_PMT", "ID_SECURITY"])
    )

    if grouped_df.empty:
        return html.Div(
            className="portfolio-cashflow-section",
            children=[
                html.Div("Cash Flows & Liquidity", className="portfolio-section-title"),
                html.Div(
                    className="portfolio-cashflow-card",
                    children=[
                        html.Div(
                            "No upcoming cash flow events available",
                            className="portfolio-chart-subtitle",
                        )
                    ],
                ),
            ],
        )

    grouped_df["total_cashflow"] = grouped_df["principal"] + grouped_df["interest"]
    grouped_df["asset_name"] = grouped_df.apply(
        lambda row: _instrument_display_label(
            row.get("NM_INSTRUMENT_TYPE"),
            row.get("NM_INDEX"),
        ),
        axis=1,
    )
    grouped_df["date_display"] = grouped_df["DT_PMT"].dt.strftime("%d %b")
    grouped_df["principal_display"] = grouped_df["principal"].apply(
        lambda value: "-" if value <= 0 else _format_brl_string(value)
    )
    grouped_df["interest_display"] = grouped_df["interest"].apply(
        lambda value: "-" if value <= 0 else _format_brl_string(value)
    )
    grouped_df["total_display"] = grouped_df["total_cashflow"].apply(_format_brl_string)

    total_expected_cashflow = grouped_df["total_cashflow"].sum()
    total_events = len(grouped_df)
    impacted_securities = grouped_df[["ID_SECURITY", "NM_SECURITY"]].drop_duplicates()
    clients_impacted = (
        snapshot_df.merge(
            impacted_securities,
            on=["ID_SECURITY", "NM_SECURITY"],
            how="inner",
        )["ID_CLIENT"]
        .nunique()
    )
    total_principal = grouped_df["principal"].sum()
    total_interest = grouped_df["interest"].sum()

    row_data = (
        grouped_df.assign(
            date=grouped_df["date_display"],
            asset_code=grouped_df["NM_SECURITY"],
            issuer=grouped_df["NM_ISSUER"].fillna("--"),
            instrument=grouped_df["NM_INSTRUMENT_TYPE"].fillna("--"),
            clients=grouped_df["clients"].fillna(0).astype(int).astype(str),
            principal=grouped_df["principal_display"],
            interest=grouped_df["interest_display"],
            total_cashflow=grouped_df["total_display"],
        )[
            [
                "date",
                "event_type",
                "asset_code",
                "issuer",
                "instrument",
                "clients",
                "principal",
                "interest",
                "total_cashflow",
            ]
        ]
        .to_dict("records")
    )

    pinned_bottom_rows = [
        {
            "date": "",
            "event_type": "",
            "asset_code": "",
            "issuer": "",
            "instrument": "",
            "clients": "TOTAL (7 days):",
            "principal": _format_brl_string(total_principal),
            "interest": _format_brl_string(total_interest),
            "total_cashflow": _format_brl_string(total_expected_cashflow),
        }
    ]

    return html.Div(
        className="portfolio-cashflow-section",
        children=[
            html.Div("Cash Flows & Liquidity", className="portfolio-section-title"),
            html.Div(
                className="portfolio-cashflow-card",
                children=[
                    html.Div(
                        "Next 7 Days Cash Events",
                        className="portfolio-chart-title",
                    ),
                    html.Div(
                        "Upcoming coupon payments, amortizations, and maturities",
                        className="portfolio-chart-subtitle",
                    ),
                    html.Div(
                        className="portfolio-cashflow-stat-grid",
                        children=[
                            html.Div(
                                className="portfolio-cashflow-stat stat-blue",
                                children=[
                                    html.Div(
                                        "Total Expected Cashflow",
                                        className="portfolio-cashflow-stat-title",
                                    ),
                                    html.Div(
                                        _format_brl_string(total_expected_cashflow),
                                        className="portfolio-cashflow-stat-value",
                                    ),
                                ],
                            ),
                            html.Div(
                                className="portfolio-cashflow-stat stat-green",
                                children=[
                                    html.Div(
                                        "Total Events",
                                        className="portfolio-cashflow-stat-title",
                                    ),
                                    html.Div(
                                        str(total_events),
                                        className="portfolio-cashflow-stat-value",
                                    ),
                                ],
                            ),
                            html.Div(
                                className="portfolio-cashflow-stat stat-purple",
                                children=[
                                    html.Div(
                                        "Clients Impacted",
                                        className="portfolio-cashflow-stat-title",
                                    ),
                                    html.Div(
                                        str(int(clients_impacted)),
                                        className="portfolio-cashflow-stat-value",
                                    ),
                                ],
                            ),
                        ],
                    ),
                    html.Div(
                        className="portfolio-cashflow-grid-wrap",
                        children=[
                            dag.AgGrid(
                                id="portfolio-cashflow-grid",
                                className="ag-theme-alpine portfolio-grid portfolio-cashflow-grid",
                                columnDefs=CASHFLOW_COLUMN_DEFS,
                                rowData=row_data,
                                defaultColDef={
                                    "sortable": True,
                                    "filter": False,
                                    "resizable": False,
                                    "suppressMovable": True,
                                },
                                dashGridOptions={
                                    "domLayout": "autoHeight",
                                    "pagination": False,
                                    "pinnedBottomRowData": pinned_bottom_rows,
                                    "suppressMovableColumns": True,
                                },
                                dangerously_allow_code=True,
                            )
                        ],
                    ),
                ],
            ),
        ],
    )


def build_forward_cashflow_section(snapshot_df, schedule_df):
    projected_df, latest_schedule_ref = build_projected_schedule_df(snapshot_df, schedule_df)
    bucket_order = [f"W{week}" for week in range(1, 7)] + [f"Y{year}" for year in range(1, 11)]
    hover_band_width = int(max(56, min(88, round(1280 / max(len(bucket_order), 1)))))
    empty_options = {
        "chart": {
            "type": "column",
            "backgroundColor": "transparent",
            "height": 420,
            "spacingLeft": 8,
            "spacingRight": 8,
            "spacingTop": 12,
            "spacingBottom": 6,
        },
        "title": {"text": None},
        "credits": {"enabled": False},
        "xAxis": {
            "categories": bucket_order,
            "crosshair": {
                "color": "rgba(79,131,241,0.10)",
                "width": hover_band_width,
                "zIndex": 3,
            },
            "lineColor": "#94a3b8",
            "tickColor": "#94a3b8",
            "labels": {"style": {"color": "#64748b", "fontSize": "11px"}},
        },
        "yAxis": {
            "min": 0,
            "title": {
                "text": "Cash Flow (R$)",
                "style": {"color": "#6b7280", "fontSize": "12px"},
            },
            "gridLineColor": "#e2e8f0",
            "gridLineDashStyle": "ShortDash",
            "labels": {"format": "R$ {value:.0f}", "style": {"color": "#64748b", "fontSize": "11px"}},
        },
        "legend": {"enabled": False},
        "tooltip": {
            "shared": True,
            "outside": False,
            "backgroundColor": "#ffffff",
            "borderColor": "#e2e8f0",
            "borderRadius": 8,
            "shadow": True,
            "pointFormat": "<span style=\"color:{series.color}\">\u25CF</span> {series.name}: <b>{point.raw_display}</b><br/>",
        },
        "plotOptions": {
            "column": {
                "stacking": "normal",
                "borderRadius": 4,
                "pointPadding": 0.08,
                "groupPadding": 0.12,
            },
            "series": {"animation": {"duration": 700}},
        },
        "series": [
            {"name": "Principal", "data": [0.0] * len(bucket_order), "color": "#4f83f1"},
            {"name": "Interest", "data": [0.0] * len(bucket_order), "color": "#20b981"},
        ],
    }
    empty_section = html.Div(
        className="portfolio-forward-cashflow-card",
        children=[
            html.Div("Forward Cash Flow Projection", className="portfolio-chart-title"),
            html.Div(
                "Expected payments over next 6 weeks and 10 years",
                className="portfolio-chart-subtitle",
            ),
            html.Div(id="forward-cashflow-chart", className="portfolio-forward-cashflow-chart"),
            html.Div(
                className="portfolio-cashflow-stat-grid",
                children=[
                    html.Div(
                        className="portfolio-cashflow-stat stat-blue",
                        children=[
                            html.Div("Next 3 Weeks", className="portfolio-cashflow-stat-title"),
                            html.Div(_format_brl_string(0.0), className="portfolio-cashflow-stat-value"),
                        ],
                    ),
                    html.Div(
                        className="portfolio-cashflow-stat stat-green",
                        children=[
                            html.Div("Next 6 Months", className="portfolio-cashflow-stat-title"),
                            html.Div(_format_brl_string(0.0), className="portfolio-cashflow-stat-value"),
                        ],
                    ),
                    html.Div(
                        className="portfolio-cashflow-stat stat-purple",
                        children=[
                            html.Div("Next 1 Year", className="portfolio-cashflow-stat-title"),
                            html.Div(_format_brl_string(0.0), className="portfolio-cashflow-stat-value"),
                        ],
                    ),
                ],
            ),
        ],
    )
    if projected_df.empty or pd.isna(latest_schedule_ref):
        return empty_section, empty_options

    ten_year_horizon = latest_schedule_ref + pd.DateOffset(years=10)
    work_df = projected_df.loc[
        (projected_df["DT_PMT"] >= latest_schedule_ref)
        & (projected_df["DT_PMT"] <= ten_year_horizon)
        & (projected_df["total_cashflow"] > 0)
    ].copy()
    if work_df.empty:
        return empty_section, empty_options

    six_week_horizon = latest_schedule_ref + pd.Timedelta(weeks=6)

    def _projection_bucket(payment_date):
        payment_date = pd.Timestamp(payment_date).normalize()
        if payment_date <= six_week_horizon:
            day_offset = max((payment_date - latest_schedule_ref).days, 0)
            week_number = min(6, (day_offset // 7) + 1)
            return f"W{week_number}"
        for year_number in range(1, 11):
            if payment_date <= latest_schedule_ref + pd.DateOffset(years=year_number):
                return f"Y{year_number}"
        return None

    work_df["bucket"] = work_df["DT_PMT"].map(_projection_bucket)
    work_df = work_df.dropna(subset=["bucket"])
    if work_df.empty:
        return empty_section, empty_options

    bucket_df = (
        work_df.groupby("bucket", as_index=False)[["principal", "interest", "total_cashflow"]]
        .sum()
        .set_index("bucket")
        .reindex(bucket_order, fill_value=0.0)
        .reset_index()
    )

    max_total_raw = float(bucket_df["total_cashflow"].max()) if not bucket_df.empty else 0.0
    if max_total_raw >= 1_000_000_000:
        display_divisor = 1_000_000_000
        axis_unit = "B"
        axis_decimals = 0
    elif max_total_raw >= 1_000_000:
        display_divisor = 1_000_000
        axis_unit = "mm"
        axis_decimals = 1
    elif max_total_raw >= 1_000:
        display_divisor = 1_000
        axis_unit = "k"
        axis_decimals = 1
    else:
        display_divisor = 1
        axis_unit = ""
        axis_decimals = 0

    principal_display_values = (bucket_df["principal"] / display_divisor).tolist()
    interest_display_values = (bucket_df["interest"] / display_divisor).tolist()
    total_display_values = (bucket_df["total_cashflow"] / display_divisor).tolist()
    max_total_display = max(total_display_values) if total_display_values else 0.0

    if max_total_display >= 500:
        y_tick = 200
    elif max_total_display >= 200:
        y_tick = 100
    elif max_total_display >= 50:
        y_tick = 25
    elif max_total_display >= 10:
        y_tick = 5
    elif max_total_display >= 2:
        y_tick = 1
    elif max_total_display >= 0.5:
        y_tick = 0.25
    elif max_total_display >= 0.1:
        y_tick = 0.05
    elif max_total_display >= 0.02:
        y_tick = 0.01
    else:
        y_tick = 0.005
    y_max = max(y_tick, y_tick * math.ceil((max_total_display * 1.15) / y_tick))
    if axis_unit:
        axis_title = f"Cash Flow (R$ {axis_unit})"
        axis_label_format = f"R$ {{value:.{axis_decimals}f}}{axis_unit}"
    else:
        axis_title = "Cash Flow (R$)"
        axis_label_format = "R$ {value:.0f}"

    next_three_weeks_horizon = latest_schedule_ref + pd.Timedelta(weeks=3)
    next_six_months_horizon = latest_schedule_ref + pd.DateOffset(months=6)
    next_one_year_horizon = latest_schedule_ref + pd.DateOffset(years=1)

    next_three_weeks_total = projected_df.loc[
        (projected_df["DT_PMT"] >= latest_schedule_ref)
        & (projected_df["DT_PMT"] <= next_three_weeks_horizon),
        "total_cashflow",
    ].sum()
    next_six_months_total = projected_df.loc[
        (projected_df["DT_PMT"] >= latest_schedule_ref)
        & (projected_df["DT_PMT"] <= next_six_months_horizon),
        "total_cashflow",
    ].sum()
    next_one_year_total = projected_df.loc[
        (projected_df["DT_PMT"] >= latest_schedule_ref)
        & (projected_df["DT_PMT"] <= next_one_year_horizon),
        "total_cashflow",
    ].sum()

    options = {
        "chart": {
            "type": "column",
            "backgroundColor": "transparent",
            "height": 420,
            "spacingLeft": 8,
            "spacingRight": 8,
            "spacingTop": 12,
            "spacingBottom": 6,
        },
        "title": {"text": None},
        "credits": {"enabled": False},
        "xAxis": {
            "categories": bucket_order,
            "crosshair": {
                "color": "rgba(79,131,241,0.10)",
                "width": hover_band_width,
                "zIndex": 3,
            },
            "lineColor": "#94a3b8",
            "tickColor": "#94a3b8",
            "labels": {"style": {"color": "#64748b", "fontSize": "11px"}},
        },
        "yAxis": {
            "min": 0,
            "max": y_max,
            "tickInterval": y_tick,
            "title": {
                "text": axis_title,
                "style": {"color": "#6b7280", "fontSize": "12px"},
            },
            "gridLineColor": "#e2e8f0",
            "gridLineDashStyle": "ShortDash",
            "labels": {"format": axis_label_format, "style": {"color": "#64748b", "fontSize": "11px"}},
        },
        "legend": {"enabled": False},
        "tooltip": {
            "shared": True,
            "outside": False,
            "backgroundColor": "#ffffff",
            "borderColor": "#e2e8f0",
            "borderRadius": 8,
            "shadow": True,
            "headerFormat": "<b>{point.key}</b><br/>",
            "pointFormat": "<span style=\"color:{series.color}\">\u25CF</span> {series.name}: <b>{point.raw_display}</b><br/>",
        },
        "plotOptions": {
            "column": {
                "stacking": "normal",
                "borderRadius": 4,
                "pointPadding": 0.08,
                "groupPadding": 0.12,
            },
            "series": {"animation": {"duration": 700}},
        },
        "series": [
            {
                "name": "Principal",
                "data": [
                    {
                        "y": round(display_value, 4),
                        "raw_display": _format_brl_string(raw_value),
                    }
                    for display_value, raw_value in zip(
                        principal_display_values, bucket_df["principal"]
                    )
                ],
                "color": "#4f83f1",
            },
            {
                "name": "Interest",
                "data": [
                    {
                        "y": round(display_value, 4),
                        "raw_display": _format_brl_string(raw_value),
                    }
                    for display_value, raw_value in zip(
                        interest_display_values, bucket_df["interest"]
                    )
                ],
                "color": "#20b981",
            },
        ],
    }

    section = html.Div(
        className="portfolio-forward-cashflow-card",
        children=[
            html.Div("Forward Cash Flow Projection", className="portfolio-chart-title"),
            html.Div(
                "Expected payments over next 6 weeks and 10 years",
                className="portfolio-chart-subtitle",
            ),
            html.Div(id="forward-cashflow-chart", className="portfolio-forward-cashflow-chart"),
            html.Div(
                className="portfolio-cashflow-stat-grid",
                children=[
                    html.Div(
                        className="portfolio-cashflow-stat stat-blue",
                        children=[
                            html.Div("Next 3 Weeks", className="portfolio-cashflow-stat-title"),
                            html.Div(
                                _format_brl_string(next_three_weeks_total),
                                className="portfolio-cashflow-stat-value",
                            ),
                        ],
                    ),
                    html.Div(
                        className="portfolio-cashflow-stat stat-green",
                        children=[
                            html.Div("Next 6 Months", className="portfolio-cashflow-stat-title"),
                            html.Div(
                                _format_brl_string(next_six_months_total),
                                className="portfolio-cashflow-stat-value",
                            ),
                        ],
                    ),
                    html.Div(
                        className="portfolio-cashflow-stat stat-purple",
                        children=[
                            html.Div("Next 1 Year", className="portfolio-cashflow-stat-title"),
                            html.Div(
                                _format_brl_string(next_one_year_total),
                                className="portfolio-cashflow-stat-value",
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
    return section, options


layout = dbc.Container(
    fluid=True,
    className="page-container",
    children=[
        dcc.Interval(id="portfolio-refresh-interval", interval=30_000, n_intervals=0),
        html.Div(
            className="portfolio-hero",
            children=[
                html.H3(
                    "Fixed Income Portfolio",
                    className="portfolio-hero-title",
                    id="portfolio-data-loader",
                ),
                html.Div(
                    "Comprehensive breakdown of fixed income portfolio under custody",
                    className="portfolio-hero-subtitle",
                ),
            ],
        ),
        html.Div("Overview", className="portfolio-section-title"),
        html.Div(
            id="portfolio-stat-grid",
            className="portfolio-stat-grid",
            children=[
                stat_card("Total Exposure", "--", "↗", "accent-blue"),
                stat_card("Positions", "--", "◎", "accent-orange"),
                stat_card("Clients", "--", "👥", "accent-purple"),
            ],
        ),
        html.Div(
            id="portfolio-category-grid",
            className="portfolio-category-grid",
            children=[
                category_card(
                    "Fixed Rate (Prefixado)",
                    "LTN, NTN-F, Fixed Debentures",
                    "--",
                    "--",
                    "--",
                    "accent-green",
                ),
                category_card(
                    "Inflation-Linked (IPCA)",
                    "NTN-B, CRI, CRA, IPCA Debentures",
                    "--",
                    "--",
                    "--",
                    "accent-amber",
                ),
                category_card(
                    "Floating Rate (Pos-fixado)",
                    "LFT, CDB, LCI, LCA, CDI Debentures (CDI/SELIC)",
                    "--",
                    "--",
                    "--",
                    "accent-indigo",
                ),
            ],
        ),
        html.Div(
            className="portfolio-chart-card",
            children=[
                html.Div(
                    className="portfolio-chart-header",
                    children=[
                        html.Div(
                            "Exposure by Issuer",
                            className="portfolio-chart-title",
                        ),
                        html.Div(
                            "Top issuers by exposure amount",
                            className="portfolio-chart-subtitle",
                        ),
                    ],
                ),
                html.Div(
                    id="exposure-by-issuer-chart",
                    className="portfolio-exposure-chart",
                ),
                html.Div(
                    className="portfolio-rating-legend",
                    children=[
                        html.Span("Rating Scale:", className="rating-legend-title"),
                        html.Span(
                            [html.Span(className="rating-dot rating-aaa"), "AAA"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-aa-plus"), "AA+"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-aa"), "AA"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-aa-minus"), "AA-"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-a-plus"), "A+"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-a"), "A"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-a-minus"), "A-"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-bbb-plus"), "BBB+"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-bbb"), "BBB"],
                            className="rating-legend-item",
                        ),
                        html.Span(
                            [html.Span(className="rating-dot rating-unrated"), "Unrated"],
                            className="rating-legend-item",
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            className="portfolio-table-card",
            children=[
                html.Div(
                    className="portfolio-table-header",
                    children=[
                        html.Div(
                            [
                                html.Div(
                                    "Issuer Details",
                                    className="portfolio-table-title",
                                ),
                                html.Div(
                                    "Complete list of securities by issuer and sector",
                                    className="portfolio-table-subtitle",
                                ),
                            ]
                        ),
                    ],
                ),
                html.Div(
                    className="portfolio-summary-pills",
                    children=[
                        html.Div(
                            "Total Securities: --",
                            className="portfolio-summary-pill",
                            id="portfolio-summary-securities",
                        ),
                        html.Div(
                            "Total Exposure: --",
                            className="portfolio-summary-pill primary",
                            id="portfolio-summary-exposure",
                        ),
                    ],
                ),
                dag.AgGrid(
                    id="portfolio-issuer-grid",
                    className="ag-theme-alpine portfolio-grid",
                    columnDefs=ISSUER_COLUMN_DEFS,
                    rowData=[],
                    columnSize="sizeToFit",
                    defaultColDef={
                        "sortable": True,
                        "filter": True,
                        "resizable": False,
                        "suppressMovable": True,
                    },
                    dashGridOptions={
                        "pagination": True,
                        "paginationAutoPageSize": True,
                        "suppressHorizontalScroll": True,
                        "suppressMovableColumns": True,
                    },
                    dangerously_allow_code=True,
                    style={"height": "420px"},
                ),
            ],
        ),
        html.Div(
            className="portfolio-duration-overview-card",
            children=[
                html.Div("Exposure by Duration", className="portfolio-chart-title"),
                html.Div(
                    "Duration vs Yield analysis by bond type",
                    className="portfolio-chart-subtitle",
                ),
                html.Div(
                    className="portfolio-rating-legend",
                    children=[
                        html.Span("Rating Scale:", className="rating-legend-title"),
                        html.Span([html.Span(className="rating-dot rating-aaa"), "AAA"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-aa-plus"), "AA+"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-aa"), "AA"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-aa-minus"), "AA-"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-a-plus"), "A+"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-a"), "A"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-a-minus"), "A-"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-bbb-plus"), "BBB+"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-bbb"), "BBB"], className="rating-legend-item"),
                        html.Span([html.Span(className="rating-dot rating-unrated"), "Unrated"], className="rating-legend-item"),
                    ],
                ),
            ],
        ),
        html.Div(
            className="portfolio-duration-grid",
            children=[
                html.Div(
                    className="portfolio-duration-card",
                    children=[
                        html.Div("Fixed-rate (Prefixado)", className="portfolio-chart-title"),
                        html.Div(
                            "Duration vs Yield (bubble size = exposure)",
                            className="portfolio-chart-subtitle",
                        ),
                        html.Div(
                            id="duration-fixed-chart",
                            className="portfolio-duration-chart",
                        ),
                    ],
                ),
                html.Div(
                    className="portfolio-duration-card",
                    children=[
                        html.Div("Inflation", className="portfolio-chart-title"),
                        html.Div(
                            "Duration vs Yield (bubble size = exposure)",
                            className="portfolio-chart-subtitle",
                        ),
                        html.Div(
                            id="duration-ipca-chart",
                            className="portfolio-duration-chart",
                        ),
                    ],
                ),
                html.Div(
                    className="portfolio-duration-card",
                    children=[
                        html.Div("Floating-rate (Pós-fixado)", className="portfolio-chart-title"),
                        html.Div(
                            "Duration vs Yield (bubble size = exposure)",
                            className="portfolio-chart-subtitle",
                        ),
                        html.Div(
                            id="duration-floating-chart",
                            className="portfolio-duration-chart",
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            className="portfolio-instrument-card",
            children=[
                html.Div("Exposure by Instrument Type", className="portfolio-chart-title"),
                html.Div(
                    "Portfolio breakdown by Brazilian fixed income instruments",
                    className="portfolio-chart-subtitle",
                ),
                html.Div(
                    id="exposure-by-instrument-type-chart",
                    className="portfolio-instrument-chart",
                ),
                html.Div(
                    id="portfolio-instrument-type-legend",
                    className="portfolio-instrument-legend",
                ),
            ],
        ),
        html.Div(id="portfolio-cashflow-section"),
        html.Div(id="portfolio-forward-cashflow-section"),
        html.Div(id="portfolio-highcharts-options-json", style={"display": "none"}),
        html.Div(id="portfolio-exposure-render-probe", style={"display": "none"}),
        html.Div(id="portfolio-duration-fixed-render-probe", style={"display": "none"}),
        html.Div(id="portfolio-duration-floating-render-probe", style={"display": "none"}),
        html.Div(id="portfolio-duration-ipca-render-probe", style={"display": "none"}),
        html.Div(id="portfolio-instrument-type-render-probe", style={"display": "none"}),
        html.Div(id="portfolio-forward-cashflow-render-probe", style={"display": "none"}),
    ],
)


@callback(
    Output("portfolio-stat-grid", "children"),
    Output("portfolio-category-grid", "children"),
    Output("portfolio-issuer-grid", "rowData"),
    Output("portfolio-summary-securities", "children"),
    Output("portfolio-summary-exposure", "children"),
    Output("portfolio-instrument-type-legend", "children"),
    Output("portfolio-cashflow-section", "children"),
    Output("portfolio-forward-cashflow-section", "children"),
    Output("portfolio-highcharts-options-json", "children"),
    Input("portfolio-refresh-interval", "n_intervals"),
)
def refresh_portfolio_page(_):
    tables = load_portfolio_tables()
    snapshot_df, total_exposure, positions_count, clients_count = (
        build_portfolio_snapshot(tables)
    )

    stat_children = [
        stat_card("Total Exposure", _format_brl_string(total_exposure), "↗", "accent-blue"),
        stat_card("Positions", f"{positions_count}", "◎", "accent-orange"),
        stat_card("Clients", f"{clients_count}", "👥", "accent-purple"),
    ]

    category_config = [
        (
            "fixed",
            "Fixed Rate (Prefixado)",
            "LTN, NTN-F, Fixed Debentures",
            "accent-amber",
        ),
        (
            "inflation",
            "Inflation",
            "NTN-B, CRI, CRA, IPCA Debentures",
            "accent-green",
        ),
        (
            "floating",
            "Floating Rate (Pos-fixado)",
            "LFT, CDB, LCI, LCA, CDI Debentures (CDI/SELIC)",
            "accent-indigo",
        ),
    ]

    category_children = []
    for bucket, title, subtitle, accent_class in category_config:
        bucket_df = snapshot_df.loc[snapshot_df["bucket"] == bucket].copy()
        exposure_value = bucket_df["VL_EXPOSURE"].sum() if not bucket_df.empty else 0.0
        avg_yield = _weighted_average(bucket_df, "VL_COMPOUNDED_YIELD", "VL_EXPOSURE")
        avg_duration = _weighted_average(bucket_df, "VL_DURATION", "VL_EXPOSURE")
        category_children.append(
            category_card(
                title=title,
                subtitle=subtitle,
                exposure=_format_brl_string(exposure_value),
                avg_yield=_format_pct(avg_yield),
                duration=_format_years(avg_duration),
                accent_class=accent_class,
            )
        )

    exposure_options = build_exposure_chart_options(snapshot_df)
    duration_options = build_duration_chart_options(snapshot_df)
    instrument_type_options, instrument_type_legend = build_instrument_type_chart(
        snapshot_df
    )
    cashflow_section = build_cashflow_section(snapshot_df, tables["schedule"])
    forward_cashflow_section, forward_cashflow_options = build_forward_cashflow_section(
        snapshot_df, tables["schedule"]
    )
    grid_rows = build_issuer_grid_data(snapshot_df)

    summary_securities = f"Total Securities: {positions_count}"
    summary_exposure = f"Total Exposure: {_format_brl_string(total_exposure)}"

    chart_payload = {
        "exposure": exposure_options,
        "duration": {
            "fixed": duration_options["fixed"],
            "floating": duration_options["floating"],
            "inflation": duration_options["inflation"],
        },
        "instrument_type": instrument_type_options,
        "forward_cashflow": forward_cashflow_options,
    }

    return (
        stat_children,
        category_children,
        grid_rows,
        summary_securities,
        summary_exposure,
        instrument_type_legend,
        cashflow_section,
        forward_cashflow_section,
        json.dumps(chart_payload),
    )


clientside_callback(
    ClientsideFunction(namespace="highcharts", function_name="renderPortfolioExposureChart"),
    Output("portfolio-exposure-render-probe", "children"),
    Input("portfolio-highcharts-options-json", "children"),
)

clientside_callback(
    ClientsideFunction(
        namespace="highcharts", function_name="renderPortfolioDurationFixedChart"
    ),
    Output("portfolio-duration-fixed-render-probe", "children"),
    Input("portfolio-highcharts-options-json", "children"),
)

clientside_callback(
    ClientsideFunction(
        namespace="highcharts", function_name="renderPortfolioDurationFloatingChart"
    ),
    Output("portfolio-duration-floating-render-probe", "children"),
    Input("portfolio-highcharts-options-json", "children"),
)

clientside_callback(
    ClientsideFunction(namespace="highcharts", function_name="renderPortfolioDurationIpcaChart"),
    Output("portfolio-duration-ipca-render-probe", "children"),
    Input("portfolio-highcharts-options-json", "children"),
)

clientside_callback(
    ClientsideFunction(
        namespace="highcharts", function_name="renderPortfolioInstrumentTypeChart"
    ),
    Output("portfolio-instrument-type-render-probe", "children"),
    Input("portfolio-highcharts-options-json", "children"),
)

clientside_callback(
    ClientsideFunction(
        namespace="highcharts", function_name="renderPortfolioForwardCashFlowChart"
    ),
    Output("portfolio-forward-cashflow-render-probe", "children"),
    Input("portfolio-highcharts-options-json", "children"),
)
