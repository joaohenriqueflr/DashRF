"""Backend-authorized routine tools. No model-controlled SQL or Python execution.

Handlers receive the executor's connection so the operational write and its
execution record share one SQLite transaction. Rates use the existing database's
decimal convention (0.07 = 7%); NTN-F source spreads are in basis points.
"""

from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import BytesParser
import csv
import io
import math
from pathlib import Path
import re
import sqlite3
from typing import Callable
from zoneinfo import ZoneInfo

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
MAX_IMPORT_ROWS = 5000
MAX_FILE_BYTES = 10 * 1024 * 1024
QUOTE_COLUMNS = ["NM_SECURITY", "SIDE", "TX_QUOTE", "VL_QUOTE", "TS_REF", "SPREAD_QUOTE", "TX_DI_REF", "TX_SPREAD"]
DEBTOR_COLUMNS = ["ID_CLIENT", "NR_DAYS", "NM_RESP_GWM", "DESCR", "FL_RESOLVED"]


@dataclass(frozen=True)
class ToolSpec:
    write: bool
    description: str
    schema: dict
    validate: Callable
    preview: Callable
    execute: Callable
    # Derived values that must be shown again if validation changes the preview.
    review_fields: tuple = ()


# ---- Generic SQLite tools and validation -------------------------------------

def ensure_operational_schema(conn):
    """Add only optional market fields; never create a guessed broker schema."""
    if _table_exists(conn, "UGM_DEVEDORES"):
        if _table_exists(conn, "UGM_ROUTINES_DEVEDORES"):
            raise ValueError(
                "Both UGM_DEVEDORES and UGM_ROUTINES_DEVEDORES exist; "
                "resolve the table migration explicitly before enabling routines.")
        conn.execute("ALTER TABLE UGM_DEVEDORES RENAME TO UGM_ROUTINES_DEVEDORES")
    broker_columns = {row[1] for row in conn.execute("PRAGMA table_info(FIXED_INCOME_BROKER_DATA)")}
    if broker_columns:
        for name, kind in (("TS_REF", "TEXT"), ("SPREAD_QUOTE", "REAL"), ("TX_DI_REF", "REAL"), ("TX_SPREAD", "REAL")):
            if name not in broker_columns:
                conn.execute(f'ALTER TABLE FIXED_INCOME_BROKER_DATA ADD COLUMN "{name}" {kind}')
    conn.execute("""CREATE TABLE IF NOT EXISTS UGM_ROUTINES_DEVEDORES (
        DT_REF TEXT NOT NULL,
        ID_CLIENT TEXT NOT NULL,
        NR_DAYS INTEGER NOT NULL CHECK (NR_DAYS >= 0),
        NM_RESP_GWM TEXT NOT NULL,
        DESCR TEXT NOT NULL DEFAULT '',
        FL_RESOLVED INTEGER NOT NULL DEFAULT 1 CHECK (FL_RESOLVED IN (0, 1)),
        PRIMARY KEY (DT_REF, ID_CLIENT)
    )""")
    expected = {"DT_REF", *DEBTOR_COLUMNS}
    actual = {row[1] for row in conn.execute("PRAGMA table_info(UGM_ROUTINES_DEVEDORES)")}
    if not expected.issubset(actual):
        raise ValueError("UGM_ROUTINES_DEVEDORES has an incompatible schema; migrate it explicitly before enabling routines.")
    duplicate = conn.execute("SELECT 1 FROM UGM_ROUTINES_DEVEDORES GROUP BY DT_REF, ID_CLIENT HAVING COUNT(*) > 1 LIMIT 1").fetchone()
    if duplicate:
        raise ValueError("UGM_ROUTINES_DEVEDORES has multiple cases per client/day; resolve the data contract before enabling imports.")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ugm_devedores_day_client ON UGM_ROUTINES_DEVEDORES (DT_REF, ID_CLIENT)")


def _object(value, allowed):
    if not isinstance(value, dict):
        raise ValueError("Tool parameters must be an object.")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError("Unsupported fields: " + ", ".join(sorted(str(x) for x in unknown)))
    return value


def _text(value, name, *, optional=False, limit=1000):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must contain text.")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters.")
    return value


def _number(value, name, *, optional=False, minimum=None, maximum=None):
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric, using a decimal point.") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise ValueError(f"{name} is outside its permitted range.")
    return number


def _integer(value, name, *, minimum=0, maximum=None):
    number = _number(value, name, minimum=minimum, maximum=maximum)
    if number != int(number):
        raise ValueError(f"{name} must be an integer.")
    return int(number)


def _rows(args):
    rows = args.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_IMPORT_ROWS:
        raise ValueError(f"Provide between 1 and {MAX_IMPORT_ROWS} records.")
    return rows


