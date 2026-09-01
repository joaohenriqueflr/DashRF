from pathlib import Path
import json
import sqlite3
from urllib.parse import quote
from datetime import date, timedelta
from functools import lru_cache

from dash import (
    ClientsideFunction,
    html,
    dcc,
    Input,
    Output,
    State,
    callback,
    clientside_callback,
    register_page,
)
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

import numpy as np
import pandas as pd

register_page(__name__, path="/", name="Fixed Income Monitor")

BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "data" / "fixed_income.db"

column_defs = [
    {"headerName": "Date", "field": "date"},
    {
        "headerName": "Source",
        "field": "source",
        "cellRenderer": "SourceBadge",
    },
    {"headerName": "Broker", "field": "broker"},
    {
        "headerName": "Side",
        "field": "side",
        "cellRenderer": "SideBadge",
    },
    {
        "headerName": "Quote (%)",
        "field": "quote",
        "valueFormatter": {"function": "d3.format('.3f')(params.value)"},
        "cellStyle": {"textAlign": "right"},
    },
    {
        "headerName": "Value",
        "field": "volume",
        "valueFormatter": {"function": "d3.format('.2s')(params.value)"},
        "cellStyle": {"textAlign": "right"},
    },
    {"headerName": "Input Date", "field": "input_date"},
    {"headerName": "User", "field": "user"},
]

TIME_RANGE_OPTIONS = [
    {"label": "Last 7 Days", "value": "7d"},
    {"label": "Last 30 Days", "value": "30d"},
    {"label": "Last 90 Days", "value": "90d"},
    {"label": "Year to Date", "value": "ytd"},
]


def resolve_time_window(time_range, fallback_start):
    business_days = load_brazil_business_days(time_range)
    if len(business_days) > 0:
        return business_days.min(), business_days.max()

    end_date = pd.Timestamp.today().normalize()
    start_date = fallback_start if fallback_start is not None else end_date
    return start_date, end_date


def _easter_sunday(year):
    # Meeus/Jones/Butcher Gregorian algorithm
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=16)
def _brazil_busdaycalendar(start_year, end_year):
    holiday_dates = set()
    for year in range(start_year, end_year + 1):
        easter = _easter_sunday(year)
        holiday_dates.update(
            {
                date(year, 1, 1),  # Confraternizacao Universal
                date(year, 4, 21),  # Tiradentes
                date(year, 5, 1),  # Dia do Trabalho
                date(year, 9, 7),  # Independencia
                date(year, 10, 12),  # Nossa Senhora Aparecida
                date(year, 11, 2),  # Finados
                date(year, 11, 15),  # Proclamacao da Republica
                date(year, 11, 20),  # Dia da Consciencia Negra
                date(year, 12, 25),  # Natal
                easter - timedelta(days=48),  # Carnaval (segunda)
                easter - timedelta(days=47),  # Carnaval (terca)
                easter - timedelta(days=2),  # Sexta-feira Santa
                easter + timedelta(days=60),  # Corpus Christi
            }
        )

    holiday_dates = sorted(
        holiday_dates
    )
    return np.busdaycalendar(
        weekmask="1111100",
        holidays=np.array(
            [np.datetime64(holiday.isoformat()) for holiday in holiday_dates],
            dtype="datetime64[D]",
        ),
    )


def _reference_business_day():
    today = pd.Timestamp.today().normalize()
    calendar = _brazil_busdaycalendar(today.year - 2, today.year + 1)
    today_np = np.datetime64(today.date().isoformat())
    offset_days = -1 if np.is_busday(today_np, busdaycal=calendar) else 0
    reference_np = np.busday_offset(
        today_np,
        offset_days,
        roll="backward",
        busdaycal=calendar,
    )
    return pd.Timestamp(str(reference_np)), calendar


def load_brazil_business_days(time_range):
    reference_day, calendar = _reference_business_day()
    if time_range == "7d":
        start_date = reference_day - pd.Timedelta(days=7)
    elif time_range == "30d":
        start_date = reference_day - pd.Timedelta(days=30)
    elif time_range == "90d":
        start_date = reference_day - pd.Timedelta(days=90)
    elif time_range == "ytd":
        jan1_np = np.datetime64(f"{reference_day.year}-01-01")
        start_np = np.busday_offset(jan1_np, 0, roll="forward", busdaycal=calendar)
        start_date = pd.Timestamp(str(start_np))
    else:
        start_date = reference_day - pd.Timedelta(days=30)

    start_np = np.datetime64(start_date.date().isoformat())
    reference_np = np.datetime64(reference_day.date().isoformat())

    all_days_np = np.arange(start_np, reference_np + np.timedelta64(1, "D"), dtype="datetime64[D]")
    business_days_np = all_days_np[np.is_busday(all_days_np, busdaycal=calendar)]
    if len(business_days_np) == 0:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(pd.to_datetime(business_days_np.astype(str))).sort_values()


