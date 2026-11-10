"""UGM Routines: one layout and shared callbacks for every configured routine."""

import base64
from datetime import datetime
import json
import logging
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from dash import ALL, MATCH, Input, Output, State, callback, clientside_callback, ctx, dcc, html, no_update, register_page
from dash.exceptions import PreventUpdate
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import plotly.graph_objects as go

from services.openai_client import AIError
from services.routine_tools import parse_import
from services.routines import RoutineService


register_page(__name__, path="/routines/workspace", name="Executar rotina")
BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "data" / "fixed_income.db"
logger = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("America/Sao_Paulo")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
STATE_LABELS = {
    "PENDING": "Pendente", "OVERDUE": "Atrasado",
    "ON_TIME": "Concluído no prazo", "LATE": "Concluído atrasado",
}
ACTION_LABELS = {
    "save_quotes": "Importação de cotações", "query_quotes": "Consulta de cotações",
    "import_debtors": "Importação de devedores", "update_debtor": "Atualização do cliente",
    "query_debtors": "Consulta de devedores", "debtor_report": "Fechamento de devedores",
    "manual_complete": "Conclusão da etapa",
}
EXECUTION_LABELS = {
    "PENDING_REVIEW": "Aguardando confirmação", "EXECUTING": "Em andamento",
    "SUCCESS": "Concluído", "FAILED": "Falhou", "CANCELLED": "Cancelado",
}


def service():
    return RoutineService(DATABASE_PATH)


def _username(current_user):
    username = (current_user or {}).get("user")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Ative seu acesso em UGM Access para operar as rotinas.")
    return username.strip()


def _selected_activity(routine_id, selection):
    if isinstance(selection, dict) and selection.get("routine") == routine_id:
        return selection.get("activity")
    return None


def _require_activity(routine_id, selection):
    activity_id = _selected_activity(routine_id, selection)
    if not activity_id:
        raise ValueError("Selecione uma atividade no menu à esquerda antes de continuar.")
    return activity_id


def _error(exc):
    if isinstance(exc, (ValueError, LookupError, AIError)):
        return str(exc)
    logger.error("Routine request failed: %s", type(exc).__name__)
    return "Não foi possível concluir a operação. Verifique a configuração e tente novamente."


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {"text": value}
    return value or {}


def _grid_columns(rows, *, editable=False, update=False):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    return [{
        "field": key, "headerName": key, "minWidth": 145,
        "editable": editable and key not in {"ID_SECURITY", "TX_DI_REF", "TX_SPREAD"}
                    and (not update or key in {"description", "resolved"}),
        **({"checkboxSelection": True, "headerCheckboxSelection": True} if index == 0 and editable else {}),
    } for index, key in enumerate(fields)]


def _quote_summary_columns(rows):
    labels = {
        "ID_EXECUTION": "Run", "NM_ACTIVITY": "Atividade", "TS_EXECUTION": "Importação confirmada", "NM_BROKER": "Broker",
        "NM_SECURITY": "Ativo", "DT_MATURITY": "Vencimento", "SIDE": "Lado", "TX_QUOTE": "Taxa (%)",
        "VL_QUOTE": "Volume financeiro", "TS_REF": "Referência da fonte", "SPREAD_QUOTE": "Spread (bps)",
        "TX_DI_REF": "DI de referência (%)", "TX_SPREAD": "Taxa calculada (%)",
    }
    available = {key for row in rows for key in row}
    columns = []
    for field, label in labels.items():
        if field not in available:
            continue
        if field in {"SPREAD_QUOTE", "TX_DI_REF", "TX_SPREAD"} and all(row.get(field) is None for row in rows):
            continue
        column = {"field": field, "headerName": label, "minWidth": 130, "editable": False}
        if field in {"TX_QUOTE", "TX_DI_REF", "TX_SPREAD"}:
            column.update(type="numericColumn", valueFormatter={
                "function": "params.value == null ? '—' : (params.value * 100).toFixed(4) + '%'",
            })
        if field == "NM_SECURITY":
            column.update(pinned="left", minWidth=185)
        columns.append(column)
    return columns


def _run_label(run):
    label = run.get("label")
    if not label:
        try:
            label = datetime.fromisoformat(run["ts_execution"].replace("Z", "+00:00")).astimezone(TIMEZONE).strftime("%H:%M:%S")
        except (KeyError, TypeError, ValueError):
            label = "Horário não informado"
    identifier = f"#{run['id_execution']}"
    return str(label) if identifier in str(label) else f"{label} · {identifier}"


