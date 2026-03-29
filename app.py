import re

from dash import Dash, Input, Output, callback, dcc, html, no_update
import dash
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc

app = Dash(
    __name__,
    use_pages=True,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    external_scripts=[
        "https://code.highcharts.com/highcharts.js",
        "https://code.highcharts.com/highcharts-more.js",
    ],
    suppress_callback_exceptions=True,
)

top_nav = html.Div(
    className="top-nav",
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
                dbc.NavLink(
                    "Fixed Income Monitor",
                    href="/",
                    active="exact",
                    className="nav-tab",
                ),
                dbc.NavLink(
                    "Fixed Income Portfolio",
                    href="/portfolio",
                    active="exact",
                    className="nav-tab",
                ),
                dbc.NavLink(
                    "Fixed Income Screener",
                    href="/FI-Screener",
                    active="exact",
                    className="nav-tab",
                ),
                dbc.NavLink(
                    "UGM Request for Quote",
                    href="/rfq",
                    active="exact",
                    className="nav-tab",
                ),
                dbc.NavLink(
                    "UGM Access",
                    href="/keys",
                    active="exact",
                    className="nav-tab",
                ),
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
                    className="user-fab user-fab-hidden",
                    children=[
                        html.Div(
                            className="user-fab-bubble",
                            children=[
                                html.Div(
                                    id="user-fab-initials",
                                    className="user-fab-initials",
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
                                                            id="user-fab-avatar-initials",
                                                            className="user-fab-avatar-initials",
                                                        ),
                                                    ],
                                                ),
                                                html.Div(
                                                    className="user-fab-header-text",
                                                    children=[
                                                        html.Div(
                                                            id="user-fab-name",
                                                            className="user-fab-name",
                                                        ),
                                                        html.Div(
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
                                                    className="user-fab-profile",
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
                                                    className="user-fab-logout",
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
        return "--", "--", "No active user", "", "", "user-fab user-fab-hidden"
    user_name = current_user["user"]
    initials = "".join([part[0] for part in user_name.split() if part]).upper()
    initials = initials[:2] if initials else "--"
    email = current_user.get("email")
    if not email:
        handle = re.sub(r"[^a-z0-9]+", ".", user_name.lower()).strip(".")
        email = f"{handle}@trading.com" if handle else "user@trading.com"
    role = current_user.get("role") or ""
    if role:
        role_display = role.replace("_", " ").strip().title()
    else:
        role_display = "--"
    return initials, initials, user_name, email, role_display, "user-fab"


@callback(
    Output("current-user", "data", allow_duplicate=True),
    Input("user-logout", "n_clicks"),
    prevent_initial_call=True,
)
def logout_user(_clicks):
    return None
if __name__ == "__main__":
    app.run(debug=True)
