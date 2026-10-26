"""Versioned, feature-scoped prompts shared by all AI integrations."""

from contextlib import closing
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    id: int
    feature: str
    key: str
    version: int
    instructions: str
    model: str
    comment: str = None


class PromptStore:
    def __init__(self, database_path):
        self.database_path = Path(database_path)

    def ensure_schema(self):
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS AI_PROMPTS (
                    PROMPT_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    FEATURE TEXT NOT NULL,
                    PROMPT_KEY TEXT NOT NULL,
                    VERSION INTEGER NOT NULL CHECK (VERSION > 0),
                    INSTRUCTIONS TEXT NOT NULL,
                    MODEL TEXT NOT NULL,
                    "COMMENT" TEXT,
                    FL_ACTIVE INTEGER NOT NULL DEFAULT 1 CHECK (FL_ACTIVE IN (0, 1)),
                    DT_CREATED TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    UNIQUE (FEATURE, PROMPT_KEY, VERSION)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_prompts_active
                ON AI_PROMPTS (FEATURE, PROMPT_KEY) WHERE FL_ACTIVE = 1;
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(AI_PROMPTS)")}
            if "MODEL" not in columns:
                conn.execute("ALTER TABLE AI_PROMPTS ADD COLUMN MODEL TEXT")
            if "COMMENT" not in columns:
                conn.execute('ALTER TABLE AI_PROMPTS ADD COLUMN "COMMENT" TEXT')

    def seed(self, feature, key, instructions, model, comment=None):
        """Insert the initial prompt once; never overwrite an edited prompt."""
        self.ensure_schema()
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.execute(
                """INSERT OR IGNORE INTO AI_PROMPTS
                   (FEATURE, PROMPT_KEY, VERSION, INSTRUCTIONS, MODEL, "COMMENT")
                   SELECT ?, ?, 1, ?, ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM AI_PROMPTS WHERE FEATURE = ? AND PROMPT_KEY = ?
                   )""",
                (feature, key, instructions, model, comment, feature, key),
            )

    def get_active(self, feature, key):
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            row = conn.execute(
                """SELECT PROMPT_ID, FEATURE, PROMPT_KEY, VERSION, INSTRUCTIONS, MODEL, "COMMENT"
                   FROM AI_PROMPTS
                   WHERE FEATURE = ? AND PROMPT_KEY = ? AND FL_ACTIVE = 1""",
                (feature, key),
            ).fetchone()
        if row is None:
            raise LookupError(f"No active prompt for {feature}/{key}.")
        return Prompt(*row)

    def save_version(self, feature, key, instructions, model, comment=None):
        """Activate a version; omit comment to inherit it, or pass '' to clear it."""
        if not instructions.strip():
            raise ValueError("Prompt instructions cannot be empty.")
        if not model or not model.strip():
            raise ValueError("A model must be configured for the prompt.")
        self.ensure_schema()
        with closing(sqlite3.connect(self.database_path, timeout=30)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if comment is None:
                previous = conn.execute(
                    'SELECT "COMMENT" FROM AI_PROMPTS WHERE FEATURE = ? AND PROMPT_KEY = ? ORDER BY FL_ACTIVE DESC, VERSION DESC LIMIT 1',
                    (feature, key),
                ).fetchone()
                comment = previous[0] if previous else None
            version = conn.execute(
                "SELECT COALESCE(MAX(VERSION), 0) + 1 FROM AI_PROMPTS WHERE FEATURE = ? AND PROMPT_KEY = ?",
                (feature, key),
            ).fetchone()[0]
            conn.execute(
                "UPDATE AI_PROMPTS SET FL_ACTIVE = 0 WHERE FEATURE = ? AND PROMPT_KEY = ?",
                (feature, key),
            )
            conn.execute(
                """INSERT INTO AI_PROMPTS (FEATURE, PROMPT_KEY, VERSION, INSTRUCTIONS, MODEL, "COMMENT")
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (feature, key, version, instructions, model.strip(), comment),
            )
        return self.get_active(feature, key)