def quote_run_figure(snapshot, *, revision=None):
    """Draw confirmed observations, preserving missing/ambiguous vertex gaps."""
    vertices = sorted(snapshot.get("vertices", []),
                      key=lambda item: (item.get("DT_MATURITY") or "9999", item["NM_SECURITY"]))
    securities = list(dict.fromkeys(item["NM_SECURITY"] for item in vertices))
    figure = go.Figure()
    for index, run in enumerate(snapshot.get("runs", [])):
        label = _run_label(run)
        color = f"hsl({(218 + index * 137.508) % 360:.6f}, 65%, 45%)"
        observations = {}
        for row in run.get("rows", []):
            if type(row.get("TX_QUOTE")) in (int, float) and math.isfinite(row["TX_QUOTE"]):
                observations.setdefault((row.get("NM_SECURITY"), row.get("SIDE")), []).append(row)
        for side, dash in (("Bid", "solid"), ("Ask", "dash")):
            x, y, details = [], [], []
            for security in securities:
                quotes = observations.get((security, side), [])
                if not quotes:
                    x.append(security)
                    y.append(None)
                    details.append(None)
                    continue
                if len(quotes) > 1:
                    # Preserve every actual quote without choosing or averaging
                    # observations at an ambiguous vertex of this import.
                    x.append(None)
                    y.append(None)
                    details.append(None)
                for row in quotes:
                    x.append(security)
                    y.append(row["TX_QUOTE"] * 100)
                    details.append([row.get("NM_BROKER") or run.get("source") or "",
                                    side, row.get("TS_REF") or "Não informado"])
                    if len(quotes) > 1:
                        x.append(None)
                        y.append(None)
                        details.append(None)
            figure.add_trace(go.Scatter(
                x=x, y=y, mode="lines+markers", name=f"{label} · {side}",
                legendgroup=f"run-{run['id_execution']}", showlegend=True,
                line={"color": color, "dash": dash, "width": 2.3, "shape": "linear"},
                marker={"color": color, "size": 7, "line": {"color": "white", "width": 1.2}},
                connectgaps=False, customdata=details,
                hovertemplate=("<b>%{x}</b><br>%{customdata[0]} · %{customdata[1]}"
                               "<br>Taxa: %{y:.4f}%<br>Referência: %{customdata[2]}<extra>%{fullData.name}</extra>"),
            ))
    figure.update_layout(
        template="plotly_white", height=390, margin={"l": 55, "r": 20, "t": 65, "b": 70},
        font={"family": "Frutiger, Arial, sans-serif", "color": "#475569", "size": 11},
        paper_bgcolor="white", plot_bgcolor="white", hovermode="closest", uirevision=revision,
        legend={"orientation": "h", "x": 0, "y": 1.03, "yanchor": "bottom", "groupclick": "togglegroup", "font": {"size": 10}},
        xaxis={"title": "Vértices por vencimento", "type": "category", "categoryorder": "array",
               "categoryarray": securities, "showgrid": False, "tickangle": -25, "automargin": True},
        yaxis={"title": "Taxa (%)", "tickformat": ".2f", "ticksuffix": "%", "gridcolor": "#eef2f7", "zeroline": False},
    )
    if not snapshot.get("runs"):
        figure.add_annotation(text="As curvas aparecerão após a primeira importação confirmada de hoje.",
                              x=.5, y=.5, xref="paper", yref="paper", showarrow=False,
                              font={"color": "#94a3b8", "size": 13})
    return figure


def _quote_summary_metrics(summary):
    return [html.Div([
        html.Span(str(summary.get(key, 0)), className="routine-metric-value"),
        html.Span(label, className="routine-metric-label"),
    ], className="routine-metric") for key, label in (
        ("run_count", "runs confirmadas"), ("quote_count", "cotações"), ("security_count", "vértices"),
    )]


def review_card(execution):
    execution_id = execution["ID_EXECUTION"]
    args = _json(execution.get("INPUT_JSON"))
    preview = _json(execution.get("RESULT_JSON"))
    pending = execution["STATUS"] == "PENDING_REVIEW"
    update = preview.get("kind") == "update"
    importing = "rows" in args
    rows = args.get("rows") if pending and importing else preview.get("rows", [])
    if pending and update:
        rows = [args]
    rows = rows if isinstance(rows, list) else []
    elements = [
        html.Div([
            html.Strong(ACTION_LABELS.get(execution["NM_ACTION"], execution["NM_ACTION"])),
            html.Span(f"#{execution_id} · {EXECUTION_LABELS.get(execution['STATUS'], execution['STATUS'])}", className="routine-card-badge"),
        ], className="routine-card-heading"),
    ]
    if preview.get("source"):
        elements.append(html.Div(
            f"Fonte: {preview['source']} · Registros: {preview.get('count', len(rows))}",
            className="routine-muted",
        ))
    if preview.get("filters"):
        elements.append(html.Div("Filtros: " + json.dumps(preview["filters"], ensure_ascii=False), className="routine-muted"))
    if preview.get("current") is not None:
        elements += [html.Div("Valores atuais", className="routine-small-label"),
                     html.Pre(json.dumps(preview["current"], ensure_ascii=False, indent=2), className="routine-pre")]
    if preview.get("warnings"):
        elements.append(html.Div(" · ".join(map(str, preview["warnings"])), className="routine-warning"))
    if preview.get("text"):
        elements.append(html.Pre(str(preview["text"]), className="routine-pre"))
    if preview.get("body"):
        elements += [html.Div(str(preview.get("subject", "Rascunho de fechamento"))),
                     html.Pre(str(preview["body"]), className="routine-pre")]
    if pending:
        elements.append(html.Div(
            "Revise os valores. Somente o clique em Confirmar efetiva esta proposta.",
            className="routine-muted",
        ))
    if rows or pending:
        elements.append(dag.AgGrid(
            id={"type": "routine-card-grid", "execution": execution_id},
            rowData=rows,
            columnDefs=_grid_columns(rows, editable=pending and (importing or update), update=update),
            defaultColDef={"resizable": True, "sortable": True, "filter": True},
            dashGridOptions={"rowSelection": "multiple", "stopEditingWhenCellsLoseFocus": True,
                             "singleClickEdit": True},
            className="ag-theme-alpine routine-review-grid",
            style={"height": "260px"},
        ))
    if pending:
        elements += [
            html.Div([
                html.Button("Confirmar", id={"type": "routine-card-confirm", "execution": execution_id},
                            n_clicks=0, className="routine-primary", type="button"),
                html.Button("Excluir linhas selecionadas", id={"type": "routine-card-delete", "execution": execution_id},
                            n_clicks=0, className="routine-secondary", type="button", disabled=not importing),
                html.Button("Cancelar", id={"type": "routine-card-cancel", "execution": execution_id},
                            n_clicks=0, className="routine-secondary", type="button"),
            ], className="routine-actions"),
            html.Div(id={"type": "routine-card-feedback", "execution": execution_id},
                     className="routine-feedback", role="status"),
            dcc.Store(id={"type": "routine-card-result", "execution": execution_id}),
        ]
    elif execution.get("ERROR_MESSAGE"):
        elements.append(html.Div(execution["ERROR_MESSAGE"], className="routine-warning"))
    elif not rows and not any(preview.get(key) for key in ("text", "body")):
        elements.append(html.Pre(json.dumps(preview, ensure_ascii=False, indent=2), className="routine-pre"))
    return html.Div(elements, className="routine-review-card")


