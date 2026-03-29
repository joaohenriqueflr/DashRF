import secrets
import sqlite3
from datetime import date
from pathlib import Path

from dash import Input, Output, State, callback, dcc, html, no_update, register_page
import dash_ag_grid as dag
import dash_bootstrap_components as dbc

register_page(__name__, path="/keys", name="Access")

BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "data" / "fixed_income.db"

def _derive_email(user_name):
    if not user_name:
        return "user@trading.com"
    handle = "".join(
        ch.lower() if ch.isalnum() else "." for ch in user_name.strip()
    ).strip(".")
    while ".." in handle:
        handle = handle.replace("..", ".")
    return f"{handle}@trading.com" if handle else "user@trading.com"


def ensure_user_keys_table():
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS RFQ_USER_KEYS (
                KEY_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                SECRET_KEY TEXT UNIQUE,
                USR_NAME TEXT,
                EMAIL TEXT,
                ROLE TEXT,
                FL_ACTIVE INTEGER,
                DT_CREATED TEXT
            )
            """
        )
        columns = [row[1] for row in conn.execute("PRAGMA table_info(RFQ_USER_KEYS)")]
        if "EMAIL" not in columns:
            conn.execute("ALTER TABLE RFQ_USER_KEYS ADD COLUMN EMAIL TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rfq_user_keys_secret
            ON RFQ_USER_KEYS (SECRET_KEY)
            """
        )
        master_count = conn.execute(
            "SELECT COUNT(*) FROM RFQ_USER_KEYS WHERE ROLE = 'master' AND FL_ACTIVE = 1"
        ).fetchone()[0]
        if master_count == 0:
            master_key = secrets.token_urlsafe(12)
            conn.execute(
                """
                INSERT INTO RFQ_USER_KEYS (
                    SECRET_KEY,
                    USR_NAME,
                    EMAIL,
                    ROLE,
                    FL_ACTIVE,
                    DT_CREATED
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    master_key,
                    "Master User",
                    _derive_email("Master User"),
                    "master",
                    1,
                    date.today().isoformat(),
                ),
            )
            conn.commit()
            print(f"[DashRF] Created master key: {master_key}")
        rows = conn.execute(
            """
            SELECT KEY_ID, USR_NAME
            FROM RFQ_USER_KEYS
            WHERE EMAIL IS NULL OR EMAIL = ''
            """
        ).fetchall()
        for key_id, user_name in rows:
            conn.execute(
                "UPDATE RFQ_USER_KEYS SET EMAIL = ? WHERE KEY_ID = ?",
                (_derive_email(user_name), key_id),
            )
        conn.commit()


ensure_user_keys_table()

KEY_COLUMNS = [
    {"headerName": "User", "field": "user", "minWidth": 160},
    {"headerName": "Email", "field": "email", "minWidth": 200},
    {"headerName": "Role", "field": "role", "maxWidth": 120},
    {"headerName": "Secret Key", "field": "secret_key", "minWidth": 220},
    {"headerName": "Active", "field": "active", "maxWidth": 100},
    {"headerName": "Created", "field": "created", "minWidth": 140},
]

layout = dbc.Container(
    fluid=True,
    className="page-container keys-page",
    children=[
        dcc.Store(id="keys-refresh", data=0),
        html.Div(
            className="keys-hero",
            children=[
                html.H3("Access Keys", className="keys-title"),
                html.Div(
                    "Enter your secret key to identify your trader profile. "
                    "Master users can generate and review keys.",
                    className="keys-subtitle",
                ),
            ],
        ),
        html.Div(
            className="keys-card",
            children=[
                html.H4("Enter Secret Key", className="keys-card-title"),
                html.Div(
                    className="keys-form",
                    children=[
                        dcc.Input(
                            id="keys-input",
                            type="password",
                            placeholder="Paste your secret key",
                            className="keys-input",
                        ),
                        html.Button(
                            "Activate",
                            id="keys-activate",
                            className="keys-primary-btn",
                            type="button",
                        ),
                    ],
                ),
                html.Div(id="keys-status", className="keys-status"),
            ],
        ),
        html.Div(
            id="keys-admin-panel",
            className="keys-card",
            children=[
                html.H4("Master Key Management", className="keys-card-title"),
                html.Div(
                    className="keys-form",
                    children=[
                        dcc.Input(
                            id="keys-user-name",
                            type="text",
                            placeholder="User name",
                            className="keys-input",
                        ),
                        dcc.Dropdown(
                            id="keys-role",
                            options=[
                                {"label": "Trader", "value": "trader"},
                                {"label": "Master", "value": "master"},
                            ],
                            value="trader",
                            clearable=False,
                            className="keys-input keys-select",
                        ),
                        html.Button(
                            "Create Key",
                            id="keys-create",
                            className="keys-primary-btn",
                            type="button",
                        ),
                    ],
                ),
                html.Div(id="keys-create-status", className="keys-status"),
                dag.AgGrid(
                    id="keys-grid",
                    className="ag-theme-alpine keys-grid",
                    columnDefs=KEY_COLUMNS,
                    rowData=[],
                    defaultColDef={"sortable": True, "filter": True, "resizable": True},
                    style={"height": "260px"},
                ),
            ],
        ),
    ],
)


@callback(
    Output("current-user", "data"),
    Output("keys-status", "children"),
    Output("keys-input", "value"),
    Input("keys-activate", "n_clicks"),
    State("keys-input", "value"),
    prevent_initial_call=True,
)
def activate_key(n_clicks, secret_key):
    if not secret_key:
        return no_update, "Enter a secret key to activate.", no_update
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT KEY_ID, USR_NAME, EMAIL, ROLE
                FROM RFQ_USER_KEYS
                WHERE SECRET_KEY = ? AND FL_ACTIVE = 1
                """,
                (secret_key.strip(),),
            ).fetchone()
    except sqlite3.Error:
        return no_update, "Unable to validate key. Try again.", no_update

    if not row:
        return no_update, "Invalid or inactive key.", no_update
    user_data = {
        "key_id": row["KEY_ID"],
        "user": row["USR_NAME"],
        "email": row["EMAIL"],
        "role": row["ROLE"],
    }
    return user_data, f"Active session: {row['USR_NAME']} ({row['ROLE']}).", ""


