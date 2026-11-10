"""Read-only AI investigation queries; prompt activation uses the existing store."""

from contextlib import contextmanager
import csv
from datetime import date, datetime, time, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from services.prompts import PromptStore


LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
ALL = "__all__"
UNKNOWN = "__unknown__"
TABLES = {"chats": "UGM_ROUTINE_CHAT", "requests": "AI_REQUEST_LOG"}
TOKEN_FIELDS = ("INPUT_TOKENS", "OUTPUT_TOKENS", "TOTAL_TOKENS")
REQUEST_COLUMNS = (
    "LOG_ID", "FEATURE", "FORM_TYPE", "USERNAME", "DT_STARTED", "DT_FINISHED",
    "STATUS", "MODEL", "PROMPT_ID", "PROMPT_VERSION", *TOKEN_FIELDS,
    "RECORD_TYPE", "CHAT_MESSAGE_ID", "ERROR_TYPE",
)
CHAT_COLUMNS = (
    "ID_MESSAGE", "ID_ROUTINE", "USERNAME", "DT_REF", "TS_MESSAGE", "TS_RESPONSE",
    "STATUS", "ID_EXECUTION",
)


def parse_stamp(value, *, naive_tz=timezone.utc):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return stamp.replace(tzinfo=naive_tz) if stamp.tzinfo is None else stamp


def local_stamp(value, *, naive_tz=timezone.utc):
    stamp = parse_stamp(value, naive_tz=naive_tz)
    return stamp.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S") if stamp else None


def scope_value(feature, key):
    return json.dumps([feature, key], ensure_ascii=False)


def decode_scope(value):
    if value in (None, "", ALL):
        return None
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        feature, key = decoded
    except (ValueError, TypeError):
        raise ValueError("Choose a valid routine or feature.") from None
    if not isinstance(feature, str) or not isinstance(key, str):
        raise ValueError("Choose a valid routine or feature.")
    return feature, key


def day_bounds(start, end):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        raise ValueError("Choose a valid start and end date.") from None
    if first > last:
        raise ValueError("The start date must be on or before the end date.")
    lower = datetime.combine(first, time.min, LOCAL_TZ).astimezone(timezone.utc)
    upper = datetime.combine(last + timedelta(days=1), time.min, LOCAL_TZ).astimezone(timezone.utc)
    return lower.isoformat(), upper.isoformat()