def _transcript(history, executions, *, quote_import=False):
    events = []
    for message in history:
        for role, field, timestamp in (("user", "USER_MESSAGE", "TS_MESSAGE"),
                                       ("assistant", "ASSISTANT_MESSAGE", "TS_RESPONSE")):
            text = message.get(field)
            if not text:
                continue
            events.append((message.get(timestamp) or message.get("TS_MESSAGE", ""), message.get("ID_MESSAGE", 0), html.Div([
                html.Div("Você" if role == "user" else "Assistente", className="routine-small-label"),
                html.Div(text, className="routine-message-text"),
            ], className=f"routine-message routine-message-{role}")))
    for execution in executions:
        if quote_import and execution["NM_ACTION"] != "save_quotes":
            continue
        events.append((execution.get("TS_REQUEST", ""), execution["ID_EXECUTION"], review_card(execution)))
    events.sort(key=lambda event: (event[0], event[1]))
    return [event[2] for event in events] or [html.Div(
        "Cole a tabela da run XP para preparar a importação." if quote_import else
        "Sua conversa começa aqui. O histórico é individual e reinicia a cada dia; os dados operacionais são compartilhados.",
        className="routine-empty",
    )]


def _timeline(steps, *, allow_manual=True):
    if not steps:
        return html.Div("Atividade sem horário definido para hoje.", className="routine-muted")
    children = []
    for index, step in enumerate(steps, 1):
        state = step.get("state", "PENDING")
        progress = {"IN_PROGRESS": "Em andamento", "PENDING_REVIEW": "Aguardando confirmação"}.get(step.get("progress"))
        status = STATE_LABELS.get(state, state)
        if progress and state not in {"ON_TIME", "LATE"}:
            status = f"Atrasado · {progress}" if state == "OVERDUE" else progress
        detail = step.get("completed_by") or ""
        if step.get("completed_at"):
            completed_at = step["completed_at"]
            if not allow_manual:
                try:
                    completed_at = datetime.fromisoformat(completed_at).astimezone(TIMEZONE).strftime("%H:%M")
                except (TypeError, ValueError):
                    pass
            detail += f" · {completed_at}"
        content = [
            html.Div([
                html.Strong(step["NM_ACTIVITY"]),
                html.Span(step.get("DEADLINE_TIME") or "Sem horário", className="routine-muted"),
            ], className="routine-step-heading"),
            html.Div(status, className="routine-step-state"),
            html.Small(detail),
        ]
        if allow_manual:
            content.append(html.Button("Concluir atividade", id={"type": "routine-activity-complete", "activity": step["ID_ACTIVITY"]},
                        n_clicks=0, type="button", className="routine-secondary",
                        disabled=state in {"ON_TIME", "LATE"}))
        if allow_manual:
            children.append(html.Div(content, className=f"routine-step routine-step-{state.lower()}"))
        else:
            children.append(html.Div([
                html.Div(str(index), className="routine-step-number"),
                html.Div(content, className="routine-step-content"),
            ], className=f"routine-step routine-step-vertical routine-step-{state.lower()} routine-progress-{str(step.get('progress') or '').lower()}"))
    return children


