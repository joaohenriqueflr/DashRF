import json
import hashlib
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from dash import (
    ClientsideFunction,
    Input,
    Output,
    State,
    callback,
    clientside_callback,
    ctx,
    dcc,
    html,
    register_page,
    no_update,
)
from dash.exceptions import PreventUpdate
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc

register_page(__name__, path="/rfq", name="UGM Request for Quote")

BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "data" / "fixed_income.db"


def _sovereign_security_type(security_name):
    if not security_name:
        return ""
    return str(security_name).strip().split(" ", 1)[0]


def _sovereign_maturity_label(security_name):
    if not security_name:
        return ""
    parts = str(security_name).strip().split(" ", 1)
    return parts[1] if len(parts) > 1 else parts[0]


def load_sovereign_bond_options():
    if not DATABASE_PATH.exists():
        return []
    security_type_expr = "substr(NM_SECURITY, 1, instr(NM_SECURITY || ' ', ' ') - 1)"
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT NM_SECURITY
                FROM FIXED_INCOME_SECURITY_CLASSIFICATION
                WHERE NM_SECTOR = 'Sovereign'
                  AND NM_SECURITY IS NOT NULL
                  AND TRIM(NM_SECURITY) <> ''
                ORDER BY
                    CASE {security_type_expr}
                        WHEN 'LFT' THEN 1
                        WHEN 'NTN-B' THEN 2
                        WHEN 'NTN-F' THEN 3
                        WHEN 'LTN' THEN 4
                        WHEN 'NTN-C' THEN 5
                        ELSE 99
                    END,
                    {security_type_expr}
                """
            ).fetchall()
        seen = set()
        options = []
        for (security_name,) in rows:
            security_type = _sovereign_security_type(security_name)
            if not security_type or security_type in seen:
                continue
            seen.add(security_type)
            options.append({"value": security_type, "label": security_type})
        return options
    except sqlite3.Error:
        return []


def load_sovereign_maturity_options(bond_name):
    if not bond_name:
        return []
    if not DATABASE_PATH.exists():
        return []
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            rows = conn.execute(
                """
                SELECT NM_SECURITY, DT_MATURITY
                FROM FIXED_INCOME_SECURITY_CLASSIFICATION
                WHERE NM_SECTOR = 'Sovereign'
                  AND NM_SECURITY IS NOT NULL
                  AND TRIM(NM_SECURITY) <> ''
                  AND DT_MATURITY IS NOT NULL
                  AND TRIM(DT_MATURITY) <> ''
                  AND substr(NM_SECURITY, 1, instr(NM_SECURITY || ' ', ' ') - 1) = ?
                ORDER BY DT_MATURITY
                """,
                (bond_name,),
            ).fetchall()
        return [
            {
                "label": _sovereign_maturity_label(security_name),
                "value": security_name,
            }
            for security_name, _maturity in rows
        ]
    except sqlite3.Error:
        return []


SOVEREIGN_BOND_OPTIONS = load_sovereign_bond_options()
DEFAULT_SOVEREIGN_BOND = (
    SOVEREIGN_BOND_OPTIONS[0]["value"] if SOVEREIGN_BOND_OPTIONS else "LFT"
)
DEFAULT_SOVEREIGN_MATURITY_OPTIONS = load_sovereign_maturity_options(
    DEFAULT_SOVEREIGN_BOND
)

def _format_quantity(value):
    if value in (None, "", "--"):
        return "--"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _format_limit(value):
    if value in (None, "", "--"):
        return "--"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _format_volume(value):
    if value in (None, "", "--"):
        return "--"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _parse_quantity(value):
    if value in (None, "", "--"):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _parse_number(value):
    if value in (None, "", "--"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_iso_minute_timestamp(value):
    if value in (None, "", "--"):
        return None
    text = str(value).strip()
    if not text:
        return None

    if " " in text and "T" not in text:
        text = text.replace(" ", "T")

    if re.fullmatch(r"\d{2}:\d{2}", text):
        text = f"{date.today().isoformat()}T{text}:00"
    elif re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
        text = f"{date.today().isoformat()}T{text}"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def _split_response_time(value):
    if value in (None, "", "--"):
        return None, None, False
    text = str(value).strip()
    time_pattern = (
        r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"
        r"|\d{2}:\d{2}(?::\d{2})?)"
    )
    matches = list(re.finditer(time_pattern, text))
    if not matches:
        return text, None, True

    last_match = matches[-1].group(1)
    timestamp_text = last_match.replace(" ", "T")
    if len(timestamp_text) == 16:
        timestamp_text = f"{timestamp_text}:00"

    cleaned = re.sub(time_pattern, "", text).strip()
    cleaned = cleaned.replace("|", " ").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    timestamp = _normalize_iso_minute_timestamp(timestamp_text)
    if not timestamp:
        return cleaned, None, True

    return cleaned, timestamp, False


def _extract_time(timestamp):
    normalized_timestamp = _normalize_iso_minute_timestamp(timestamp)
    if not normalized_timestamp:
        return None
    return normalized_timestamp.split("T", 1)[1][:5]


def _format_maturity_label(value):
    if not value or value in ("--", ""):
        return ""
    parts = str(value).split("-")
    if len(parts) < 2:
        return ""
    months = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ]
    try:
        month_index = int(parts[1]) - 1
    except ValueError:
        return ""
    if month_index < 0 or month_index > 11:
        return ""
    year = parts[0][-2:]
    return f"{months[month_index]}-{year}"


def _build_rfq_text(row_data):
    if not row_data:
        return ""
    direction = str(row_data.get("direction") or "").lower()
    side = "bid" if direction == "sell" else "offer"
    RFQ_text_parts = []
    if side:
        RFQ_text_parts.append(side)
    security = row_data.get("security")
    if security and security != "--":
        RFQ_text_parts.append(str(security))
    if row_data.get("asset_class") == "Sovereign Bond":
        maturity = _format_maturity_label(row_data.get("maturity"))
        if maturity and maturity not in str(security or ""):
            RFQ_text_parts.append(maturity)
    volume = _parse_number(row_data.get("volume_raw"))
    if volume and volume > 0:
        if volume >= 1_000_000:
            volume_mm = volume / 1_000_000
            size_text = f"{volume_mm:.1f}mm BRL"
        else:
            volume_k = int(round(volume / 1000))
            size_text = f"{volume_k:,}k BRL"
        RFQ_text_parts.append(size_text)
    else:
        quantity = _parse_number(row_data.get("quantity_raw"))
        if quantity and quantity > 0:
            RFQ_text_parts.append(f"{int(quantity):,}q")
    RFQ_text_parts.append("D0")
    return " ".join(RFQ_text_parts)


def _parse_response(payload):
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _normalize_response_entries(entries):
    normalized = []
    changed = False
    for entry in entries:
        broker_name = str(entry.get("broker name") or "").strip()
        broker_response = str(entry.get("broker_response") or "").strip()
        if not broker_name or not broker_response:
            changed = True
            continue

        normalized_entry = {
            "broker name": broker_name,
            "broker_response": broker_response,
        }
        normalized_timestamp = _normalize_iso_minute_timestamp(entry.get("timestamp"))
        if normalized_timestamp:
            normalized_entry["timestamp"] = normalized_timestamp
            if entry.get("timestamp") != normalized_timestamp:
                changed = True
        elif entry.get("timestamp") not in (None, "", "--"):
            changed = True

        if (
            entry.get("broker name") != broker_name
            or entry.get("broker_response") != broker_response
        ):
            changed = True
        normalized.append(normalized_entry)
    return normalized, changed


def _counterparty_slug(name):
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not cleaned:
        cleaned = "counterparty"
    digest = hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"cp_{cleaned}_{digest}"


def ensure_rfq_schema():
    if not DATABASE_PATH.exists():
        return
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

            if "RFQ" in tables:
                conn.execute("UPDATE RFQ SET FL_ACTIVE = 1 WHERE FL_ACTIVE IS NULL")
                rows = conn.execute(
                    """
                    SELECT RFQ_ID, STR_RESPONSE
                    FROM RFQ
                    WHERE STR_RESPONSE IS NOT NULL AND STR_RESPONSE <> ''
                    """
                ).fetchall()
                for rfq_id, payload in rows:
                    normalized_payload, changed = _normalize_response_entries(
                        _parse_response(payload)
                    )
                    if changed:
                        conn.execute(
                            "UPDATE RFQ SET STR_RESPONSE = ? WHERE RFQ_ID = ?",
                            (json.dumps(normalized_payload), rfq_id),
                        )

                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_rfq_class_active_dt_input
                    ON RFQ (CLASS, FL_ACTIVE, DT_INPUT DESC, RFQ_ID DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_rfq_security
                    ON RFQ (SEC_NAME)
                    """
                )

            if "RFQ_COUNTERPARTY" in tables:
                conn.execute(
                    """
                    UPDATE RFQ_COUNTERPARTY
                    SET TP_TRADE = trim(TP_TRADE),
                        NM_COUNTERPARTY = trim(NM_COUNTERPARTY)
                    WHERE COALESCE(TP_TRADE, '') <> trim(COALESCE(TP_TRADE, ''))
                       OR COALESCE(NM_COUNTERPARTY, '') <> trim(COALESCE(NM_COUNTERPARTY, ''))
                    """
                )
                conn.execute(
                    """
                    DELETE FROM RFQ_COUNTERPARTY
                    WHERE rowid NOT IN (
                        SELECT MIN(rowid)
                        FROM RFQ_COUNTERPARTY
                        GROUP BY
                            lower(trim(COALESCE(TP_TRADE, ''))),
                            lower(trim(COALESCE(NM_COUNTERPARTY, '')))
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_rfq_counterparty_trade_name
                    ON RFQ_COUNTERPARTY (
                        TP_TRADE COLLATE NOCASE,
                        NM_COUNTERPARTY COLLATE NOCASE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_rfq_counterparty_trade_active_name
                    ON RFQ_COUNTERPARTY (TP_TRADE, FL_ACTIVE, NM_COUNTERPARTY)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_rfq_counterparty_active_name
                    ON RFQ_COUNTERPARTY (FL_ACTIVE, NM_COUNTERPARTY)
                    """
                )

            if "RFQ_REQUESTERS" in tables:
                conn.execute(
                    """
                    UPDATE RFQ_REQUESTERS
                    SET NM_REQUESTER = trim(NM_REQUESTER)
                    WHERE COALESCE(NM_REQUESTER, '') <> trim(COALESCE(NM_REQUESTER, ''))
                    """
                )
                conn.execute(
                    """
                    DELETE FROM RFQ_REQUESTERS
                    WHERE rowid NOT IN (
                        SELECT MIN(rowid)
                        FROM RFQ_REQUESTERS
                        GROUP BY lower(trim(COALESCE(NM_REQUESTER, '')))
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_rfq_requesters_name
                    ON RFQ_REQUESTERS (NM_REQUESTER COLLATE NOCASE)
                    """
                )

            if "FIXED_INCOME_SECURITY_CLASSIFICATION" in tables:
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_fix_class_sector_type_maturity
                    ON FIXED_INCOME_SECURITY_CLASSIFICATION (
                        NM_SECTOR,
                        NM_INSTRUMENT_TYPE,
                        DT_MATURITY
                    )
                    """
                )

            if "FIXED_INCOME_MARKET_DATA" in tables:
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_fix_char_security_ref_input
                    ON FIXED_INCOME_MARKET_DATA (NM_SECURITY, DT_REF DESC, DT_INPUT DESC)
                    """
                )

            if "FIXED_INCOME_BROKER_DATA" in tables:
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_fix_broker_security_ref
                    ON FIXED_INCOME_BROKER_DATA (NM_SECURITY, DT_REF DESC)
                    """
                )

            conn.commit()
    except sqlite3.Error:
        return


ensure_rfq_schema()



def load_counterparties(trade_type):
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            rows = conn.execute(
                """
                SELECT NM_COUNTERPARTY
                FROM RFQ_COUNTERPARTY
                WHERE TP_TRADE = ?
                  AND FL_ACTIVE = 1
                  AND NM_COUNTERPARTY IS NOT NULL
                  AND NM_COUNTERPARTY <> ''
                ORDER BY NM_COUNTERPARTY
                """,
                (trade_type,),
            ).fetchall()
        return [row[0] for row in rows]
    except sqlite3.Error:
        return []


def load_counterparty_slug_map():
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            rows = conn.execute(
                """
                SELECT NM_COUNTERPARTY
                FROM RFQ_COUNTERPARTY
                WHERE FL_ACTIVE = 1
                  AND NM_COUNTERPARTY IS NOT NULL
                  AND NM_COUNTERPARTY <> ''
                ORDER BY NM_COUNTERPARTY
                """
            ).fetchall()
        return {_counterparty_slug(row[0]): row[0] for row in rows}
    except sqlite3.Error:
        return {}


def load_requesters():
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            rows = conn.execute(
                """
                SELECT NM_REQUESTER
                FROM RFQ_REQUESTERS
                WHERE NM_REQUESTER IS NOT NULL AND NM_REQUESTER <> ''
                ORDER BY NM_REQUESTER
                """
            ).fetchall()
        return [{"label": row[0], "value": row[0]} for row in rows]
    except sqlite3.Error:
        return []


def load_corporate_security_options():
    if not DATABASE_PATH.exists():
        return []
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            rows = conn.execute(
                """
                SELECT
                    sc.NM_SECURITY,
                    sc.NM_ISSUER,
                    sc.DT_MATURITY,
                    sc.NM_INDEX
                FROM FIXED_INCOME_SECURITY_CLASSIFICATION sc
                WHERE sc.NM_SECURITY IS NOT NULL AND TRIM(sc.NM_SECURITY) <> ''
                  AND COALESCE(sc.NM_SECTOR, '') <> 'Sovereign'
                ORDER BY sc.NM_SECURITY
                """
            ).fetchall()
        options = []
        for security, issuer, maturity, index_name in rows:
            if not security:
                continue
            label = (
                f"{security} | {issuer or '--'} | "
                f"{maturity or '--'} | {index_name or '--'}"
            )
            options.append({"label": label, "value": security})
        return options
    except sqlite3.Error:
        return []


def load_security_maturity(security_name):
    if not security_name or not DATABASE_PATH.exists():
        return None
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            row = conn.execute(
                """
                SELECT DT_MATURITY
                FROM FIXED_INCOME_SECURITY_CLASSIFICATION
                WHERE NM_SECURITY = ?
                LIMIT 1
                """,
                (security_name,),
            ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def build_rfq_columns(counterparties):
    columns = list(RFQ_BASE_COLUMNS)
    for name in counterparties:
        columns.append(
            {
                "headerName": name,
                "field": _counterparty_slug(name),
                "cellClass": "rfq-quote-cell",
                "minWidth": 110,
                "editable": True,
                "wrapText": True,
                "autoHeight": True,
                "cellRenderer": "RfqQuoteCell",
                "cellClassRules": {
                    "rfq-quote-highlight": "rfqQuoteHighlight(params)"
                },
            }
        )
    columns.append(
        {
            "headerName": "Actions",
            "field": "actions",
            "maxWidth": 90,
            "cellRenderer": "RfqActionsCell",
            "cellClass": "rfq-actions-cell",
        }
    )
    return columns


def load_rfq_rows(asset_class, counterparties, show_active=True):
    broker_map = {name: _counterparty_slug(name) for name in counterparties}
    active_flag = 1 if show_active else 0
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    RFQ_ID,
                    DT_INPUT,
                    USR_INPUT,
                    REQUESTER,
                    CLASS,
                    DIRECTION,
                    TYPE,
                    SEC_NAME,
                    DT_MATURITY,
                    QTY,
                    VOLUME,
                    "LIMIT" AS limit_value,
                    STR_RESPONSE
                FROM RFQ
                WHERE CLASS = ? AND FL_ACTIVE = ?
                ORDER BY DT_INPUT DESC, RFQ_ID DESC
                """,
                (asset_class, active_flag),
            ).fetchall()
    except sqlite3.Error:
        return []

    formatted = []
    for row in rows:
        response_data, _ = _normalize_response_entries(_parse_response(row["STR_RESPONSE"]))
        broker_fields = {field: "--" for field in broker_map.values()}
        latest_timestamp = None
        latest_by_broker = {}
        for response in response_data:
            broker_name = response.get("broker name")
            broker_response = response.get("broker_response")
            response_time = _extract_time(response.get("timestamp"))
            ts = None
            if broker_response:
                response_text = (
                    f"{broker_response}|{response_time}"
                    if response_time
                    else broker_response
                )
            else:
                response_text = "--"
            if response.get("timestamp"):
                try:
                    ts = datetime.fromisoformat(response["timestamp"])
                except ValueError:
                    ts = None
                if ts and (latest_timestamp is None or ts > latest_timestamp):
                    latest_timestamp = ts
            if broker_name in broker_map:
                existing = latest_by_broker.get(broker_name)
                if existing is None or (ts and (existing[0] is None or ts > existing[0])):
                    latest_by_broker[broker_name] = (ts, response_text)

        for broker_name, (_, response_text) in latest_by_broker.items():
            broker_fields[broker_map[broker_name]] = response_text

        if latest_timestamp:
            time_value = latest_timestamp.strftime("%H:%M")
        else:
            time_value = _extract_time(row["DT_INPUT"]) or "00:00"

        formatted.append(
            {
                "rfq_id": row["RFQ_ID"],
                "asset_class": row["CLASS"] or "",
                "time": time_value,
                "trader": row["USR_INPUT"] or "--",
                "requester": row["REQUESTER"] or "--",
                "direction": row["DIRECTION"] or "--",
                "security": row["SEC_NAME"] or "--",
                "maturity": row["DT_MATURITY"] or "--",
                "quantity": _format_quantity(row["QTY"]),
                "quantity_raw": row["QTY"],
                "volume": _format_volume(row["VOLUME"]),
                "volume_raw": row["VOLUME"],
                "quote_format": row["TYPE"] or "--",
                "client_limit": _format_limit(row["limit_value"]),
                **broker_fields,
                "actions": "draft",
            }
        )
    return formatted