def kpi_card(title, value, color_class, value_id=None):
    return html.Div(
        className=f"kpi-card {color_class}",
        children=[
            html.Div(title, className="kpi-title"),
            html.Div(value, className="kpi-value", id=value_id),
        ],
    )


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_bps(value):
    if value is None:
        return "--"
    return f"{value:.2f} bps"


def _format_pct(value):
    if value is None:
        return "--"
    return f"{value:.3f}%"


def _format_duration(value):
    if value is None:
        return "--"
    return f"{value:.2f}"


def _format_volume_compact(value):
    if value is None:
        return "--"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:.0f}"


def _load_sqlite_dataframe(query, params=()):
    if not DATABASE_PATH.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            return pd.read_sql(query, conn, params=params)
    except sqlite3.Error:
        return pd.DataFrame()


def load_security_classification_df(selected_security=None):
    query = """
        SELECT
            ID_SECURITY,
            NM_SECURITY,
            NM_ISSUER,
            DT_MATURITY,
            NM_INDEX
        FROM FIXED_INCOME_SECURITY_CLASSIFICATION
    """
    params = ()
    if selected_security:
        query += " WHERE NM_SECURITY = ?"
        params = (selected_security,)
    df = _load_sqlite_dataframe(query, params)
    if df.empty:
        return df
    if "DT_MATURITY" in df.columns:
        df["DT_MATURITY"] = df["DT_MATURITY"].fillna("").astype(str).str.strip()
    for column_name in ["NM_SECURITY", "NM_ISSUER", "NM_INDEX"]:
        if column_name in df.columns:
            df[column_name] = df[column_name].fillna("").astype(str).str.strip()
    return df


def load_market_data_df(selected_security=None):
    query = """
        SELECT
            DT_REF,
            NM_SECURITY,
            VL_SPREAD_BID,
            VL_SPREAD_ASK,
            VL_BASE_YIELD,
            VL_DURATION,
            VL_TRADED,
            DT_INPUT
        FROM FIXED_INCOME_MARKET_DATA
    """
    params = ()
    if selected_security:
        query += " WHERE NM_SECURITY = ?"
        params = (selected_security,)
    df = _load_sqlite_dataframe(query, params)
    if df.empty:
        return df
    df["DT_REF"] = pd.to_datetime(df["DT_REF"], errors="coerce")
    df["DT_INPUT"] = pd.to_datetime(df["DT_INPUT"], errors="coerce")
    for numeric_col in [
        "VL_SPREAD_BID",
        "VL_SPREAD_ASK",
        "VL_BASE_YIELD",
        "VL_DURATION",
        "VL_TRADED",
    ]:
        df[numeric_col] = pd.to_numeric(df[numeric_col], errors="coerce")
    df["VL_SPREAD_BID"] = df["VL_SPREAD_BID"] * 10_000.0
    df["VL_SPREAD_ASK"] = df["VL_SPREAD_ASK"] * 10_000.0
    df["VL_BASE_YIELD"] = df["VL_BASE_YIELD"] * 100.0
    df["NM_SECURITY"] = df["NM_SECURITY"].fillna("").astype(str).str.strip()
    return df


def load_broker_data_df(selected_security=None):
    query = """
        SELECT
            DT_REF,
            NM_SECURITY,
            NM_SOURCE,
            NM_BROKER,
            SIDE,
            TX_QUOTE,
            VL_QUOTE,
            DT_INPUT,
            USR_INPUT
        FROM FIXED_INCOME_BROKER_DATA
    """
    params = ()
    if selected_security:
        query += " WHERE NM_SECURITY = ?"
        params = (selected_security,)
    df = _load_sqlite_dataframe(query, params)
    if df.empty:
        return df
    df["DT_REF"] = pd.to_datetime(df["DT_REF"], errors="coerce")
    df["DT_INPUT"] = pd.to_datetime(df["DT_INPUT"], errors="coerce")
    for numeric_col in ["TX_QUOTE", "VL_QUOTE"]:
        df[numeric_col] = pd.to_numeric(df[numeric_col], errors="coerce")
    for column_name in ["NM_SECURITY", "NM_SOURCE", "NM_BROKER", "SIDE", "USR_INPUT"]:
        if column_name in df.columns:
            df[column_name] = df[column_name].fillna("").astype(str).str.strip()
    return df