def layout(routine=None, activity=None, **_query):
    engine = service()
    engine.ensure_schema()
    routines = engine.list_routines()
    available = {entry["ID_ROUTINE"] for entry in routines}
    selected = routine if routine in available else (routines[0]["ID_ROUTINE"] if routines else None)
    activity_ids = {item["ID_ACTIVITY"] for item in engine.list_activities(selected)} if selected else set()
    initial_activity = {"routine": selected, "activity": activity} if activity in activity_ids else None
    initial_quote_import = bool(initial_activity and engine.is_quote_import(selected, activity))
    groups = {}
    for routine in routines:
        group = routine["NM_GROUP"]
        groups.setdefault(group, []).append(routine)
    menu = []
    for group, entries in groups.items():
        menu.append(html.Div(group, className="routine-menu-group"))
        menu.extend(html.Button(entry["NM_ROUTINE"],
                                id={"type": "routine-select", "routine": entry["ID_ROUTINE"]},
                                n_clicks=0, type="button", className="routine-menu-button") for entry in entries)
    return dbc.Container(fluid=True, className="page-container routines-page", children=[
        dcc.Store(id="routine-selected", data=selected),
        dcc.Store(id="routine-activity-selected", data=initial_activity),
        dcc.Store(id="routine-refresh", data=0),
        dcc.Store(id="routine-view-key"),
        dcc.Store(id="routine-scroll"),
        dcc.Store(id="routine-card-notice"),
        dcc.Interval(id="routine-poll", interval=15_000, n_intervals=0),
        html.Div(["Ative seu acesso em ", dcc.Link("UGM Access", href="/keys"), " para abrir UGM Routines."],
                 id="routine-access", className="routine-panel"),
        html.Div(id="routine-shell", className="routine-shell routine-shell-activities", style={"display": "none"}, children=[
            html.Aside([
                html.H4("UGM Routines"),
                dcc.Link("Panorama do dia", href="/routines", className="routine-menu-button routine-menu-overview"),
                *menu,
            ], className="routine-sidebar"),
            html.Aside([
                html.H4("Atividades", id="routine-activities-heading"),
                html.P("Escolha o que deseja executar.", className="routine-muted"),
                html.Div(id="routine-activity-menu", **{"aria-label": "Atividades da rotina"}),
            ], className="routine-activities-panel", **{"aria-labelledby": "routine-activities-heading"}),
            html.Main(className="routine-main", children=[
                html.Header([html.H3(id="routine-title"), html.P(id="routine-description"),
                             html.H4(id="routine-activity-title", className="routine-activity-title"),
                             html.Div(id="routine-configuration", className="routine-warning")], className="routine-hero"),
                html.Div("Selecione uma atividade no menu à esquerda para começar.",
                         id="routine-activity-empty", className="routine-panel routine-activity-empty"),
                html.Div(id="routine-activity-content", style={} if initial_activity else {"display": "none"}, children=[
                    html.Div(id="routine-workspace", className="routine-workspace routine-workspace-import" if initial_quote_import else "routine-workspace", children=[
                        html.Section(className="routine-panel routine-chat-panel", children=[
                            html.H5("Importar run" if initial_quote_import else "Seu chat", id="routine-chat-heading"),
                            html.P(id="routine-chat-hint", className="routine-muted"),
                            html.Div(id="routine-chat", className="routine-chat", **{"aria-live": "polite"}),
                            dcc.Textarea(id="routine-chat-text", value="", placeholder="Converse com o assistente desta rotina…",
                                         className="routine-textarea", maxLength=100_000),
                            html.Div([html.Button("Enviar", id="routine-send", n_clicks=0, type="button", className="routine-primary"),
                                      html.Button("Preparar fechamento", id="routine-report", n_clicks=0,
                                                  type="button", className="routine-secondary", style={"display": "none"})],
                                     className="routine-actions"),
                            html.Div(id="routine-chat-feedback", className="routine-feedback", role="status"),
                        ]),
                        html.Section(id="routine-timeline-panel", className="routine-panel routine-timeline-panel", children=[
                            html.Div("ACOMPANHAMENTO", className="routine-eyebrow"),
                            html.H5("Atividade selecionada", id="routine-timeline-title"),
                            html.Div(id="routine-timeline", className="routine-timeline routine-timeline-vertical" if initial_quote_import else "routine-timeline"),
                            html.Div(id="routine-timeline-feedback", role="status"),
                        ]),
                        html.Section(id="routine-import-panel", className="routine-panel routine-import-panel",
                                     style={"display": "none"} if initial_quote_import else {}, children=[
                            html.H5("Importar dados"),
                            html.Label("Fonte", htmlFor="routine-source"),
                            dcc.Dropdown(id="routine-source", clearable=False),
                            html.P(id="routine-import-hint", className="routine-muted"),
                            dcc.Textarea(id="routine-import-text", value="", placeholder="Cole a run ou tabela para revisão…",
                                         className="routine-textarea", maxLength=100_000),
                            dcc.Upload(id="routine-upload", multiple=False, className="routine-upload",
                                       accept=".csv,.tsv,.xlsx,.eml,.txt", children=html.Div("Arraste um arquivo ou clique para selecionar")),
                            html.Div(id="routine-upload-name", className="routine-muted"),
                            html.Button("Preparar revisão", id="routine-prepare", n_clicks=0,
                                        type="button", className="routine-primary"),
                            html.Div(id="routine-import-feedback", className="routine-feedback", role="status"),
                        ]),
                    ]),
                    html.Section(id="routine-quote-summary-panel", className="routine-panel routine-quote-summary-panel",
                                 style={} if initial_quote_import else {"display": "none"}, children=[
                        html.Div([
                            html.Div([html.Div("MERCADO DO DIA", className="routine-eyebrow"), html.H5("Resumo das runs")]),
                            html.Div(id="routine-quote-feedback", className="routine-muted"),
                        ], className="routine-section-heading"),
                        html.Div(id="routine-quote-counts", className="routine-quote-counts"),
                        html.Div([
                            html.H6("Curvas por importação"),
                            html.Div("Cada cor representa uma run · Bid contínuo · Ask tracejado", className="routine-muted"),
                        ], className="routine-chart-heading"),
                        dcc.Graph(id="routine-quote-chart", figure=quote_run_figure({}),
                                  config={"displayModeBar": False, "responsive": True}, className="routine-quote-chart"),
                        html.Div([html.H6("Cotações confirmadas"), html.Span("Todas as importações do dia", className="routine-muted")],
                                 className="routine-section-heading routine-table-heading"),
                        dag.AgGrid(id="routine-quote-data", rowData=[], columnDefs=[],
                                   defaultColDef={"resizable": True, "sortable": True, "filter": True},
                                   dashGridOptions={"pagination": True, "paginationPageSize": 20},
                                   className="ag-theme-alpine routine-quote-grid", style={"height": "350px"}),
                    ]),
                    html.Section(id="routine-data-panel", className="routine-panel",
                                 style={"display": "none"} if initial_quote_import else {}, children=[
                        html.H5("Dados compartilhados do dia"),
                        html.Div(id="routine-data-feedback", className="routine-muted"),
                        dag.AgGrid(id="routine-data", rowData=[], columnDefs=[],
                                   defaultColDef={"resizable": True, "sortable": True, "filter": True},
                                   className="ag-theme-alpine", style={"height": "330px"}),
                    ]),
                ]),
            ]),
        ]),
    ])