RFQ_BASE_COLUMNS = [
    {"headerName": "ID", "field": "rfq_id", "hide": True},
    {"headerName": "Time", "field": "time", "maxWidth": 110},
    {"headerName": "Trader", "field": "trader", "minWidth": 100},
    {"headerName": "Quote Requester", "field": "requester", "minWidth": 120},
    {
        "headerName": "Direction",
        "field": "direction",
        "cellRenderer": "RfqDirectionBadge",
        "cellStyle": {"display": "flex", "alignItems": "center"},
        "minWidth": 50,
    },
    {"headerName": "Security", "field": "security", "minWidth": 100},
    {"headerName": "Maturity", "field": "maturity", "maxWidth": 100},
    {
        "headerName": "Quantity",
        "field": "quantity",
        "minWidth": 90,
        "cellStyle": {"textAlign": "right"},
    },
    {
        "headerName": "Volume",
        "field": "volume",
        "minWidth": 90,
        "cellStyle": {"textAlign": "right"},
    },
    {
        "headerName": "Quote Format",
        "field": "quote_format",
        "minWidth": 130,
        "cellRenderer": "RfqQuoteFormatBadge",
    },
    {
        "headerName": "Limit",
        "field": "client_limit",
        "minWidth": 80,
        "cellStyle": {"textAlign": "right"},
        "cellClassRules": {
            "rfq-limit-highlight": "rfqLimitHighlight(params)",
        },
    },
]