class AIMaintenanceService:
    def __init__(self, database_path, *, clock=None):
        self.database_path = Path(database_path)
        self.clock = clock or (lambda: datetime.now(LOCAL_TZ))

    def now(self):
        stamp = self.clock()
        if stamp.tzinfo is None:
            raise ValueError("The maintenance clock must include a timezone.")
        return stamp.astimezone(LOCAL_TZ)

    @contextmanager
    def connection(self):
        # Browsing must never initialize, migrate, or create an operational DB.
        conn = sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.create_function("LOCAL_STAMP", 1, local_stamp, deterministic=True)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def exists(conn, table):
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def authorized(self, user):
        """Use the app's active master identity, rechecking its database role."""
        if not isinstance(user, dict) or not user.get("user") or user.get("role") != "master":
            return False
        try:
            with self.connection() as conn:
                if not self.exists(conn, "USER_KEYS"):
                    return False
                return bool(conn.execute("""SELECT 1 FROM USER_KEYS WHERE KEY_ID=? AND USR_NAME=?
                    AND ROLE='master' AND FL_ACTIVE=1""", (user.get("key_id"), user["user"])).fetchone())
        except sqlite3.Error:
            return False

    @staticmethod
    def _names(conn):
        if AIMaintenanceService.exists(conn, "UGM_ROUTINES"):
            return dict(conn.execute("SELECT ID_ROUTINE,NM_ROUTINE FROM UGM_ROUTINES"))
        return {}

    def metadata(self):
        with self.connection() as conn:
            users, scopes, statuses, models, versions = set(), set(), {"chats": set(), "requests": set()}, set(), set()
            names = self._names(conn)
            for kind, table in TABLES.items():
                if not self.exists(conn, table):
                    continue
                users.update(row[0] for row in conn.execute(f"SELECT DISTINCT USERNAME FROM {table}"))
                statuses[kind].update(row[0] for row in conn.execute(f"SELECT DISTINCT STATUS FROM {table}"))
                if kind == "requests":
                    scopes.update(tuple(row) for row in conn.execute("SELECT DISTINCT FEATURE,FORM_TYPE FROM AI_REQUEST_LOG"))
                    models.update(row[0] for row in conn.execute("SELECT DISTINCT MODEL FROM AI_REQUEST_LOG"))
                    versions.update(row[0] for row in conn.execute("SELECT DISTINCT PROMPT_VERSION FROM AI_REQUEST_LOG"))
                else:
                    scopes.update(("routines", row[0]) for row in conn.execute("SELECT DISTINCT ID_ROUTINE FROM UGM_ROUTINE_CHAT"))
            scopes.update(("routines", routine) for routine in names)
            prompts = [dict(row) for row in conn.execute("""SELECT PROMPT_ID,FEATURE,PROMPT_KEY,VERSION,MODEL,DT_CREATED
                FROM AI_PROMPTS WHERE FL_ACTIVE=1 ORDER BY FEATURE,PROMPT_KEY"""
            )] if self.exists(conn, "AI_PROMPTS") else []
            return {
                "users": [{"label": u or "Unattributed", "value": u if u else UNKNOWN} for u in sorted(users, key=lambda u: u or "")],
                "workloads": [{"label": names.get(key, key) + ("" if feature == "routines" else f" · {feature}"),
                               "value": scope_value(feature, key)} for feature, key in sorted(scopes)],
                "statuses": {kind: sorted(values) for kind, values in statuses.items()},
                "models": [{"label": m or "Unknown model", "value": m if m is not None else UNKNOWN}
                           for m in sorted(models, key=lambda m: m or "")],
                "versions": [{"label": f"v{v}" if v is not None else "Unknown version", "value": str(v) if v is not None else UNKNOWN}
                             for v in sorted(versions, key=lambda v: -1 if v is None else v)],
                "prompts": prompts,
            }

    def _where(self, kind, filters):
        start, end = filters.get("start"), filters.get("end")
        lower, upper = day_bounds(start, end)
        clauses, params = [], []
        if kind == "chats":
            clauses.append("r.DT_REF>=? AND r.DT_REF<=?")
            params.extend((start, end))
        else:
            clauses.append("julianday(r.DT_STARTED)>=julianday(?) AND julianday(r.DT_STARTED)<julianday(?)")
            params.extend((lower, upper))
        user = filters.get("user", ALL)
        if user not in (None, ALL):
            clauses.append("COALESCE(NULLIF(r.USERNAME,''),?)=?")
            params.extend((UNKNOWN, user))
        scope = decode_scope(filters.get("workload"))
        if scope:
            feature, key = scope
            if kind == "chats":
                clauses.append("r.ID_ROUTINE=?" if feature == "routines" else "0")
                if feature == "routines":
                    params.append(key)
            else:
                clauses.append("r.FEATURE=? AND r.FORM_TYPE=?")
                params.extend((feature, key))
        status = filters.get("chat_status" if kind == "chats" else "request_status", ALL)
        if status not in (None, ALL):
            clauses.append("r.STATUS=?")
            params.append(status)
        if kind == "requests":
            for option, field in (("model", "MODEL"), ("version", "PROMPT_VERSION"), ("record_type", "RECORD_TYPE")):
                value = filters.get(option, ALL)
                if value not in (None, ALL):
                    if value == UNKNOWN:
                        clauses.append(f"r.{field} IS NULL")
                    else:
                        clauses.append(f"r.{field}=?")
                        params.append(value)
            usage = filters.get("usage", ALL)
            if usage in ("missing", "reported"):
                clauses.append("r.TOTAL_TOKENS IS " + ("NULL" if usage == "missing" else "NOT NULL"))
            if filters.get("chat_id") is not None:
                clauses.append("r.CHAT_MESSAGE_ID=?")
                params.append(int(filters["chat_id"]))
        query = (filters.get("chat_query" if kind == "chats" else "request_query") or "").strip()
        if query:
            with self.connection() as conn:
                fields = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLES[kind]})")]
            references = ['r."' + field.replace('"', '""') + '"' for field in fields]
            clauses.append("(" + " OR ".join(f"instr(lower(CAST({field} AS TEXT)),lower(?))>0" for field in references) + ")" if fields else "0")
            params.extend([query] * len(fields))
        return " AND ".join(clauses), params

    @staticmethod
    def _sort(kind, sort_model):
        allowed = set(CHAT_COLUMNS if kind == "chats" else REQUEST_COLUMNS)
        timestamp = "TS_MESSAGE" if kind == "chats" else "DT_STARTED"
        identity = "ID_MESSAGE" if kind == "chats" else "LOG_ID"
        expressions = {field: f"r.{field}" for field in allowed}
        expressions.update({"LOCAL_TIME": f"julianday(r.{timestamp})", "DURATION_MS":
                            "CASE WHEN r.RECORD_TYPE='REQUEST' THEN (julianday(r.DT_FINISHED)-julianday(r.DT_STARTED))*86400000 END"})
        expressions[timestamp] = f"julianday(r.{timestamp})"
        if kind == "chats":
            expressions.pop("DURATION_MS")
        parts = []
        for spec in (sort_model or [])[:4]:
            expression = expressions.get(spec.get("colId"))
            if expression:
                direction = "DESC" if spec.get("sort") == "desc" else "ASC"
                parts.extend((f"({expression}) IS NULL", f"{expression} {direction}"))
        return ",".join(parts + [f"julianday(r.{timestamp}) DESC", f"r.{identity} DESC"])

    def browse(self, kind, filters, *, start=0, limit=25, sort_model=None):
        if kind not in TABLES:
            raise ValueError("Unknown log table.")
        start, limit = max(0, int(start)), min(500, max(1, int(limit)))
        where, params = self._where(kind, filters)
        table = TABLES[kind]
        with self.connection() as conn:
            if not self.exists(conn, table):
                return {"rows": [], "count": 0}
            count = conn.execute(f"SELECT COUNT(*) FROM {table} r WHERE {where}", params).fetchone()[0]
            columns = ",".join(f"r.{field}" for field in (CHAT_COLUMNS if kind == "chats" else REQUEST_COLUMNS))
            if kind == "chats":
                columns += ",substr(r.USER_MESSAGE,1,140) AS PREVIEW"
                if "ID_ACTIVITY" in {row[1] for row in conn.execute("PRAGMA table_info(UGM_ROUTINE_CHAT)")}:
                    columns += ",r.ID_ACTIVITY"
                if self.exists(conn, "AI_REQUEST_LOG"):
                    columns += "," + """(SELECT COUNT(*) FROM AI_REQUEST_LOG a WHERE a.CHAT_MESSAGE_ID=r.ID_MESSAGE
                        AND a.RECORD_TYPE='REQUEST') AS REQUESTS,
                        (SELECT COUNT(*) FROM AI_REQUEST_LOG a WHERE a.CHAT_MESSAGE_ID=r.ID_MESSAGE
                        AND a.RECORD_TYPE='LEGACY_AGGREGATE') AS HISTORICAL_RECORDS"""
            rows = [dict(row) for row in conn.execute(f"SELECT {columns} FROM {table} r WHERE {where} ORDER BY "
                    + self._sort(kind, sort_model) + " LIMIT ? OFFSET ?", [*params, limit, start])]
            names = self._names(conn)
            for row in rows:
                row["ROUTINE_NAME"] = names.get(row.get("ID_ROUTINE", row.get("FORM_TYPE")), row.get("ID_ROUTINE", row.get("FORM_TYPE")))
                if kind == "requests":
                    row["LOCAL_TIME"] = local_stamp(row["DT_STARTED"])
                    first, last = parse_stamp(row["DT_STARTED"]), parse_stamp(row["DT_FINISHED"])
                    row["DURATION_MS"] = round((last - first).total_seconds() * 1000) if (
                        row["RECORD_TYPE"] == "REQUEST" and first and last and last >= first) else None
                else:
                    row["LOCAL_TIME"] = local_stamp(row["TS_MESSAGE"], naive_tz=LOCAL_TZ)
            return {"rows": rows, "count": count}

    def record(self, kind, record_id):
        if kind not in TABLES:
            raise ValueError("Unknown log table.")
        field = "ID_MESSAGE" if kind == "chats" else "LOG_ID"
        with self.connection() as conn:
            if not self.exists(conn, TABLES[kind]):
                return None
            row = conn.execute(f"SELECT * FROM {TABLES[kind]} WHERE {field}=?", (int(record_id),)).fetchone()
            return dict(row) if row else None

    def matching_record(self, kind, record_id, filters):
        where, params = self._where(kind, filters)
        field = "ID_MESSAGE" if kind == "chats" else "LOG_ID"
        with self.connection() as conn:
            if not self.exists(conn, TABLES[kind]):
                return None
            row = conn.execute(f"SELECT r.* FROM {TABLES[kind]} r WHERE {where} AND r.{field}=?",
                               [*params, int(record_id)]).fetchone()
            return dict(row) if row else None

    def conversation(self, chat_id):
        row = self.record("chats", chat_id)
        if not row:
            return []
        clauses, params = ["ID_ROUTINE=?", "USERNAME=?", "DT_REF=?"], [row["ID_ROUTINE"], row["USERNAME"], row["DT_REF"]]
        if "ID_ACTIVITY" in row:
            clauses.append("COALESCE(ID_ACTIVITY,ID_ROUTINE || ':DEFAULT')=?")
            params.append(row["ID_ACTIVITY"] or row["ID_ROUTINE"] + ":DEFAULT")
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM UGM_ROUTINE_CHAT WHERE " + " AND ".join(clauses)
                    + " ORDER BY julianday(TS_MESSAGE),ID_MESSAGE", params)]

    def linked_context(self, chat_id):
        chat = self.record("chats", chat_id)
        if not chat:
            raise ValueError("The originating chat is no longer available.")
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM AI_REQUEST_LOG WHERE CHAT_MESSAGE_ID=? ORDER BY LOG_ID", (chat_id,))
                    ] if self.exists(conn, "AI_REQUEST_LOG") else []
        dates = sorted(stamp[:10] for row in rows if (stamp := local_stamp(row["DT_STARTED"])))
        return {"chat": chat, "records": rows, "start": dates[0] if dates else chat["DT_REF"],
                "end": dates[-1] if dates else chat["DT_REF"]}

    def summary(self, kind, filters):
        where, params = self._where(kind, filters)
        with self.connection() as conn:
            if not self.exists(conn, TABLES[kind]):
                return {"records": 0, "requests": 0, "historical": 0, "missing": 0, "total": None}
            if kind == "chats":
                return {"records": conn.execute(f"SELECT COUNT(*) FROM UGM_ROUTINE_CHAT r WHERE {where}", params).fetchone()[0]}
            return dict(conn.execute(f"""SELECT COUNT(*) AS records,
                COALESCE(SUM(RECORD_TYPE='REQUEST'),0) AS requests,
                COALESCE(SUM(RECORD_TYPE='LEGACY_AGGREGATE'),0) AS historical,
                COALESCE(SUM(TOTAL_TOKENS IS NULL),0) AS missing,SUM(TOTAL_TOKENS) AS total
                FROM AI_REQUEST_LOG r WHERE {where}""", params).fetchone())

    def usage(self, as_of, *, user=ALL, workload=ALL, period="MTD"):
        selected = date.fromisoformat(as_of)
        month = selected.replace(day=1).isoformat()
        base = {"start": month, "end": as_of, "user": user, "workload": workload}
        selected_filters = {**base, "start": as_of if period == "DLY" else month}
        empty = {"REQUESTS": 0, "HISTORICAL_RECORDS": 0, "MISSING_USAGE": 0, **dict.fromkeys(TOKEN_FIELDS)}
        aggregates = """COUNT(CASE WHEN RECORD_TYPE='REQUEST' THEN 1 END) AS REQUESTS,
            COUNT(CASE WHEN RECORD_TYPE='LEGACY_AGGREGATE' THEN 1 END) AS HISTORICAL_RECORDS,
            COUNT(CASE WHEN RECORD_TYPE='REQUEST' AND TOTAL_TOKENS IS NULL THEN 1 END) AS MISSING_USAGE,
            SUM(INPUT_TOKENS) AS INPUT_TOKENS,SUM(OUTPUT_TOKENS) AS OUTPUT_TOKENS,SUM(TOTAL_TOKENS) AS TOTAL_TOKENS"""
        with self.connection() as conn:
            has_requests = self.exists(conn, "AI_REQUEST_LOG")
            def totals(filters):
                if not has_requests:
                    return dict(empty)
                where, params = self._where("requests", filters)
                return dict(conn.execute(f"SELECT {aggregates} FROM AI_REQUEST_LOG r WHERE {where}", params).fetchone())
            daily, monthly = totals({**base, "start": as_of}), totals(base)
            where, params = self._where("requests", selected_filters)
            grouped = {}
            for label, fields in (("users", "USERNAME"), ("workloads", "FEATURE,FORM_TYPE"),
                                  ("models", "NULLIF(TRIM(MODEL),'') AS MODEL"),
                                  ("user_routines", "USERNAME,FEATURE,FORM_TYPE")):
                group_fields = "NULLIF(TRIM(MODEL),'')" if label == "models" else fields
                grouped[label] = [dict(row) for row in conn.execute(f"SELECT {fields},{aggregates} FROM AI_REQUEST_LOG r WHERE {where} "
                    f"GROUP BY {group_fields} ORDER BY SUM(TOTAL_TOKENS) DESC", params)] if has_requests else []
            # Aggregate chats separately: joining raw calls would count an exchange
            # once per model call, and would omit interactions processed in Python.
            has_chats = self.exists(conn, "UGM_ROUTINE_CHAT")
            user_routines = {(r["USERNAME"], r["FEATURE"], r["FORM_TYPE"]): r for r in grouped["user_routines"]}
            for row in user_routines.values():
                row["INTERACTIONS"] = 0 if has_chats and row["FEATURE"] == "routines" else None
            if has_chats:
                chat_where, chat_params = self._where("chats", selected_filters)
                for chat in conn.execute(f"""SELECT USERNAME,ID_ROUTINE,COUNT(*) AS INTERACTIONS
                        FROM UGM_ROUTINE_CHAT r WHERE {chat_where} GROUP BY USERNAME,ID_ROUTINE""", chat_params):
                    key = (chat["USERNAME"], "routines", chat["ID_ROUTINE"])
                    if key not in user_routines:
                        user_routines[key] = {**dict(empty), **dict.fromkeys(TOKEN_FIELDS, 0),
                                              "USERNAME": chat["USERNAME"], "FEATURE": "routines", "FORM_TYPE": chat["ID_ROUTINE"]}
                    user_routines[key]["INTERACTIONS"] = chat["INTERACTIONS"]
            grouped["user_routines"] = sorted(user_routines.values(), key=lambda row: (
                row["TOTAL_TOKENS"] is None, -(row["TOTAL_TOKENS"] or 0), row["USERNAME"] or "", row["FEATURE"], row["FORM_TYPE"]))
            bucket = "substr(LOCAL_STAMP(DT_STARTED),12,2) || ':00'" if period == "DLY" else "substr(LOCAL_STAMP(DT_STARTED),1,10)"
            trend = [dict(row) for row in conn.execute(f"SELECT {bucket} AS BUCKET,{aggregates} FROM AI_REQUEST_LOG r "
                f"WHERE {where} GROUP BY {bucket} ORDER BY BUCKET", params)] if has_requests else []
            names = self._names(conn)
            for label in ("workloads", "user_routines"):
                for row in grouped[label]:
                    row["WORKLOAD"] = scope_value(row["FEATURE"], row["FORM_TYPE"])
                    row["ROUTINE_NAME"] = (names.get(row["FORM_TYPE"], row["FORM_TYPE"]) if row["FEATURE"] == "routines"
                                           else row["FORM_TYPE"] + " · " + row["FEATURE"])
            return {"daily": daily, "monthly": monthly, "selected": daily if period == "DLY" else monthly,
                    **grouped, "trend": trend}

    def prompt(self, prompt_id):
        with self.connection() as conn:
            if not self.exists(conn, "AI_PROMPTS"):
                return None
            row = conn.execute("SELECT * FROM AI_PROMPTS WHERE PROMPT_ID=?", (int(prompt_id),)).fetchone()
            return dict(row) if row else None

    def prompt_history(self, prompt_id):
        row = self.prompt(prompt_id)
        if not row:
            return []
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("""SELECT * FROM AI_PROMPTS WHERE FEATURE=? AND PROMPT_KEY=?
                ORDER BY VERSION DESC""", (row["FEATURE"], row["PROMPT_KEY"]))]

    def activate_prompt(self, prompt_id, instructions, model, comment, *, user):
        if not self.authorized(user):
            raise PermissionError("Active master access is required to activate a prompt.")
        row = self.prompt(prompt_id)
        if not row:
            raise ValueError("The prompt is no longer available.")
        return PromptStore(self.database_path).save_version(row["FEATURE"], row["PROMPT_KEY"], instructions,
            model, comment, expected_version=row["VERSION"], created_by=user["user"])

    def export_csv(self, kind, filters, sort_model=None):
        """Export the entire filtered result, never just the loaded grid block."""
        if kind not in TABLES:
            raise ValueError("Unknown log table.")
        where, params = self._where(kind, filters)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        with self.connection() as conn:
            if not self.exists(conn, TABLES[kind]):
                return ""
            cursor = conn.execute(f"SELECT r.* FROM {TABLES[kind]} r WHERE {where} ORDER BY " + self._sort(kind, sort_model), params)
            writer.writerow([column[0] for column in cursor.description])
            for row in cursor:
                # Preserve numeric cells, and prevent chat text from becoming a spreadsheet formula.
                writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                                 else value for value in row])
        return stream.getvalue()