@callback(Output("routine-shell", "style"), Output("routine-access", "style"), Input("current-user", "data"))
def routine_access(current_user):
    active = bool((current_user or {}).get("user"))
    return ({"display": "grid"} if active else {"display": "none"},
            {"display": "none"} if active else {"display": "block"})


@callback(Output("routine-selected", "data"),
          Input({"type": "routine-select", "routine": ALL}, "n_clicks"),
          State("routine-selected", "data"), prevent_initial_call=True)
def select_routine(clicks, selected):
    if not isinstance(ctx.triggered_id, dict) or not any(clicks or []):
        raise PreventUpdate
    routine_id = ctx.triggered_id["routine"]
    return routine_id if routine_id != selected else no_update


@callback(Output({"type": "routine-select", "routine": ALL}, "className"),
          Input("routine-selected", "data"), State({"type": "routine-select", "routine": ALL}, "id"))
def highlight_routine(routine_id, menu_ids):
    return ["routine-menu-button" + (" routine-menu-selected" if item["routine"] == routine_id else "")
            for item in menu_ids]


@callback(Output("routine-activity-selected", "data"),
          Input("routine-selected", "data"),
          Input({"type": "routine-activity-select", "routine": ALL, "activity": ALL}, "n_clicks"),
          State("routine-activity-selected", "data"))
def select_activity(routine_id, clicks, selection):
    if isinstance(ctx.triggered_id, dict) and any(clicks or []):
        if ctx.triggered_id.get("routine") != routine_id:
            raise PreventUpdate
        activity_id = ctx.triggered_id["activity"]
        service().load_activity(routine_id, activity_id)
        return {"routine": routine_id, "activity": activity_id}
    if _selected_activity(routine_id, selection):
        return no_update
    return None if selection is not None else no_update


@callback(Output("routine-activity-menu", "children"),
          Input("routine-selected", "data"), Input("routine-activity-selected", "data"),
          Input("current-user", "data"), Input("routine-poll", "n_intervals"), Input("routine-refresh", "data"))
def activity_menu(routine_id, selection, current_user, _interval, _refresh):
    if not routine_id or not (current_user or {}).get("user"):
        return []
    try:
        engine = service()
        activities = engine.list_activities(routine_id)
        timeline = engine.get_routine_timeline(routine_id)
        selected = _selected_activity(routine_id, selection)
        buttons = []
        for activity in activities:
            activity_id = activity["ID_ACTIVITY"]
            steps = sorted((step for step in timeline if step["ID_ACTIVITY"] == activity_id),
                           key=lambda step: (step["DEADLINE_TIME"] is None, step["DEADLINE_TIME"] or "", step["DISPLAY_ORDER"]))
            step = steps[0] if steps else None
            completion = engine.get_activity_completion(routine_id, activity_id)
            schedule = (step.get("DEADLINE_TIME") or "Sem horário definido") if step else "Sem horário hoje"
            state = step["state"] if step else "ON_TIME" if completion else "PENDING"
            status = "Concluída" if state in {"ON_TIME", "LATE"} else "Atrasada" if state == "OVERDUE" else "Pendente"
            if step and step.get("progress") and not completion:
                status = "Aguardando confirmação" if step["progress"] == "PENDING_REVIEW" else "Em andamento"
            buttons.append(html.Button([
                html.Span(activity["NM_ACTIVITY"], className="routine-activity-name"),
                html.Span(schedule, className="routine-activity-schedule"),
                html.Span(status, className=f"routine-activity-state routine-activity-state-{state.lower()}"),
            ], id={"type": "routine-activity-select", "routine": routine_id, "activity": activity_id},
               n_clicks=0, type="button", className="routine-activity-button" + (" routine-activity-selected" if activity_id == selected else ""),
               **{"aria-pressed": "true" if activity_id == selected else "false"}))
        return buttons or html.P("Nenhuma atividade disponível.", className="routine-muted")
    except Exception as exc:
        return html.Div(_error(exc), className="routine-warning")


@callback(Output("routine-title", "children"), Output("routine-description", "children"),
          Output("routine-source", "options"), Output("routine-source", "value"),
          Output("routine-import-hint", "children"), Output("routine-report", "style"),
          Output("routine-configuration", "children"),
          Output("routine-workspace", "className"), Output("routine-import-panel", "style"),
          Output("routine-data-panel", "style"), Output("routine-chat-heading", "children"),
          Output("routine-chat-hint", "children"), Output("routine-chat-text", "placeholder"),
          Output("routine-quote-summary-panel", "style"), Output("routine-timeline", "className"),
          Output("routine-timeline-title", "children"),
          Output("routine-activity-title", "children"), Output("routine-activity-content", "style"),
          Output("routine-activity-empty", "style"),
          Input("routine-selected", "data"), Input("current-user", "data"), Input("routine-view-key", "data"),
          Input("routine-activity-selected", "data"))