def load_rfq_table_df(selected_security=None):
    query = """
        SELECT
            RFQ_ID,
            DT_INPUT,
            USR_INPUT,
            DIRECTION,
            SEC_NAME,
            QTY,
            VOLUME,
            STR_RESPONSE
        FROM RFQ
    """
    params = ()
    if selected_security:
        query += " WHERE SEC_NAME = ?"
        params = (selected_security,)
    df = _load_sqlite_dataframe(query, params)
    if df.empty:
        return df
    df["DT_INPUT"] = pd.to_datetime(df["DT_INPUT"], errors="coerce")
    for numeric_col in ["QTY", "VOLUME"]:
        df[numeric_col] = pd.to_numeric(df[numeric_col], errors="coerce")
    for column_name in ["USR_INPUT", "DIRECTION", "SEC_NAME", "STR_RESPONSE"]:
        if column_name in df.columns:
            df[column_name] = df[column_name].fillna("").astype(str).str.strip()
    return df


def load_characteristics_timeseries_df(selected_security, time_range):
    empty_df = pd.DataFrame()
    if not selected_security or not DATABASE_PATH.exists():
        return empty_df, empty_df, pd.DatetimeIndex([])

    base_df = load_market_data_df(selected_security)
    if base_df.empty:
        return base_df, base_df, pd.DatetimeIndex([])

    base_df = base_df.dropna(subset=["DT_REF"])

    if base_df.empty:
        return base_df, base_df, pd.DatetimeIndex([])

    latest_df = base_df.sort_values(
        ["DT_REF", "DT_INPUT"], ascending=False
    ).head(1)

    business_days = load_brazil_business_days(time_range)
    if len(business_days) > 0:
        start_date = business_days.min()
        end_date = business_days.max()
        window_df = base_df.loc[
            (base_df["DT_REF"] >= start_date) & (base_df["DT_REF"] <= end_date)
        ].copy()
    else:
        window_df = base_df.copy()

    return latest_df, window_df, business_days


def load_kpi_metrics(selected_security, time_range):
    empty_metrics = {
        "spread_bid": "--",
        "spread_ask": "--",
        "reference_rate": "--",
        "duration": "--",
        "avg_volume": "--",
    }
    if not selected_security or not DATABASE_PATH.exists():
        return empty_metrics

    latest_characteristics_df, trades_df, business_days = (
        load_characteristics_timeseries_df(selected_security, time_range)
    )

    spread_bid = None
    spread_ask = None
    reference_rate = None
    duration = None

    if not latest_characteristics_df.empty:
        latest_row = latest_characteristics_df.iloc[0]
        spread_bid = _to_float(latest_row.get("VL_SPREAD_BID"))
        spread_ask = _to_float(latest_row.get("VL_SPREAD_ASK"))
        reference_rate = _to_float(latest_row.get("VL_BASE_YIELD"))
        duration = _to_float(latest_row.get("VL_DURATION"))

    avg_volume = None
    if len(business_days) > 0:
        avg_volume = 0.0

    if not trades_df.empty and len(business_days) > 0:
        daily_volume = (
            trades_df.groupby("DT_REF")["VL_TRADED"]
            .sum(min_count=1)
            .reindex(business_days, fill_value=0.0)
        )
        avg_volume = _to_float(daily_volume.mean())

    return {
        "spread_bid": _format_bps(spread_bid),
        "spread_ask": _format_bps(spread_ask),
        "reference_rate": _format_pct(reference_rate),
        "duration": _format_duration(duration),
        "avg_volume": _format_volume_compact(avg_volume),
    }