layout = dbc.Container(
    fluid=True,
    className="page-container rfq-page",
    children=[
        dcc.Store(
            id="rfq-form-state",
            data={"sovereign": False, "corporate": False, "derivatives": False},
        ),
        dcc.Store(id="rfq-refresh", data=0),
        dcc.Clipboard(id="rfq-copy-clipboard", style={"display": "none"}),
        dcc.Store(id="rfq-grid-copy-result"),
        dcc.Store(id="rfq-copy-text", data=""),
        html.Div(
            className="rfq-hero",
            children=[
                html.H3("UGM Request For Quote (RFQ)", className="rfq-hero-title"),
                html.Div(
                    "Create RFQs and capture broker quotes for sovereign bonds, corporate bonds, and derivatives",
                    className="rfq-hero-subtitle",
                ),
            ],
        ),
        html.Div(
            id="rfq-auth-content",
            children=[
        html.Div(
            className="rfq-tabs-card",
            children=[
                dcc.Tabs(
                    id="rfq-tabs",
                    value="sovereign",
                    parent_className="rfq-tabs",
                    className="rfq-tabs-container",
                    children=[
                        dcc.Tab(
                            label="Sovereign Bonds",
                            value="sovereign",
                            className="rfq-tab",
                            selected_className="rfq-tab rfq-tab-active",
                        ),
                        dcc.Tab(
                            label="Corporate Bonds",
                            value="corporate",
                            className="rfq-tab",
                            selected_className="rfq-tab rfq-tab-active",
                        ),
                        dcc.Tab(
                            label="Derivatives",
                            value="derivatives",
                            className="rfq-tab",
                            selected_className="rfq-tab rfq-tab-active",
                        ),
                    ],
                ),
                html.Button(
                    className="rfq-create",
                    id="rfq-create-btn",
                    children=[
                        html.Span("+", className="rfq-create-icon"),
                        html.Span("Create Sovereign RFQ"),
                    ],
                    type="button",
                ),
            ],
        ),
        html.Div(
            id="rfq-form-sovereign",
            className="rfq-form-card rfq-hidden",
            children=[
                html.Div(
                    className="rfq-form-header",
                    children=[
                        html.Div(
                            "New Sovereign Bonds RFQ",
                            className="rfq-form-title",
                        ),
                    ],
                ),
                html.Div(
                    className="rfq-form-grid",
                    children=[
                        html.Fieldset(
                            className="rfq-fieldset",
                            children=[
                                html.Legend(
                                    "Trade Type", className="rfq-fieldset-title"
                                ),
                                html.Div(
                                    className="rfq-fieldset-body",
                                    children=[
                                        html.Div(
                                            className="rfq-form-field",
                                            children=dmc.Stack(
                                                [
                                                    dmc.Text(
                                                        "Direction",
                                                        className="rfq-form-label",
                                                    ),
                                                    dmc.SegmentedControl(
                                                        data=[
                                                            {
                                                                "value": "Buy",
                                                                "label": "Buy",
                                                            },
                                                            {
                                                                "value": "Sell",
                                                                "label": "Sell",
                                                            },
                                                        ],
                                                        value="Buy",
                                                        className="rfq-segment",
                                                        fullWidth=True,
                                                        id="rfq-direction-sovereign",
                                                    ),
                                                ],
                                                align="flex-start",
                                                gap="xs",
                                            ),
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=dmc.Stack(
                                                [
                                                    dmc.Text(
                                                        "Quote Type",
                                                        className="rfq-form-label",
                                                    ),
                                                    dmc.SegmentedControl(
                                                        data=[
                                                            {
                                                                "value": "Trade",
                                                                "label": "Trade",
                                                            },
                                                            {
                                                                "value": "Mkt Color",
                                                                "label": "Mkt Color",
                                                            },
                                                        ],
                                                        value="Trade",
                                                        className="rfq-segment",
                                                        fullWidth=True,
                                                        id="rfq-quote-type-sovereign",
                                                    ),
                                                ],
                                                align="flex-start",
                                                gap="xs",
                                            ),
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Quote Requester",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Dropdown(
                                                    className="rfq-input rfq-select",
                                                    options=load_requesters(),
                                                    value=None,
                                                    placeholder="Select requester",
                                                    clearable=False,
                                                    maxHeight=420,
                                                    id="rfq-requester-sovereign",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        html.Fieldset(
                            className="rfq-fieldset",
                            children=[
                                html.Legend(
                                    "Traded Security",
                                    className="rfq-fieldset-title",
                                ),
                                html.Div(
                                    className="rfq-fieldset-body",
                                    children=[
                                        html.Div(
                                            className="rfq-form-field",
                                            children=dmc.Stack(
                                                [
                                                    dmc.Text(
                                                        "Bond Name",
                                                        className="rfq-form-label",
                                                    ),
                                                    dmc.SegmentedControl(
                                                        data=SOVEREIGN_BOND_OPTIONS,
                                                        value=DEFAULT_SOVEREIGN_BOND,
                                                        className="rfq-segment",
                                                        fullWidth=True,
                                                        id="rfq-bond-name-sovereign",
                                                    ),
                                                ],
                                                align="flex-start",
                                                gap="xs",
                                            ),
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Maturity Date",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Dropdown(
                                                    className="rfq-input rfq-select",
                                                    options=DEFAULT_SOVEREIGN_MATURITY_OPTIONS,
                                                    value=None,
                                                    placeholder="Select maturity date",
                                                    clearable=False,
                                                    maxHeight=420,
                                                    id="rfq-maturity-sovereign",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        html.Fieldset(
                            className="rfq-fieldset",
                            children=[
                                html.Legend(
                                    "Trade Definition",
                                    className="rfq-fieldset-title",
                                ),
                                html.Div(
                                    className="rfq-fieldset-body",
                                    children=[
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Quantity (optional)",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    className="rfq-input",
                                                    placeholder="e.g., 50000000",
                                                    type="text",
                                                    id="rfq-qty-sovereign",
                                                ),
                                            ],
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Volume (optional)",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    className="rfq-input",
                                                    placeholder="e.g., 1000000",
                                                    type="text",
                                                    id="rfq-volume-sovereign",
                                                ),
                                            ],
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Client Yield Limit (optional)",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    className="rfq-input",
                                                    placeholder="e.g., 6.50",
                                                    type="number",
                                                    step="0.01",
                                                    id="rfq-limit-sovereign",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
                html.Div(
                    className="rfq-form-actions",
                    children=[
                        html.Button(
                            "Cancel",
                            className="rfq-secondary-btn",
                            id="rfq-cancel-sovereign",
                            type="button",
                        ),
                        html.Button(
                            "Create RFQ",
                            className="rfq-primary-btn",
                            type="button",
                            id="rfq-submit-sovereign",
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            id="rfq-form-corporate",
            className="rfq-form-card rfq-hidden",
            children=[
                html.Div(
                    className="rfq-form-header",
                    children=[
                        html.Div(
                            "New Corporate Bonds RFQ",
                            className="rfq-form-title",
                        ),
                    ],
                ),
                html.Div(
                    className="rfq-form-grid",
                    children=[
                        html.Fieldset(
                            className="rfq-fieldset",
                            children=[
                                html.Legend(
                                    "Trade Type", className="rfq-fieldset-title"
                                ),
                                html.Div(
                                    className="rfq-fieldset-body",
                                    children=[
                                        html.Div(
                                            className="rfq-form-field",
                                            children=dmc.Stack(
                                                [
                                                    dmc.Text(
                                                        "Direction",
                                                        className="rfq-form-label",
                                                    ),
                                                    dmc.SegmentedControl(
                                                        data=[
                                                            {
                                                                "value": "Buy",
                                                                "label": "Buy",
                                                            },
                                                            {
                                                                "value": "Sell",
                                                                "label": "Sell",
                                                            },
                                                        ],
                                                        value="Buy",
                                                        className="rfq-segment",
                                                        fullWidth=True,
                                                        id="rfq-direction-corporate",
                                                    ),
                                                ],
                                                align="flex-start",
                                                gap="xs",
                                            ),
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=dmc.Stack(
                                                [
                                                    dmc.Text(
                                                        "Quote Type",
                                                        className="rfq-form-label",
                                                    ),
                                                    dmc.SegmentedControl(
                                                        data=[
                                                            {
                                                                "value": "Trade",
                                                                "label": "Trade",
                                                            },
                                                            {
                                                                "value": "Mkt Color",
                                                                "label": "Mkt Color",
                                                            },
                                                        ],
                                                        value="Trade",
                                                        className="rfq-segment",
                                                        fullWidth=True,
                                                        id="rfq-quote-type-corporate",
                                                    ),
                                                ],
                                                align="flex-start",
                                                gap="xs",
                                            ),
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Quote Requester",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Dropdown(
                                                    className="rfq-input rfq-select",
                                                    options=load_requesters(),
                                                    value=None,
                                                    placeholder="Select requester",
                                                    clearable=False,
                                                    maxHeight=420,
                                                    id="rfq-requester-corporate",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        html.Fieldset(
                            className="rfq-fieldset",
                            children=[
                                html.Legend(
                                    "Traded Security",
                                    className="rfq-fieldset-title",
                                ),
                                html.Div(
                                    className="rfq-fieldset-body",
                                    children=[
                                        html.Div(
                                            className="rfq-form-field",
                                            children=dmc.Stack(
                                                [
                                                    dmc.Text(
                                                        "Bond Type",
                                                        className="rfq-form-label",
                                                    ),
                                                    dmc.SegmentedControl(
                                                        data=[
                                                            {
                                                                "value": "Banking Instruments",
                                                                "label": "Banking Instruments",
                                                            },
                                                            {
                                                                "value": "Corporate Bonds",
                                                                "label": "Corporate Bonds",
                                                            },
                                                        ],
                                                        value="Corporate Bonds",
                                                        className="rfq-segment",
                                                        fullWidth=True,
                                                        id="rfq-bond-type-corporate",
                                                    ),
                                                ],
                                                align="flex-start",
                                                gap="xs",
                                            ),
                                        ),
                                        html.Div(
                                            id="rfq-corp-security-text-wrap",
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Security Name",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    className="rfq-input",
                                                    placeholder="e.g., CDB Banco X 1y %CDI",
                                                    type="text",
                                                    id="rfq-corp-security-text",
                                                ),
                                            ],
                                        ),
                                        html.Div(
                                            id="rfq-corp-security-dropdown-wrap",
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Security",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Dropdown(
                                                    className="rfq-input rfq-select",
                                                    options=load_corporate_security_options(),
                                                    value=None,
                                                    clearable=False,
                                                    maxHeight=420,
                                                    id="rfq-corp-security-dropdown",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        html.Fieldset(
                            className="rfq-fieldset",
                            children=[
                                html.Legend(
                                    "Trade Definition",
                                    className="rfq-fieldset-title",
                                ),
                                html.Div(
                                    className="rfq-fieldset-body",
                                    children=[
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Quantity (optional)",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    className="rfq-input",
                                                    placeholder="e.g., 20000000",
                                                    type="text",
                                                    id="rfq-qty-corporate",
                                                ),
                                            ],
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Volume (optional)",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    className="rfq-input",
                                                    placeholder="e.g., 5000000",
                                                    type="text",
                                                    id="rfq-volume-corporate",
                                                ),
                                            ],
                                        ),
                                        html.Div(
                                            className="rfq-form-field",
                                            children=[
                                                html.Div(
                                                    "Client Yield Limit (optional)",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    className="rfq-input",
                                                    placeholder="e.g., 6.50",
                                                    type="number",
                                                    step="0.01",
                                                    id="rfq-limit-corporate",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
                html.Div(
                    className="rfq-form-actions",
                    children=[
                        html.Button(
                            "Cancel",
                            className="rfq-secondary-btn",
                            id="rfq-cancel-corporate",
                            type="button",
                        ),
                        html.Button(
                            "Create RFQ",
                            className="rfq-primary-btn",
                            type="button",
                            id="rfq-submit-corporate",
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            id="rfq-form-derivatives",
            className="rfq-form-card rfq-hidden",
            children=[
                html.Div(
                    className="rfq-form-header",
                    children=[
                        html.Div(
                            [
                                html.Span("Options RFQ", className="rfq-form-title"),
                                html.Span("New Request", className="rfq-form-kicker"),
                            ],
                            className="rfq-form-title",
                        ),
                    ],
                ),
                dcc.Input(
                    id="rfq-quote-type-derivatives",
                    value="Trade",
                    type="hidden",
                ),
                dcc.Input(
                    id="rfq-qty-derivatives",
                    value="",
                    type="hidden",
                ),
                html.Div(
                    className="rfq-derivatives-grid",
                    children=[
                        html.Div(
                            className="rfq-form-field",
                            children=[
                                html.Div(
                                    "Quote Requester *",
                                    className="rfq-form-label",
                                ),
                                dcc.Dropdown(
                                    className="rfq-input rfq-select",
                                    options=load_requesters(),
                                    value=None,
                                    placeholder="Client / desk name",
                                    clearable=False,
                                    maxHeight=420,
                                    id="rfq-requester-derivatives",
                                ),
                            ],
                        ),
                        html.Div(
                            className="rfq-form-field",
                            children=[
                                html.Div(
                                    "Underlying *",
                                    className="rfq-form-label",
                                ),
                                dcc.Dropdown(
                                    className="rfq-input rfq-select",
                                    options=[
                                        {"label": "DI1", "value": "DI1"},
                                        {"label": "DOL", "value": "DOL"},
                                        {"label": "IND", "value": "IND"},
                                        {"label": "CDI", "value": "CDI"},
                                    ],
                                    value=None,
                                    placeholder="Select ticker...",
                                    clearable=False,
                                    maxHeight=420,
                                    id="rfq-bond-name-derivatives",
                                ),
                            ],
                        ),
                        html.Div(
                            className="rfq-form-field",
                            children=[
                                html.Div("Maturity *", className="rfq-form-label"),
                                html.Div(
                                    className="rfq-maturity-control",
                                    children=[
                                        dcc.Input(
                                            className="rfq-input",
                                            placeholder="e.g. 3",
                                            type="text",
                                            id="rfq-maturity-derivatives",
                                        ),
                                        dcc.Dropdown(
                                            className="rfq-input rfq-select rfq-maturity-unit",
                                            options=[
                                                {"label": "D", "value": "D"},
                                                {"label": "M", "value": "M"},
                                                {"label": "Y", "value": "Y"},
                                            ],
                                            value="M",
                                            clearable=False,
                                            searchable=False,
                                            id="rfq-maturity-unit-derivatives",
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        html.Div(
                            className="rfq-form-field",
                            children=[
                                html.Div("Notional *", className="rfq-form-label"),
                                dcc.Input(
                                    className="rfq-input",
                                    placeholder="e.g. 10,000,000",
                                    type="text",
                                    id="rfq-volume-derivatives",
                                ),
                            ],
                        ),
                        html.Div(
                            className="rfq-form-field",
                            children=[
                                html.Div("Premium *", className="rfq-form-label"),
                                dcc.Input(
                                    className="rfq-input",
                                    placeholder="e.g. 0.05",
                                    type="number",
                                    step="0.01",
                                    id="rfq-limit-derivatives",
                                ),
                            ],
                        ),
                        html.Div(
                            className="rfq-legs-header",
                            children=[
                                html.Div("Legs", className="rfq-fieldset-title"),
                                html.Button(
                                    "+ Add Leg",
                                    className="rfq-link-btn",
                                    type="button",
                                ),
                            ],
                        ),
                        html.Div(
                            className="rfq-derivatives-leg",
                            children=[
                                html.Div(
                                    className="rfq-form-field rfq-leg-side",
                                    children=[
                                        html.Div("Side", className="rfq-form-label"),
                                        dmc.SegmentedControl(
                                            data=[
                                                {"value": "Buy", "label": "Buy"},
                                                {"value": "Sell", "label": "Sell"},
                                            ],
                                            value="Buy",
                                            className="rfq-segment",
                                            fullWidth=True,
                                            id="rfq-direction-derivatives",
                                        ),
                                    ],
                                ),
                                html.Div(
                                    className="rfq-form-field rfq-leg-strategy",
                                    children=[
                                        html.Div("Strategy", className="rfq-form-label"),
                                        dcc.Dropdown(
                                            className="rfq-input rfq-select",
                                            options=[
                                                {"label": "Call", "value": "Call"},
                                                {"label": "Put", "value": "Put"},
                                                {"label": "Call Spread", "value": "Call Spread"},
                                                {"label": "Put Spread", "value": "Put Spread"},
                                            ],
                                            value="Call",
                                            clearable=False,
                                            maxHeight=420,
                                            id="rfq-strategy-derivatives",
                                        ),
                                    ],
                                ),
                                html.Div(
                                    className="rfq-form-field rfq-leg-solve-field",
                                    children=[
                                        html.Div("Strike", className="rfq-form-label"),
                                        dcc.Input(
                                            className="rfq-input",
                                            placeholder="Strike",
                                            type="number",
                                            id="rfq-strike-derivatives",
                                        ),
                                        html.Div(
                                            className="rfq-solve-option",
                                            children=[
                                                dcc.Checklist(
                                                    options=[{"label": "Solve", "value": "solve"}],
                                                    value=["solve"],
                                                    id="rfq-strike-solve-derivatives",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                                html.Div(
                                    className="rfq-form-field rfq-leg-solve-field rfq-leg-barrier",
                                    children=[
                                        html.Div("Barrier", className="rfq-form-label"),
                                        dcc.Input(
                                            className="rfq-input",
                                            placeholder="Barrier",
                                            type="number",
                                            id="rfq-barrier-derivatives",
                                        ),
                                        html.Div(
                                            className="rfq-solve-option",
                                            children=[
                                                dcc.Checklist(
                                                    options=[{"label": "Solve", "value": "solve"}],
                                                    value=[],
                                                    id="rfq-barrier-solve-derivatives",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                                html.Div(
                                    className="rfq-form-field rfq-leg-barrier-type",
                                    children=[
                                        html.Div("Barrier Type", className="rfq-form-label"),
                                        dcc.Dropdown(
                                            className="rfq-input rfq-select",
                                            options=[
                                                {"label": "No Barrier", "value": "No Barrier"},
                                                {"label": "Up-and-Out", "value": "Up-and-Out"},
                                                {"label": "Down-and-Out", "value": "Down-and-Out"},
                                                {"label": "Up-and-In", "value": "Up-and-In"},
                                                {"label": "Down-and-In", "value": "Down-and-In"},
                                            ],
                                            value="No Barrier",
                                            clearable=False,
                                            maxHeight=420,
                                            id="rfq-barrier-type-derivatives",
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
                html.Div(
                    className="rfq-form-actions",
                    children=[
                        html.Button(
                            "Cancel",
                            className="rfq-secondary-btn",
                            id="rfq-cancel-derivatives",
                            type="button",
                        ),
                        html.Button(
                            "Create RFQ",
                            className="rfq-primary-btn",
                            type="button",
                            id="rfq-submit-derivatives",
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            className="rfq-table-card",
            children=[
                html.Div(
                    className="rfq-grid-toolbar",
                    children=[
                        html.Div(
                            className="rfq-grid-toolbar-controls",
                            children=[
                                dmc.Switch(
                                    id="rfq-active-switch",
                                    className="rfq-active-switch",
                                    label="Show Active RFQs",
                                    checked=True,
                                    size="md",
                                ),
                                html.Button(
                                    "Copy Grid",
                                    id="rfq-copy-grid-btn",
                                    className="rfq-grid-copy-btn",
                                    type="button",
                                ),
                            ],
                        ),
                    ],
                ),
                dag.AgGrid(
                    id="rfq-grid",
                    className="ag-theme-alpine rfq-grid",
                    columnDefs=build_rfq_columns(
                        load_counterparties("Sovereign Bond")
                    ),
                    rowData=load_rfq_rows(
                        "Sovereign Bond",
                        load_counterparties("Sovereign Bond"),
                        show_active=True,
                    ),
                    getRowId="params.data.rfq_id",
                    defaultColDef={
                        "sortable": True,
                        "filter": True,
                        "resizable": True,
                        "editable": False,
                        "flex": 1,
                        "minWidth": 90,
                    },
                    dashGridOptions={
                        "suppressHorizontalScroll": True,
                        "singleClickEdit": True,
                        "stopEditingWhenCellsLoseFocus": True,
                    },
                    dangerously_allow_code=True,
                    style={"height": "672px"},
                ),
                dmc.NotificationContainer(
                    id="rfq-notification-container",
                    position="top-right",
                    autoClose=5000,
                    zIndex=3000,
                ),
                dmc.Notification(
                    id="rfq-error-notification",
                    action="hide",
                    title="RFQ Error",
                    message="",
                    color="red",
                    autoClose=5000,
                ),
            ],
        ),
            ],
        ),
    ],
)


@callback(
    Output("rfq-auth-content", "style"),
    Input("current-user", "data"),
)
def toggle_rfq_auth_content(current_user):
    if current_user and current_user.get("user"):
        return {"display": "block"}
    return {"display": "none"}


@callback(
    Output("rfq-form-state", "data"),
    Input("rfq-create-btn", "n_clicks"),
    Input("rfq-cancel-sovereign", "n_clicks"),
    Input("rfq-cancel-corporate", "n_clicks"),
    Input("rfq-cancel-derivatives", "n_clicks"),
    Input("rfq-tabs", "value"),
    State("rfq-tabs", "value"),
    State("rfq-form-state", "data"),
)
def toggle_rfq_form(
    create_clicks,
    cancel_sovereign,
    cancel_corporate,
    cancel_derivatives,
    active_tab_change,
    active_tab,
    form_state,
):
    form_state = form_state or {
        "sovereign": False,
        "corporate": False,
        "derivatives": False,
    }
    if not ctx.triggered_id:
        return form_state
    if ctx.triggered_id == "rfq-tabs":
        return {
            "sovereign": False,
            "corporate": False,
            "derivatives": False,
        }
    if ctx.triggered_id == "rfq-create-btn":
        form_state[active_tab] = True
    elif ctx.triggered_id == "rfq-cancel-sovereign":
        form_state["sovereign"] = False
    elif ctx.triggered_id == "rfq-cancel-corporate":
        form_state["corporate"] = False
    elif ctx.triggered_id == "rfq-cancel-derivatives":
        form_state["derivatives"] = False
    return form_state


@callback(
    Output("rfq-form-sovereign", "className"),
    Output("rfq-form-corporate", "className"),
    Output("rfq-form-derivatives", "className"),
    Input("rfq-form-state", "data"),
)
def set_rfq_form_classes(form_state):
    form_state = form_state or {}
    base = "rfq-form-card"
    return (
        base if form_state.get("sovereign") else f"{base} rfq-hidden",
        base if form_state.get("corporate") else f"{base} rfq-hidden",
        base if form_state.get("derivatives") else f"{base} rfq-hidden",
    )


@callback(
    Output("rfq-corp-security-text-wrap", "style"),
    Output("rfq-corp-security-dropdown-wrap", "style"),
    Input("rfq-bond-type-corporate", "value"),
)
def toggle_corporate_security_input(bond_type):
    if bond_type == "Banking Instruments":
        return {"display": "block"}, {"display": "none"}
    return {"display": "none"}, {"display": "block"}


@callback(
    Output("rfq-requester-sovereign", "disabled"),
    Output("rfq-requester-sovereign", "value"),
    Input("rfq-quote-type-sovereign", "value"),
    State("rfq-requester-sovereign", "value"),
)
def toggle_sovereign_requester(quote_type, current_value):
    if quote_type == "Trade":
        return True, None
    return False, current_value


@callback(
    Output("rfq-requester-corporate", "disabled"),
    Output("rfq-requester-corporate", "value"),
    Input("rfq-quote-type-corporate", "value"),
    State("rfq-requester-corporate", "value"),
)
def toggle_corporate_requester(quote_type, current_value):
    if quote_type == "Trade":
        return True, None
    return False, current_value


@callback(
    Output("rfq-maturity-sovereign", "options"),
    Output("rfq-maturity-sovereign", "value"),
    Input("rfq-bond-name-sovereign", "value"),
    State("rfq-maturity-sovereign", "value"),
)
def update_sovereign_maturity_dropdown(bond_name, current_value):
    options = load_sovereign_maturity_options(bond_name)
    if not options:
        return [], None
    valid_values = {item["value"] for item in options}
    if current_value in valid_values:
        return options, current_value
    return options, None


@callback(
    Output("rfq-grid", "rowData"),
    Output("rfq-create-btn", "children"),
    Output("rfq-grid", "columnDefs"),
    Input("rfq-tabs", "value"),
    Input("rfq-refresh", "data"),
    Input("rfq-active-switch", "checked"),
)
def switch_rfq_tab(active_tab, _refresh, show_active):
    class_map = {
        "sovereign": ("Sovereign Bond", "Sovereign Bond"),
        "corporate": ("Corporate Bond", "Corporate Bond"),
        "derivatives": ("Derivatives", "Derivative"),
    }
    rfq_class, counterparty_class = class_map.get(
        active_tab, ("Sovereign Bond", "Sovereign Bond")
    )
    counterparties = load_counterparties(counterparty_class)
    if active_tab == "corporate":
        rows = load_rfq_rows(rfq_class, counterparties, show_active=bool(show_active))
        label = "Create Corporate RFQ"
    elif active_tab == "derivatives":
        rows = load_rfq_rows(rfq_class, counterparties, show_active=bool(show_active))
        label = "Create Derivatives RFQ"
    else:
        rows = load_rfq_rows(rfq_class, counterparties, show_active=bool(show_active))
        label = "Create Sovereign RFQ"
    columns = build_rfq_columns(counterparties)
    if not show_active:
        for col in columns:
            if col.get("field", "").startswith("cp_"):
                col["editable"] = False
    return rows, [html.Span("+", className="rfq-create-icon"), html.Span(label)], columns


@callback(
    Output("rfq-refresh", "data", allow_duplicate=True),
    Output("rfq-notification-container", "sendNotifications", allow_duplicate=True),
    Input("rfq-submit-sovereign", "n_clicks"),
    Input("rfq-submit-corporate", "n_clicks"),
    Input("rfq-submit-derivatives", "n_clicks"),
    State("rfq-direction-sovereign", "value"),
    State("rfq-quote-type-sovereign", "value"),
    State("rfq-bond-name-sovereign", "value"),
    State("rfq-maturity-sovereign", "value"),
    State("rfq-qty-sovereign", "value"),
    State("rfq-volume-sovereign", "value"),
    State("rfq-limit-sovereign", "value"),
    State("rfq-requester-sovereign", "value"),
    State("rfq-direction-corporate", "value"),
    State("rfq-quote-type-corporate", "value"),
    State("rfq-bond-type-corporate", "value"),
    State("rfq-corp-security-text", "value"),
    State("rfq-corp-security-dropdown", "value"),
    State("rfq-qty-corporate", "value"),
    State("rfq-volume-corporate", "value"),
    State("rfq-limit-corporate", "value"),
    State("rfq-requester-corporate", "value"),
    State("rfq-direction-derivatives", "value"),
    State("rfq-quote-type-derivatives", "value"),
    State("rfq-bond-name-derivatives", "value"),
    State("rfq-maturity-derivatives", "value"),
    State("rfq-maturity-unit-derivatives", "value"),
    State("rfq-qty-derivatives", "value"),
    State("rfq-volume-derivatives", "value"),
    State("rfq-limit-derivatives", "value"),
    State("rfq-requester-derivatives", "value"),
    State("current-user", "data"),
    State("rfq-refresh", "data"),
    prevent_initial_call=True,
)
def submit_rfq(
    sovereign_clicks,
    corporate_clicks,
    derivatives_clicks,
    direction_sovereign,
    quote_type_sovereign,
    bond_name_sovereign,
    maturity_sovereign,
    qty_sovereign,
    volume_sovereign,
    limit_sovereign,
    requester_sovereign,
    direction_corporate,
    quote_type_corporate,
    bond_type_corporate,
    corp_security_text,
    corp_security_dropdown,
    qty_corporate,
    volume_corporate,
    limit_corporate,
    requester_corporate,
    direction_derivatives,
    quote_type_derivatives,
    bond_name_derivatives,
    maturity_derivatives,
    maturity_unit_derivatives,
    qty_derivatives,
    volume_derivatives,
    limit_derivatives,
    requester_derivatives,
    current_user,
    refresh_value,
):
    if not ctx.triggered_id:
        raise PreventUpdate

    if ctx.triggered_id == "rfq-submit-sovereign":
        asset_class = "Sovereign Bond"
        direction = direction_sovereign
        quote_type = quote_type_sovereign
        bond_name = maturity_sovereign
        maturity = load_security_maturity(maturity_sovereign)
        qty = qty_sovereign
        volume = volume_sovereign
        limit_value = limit_sovereign
        requester = requester_sovereign
    elif ctx.triggered_id == "rfq-submit-corporate":
        asset_class = "Corporate Bond"
        direction = direction_corporate
        quote_type = quote_type_corporate
        if bond_type_corporate == "Banking Instruments":
            bond_name = corp_security_text
            maturity = None
        else:
            bond_name = corp_security_dropdown
            maturity = load_security_maturity(corp_security_dropdown)
        qty = qty_corporate
        volume = volume_corporate
        limit_value = limit_corporate
        requester = requester_corporate
    elif ctx.triggered_id == "rfq-submit-derivatives":
        asset_class = "Derivatives"
        direction = direction_derivatives
        quote_type = quote_type_derivatives
        bond_name = bond_name_derivatives
        maturity = (
            f"{str(maturity_derivatives).strip()}{maturity_unit_derivatives}"
            if maturity_derivatives not in (None, "")
            else maturity_derivatives
        )
        qty = qty_derivatives
        volume = volume_derivatives
        limit_value = limit_derivatives
        requester = requester_derivatives
    else:
        raise PreventUpdate

    security_text = str(bond_name).strip() if bond_name is not None else ""
    maturity_text = str(maturity).strip() if maturity not in (None, "") else ""
    requester_text = str(requester).strip() if requester not in (None, "") else ""
    qty_text = str(qty).strip() if qty not in (None, "") else ""
    volume_text = str(volume).strip() if volume not in (None, "") else ""
    limit_text = str(limit_value).strip() if limit_value not in (None, "") else ""

    qty_parsed = _parse_quantity(qty)
    volume_parsed = _parse_number(volume)
    limit_parsed = _parse_number(limit_value)

    validation_errors = []

    if not security_text:
        validation_errors.append("Security must be filled.")

    if (
        asset_class in ("Sovereign Bond", "Corporate Bond")
        and quote_type == "Mkt Color"
        and not requester_text
    ):
        validation_errors.append("Quote requester must be selected.")

    if asset_class == "Derivatives" and not requester_text:
        validation_errors.append("Quote requester must be selected.")

    if asset_class in ("Sovereign Bond", "Derivatives") and not maturity_text:
        validation_errors.append("Maturity date must be selected.")

    if asset_class == "Derivatives" and not limit_text:
        validation_errors.append("Premium must be filled.")

    if not qty_text and not volume_text:
        if asset_class == "Derivatives":
            validation_errors.append("Fill Notional before creating the RFQ.")
        else:
            validation_errors.append(
                "Fill Quantity or Volume before creating the RFQ."
            )

    if qty_text and qty_parsed is None:
        validation_errors.append("Quantity must be a positive number or empty.")
    elif qty_parsed is not None and qty_parsed <= 0:
        validation_errors.append("Quantity must be greater than zero.")

    if volume_text and volume_parsed is None:
        volume_label = "Notional" if asset_class == "Derivatives" else "Volume"
        validation_errors.append(f"{volume_label} must be a positive number or empty.")
    elif volume_parsed is not None and volume_parsed <= 0:
        volume_label = "Notional" if asset_class == "Derivatives" else "Volume"
        validation_errors.append(f"{volume_label} must be greater than zero.")

    if limit_text and limit_parsed is None:
        limit_label = "Premium" if asset_class == "Derivatives" else "Yield limit"
        validation_errors.append(f"{limit_label} must be a positive number or empty.")
    elif limit_parsed is not None and limit_parsed <= 0:
        limit_label = "Premium" if asset_class == "Derivatives" else "Yield limit"
        validation_errors.append(f"{limit_label} must be greater than zero.")

    if validation_errors:
        notification_prefix = datetime.now().strftime("%Y%m%d%H%M%S%f")
        notifications = [
            {
                "id": f"rfq-submit-error-{notification_prefix}-{idx}",
                "action": "show",
                "title": "RFQ Validation Error",
                "message": message,
                "color": "red",
                "autoClose": 5000,
                "withCloseButton": True,
                "position": "top-right",
            }
            for idx, message in enumerate(validation_errors)
        ]
        return (
            no_update,
            notifications,
        )

    dt_input = datetime.now().isoformat(timespec="minutes")
    usr_input = (current_user or {}).get("user") or "Unknown User"
    if asset_class in ("Corporate Bond", "Sovereign Bond"):
        requester = requester_text if quote_type == "Mkt Color" else None
    else:
        requester = requester_text

    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.execute(
                """
                INSERT INTO RFQ (
                    DT_INPUT,
                    USR_INPUT,
                    REQUESTER,
                    CLASS,
                    DIRECTION,
                    TYPE,
                    SEC_NAME,
                    DT_MATURITY,
                    QTY,
                    VOLUME,
                    "LIMIT",
                    STR_RESPONSE,
                    FL_ACTIVE
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dt_input,
                    usr_input,
                    requester,
                    asset_class,
                    direction,
                    quote_type,
                    bond_name,
                    maturity,
                    qty_parsed,
                    volume_parsed,
                    limit_parsed,
                    None,
                    1,
                ),
            )
            conn.commit()
    except sqlite3.Error:
        return (
            no_update,
            [
                {
                    "id": f"rfq-submit-error-db-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                    "action": "show",
                    "title": "RFQ Error",
                    "message": "Unable to create RFQ. Try again.",
                    "color": "red",
                    "autoClose": 5000,
                    "withCloseButton": True,
                    "position": "top-right",
                }
            ],
        )

    return (refresh_value or 0) + 1, no_update


@callback(
    Output("rfq-refresh", "data", allow_duplicate=True),
    Output("rfq-error-notification", "message"),
    Output("rfq-error-notification", "action"),
    Input("rfq-grid", "cellValueChanged"),
    State("rfq-active-switch", "checked"),
    State("current-user", "data"),
    State("rfq-refresh", "data"),
    prevent_initial_call=True,
)
def append_broker_response(event, show_active, current_user, refresh_value):
    if not event:
        raise PreventUpdate
    if not show_active:
        raise PreventUpdate
    if isinstance(event, list):
        if not event:
            raise PreventUpdate
        event = event[-1]

    col_id = event.get("colId") or event.get("columnId")
    broker_map = load_counterparty_slug_map()
    if not col_id or col_id not in broker_map:
        raise PreventUpdate

    row_data = event.get("data") or {}
    rfq_id = row_data.get("rfq_id")
    if not rfq_id:
        raise PreventUpdate

    new_value = event.get("newValue", event.get("value"))

    broker_name = broker_map[col_id]

    try:
        with sqlite3.connect(DATABASE_PATH, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                'SELECT STR_RESPONSE FROM RFQ WHERE RFQ_ID = ?',
                (rfq_id,),
            ).fetchone()
            existing, responses_changed = _normalize_response_entries(
                _parse_response(row["STR_RESPONSE"] if row else None)
            )
            target_idx = None
            for idx in range(len(existing) - 1, -1, -1):
                if existing[idx].get("broker name") == broker_name:
                    target_idx = idx
                    break

            if new_value in (None, "", "--"):
                if target_idx is None:
                    if responses_changed:
                        conn.execute(
                            'UPDATE RFQ SET STR_RESPONSE = ? WHERE RFQ_ID = ?',
                            (json.dumps(existing), rfq_id),
                        )
                        conn.commit()
                    return (refresh_value or 0) + 1, no_update, "hide"
                existing.pop(target_idx)
                conn.execute(
                    'UPDATE RFQ SET STR_RESPONSE = ? WHERE RFQ_ID = ?',
                    (json.dumps(existing), rfq_id),
                )
                conn.commit()
                return (refresh_value or 0) + 1, no_update, "hide"

            value_text = str(new_value).strip()
            has_separator = "|" in value_text
            if has_separator:
                cleaned_response, timestamp, invalid_time = _split_response_time(
                    value_text
                )
                if not cleaned_response:
                    message = "Quote value is not in a valid format."
                    return (refresh_value or 0) + 1, message, "show"
                if invalid_time or not timestamp:
                    message = "Invalid timestamp. Use HH:MM."
                    return (refresh_value or 0) + 1, message, "show"
                new_entry = {
                    "broker name": broker_name,
                    "broker_response": cleaned_response,
                    "timestamp": timestamp,
                }
            else:
                if target_idx is not None:
                    message = "Use format: quote|HH:MM."
                    return (refresh_value or 0) + 1, message, "show"
                cleaned_response = value_text.replace("|", " ").strip()
                if not cleaned_response:
                    message = "Quote value is not in a valid format."
                    return (refresh_value or 0) + 1, message, "show"
                timestamp = datetime.now().isoformat(timespec="minutes")
                new_entry = {
                    "broker name": broker_name,
                    "broker_response": cleaned_response,
                    "timestamp": timestamp,
                }

            if target_idx is None:
                existing.append(new_entry)
            else:
                existing[target_idx] = new_entry
            conn.execute(
                'UPDATE RFQ SET STR_RESPONSE = ? WHERE RFQ_ID = ?',
                (json.dumps(existing), rfq_id),
            )
            conn.commit()
    except sqlite3.Error:
        message = "Unable to update quote. Try again."
        return (refresh_value or 0) + 1, message, "show"

    return (refresh_value or 0) + 1, no_update, "hide"


@callback(
    Output("rfq-refresh", "data", allow_duplicate=True),
    Output("rfq-copy-clipboard", "content"),
    Output("rfq-copy-clipboard", "n_clicks"),
    Input("rfq-grid", "cellRendererData"),
    State("rfq-grid", "rowData"),
    State("rfq-active-switch", "checked"),
    State("current-user", "data"),
    State("rfq-refresh", "data"),
    State("rfq-copy-clipboard", "n_clicks"),
    prevent_initial_call=True,
)
def handle_rfq_action(
    event,
    row_data,
    show_active,
    current_user,
    refresh_value,
    copy_clicks,
):
    if not event:
        raise PreventUpdate
    if not show_active:
        raise PreventUpdate
    payload = event.get("value") if isinstance(event, dict) else event
    if not isinstance(payload, dict):
        raise PreventUpdate
    action = payload.get("action")
    rfq_id = payload.get("rfq_id")
    if not action or rfq_id is None:
        raise PreventUpdate

    if action == "close":
        usr_closed = (current_user or {}).get("user") or "Unknown User"
        dt_closed = datetime.now().isoformat(timespec="minutes")
        try:
            with sqlite3.connect(DATABASE_PATH) as conn:
                conn.execute(
                    """
                    UPDATE RFQ
                    SET FL_ACTIVE = 0, USR_CLOSED = ?, DT_CLOSED = ?
                    WHERE RFQ_ID = ?
                    """,
                    (usr_closed, dt_closed, rfq_id),
                )
                conn.commit()
        except sqlite3.Error:
            raise PreventUpdate
        return (refresh_value or 0) + 1, no_update, no_update

    if action != "copy":
        raise PreventUpdate

    if not row_data:
        raise PreventUpdate
    target = None
    for row in row_data:
        if str(row.get("rfq_id")) == str(rfq_id):
            target = row
            break
    if not target:
        raise PreventUpdate

    copy_text = _build_rfq_text(target)
    if not copy_text:
        raise PreventUpdate
    return no_update, copy_text, (copy_clicks or 0) + 1


clientside_callback(
    ClientsideFunction(namespace="rfq", function_name="copyGridToClipboard"),
    Output("rfq-grid-copy-result", "data"),
    Input("rfq-copy-grid-btn", "n_clicks"),
    State("rfq-grid", "rowData"),
    State("rfq-grid", "columnDefs"),
    prevent_initial_call=True,
)
