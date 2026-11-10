"""UGM AI Maintenance: usage, versioned prompts, and linked audit explorers."""

from datetime import date
import difflib
import hashlib
import json
import logging
from pathlib import Path

from dash import Input, Output, State, callback, ctx, dcc, html, no_update, register_page
from dash.exceptions import PreventUpdate
import dash_ag_grid as dag
import plotly.graph_objects as go

from services.ai_maintenance import AIMaintenanceService, ALL, UNKNOWN, LOCAL_TZ, local_stamp, scope_value
from services.prompts import PromptConflictError


register_page(__name__, path="/ai-maintenance", name="AI Maintenance", title="AI Maintenance | UGM")
DATABASE_PATH = Path(__file__).resolve().parents[1] / "data" / "fixed_income.db"
logger = logging.getLogger(__name__)

CHAT_FIELDS = [
    ("ID_MESSAGE", "Chat ID"), ("LOCAL_TIME", "Message time · São Paulo"), ("DT_REF", "Operational date"),
    ("USERNAME", "User"), ("ID_ROUTINE", "Routine"), ("ID_ACTIVITY", "Activity"), ("STATUS", "Status"),
    ("PREVIEW", "User message"), ("REQUESTS", "AI calls"), ("HISTORICAL_RECORDS", "Historical records"),
    ("ID_EXECUTION", "Execution ID"),
]
REQUEST_FIELDS = [
    ("LOG_ID", "Log ID"), ("LOCAL_TIME", "Started · São Paulo"), ("USERNAME", "User"),
    ("FEATURE", "Feature"), ("FORM_TYPE", "Routine / form"), ("STATUS", "Status"), ("MODEL", "Model"),
    ("PROMPT_ID", "Prompt ID"), ("PROMPT_VERSION", "Prompt version"), ("INPUT_TOKENS", "Input tokens"),
    ("OUTPUT_TOKENS", "Output tokens"), ("TOTAL_TOKENS", "Total tokens"), ("DURATION_MS", "Elapsed · ms"),
    ("RECORD_TYPE", "Record type"), ("CHAT_MESSAGE_ID", "Chat ID"), ("ERROR_TYPE", "Error type"),
]
DEFAULT_CHAT_FIELDS = ["ID_MESSAGE", "LOCAL_TIME", "USERNAME", "ID_ROUTINE", "STATUS", "PREVIEW", "REQUESTS"]
DEFAULT_REQUEST_FIELDS = ["LOG_ID", "LOCAL_TIME", "USERNAME", "FORM_TYPE", "STATUS", "TOTAL_TOKENS", "PROMPT_VERSION", "RECORD_TYPE"]
NUMERIC = {"REQUESTS", "HISTORICAL_RECORDS", "INPUT_TOKENS", "OUTPUT_TOKENS", "TOTAL_TOKENS", "DURATION_MS", "PROMPT_VERSION"}


def service():
    return AIMaintenanceService(DATABASE_PATH)


def number(value):
    return "—" if value is None else f"{value:,}"


def message(text, *, error=False):
    return html.Div(text, className="aim-feedback aim-error" if error else "aim-feedback",
                    role="alert" if error else "status")


def button(label, identity, *, primary=False, **kwargs):
    return html.Button(label, id=identity, type="button", className="aim-button" + (" aim-primary" if primary else ""), **kwargs)


def field(label, control):
    return html.Div([html.Label(label, htmlFor=control.id), control], className="aim-field")


def dropdown(identity, label="All", **kwargs):
    return dcc.Dropdown(id=identity, options=[{"label": label, "value": ALL}], value=ALL,
                        clearable=False, className="aim-select", **kwargs)


def raw_block(value):
    if value is None:
        text = "Not recorded"
    elif isinstance(value, str):
        try:
            text = json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return html.Pre(text, className="aim-raw")


def details(label, value, *, opened=False):
    return html.Details([html.Summary(label), raw_block(value)], open=opened, className="aim-details")


def metadata_list(items):
    return html.Dl([html.Div([html.Dt(label), html.Dd(value if value is not None else "—")])
                    for label, value in items], className="aim-record-meta")


def column_defs(kind, chosen):
    fields = CHAT_FIELDS if kind == "chats" else REQUEST_FIELDS
    identity = "ID_MESSAGE" if kind == "chats" else "LOG_ID"
    selected = set(chosen or []) | {identity}
    definitions = []
    for name, label in fields:
        definition = {"field": name, "headerName": label, "hide": name not in selected,
                      "minWidth": 105, "tooltipField": name}
        if name == identity:
            definition.update(pinned="left", maxWidth=105, cellClass="aim-grid-id")
        if name in ("ID_ROUTINE", "FORM_TYPE"):
            definition.update(minWidth=160, tooltipField="ROUTINE_NAME",
                              valueFormatter={"function": "params.data ? (params.data.ROUTINE_NAME || params.value) : params.value"})
        if name == "LOCAL_TIME":
            definition["minWidth"] = 195
        if name == "PREVIEW":
            definition.update(minWidth=230, sortable=False, wrapText=True, autoHeight=True)
        if name in ("ID_ACTIVITY", "HISTORICAL_RECORDS", "REQUESTS"):
            definition["sortable"] = False
        if name in NUMERIC:
            definition.update(type="numericColumn", valueFormatter={"function": "params.value == null ? '—' : params.value.toLocaleString('en-US')"})
        if name == "RECORD_TYPE":
            definition.update(minWidth=185, valueFormatter={"function": "params.value === 'LEGACY_AGGREGATE' ? 'Historical aggregate' : 'Individual request'"})
        if name in ("MODEL", "PROMPT_VERSION", "PROMPT_ID"):
            definition.setdefault("valueFormatter", {"function": "params.value == null ? 'Unknown' : params.value"})
        definitions.append(definition)
    return definitions


def explorer_grid(kind, columns):
    identity = "ID_MESSAGE" if kind == "chats" else "LOG_ID"
    return dag.AgGrid(
        id=f"aim-{kind}-grid", className="ag-theme-alpine aim-grid", rowModelType="infinite",
        columnDefs=column_defs(kind, columns), defaultColDef={"sortable": True, "resizable": True, "filter": False},
        getRowId=f"String(params.data.{identity})",
        dashGridOptions={"pagination": True, "paginationPageSize": 25, "paginationPageSizeSelector": False,
                         "cacheBlockSize": 25, "maxBlocksInCache": 4, "rowSelection": {"mode": "singleRow", "checkboxes": False},
                         "rowHeight": 46, "tooltipShowDelay": 250, "overlayNoRowsTemplate": "No records match these filters."},
        style={"height": "405px"},
    )