def load_grid_trades_df(selected_security, time_range):
    if not selected_security or not DATABASE_PATH.exists():
        return pd.DataFrame()

    broker_df = load_broker_data_df(selected_security)
    rfq_df = load_rfq_table_df(selected_security)

    def _parse_response_payload(payload):
        if not payload:
            return []
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return []
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return []

    rfq_quotes_df = pd.DataFrame()
    if not rfq_df.empty:
        rfq_expanded_df = rfq_df.copy()
        rfq_expanded_df["response_list"] = rfq_expanded_df["STR_RESPONSE"].apply(
            _parse_response_payload
        )
        rfq_expanded_df = rfq_expanded_df.explode("response_list").dropna(
            subset=["response_list"]
        )

        if not rfq_expanded_df.empty:
            response_details_df = pd.json_normalize(rfq_expanded_df["response_list"])
            rfq_expanded_df = pd.concat(
                [
                    rfq_expanded_df.drop(columns=["response_list"]).reset_index(
                        drop=True
                    ),
                    response_details_df.reset_index(drop=True),
                ],
                axis=1,
            )

            rfq_expanded_df["broker_response"] = (
                rfq_expanded_df.get("broker_response", "")
                .astype(str)
                .str.strip()
            )
            rfq_expanded_df["broker name"] = (
                rfq_expanded_df.get("broker name", "")
                .astype(str)
                .str.strip()
            )
            rfq_expanded_df = rfq_expanded_df.loc[
                (rfq_expanded_df["broker name"] != "")
                & (rfq_expanded_df["broker_response"] != "")
            ].copy()

            if not rfq_expanded_df.empty:
                quote_match = rfq_expanded_df["broker_response"].str.extract(
                    r"([-+]?\d+(?:[.,]\d+)?)",
                    expand=False,
                )
                rfq_expanded_df["TX_QUOTE"] = pd.to_numeric(
                    quote_match.str.replace(",", ".", regex=False),
                    errors="coerce",
                )

                direction_map = {"sell": "Bid", "buy": "Ask"}
                rfq_expanded_df["SIDE"] = (
                    rfq_expanded_df["DIRECTION"]
                    .astype(str)
                    .str.strip()
                    .str.lower()
                    .map(direction_map)
                    .fillna("--")
                )

                volume_series = pd.to_numeric(rfq_expanded_df["VOLUME"], errors="coerce")
                qty_series = pd.to_numeric(rfq_expanded_df["QTY"], errors="coerce")
                rfq_expanded_df["VL_QUOTE"] = volume_series.fillna(qty_series)

                dt_input_fallback = pd.to_datetime(
                    rfq_expanded_df["DT_INPUT"], errors="coerce"
                )
                response_ts = pd.to_datetime(
                    rfq_expanded_df.get("timestamp"), errors="coerce"
                )
                dt_ref_series = response_ts.fillna(dt_input_fallback)

                rfq_expanded_df = rfq_expanded_df.loc[dt_ref_series.notna()].copy()
                if not rfq_expanded_df.empty:
                    response_ts = response_ts.loc[rfq_expanded_df.index]
                    dt_input_fallback = dt_input_fallback.loc[rfq_expanded_df.index]
                    dt_ref_series = dt_ref_series.loc[rfq_expanded_df.index]

                    rfq_quotes_df = pd.DataFrame(
                        {
                            "DT_REF": dt_ref_series.dt.normalize(),
                            "NM_SECURITY": rfq_expanded_df["SEC_NAME"],
                            "NM_SOURCE": "Trader",
                            "NM_BROKER": rfq_expanded_df["broker name"],
                            "SIDE": rfq_expanded_df["SIDE"],
                            "TX_QUOTE": rfq_expanded_df["TX_QUOTE"],
                            "VL_QUOTE": rfq_expanded_df["VL_QUOTE"],
                            "DT_INPUT": response_ts.fillna(dt_input_fallback),
                            "USR_INPUT": rfq_expanded_df["USR_INPUT"],
                        }
                    )
                    rfq_quotes_df = rfq_quotes_df.dropna(subset=["TX_QUOTE"])

    frames = [df for df in (broker_df, rfq_quotes_df) if df is not None and not df.empty]
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        trades_df = frames[0].copy()
    else:
        trades_df = pd.concat(frames, ignore_index=True, sort=False)

    if trades_df.empty:
        return trades_df

    trades_df = trades_df.dropna(subset=["DT_REF"])

    end_date = pd.Timestamp.today().normalize()
    if time_range == "7d":
        start_date = end_date - pd.Timedelta(days=7)
    elif time_range == "30d":
        start_date = end_date - pd.Timedelta(days=30)
    elif time_range == "90d":
        start_date = end_date - pd.Timedelta(days=90)
    elif time_range == "ytd":
        start_date = pd.Timestamp(end_date.year, 1, 1)
    else:
        start_date = trades_df["DT_REF"].min()

    trades_df = trades_df.loc[
        (trades_df["DT_REF"] >= start_date) & (trades_df["DT_REF"] <= end_date)
    ]

    return trades_df


def build_anbima_characteristics_url(security_code):
    if not security_code:
        return "#"
    code = str(security_code).strip()
    if not code:
        return "#"
    encoded_code = quote(code, safe="")
    if len(code) <= 7:
        return f"https://data.anbima.com.br/debentures/{encoded_code}/caracteristicas"
    return f"https://data.anbima.com.br/certificado-de-recebiveis/{encoded_code}/caracteristicas"