def _now(context):
    value = context.get("now") or datetime.now(SAO_PAULO)
    if value.tzinfo is None:
        value = value.replace(tzinfo=SAO_PAULO)
    return value.astimezone(SAO_PAULO)


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _fetch(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ---- Data import and interpretation -----------------------------------------

def parse_import(content, filename=None):
    """Read canonical-column files without guessing undocumented vendor formats.

    CSV/TSV and XLSX require canonical field names. Unstructured content and EML
    bodies return text for the existing OpenAI service. The model still cannot
    save anything without the persisted proposal's confirmation button.
    """
    if not isinstance(content, (bytes, str)):
        raise ValueError("Import content must be bytes or text.")
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("The maximum import size is 10 MB.")
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError("Install openpyxl in the dashboard environment to import XLSX files.") from exc
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            sheet = workbook.active
            iterator = sheet.iter_rows(values_only=True)
            headers = next(iterator, None)
            if not headers:
                raise ValueError("The spreadsheet is empty.")
            names = [str(v).strip() if v is not None else "" for v in headers]
            _validate_headers(names)
            rows = []
            for values in iterator:
                if not any(v is not None for v in values):
                    continue
                if len(rows) >= MAX_IMPORT_ROWS:
                    raise ValueError(f"Import exceeds {MAX_IMPORT_ROWS} rows.")
                row = {}
                for name, value in zip(names, values):
                    if value is None:
                        continue
                    if isinstance(value, datetime):
                        value = value.isoformat(timespec="seconds")
                    row[name] = value
                rows.append(row)
            return {"rows": rows, "text": "", "warnings": []}
        finally:
            workbook.close()
    if suffix == ".eml":
        message = BytesParser(policy=policy.default).parsebytes(raw)
        part = message.get_body(preferencelist=("plain",))
        if part is None:
            raise ValueError("The email has no plain-text body. Paste its table or upload a structured attachment.")
        return {"rows": None, "text": part.get_content(), "warnings": ["Review the extracted email data before saving."]}
    if suffix and suffix not in {".csv", ".tsv", ".txt"}:
        raise ValueError("Supported uploads: CSV, TSV, TXT, XLSX and plain-text EML. Export other file formats first.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Text imports must use UTF-8. Export as UTF-8 CSV or paste the text.") from exc
    if not text.strip():
        raise ValueError("The import is empty.")
    first_line = text.splitlines()[0]
    delimiter = "\t" if "\t" in first_line else ";" if ";" in first_line else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    names = [str(x).strip() for x in (reader.fieldnames or [])]
    if not set(names) & {"NM_SECURITY", "ID_CLIENT"}:
        if suffix in {".csv", ".tsv"}:
            raise ValueError("Structured imports require canonical NM_SECURITY or ID_CLIENT headers; use TXT for unstructured text.")
        return {"rows": None, "text": text, "warnings": []}
    _validate_headers(names)
    reader.fieldnames = names
    rows = []
    for row in reader:
        if not any(value not in (None, "") for value in row.values()):
            continue
        if None in row:
            raise ValueError("Some rows contain more columns than the header.")
        if len(rows) >= MAX_IMPORT_ROWS:
            raise ValueError(f"Import exceeds {MAX_IMPORT_ROWS} rows.")
        rows.append({key: value for key, value in row.items() if value not in (None, "")})
    return {"rows": rows, "text": "", "warnings": []}


def _validate_headers(names):
    if not names or any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("Column headers must be nonempty and unique.")
    allowed = set(QUOTE_COLUMNS + DEBTOR_COLUMNS + ["ID_SECURITY"])
    if not set(names).issubset(allowed) or not set(names) & {"NM_SECURITY", "ID_CLIENT"}:
        raise ValueError("Use canonical quote or debtor column headers; undocumented source formats need text interpretation.")


# ---- Market data and Bloomberg contracts ------------------------------------

_DI_CURVE_PROVIDER = None
_NTNF_RATE_CALCULATOR = None


def configure_ntnf_market_data(*, curve_provider, rate_calculator):
    """Register existing trusted integrations once their conventions are supplied.

    curve_provider(dt_ref=..., securities=[classification dicts]) returns a curve.
    rate_calculator(spread_bps=..., security=..., curve=..., dt_ref=...) returns
    {'reference_rate': decimal, 'rate': decimal}. No financial convention is
    selected here, and these callables are never supplied by the model/SQLite.
    """
    if not callable(curve_provider) or not callable(rate_calculator):
        raise ValueError("Both a trusted DI curve provider and a deterministic rate calculator are required.")
    global _DI_CURVE_PROVIDER, _NTNF_RATE_CALCULATOR
    _DI_CURVE_PROVIDER = curve_provider
    _NTNF_RATE_CALCULATOR = rate_calculator


MARKET_SOURCES = ("XP", "Santander", "Ativa", "BGC", "xBridge")


def _market_scope(context):
    """Trusted SQLite configuration specializes the same handlers for new runs.

    Activity columns are the source of truth. Source names stay inside the
    Python allowlist; model arguments never grant sources or instrument scopes.
    """
    config = context.get("config", {})
    scope = {}
    for key, fallback in (("instrument_types", []), ("excluded_instrument_types", []), ("sources", [])):
        value = config.get(key, fallback)
        if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"Configured {key} must be a list of nonempty names.")
        scope[key] = list(dict.fromkeys(value))
    scope["sources"] = [source for source in scope["sources"] if source in MARKET_SOURCES]
    scope["quote_mode"] = config.get("quote_mode", "RATE")
    if scope["quote_mode"] not in {"RATE", "DI_SPREAD"}:
        raise ValueError("Configured quote_mode must be RATE or DI_SPREAD.")
    return scope