def layout():
    today = service().now().date()
    return html.Main(className="page-container aim-page", children=[
        dcc.Store(id="aim-refresh-token", data=0), dcc.Store(id="aim-prompt-refresh", data=0),
        dcc.Store(id="aim-query"), dcc.Store(id="aim-selected-chat"), dcc.Store(id="aim-selected-request"),
        dcc.Store(id="aim-link-chat"), dcc.Store(id="aim-backstack", data=[]), dcc.Store(id="aim-active-prompt"),
        dcc.Store(id="aim-reviewed"), dcc.Store(id="aim-history-target"), dcc.Download(id="aim-download"),
        html.Div([html.H1("AI Maintenance"), html.P([
            "Activate a master profile in ", dcc.Link("UGM Access", href="/keys"),
            " to manage prompts and inspect shared AI activity.",
        ])], id="aim-access", className="aim-panel aim-access"),
        html.Div(id="aim-shell", style={"display": "none"}, children=[
            html.Header(className="aim-header", children=[
                html.Div([html.Div("UGM / ADMINISTRATION", className="aim-eyebrow"), html.H1("AI Maintenance"),
                          html.P("Prompts, token consumption & investigation")]),
                html.Div([html.Span("São Paulo time", className="aim-timezone"), button("Refresh", "aim-refresh")], className="aim-actions"),
            ]),
            html.Div(id="aim-feedback", role="status"),
            html.Div([button("← Back to investigation", "aim-back", disabled=True), html.Span(id="aim-refreshed")], className="aim-backbar"),
            dcc.Tabs(id="aim-tab", value="usage", className="aim-tabs", children=[
                dcc.Tab(label=label, value=value, className="aim-tab", selected_className="aim-tab-selected")
                for value, label in (("usage", "Usage"), ("prompts", "Prompts"), ("chats", "Routine chats"), ("requests", "AI request log"))
            ]),
            html.Section(id="aim-usage-panel", children=[
                html.Div(className="aim-usage-filters", children=[
                    field("Reference date", dcc.DatePickerSingle(id="aim-usage-date", date=today.isoformat(), max_date_allowed=today,
                                                                 display_format="DD MMM YYYY", className="aim-date")),
                    field("User", dropdown("aim-usage-user", "All users")),
                    field("Routine / feature", dropdown("aim-usage-workload", "All routines & features")),
                ]),
                html.Div([dcc.RadioItems(id="aim-period", value="MTD", options=[{"label": "Daily · DLY", "value": "DLY"},
                    {"label": "Month to date · MTD", "value": "MTD"}], inline=True, className="aim-period"),
                    button("Inspect matching requests →", "aim-usage-inspect")], className="aim-sectionbar"),
                dcc.Loading(html.Div(id="aim-usage-content"), type="circle", color="#b30e20"),
            ]),
            html.Section(id="aim-prompts-panel", style={"display": "none"}, children=[
                html.Div(className="aim-prompt-layout", children=[
                    html.Aside(className="aim-panel aim-catalog", children=[
                        html.H2("Prompt catalog"), field("Routine / feature prompt", dcc.Dropdown(id="aim-prompt", options=[], clearable=False,
                            placeholder="Choose a prompt", className="aim-select")),
                        html.P("One active version per feature and prompt key. Model configuration is stored with each version.", className="aim-note"),
                    ]),
                    html.Div(className="aim-panel aim-editor", children=[
                        html.Div(id="aim-prompt-heading"),
                        html.Div(className="aim-two-columns", children=[
                            field("Model", dcc.Input(id="aim-model", type="text", value="", className="aim-input", debounce=False)),
                            field("Version history", dcc.Dropdown(id="aim-history", options=[], clearable=False, className="aim-select")),
                        ]),
                        field("Instructions", dcc.Textarea(id="aim-instructions", value="", className="aim-textarea")),
                        field("Version comment", dcc.Textarea(id="aim-comment", value="", className="aim-comment")),
                        html.Div([html.Span(id="aim-dirty"), html.Div([button("Discard changes", "aim-discard"),
                                  button("Review & activate", "aim-review", primary=True)], className="aim-actions")], className="aim-sectionbar"),
                        html.P("Activation creates a new version for future calls. Previous versions remain available.", className="aim-note"),
                        html.Div(id="aim-review-content"), button("Activate reviewed version", "aim-activate", primary=True, disabled=True),
                        html.Div(id="aim-save-status", role="status"),
                        html.H3("Selected historical version", className="aim-history-heading"),
                        html.Div(id="aim-history-content"), button("Use selected version as a new draft", "aim-restore", disabled=True),
                    ]),
                ]),
            ]),
            html.Section(id="aim-browse-panel", style={"display": "none"}, children=[
                html.Div(className="aim-sectionbar", children=[html.P(id="aim-date-basis", className="aim-note"),
                    html.Div([button("Today", "aim-today"), button("MTD", "aim-mtd"), button("Reset filters", "aim-reset")], className="aim-actions")]),
                html.Div(className="aim-browse-filters", children=[
                    field("From", dcc.DatePickerSingle(id="aim-from", date=today.replace(day=1).isoformat(), display_format="DD MMM YYYY", className="aim-date")),
                    field("Through", dcc.DatePickerSingle(id="aim-through", date=today.isoformat(), display_format="DD MMM YYYY", className="aim-date")),
                    field("User", dropdown("aim-browse-user", "All users")),
                    field("Routine / feature", dropdown("aim-browse-workload", "All routines & features")),
                ]),
                html.Div(id="aim-linked-context", className="aim-linked-context", children=[
                    html.Span(id="aim-link-label"), button("Remove chat filter", "aim-unlink")], style={"display": "none"}),
                html.Div(id="aim-chats-tools", children=[
                    html.Div(className="aim-queryrow", children=[
                        field("Search all recorded fields", dcc.Input(id="aim-chat-query", type="search", value="", debounce=0.35,
                              placeholder="Message text, chat ID, execution ID…", className="aim-input")),
                        field("Status", dropdown("aim-chat-status", "All statuses")),
                    ]),
                    html.Details([html.Summary("Visible columns"), dcc.Checklist(id="aim-chat-columns", value=DEFAULT_CHAT_FIELDS,
                        options=[{"label": label, "value": name, "disabled": name == "ID_MESSAGE"} for name, label in CHAT_FIELDS], className="aim-columns")]),
                ]),
                html.Div(id="aim-requests-tools", style={"display": "none"}, children=[
                    html.Div(className="aim-queryrow", children=[
                        field("Search all recorded fields", dcc.Input(id="aim-request-query", type="search", value="", debounce=0.35,
                              placeholder="Request ID, prompt, model output, error…", className="aim-input")),
                        field("Status", dropdown("aim-request-status", "All statuses")),
                    ]),
                    html.Details([html.Summary("More filters · model, prompt version, record type, usage"),
                        html.Div(className="aim-browse-filters", children=[
                            field("Model", dropdown("aim-browse-model", "All models")),
                            field("Prompt version", dropdown("aim-browse-version", "All versions")),
                            field("Record type", dcc.Dropdown(id="aim-record-type", clearable=False, value=ALL, className="aim-select", options=[
                                {"label": "All records", "value": ALL}, {"label": "Individual request", "value": "REQUEST"},
                                {"label": "Historical aggregate", "value": "LEGACY_AGGREGATE"}])),
                            field("Token usage", dcc.Dropdown(id="aim-usage-state", clearable=False, value=ALL, className="aim-select", options=[
                                {"label": "All usage states", "value": ALL}, {"label": "Reported, including zero", "value": "reported"},
                                {"label": "Unreported", "value": "missing"}])),
                        ]),
                    ], className="aim-advanced"),
                    html.Details([html.Summary("Visible columns"), dcc.Checklist(id="aim-request-columns", value=DEFAULT_REQUEST_FIELDS,
                        options=[{"label": label, "value": name, "disabled": name == "LOG_ID"} for name, label in REQUEST_FIELDS], className="aim-columns")]),
                ]),
                html.Div([html.Div(id="aim-result-count"), button("Export all filtered records · CSV", "aim-export")], className="aim-sectionbar"),
                html.Div(id="aim-export-status", role="status"),
                html.Div(id="aim-nav-status", role="status"),
                html.Div(id="aim-chats-error", role="status"), html.Div(id="aim-requests-error", role="status"),
                html.Div(id="aim-chats-results", children=explorer_grid("chats", DEFAULT_CHAT_FIELDS)),
                html.Div(id="aim-requests-results", children=explorer_grid("requests", DEFAULT_REQUEST_FIELDS), style={"display": "none"}),
                html.P("Select a row to inspect it. Sorting and paging query the full filtered table.", className="aim-note"),
                html.Section(id="aim-chat-inspector", className="aim-panel aim-inspector", children=[
                    html.Div([html.H2("Chat inspection"), html.Div([button("Open day conversation", "aim-thread"),
                              button("View linked records →", "aim-chat-requests", primary=True, disabled=True)], className="aim-actions")], className="aim-sectionbar"),
                    html.Div(id="aim-chat-detail", children=html.P("Select a chat interaction.", className="aim-note")),
                ]),
                html.Section(id="aim-request-inspector", className="aim-panel aim-inspector", style={"display": "none"}, children=[
                    html.Div([html.H2("Request inspection"), html.Div([button("Open recorded prompt version", "aim-log-prompt", disabled=True),
                        button("View originating chat →", "aim-request-chat", primary=True, disabled=True)], className="aim-actions")], className="aim-sectionbar"),
                    html.Div(id="aim-request-detail", children=html.P("Select a request record.", className="aim-note")),
                ]),
            ]),
        ]),
    ])


