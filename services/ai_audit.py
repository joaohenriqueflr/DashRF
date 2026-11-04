"""Shared SQLite audit storage for AI attempts."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


class AIRequestLog:
    def __init__(self, database_path):
        self.database_path = Path(database_path)

    def ensure_schema(self):
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS AI_REQUEST_LOG (
                    LOG_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    FEATURE TEXT NOT NULL,
                    FORM_TYPE TEXT NOT NULL,
                    USERNAME TEXT NOT NULL,
                    PROMPT_TEXT TEXT NOT NULL,
                    FULL_PROMPT TEXT,
                    DT_STARTED TEXT NOT NULL,
                    DT_FINISHED TEXT,
                    PROMPT_ID INTEGER NOT NULL,
                    PROMPT_VERSION INTEGER NOT NULL,
                    MODEL TEXT,
                    MODEL_OUTPUT TEXT,
                    INPUT_TOKENS INTEGER,
                    OUTPUT_TOKENS INTEGER,
                    TOTAL_TOKENS INTEGER,
                    STATUS TEXT NOT NULL CHECK (
                        STATUS IN ('running', 'completed', 'failed', 'invalid_response')
                    ),
                    ERROR_TYPE TEXT,
                    ERROR_MESSAGE TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_ai_request_log_feature_time
                ON AI_REQUEST_LOG (FEATURE, DT_STARTED DESC);
                CREATE INDEX IF NOT EXISTS idx_ai_request_log_user_time
                ON AI_REQUEST_LOG (USERNAME, DT_STARTED DESC);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(AI_REQUEST_LOG)")}
            for column, kind in (("FULL_PROMPT", "TEXT"), ("INPUT_TOKENS", "INTEGER"),
                                 ("OUTPUT_TOKENS", "INTEGER"), ("TOTAL_TOKENS", "INTEGER")):
                if column not in columns:
                    conn.execute(f"ALTER TABLE AI_REQUEST_LOG ADD COLUMN {column} {kind}")
            conn.commit()
            obsolete = columns & {"RESPONSE_ID", "JSON_OUTPUT", "RAW_OUTPUT"}
            if obsolete or "MODEL_OUTPUT" not in columns:
                conn.execute("BEGIN IMMEDIATE")
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
                    conn.execute("UPDATE AI_REQUEST_LOG SET MODEL_OUTPUT = COALESCE(" + ", ".join(sources) + ")")
                for column in sorted(obsolete):
                    conn.execute(f"ALTER TABLE AI_REQUEST_LOG DROP COLUMN {column}")

    def begin(self, *, feature, form_type, username, text, prompt, full_prompt=None):
        """Commit the user's attempt before the API call, releasing the DB lock."""
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            row = conn.execute(
                """INSERT INTO AI_REQUEST_LOG (
                       FEATURE, FORM_TYPE, USERNAME, PROMPT_TEXT, FULL_PROMPT, DT_STARTED,
                       PROMPT_ID, PROMPT_VERSION, MODEL, STATUS
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
                (feature, form_type, username, text,
                 json.dumps(full_prompt, ensure_ascii=False, allow_nan=False) if full_prompt is not None else None,
                 datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 prompt.id, prompt.version, prompt.model),
            )
            log_id = row.lastrowid
        return log_id

    def finish(self, log_id, *, status, result=None, error=None):
        """Save original model output, including output rejected by the form."""
        model_output = result.output_text if result is not None else getattr(error, "output_text", None)
        if model_output is None and result is not None:
            model_output = json.dumps(result.data, ensure_ascii=False)
        error_type = None
        error_message = None
        if error is not None:
            from services.openai_client import AIError

            error_type = getattr(error, "error_type", None) or type(error).__name__
            error_message = str(error) if isinstance(error, AIError) else "Unexpected error while processing the AI result."
        model = result.model if result is not None else getattr(error, "model", None)
        usage_source = result if result is not None else error
        usage = [getattr(usage_source, name, None) for name in ("input_tokens", "output_tokens", "total_tokens")]
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.execute(
                """UPDATE AI_REQUEST_LOG
                   SET DT_FINISHED = ?, STATUS = ?, MODEL_OUTPUT = ?, MODEL = COALESCE(?, MODEL),
                       INPUT_TOKENS = ?, OUTPUT_TOKENS = ?, TOTAL_TOKENS = ?,
                       ERROR_TYPE = ?, ERROR_MESSAGE = ?
                   WHERE LOG_ID = ?""",
                (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), status,
                 model_output, model, *usage, error_type, error_message, log_id),
            )


    def mark_invalid(self, log_id, *, error_type, message):
        """Record a completed model response rejected during form validation."""
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.execute(
                """UPDATE AI_REQUEST_LOG
                   SET STATUS = 'invalid_response',
                       ERROR_TYPE = COALESCE(NULLIF(ERROR_TYPE, ''), ?),
                       ERROR_MESSAGE = COALESCE(NULLIF(ERROR_MESSAGE, ''), ?)
                   WHERE LOG_ID = ? AND STATUS = 'completed'""",
                (error_type, message, log_id),
            )