@callback(
    Output("keys-admin-panel", "style"),
    Input("current-user", "data"),
)
def toggle_admin_panel(current_user):
    if current_user and current_user.get("role") == "master":
        return {"display": "block"}
    return {"display": "none"}


@callback(
    Output("keys-grid", "rowData"),
    Input("keys-refresh", "data"),
    Input("current-user", "data"),
)
def load_keys_grid(_refresh, current_user):
    if not current_user or current_user.get("role") != "master":
        return []
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            rows = conn.execute(
                """
                SELECT USR_NAME, EMAIL, ROLE, SECRET_KEY, FL_ACTIVE, DT_CREATED
                FROM RFQ_USER_KEYS
                ORDER BY DT_CREATED DESC
                """
            ).fetchall()
        return [
            {
                "user": row[0],
                "email": row[1],
                "role": row[2],
                "secret_key": row[3],
                "active": "Yes" if row[4] else "No",
                "created": row[5],
            }
            for row in rows
        ]
    except sqlite3.Error:
        return []


@callback(
    Output("keys-refresh", "data"),
    Output("keys-create-status", "children"),
    Input("keys-create", "n_clicks"),
    State("keys-user-name", "value"),
    State("keys-role", "value"),
    State("current-user", "data"),
    State("keys-refresh", "data"),
    prevent_initial_call=True,
)
def create_key(n_clicks, user_name, role, current_user, refresh_value):
    if not current_user or current_user.get("role") != "master":
        return no_update, "Only master users can create keys."
    if not user_name:
        return no_update, "Enter a user name before creating a key."
    role_value = role or "trader"
    new_key = secrets.token_urlsafe(10)
    email = _derive_email(user_name)
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.execute(
                """
                INSERT INTO RFQ_USER_KEYS (
                    SECRET_KEY,
                    USR_NAME,
                    EMAIL,
                    ROLE,
                    FL_ACTIVE,
                    DT_CREATED
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_key,
                    user_name.strip(),
                    email,
                    role_value,
                    1,
                    date.today().isoformat(),
                ),
            )
            conn.commit()
    except sqlite3.Error:
        return no_update, "Unable to create key. Try again."

    message = f"Key created for {user_name}: {new_key}"
    return (refresh_value or 0) + 1, message