@callback(
    Output("aim-shell", "style"), Output("aim-access", "style"), Output("aim-feedback", "children"),
    Output("aim-usage-user", "options"), Output("aim-browse-user", "options"),
    Output("aim-usage-workload", "options"), Output("aim-browse-workload", "options"),
    Output("aim-chat-status", "options"), Output("aim-request-status", "options"),
    Output("aim-browse-model", "options"), Output("aim-browse-version", "options"),
    Output("aim-prompt", "options"), Output("aim-prompt", "value"), Output("aim-refreshed", "children"),
    Input("current-user", "data"), Input("aim-refresh", "n_clicks"), Input("aim-prompt-refresh", "data"),
    State("aim-prompt", "value"),
)
def load_metadata(user, _refresh, _prompt_refresh, selected):
    empty = ({"display": "none"}, {}, [], [], [], [], [], [], [], [], [], [], None, "")
    engine = service()
    if not engine.authorized(user):
        return empty
    try:
        data = engine.metadata()
        all_options = lambda label, values: [{"label": label, "value": ALL}, *values]
        choices = [{"label": f"{p['FEATURE']} / {p['PROMPT_KEY']} · v{p['VERSION']}", "value": p["PROMPT_ID"]} for p in data["prompts"]]
        active_ids = [option["value"] for option in choices]
        selected_value = no_update
        if selected not in active_ids:
            previous = engine.prompt(selected) if selected else None
            match = next((p for p in data["prompts"] if previous and p["FEATURE"] == previous["FEATURE"]
                          and p["PROMPT_KEY"] == previous["PROMPT_KEY"]), None)
            selected_value = match["PROMPT_ID"] if match else active_ids[0] if active_ids else None
        return ({}, {"display": "none"}, [], all_options("All users", data["users"]), all_options("All users", data["users"]),
                all_options("All routines & features", data["workloads"]), all_options("All routines & features", data["workloads"]),
                all_options("All statuses", [{"label": s, "value": s} for s in data["statuses"]["chats"]]),
                all_options("All statuses", [{"label": s, "value": s} for s in data["statuses"]["requests"]]),
                all_options("All models", data["models"]), all_options("All versions", data["versions"]), choices, selected_value,
                "Updated " + engine.now().strftime("%d %b %Y · %H:%M:%S") + " · São Paulo")
    except Exception:
        logger.exception("Could not load AI Maintenance metadata")
        return ({}, {"display": "none"}, message("Unable to load AI Maintenance. Try Refresh.", error=True), *empty[3:])