def _market_filter(context, alias=""):
    """Return parameterized classification filters for fixed backend columns."""
    scope = _market_scope(context)
    column = (alias + "." if alias else "") + "NM_INSTRUMENT_TYPE"
    where, values = [], []
    for key, operator in (("instrument_types", "IN"), ("excluded_instrument_types", "NOT IN")):
        names = scope[key]
        if names:
            where.append(column + " " + operator + " (" + ",".join("?" for _ in names) + ")")
            values.extend(names)
    return where, values


def _source(args, context):
    allowed = _market_scope(context)["sources"]
    tool_config = context.get("tool_config", {})
    if "allowed_sources" in tool_config:
        configured = tool_config["allowed_sources"]
        if not isinstance(configured, list) or any(not isinstance(source, str) for source in configured):
            raise ValueError("Configured sources must be a list of source names.")
        allowed = [source for source in allowed if source in configured]
    source = _text(args.get("source"), "source", limit=100)
    if source not in allowed:
        raise ValueError("Source is not enabled for this routine.")
    return source


def _classification(conn, row, context):
    if not _table_exists(conn, "FIXED_INCOME_SECURITY_CLASSIFICATION"):
        raise ValueError("The existing security classification database is unavailable.")
    name = _text(row.get("NM_SECURITY"), "NM_SECURITY", limit=200)
    where, values = _market_filter(context)
    where.insert(0, "NM_SECURITY = ?")
    values.insert(0, name)
    matches = _fetch(conn, "SELECT ID_SECURITY, NM_SECURITY, NM_INSTRUMENT_TYPE, DT_MATURITY FROM FIXED_INCOME_SECURITY_CLASSIFICATION WHERE " + " AND ".join(where), values)
    if len(matches) != 1:
        raise ValueError(f"Security '{name}' must match one existing classification record enabled for this routine exactly.")
    security = matches[0]
    # Editable cards can retain a previous hidden ID when NM_SECURITY changes.
    # Always derive the ID from the exact backend classification match.
    return security


def _quote_validate(conn, args, context):
    _object(args, {"source", "rows"})
    source = _source(args, context)
    if not _table_exists(conn, "FIXED_INCOME_BROKER_DATA"):
        raise ValueError("FIXED_INCOME_BROKER_DATA is unavailable; its schema must be supplied before market imports.")
    raw_rows = _rows(args)
    normalized = []
    securities = []
    spread_mode = _market_scope(context)["quote_mode"] == "DI_SPREAD"
    for i, row in enumerate(raw_rows, 1):
        _object(row, set(QUOTE_COLUMNS) | {"ID_SECURITY"})
        security = _classification(conn, row, context)
        securities.append(security)
        side = _text(row.get("SIDE"), f"Row {i} SIDE", limit=20)
        if side not in {"Bid", "Ask"}:
            raise ValueError(f"Row {i}: SIDE must be Bid or Ask.")
        quote = _number(row.get("TX_QUOTE"), f"Row {i} TX_QUOTE", optional=spread_mode, minimum=-1, maximum=1)
        if spread_mode:
            spread = _number(row.get("SPREAD_QUOTE"), f"Row {i} SPREAD_QUOTE", minimum=-10000, maximum=10000)
        else:
            spread = _number(row.get("SPREAD_QUOTE"), f"Row {i} SPREAD_QUOTE", optional=True, minimum=-10000, maximum=10000)
        stamp = row.get("TS_REF")
        if stamp in (None, ""):
            stamp = _now(context).isoformat(timespec="seconds")
        else:
            stamp = _text(stamp, f"Row {i} TS_REF", limit=60)
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError as exc:
                raise ValueError(f"Row {i}: TS_REF must be an ISO date and time.") from exc
            if "T" not in stamp and " " not in stamp:
                raise ValueError(f"Row {i}: TS_REF must include a time.")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=SAO_PAULO)
            stamp = parsed.astimezone(SAO_PAULO).isoformat(timespec="seconds")
            if parsed.astimezone(SAO_PAULO).date().isoformat() != context["dt_ref"]:
                raise ValueError(f"Row {i}: TS_REF must belong to the selected operational date.")
        normalized.append({"ID_SECURITY": security["ID_SECURITY"], "NM_SECURITY": security["NM_SECURITY"],
                           "SIDE": side, "TX_QUOTE": quote,
                           "VL_QUOTE": _number(row.get("VL_QUOTE"), f"Row {i} VL_QUOTE", optional=True, minimum=0),
                           "TS_REF": stamp, "SPREAD_QUOTE": spread})
    if spread_mode:
        if _DI_CURVE_PROVIDER is None or _NTNF_RATE_CALCULATOR is None:
            raise ValueError("NTN-F is pending: supply the Bloomberg DI curve integration and approve the interpolation/composition convention. No rate has been calculated or saved.")
        curve = _DI_CURVE_PROVIDER(dt_ref=context["dt_ref"], securities=securities)
        if curve is None:
            raise ValueError("The Bloomberg DI reference curve is unavailable. No NTN-F rates were calculated.")
        for row, security in zip(normalized, securities):
            computed = _NTNF_RATE_CALCULATOR(spread_bps=row["SPREAD_QUOTE"], security=security, curve=curve, dt_ref=context["dt_ref"])
            row["TX_DI_REF"] = _number(computed.get("reference_rate"), "DI reference rate", minimum=-1, maximum=1)
            row["TX_SPREAD"] = _number(computed.get("rate"), "NTN-F calculated rate", minimum=-1, maximum=1)
            row["TX_QUOTE"] = row["TX_SPREAD"]
    return {"source": source, "rows": normalized}


