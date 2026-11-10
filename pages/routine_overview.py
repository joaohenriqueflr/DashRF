"""Read-only daily agenda; operational actions stay in each routine workspace."""

from datetime import datetime
import logging
import math
from pathlib import Path
from urllib.parse import urlencode

from dash import Input, Output, callback, dcc, html, register_page
import dash_bootstrap_components as dbc

from services.routines import RoutineService


register_page(__name__, path="/routines", name="UGM Routines")
DATABASE_PATH = Path(__file__).resolve().parents[1] / "data" / "fixed_income.db"
logger = logging.getLogger(__name__)
COMPLETED = {"ON_TIME", "LATE"}
ACTION_NEEDED = {"OVERDUE", "DUE_SOON", "PENDING_REVIEW", "FAILED"}
CATEGORY_LABELS = {"Market Data": "Dados de mercado", "Operations": "Operações"}
STATUS_LABELS = {
    "OVERDUE": "Atrasada", "DUE_SOON": "Na próxima hora",
    "PENDING_REVIEW": "Aguardando confirmação", "IN_PROGRESS": "Em andamento",
    "FAILED": "Falha na execução", "ON_TIME": "Concluída no prazo",
    "LATE": "Concluída com atraso", "PENDING": "Programada",
    "NO_SCHEDULE": "Sem agenda cadastrada", "NO_STEPS_TODAY": "Sem etapas hoje",
}
WEEKDAYS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
MONTHS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro")


def service():
    return RoutineService(DATABASE_PATH)


def routine_href(routine_id, activity_id=None):
    params = {"routine": routine_id}
    if activity_id:
        params["activity"] = activity_id
    return "/routines/workspace?" + urlencode(params)


def _category(item):
    value = item.get("category") or "Outras rotinas"
    return CATEGORY_LABELS.get(value, value)


def _done(item):
    return item.get("state") in COMPLETED


def _needs_attention(item):
    return not _done(item) and (item.get("state") == "OVERDUE" or item.get("attention") in ACTION_NEEDED)


def _status(item):
    if _done(item):
        return item["state"]
    if item.get("state") == "OVERDUE":
        return "OVERDUE"
    return item.get("attention") or item.get("schedule_status") or item.get("state") or "PENDING"


def _badge(item):
    status = _status(item)
    return html.Span(STATUS_LABELS.get(status, status),
                     className=f"ugm-overview-badge ugm-overview-badge-{status.lower()}")


def _timing(item):
    if _done(item):
        stamp = item.get("completed_at")
        if stamp:
            return "Concluída às " + datetime.fromisoformat(stamp).strftime("%H:%M")
        return "Etapa concluída"
    minutes = item.get("minutes_to_deadline")
    if minutes is None:
        return "Horário não definido" if item.get("has_schedule") else "Abra a rotina para acompanhar"
    if minutes < 0:
        elapsed = max(1, math.ceil(abs(minutes)))
        return f"{elapsed} min de atraso" if elapsed < 60 else f"{elapsed // 60}h {elapsed % 60:02d}min de atraso"
    remaining = math.ceil(minutes)
    if remaining == 0:
        return "Prazo agora"
    if remaining < 60:
        return f"Em {remaining} min"
    return f"Em {remaining // 60}h {remaining % 60:02d}min"


def _progress_detail(item):
    if _done(item):
        return _timing(item)
    if item.get("progress") == "PENDING_REVIEW" or item.get("latest_status") == "PENDING_REVIEW":
        return "Revise e confirme os dados na rotina."
    if item.get("progress") == "IN_PROGRESS" or item.get("latest_status") == "EXECUTING":
        return "Execução em andamento. Acompanhe na rotina."
    if item.get("latest_status") == "FAILED":
        return "Verifique a falha e retome a atividade na rotina."
    if item.get("schedule_status") == "NO_STEPS_TODAY":
        return "A agenda desta rotina não prevê etapas para hoje."
    if item.get("schedule_status") == "NO_SCHEDULE":
        return "Os prazos desta rotina ainda não foram cadastrados."
    return "Abra a rotina para executar esta etapa."