@callback(
    Output("aim-usage-panel", "style"), Output("aim-prompts-panel", "style"), Output("aim-browse-panel", "style"),
    Output("aim-chats-tools", "style"), Output("aim-requests-tools", "style"),
    Output("aim-chats-results", "style"), Output("aim-requests-results", "style"),
    Output("aim-chat-inspector", "style"), Output("aim-request-inspector", "style"), Output("aim-date-basis", "children"),
    Input("aim-tab", "value"),
)
def switch_tab(tab):
    visible = lambda active: {} if active else {"display": "none"}
    return (visible(tab == "usage"), visible(tab == "prompts"), visible(tab in ("chats", "requests")),
            visible(tab == "chats"), visible(tab == "requests"), visible(tab == "chats"), visible(tab == "requests"),
            visible(tab == "chats"), visible(tab == "requests"),
            "Dates filter the chat operational date · São Paulo" if tab == "chats" else "Dates filter request start times · São Paulo")


def usage_table(identity, rows, columns):
    return dag.AgGrid(id=identity, rowData=rows, columnDefs=columns, className="ag-theme-alpine aim-grid aim-usage-grid",
                     defaultColDef={"resizable": True, "sortable": True, "flex": 1, "minWidth": 40,
                                    "wrapText": True, "autoHeight": True, "wrapHeaderText": True, "autoHeaderHeight": True},
                     dashGridOptions={"domLayout": "autoHeight", "suppressHorizontalScroll": False}, style={"width": "100%"})


@callback(Output("aim-usage-content", "children"), Input("current-user", "data"), Input("aim-usage-date", "date"),
          Input("aim-usage-user", "value"), Input("aim-usage-workload", "value"), Input("aim-period", "value"),
          Input("aim-refresh", "n_clicks"))
def load_usage(user, as_of, username, workload, period, _refresh):
    engine = service()
    if not engine.authorized(user):
        return []
    try:
        snapshot = engine.usage(as_of, user=username, workload=workload, period=period)
        metrics = []
        for key, label, detail in (("daily", "Daily · DLY", as_of), ("monthly", "Month to date · MTD", as_of[:7] + "-01 through " + as_of)):
            total = snapshot[key]
            metrics.append(html.Div([html.Span(label, className="aim-metric-label"), html.Strong(number(total["TOTAL_TOKENS"])),
                html.Small(f"reported tokens · {number(total['REQUESTS'])} model calls"), html.Small(detail)],
                className="aim-metric" + (" aim-metric-selected" if (key == "daily") == (period == "DLY") else "")))
        chosen = snapshot["selected"]
        metrics.append(html.Div([html.Span("Token mix · " + period, className="aim-metric-label"),
            html.Div([html.Div([html.Small("Input"), html.Strong(number(chosen["INPUT_TOKENS"]))]),
                      html.Div([html.Small("Output"), html.Strong(number(chosen["OUTPUT_TOKENS"]))])], className="aim-mix")], className="aim-metric"))
        trend = snapshot["trend"]
        figure = go.Figure()
        for name, field_name, color in (("Input", "INPUT_TOKENS", "#75889d"), ("Output", "OUTPUT_TOKENS", "#be4354")):
            figure.add_bar(name=name, x=[r["BUCKET"] for r in trend], y=[r[field_name] for r in trend], marker_color=color,
                           hovertemplate="%{x}<br>" + name + ": %{y:,} tokens<extra></extra>")
        figure.update_layout(barmode="stack", height=250, margin=dict(l=55, r=20, t=25, b=50),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(family="Arial", size=12, color="#374151"),
            yaxis_title="Reported tokens", xaxis_title="Hour · São Paulo" if period == "DLY" else "Date · São Paulo",
            legend=dict(orientation="h", x=1, xanchor="right", y=1.15), yaxis=dict(gridcolor="#e5e7eb"))
        if not trend:
            figure.add_annotation(text="No AI usage in this period", x=.5, y=.5, xref="paper", yref="paper", showarrow=False)
        numeric_columns = [{"headerName": label, "field": name, "type": "numericColumn", "valueFormatter":
                            {"function": "params.value == null ? '—' : params.value.toLocaleString('en-US')"}}
                           for name, label in (("REQUESTS", "Calls"), ("INPUT_TOKENS", "Input"), ("OUTPUT_TOKENS", "Output"), ("TOTAL_TOKENS", "Total"))]
        user_column = {"field": "USERNAME", "headerName": "User", "flex": 1.6, "tooltipField": "USERNAME",
                       "valueFormatter": {"function": "params.value || 'Unattributed'"}}
        routine_column = {"field": "ROUTINE_NAME", "headerName": "Routine / feature", "flex": 1.8, "tooltipField": "ROUTINE_NAME"}
        interaction_column = {"field": "INTERACTIONS", "headerName": "Interactions", "type": "numericColumn", "valueFormatter":
                              {"function": "params.value == null ? '—' : params.value.toLocaleString('en-US')"}}
        return [html.Div(metrics, className="aim-metrics"),
            html.Section([html.H2("Token consumption · " + period), dcc.Graph(figure=figure, config={"displayModeBar": False, "responsive": True})], className="aim-panel aim-chart-panel"),
            html.Div(className="aim-breakdowns", children=[
                html.Section([html.H2("By user"), html.P("Select a row to filter usage.", className="aim-note"), usage_table("aim-usage-users-grid", snapshot["users"],
                    [user_column, *numeric_columns])], className="aim-panel"),
                html.Section([html.H2("By routine / feature"), html.P("Select a row to filter usage.", className="aim-note"), usage_table("aim-usage-workloads-grid", snapshot["workloads"],
                    [routine_column, *numeric_columns])], className="aim-panel"),
                html.Section([html.H2("By model"), html.P("Reported model for each request; historical model provenance stays unknown.", className="aim-note"),
                    usage_table("aim-usage-models-grid", snapshot["models"], [
                        {"field": "MODEL", "headerName": "Model", "flex": 1.8, "tooltipField": "MODEL", "valueFormatter":
                         {"function": "params.value == null ? 'Unknown model' : params.value"}}, *numeric_columns])],
                    className="aim-panel aim-breakdown-wide"),
                html.Section([html.H2("By user × routine / feature"), html.P("Select a row to filter by both user and routine.", className="aim-note"),
                    usage_table("aim-usage-user-routines-grid", snapshot["user_routines"], [
                        user_column, routine_column, *numeric_columns[1:], interaction_column]),
                    html.P("Interactions count routine chat messages by operational date, including chats with no AI call. "
                           "Tokens use request start times. Interactions are available for UGM routines only.", className="aim-note")],
                    className="aim-panel aim-breakdown-wide"),
            ]),
            html.P(f"{chosen['MISSING_USAGE']} calls without reported usage · {chosen['HISTORICAL_RECORDS']} historical aggregates. "
                   "Unreported usage is not zero. Historical totals contribute tokens, not individual call counts.", className="aim-note"),
        ]
    except (ValueError, TypeError):
        return message("Choose a valid reference date and filters.", error=True)
    except Exception:
        logger.exception("Could not load AI usage")
        return message("Unable to load AI usage. Try Refresh.", error=True)


