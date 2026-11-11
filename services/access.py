"""Shared profile restrictions for navigation, page routing, and callbacks."""

from contextlib import closing
from pathlib import Path
import os
import secrets
import sqlite3
import tempfile

from flask import current_app, has_request_context, redirect, request, session


DATABASE_PATH = Path(__file__).resolve().parents[1] / "data" / "fixed_income.db"
GWM_RESTRICTED_PATHS = ("/fx-trading", "/routines", "/ai-maintenance", "/keys")
GWM_RESTRICTED_MODULES = {
    "pages.fx_trading", "pages.routines", "pages.routine_overview",
    "pages.ai_maintenance", "pages.user_keys",
}


def load_session_secret(path=None):
    """Keep signed logins valid across restarts and local server processes."""
    configured = os.environ.get("DASH_SECRET_KEY")
    if configured:
        return configured
    path = Path(path) if path is not None else DATABASE_PATH.parent.parent / ".instance" / "session-secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Publish a complete private file atomically if several workers start together.
        descriptor, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_hex(32))
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    secret = path.read_text().strip()
    if not secret:
        raise RuntimeError("The session signing key is empty. Set DASH_SECRET_KEY before starting the app.")
    return secret


def current_profile(user=None, database_path=None):
    """Recheck stored identities; a signed login cookie takes priority over UI state."""
    user = user if isinstance(user, dict) else {}
    cookie_id = session.get("user_key_id") if has_request_context() else None
    if has_request_context() and current_app.config.get("DASHRF_PROFILE_ACCESS") and cookie_id is None:
        return None
    key_id = cookie_id if cookie_id is not None else (user or {}).get("key_id")
    if key_id is None:
        return user if isinstance(user, dict) and user.get("user") else None
    try:
        with closing(sqlite3.connect(database_path or DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT KEY_ID, USR_NAME, EMAIL, ROLE FROM USER_KEYS WHERE KEY_ID=? AND FL_ACTIVE=1",
                (key_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or (cookie_id is None and row["USR_NAME"] != (user or {}).get("user")):
        return None
    return {"key_id": row["KEY_ID"], "user": row["USR_NAME"],
            "email": row["EMAIL"], "role": row["ROLE"]}


def is_gwm(user=None, database_path=None):
    profile = current_profile(user, database_path)
    return bool(profile and str(profile.get("role", "")).lower() == "gwm")


def internal_access_denied(user=None):
    user = user if isinstance(user, dict) else {}
    profile = current_profile(user)
    if profile:
        return str(profile.get("role", "")).lower() == "gwm"
    if has_request_context() and current_app.config.get("DASHRF_PROFILE_ACCESS"):
        return True
    # A revoked identity or a failed lookup must not open restricted pages.
    cookie_id = session.get("user_key_id") if has_request_context() else None
    return cookie_id is not None or (user or {}).get("key_id") is not None or is_gwm(user)


def restricted_path(path):
    path = (path or "/").split("?", 1)[0].rstrip("/").lower()
    return any(path == prefix or path.startswith(prefix + "/") for prefix in GWM_RESTRICTED_PATHS)


def guard_layout(layout):
    def render(current_user=None, **kwargs):
        if internal_access_denied(current_user):
            from dash import dcc, html

            return html.Div([
                html.H3("Access unavailable"),
                html.P("This page is unavailable for the GWM profile." if is_gwm(current_user)
                       else "Log in using the account bubble to access this page."),
                dcc.Link("Open Request for Quote", href="/rfq"),
            ], className="page-container", role="alert")
        return layout(**kwargs) if callable(layout) else layout
    return render


def install_access_controls(app):
    """Block restricted routes and server callbacks, including direct requests."""
    import dash

    app.server.config["DASHRF_PROFILE_ACCESS"] = True
    for page in dash.page_registry.values():
        if restricted_path(page["path"]):
            page["layout"] = guard_layout(page["layout"])

    @app.server.before_request
    def enforce_profile_access():
        callback_request = request.path.rstrip("/").endswith("/_dash-update-component")
        if not callback_request and not restricted_path(request.path):
            return None
        body = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
        user = next((item.get("value") for group in ("inputs", "state")
                     for item in body.get(group, []) if isinstance(item, dict)
                     and item.get("id") == "current-user" and item.get("property") == "data"), None)
        if not internal_access_denied(user):
            return None
        if restricted_path(request.path):
            return redirect("/rfq")
        if callback_request:
            callback = app.callback_map.get(body.get("output"), {}).get("callback")
            if getattr(callback, "__module__", None) in GWM_RESTRICTED_MODULES:
                return {"error": "This page is unavailable for the GWM profile."}, 403
        return None