def _activity_row(item):
    return dcc.Link([
        html.Div((item.get("DEADLINE_TIME") or "—")[:5], className="ugm-overview-time"),
        html.Div([
            html.Div(f"{_category(item)} · {item['NM_ROUTINE']}", className="ugm-overview-row-meta"),
            html.H4(item["NM_ACTIVITY"]),
            html.Div(item.get("NM_ACTIVITY", ""), className="ugm-overview-row-meta"),
            html.P(_progress_detail(item)),
        ], className="ugm-overview-row-main"),
        html.Div([_badge(item), html.Small(_timing(item))], className="ugm-overview-row-status"),
        html.Span("↗", className="ugm-overview-arrow", **{"aria-hidden": "true"}),
    ], href=routine_href(item["ID_ROUTINE"], item.get("ID_ACTIVITY")),
        className=f"ugm-overview-row ugm-overview-row-{_status(item).lower()}",
        title=f"Abrir {item['NM_ROUTINE']}: {item['NM_ACTIVITY']}")


def _period(item):
    clock = item.get("DEADLINE_TIME")
    if not clock:
        return "Sem horário definido", "Etapas do dia sem prazo cadastrado"
    hour = int(clock.split(":", 1)[0])
    if hour < 12:
        return "Manhã", "Até 11h59"
    if hour < 17:
        return "Tarde", "12h às 16h59"
    return "Fim do dia", "A partir das 17h"


def _agenda(activities, view):
    selected = [item for item in activities if (
        view == "all" or (view == "attention" and _needs_attention(item))
        or (view == "pending" and not _done(item)) or (view == "completed" and _done(item))
    )]
    if not selected:
        message = "Nenhuma etapa corresponde a este filtro." if activities else "Nenhuma etapa agendada para hoje."
        return html.Div(message, className="ugm-overview-empty")
    periods = {}
    for item in selected:
        periods.setdefault(_period(item), []).append(item)
    return [html.Section([
        html.Div([html.H3(title), html.P(subtitle)], className="ugm-overview-period"),
        html.Div([_activity_row(item) for item in items], className="ugm-overview-timeline"),
    ]) for (title, subtitle), items in periods.items()]


def _summary(activities, unscheduled):
    completed = sum(_done(item) for item in activities)
    attention = sum(_needs_attention(item) for item in [*activities, *unscheduled])
    overdue = sum(item.get("state") == "OVERDUE" for item in activities)
    total = len(activities)
    definitions = [
        (f"{completed}/{total}", "Etapas concluídas", "success"),
        (str(attention), "Precisam de atenção", "warning" if attention else ""),
        (str(overdue), "Etapas atrasadas", "danger" if overdue else ""),
        (str(sum(item.get("schedule_status") == "NO_SCHEDULE" for item in unscheduled)), "Atividades sem agenda", ""),
    ]
    return [html.Div([
        html.Strong(value), html.Small(label),
        *([html.Div(html.Span(style={"width": f"{100 * completed / total if total else 0:.1f}%"}),
                    className="ugm-overview-progress")] if index == 0 else []),
    ], className="ugm-overview-metric" + (f" ugm-overview-metric-{tone}" if tone else ""))
        for index, (value, label, tone) in enumerate(definitions)]