@callback(Output("aim-usage-user", "value"), Output("aim-usage-workload", "value"),
          Input("aim-usage-users-grid", "cellClicked"), Input("aim-usage-workloads-grid", "cellClicked"),
          Input("aim-usage-user-routines-grid", "cellClicked"), prevent_initial_call=True)
def usage_drilldown(user_click, workload_click, user_routine_click):
    if ctx.triggered_id == "aim-usage-users-grid" and user_click and user_click.get("data"):
        return user_click["data"].get("USERNAME") or UNKNOWN, no_update
    if ctx.triggered_id == "aim-usage-workloads-grid" and workload_click and workload_click.get("data"):
        return no_update, workload_click["data"]["WORKLOAD"]
    if ctx.triggered_id == "aim-usage-user-routines-grid" and user_routine_click and user_routine_click.get("data"):
        row = user_routine_click["data"]
        return row.get("USERNAME") or UNKNOWN, row["WORKLOAD"]
    raise PreventUpdate


FILTER_PROPS = [
    ("start", "aim-from", "date"), ("end", "aim-through", "date"),
    ("user", "aim-browse-user", "value"), ("workload", "aim-browse-workload", "value"),
    ("chat_status", "aim-chat-status", "value"), ("request_status", "aim-request-status", "value"),
    ("chat_query", "aim-chat-query", "value"), ("request_query", "aim-request-query", "value"),
    ("model", "aim-browse-model", "value"), ("version", "aim-browse-version", "value"),
    ("record_type", "aim-record-type", "value"), ("usage", "aim-usage-state", "value"),
    ("chat_id", "aim-link-chat", "data"),
]


@callback(Output("aim-query", "data"), Output("aim-chats-results", "children"),
          Output("aim-requests-results", "children"), Output("aim-result-count", "children"),
          Input("aim-tab", "value"), Input("current-user", "data"), Input("aim-refresh", "n_clicks"),
          Input("aim-chat-columns", "value"), Input("aim-request-columns", "value"),
          *[Input(identity, prop) for _, identity, prop in FILTER_PROPS])
def prepare_explorer(tab, user, _refresh, chat_columns, request_columns, *values):
    engine = service()
    if not engine.authorized(user):
        return None, explorer_grid("chats", chat_columns), explorer_grid("requests", request_columns), ""
    if tab not in ("chats", "requests"):
        return no_update, no_update, no_update, ""
    filters = dict(zip((key for key, _, _ in FILTER_PROPS), values))
    try:
        stats = engine.summary(tab, filters)
        summary = (f"{number(stats['records'])} chat interactions · grouped by operational date, routine, activity and user" if tab == "chats" else
                   f"{number(stats['requests'])} model calls · {number(stats['historical'])} historical aggregates · "
                   f"{number(stats['total'])} reported tokens · {number(stats['missing'])} records without reported usage")
        columns = chat_columns if tab == "chats" else request_columns
        cache_key = hashlib.sha256(json.dumps([tab, filters, columns, _refresh], sort_keys=True).encode()).hexdigest()
        # A new React key discards the infinite-row cache when its query changes.
        grid = html.Div(explorer_grid(tab, columns), key=cache_key)
        return filters, grid if tab == "chats" else no_update, grid if tab == "requests" else no_update, summary
    except ValueError as exc:
        return None, explorer_grid("chats", chat_columns), explorer_grid("requests", request_columns), message(str(exc), error=True)
    except Exception:
        logger.exception("Could not prepare AI log explorer")
        return None, explorer_grid("chats", chat_columns), explorer_grid("requests", request_columns), message("Unable to query the selected log. Try Refresh.", error=True)


def fetch_rows(kind, request, filters, user, tab):
    engine = service()
    if not engine.authorized(user) or tab != kind or not filters:
        return {"rowData": [], "rowCount": 0}, []
    if not request:
        raise PreventUpdate
    try:
        start = int(request.get("startRow", 0))
        end = int(request.get("endRow", start + 25))
        result = engine.browse(kind, filters, start=start, limit=end - start, sort_model=request.get("sortModel"))
        return {"rowData": result["rows"], "rowCount": result["count"]}, []
    except Exception:
        logger.exception("Could not fetch an AI log block")
        return {"rowData": [], "rowCount": 0}, message("Unable to load records. Try Refresh.", error=True)


@callback(Output("aim-chats-grid", "getRowsResponse"), Output("aim-chats-error", "children"),
          Input("aim-chats-grid", "getRowsRequest"), State("aim-query", "data"),
          State("current-user", "data"), State("aim-tab", "value"))
def fetch_chats(request, filters, user, tab):
    return fetch_rows("chats", request, filters, user, tab)


@callback(Output("aim-requests-grid", "getRowsResponse"), Output("aim-requests-error", "children"),
          Input("aim-requests-grid", "getRowsRequest"), State("aim-query", "data"),
          State("current-user", "data"), State("aim-tab", "value"))
def fetch_requests(request, filters, user, tab):
    return fetch_rows("requests", request, filters, user, tab)


@callback(Output("aim-chat-detail", "children"), Output("aim-chat-requests", "disabled"),
          Input("aim-selected-chat", "data"), Input("aim-query", "data"), Input("current-user", "data"),
          Input("aim-thread", "n_clicks"))
