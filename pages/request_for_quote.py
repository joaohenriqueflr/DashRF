from contextlib import closing
import json
import hashlib
import logging
import math
from dataclasses import dataclass
import re
import sqlite3
from uuid import uuid4
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

from services.openai_client import AIConfigurationError, AIError, OpenAIService, build_structured_request
from services.ai_audit import AIRequestLog, FormEventLog
from services.prompts import PromptStore

register_page(__name__, path="/rfq", name="UGM Request for Quote")

BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "data" / "fixed_income.db"
logger = logging.getLogger(__name__)
RFQ_AI_MAX_CHARS = 200
FIXED_INCOME_QUOTE_TYPE = "Mkt Color"


RFQ_FIELDS = {
    "sovereign": {
        "direction": "rfq-direction-sovereign",
        "requester": "rfq-requester-sovereign",
        "bond_name": "rfq-bond-name-sovereign",
        "maturity": "rfq-maturity-sovereign",
        "quantity": "rfq-qty-sovereign",
        "volume": "rfq-volume-sovereign",
        "yield_limit": "rfq-limit-sovereign",
    },
    "corporate": {
        "direction": "rfq-direction-corporate",
        "requester": "rfq-requester-corporate",
        "bond_type": "rfq-bond-type-corporate",
        "security_text": "rfq-corp-security-text",
        "security": "rfq-corp-security-dropdown",
        "quantity": "rfq-qty-corporate",
        "volume": "rfq-volume-corporate",
        "yield_limit": "rfq-limit-corporate",
    },
    "derivatives": {
        "requester": "rfq-requester-derivatives",
        "security": "rfq-bond-name-derivatives",
        "maturity": "rfq-maturity-derivatives",
        "maturity_unit": "rfq-maturity-unit-derivatives",
        "volume": "rfq-volume-derivatives",
        "premium": "rfq-limit-derivatives",
        "direction": "rfq-direction-derivatives",
        "strategy": "rfq-strategy-derivatives",
        "strike": "rfq-strike-derivatives",
        "strike_solve": "rfq-strike-solve-derivatives",
        "barrier": "rfq-barrier-derivatives",
        "barrier_solve": "rfq-barrier-solve-derivatives",
        "barrier_type": "rfq-barrier-type-derivatives",
    },
}

FIELD_LABELS = {
    "direction": "buy or sell direction", "quote_type": "quote type",
    "requester": "quote requester", "bond_name": "sovereign bond type",
    "maturity": "maturity", "quantity": "quantity", "volume": "volume / notional",
    "yield_limit": "yield limit", "bond_type": "corporate instrument category",
    "security_text": "banking instrument description", "security": "security",
    "maturity_unit": "maturity unit (D, M, or Y)", "premium": "premium",
    "strategy": "option strategy", "strike": "strike or Solve",
    "strike_solve": "whether to solve for strike", "barrier": "barrier or Solve",
    "barrier_solve": "whether to solve for barrier", "barrier_type": "barrier type",
    "quantity_or_volume": "quantity or volume",
}

NUMERIC_FIELDS = {"quantity", "volume", "yield_limit", "premium", "strike", "barrier"}

SOLVE_FIELDS = {"strike_solve", "barrier_solve"}


def _sovereign_security_type(security_name):
    if not security_name:
        return ""
    return str(security_name).strip().split(" ", 1)[0]


def _sovereign_maturity_label(security_name):
    if not security_name:
        return ""
    parts = str(security_name).strip().split(" ", 1)
    return parts[1] if len(parts) > 1 else parts[0]


