"""Shared SQLite audit storage for actual AI requests and labeled legacy totals."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


class AIRequestLog:
    def __init__(self, database_path):
        self.database_path = Path(database_path)

    def ensure_schema(self):
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS AI_REQUEST_LOG (
                LOG_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                FEATURE TEXT NOT NULL, FORM_TYPE TEXT NOT NULL, USERNAME TEXT NOT NULL,
                PROMPT_TEXT TEXT NOT NULL, FULL_PROMPT TEXT, DT_STARTED TEXT NOT NULL,
                DT_FINISHED TEXT, PROMPT_ID INTEGER, PROMPT_VERSION INTEGER,
                MODEL TEXT, MODEL_OUTPUT TEXT,
                INPUT_TOKENS INTEGER, OUTPUT_TOKENS INTEGER, TOTAL_TOKENS INTEGER,
                STATUS TEXT NOT NULL CHECK (STATUS IN ('running','completed','failed','invalid_response')),
                ERROR_TYPE TEXT, ERROR_MESSAGE TEXT,
                RECORD_TYPE TEXT NOT NULL DEFAULT 'REQUEST', CHAT_MESSAGE_ID INTEGER
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(AI_REQUEST_LOG)")}
            for column, kind in (("FULL_PROMPT", "TEXT"), ("INPUT_TOKENS", "INTEGER"),
                                 ("OUTPUT_TOKENS", "INTEGER"), ("TOTAL_TOKENS", "INTEGER"),
                                 ("RECORD_TYPE", "TEXT NOT NULL DEFAULT 'REQUEST'"), ("CHAT_MESSAGE_ID", "INTEGER")):
                if column not in columns:
                    conn.execute(f"ALTER TABLE AI_REQUEST_LOG ADD COLUMN {column} {kind}")
            obsolete = columns & {"RESPONSE_ID", "JSON_OUTPUT", "RAW_OUTPUT"}
            if obsolete or "MODEL_OUTPUT" not in columns:
                if "MODEL_OUTPUT" not in columns:
                    conn.execute("ALTER TABLE AI_REQUEST_LOG ADD COLUMN MODEL_OUTPUT TEXT")
                sources = ["MODEL_OUTPUT"]
                if "RAW_OUTPUT" in columns:
                    sources.append("NULLIF(RAW_OUTPUT, '')")
                if "JSON_OUTPUT" in columns:
                    sources.append("JSON_OUTPUT")
                if "RAW_OUTPUT" in columns:
                    sources.append("RAW_OUTPUT")
                if len(sources) > 1:
                    conn.execute("UPDATE AI_REQUEST_LOG SET MODEL_OUTPUT=COALESCE(" + ",".join(sources) + ")")
                for column in sorted(obsolete):
                    conn.execute(f"ALTER TABLE AI_REQUEST_LOG DROP COLUMN {column}")
            self._allow_unknown_prompt_provenance(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_request_log_feature_time ON AI_REQUEST_LOG (FEATURE,DT_STARTED DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_request_log_user_time ON AI_REQUEST_LOG (USERNAME,DT_STARTED DESC)")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_request_log_legacy_chat ON AI_REQUEST_LOG (CHAT_MESSAGE_ID)
                WHERE RECORD_TYPE='LEGACY_AGGREGATE'""")

    @staticmethod
    def _allow_unknown_prompt_provenance(conn):
        """Historical aggregate totals cannot be attributed to a guessed prompt."""
        info = list(conn.execute("PRAGMA table_info(AI_REQUEST_LOG)"))
        required = [row[1] for row in info if row[1] in {"PROMPT_ID", "PROMPT_VERSION"} and row[3]]
        if not required:
            return
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='AI_REQUEST_LOG'").fetchone()[0]
        for name in required:
            identifier = rf'(?:"{name}"|`{name}`|\[{name}\]|{name})'
            sql, changed = re.subn(rf'({identifier}\s+INTEGER)\s+NOT\s+NULL', r'\1', sql, count=1, flags=re.IGNORECASE)
            if changed != 1:
                raise sqlite3.OperationalError("Cannot migrate the AI audit prompt provenance constraint safely.")
        sql, changed = re.subn(r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"AI_REQUEST_LOG"|`AI_REQUEST_LOG`|\[AI_REQUEST_LOG\]|AI_REQUEST_LOG)',
                               'CREATE TABLE AI_REQUEST_LOG_PROVENANCE_MIGRATION', sql, count=1, flags=re.IGNORECASE)
        if changed != 1:
            raise sqlite3.OperationalError("Cannot migrate the AI audit table safely.")
        dependent_sql = [row[0] for row in conn.execute("""SELECT sql FROM sqlite_master
            WHERE tbl_name='AI_REQUEST_LOG' AND type IN ('index','trigger') AND sql IS NOT NULL""")]
        old_sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='AI_REQUEST_LOG'").fetchone()
        conn.execute(sql)
        names = [row[1] for row in info]
        quoted = ",".join('"' + name.replace('"', '""') + '"' for name in names)
        conn.execute("INSERT INTO AI_REQUEST_LOG_PROVENANCE_MIGRATION (" + quoted + ") SELECT " + quoted + " FROM AI_REQUEST_LOG")
        conn.execute("DROP TABLE AI_REQUEST_LOG")
        # Existing read-only views still refer to the final table name. Avoid
        # rewriting them to the temporary migration name during the rename.
        legacy_alter = conn.execute("PRAGMA legacy_alter_table").fetchone()[0]
        conn.execute("PRAGMA legacy_alter_table=ON")
        try:
            conn.execute("ALTER TABLE AI_REQUEST_LOG_PROVENANCE_MIGRATION RENAME TO AI_REQUEST_LOG")
        finally:
            conn.execute(f"PRAGMA legacy_alter_table={int(legacy_alter)}")
        for statement in dependent_sql:
            conn.execute(statement)
        if old_sequence:
            conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='AI_REQUEST_LOG'", (old_sequence[0],))

    def begin(self, *, feature, form_type, username, text, prompt, full_prompt=None,
              record_type="REQUEST", chat_message_id=None):
        """Commit the attempt before SDK access, releasing the SQLite lock."""
        if record_type not in {"REQUEST", "LEGACY_AGGREGATE"}:
            raise ValueError("Unknown AI request audit record type.")
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            row = conn.execute("""INSERT INTO AI_REQUEST_LOG (
                FEATURE,FORM_TYPE,USERNAME,PROMPT_TEXT,FULL_PROMPT,DT_STARTED,
                PROMPT_ID,PROMPT_VERSION,MODEL,STATUS,RECORD_TYPE,CHAT_MESSAGE_ID
                ) VALUES(?,?,?,?,?,?,?,?,?,'running',?,?)""",
                (feature, form_type, username, text,
                 json.dumps(full_prompt, ensure_ascii=False, allow_nan=False) if full_prompt is not None else None,
                 datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 prompt.id, prompt.version, prompt.model, record_type, chat_message_id))
            return row.lastrowid

    def finish(self, log_id, *, status, result=None, error=None):
        """Retain original output even when parsing or backend validation fails."""
        model_output = result.output_text if result is not None else getattr(error, "output_text", None)
        if result is not None and hasattr(result, "items"):
            # Function-call responses often have no text. Their original items
            # belong in the central audit, never in the compact human chat log.
            model_output = json.dumps(result.items, ensure_ascii=False, allow_nan=False)
        elif model_output is None and result is not None:
            model_output = json.dumps(result.data, ensure_ascii=False, allow_nan=False)
        error_type = error_message = None
        if error is not None:
            from services.openai_client import AIError
            error_type = getattr(error, "error_type", None) or type(error).__name__
            error_message = str(error) if isinstance(error, AIError) else "Unexpected error while processing the AI result."
        source = result if result is not None else error
        model = getattr(source, "model", None)
        usage = []
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(source, name, None)
            usage.append(value if type(value) is int and value >= 0 else None)
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.execute("""UPDATE AI_REQUEST_LOG SET DT_FINISHED=?,STATUS=?,MODEL_OUTPUT=?,MODEL=COALESCE(?,MODEL),
                INPUT_TOKENS=?,OUTPUT_TOKENS=?,TOTAL_TOKENS=?,ERROR_TYPE=?,ERROR_MESSAGE=? WHERE LOG_ID=?""",
                (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), status, model_output, model,
                 *usage, error_type, error_message, log_id))

    def mark_invalid(self, log_id, *, error_type, message):
        """A completed API response was rejected by deterministic application validation."""
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.execute("""UPDATE AI_REQUEST_LOG SET STATUS='invalid_response',
                ERROR_TYPE=COALESCE(NULLIF(ERROR_TYPE,''),?),ERROR_MESSAGE=COALESCE(NULLIF(ERROR_MESSAGE,''),?)
                WHERE LOG_ID=? AND STATUS='completed'""", (error_type, message, log_id))