def _attention_panel(activities, unscheduled):
    items = [item for item in [*activities, *unscheduled] if _needs_attention(item)]
    priority = {"OVERDUE": 0, "FAILED": 1, "PENDING_REVIEW": 2, "DUE_SOON": 3}
    items.sort(key=lambda item: (priority.get(_status(item), 4), item.get("deadline") or "9999"))
    attention = [dcc.Link([
        _badge(item), html.Strong(item.get("NM_ACTIVITY") or item.get("NM_ACTIVITY") or item["NM_ROUTINE"]),
        html.Span(item["NM_ROUTINE"] if item.get("has_schedule") else _category(item)),
        html.Small(_progress_detail(item) if item.get("progress") or item.get("latest_status") == "FAILED" else _timing(item)),
        html.Span("Abrir rotina →", className="ugm-overview-attention-link"),
    ], href=routine_href(item["ID_ROUTINE"], item.get("ID_ACTIVITY")), className="ugm-overview-attention-item") for item in items]
    next_item = next((item for item in activities if not _done(item) and item.get("minutes_to_deadline") is not None
                      and item["minutes_to_deadline"] >= 0), None)
    return [
        html.Div([html.H2("Atenção agora"), html.Span(str(len(items)), className="ugm-overview-badge")],
                 className="ugm-overview-panel-heading"),
        html.P("Atrasos, falhas, confirmações e prazos na próxima hora.", className="ugm-overview-note"),
        *(attention or [html.Div("Nenhuma etapa exige atenção imediata.", className="ugm-overview-empty")]),
        html.Div([
            html.Div("PRÓXIMO PRAZO", className="ugm-overview-eyebrow"),
            *([
                html.Strong((next_item.get("DEADLINE_TIME") or "")[:5]),
                html.P(next_item["NM_ACTIVITY"]), html.Small(_timing(next_item)),
                dcc.Link("Abrir " + next_item["NM_ROUTINE"] + " →", href=routine_href(next_item["ID_ROUTINE"], next_item.get("ID_ACTIVITY"))),
            ] if next_item else [html.P("Nenhum outro prazo pendente para hoje.")]),
        ], className="ugm-overview-next"),
    ]


def _unscheduled(routines):
    if not routines:
        return []
    return [
        html.Div([html.H2("Outras atividades"), html.P("Atividades sem agenda cadastrada ou sem etapas previstas para hoje.")],
                 className="ugm-overview-panel-heading"),
        html.Div([dcc.Link([
            html.Div(_category(item), className="ugm-overview-row-meta"),
            html.H4(item.get("NM_ACTIVITY") or item["NM_ROUTINE"]), html.P(item["NM_ROUTINE"]),
            _badge(item), html.Small(_progress_detail(item)),
            html.Span("Abrir rotina →", className="ugm-overview-attention-link"),
        ], href=routine_href(item["ID_ROUTINE"], item.get("ID_ACTIVITY")), className="ugm-overview-routine-card") for item in routines],
                 className="ugm-overview-unscheduled-grid"),
    ]


def _routine_menu(routines):
    groups = {}
    for routine in routines:
        group = routine["NM_GROUP"]
        groups.setdefault(group, []).append(routine)
    menu = []
    for group, entries in groups.items():
        menu.append(html.Div(group, className="routine-menu-group"))
        menu.extend(dcc.Link(entry["NM_ROUTINE"], href=routine_href(entry["ID_ROUTINE"]),
                             className="routine-menu-button") for entry in entries)
    return menu


