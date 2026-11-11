import re

from dash import Dash, Input, Output, State, callback, ctx, dcc, html
from flask import session
import dash
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc

from services.access import install_access_controls, internal_access_denied, load_session_secret

app = Dash(
    __name__,
    use_pages=True,
    title="Unified Global Markets",
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    external_scripts=[
        "https://code.highcharts.com/highcharts.js",
        "https://code.highcharts.com/highcharts-more.js",
    ],
    suppress_callback_exceptions=True,
    routing_callback_inputs={"current_user": Input("current-user", "data")},
)
app.server.secret_key = load_session_secret()
app.server.config["SESSION_COOKIE_HTTPONLY"] = True
app.server.config["SESSION_COOKIE_SAMESITE"] = "Lax"

app.index_string = """
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        <link rel="icon" type="image/png" href="/assets/ubs-logo.png">
        {%css%}
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
"""

def nav_group(label, slug, links):
    return html.Div(
        id=f"nav-{slug}-group",
        className="nav-group",
        style={"display": "none"} if slug in ("fx", "ugm") else {},
        children=[
            html.Button(
                [label, html.Span(className="nav-chevron", **{"aria-hidden": "true"})],
                id=f"nav-{slug}-trigger",
                className="nav-tab nav-group-trigger",
                type="button",
                **{"aria-expanded": "false", "aria-controls": f"nav-{slug}-submenu"},
            ),
            html.Div(
                [
                    dbc.NavLink(name, href=href, active="exact", className="nav-tab nav-submenu-link")
                    for name, href in links
                ],
                id=f"nav-{slug}-submenu",
                className="nav-submenu",
                **{"aria-labelledby": f"nav-{slug}-trigger"},
            ),
        ],
    )


top_nav = html.Nav(
    className="top-nav",
    **{"aria-label": "Main navigation"},
    children=[
        html.Div(
            className="nav-brand",
            children=[
                html.Img(src="/assets/ubs-logo.png", className="nav-logo"),
            ],
        ),
        dbc.Nav(
            className="nav-tabs",
            children=[
                nav_group(
                    "Fixed Income",
                    "fixed-income",
                    [("Monitor", "/"), ("Portfolio", "/portfolio"), ("Screener", "/FI-Screener")],
                ),
                dbc.NavLink(
                    "Request for Quote",
                    href="/rfq",
                    active="exact",
                    className="nav-tab",
                ),
                nav_group("FX", "fx", [("Trading", "/fx-trading")]),
                nav_group("UGM", "ugm", [("Routines", "/routines"), ("AI Maintenance", "/ai-maintenance"), ("Access", "/keys")]),
            ],
        ),
        html.Div(className="nav-spacer"),
        html.Div("Unified Global Markets | Brazil", className="nav-brand-text nav-brand-right"),
    ],
)