def load_sovereign_assets():
    """Read the full sovereign asset-class/maturity list with one SQLite query."""
    if not DATABASE_PATH.exists():
        return []
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT NM_SECURITY, DT_MATURITY
                FROM FIXED_INCOME_SECURITY_CLASSIFICATION
                WHERE NM_SECTOR = 'Sovereign'
                  AND NM_SECURITY IS NOT NULL AND TRIM(NM_SECURITY) <> ''
                  AND DT_MATURITY IS NOT NULL AND TRIM(DT_MATURITY) <> ''
                ORDER BY DT_MATURITY, NM_SECURITY
                """
            ).fetchall()
        return [
            {"asset_class": _sovereign_security_type(security),
             "maturity": maturity, "security": security}
            for security, maturity in rows
        ]
    except sqlite3.Error:
        return []


def sovereign_form_options(assets):
    """Build both controls from one asset list; filtering never accesses SQLite."""
    priority = {"LFT": 0, "NTN-B": 1, "NTN-F": 2, "LTN": 3, "NTN-C": 4}
    names = sorted({asset["asset_class"] for asset in assets or []},
                   key=lambda name: (priority.get(name, len(priority)), name))
    bonds = [{"label": name, "value": name} for name in names]
    maturities = {name: [] for name in names}
    for asset in sorted(assets or [], key=lambda item: (item["maturity"], item["security"])):
        maturities[asset["asset_class"]].append({
            "label": _sovereign_maturity_label(asset["security"]),
            "value": asset["security"],
        })
    return bonds, maturities


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
    number = _parse_number(value)
    return int(number) if number is not None and number.is_integer() else None


def _parse_number(value):
    if value in (None, "", "--"):
        return None
    if isinstance(value, bool):
        return None
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return None
    try:
        number = float(cleaned)
        return number if math.isfinite(number) else None
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
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
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
PromptStore(DATABASE_PATH).ensure_schema()
AIRequestLog(DATABASE_PATH).ensure_schema()
FormEventLog(DATABASE_PATH).ensure_schema()



def load_counterparties(trade_type):
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
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
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
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


def load_corporate_security_options():
    if not DATABASE_PATH.exists():
        return []
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
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
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
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
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
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

RFQ_RULES = {
    "sovereign": {
        "required": ["direction", "bond_name", "maturity"],
        "one_of": [["quantity", "volume"]],
        "conditional": [],
    },
    "corporate": {
        "required": ["direction", "bond_type"],
        "one_of": [["quantity", "volume"]],
        "conditional": [
            {"when": {"bond_type": "Banking Instruments"}, "required": ["security_text"]},
            {"when": {"bond_type": "Corporate Bonds"}, "required": ["security"]},
        ],
    },
    "derivatives": {
        "required": ["direction", "security", "maturity", "maturity_unit", "volume", "premium", "strategy", "barrier_type"],
        "one_of": [],
        "conditional": [
            {"when": {"strike_solve": False}, "required": ["strike"]},
            {"when": {"barrier_solve": False}, "unless": {"barrier_type": "No Barrier"}, "required": ["barrier"]},
        ],
    },
}


def rfq_components():
    """The actual form controls supply defaults and fixed option lists."""
    found = {}

    def visit(component):
        if isinstance(component, (list, tuple)):
            for child in component:
                visit(child)
        elif hasattr(component, "to_plotly_json"):
            if getattr(component, "id", None):
                found[component.id] = component
            visit(getattr(component, "children", None))

    visit(layout)
    return found


def rfq_empty_form(asset_class, username):
    """Restore the blank form, keeping the signed-in requester identity."""
    components = rfq_components()
    values = {}
    for name, component_id in RFQ_FIELDS[asset_class].items():
        value = getattr(components[component_id], "value", None)
        values[name] = ("solve" in (value or [])) if name in SOLVE_FIELDS else value
    values["requester"] = username
    return values


def rfq_json_contract(asset_class, username, *, sovereign_assets=None):
    """Build one JSON contract per form from its controls and existing loaders."""
    components = rfq_components()
    defaults = rfq_empty_form(asset_class, username)
    options = {}
    for name, component_id in RFQ_FIELDS[asset_class].items():
        component = components[component_id]
        if name not in SOLVE_FIELDS:
            choices = getattr(component, "options", getattr(component, "data", None))
            if choices is not None:
                options[name] = choices

    maturity_by_bond = {}
    if asset_class == "sovereign":
        assets = load_sovereign_assets() if sovereign_assets is None else sovereign_assets
        options["bond_name"], maturity_by_bond = sovereign_form_options(assets)
        options["maturity"] = [option for group in maturity_by_bond.values() for option in group]
    elif asset_class == "corporate":
        options["security"] = load_corporate_security_options()

    properties = {}
    definitions = {}
    for name, component_id in RFQ_FIELDS[asset_class].items():
        kind = "string"
        if name in NUMERIC_FIELDS or (asset_class == "derivatives" and name == "maturity"):
            kind = "integer" if name == "quantity" else "number"
        elif name in SOLVE_FIELDS:
            kind = "boolean"
        spec = {"type": [kind, "null"]}
        if kind in ("integer", "number"):
            spec["exclusiveMinimum"] = 0
        if name in options:
            spec["enum"] = [item["value"] if isinstance(item, dict) else item for item in options[name]] + [None]
        if name == "requester":
            spec = {"type": "string", "enum": [username]}
        properties[name] = spec
        definitions[name] = {
            "component_id": component_id, "type": kind, "default": defaults[name],
            "required": name == "requester" or name in RFQ_RULES[asset_class]["required"],
            "optional": name != "requester" and name not in RFQ_RULES[asset_class]["required"],
            "options": options.get(name), "read_only": name == "requester",
        }
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["asset_class", "is_rfq_request", "request_count", "has_conflicts", "fields", "unresolved_fields"],
        "properties": {
            "asset_class": {"type": "string", "enum": [asset_class]},
            "is_rfq_request": {"type": "boolean"},
            "request_count": {"type": "integer"},
            "has_conflicts": {"type": "boolean"},
            "fields": {"type": "object", "additionalProperties": False,
                       "required": list(properties), "properties": properties},
            "unresolved_fields": {"type": "array", "items": {"type": "string", "enum": list(properties)}},
        },
    }
    return {"asset_class": asset_class, "output_schema": schema,
            "field_definitions": definitions, "validation_rules": RFQ_RULES[asset_class],
            "fixed_values": {"quote_type": FIXED_INCOME_QUOTE_TYPE} if asset_class in ("sovereign", "corporate") else {},
            "maturity_options_by_bond": maturity_by_bond}


def rfq_missing_fields(asset_class, values):
    """Use the same required/optional rules for AI and manual submission."""
    rules = RFQ_RULES[asset_class]
    required = list(rules["required"])
    for rule in rules["conditional"]:
        matches = all(values.get(name) == value for name, value in rule["when"].items())
        excluded = rule.get("unless") and all(values.get(name) == value for name, value in rule["unless"].items())
        if matches and not excluded:
            required.extend(rule["required"])
    missing = [name for name in required if values.get(name) in (None, "")]
    for group in rules["one_of"]:
        if not any(values.get(name) not in (None, "") for name in group):
            missing.append("quantity_or_volume")
    return list(dict.fromkeys(missing))


@dataclass(frozen=True)
class RFQDraft:
    fields: dict
    missing_fields: list
    metadata: dict
    relevant: bool = True
    ambiguous: bool = False


def interpret_rfq_json(asset_class, data, username, contract):
    """Validate model JSON against the same option values rendered in the form."""
    values = rfq_empty_form(asset_class, username)
    expected = set(RFQ_FIELDS[asset_class])
    if (not isinstance(data, dict)
            or set(data) != {"asset_class", "is_rfq_request", "request_count", "has_conflicts", "fields", "unresolved_fields"}
            or data.get("asset_class") != asset_class
            or not isinstance(data.get("is_rfq_request"), bool)
            or type(data.get("request_count")) is not int or data["request_count"] < 0
            or not isinstance(data.get("has_conflicts"), bool)
            or not isinstance(data.get("fields"), dict) or set(data["fields"]) != expected
            or not isinstance(data.get("unresolved_fields"), list)
            or any(not isinstance(name, str) or name not in expected for name in data["unresolved_fields"])):
        raise AIError("The AI returned invalid RFQ JSON. Please try again.")
    if not data["is_rfq_request"]:
        return values, [], False, False
    if data["request_count"] != 1 or data["has_conflicts"]:
        return values, [], True, True
    unresolved = set(data["unresolved_fields"]) - {"requester"}
    properties = contract["output_schema"]["properties"]["fields"]["properties"]
    for name, value in data["fields"].items():
        if name == "requester" or value is None or name in unresolved:
            continue
        kind = contract["field_definitions"][name]["type"]
        valid = True
        if kind in ("number", "integer"):
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            if valid:
                parsed = _parse_quantity(value) if kind == "integer" else _parse_number(value)
                valid = parsed is not None
                value = parsed
        elif kind == "boolean":
            valid = isinstance(value, bool)
        else:
            valid = isinstance(value, str) and bool(value.strip()) and len(value) <= 200
        if valid and "enum" in properties[name]:
            valid = value in properties[name]["enum"]
        if valid:
            values[name] = value
        else:
            unresolved.add(name)
    # Resolve the bond from the loader's grouping rather than parsing the name again.
    if asset_class == "sovereign" and values.get("maturity"):
        bond = next((name for name, choices in contract["maturity_options_by_bond"].items()
                     if any(option["value"] == values["maturity"] for option in choices)), None)
        if values.get("bond_name") not in (None, bond):
            values["maturity"] = None
            unresolved.add("maturity")
        else:
            values["bond_name"] = bond
    for name in unresolved:
        values[name] = None if name not in SOLVE_FIELDS else False
    missing = list(dict.fromkeys(sorted(unresolved) + rfq_missing_fields(asset_class, values)))
    return values, missing, True, False


def extract_rfq(asset_class, text, username, *, service=None, sovereign_assets=None):
    if asset_class not in RFQ_FIELDS or not username:
        raise AIError("Sign in before filling an RFQ.")
    if not isinstance(text, str) or not text.strip():
        raise AIError("Describe one complete RFQ to fill.")
    # HTML maxlength and the browser counter both count UTF-16 code units.
    if len(text.encode("utf-16-le", errors="surrogatepass")) // 2 > RFQ_AI_MAX_CHARS:
        raise AIError(f"Keep the RFQ description within {RFQ_AI_MAX_CHARS} characters.")
    contract = rfq_json_contract(asset_class, username, sovereign_assets=sovereign_assets)
    prompt = PromptStore(DATABASE_PATH).get_active("rfq", f"fill_{asset_class}")
    payload = {"request_text": text.strip(), "requester_username": username, "form_contract": contract}
    request = build_structured_request(prompt=prompt, payload=payload,
                                       schema=contract["output_schema"], schema_name=f"rfq_{asset_class}")
    audit = AIRequestLog(DATABASE_PATH)
    log_id = audit.begin(
        feature="rfq", form_type=asset_class, username=username, text=text, prompt=prompt, full_prompt=request,
    )
    result = None
    try:
        result = (service or OpenAIService()).extract(
            prompt=prompt, schema=contract["output_schema"], schema_name=f"rfq_{asset_class}",
            payload=payload,
        )
        fields, missing, relevant, ambiguous = interpret_rfq_json(asset_class, result.data, username, contract)
    except Exception as exc:
        if isinstance(exc, AIError):
            exc.ai_log_id = log_id
        invalid_response = result is not None or getattr(exc, "error_type", None) in {"InvalidJSON", "InvalidJSONShape"}
        audit.finish(log_id, status="invalid_response" if invalid_response else "failed", result=result, error=exc)
        raise
    audit.finish(log_id, status="completed", result=result)
    return RFQDraft(fields, missing, {
        "log_id": log_id,
        "prompt_id": result.prompt_id, "prompt_version": result.prompt_version,
        "model": result.model, "response_id": result.response_id,
    }, relevant, ambiguous)


def log_rfq_form_event(message, *, source, form_type=None, current_user=None, level="error",
                       ai_log_id=None, rfq_id=None, context=None, alert_type=None):
    """Record user-facing warnings/errors after releasing business DB locks."""
    try:
        return FormEventLog(DATABASE_PATH).record(
            feature="rfq", form_type=form_type, username=(current_user or {}).get("user") or "",
            level=level, source=source, message=message, ai_log_id=ai_log_id,
            rfq_id=rfq_id, context=context, alert_type=alert_type,
        )
    except sqlite3.Error as exc:
        # A database outage must not hide the original alert from the user.
        logger.error("Unable to save RFQ form event (%s)", type(exc).__name__)
        return None


def rfq_alert_text(component):
    if component is None:
        return ""
    if isinstance(component, (list, tuple)):
        return " ".join(filter(None, (rfq_alert_text(child) for child in component)))
    if hasattr(component, "children"):
        return rfq_alert_text(component.children)
    return str(component)


class RFQQuoteValidationError(ValueError):
    """A quote validation alert that must be logged outside the transaction."""


def rfq_ai_panel(asset_class):
    examples = {
        "sovereign": "Buy 500k LTN maturing January 2028, yield limit 12.5%. Use 500q for 500 units.",
        "corporate": "Sell 2mm of [security code], yield limit 13%. Use 25q for 25 units.",
        "derivatives": "Buy a 3-month DOL call, notional 10 million, premium 0.05, solve for strike, no barrier.",
    }
    return html.Div(
        id=f"rfq-ai-panel-{asset_class}",
        className="rfq-ai-panel",
        **{"aria-busy": "false"},
        children=[
            dcc.Store(id=f"rfq-ai-result-{asset_class}"),
            html.Div(
                className="rfq-ai-heading",
                children=[
                    html.Label("Fill with AI", htmlFor=f"rfq-ai-text-{asset_class}", className="rfq-ai-title"),
                    html.Span("Describe your RFQ", id=f"rfq-ai-stage-{asset_class}", className="rfq-ai-badge"),
                ],
            ),
            html.P(
                "Describe one complete RFQ in English or Portuguese. Each AI run replaces the form with a fresh trade.",
                className="rfq-ai-hint",
            ),
            dcc.Textarea(
                id=f"rfq-ai-text-{asset_class}",
                className="rfq-ai-textarea",
                placeholder=examples[asset_class],
                maxLength=RFQ_AI_MAX_CHARS,
                rows=2,
            ),
            html.Div(
                className="rfq-ai-toolbar",
                children=[
                    html.Button(
                        "Apply to form", id=f"rfq-ai-apply-{asset_class}",
                        className="rfq-ai-apply", type="button",
                    ),
                    html.Div(
                        className="rfq-ai-progress", role="status",
                        children=[html.Span(className="rfq-ai-spinner", **{"aria-hidden": "true"}),
                                  html.Span("Reading your request and filling the form…")],
                    ),
                    html.Span(
                        f"0 / {RFQ_AI_MAX_CHARS}", id=f"rfq-ai-count-{asset_class}",
                        className="rfq-ai-count", role="status",
                        **{"aria-live": "polite", "aria-atomic": "true"},
                    ),
                ],
            ),
            html.Div(
                id=f"rfq-ai-status-{asset_class}", className="rfq-ai-status",
                role="status", **{"aria-live": "polite", "aria-atomic": "true"},
            ),
        ],
    )


def rfq_form_modal(asset_class, title, children):
    return dmc.Modal(
        id=f"rfq-form-{asset_class}", title=title,
        opened=False, keepMounted=True, centered=True,
        size="min(1200px, calc(100vw - 32px))", zIndex=2000,
        classNames={"content": "rfq-modal-content", "header": "rfq-modal-header",
                    "title": "rfq-modal-title", "body": "rfq-modal-body"},
        children=[
            rfq_ai_panel(asset_class),
            html.Fieldset(
                id=f"rfq-fields-{asset_class}", className="rfq-fields-shell",
                children=[
                    html.Div(
                        className="rfq-review-hint", role="status",
                        children=[html.Strong("Review trade details"),
                                  html.Span("Check the values, then select Create RFQ.")],
                    ),
                    *children[:-1],
                ],
            ),
            children[-1],
        ],
    )


layout = dbc.Container(
    fluid=True,
    className="page-container rfq-page",
    children=[
        dcc.Store(id="rfq-refresh", data=0),
        dcc.Store(id="rfq-sovereign-assets", data=[]),
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
        rfq_form_modal(
            "sovereign", "New Sovereign Bonds RFQ",
            children=[
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
                                                        value=None,
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
                                            children=[
                                                html.Div(
                                                    "Quote Requester",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    id="rfq-requester-sovereign",
                                                    className="rfq-input",
                                                    type="text",
                                                    readOnly=True,
                                                    placeholder="Signed-in username",
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
                                                        data=[],
                                                        value=None,
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
                                                    options=[],
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
                                                dmc.NumberInput(
                                                    className="rfq-number-input",
                                                    classNames={"input": "rfq-input"},
                                                    placeholder='e.g., 50,000,000',
                                                    thousandSeparator=",",
                                                    hideControls=True,
                                                    allowNegative=False,
                                                    allowDecimal=False,
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
                                                dmc.NumberInput(
                                                    className="rfq-number-input",
                                                    classNames={"input": "rfq-input"},
                                                    placeholder='e.g., 1,000,000',
                                                    thousandSeparator=",",
                                                    hideControls=True,
                                                    allowNegative=False,
                                                    allowDecimal=True,
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
        rfq_form_modal(
            "corporate", "New Corporate Bonds RFQ",
            children=[
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
                                                        value=None,
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
                                            children=[
                                                html.Div(
                                                    "Quote Requester",
                                                    className="rfq-form-label",
                                                ),
                                                dcc.Input(
                                                    id="rfq-requester-corporate",
                                                    className="rfq-input",
                                                    type="text",
                                                    readOnly=True,
                                                    placeholder="Signed-in username",
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
                                                        value=None,
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
                                                dmc.NumberInput(
                                                    className="rfq-number-input",
                                                    classNames={"input": "rfq-input"},
                                                    placeholder='e.g., 20,000,000',
                                                    thousandSeparator=",",
                                                    hideControls=True,
                                                    allowNegative=False,
                                                    allowDecimal=False,
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
                                                dmc.NumberInput(
                                                    className="rfq-number-input",
                                                    classNames={"input": "rfq-input"},
                                                    placeholder='e.g., 5,000,000',
                                                    thousandSeparator=",",
                                                    hideControls=True,
                                                    allowNegative=False,
                                                    allowDecimal=True,
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
        rfq_form_modal(
            "derivatives", "New Derivatives RFQ",
            children=[
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
                                dcc.Input(
                                    id="rfq-requester-derivatives",
                                    className="rfq-input",
                                    type="text",
                                    readOnly=True,
                                    placeholder="Signed-in username",
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
                                            value=None,
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
                                dmc.NumberInput(
                                    className="rfq-number-input",
                                    classNames={"input": "rfq-input"},
                                    placeholder='e.g. 10,000,000',
                                    thousandSeparator=",",
                                    hideControls=True,
                                    allowNegative=False,
                                    allowDecimal=True,
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
                                            value=None,
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
                                            value=None,
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
                                                    value=[],
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
                                            value=None,
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


def register_rfq_ai_callback(asset_class):
    clientside_callback(
        """function(value) {
            const count = (value || "").length;
            const limit = %d;
            const state = count > limit ? " rfq-ai-count-over" : count === limit ? " rfq-ai-count-limit" : "";
            return [count + " / " + limit, "rfq-ai-count" + state];
        }""" % RFQ_AI_MAX_CHARS,
        Output(f"rfq-ai-count-{asset_class}", "children"),
        Output(f"rfq-ai-count-{asset_class}", "className"),
        Input(f"rfq-ai-text-{asset_class}", "value"),
    )
    # One callback owns presentation so the end of the running state cannot
    # overwrite the review highlight returned after the form has been filled.
    clientside_callback(
        """function(result, busy) {
            if (busy === "true") {
                return ["rfq-ai-panel rfq-ai-running", "rfq-fields-shell", "Filling form…"];
            }
            if (result && result.review) {
                return ["rfq-ai-panel rfq-ai-complete", "rfq-fields-shell rfq-fields-review", "Review trade below"];
            }
            return ["rfq-ai-panel", "rfq-fields-shell", "Describe your RFQ"];
        }""",
        Output(f"rfq-ai-panel-{asset_class}", "className"),
        Output(f"rfq-fields-{asset_class}", "className"),
        Output(f"rfq-ai-stage-{asset_class}", "children"),
        Input(f"rfq-ai-result-{asset_class}", "data"),
        Input(f"rfq-ai-panel-{asset_class}", "aria-busy"),
    )
    dependent = {"requester", "maturity"} if asset_class == "sovereign" else {"requester"}
    direct_fields = {name: component_id for name, component_id in RFQ_FIELDS[asset_class].items() if name not in dependent}
    reset_values = rfq_empty_form(asset_class, "")
    reset_outputs = [Output(component_id, "value", allow_duplicate=True) for component_id in direct_fields.values()]
    reset_outputs += [Output(f"rfq-ai-result-{asset_class}", "data", allow_duplicate=True),
                      Output(f"rfq-ai-status-{asset_class}", "children", allow_duplicate=True)]
    reset_payload = [(["solve"] if reset_values[name] else []) if name in SOLVE_FIELDS else reset_values[name]
                     for name in direct_fields] + [None, ""]
    reset_outputs.append(Output(f"rfq-ai-text-{asset_class}", "value"))
    # Opening clears the draft and description; Apply clears only the trade fields.
    reset_script = """function(n, opened) {
        const changed = window.dash_clientside.callback_context.triggered;
        const opening = opened && changed.some(item => item.prop_id === %s);
        const applying = changed.some(item => item.prop_id === %s);
        if (!opening && !applying) {
            return Array(%d).fill(window.dash_clientside.no_update);
        }
        const values = %s;
        values.push(opening ? "" : window.dash_clientside.no_update);
        return values;
    }""" % (json.dumps(f"rfq-form-{asset_class}.opened"),
             json.dumps(f"rfq-ai-apply-{asset_class}.n_clicks"),
             len(reset_outputs), json.dumps(reset_payload))
    clientside_callback(
        reset_script, reset_outputs,
        Input(f"rfq-ai-apply-{asset_class}", "n_clicks"),
        Input(f"rfq-form-{asset_class}", "opened"), prevent_initial_call=True,
    )
    dynamic_options = []
    if asset_class == "corporate":
        dynamic_options = [Output("rfq-corp-security-dropdown", "options")]

    @callback(
        [Output(component_id, "value") for component_id in direct_fields.values()]
        + [Output(f"rfq-ai-result-{asset_class}", "data"),
           Output(f"rfq-ai-status-{asset_class}", "children")] + dynamic_options,
        Input(f"rfq-ai-apply-{asset_class}", "n_clicks"),
        [State(f"rfq-ai-text-{asset_class}", "value"),
         State("current-user", "data"), State(f"rfq-form-{asset_class}", "opened")]
        + ([State("rfq-sovereign-assets", "data")] if asset_class == "sovereign" else []),
        prevent_initial_call=True,
        running=[
            (Output(f"rfq-ai-panel-{asset_class}", "aria-busy"), "true", "false"),
            (Output(f"rfq-ai-text-{asset_class}", "disabled"), True, False),
            (Output(f"rfq-ai-apply-{asset_class}", "disabled"), True, False),
            (Output(f"rfq-fields-{asset_class}", "disabled"), True, False),
            (Output(f"rfq-submit-{asset_class}", "disabled"), True, False),
            (Output(f"rfq-cancel-{asset_class}", "disabled"), True, False),
            (Output(f"rfq-form-{asset_class}", "closeOnEscape"), False, True),
            (Output(f"rfq-form-{asset_class}", "closeOnClickOutside"), False, True),
            (Output(f"rfq-form-{asset_class}", "withCloseButton"), False, True),
        ],
    )
    def fill_rfq_form(_clicks, text, current_user, opened, sovereign_assets=None):
        if not opened or not current_user or not current_user.get("user"):
            raise PreventUpdate
        username = current_user["user"]
        fields = rfq_empty_form(asset_class, username)
        metadata = {}
        review = False
        alert_level = None
        alert_type = None
        alert_log_id = None
        alert_context = {"request_text": text}
        try:
            draft = extract_rfq(asset_class, text, username, sovereign_assets=sovereign_assets)
            fields, metadata = draft.fields, draft.metadata
            alert_log_id = metadata.get("log_id")
            review = draft.relevant and not draft.ambiguous and any(
                value not in (None, "") for name, value in fields.items()
                if name != "requester" and name not in SOLVE_FIELDS
            )
            if draft.ambiguous:
                alert_level = "warning"
                alert_type = "ConflictingRFQs"
                status = html.Span(
                    "Several RFQs or conflicting details were found. The form was reset. Describe one complete trade and apply again.",
                    className="rfq-ai-needs-details",
                )
            elif not draft.relevant:
                alert_level = "warning"
                alert_type = "UnrelatedRequest"
                status = html.Span(
                    "Describe one RFQ: security, side, size, and any pricing details. The form was reset.",
                    className="rfq-ai-needs-details",
                )
            elif draft.missing_fields:
                alert_level = "warning"
                alert_type = "MissingRFQDetails"
                alert_context["missing_fields"] = draft.missing_fields
                status = html.Div([
                    html.Strong("Please specify the following details:"),
                    html.Ul([html.Li(FIELD_LABELS[name]) for name in draft.missing_fields]),
                    html.Span("Include them in a complete RFQ description and apply again. Each run starts fresh."),
                ], className="rfq-ai-needs-details")
            else:
                status = html.Span(
                    "Fields applied. Review the trade details below, then select Create RFQ.", className="rfq-ai-success",
                )
        except AIConfigurationError as exc:
            alert_level = "error"
            alert_log_id = getattr(exc, "ai_log_id", None)
            alert_context["error_type"] = type(exc).__name__
            logger.warning("RFQ AI configuration: %s", exc)
            status = html.Span("AI assistance is unavailable. The form was reset; you can fill it manually.", className="rfq-ai-error")
        except (AIError, sqlite3.Error, LookupError) as exc:
            alert_level = "error"
            alert_log_id = getattr(exc, "ai_log_id", None)
            alert_context["error_type"] = type(exc).__name__
            logger.warning("RFQ AI request failed (%s)", type(exc).__name__)
            message = str(exc) if isinstance(exc, AIError) else "AI assistance is temporarily unavailable. Please try again."
            status = html.Span(message + " The form was reset.", className="rfq-ai-error")
        if alert_level:
            log_rfq_form_event(rfq_alert_text(status), source="ai_fill", form_type=asset_class,
                               current_user=current_user, level=alert_level,
                               ai_log_id=alert_log_id, context=alert_context, alert_type=alert_type)
        result = {"fields": fields, "metadata": metadata, "review": review, "request_id": uuid4().hex}
        values = [(["solve"] if fields[name] else []) if name in SOLVE_FIELDS else fields[name] for name in direct_fields]
        choices = []
        if asset_class == "corporate":
            choices = [load_corporate_security_options()]
        return values + [result, status] + choices


for _asset_class in RFQ_FIELDS:
    register_rfq_ai_callback(_asset_class)


@callback(
    Output("rfq-form-sovereign", "opened"),
    Output("rfq-form-corporate", "opened"),
    Output("rfq-form-derivatives", "opened"),
    Input("rfq-create-btn", "n_clicks"),
    Input("rfq-cancel-sovereign", "n_clicks"),
    Input("rfq-cancel-corporate", "n_clicks"),
    Input("rfq-cancel-derivatives", "n_clicks"),
    Input("rfq-tabs", "value"),
    Input("rfq-refresh", "data"),
    Input("current-user", "data"),
)
def toggle_rfq_form(
    create_clicks,
    cancel_sovereign,
    cancel_corporate,
    cancel_derivatives,
    active_tab,
    refresh_value,
    current_user,
):
    # Only a successful submission changes rfq-refresh; validation errors
    # leave the modal open with the user's inputs intact.
    if ctx.triggered_id == "rfq-create-btn" and current_user and current_user.get("user"):
        return tuple(active_tab == tab for tab in ("sovereign", "corporate", "derivatives"))
    return False, False, False


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
    Output("rfq-requester-sovereign", "value"),
    Output("rfq-requester-corporate", "value"),
    Output("rfq-requester-derivatives", "value"),
    Input("current-user", "data"),
)
def set_rfq_requester(current_user):
    username = (current_user or {}).get("user") or ""
    return username, username, username


@callback(
    Output("rfq-sovereign-assets", "data"),
    Output("rfq-bond-name-sovereign", "data"),
    Input("rfq-form-sovereign", "opened"),
)
def refresh_sovereign_assets(opened):
    assets = load_sovereign_assets() if opened else []
    bonds, _ = sovereign_form_options(assets)
    return assets, bonds


@callback(
    Output("rfq-maturity-sovereign", "options"),
    Output("rfq-maturity-sovereign", "value"),
    Input("rfq-bond-name-sovereign", "value"),
    Input("rfq-ai-result-sovereign", "data"),
    Input("rfq-sovereign-assets", "data"),
    State("rfq-maturity-sovereign", "value"),
)
def update_sovereign_maturity_dropdown(bond_name, ai_result, assets, current_value):
    _, maturities = sovereign_form_options(assets)
    options = maturities.get(bond_name, [])
    if not options:
        return [], None
    valid_values = {item["value"] for item in options}
    if "rfq-ai-result-sovereign.data" in ctx.triggered_prop_ids:
        fields = (ai_result or {}).get("fields", {})
        if "maturity" in fields and fields["maturity"] in valid_values:
            return options, fields["maturity"]
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
    State("rfq-bond-name-sovereign", "value"),
    State("rfq-maturity-sovereign", "value"),
    State("rfq-qty-sovereign", "value"),
    State("rfq-volume-sovereign", "value"),
    State("rfq-limit-sovereign", "value"),
    State("rfq-sovereign-assets", "data"),
    State("rfq-direction-corporate", "value"),
    State("rfq-bond-type-corporate", "value"),
    State("rfq-corp-security-text", "value"),
    State("rfq-corp-security-dropdown", "value"),
    State("rfq-qty-corporate", "value"),
    State("rfq-volume-corporate", "value"),
    State("rfq-limit-corporate", "value"),
    State("rfq-direction-derivatives", "value"),
    State("rfq-quote-type-derivatives", "value"),
    State("rfq-bond-name-derivatives", "value"),
    State("rfq-maturity-derivatives", "value"),
    State("rfq-maturity-unit-derivatives", "value"),
    State("rfq-qty-derivatives", "value"),
    State("rfq-volume-derivatives", "value"),
    State("rfq-limit-derivatives", "value"),
    State("rfq-strategy-derivatives", "value"),
    State("rfq-strike-derivatives", "value"),
    State("rfq-strike-solve-derivatives", "value"),
    State("rfq-barrier-derivatives", "value"),
    State("rfq-barrier-solve-derivatives", "value"),
    State("rfq-barrier-type-derivatives", "value"),
    State("current-user", "data"),
    State("rfq-refresh", "data"),
    prevent_initial_call=True,
)
def submit_rfq(
    sovereign_clicks,
    corporate_clicks,
    derivatives_clicks,
    direction_sovereign,
    bond_name_sovereign,
    maturity_sovereign,
    qty_sovereign,
    volume_sovereign,
    limit_sovereign,
    sovereign_assets,
    direction_corporate,
    bond_type_corporate,
    corp_security_text,
    corp_security_dropdown,
    qty_corporate,
    volume_corporate,
    limit_corporate,
    direction_derivatives,
    quote_type_derivatives,
    bond_name_derivatives,
    maturity_derivatives,
    maturity_unit_derivatives,
    qty_derivatives,
    volume_derivatives,
    limit_derivatives,
    strategy_derivatives,
    strike_derivatives,
    strike_solve_derivatives,
    barrier_derivatives,
    barrier_solve_derivatives,
    barrier_type_derivatives,
    current_user,
    refresh_value,
):
    if not ctx.triggered_id:
        raise PreventUpdate

    if ctx.triggered_id == "rfq-submit-sovereign":
        asset_class = "Sovereign Bond"
        direction = direction_sovereign
        quote_type = FIXED_INCOME_QUOTE_TYPE
        bond_name = maturity_sovereign
        sovereign_assets = load_sovereign_assets() if sovereign_assets is None else sovereign_assets
        maturity = next((asset["maturity"] for asset in sovereign_assets
                         if asset["security"] == maturity_sovereign), None)
        qty = qty_sovereign
        volume = volume_sovereign
        limit_value = limit_sovereign
    elif ctx.triggered_id == "rfq-submit-corporate":
        asset_class = "Corporate Bond"
        direction = direction_corporate
        quote_type = FIXED_INCOME_QUOTE_TYPE
        if bond_type_corporate == "Banking Instruments":
            bond_name = corp_security_text
            maturity = None
        else:
            bond_name = corp_security_dropdown
            maturity = load_security_maturity(corp_security_dropdown)
        qty = qty_corporate
        volume = volume_corporate
        limit_value = limit_corporate
    elif ctx.triggered_id == "rfq-submit-derivatives":
        asset_class = "Derivatives"
        direction = direction_derivatives
        quote_type = quote_type_derivatives
        bond_name = bond_name_derivatives
        tenor = _parse_number(maturity_derivatives)
        maturity = f"{tenor:g}{maturity_unit_derivatives}" if tenor is not None else None
        qty = qty_derivatives
        volume = volume_derivatives
        limit_value = limit_derivatives
    else:
        raise PreventUpdate

    security_text = str(bond_name).strip() if bond_name is not None else ""
    maturity_text = str(maturity).strip() if maturity not in (None, "") else ""
    requester = (current_user or {}).get("user") or ""
    qty_text = str(qty).strip() if qty not in (None, "") else ""
    volume_text = str(volume).strip() if volume not in (None, "") else ""
    limit_text = str(limit_value).strip() if limit_value not in (None, "") else ""

    qty_parsed = _parse_quantity(qty)
    volume_parsed = _parse_number(volume)
    limit_parsed = _parse_number(limit_value)

    validation_errors = []

    if not security_text:
        validation_errors.append("Security must be filled.")

    if not requester:
        validation_errors.append("Sign in before creating an RFQ.")

    form_class = {"Sovereign Bond": "sovereign", "Corporate Bond": "corporate", "Derivatives": "derivatives"}[asset_class]
    form_values = {
        "direction": direction, "quote_type": quote_type,
        "quantity": qty_text or None, "volume": volume_text or None, "yield_limit": limit_value,
    }
    if form_class == "sovereign":
        form_values.update(bond_name=bond_name_sovereign, maturity=maturity_sovereign)
    elif form_class == "corporate":
        form_values.update(bond_type=bond_type_corporate, security=corp_security_dropdown, security_text=corp_security_text)
    else:
        form_values.update(
            security=bond_name_derivatives, maturity=maturity_derivatives,
            maturity_unit=maturity_unit_derivatives, premium=limit_derivatives,
            strategy=strategy_derivatives, strike=strike_derivatives,
            strike_solve="solve" in (strike_solve_derivatives or []),
            barrier=barrier_derivatives, barrier_solve="solve" in (barrier_solve_derivatives or []),
            barrier_type=barrier_type_derivatives,
        )
    for field in rfq_missing_fields(form_class, form_values):
        validation_errors.append(f"Please fill {FIELD_LABELS[field]}.")
    contract = rfq_json_contract(form_class, requester, sovereign_assets=sovereign_assets)
    for field, spec in contract["output_schema"]["properties"]["fields"]["properties"].items():
        value = form_values.get(field)
        if field != "requester" and value not in (None, "") and "enum" in spec and value not in spec["enum"]:
            validation_errors.append(f"Select a valid {FIELD_LABELS[field]}.")
        field_type = contract["field_definitions"][field]["type"]
        if value not in (None, "") and field_type in ("number", "integer"):
            parsed = _parse_quantity(value) if field_type == "integer" else _parse_number(value)
            if parsed is None or parsed <= 0:
                validation_errors.append(f"Enter a positive {'whole number' if field_type == 'integer' else 'number'} for {FIELD_LABELS[field]}.")

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

    if validation_errors:
        for message in validation_errors:
            log_rfq_form_event(message, source="submit_rfq", form_type=form_class,
                               current_user=current_user, context={"fields": form_values})
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
    usr_input = requester

    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
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
    except sqlite3.Error as exc:
        log_rfq_form_event("Unable to create RFQ. Try again.", source="submit_rfq", form_type=form_class,
                           current_user=current_user, context={"error_type": type(exc).__name__})
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
    form_type = None

    try:
        with closing(sqlite3.connect(DATABASE_PATH, timeout=30)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                'SELECT STR_RESPONSE, CLASS FROM RFQ WHERE RFQ_ID = ?',
                (rfq_id,),
            ).fetchone()
            if row:
                form_type = {"Sovereign Bond": "sovereign", "Corporate Bond": "corporate", "Derivatives": "derivatives"}.get(row["CLASS"])
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
                    raise RFQQuoteValidationError("Quote value is not in a valid format.")
                if invalid_time or not timestamp:
                    raise RFQQuoteValidationError("Invalid timestamp. Use HH:MM.")
                new_entry = {
                    "broker name": broker_name,
                    "broker_response": cleaned_response,
                    "timestamp": timestamp,
                }
            else:
                if target_idx is not None:
                    raise RFQQuoteValidationError("Use format: quote|HH:MM.")
                cleaned_response = value_text.replace("|", " ").strip()
                if not cleaned_response:
                    raise RFQQuoteValidationError("Quote value is not in a valid format.")
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
    except RFQQuoteValidationError as exc:
        message = str(exc)
        log_rfq_form_event(message, source="broker_response", form_type=form_type,
                           current_user=current_user, rfq_id=rfq_id,
                           context={"broker": broker_name, "value": new_value})
        return (refresh_value or 0) + 1, message, "show"
    except sqlite3.Error as exc:
        message = "Unable to update quote. Try again."
        log_rfq_form_event(message, source="broker_response", form_type=form_type,
                           current_user=current_user, rfq_id=rfq_id,
                           context={"error_type": type(exc).__name__, "broker": broker_name})
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
            with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
                conn.execute(
                    """
                    UPDATE RFQ
                    SET FL_ACTIVE = 0, USR_CLOSED = ?, DT_CLOSED = ?
                    WHERE RFQ_ID = ?
                    """,
                    (usr_closed, dt_closed, rfq_id),
                )
                conn.commit()
        except sqlite3.Error as exc:
            log_rfq_form_event("Unable to close RFQ. Try again.", source="rfq_action",
                               current_user=current_user, rfq_id=rfq_id,
                               context={"error_type": type(exc).__name__})
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
