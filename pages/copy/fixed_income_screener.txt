from dash import html, register_page
import dash_bootstrap_components as dbc

register_page(__name__, path="/FI-Screener", name="Fixed Income Screener")

layout = dbc.Container(
    fluid=True,
    className="page-container",
    children=[
        html.Div(
            className="section-card ugm-placeholder",
            children=[
                html.H4("Fixed Income Screener", className="section-title"),
                html.Div(
                    "Layout and workflows will be added in the next iteration.",
                    className="section-subtitle",
                ),
            ],
        )
    ],
)