def layout(**_query):
    try:
        service().ensure_schema()
    except Exception:
        # Leave the page mounted so polling can report and recover from an outage.
        logger.exception("Could not initialize the routine overview")
    return dbc.Container(fluid=True, className="page-container routines-page ugm-overview", children=[
        dcc.Interval(id="ugm-overview-poll", interval=15_000, n_intervals=0),
        html.Div(["Ative seu acesso em ", dcc.Link("UGM Access", href="/keys"), " para abrir UGM Routines."],
                 id="ugm-overview-access", className="routine-panel"),
        html.Div(id="ugm-overview-shell", className="routine-shell", style={"display": "none"}, children=[
            html.Aside([
                html.H4("UGM Routines"),
                dcc.Link("Panorama do dia", href="/routines",
                         className="routine-menu-button routine-menu-overview routine-menu-selected"),
                html.Div(id="ugm-overview-menu"),
            ], className="routine-sidebar"),
            html.Main(className="routine-main", children=[
                html.Header([
                    html.Div([
                        html.Div("UGM · AGENDA DA EQUIPE", className="ugm-overview-eyebrow"),
                        html.H1("Panorama do dia"),
                        html.P("O que a equipe precisa acompanhar hoje. Abra uma rotina para revisar e resolver."),
                    ]),
                    html.Div(id="ugm-overview-clock", className="ugm-overview-clock"),
                ], className="ugm-overview-header"),
                html.Div(id="ugm-overview-feedback", role="status"),
                html.Div(id="ugm-overview-summary", className="ugm-overview-summary"),
                html.Div(className="ugm-overview-body", children=[
                    html.Section(className="ugm-overview-panel", children=[
                        html.Div([html.H2("Agenda do dia"), html.Span("Horários de São Paulo", className="ugm-overview-note")],
                                 className="ugm-overview-panel-heading"),
                        html.Div([
                            html.Div(dcc.RadioItems(id="ugm-overview-filter", value="all", className="ugm-overview-filters", options=[
                                {"label": "Todas", "value": "all"}, {"label": "Atenção", "value": "attention"},
                                {"label": "Pendentes", "value": "pending"}, {"label": "Concluídas", "value": "completed"},
                            ]), role="group", **{"aria-label": "Filtrar etapas por situação"}),
                            html.Div([
                                html.Label("Frente da equipe", htmlFor="ugm-overview-category", className="visually-hidden"),
                                dcc.Dropdown(id="ugm-overview-category", value="all", clearable=False, searchable=False,
                                         options=[{"label": "Todas as frentes", "value": "all"}],
                                         className="ugm-overview-category"),
                            ]),
                        ], className="ugm-overview-toolbar"),
                        html.Div(id="ugm-overview-agenda"),
                    ]),
                    html.Aside(id="ugm-overview-attention", className="ugm-overview-panel",
                               **{"aria-label": "Atividades que precisam de atenção"}),
                ]),
                html.Section(id="ugm-overview-unscheduled", className="ugm-overview-other"),
                html.P("Atualização automática a cada 15 segundos · A conclusão é registrada na página de cada rotina.",
                       className="ugm-overview-note"),
            ]),
        ]),
    ])


@callback(Output("ugm-overview-shell", "style"), Output("ugm-overview-access", "style"), Input("current-user", "data"))
def overview_access(current_user):
    active = bool((current_user or {}).get("user"))
    return ({} if active else {"display": "none"}, {"display": "none"} if active else {})


@callback(
    Output("ugm-overview-clock", "children"), Output("ugm-overview-summary", "children"),
    Output("ugm-overview-agenda", "children"), Output("ugm-overview-attention", "children"),
    Output("ugm-overview-unscheduled", "children"), Output("ugm-overview-category", "options"),
    Output("ugm-overview-feedback", "children"),
    Output("ugm-overview-menu", "children"),
    Input("ugm-overview-poll", "n_intervals"), Input("current-user", "data"),
    Input("ugm-overview-filter", "value"), Input("ugm-overview-category", "value"),
)
def refresh_overview(_interval, current_user, view, category):
    empty = ([], [], [], [], [], [{"label": "Todas as frentes", "value": "all"}], [], [])
    if not (current_user or {}).get("user"):
        return empty
    try:
        engine = service()
        snapshot = engine.get_daily_overview()
        now = datetime.fromisoformat(snapshot["now"])
        all_items = [*snapshot["activities"], *snapshot["unscheduled_routines"]]
        categories = list(dict.fromkeys(item.get("category") or "Outras rotinas" for item in all_items))
        options = [{"label": "Todas as frentes", "value": "all"}] + [
            {"label": CATEGORY_LABELS.get(value, value), "value": value} for value in categories
        ]
        category = category if category in categories else "all"
        activities = [item for item in snapshot["activities"] if category == "all" or (item.get("category") or "Outras rotinas") == category]
        unscheduled = [item for item in snapshot["unscheduled_routines"] if category == "all" or (item.get("category") or "Outras rotinas") == category]
        clock = [html.Strong(f"{WEEKDAYS[now.weekday()]}, {now.day} de {MONTHS[now.month - 1]}"),
                 html.Span(f"Atualizado às {now:%H:%M:%S} · São Paulo")]
        return (clock, _summary(activities, unscheduled), _agenda(activities, view or "all"),
                _attention_panel(activities, unscheduled), _unscheduled(unscheduled), options, [],
                _routine_menu(engine.list_routines()))
    except Exception:
        logger.exception("Could not load the daily routine overview")
        return (*empty[:6], html.Div("Não foi possível atualizar o panorama. Nova tentativa em até 15 segundos.",
                                     className="ugm-overview-error", role="alert"), [])