layout = dbc.Container(
    fluid=True,
    className="page-container",
    children=[
        dcc.Clipboard(id="monitor-grid-clipboard", style={"display": "none"}),
        html.Div(id="spread-volume-options-json", style={"display": "none"}),
        html.Div(
            className="controls-grid",
            children=[
                html.Div(
                    className="control-card",
                    children=[
                        html.Div("Selected Instrument", className="control-label"),
                        dcc.Dropdown(
                            id="instrument-dropdown",
                            options=[],
                            value=None,
                            placeholder="Search by security, issuer, or index...",
                            searchable=True,
                            clearable=False,
                            className="dropdown-input instrument-input",
                        ),
                    ],
                ),
                html.Div(
                    className="meta-grid",
                    children=[
                        html.Div(
                            className="control-card",
                            children=[
                                html.Div("Time Range", className="control-label"),
                                dcc.Dropdown(
                                    id="time-range-dropdown",
                                    options=TIME_RANGE_OPTIONS,
                                    value="30d",
                                    clearable=False,
                                    className="dropdown-input time-input",
                                ),
                            ],
                        ),
                        html.Div(
                            className="desk-card",
                            children=[
                                html.Div("Trading Desk", className="control-label"),
                                html.Div(
                                    "Fixed Income Monitor",
                                    className="desk-value",
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            className="kpi-grid",
            children=[
                kpi_card(
                    "Credit Spread - Bid",
                    "--",
                    "kpi-green",
                    "kpi-spread-bid-value",
                ),
                kpi_card(
                    "Credit Spread - Ask",
                    "--",
                    "kpi-orange",
                    "kpi-spread-ask-value",
                ),
                kpi_card(
                    "Reference Market Rate",
                    "--",
                    "kpi-blue",
                    "kpi-ref-rate-value",
                ),
                kpi_card(
                    "Yield Duration",
                    "--",
                    "kpi-blue",
                    "kpi-duration-value",
                ),
                kpi_card(
                    "Average Traded Volume (R$)",
                    "--",
                    "kpi-blue",
                    "kpi-avg-volume-value",
                ),
            ],
        ),
        html.Div(
            className="section-card",
            children=[
                html.Div(
                    className="section-header",
                    children=[
                        html.H4(
                            "Market Spread and Volume Analysis",
                            className="section-title",
                        ),
                        html.Div(
                            "Spread range (lines), individual quotes (points), and daily volume (bars)",
                            className="section-subtitle",
                        ),
                    ],
                ),
                html.Div(id="spread-volume-chart", className="chart-figure"),
                html.Div(
                    id="spread-volume-chart-probe",
                    style={"display": "none"},
                ),
            ],
        ),
        html.Div(
            className="section-card",
            children=[
                html.Div(
                    className="section-header section-header-with-action",
                    children=[
                        html.Div(
                            className="section-header-main",
                            children=[
                                html.H4(
                                    "Detailed Quote and Trade Data",
                                    className="section-title",
                                ),
                                html.Div(
                                    "Intraday quotes and execution snapshots",
                                    className="section-subtitle",
                                ),
                            ],
                        ),
                        html.Button(
                            "Copy Grid",
                            id="monitor-copy-grid-btn",
                            className="monitor-grid-copy-btn",
                            type="button",
                        ),
                    ],
                ),
                dag.AgGrid(
                    id="trade-grid",
                    rowData=[],
                    columnDefs=column_defs,
                    defaultColDef={
                        "sortable": True,
                        "filter": True,
                        "resizable": True,
                    },
                    className="ag-theme-alpine ag-grid",
                    style={"height": "380px"},
                ),
            ],
        ),
        html.Div(
            className="section-card anbima-link-section",
            children=[
                html.A(
                    "Link to Anbima webpage for security characteristics",
                    id="anbima-characteristics-link",
                    href="#",
                    target="_blank",
                    rel="noopener noreferrer",
                    className="anbima-characteristics-link",
                )
            ],
        ),
    ],
)


@callback(
    Output("instrument-dropdown", "options"),
    Output("instrument-dropdown", "value"),
    Input("instrument-dropdown", "id"),
)
def refresh_instrument_dropdown(_):
    options = []
    df = load_security_classification_df()
    if not df.empty:
        df = df.loc[df["NM_SECURITY"] != ""].sort_values("NM_SECURITY")
        for _, row in df.iterrows():
            issuer = row["NM_ISSUER"] or "--"
            maturity = row["DT_MATURITY"] or "--"
            index_name = row["NM_INDEX"] or "--"
            label = (
                f"{row['NM_SECURITY']} | {issuer} | "
                f"{maturity} | {index_name}"
            )
            options.append({"label": label, "value": row["NM_SECURITY"]})
    value = options[0]["value"] if options else None
    return options, value


@callback(
    Output("kpi-spread-bid-value", "children"),
    Output("kpi-spread-ask-value", "children"),
    Output("kpi-ref-rate-value", "children"),
    Output("kpi-duration-value", "children"),
    Output("kpi-avg-volume-value", "children"),
    Input("instrument-dropdown", "value"),
    Input("time-range-dropdown", "value"),
)
def update_kpi_cards(selected_security, time_range):
    metrics = load_kpi_metrics(selected_security, time_range)
    return (
        metrics["spread_bid"],
        metrics["spread_ask"],
        metrics["reference_rate"],
        metrics["duration"],
        metrics["avg_volume"],
    )


def build_spread_volume_options(selected_security, time_range):
    _, trade_df, _ = load_characteristics_timeseries_df(selected_security, time_range)
    if not trade_df.empty:
        trade_df = trade_df[
            [
                "DT_REF",
                "NM_SECURITY",
                "VL_SPREAD_BID",
                "VL_SPREAD_ASK",
                "VL_TRADED",
            ]
        ].copy()
    quote_df = load_grid_trades_df(selected_security, time_range)
    calendar_index = load_brazil_business_days(time_range)
    calendar_index = (
        pd.DatetimeIndex(calendar_index.dropna().drop_duplicates()).sort_values()
    )
    categories = [
        pd.Timestamp(dt_ref).strftime("%d %b")
        for dt_ref in calendar_index
    ]
    subtitle_options = {"text": "No trades found for the selected instrument", "align": "center", "style": {"color": "#64748b", "fontSize": "13px"}}
    series = []
    volume_data = []
    bid_data = []
    ask_data = []
    quote_series = []
    quote_activity_by_index = {}

    if not trade_df.empty:
        trade_df["DT_REF"] = pd.to_datetime(trade_df["DT_REF"], errors="coerce")
        trade_df["VL_SPREAD_BID"] = pd.to_numeric(
            trade_df["VL_SPREAD_BID"], errors="coerce"
        )
        trade_df["VL_SPREAD_ASK"] = pd.to_numeric(
            trade_df["VL_SPREAD_ASK"], errors="coerce"
        )
        trade_df["VL_TRADED"] = pd.to_numeric(trade_df["VL_TRADED"], errors="coerce")
        trade_df = trade_df.dropna(subset=["DT_REF"])

        plot_df = (
            trade_df.groupby("DT_REF", as_index=True)
            .agg(
                {
                    "VL_TRADED": "sum",
                    "VL_SPREAD_BID": "mean",
                    "VL_SPREAD_ASK": "mean",
                }
            )
            .rename(
                columns={
                    "VL_TRADED": "VL_QUOTE",
                    "VL_SPREAD_BID": "Bid",
                    "VL_SPREAD_ASK": "Ask",
                }
            )
            .sort_index()
        )

        if len(calendar_index) > 0:
            plot_df = plot_df.reindex(calendar_index)
        else:
            categories = [
                pd.Timestamp(dt_ref).strftime("%d %b")
                for dt_ref in plot_df.index
            ]

        if not plot_df.empty:
            volume_series = pd.to_numeric(plot_df["VL_QUOTE"], errors="coerce").div(
                1_000_000
            )
            bid_series = pd.to_numeric(plot_df["Bid"], errors="coerce")
            ask_series = pd.to_numeric(plot_df["Ask"], errors="coerce")

            volume_data = [
                round(float(value), 3) if pd.notna(value) else None
                for value in volume_series
            ]
            bid_data = [
                round(float(value), 3) if pd.notna(value) else None
                for value in bid_series
            ]
            ask_data = [
                round(float(value), 3) if pd.notna(value) else None
                for value in ask_series
            ]

            subtitle_options = {"text": None}

    if not quote_df.empty and len(categories) > 0:
        quote_df["DT_REF"] = pd.to_datetime(quote_df["DT_REF"], errors="coerce")
        quote_df["TX_QUOTE"] = pd.to_numeric(quote_df["TX_QUOTE"], errors="coerce")
        quote_df["DT_INPUT"] = pd.to_datetime(quote_df["DT_INPUT"], errors="coerce")
        quote_df["SIDE"] = quote_df["SIDE"].astype(str).str.strip().str.lower()
        quote_df["NM_BROKER"] = quote_df["NM_BROKER"].fillna("--").astype(str).str.strip()
        quote_df = quote_df.dropna(subset=["DT_REF", "TX_QUOTE"])
        quote_df["DT_REF"] = quote_df["DT_REF"].dt.normalize()
        quote_df = quote_df.loc[quote_df["SIDE"].isin(["bid", "ask"])]

        date_to_x = {
            pd.Timestamp(dt_ref).normalize(): idx
            for idx, dt_ref in enumerate(calendar_index)
        }

        if date_to_x:
            quote_df = quote_df.loc[quote_df["DT_REF"].isin(date_to_x.keys())].copy()
            if not quote_df.empty:
                latest_quote_df = (
                    quote_df.sort_values(["DT_REF", "SIDE", "NM_BROKER", "DT_INPUT"])
                    .groupby(["DT_REF", "SIDE", "NM_BROKER"], as_index=False)
                    .tail(1)
                )

                for dt_ref, date_df in latest_quote_df.groupby("DT_REF"):
                    x_idx = date_to_x.get(pd.Timestamp(dt_ref).normalize())
                    if x_idx is None:
                        continue
                    quote_activity_by_index[int(x_idx)] = {
                        "bid": [
                            {
                                "broker": row["NM_BROKER"],
                                "quote": round(float(row["TX_QUOTE"]), 3),
                                "source": str(row.get("NM_SOURCE") or "--"),
                            }
                            for _, row in date_df.loc[
                                date_df["SIDE"] == "bid"
                            ]
                            .sort_values(["TX_QUOTE", "NM_BROKER"], ascending=[False, True])
                            .iterrows()
                        ],
                        "ask": [
                            {
                                "broker": row["NM_BROKER"],
                                "quote": round(float(row["TX_QUOTE"]), 3),
                                "source": str(row.get("NM_SOURCE") or "--"),
                            }
                            for _, row in date_df.loc[
                                date_df["SIDE"] == "ask"
                            ]
                            .sort_values(["TX_QUOTE", "NM_BROKER"], ascending=[True, True])
                            .iterrows()
                        ],
                    }

                for side_key, side_label, side_color in [
                    ("bid", "Bid", "#16a34a"),
                    ("ask", "Ask", "#f97316"),
                ]:
                    side_df = latest_quote_df.loc[
                        latest_quote_df["SIDE"] == side_key
                    ].copy()
                    if side_df.empty:
                        continue
                    for broker_name, broker_df in side_df.groupby("NM_BROKER"):
                        data = [None] * len(categories)
                        for _, row in broker_df.iterrows():
                            x_idx = date_to_x.get(row["DT_REF"])
                            if x_idx is None:
                                continue
                            data[x_idx] = round(float(row["TX_QUOTE"]), 3)
                        if all(value is None for value in data):
                            continue
                        quote_series.append(
                            {
                                "type": "scatter",
                                "name": f"{side_label} - {broker_name}",
                                "data": data,
                                "color": side_color,
                                "yAxis": 0,
                                "showInLegend": False,
                                "marker": {
                                    "enabled": True,
                                    "radius": 3,
                                    "symbol": "circle",
                                },
                                "tooltip": {
                                    "pointFormat": (
                                        "<span style=\"color:{series.color}\">\u25CF</span> "
                                        f"{side_label} - {broker_name}: "
                                        "<b>{point.y:.3f}</b><br/>"
                                    )
                                },
                            }
                        )

    point_count = len(categories) if categories else 1
    hover_band_width = int(max(20, min(180, round(1200 / point_count))))

    base_options = {
        "chart": {
            "backgroundColor": "transparent",
            "height": 420,
            "spacingLeft": 0,
            "spacingRight": 0,
            "spacingTop": 10,
            "spacingBottom": 50,
        },
        "title": {"text": None},
        "subtitle": subtitle_options,
        "credits": {"enabled": False},
        "legend": {
            "enabled": True,
            "align": "left",
            "verticalAlign": "bottom",
            "itemStyle": {"color": "#1f2937", "fontSize": "12px"},
        },
        "xAxis": {
            "type": "category",
            "categories": categories,
            "lineColor": "#e8edf5",
            "tickColor": "#e8edf5",
            "gridLineWidth": 0,
            "crosshair": {
                "color": "rgba(37, 99, 235, 0.10)",
                "width": hover_band_width,
                "zIndex": 2,
            },
            "tickPixelInterval": 140,
            "labels": {
                "style": {"color": "#64748b", "fontSize": "12px"},
            },
        },
        "yAxis": [
            {
                "title": {
                    "text": "Quote",
                    "style": {"color": "#1f2937", "fontSize": "12px"},
                },
                "gridLineColor": "#e8edf5",
                "labels": {"style": {"color": "#64748b", "fontSize": "12px"}},
                "opposite": False,
            },
            {
                "title": {
                    "text": "Volume (R$ MM)",
                    "style": {"color": "#1f2937", "fontSize": "12px"},
                },
                "gridLineWidth": 0,
                "labels": {"style": {"color": "#64748b", "fontSize": "12px"}},
                "opposite": True,
            },
        ],
        "tooltip": {
            "shared": True,
            "backgroundColor": "#ffffff",
            "borderColor": "#d8e0ee",
            "borderRadius": 8,
            "shadow": True,
        },
        "custom": {
            "quoteActivityByIndex": quote_activity_by_index,
        },
        "plotOptions": {
            "series": {"animation": {"duration": 900}},
            "column": {
                "borderWidth": 0,
                "pointPadding": 0.08,
                "groupPadding": 0.12,
                "states": {
                    "hover": {
                        "enabled": True,
                        "color": "rgba(37, 99, 235, 0.35)",
                    }
                },
            },
            "spline": {"lineWidth": 2, "marker": {"enabled": True, "radius": 3}},
        },
        "series": [
                {
                    "type": "column",
                    "name": "Daily Volume",
                    "data": volume_data,
                    "color": "#e6e6e6",
                    "yAxis": 1,
                    "tooltip": {"valueSuffix": "M", "valueDecimals": 2},
                },
                {
                    "type": "spline",
                    "name": "Bid Quote",
                    "data": bid_data,
                    "color": "#16a34a",
                    "yAxis": 0,
                    "connectNulls": False,
                    "tooltip": {"valueDecimals": 3},
                },
                {
                    "type": "spline",
                    "name": "Ask Quote",
                    "data": ask_data,
                    "color": "#f97316",
                    "yAxis": 0,
                    "connectNulls": False,
                    "tooltip": {"valueDecimals": 3},
                },
                *quote_series,
            ],
    }

    return base_options


@callback(
    Output("spread-volume-options-json", "children"),
    Input("instrument-dropdown", "value"),
    Input("time-range-dropdown", "value"),
)
def update_chart_options_json(selected_security, time_range):
    options = build_spread_volume_options(selected_security, time_range)
    return json.dumps(options)


clientside_callback(
    ClientsideFunction(namespace="highcharts", function_name="renderSpreadVolumeChart"),
    Output("spread-volume-chart-probe", "children"),
    Input("spread-volume-options-json", "children"),
)


@callback(
    Output("trade-grid", "rowData"),
    Input("instrument-dropdown", "value"),
    Input("time-range-dropdown", "value"),
)
def update_trade_grid(selected_security, time_range):
    trades_df = load_grid_trades_df(selected_security, time_range)

    if trades_df.empty:
        return []

    trades_df = trades_df.rename(
        columns={
            "DT_REF": "date",
            "NM_SOURCE": "source",
            "NM_BROKER": "broker",
            "SIDE": "side",
            "TX_QUOTE": "quote",
            "VL_QUOTE": "volume",
            "DT_INPUT": "input_date",
            "USR_INPUT": "user",
        }
    )

    trades_df["_sort_date"] = pd.to_datetime(trades_df["date"], errors="coerce")
    trades_df["_sort_input_date"] = pd.to_datetime(
        trades_df["input_date"], errors="coerce"
    )

    trades_df = trades_df.sort_values(
        ["_sort_date", "_sort_input_date"], ascending=False
    )

    trades_df["date"] = trades_df["_sort_date"].dt.strftime("%d-%b-%Y").fillna("")
    trades_df["input_date"] = (
        trades_df["_sort_input_date"].dt.strftime("%d-%b-%Y").fillna("")
    )

    trades_df = trades_df.drop(columns=["_sort_date", "_sort_input_date"])


    return trades_df.to_dict("records")


@callback(
    Output("monitor-grid-clipboard", "content"),
    Output("monitor-grid-clipboard", "n_clicks"),
    Input("monitor-copy-grid-btn", "n_clicks"),
    State("trade-grid", "rowData"),
    State("trade-grid", "columnDefs"),
    State("monitor-grid-clipboard", "n_clicks"),
    prevent_initial_call=True,
)
def copy_monitor_grid_to_clipboard(
    copy_clicks,
    row_data,
    column_defs,
    clipboard_clicks,
):
    if not copy_clicks:
        raise PreventUpdate
    if not row_data or not column_defs:
        raise PreventUpdate

    visible_cols = []
    for col in column_defs:
        field = col.get("field")
        if not field:
            continue
        if col.get("hide"):
            continue
        if field == "actions":
            continue
        visible_cols.append((field, col.get("headerName") or field))

    if not visible_cols:
        raise PreventUpdate

    def _clean_value(value):
        if value is None:
            return ""
        text = str(value)
        text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        return text.strip()

    lines = []
    lines.append("\t".join([header for _, header in visible_cols]))
    for row in row_data:
        lines.append(
            "\t".join([_clean_value(row.get(field)) for field, _ in visible_cols])
        )

    return "\n".join(lines), (clipboard_clicks or 0) + 1


@callback(
    Output("anbima-characteristics-link", "href"),
    Input("instrument-dropdown", "value"),
)
def update_anbima_characteristics_link(selected_security):
    return build_anbima_characteristics_url(selected_security)