def load_routine_view(routine_id, current_user, _view_key, selection):
    if not routine_id or not (current_user or {}).get("user"):
        raise PreventUpdate
    engine = service()
    routine = engine.load_routine(routine_id)
    activity_id = _selected_activity(routine_id, selection)
    unavailable = ""
    if activity_id:
        try:
            routine = engine.load_activity(routine_id, activity_id)
        except ValueError as exc:
            unavailable = str(exc)
            activity_id = None
    if not activity_id:
        return (routine["NM_ROUTINE"], routine["DS_ROUTINE"], [], None, "", {"display": "none"}, unavailable,
                "routine-workspace", {"display": "none"}, {"display": "none"}, "Seu chat", "", "Selecione uma atividade…",
                {"display": "none"}, "routine-timeline", "Etapas do dia", "", {"display": "none"}, {})
    quote_import = engine.is_quote_import(routine_id, activity_id)
    debtors = routine["LAYOUT_KEY"] == "debtors"
    sources = ["Tabela manual"] if debtors else _json(routine.get("CONFIG_JSON")).get("sources", [])
    tools = {item["NM_TOOL"] for item in engine.load_routine_tools(routine_id, activity_id)}
    warnings = []
    try:
        engine.load_prompt(routine_id, activity_id)
    except (LookupError, ValueError, AIError) as exc:
        warnings.append(str(exc))
    if routine_id == "NTNF_RUN":
        warnings.append("A gravação NTN-F depende da curva DI Bloomberg e da convenção de composição configuradas no backend.")
    hint = ("CSV, TSV e Excel com os nomes de colunas do contrato são lidos sem IA. "
            "Texto livre ou e-mail é interpretado pelo assistente. Revise o card antes de gravar.")
    if not debtors:
        hint += " TX_QUOTE é uma taxa decimal: 0,07 corresponde a 7%."
        hint += " Colunas: NM_SECURITY, SIDE (Bid/Ask), TX_QUOTE; opcionais: VL_QUOTE e TS_REF."
        if routine_id == "NTNF_RUN":
            hint += " Para NTN-F, informe SPREAD_QUOTE em pontos-base."
    else:
        hint += " Colunas: ID_CLIENT, NR_DAYS, NM_RESP_GWM, DESCR; FL_RESOLVED: 0 = resolvido, 1 = devedor."
    return (routine["NM_ROUTINE"], routine["DS_ACTIVITY"] or routine["DS_ROUTINE"], [{"label": source, "value": source} for source in sources],
            sources[0] if sources else None,
            hint, {"display": "inline-block"} if debtors and "debtor_report" in tools else {"display": "none"}, " · ".join(warnings),
            "routine-workspace routine-workspace-import" if quote_import else "routine-workspace",
            {"display": "none"} if quote_import or not tools.intersection({"save_quotes", "import_debtors"}) else {},
            {"display": "none"} if quote_import or not tools.intersection({"query_quotes", "query_debtors"}) else {},
            "Importar run" if quote_import else "Seu chat",
            "Cole a tabela da run XP. Revise as cotações no card e confirme a importação." if quote_import else "",
            "Cole aqui a tabela da run XP…" if quote_import else "Converse com o assistente desta rotina…",
            {} if quote_import else {"display": "none"},
            "routine-timeline routine-timeline-vertical" if quote_import else "routine-timeline",
            "Atividade selecionada",
            routine["NM_ACTIVITY"], {}, {"display": "none"})


@callback(Output("routine-timeline", "children"), Output("routine-data", "rowData"),
          Output("routine-data", "columnDefs"), Output("routine-data-feedback", "children"),
          Output("routine-view-key", "data"),
          Output("routine-quote-data", "rowData"), Output("routine-quote-data", "columnDefs"),
          Output("routine-quote-chart", "figure"), Output("routine-quote-counts", "children"),
          Output("routine-quote-feedback", "children"),
          Input("routine-poll", "n_intervals"), Input("routine-selected", "data"),
          Input("current-user", "data"), Input("routine-refresh", "data"), Input("routine-activity-selected", "data"),
          State("routine-view-key", "data"))
def refresh_shared_routine(_interval, routine_id, current_user, _refresh, selection, previous_key):
    activity_id = _selected_activity(routine_id, selection)
    if not routine_id or not activity_id or not (current_user or {}).get("user"):
        return [], [], [], "", None, [], [], quote_run_figure({}), [], ""
    key = [routine_id, _username(current_user), datetime.now(TIMEZONE).date().isoformat(), activity_id]
    engine = service()
    try:
        quote_import = engine.is_quote_import(routine_id, activity_id)
        timeline = _timeline(engine.get_routine_timeline(routine_id, activity_id=activity_id), allow_manual=not quote_import)
        if quote_import:
            snapshot = engine.get_daily_quote_runs(routine_id)
            rows = snapshot.get("rows", [])
            return (timeline, [], [], "", key if key != previous_key else no_update,
                    rows, _quote_summary_columns(rows), quote_run_figure(snapshot, revision=f"{routine_id}:{key[2]}"),
                    _quote_summary_metrics(snapshot.get("summary", {})),
                    [html.Div("Importações confirmadas · " + datetime.fromisoformat(key[2]).strftime("%d/%m/%Y")),
                     html.Div(" · ".join(snapshot.get("warnings", [])), className="routine-warning")])
        tools = {item["NM_TOOL"] for item in engine.load_routine_tools(routine_id, activity_id)}
        if not tools.intersection({"query_debtors", "query_quotes"}):
            return timeline, [], [], "", key if key != previous_key else no_update, [], [], quote_run_figure({}), [], ""
        result = engine.get_operational_data(routine_id, activity_id=activity_id)
        rows = result.get("rows", [])
        return (timeline, rows, _grid_columns(rows), f"{len(rows)} registros exibidos · {key[2]} · America/Sao_Paulo",
                key if key != previous_key else no_update, [], [], quote_run_figure({}), [], "")
    except Exception as exc:
        return [], [], [], _error(exc), key if key != previous_key else no_update, [], [], quote_run_figure({}), [], _error(exc)