def inspect_chat(chat_id, filters, user, _thread):
    engine = service()
    empty = (html.P("Select a chat interaction.", className="aim-note"), True)
    if not engine.authorized(user) or not filters or chat_id is None:
        return empty
    try:
        row = engine.matching_record("chats", chat_id, filters)
        if not row:
            return empty
        context = engine.linked_context(chat_id)
        linked = context["records"]
        rows = engine.conversation(chat_id) if ctx.triggered_id == "aim-thread" else [row]
        content = [html.H3(f"Interaction #{row['ID_MESSAGE']}"), metadata_list([
            ("Routine / user", row["ID_ROUTINE"] + " · " + row["USERNAME"]), ("Operational date", row["DT_REF"]),
            ("Activity", row.get("ID_ACTIVITY")), ("Message · São Paulo", local_stamp(row["TS_MESSAGE"], naive_tz=LOCAL_TZ)),
            ("Response · São Paulo", local_stamp(row["TS_RESPONSE"], naive_tz=LOCAL_TZ)), ("Status", row["STATUS"]),
            ("Execution ID", row["ID_EXECUTION"]), ("Individual model calls", sum(r["RECORD_TYPE"] == "REQUEST" for r in linked)),
            ("Historical aggregates", sum(r["RECORD_TYPE"] == "LEGACY_AGGREGATE" for r in linked)),
        ])]
        for item in rows:
            content.append(html.Div([html.H3(f"Chat #{item['ID_MESSAGE']} · {local_stamp(item['TS_MESSAGE'], naive_tz=LOCAL_TZ)}"),
                html.Div([html.Div([html.H4("User message"), html.Pre(item["USER_MESSAGE"], className="aim-message-text")], className="aim-message"),
                          html.Div([html.H4("Assistant message"), html.Pre(item["ASSISTANT_MESSAGE"] or "No assistant response recorded.", className="aim-message-text")],
                                   className="aim-message")], className="aim-exchange")], className="aim-thread-item"))
        content.append(details("Raw chat row", row))
        return content, not linked
    except Exception:
        logger.exception("Could not inspect a routine chat")
        return message("Unable to inspect this chat. Try selecting it again.", error=True), True


@callback(Output("aim-request-detail", "children"), Output("aim-request-chat", "disabled"), Output("aim-log-prompt", "disabled"),
          Input("aim-selected-request", "data"), Input("aim-query", "data"), Input("current-user", "data"))
def inspect_request(log_id, filters, user):
    engine = service()
    empty = (html.P("Select a request record.", className="aim-note"), True, True)
    if not engine.authorized(user) or not filters or log_id is None:
        return empty
    try:
        row = engine.matching_record("requests", log_id, filters)
        if not row:
            return empty
        historical = row["RECORD_TYPE"] == "LEGACY_AGGREGATE"
        content = [html.H3(f"Log #{row['LOG_ID']}"), metadata_list([
            ("Record type", "Historical aggregate · not a model call" if historical else "Individual model call"),
            ("Status", row["STATUS"]), ("User", row["USERNAME"]), ("Feature / routine or form", row["FEATURE"] + " / " + row["FORM_TYPE"]),
            ("Started · São Paulo", local_stamp(row["DT_STARTED"])), ("Started · original UTC / offset", row["DT_STARTED"]),
            ("Finished · São Paulo", local_stamp(row["DT_FINISHED"])), ("Model", row["MODEL"] or "Unknown"),
            ("Prompt provenance", f"Prompt #{row['PROMPT_ID']} · v{row['PROMPT_VERSION']}" if row["PROMPT_ID"] is not None else "Unknown"),
            ("Chat link", f"#{row['CHAT_MESSAGE_ID']}" if row["CHAT_MESSAGE_ID"] is not None else "No routine chat link"),
            ("Input tokens", number(row["INPUT_TOKENS"])), ("Output tokens", number(row["OUTPUT_TOKENS"])), ("Total tokens", number(row["TOTAL_TOKENS"])),
        ])]
        if row["ERROR_TYPE"] or row["ERROR_MESSAGE"]:
            content.append(message((row["ERROR_TYPE"] or "Recorded error") + ": " + (row["ERROR_MESSAGE"] or "No error message recorded."), error=True))
        if historical:
            content.append(html.P("This historical total may combine several old calls. Unknown model and prompt provenance have not been inferred.", className="aim-note"))
        content.extend([details("Submitted text · PROMPT_TEXT", row["PROMPT_TEXT"]),
            details("Original request payload · FULL_PROMPT", row.get("FULL_PROMPT")), details("Model output · MODEL_OUTPUT", row["MODEL_OUTPUT"]),
            details("Raw AI request log row", row)])
        chat = engine.record("chats", row["CHAT_MESSAGE_ID"]) if row["CHAT_MESSAGE_ID"] is not None else None
        prompt = engine.prompt(row["PROMPT_ID"]) if row["PROMPT_ID"] is not None else None
        return content, chat is None, prompt is None
    except Exception:
        logger.exception("Could not inspect an AI request")
        return message("Unable to inspect this request. Try selecting it again.", error=True), True, True


@callback(Output("aim-linked-context", "style"), Output("aim-link-label", "children"), Input("aim-link-chat", "data"))
def show_chat_link(chat_id):
    return ({}, f"Records linked to chat #{chat_id}") if chat_id is not None else ({"display": "none"}, "")


NAV_PROPS = [("tab", "aim-tab", "value"), *FILTER_PROPS, ("selected_chat", "aim-selected-chat", "data"),
             ("selected_request", "aim-selected-request", "data"), ("prompt", "aim-prompt", "value"),
             ("history_target", "aim-history-target", "data"), ("backstack", "aim-backstack", "data")]
NAV_BUTTONS = ["aim-usage-inspect", "aim-chat-requests", "aim-request-chat", "aim-log-prompt", "aim-back", "aim-unlink", "aim-today", "aim-mtd", "aim-reset"]