app.layout = dmc.MantineProvider(
    children=[
        html.Div(
            children=[
                dcc.Store(id="current-user", storage_type="session"),
                top_nav,
                html.Div(
                    id="user-fab",
                    className="user-fab user-fab-guest",
                    children=[
                        html.Div(
                            className="user-fab-bubble",
                            children=[
                                html.Button(
                                    "--",
                                    id="user-fab-initials",
                                    className="user-fab-initials",
                                    type="button",
                                    **{"aria-label": "User account"},
                                ),
                                html.Div(
                                    className="user-fab-card",
                                    children=[
                                        html.Div(
                                            className="user-fab-header",
                                            children=[
                                                html.Div(
                                                    className="user-fab-avatar",
                                                    children=[
                                                        html.Div(
                                                            "--",
                                                            id="user-fab-avatar-initials",
                                                            className="user-fab-avatar-initials",
                                                        ),
                                                    ],
                                                ),
                                                html.Div(
                                                    className="user-fab-header-text",
                                                    children=[
                                                        html.Div(
                                                            "Log in",
                                                            id="user-fab-name",
                                                            className="user-fab-name",
                                                        ),
                                                        html.Div(
                                                            "Enter your secret key to continue.",
                                                            id="user-fab-email",
                                                            className="user-fab-email",
                                                        ),
                                                    ],
                                                ),
                                            ],
                                        ),
                                        html.Div(
                                            className="user-fab-body",
                                            children=[
                                                html.Div(
                                                    className="user-fab-login",
                                                    children=[
                                                        html.Label(
                                                            "Secret key",
                                                            htmlFor="user-login-key",
                                                            className="user-fab-profile-label",
                                                        ),
                                                        dcc.Input(
                                                            id="user-login-key",
                                                            type="password",
                                                            placeholder="Paste your secret key",
                                                            className="user-fab-key-input",
                                                            autoComplete="off",
                                                        ),
                                                        html.Button(
                                                            "Log in",
                                                            id="user-login",
                                                            className="user-fab-login-button",
                                                            type="button",
                                                        ),
                                                        html.Div(
                                                            id="user-login-status",
                                                            className="user-fab-login-status",
                                                            role="status",
                                                            **{"aria-live": "polite"},
                                                        ),
                                                    ],
                                                ),
                                                html.Div(
                                                    className="user-fab-profile user-fab-session",
                                                    children=[
                                                        html.Div(
                                                            "Access Profile",
                                                            className="user-fab-profile-label",
                                                        ),
                                                        html.Div(
                                                            id="user-fab-role",
                                                            className="user-fab-role",
                                                        ),
                                                    ],
                                                ),
                                                html.Button(
                                                    "Logout",
                                                    id="user-logout",
                                                    className="user-fab-logout user-fab-session",
                                                    type="button",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
                dash.page_container,
            ]
        )
    ],
)


@callback(
    Output("user-fab-initials", "children"),
    Output("user-fab-avatar-initials", "children"),
    Output("user-fab-name", "children"),
    Output("user-fab-email", "children"),
    Output("user-fab-role", "children"),
    Output("user-fab", "className"),
    Input("current-user", "data"),
)
def update_user_fab(current_user):
    if not current_user or not current_user.get("user"):
        return "--", "--", "Log in", "Enter your secret key to continue.", "", "user-fab user-fab-guest"
    user_name = current_user["user"]
    initials = "".join([part[0] for part in user_name.split() if part]).upper()
    initials = initials[:2] if initials else "--"
    email = current_user.get("email")
    if not email:
        handle = re.sub(r"[^a-z0-9]+", ".", user_name.lower()).strip(".")
        email = f"{handle}@trading.com" if handle else "user@trading.com"
    role = current_user.get("role") or ""
    if role:
        role_display = "GWM" if role.lower() == "gwm" else role.replace("_", " ").strip().title()
    else:
        role_display = "--"
    return initials, initials, user_name, email, role_display, "user-fab"


@callback(
    Output("current-user", "data", allow_duplicate=True),
    Output("user-login-status", "children"),
    Output("user-login-key", "value"),
    Input("user-login", "n_clicks"),
    Input("user-login-key", "n_submit"),
    Input("user-logout", "n_clicks"),
    State("user-login-key", "value"),
    prevent_initial_call=True,
)
def update_user_session(login_clicks, login_submits, logout_clicks, secret_key):
    if ctx.triggered_id == "user-logout":
        session.pop("user_key_id", None)
        return None, "", ""

    # Reuse the Access page's active-key validation and session format.
    from pages.user_keys import activate_key

    return activate_key(login_clicks or login_submits, secret_key)


@callback(
    Output("nav-fx-group", "style"), Output("nav-ugm-group", "style"),
    Input("current-user", "data"),
)
def restrict_navigation(current_user):
    style = {"display": "none"} if internal_access_denied(current_user) else {}
    return style, style


install_access_controls(app)


if __name__ == "__main__":
    app.run(debug=True)