@callback(Output("routine-chat", "children"), Input("routine-selected", "data"), Input("current-user", "data"),
          Input("routine-refresh", "data"), Input("routine-view-key", "data"), Input("routine-activity-selected", "data"))
def load_routine_chat(routine_id, current_user, _refresh, _view_key, selection):
    activity_id = _selected_activity(routine_id, selection)
    if not routine_id or not activity_id or not (current_user or {}).get("user"):
        return []
    try:
        engine = service()
        username = _username(current_user)
        return _transcript(engine.load_chat_history(routine_id, username, activity_id=activity_id),
                           engine.get_routine_executions(routine_id, username=username, activity_id=activity_id),
                           quote_import=engine.is_quote_import(routine_id, activity_id))
    except Exception as exc:
        return html.Div(_error(exc), className="routine-warning")


@callback(Output("routine-upload-name", "children"), Input("routine-upload", "filename"))
def show_routine_upload(filename):
    return filename or "Arquivos: CSV, TSV, XLSX, EML ou TXT. Limite: 10 MB."


@callback(Output("routine-import-text", "value"), Output("routine-upload", "contents"),
          Output("routine-upload", "filename"), Output("routine-chat-text", "value", allow_duplicate=True),
          Input("routine-selected", "data"), Input("current-user", "data"), Input("routine-view-key", "data"),
          Input("routine-activity-selected", "data"),
          prevent_initial_call=True)
def reset_routine_inputs(_routine_id, _current_user, _view_key, _selection):
    # Files and unsent text belong to the current routine, user and operational day.
    return "", None, None, ""


clientside_callback(
    """function(children) {
        window.requestAnimationFrame(function() {
            var chat = document.getElementById('routine-chat');
            if (chat) { chat.scrollTop = chat.scrollHeight; }
        });
        return window.dash_clientside.no_update;
    }""",
    Output("routine-scroll", "data"), Input("routine-chat", "children"),
)


@callback(Output("routine-refresh", "data"), Output("routine-chat-feedback", "children"),
          Output("routine-import-feedback", "children"), Output("routine-chat-text", "value"),
          Input("routine-send", "n_clicks"), Input("routine-prepare", "n_clicks"), Input("routine-report", "n_clicks"),
          State("routine-selected", "data"), State("current-user", "data"), State("routine-chat-text", "value"),
          State("routine-import-text", "value"), State("routine-source", "value"),
          State("routine-upload", "contents"), State("routine-upload", "filename"),
          State("routine-refresh", "data"),
          State("routine-activity-selected", "data"),
          prevent_initial_call=True,
          running=[(Output("routine-send", "disabled"), True, False),
                   (Output("routine-prepare", "disabled"), True, False),
                   (Output("routine-report", "disabled"), True, False)])
def routine_request(_send, _prepare, _report, routine_id, current_user, text, import_text, source,
                    upload, filename, refresh, selection):
    trigger = ctx.triggered_id
    if trigger not in {"routine-send", "routine-prepare", "routine-report"}:
        raise PreventUpdate
    try:
        username = _username(current_user)
        activity_id = _require_activity(routine_id, selection)
        engine = service()
        quote_import = engine.is_quote_import(routine_id, activity_id)
        if trigger == "routine-send":
            if not text or not text.strip():
                raise ValueError("Digite uma mensagem.")
            engine.process_routine_message(routine_id, username, text, activity_id=activity_id)
            return (refresh or 0) + 1, "", "", ""
        if quote_import:
            raise ValueError("Cole a tabela da run diretamente no chat para preparar a importação.")
        if trigger == "routine-report":
            engine.prepare_action(routine_id, username, "debtor_report", {}, activity_id=activity_id)
            return (refresh or 0) + 1, "Fechamento preparado a partir da base compartilhada.", "", no_update
        if engine.load_routine(routine_id)["LAYOUT_KEY"] != "debtors" and not source:
            raise ValueError("Selecione uma fonte habilitada antes de preparar a importação.")
        content = import_text or ""
        if upload:
            if len(upload) > MAX_UPLOAD_BYTES * 4 // 3 + 1024:
                raise ValueError("O arquivo excede o limite de 10 MB.")
            try:
                content = base64.b64decode(upload.split(",", 1)[1], validate=True)
            except (ValueError, IndexError) as exc:
                raise ValueError("Não foi possível ler o arquivo anexado.") from exc
            if len(content) > MAX_UPLOAD_BYTES:
                raise ValueError("O arquivo excede o limite de 10 MB.")
        if not content:
            raise ValueError("Cole os dados ou selecione um arquivo.")
        parsed = parse_import(content, filename=filename if upload else None)
        if parsed.get("rows") is not None:
            tool = "import_debtors" if engine.load_routine(routine_id)["LAYOUT_KEY"] == "debtors" else "save_quotes"
            args = {"rows": parsed["rows"]}
            if tool == "save_quotes":
                args["source"] = source
            engine.prepare_action(routine_id, username, tool, args, activity_id=activity_id)
        else:
            engine.process_routine_message(
                routine_id, username,
                f"Prepare uma proposta de importação para revisão. Fonte informada: {source}.\n\n{parsed.get('text', '')}",
                activity_id=activity_id,
            )
        return (refresh or 0) + 1, "", "Revisão preparada no chat. " + " · ".join(parsed.get("warnings", [])), no_update
    except Exception as exc:
        # Refresh also displays a user message persisted before an unavailable AI response.
        error = _error(exc)
        return ((refresh or 0) + 1, error if trigger != "routine-prepare" else "",
                error if trigger == "routine-prepare" else "", no_update)