def navigation_changes(engine, trigger, state, usage_scope, *, chat_click=None, request_click=None):
    """Keep linking and back-navigation independent of component rendering."""
    result = dict(state)
    stack = list(state.get("backstack") or [])
    def remember():
        stack.append({key: value for key, value in state.items() if key != "backstack"})
        del stack[:-8]
    def clear_requests():
        result.update(request_status=ALL, request_query="", model=ALL, version=ALL, record_type=ALL, usage=ALL, selected_request=None)
    if trigger == "aim-chats-grid" and chat_click and chat_click.get("data"):
        result["selected_chat"] = chat_click["data"]["ID_MESSAGE"]
    elif trigger == "aim-requests-grid" and request_click and request_click.get("data"):
        result["selected_request"] = request_click["data"]["LOG_ID"]
    elif trigger == "aim-back":
        if stack:
            result.update(stack.pop())
    elif trigger == "aim-usage-inspect":
        remember()
        as_of, user, workload, period = usage_scope
        date.fromisoformat(as_of)
        clear_requests()
        result.update(tab="requests", start=as_of if period == "DLY" else as_of[:7] + "-01", end=as_of,
                      user=user, workload=workload, chat_id=None)
    elif trigger == "aim-chat-requests" and state.get("selected_chat") is not None:
        context = engine.linked_context(state["selected_chat"])
        row = context["chat"]
        remember()
        clear_requests()
        result.update(tab="requests", start=context["start"], end=context["end"], user=row["USERNAME"] or UNKNOWN,
                      workload=scope_value("routines", row["ID_ROUTINE"]), chat_id=row["ID_MESSAGE"])
    elif trigger in ("aim-request-chat", "aim-log-prompt") and state.get("selected_request") is not None:
        row = engine.record("requests", state["selected_request"])
        if not row:
            raise ValueError("The selected request is no longer available.")
        if trigger == "aim-request-chat":
            chat = engine.record("chats", row["CHAT_MESSAGE_ID"]) if row["CHAT_MESSAGE_ID"] is not None else None
            if not chat:
                raise ValueError("This record has no originating routine chat.")
            remember()
            result.update(tab="chats", start=chat["DT_REF"], end=chat["DT_REF"], user=chat["USERNAME"] or UNKNOWN,
                workload=scope_value("routines", chat["ID_ROUTINE"]), chat_id=None, chat_status=ALL, chat_query="", selected_chat=chat["ID_MESSAGE"])
        else:
            history = engine.prompt_history(row["PROMPT_ID"]) if row["PROMPT_ID"] is not None else []
            if not history:
                raise ValueError("The recorded prompt version is no longer available.")
            remember()
            active = next((p for p in history if p["FL_ACTIVE"]), history[0])
            result.update(tab="prompts", prompt=active["PROMPT_ID"], history_target=row["PROMPT_ID"])
    elif trigger == "aim-unlink":
        result.update(chat_id=None, selected_request=None)
    elif trigger in ("aim-today", "aim-mtd", "aim-reset"):
        today = engine.now().date()
        result.update(start=today.isoformat() if trigger == "aim-today" else today.replace(day=1).isoformat(),
                      end=today.isoformat(), chat_id=None, selected_chat=None, selected_request=None)
        if trigger == "aim-reset":
            clear_requests()
            result.update(user=ALL, workload=ALL, chat_status=ALL, chat_query="")
    result["backstack"] = stack
    return result


@callback(*[Output(identity, prop, allow_duplicate=(key == "prompt")) for key, identity, prop in NAV_PROPS],
          Output("aim-nav-status", "children"), *[Input(identity, "n_clicks") for identity in NAV_BUTTONS],
          Input("aim-chats-grid", "cellClicked"), Input("aim-requests-grid", "cellClicked"),
          *[State(identity, prop) for _, identity, prop in NAV_PROPS],
          State("aim-usage-date", "date"), State("aim-usage-user", "value"), State("aim-usage-workload", "value"),
          State("aim-period", "value"), State("current-user", "data"), prevent_initial_call=True)
def navigate(*args):
    offset = len(NAV_BUTTONS)
    chat_click, request_click = args[offset:offset + 2]
    values = args[offset + 2:offset + 2 + len(NAV_PROPS)]
    usage_scope, user = args[-5:-1], args[-1]
    engine = service()
    if not engine.authorized(user):
        raise PreventUpdate
    state = dict(zip((key for key, _, _ in NAV_PROPS), values))
    try:
        result = navigation_changes(engine, ctx.triggered_id, state, usage_scope, chat_click=chat_click, request_click=request_click)
        return (*[result[key] if result.get(key) != state.get(key) else no_update for key, _, _ in NAV_PROPS], [])
    except ValueError as exc:
        return (*[no_update] * len(NAV_PROPS), message(str(exc), error=True))
    except Exception:
        logger.exception("Could not navigate an AI investigation")
        return (*[no_update] * len(NAV_PROPS), message("Unable to open the linked record.", error=True))


@callback(Output("aim-back", "disabled"), Input("aim-backstack", "data"))
def back_available(stack):
    return not stack


@callback(Output("aim-download", "data"), Output("aim-export-status", "children"), Input("aim-export", "n_clicks"),
          State("aim-tab", "value"), State("aim-query", "data"), State("current-user", "data"),
          State("aim-chats-grid", "getRowsRequest"), State("aim-requests-grid", "getRowsRequest"), prevent_initial_call=True)
def export_records(_click, tab, filters, user, chat_request, ai_request):
    engine = service()
    if not engine.authorized(user) or tab not in ("chats", "requests") or not filters:
        return no_update, message("Activate master access and choose valid filters before exporting.", error=True)
    try:
        sort_model = ((chat_request if tab == "chats" else ai_request) or {}).get("sortModel")
        content = engine.export_csv(tab, filters, sort_model)
        return dcc.send_string(content, f"ai-maintenance-{tab}-{filters['start']}-{filters['end']}.csv"), message("Exported all filtered records.")
    except Exception:
        logger.exception("Could not export AI investigation records")
        return no_update, message("Unable to export records. Try again.", error=True)


@callback(Output("aim-active-prompt", "data"), Output("aim-model", "value"), Output("aim-instructions", "value"),
          Output("aim-comment", "value"), Output("aim-history", "options"), Output("aim-history", "value"),
          Output("aim-prompt-heading", "children"), Input("aim-prompt", "value"), Input("aim-discard", "n_clicks"),
          Input("aim-history-target", "data"), Input("current-user", "data"))