def _quote_preview(conn, args, context):
    duplicates = 0
    for row in args["rows"]:
        duplicates += conn.execute("""SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA
            WHERE substr(DT_REF, 1, 10) = ? AND ID_SECURITY = ? AND NM_BROKER = ? AND SIDE = ? AND TX_QUOTE = ?""",
            (context["dt_ref"], row["ID_SECURITY"], args["source"], row["SIDE"], row["TX_QUOTE"])).fetchone()[0]
    warnings = ["Rates use decimals: 0.07 = 7%. Review original source times before confirming."]
    if duplicates:
        warnings.append(f"Potential duplicate: {duplicates} existing same-day quote(s) match security, broker, side and rate. Intraday imports remain allowed.")
    return {"kind": "import", "rows": args["rows"], "columns": QUOTE_COLUMNS, "source": args["source"], "warnings": warnings}


def _quote_execute(conn, args, context):
    conn.executemany("""INSERT INTO FIXED_INCOME_BROKER_DATA
        (DT_REF,ID_SECURITY,NM_SECURITY,NM_SOURCE,NM_BROKER,SIDE,TX_QUOTE,VL_QUOTE,DT_INPUT,USR_INPUT,TS_REF,SPREAD_QUOTE,TX_DI_REF,TX_SPREAD)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
            (context["dt_ref"], row["ID_SECURITY"], row["NM_SECURITY"], "Run", args["source"], row["SIDE"], row["TX_QUOTE"], row.get("VL_QUOTE"),
             context["dt_ref"], context["username"], row["TS_REF"], row.get("SPREAD_QUOTE"), row.get("TX_DI_REF"), row.get("TX_SPREAD"))
            for row in args["rows"]])
    return {"kind": "query", "source": "FIXED_INCOME_BROKER_DATA", "count": len(args["rows"]), "message": "Quotes saved.", "rows": args["rows"]}


def _query_quotes_validate(conn, args, context):
    _object(args, {"security", "source", "limit"})
    result = {"limit": _integer(args.get("limit", 100), "limit", minimum=1, maximum=1000)}
    for key in ("security", "source"):
        if args.get(key) not in (None, ""):
            result[key] = _text(args[key], key, limit=200)
    return result


def _query_quotes(conn, args, context):
    where, values = _market_filter(context, "c")
    where.insert(0, "substr(b.DT_REF,1,10) = ?")
    values.insert(0, context["dt_ref"])
    for key, column in (("security", "b.NM_SECURITY"), ("source", "b.NM_BROKER")):
        if key in args:
            where.append(column + " = ?")
            values.append(args[key])
    values.append(args["limit"])
    rows = _fetch(conn, """SELECT b.DT_REF,b.NM_SECURITY,b.NM_BROKER,b.SIDE,b.TX_QUOTE,b.VL_QUOTE,b.TS_REF,b.SPREAD_QUOTE,b.TX_DI_REF,b.TX_SPREAD,b.USR_INPUT
        FROM FIXED_INCOME_BROKER_DATA b JOIN FIXED_INCOME_SECURITY_CLASSIFICATION c ON c.ID_SECURITY = b.ID_SECURITY WHERE """
        + " AND ".join(where) + " ORDER BY b.DT_INPUT DESC,b.rowid DESC LIMIT ?", values)
    return {"kind": "query", "rows": rows, "source": "FIXED_INCOME_BROKER_DATA", "filters": {"DT_REF": context["dt_ref"], **args}, "count": len(rows)}


def _lookup_validate(conn, args, context):
    _object(args, {"search", "limit"})
    result = {"limit": _integer(args.get("limit", 100), "limit", minimum=1, maximum=1000)}
    if args.get("search") not in (None, ""):
        result["search"] = _text(args["search"], "search", limit=200)
    return result


def _lookup_securities(conn, args, context):
    where, values = _market_filter(context)
    if args.get("search"):
        # instr treats the supplied search literally rather than as SQL wildcards.
        where.append("(instr(lower(NM_SECURITY),lower(?)) > 0 OR instr(DT_MATURITY,?) > 0)")
        values.extend([args["search"], args["search"]])
    values.append(args["limit"])
    rows = _fetch(conn, "SELECT ID_SECURITY,NM_SECURITY,NM_INSTRUMENT_TYPE,DT_MATURITY FROM FIXED_INCOME_SECURITY_CLASSIFICATION"
                  + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY DT_MATURITY,NM_SECURITY LIMIT ?", values)
    return {"kind": "query", "rows": rows, "source": "FIXED_INCOME_SECURITY_CLASSIFICATION", "filters": args, "count": len(rows)}


# Import-only conversations use extraction rather than exposing the tool registry
# to the model. Other routines retain the generic read/review/write tools below.
_IMPORT_CANDIDATE_FIELDS = ("NM_SECURITY", "MATURITY_HINT", "SIDE", "TX_QUOTE", "VL_QUOTE", "TS_REF", "SPREAD_QUOTE")
_IMPORT_CLARIFICATION_FIELDS = ("SOURCE", "NM_SECURITY", "MATURITY_HINT", "SIDE", "TX_QUOTE", "RATE_UNIT", "VL_QUOTE", "TS_REF", "SPREAD_QUOTE")


def import_message_schema(context=None):
    """Strict extraction contract; no free-form response or model-selected action."""
    spread_mode = context is not None and _market_scope(context)["quote_mode"] == "DI_SPREAD"
    row = _schema({
        "NM_SECURITY": {"type": ["string", "null"], "description": "Title name exactly as observed in the run; Python resolves the existing canonical security."},
        "MATURITY_HINT": {"type": ["string", "null"], "description": "Observed maturity only: YYYY-MM-DD, DD/MM/YYYY, YYYY-MM or four-digit YYYY. Never invent a missing date component."},
        "SIDE": {"type": ["string", "null"], "enum": ["Bid", "Ask", None], "description": "Broker quote side, only when explicit. Ambiguous compra/venda perspective requires clarification."},
        "TX_QUOTE": {"type": ["number", "null"], "description": "Use null for this spread import." if spread_mode else "Explicit quote normalized to decimal rate: 7.25% = 0.0725. Unknown unit requires clarification."},
        "VL_QUOTE": {"type": ["number", "null"], "minimum": 0, "description": "Explicit monetary volume only; null when absent. Never substitute a quantity of bonds."},
        "TS_REF": {"type": ["string", "null"], "description": "Original source ISO date/time, America/Sao_Paulo when timezone absent. Null when source time is unavailable."},
        "SPREAD_QUOTE": {"type": ["number", "null"], "description": "Explicit original spread in basis points; never calculate a DI or final nominal rate." if spread_mode else "Null for direct-rate imports."},
    }, _IMPORT_CANDIDATE_FIELDS)
    return _schema({
        "intent": {"type": "string", "enum": ["IMPORT", "CAPABILITIES", "CLARIFY", "OUT_OF_SCOPE"]},
        "source": {"type": ["string", "null"], "description": "Broker explicitly identified in the material; preserve a conflicting source instead of relabeling it XP. Null if not stated."},
        "clarification_fields": {"type": "array", "items": {"type": "string", "enum": list(_IMPORT_CLARIFICATION_FIELDS)}},
        "rows": {"type": "array", "items": row, "maxItems": MAX_IMPORT_ROWS, "description": "Observed import candidates only. Empty for capabilities, clarification and out-of-scope requests."},
    }, ("intent", "source", "clarification_fields", "rows"))


def get_quote_import_securities(conn, context):
    """Complete authorized inventory for internal validation, without tool calls."""
    if not _table_exists(conn, "FIXED_INCOME_SECURITY_CLASSIFICATION"):
        raise ValueError("A classificação dos títulos não está disponível para validar a importação.")
    where, values = _market_filter(context)
    return _fetch(conn, "SELECT ID_SECURITY,NM_SECURITY,NM_INSTRUMENT_TYPE,DT_MATURITY FROM FIXED_INCOME_SECURITY_CLASSIFICATION"
                  + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY DT_MATURITY,NM_SECURITY", values)


def _maturity_hint(value):
    if value in (None, ""):
        return None
    value = _text(value, "MATURITY_HINT", limit=100)
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
        try:
            return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
        except ValueError as exc:
            raise ValueError("MATURITY_HINT: a data de vencimento é inválida.") from exc
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        raise ValueError("MATURITY_HINT: informe o vencimento completo, mês AAAA-MM ou ano de quatro dígitos; não é possível supor a data.")
    try:
        datetime.strptime(value + ("-01-01" if len(value) == 4 else "-01" if len(value) == 7 else ""), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("MATURITY_HINT: a data de vencimento é inválida.") from exc
    return value


def resolve_import_candidates(conn, rows, context):
    """Resolve only exact names or explicit, unambiguous observed maturities.

    No quote is persisted here. The caller still uses the generic Python tool
    validation and persisted proposal/confirmation flow for the entire batch.
    """
    inventory = get_quote_import_securities(conn, context)
    if not inventory:
        raise ValueError("Nenhum título autorizado foi encontrado na classificação para esta rotina.")
    raw_rows = _rows({"rows": rows})
    spread_mode = _market_scope(context)["quote_mode"] == "DI_SPREAD"
    allowed_instruments = {item["NM_INSTRUMENT_TYPE"] for item in inventory}
    resolved = []
    for index, row in enumerate(raw_rows, 1):
        _object(row, set(QUOTE_COLUMNS) | {"ID_SECURITY", "MATURITY_HINT"})
        name = _text(row.get("NM_SECURITY"), "NM_SECURITY", optional=True, limit=200)
        hint = _maturity_hint(row.get("MATURITY_HINT"))
        # An explicit different sovereign instrument must not be relabeled just
        # because its maturity happens to match an authorized title.
        instrument_match = re.match(r"^(?:NTN[\s-]*([BCF])|\b(LFT|LTN))(?=$|[\s\d/-])", name, re.IGNORECASE)
        observed_instrument = None
        if instrument_match:
            observed_instrument = "NTN-" + instrument_match.group(1).upper() if instrument_match.group(1) else instrument_match.group(2).upper()
            if observed_instrument not in allowed_instruments:
                raise ValueError(f"Linha {index}: o tipo de título informado não pertence a esta rotina.")
        exact = [item for item in inventory if item["NM_SECURITY"] == name] if name else []
        if name and not exact and conn.execute(
                "SELECT 1 FROM FIXED_INCOME_SECURITY_CLASSIFICATION WHERE NM_SECURITY = ?", (name,)).fetchone():
            raise ValueError(f"Linha {index}: o título informado existe na classificação, mas não está autorizado para esta rotina.")
        if not exact and name:
            # A name consisting only of a date/year (optionally after its
            # explicit instrument prefix) contains no inferred date components.
            observed_date = name[instrument_match.end():].strip() if instrument_match else name
            if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?|\d{2}/\d{2}/\d{4}", observed_date):
                named_hint = _maturity_hint(observed_date)
                if hint and not (hint.startswith(named_hint) or named_hint.startswith(hint)):
                    raise ValueError(f"Linha {index}: os vencimentos informados são conflitantes.")
                hint = named_hint if not hint or len(named_hint) > len(hint) else hint
            elif observed_instrument is None:
                raise ValueError(f"Linha {index}: o nome informado não identifica um título desta rotina; esclareça o ativo antes de usar o vencimento.")
        if exact:
            candidates = exact
            if hint and not str(candidates[0].get("DT_MATURITY") or "").startswith(hint):
                raise ValueError(f"Linha {index}: o nome do título e o vencimento informado são conflitantes.")
        elif hint:
            candidates = [item for item in inventory if str(item.get("DT_MATURITY") or "").startswith(hint)]
        else:
            candidates = []
        if len(candidates) != 1:
            labels = ", ".join(item["NM_SECURITY"] for item in candidates[:8])
            detail = f" Candidatos: {labels}." if labels else ""
            raise ValueError(f"Linha {index}: informe um nome canônico ou vencimento que identifique um único título autorizado.{detail}")
        if row.get("SIDE") not in {"Bid", "Ask"}:
            raise ValueError(f"Linha {index}: esclareça SIDE como Bid ou Ask na perspectiva do broker.")
        if spread_mode and row.get("SPREAD_QUOTE") is None:
            raise ValueError(f"Linha {index}: informe SPREAD_QUOTE e sua unidade em pontos-base.")
        if not spread_mode and row.get("TX_QUOTE") is None:
            raise ValueError(f"Linha {index}: informe TX_QUOTE com unidade clara para normalização em taxa decimal.")
        if (spread_mode and row.get("TX_QUOTE") is not None) or (not spread_mode and row.get("SPREAD_QUOTE") is not None):
            raise ValueError(f"Linha {index}: informe apenas {'o spread original em pontos-base' if spread_mode else 'a taxa decimal'}, conforme o contrato desta rotina.")
        result = {key: value for key, value in row.items()
                  if key in QUOTE_COLUMNS and key not in {"TX_DI_REF", "TX_SPREAD"} and value is not None}
        result["NM_SECURITY"] = candidates[0]["NM_SECURITY"]
        resolved.append(result)
    return resolved


# ---- Debtors ----------------------------------------------------------------

def _client(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return _text(value, "ID_CLIENT", limit=100)


def _debtors_validate(conn, args, context):
    _object(args, {"rows"})
    normalized = []
    seen = set()
    for i, row in enumerate(_rows(args), 1):
        _object(row, DEBTOR_COLUMNS)
        client = _client(row.get("ID_CLIENT"))
        if client in seen:
            raise ValueError(f"Row {i}: the client occurs more than once in this import.")
        seen.add(client)
        if conn.execute("SELECT 1 FROM UGM_ROUTINES_DEVEDORES WHERE DT_REF = ? AND ID_CLIENT = ?", (context["dt_ref"], client)).fetchone():
            raise ValueError(f"Client {client} already has a case today. Use update_debtor to append information.")
        normalized.append({"ID_CLIENT": client,
                           "NR_DAYS": _integer(row.get("NR_DAYS"), "NR_DAYS", minimum=0),
                           "NM_RESP_GWM": _text(row.get("NM_RESP_GWM"), "NM_RESP_GWM", limit=200),
                           "DESCR": _text(row.get("DESCR"), "DESCR", optional=True, limit=20000),
                           "FL_RESOLVED": _integer(row.get("FL_RESOLVED", 1), "FL_RESOLVED", minimum=0, maximum=1)})
    return {"rows": normalized}


def _debtors_preview(conn, args, context):
    return {"kind": "import", "rows": args["rows"], "columns": DEBTOR_COLUMNS, "source": "UGM_ROUTINES_DEVEDORES", "warnings": ["FL_RESOLVED: 0 = resolved today; 1 = still a debtor."]}


def _debtors_execute(conn, args, context):
    conn.executemany("""INSERT INTO UGM_ROUTINES_DEVEDORES (DT_REF,ID_CLIENT,NR_DAYS,NM_RESP_GWM,DESCR,FL_RESOLVED)
        VALUES (?,?,?,?,?,?)""", [(context["dt_ref"], row["ID_CLIENT"], row["NR_DAYS"], row["NM_RESP_GWM"], row["DESCR"], row["FL_RESOLVED"]) for row in args["rows"]])
    return {"kind": "query", "source": "UGM_ROUTINES_DEVEDORES", "count": len(args["rows"]), "message": "Debtor cases imported."}


def _update_validate(conn, args, context):
    _object(args, {"client_id", "description", "resolved"})
    client = _client(args.get("client_id"))
    description = _text(args.get("description"), "description", limit=20000)
    if "resolved" in args and not isinstance(args["resolved"], bool):
        raise ValueError("resolved must be true or false.")
    row = conn.execute("SELECT 1 FROM UGM_ROUTINES_DEVEDORES WHERE DT_REF = ? AND ID_CLIENT = ?", (context["dt_ref"], client)).fetchone()
    if row is None:
        raise ValueError("No debtor case matches this exact account and operational date. Query the cases to clarify the client.")
    result = {"client_id": client, "description": description}
    if "resolved" in args:
        result["resolved"] = args["resolved"]
    return result


def _update_preview(conn, args, context):
    current = _fetch(conn, "SELECT ID_CLIENT,NR_DAYS,NM_RESP_GWM,DESCR,FL_RESOLVED FROM UGM_ROUTINES_DEVEDORES WHERE DT_REF = ? AND ID_CLIENT = ?", (context["dt_ref"], args["client_id"]))[0]
    proposed = {"description": args["description"]}
    if "resolved" in args:
        proposed["resolved"] = args["resolved"]
    return {"kind": "update", "current": current, "proposed": proposed, "rows": [proposed], "columns": list(proposed), "fields": list(proposed),
            "warnings": ["The description will be appended with a timestamp. Existing history is preserved."]}


def _update_execute(conn, args, context):
    addition = f"[{_now(context).strftime('%H:%M:%S')} {context['username']}] {args['description']}"
    if "resolved" in args:
        cursor = conn.execute("""UPDATE UGM_ROUTINES_DEVEDORES SET DESCR = CASE WHEN COALESCE(DESCR,'') = '' THEN ? ELSE DESCR || char(10) || ? END,
            FL_RESOLVED = ? WHERE DT_REF = ? AND ID_CLIENT = ?""", (addition, addition, 0 if args["resolved"] else 1, context["dt_ref"], args["client_id"]))
    else:
        cursor = conn.execute("""UPDATE UGM_ROUTINES_DEVEDORES SET DESCR = CASE WHEN COALESCE(DESCR,'') = '' THEN ? ELSE DESCR || char(10) || ? END
            WHERE DT_REF = ? AND ID_CLIENT = ?""", (addition, addition, context["dt_ref"], args["client_id"]))
    if cursor.rowcount != 1:
        raise ValueError("The update must affect exactly one client case.")
    return {"kind": "query", "source": "UGM_ROUTINES_DEVEDORES", "count": 1, "message": "Client case updated.",
            "rows": _fetch(conn, "SELECT * FROM UGM_ROUTINES_DEVEDORES WHERE DT_REF = ? AND ID_CLIENT = ?", (context["dt_ref"], args["client_id"]))}


def _query_debtors_validate(conn, args, context):
    _object(args, {"unresolved_only", "client_id"})
    result = {"unresolved_only": args.get("unresolved_only", False)}
    if not isinstance(result["unresolved_only"], bool):
        raise ValueError("unresolved_only must be true or false.")
    if args.get("client_id") not in (None, ""):
        result["client_id"] = _client(args["client_id"])
    return result


def _query_debtors(conn, args, context):
    where = ["DT_REF = ?"]
    values = [context["dt_ref"]]
    if args.get("unresolved_only"):
        where.append("FL_RESOLVED = 1")
    if "client_id" in args:
        where.append("ID_CLIENT = ?")
        values.append(args["client_id"])
    rows = _fetch(conn, "SELECT DT_REF,ID_CLIENT,NR_DAYS,NM_RESP_GWM,DESCR,FL_RESOLVED FROM UGM_ROUTINES_DEVEDORES WHERE " + " AND ".join(where) + " ORDER BY NR_DAYS DESC, ID_CLIENT", values)
    return {"kind": "query", "rows": rows, "source": "UGM_ROUTINES_DEVEDORES", "filters": {"DT_REF": context["dt_ref"], **args}, "count": len(rows)}


def _report_validate(conn, args, context):
    _object(args, {"unresolved_only"})
    return _query_debtors_validate(conn, {"unresolved_only": args.get("unresolved_only", True)}, context)


def _debtor_report(conn, args, context):
    result = _query_debtors(conn, args, context)
    blocks = [f"Conta: {row['ID_CLIENT']}\nDias devedor: {row['NR_DAYS']}\nResponsável GWM: {row['NM_RESP_GWM']}\nHistórico: {row['DESCR']}" for row in result["rows"]]
    body = "Prezados,\n\nSegue posição de clientes devedores em " + context["dt_ref"] + ".\n\n" + ("\n\n".join(blocks) if blocks else "Nenhum cliente devedor para os filtros selecionados.") + "\n\nAtenciosamente,"
    return {**result, "kind": "draft", "subject": "Clientes devedores — " + context["dt_ref"], "body": body, "warnings": ["Draft only. Review recipients and send using your email client."]}


# ---- Files / Outlook ---------------------------------------------------------
# Manual uploads are parsed by parse_import. debtor_report prepares a plain-text
# draft only. There is intentionally no mailbox search or autonomous send tool.


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


QUOTE_ROW_SCHEMA = _schema({
    "ID_SECURITY": {"type": "integer", "description": "Backend-resolved classification ID. Supplied values are ignored and resolved again from NM_SECURITY."},
    "NM_SECURITY": {"type": "string", "description": "Exact existing security classification name; never invent securities."},
    "SIDE": {"type": "string", "enum": ["Bid", "Ask"]},
    "TX_QUOTE": {"type": ["number", "null"], "description": "Decimal rate: 0.07 = 7%; null only for NTN-F spread input."},
    "VL_QUOTE": {"type": ["number", "null"], "minimum": 0},
    "TS_REF": {"type": ["string", "null"], "description": "Source ISO timestamp in America/Sao_Paulo; omit if unavailable."},
    "SPREAD_QUOTE": {"type": ["number", "null"], "description": "Original NTN-F source spread in basis points. Never invent DI or nominal rates."},
    "TX_DI_REF": {"type": ["number", "null"], "description": "Backend-derived DI reference rate; ignored and recomputed from the trusted provider."},
    "TX_SPREAD": {"type": ["number", "null"], "description": "Backend-derived nominal rate; ignored and recomputed deterministically."},
}, ["NM_SECURITY", "SIDE"])
DEBTOR_ROW_SCHEMA = _schema({
    "ID_CLIENT": {"type": "string", "description": "Exact account identifier; preserve leading zeros."},
    "NR_DAYS": {"type": "integer", "minimum": 0},
    "NM_RESP_GWM": {"type": "string"}, "DESCR": {"type": "string"},
    "FL_RESOLVED": {"type": "integer", "enum": [0, 1], "description": "0 resolved; 1 remains debtor."},
}, ["ID_CLIENT", "NR_DAYS", "NM_RESP_GWM", "DESCR"])

TOOLS = {
    "lookup_securities": ToolSpec(False, "Look up existing authorized security names and maturities before proposing an import; never guess classification identifiers.",
        _schema({"search": {"type": "string", "description": "Literal name fragment or ISO maturity date."}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}),
        _lookup_validate, _lookup_securities, _lookup_securities),
    "query_quotes": ToolSpec(False, "Query shared same-day quotes for this routine using exact authorized filters.",
        _schema({"security": {"type": "string"}, "source": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}),
        _query_quotes_validate, _query_quotes, _query_quotes),
    "save_quotes": ToolSpec(True, "Propose market quote import for editable review. Saving requires the card's confirmation button. Use decimal rates.",
        _schema({"source": {"type": "string", "enum": ["XP", "Santander", "Ativa", "BGC", "xBridge"]},
                 "rows": {"type": "array", "items": QUOTE_ROW_SCHEMA, "minItems": 1, "maxItems": MAX_IMPORT_ROWS}}, ["source", "rows"]),
        _quote_validate, _quote_preview, _quote_execute, review_fields=("TX_DI_REF", "TX_SPREAD")),
    "query_debtors": ToolSpec(False, "Query shared debtor cases for this date; FL_RESOLVED=1 means still a debtor.",
        _schema({"unresolved_only": {"type": "boolean"}, "client_id": {"type": "string"}}),
        _query_debtors_validate, _query_debtors, _query_debtors),
    "import_debtors": ToolSpec(True, "Propose new daily debtor cases for editable review. Existing cases must be updated, never overwritten.",
        _schema({"rows": {"type": "array", "items": DEBTOR_ROW_SCHEMA, "minItems": 1, "maxItems": MAX_IMPORT_ROWS}}, ["rows"]),
        _debtors_validate, _debtors_preview, _debtors_execute),
    "update_debtor": ToolSpec(True, "Propose appending a client update and optional resolution; exact client ID required. Button confirmation is mandatory.",
        _schema({"client_id": {"type": "string"}, "description": {"type": "string"}, "resolved": {"type": "boolean"}}, ["client_id", "description"]),
        _update_validate, _update_preview, _update_execute),
    "debtor_report": ToolSpec(False, "Prepare a same-day debtor closing report and email draft from shared SQLite data. Does not send email.",
        _schema({"unresolved_only": {"type": "boolean"}}), _report_validate, _debtor_report, _debtor_report),
}


def get_tool(handler_key):
    try:
        return TOOLS[handler_key]
    except KeyError as exc:
        raise ValueError("The configured tool handler is not registered.") from exc


def default_tool_names(routine_id):
    return (["query_debtors", "import_debtors", "update_debtor", "debtor_report"] if routine_id == "DEBTORS"
            else ["lookup_securities", "query_quotes", "save_quotes"])