@callback(Output({"type": "routine-card-grid", "execution": MATCH}, "rowData"),
          Input({"type": "routine-card-delete", "execution": MATCH}, "n_clicks"),
          State({"type": "routine-card-grid", "execution": MATCH}, "selectedRows"),
          State({"type": "routine-card-grid", "execution": MATCH}, "rowData"), prevent_initial_call=True)
def delete_proposed_rows(clicks, selected, rows):
    if not clicks or not selected:
        raise PreventUpdate
    remaining = list(rows or [])
    for row in selected:
        if row in remaining:
            remaining.remove(row)
    return remaining


@callback(Output({"type": "routine-card-feedback", "execution": MATCH}, "children"),
          Output({"type": "routine-card-confirm", "execution": MATCH}, "disabled"),
          Output({"type": "routine-card-cancel", "execution": MATCH}, "disabled"),
          Output({"type": "routine-card-result", "execution": MATCH}, "data"),
          Input({"type": "routine-card-confirm", "execution": MATCH}, "n_clicks"),
          Input({"type": "routine-card-cancel", "execution": MATCH}, "n_clicks"),
          State({"type": "routine-card-grid", "execution": MATCH}, "rowData"),
          State({"type": "routine-card-grid", "execution": MATCH}, "id"),
          State("routine-selected", "data"), State("current-user", "data"),
          State("routine-activity-selected", "data"),
          prevent_initial_call=True)
def review_routine_action(confirm, cancel, rows, card_id, routine_id, current_user, selection):
    trigger = ctx.triggered_id
    if not isinstance(trigger, dict) or not (confirm or cancel):
        raise PreventUpdate
    try:
        username = _username(current_user)
        activity_id = _require_activity(routine_id, selection)
        engine = service()
        execution_id = card_id["execution"]
        if trigger.get("execution") != execution_id:
            raise ValueError("O botão e os dados devem pertencer à mesma proposta.")
        if trigger["type"] == "routine-card-cancel":
            result = engine.cancel_action(execution_id, username, routine_id=routine_id, activity_id=activity_id)
        else:
            execution = next((item for item in engine.get_routine_executions(routine_id, username=username, activity_id=activity_id)
                              if item["ID_EXECUTION"] == execution_id), None)
            if execution is None:
                raise ValueError("A proposta não está disponível nesta conversa.")
            args = _json(execution["INPUT_JSON"])
            if "rows" in args:
                args["rows"] = rows or []
            elif _json(execution.get("RESULT_JSON")).get("kind") == "update":
                if len(rows or []) != 1:
                    raise ValueError("A atualização deve identificar exatamente um registro.")
                args = rows[0]
            result = engine.confirm_action(execution_id, username, edited_args=args, routine_id=routine_id, activity_id=activity_id)
        notice = {"execution_id": execution_id, "status": result["STATUS"],
                  "updated_at": datetime.now(TIMEZONE).isoformat()}
        if result["STATUS"] == "FAILED":
            return result.get("ERROR_MESSAGE") or "A operação falhou.", True, True, notice
        if result["STATUS"] == "PENDING_REVIEW":
            return "As taxas calculadas mudaram. Revise o card atualizado e confirme novamente.", False, False, notice
        return "Operação concluída.", True, True, notice
    except Exception as exc:
        return _error(exc), False, False, no_update


@callback(Output("routine-refresh", "data", allow_duplicate=True), Output("routine-card-notice", "data"),
          Input({"type": "routine-card-result", "execution": ALL}, "data"),
          State("routine-refresh", "data"), State("routine-card-notice", "data"), prevent_initial_call=True)
def refresh_after_routine_review(results, refresh, previous_notice):
    # Every MATCH output belongs to its card. This ALL callback bridges card
    # results to the shared page and ignores mounting/remounting empty stores.
    notices = [result for result in results or [] if isinstance(result, dict) and result.get("updated_at")]
    if not notices:
        raise PreventUpdate
    notice = max(notices, key=lambda result: result["updated_at"])
    if notice == previous_notice:
        raise PreventUpdate
    return (refresh or 0) + 1, notice


@callback(Output("routine-refresh", "data", allow_duplicate=True), Output("routine-timeline-feedback", "children"),
          Input({"type": "routine-activity-complete", "activity": ALL}, "n_clicks"),
          State("routine-selected", "data"), State("current-user", "data"), State("routine-refresh", "data"),
          State("routine-activity-selected", "data"),
          prevent_initial_call=True)
def complete_routine_activity(clicks, routine_id, current_user, refresh, selection):
    if not isinstance(ctx.triggered_id, dict) or not any(clicks or []):
        raise PreventUpdate
    try:
        engine = service()
        activity_id = _require_activity(routine_id, selection)
        if engine.is_quote_import(routine_id, activity_id):
            raise ValueError("A etapa é atualizada automaticamente pela importação.")
        if ctx.triggered_id["activity"] != activity_id:
            raise ValueError("O botão pertence a outra atividade.")
        engine.complete_activity(routine_id, activity_id, _username(current_user))
        return (refresh or 0) + 1, "Etapa concluída e compartilhada com a equipe."
    except Exception as exc:
        return no_update, _error(exc)