def load_prompt(prompt_id, _discard, target, user):
    engine = service()
    empty = (None, "", "", "", [], None, html.P("Choose a prompt from the catalog.", className="aim-note"))
    if not engine.authorized(user) or prompt_id is None:
        return empty
    try:
        history = engine.prompt_history(prompt_id)
        if not history:
            return empty
        active = next((p for p in history if p["FL_ACTIVE"]), history[0])
        options = [{"label": f"v{p['VERSION']} · {'Active' if p['FL_ACTIVE'] else 'Previous'} · {p['DT_CREATED'][:10]}", "value": p["PROMPT_ID"]} for p in history]
        selected = target if target in [p["PROMPT_ID"] for p in history] else active["PROMPT_ID"]
        heading = [html.Div([html.H2(active["PROMPT_KEY"]), html.Span(f"Active · v{active['VERSION']}", className="aim-active-badge")], className="aim-sectionbar"),
                   html.P(active["FEATURE"] + " / " + active["PROMPT_KEY"], className="aim-note"),
                   html.P("Activated " + (local_stamp(active["DT_CREATED"]) or active["DT_CREATED"]) + " · by " +
                          (active.get("CREATED_BY") or "Not recorded"), className="aim-note")]
        return active, active["MODEL"] or "", active["INSTRUCTIONS"], active.get("COMMENT") or "", options, selected, heading
    except Exception:
        logger.exception("Could not load a maintained prompt")
        return (*empty[:6], message("Unable to load this prompt. Try Refresh.", error=True))


@callback(Output("aim-history-content", "children"), Output("aim-restore", "disabled"),
          Input("aim-history", "value"), Input("aim-active-prompt", "data"), Input("current-user", "data"))
def show_prompt_history(prompt_id, active, user):
    engine = service()
    if not engine.authorized(user) or prompt_id is None or not active:
        return [], True
    try:
        selected = engine.prompt(prompt_id)
        if not selected or (selected["FEATURE"], selected["PROMPT_KEY"]) != (active["FEATURE"], active["PROMPT_KEY"]):
            return [], True
        return [metadata_list([("Version", f"v{selected['VERSION']}"), ("Model", selected["MODEL"] or "Unknown"),
                    ("Created · São Paulo", local_stamp(selected["DT_CREATED"])), ("Created by", selected.get("CREATED_BY") or "Not recorded")]),
                details("Historical instructions", selected["INSTRUCTIONS"]), details("Version comment", selected.get("COMMENT"))], False
    except Exception:
        logger.exception("Could not inspect a prompt version")
        return message("Unable to inspect this version.", error=True), True


@callback(Output("aim-model", "value", allow_duplicate=True), Output("aim-instructions", "value", allow_duplicate=True),
          Output("aim-comment", "value", allow_duplicate=True), Input("aim-restore", "n_clicks"),
          State("aim-history", "value"), State("aim-active-prompt", "data"), State("current-user", "data"), prevent_initial_call=True)
def restore_prompt(_click, prompt_id, active, user):
    engine = service()
    if not engine.authorized(user) or not active or prompt_id is None:
        raise PreventUpdate
    row = engine.prompt(prompt_id)
    if not row or (row["FEATURE"], row["PROMPT_KEY"]) != (active["FEATURE"], active["PROMPT_KEY"]):
        raise PreventUpdate
    return row["MODEL"] or "", row["INSTRUCTIONS"], f"Restore version {row['VERSION']} as a new version."


@callback(Output("aim-reviewed", "data"), Output("aim-review-content", "children"), Output("aim-activate", "disabled"),
          Output("aim-dirty", "children"), Input("aim-review", "n_clicks"), Input("aim-model", "value"),
          Input("aim-instructions", "value"), Input("aim-comment", "value"), Input("aim-active-prompt", "data"),
          Input("current-user", "data"))
def review_prompt(_click, model, instructions, comment, active, user):
    if not service().authorized(user) or not active:
        return None, [], True, ""
    changed = (model or "", instructions or "", comment or "") != (active.get("MODEL") or "", active["INSTRUCTIONS"], active.get("COMMENT") or "")
    dirty = "Unsaved changes" if changed else ""
    if ctx.triggered_id != "aim-review":
        return None, [], True, dirty
    if not model or not model.strip() or not instructions or not instructions.strip():
        return None, message("Model and instructions are required.", error=True), True, dirty
    if not changed:
        return None, message("There are no changes to activate."), True, ""
    reviewed = {"prompt_id": active["PROMPT_ID"], "model": model, "instructions": instructions, "comment": comment or ""}
    diff = "\n".join(difflib.unified_diff(active["INSTRUCTIONS"].splitlines(), instructions.splitlines(),
                    fromfile=f"Active v{active['VERSION']}", tofile=f"New v{active['VERSION'] + 1}", lineterm=""))
    panel = html.Div([html.H3(f"Review v{active['VERSION']} → v{active['VERSION'] + 1}"),
                     metadata_list([("Previous model", active["MODEL"] or "Unknown"), ("New model", model.strip()), ("New version comment", comment or "No comment")]),
                     raw_block(diff or "Instructions unchanged; model or comment has changed.")], className="aim-review-panel")
    return reviewed, panel, False, dirty


@callback(Output("aim-prompt-refresh", "data"), Output("aim-save-status", "children"), Input("aim-activate", "n_clicks"),
          State("aim-reviewed", "data"), State("aim-active-prompt", "data"), State("aim-model", "value"),
          State("aim-instructions", "value"), State("aim-comment", "value"), State("current-user", "data"),
          State("aim-prompt-refresh", "data"), prevent_initial_call=True)
def activate_prompt(_click, reviewed, active, model, instructions, comment, user, refresh):
    engine = service()
    if not engine.authorized(user):
        return no_update, message("Active master access is required to activate a prompt.", error=True)
    if not reviewed or not active or reviewed != {"prompt_id": active["PROMPT_ID"], "model": model,
                                                "instructions": instructions, "comment": comment or ""}:
        return no_update, message("Review the current changes before activation.", error=True)
    try:
        saved = engine.activate_prompt(reviewed["prompt_id"], instructions, model, comment or "", user=user)
        return (refresh or 0) + 1, message(f"Activated {saved.feature} / {saved.key} v{saved.version} for future calls.")
    except (PromptConflictError, ValueError, PermissionError) as exc:
        return no_update, message(str(exc), error=True)
    except Exception:
        logger.exception("Could not activate a maintained prompt")
        return no_update, message("Unable to activate this prompt. Your changes remain in the editor.", error=True)
