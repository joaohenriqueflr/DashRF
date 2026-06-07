from dash import Input, Output, State, callback, dcc, html, register_page
import dash_mantine_components as dmc
import dash_bootstrap_components as dbc
from dash_iconify import DashIconify


register_page(__name__, path="/fx-trading", name="FX Trading")


APPROVAL_ITEMS = [
    {
        "id": "APR-001",
        "account": "100234",
        "client": "Construtora Andrade S.A.",
        "description": "Importacao de Bens - R$ 2.500.000 - 2 docs",
        "status": "Pending",
        "action": "Review",
    },
    {
        "id": "APR-002",
        "account": "300112",
        "client": "Exportadora Sol Ltda.",
        "description": "Exportacao de Bens - R$ 840.000 - 3 docs",
        "note": '"Documentation complete. Trade cleared."',
        "status": "Approved",
    },
    {
        "id": "APR-003",
        "account": "201456",
        "client": "Pedro Henrique Figueiredo",
        "description": "Remessa para Exterior - R$ 150.000 - 1 doc",
        "note": '"Missing KYC document for beneficiary."',
        "status": "Rejected",
    },
    {
        "id": "APR-004",
        "account": "300789",
        "client": "Ana Beatriz Cavalcanti",
        "description": "Manutencao de Conta no Exterior - R$ 320.000 - 2 docs",
        "status": "Pending",
        "action": "Review",
    },
    {
        "id": "APR-005",
        "account": "100987",
        "client": "Fundo Delta Investments",
        "description": "Operacoes Financeiras - R$ 5.000.000 - 4 docs",
        "status": "Pending",
        "action": "Review",
    },
]


SETTLEMENT_ITEMS = [
    {
        "id": "TRD-0091",
        "banker": "Felipe Rocha",
        "cam": "Mariana Costa",
        "account": "100234",
        "trade_date": "2026-06-05",
        "settlement_date": "2026-06-07",
        "ccy": "USD",
        "foreign": "USD 450,000",
        "brl": "R$ 2.340.000",
        "contract": "OK",
        "funding": "Pending",
        "status": "Pending",
    },
    {
        "id": "TRD-0090",
        "banker": "Felipe Rocha",
        "cam": "Andre Lima",
        "account": "300112",
        "trade_date": "2026-06-05",
        "settlement_date": "2026-06-06",
        "ccy": "USD",
        "foreign": "USD 320,000",
        "brl": "R$ 1.664.000",
        "contract": "OK",
        "funding": "OK",
        "status": "Complete",
    },
    {
        "id": "TRD-0089",
        "banker": "Laura Mendes",
        "cam": "Mariana Costa",
        "account": "300789",
        "trade_date": "2026-06-04",
        "settlement_date": "2026-06-04",
        "ccy": "EUR",
        "foreign": "EUR 80,000",
        "brl": "R$ 464.000",
        "contract": "Pending",
        "funding": "Pending",
        "status": "Pending",
    },
    {
        "id": "TRD-0088",
        "banker": "Laura Mendes",
        "cam": "Ricardo Pinto",
        "account": "100987",
        "trade_date": "2026-06-04",
        "settlement_date": "2026-06-06",
        "ccy": "USD",
        "foreign": "USD 1,200,000",
        "brl": "R$ 6.240.000",
        "contract": "OK",
        "funding": "OK",
        "status": "Complete",
    },
    {
        "id": "TRD-0087",
        "banker": "Thiago Nunes",
        "cam": "Camila Torres",
        "account": "201456",
        "trade_date": "2026-06-03",
        "settlement_date": "2026-06-04",
        "ccy": "GBP",
        "foreign": "GBP 25,000",
        "brl": "R$ 161.250",
        "contract": "OK",
        "funding": "Pending",
        "status": "Pending",
    },
]


def status_badge(status):
    status_class = status.lower()
    icon = "ok" if status in ("Approved", "Complete", "OK") else "!"
    if status == "Rejected":
        icon = "x"
    return html.Span(
        className=f"fx-badge fx-badge-{status_class}",
        children=[html.Span(icon, className="fx-badge-icon"), status],
    )


def form_field(label, control, required=False, class_name=""):
    label_text = f"{label} *" if required else label
    return html.Div(
        className=f"fx-field {class_name}".strip(),
        children=[
            html.Label(label_text, className="fx-label"),
            control,
        ],
    )


def section_label(text):
    return html.Div(
        className="fx-section-divider",
        children=[
            html.Span(text, className="fx-section-divider-label"),
            html.Span(className="fx-section-divider-line"),
        ],
    )


def approval_queue_item(item):
    meta = html.Div(
        className="fx-approval-meta",
        children=[
            html.Span(item["id"], className="fx-mono fx-muted"),
            html.Span("/", className="fx-dot"),
            html.Span(f"Acct {item['account']}", className="fx-muted"),
        ],
    )
    return html.Div(
        className="fx-approval-row",
        children=[
            html.Div(
                className="fx-approval-copy",
                children=[
                    meta,
                    html.Div(item["client"], className="fx-approval-client"),
                    html.Div(item["description"], className="fx-approval-description"),
                    html.Div(item.get("note", ""), className="fx-approval-note"),
                ],
            ),
            html.Div(
                className="fx-approval-actions",
                children=[
                    status_badge(item["status"]),
                    html.Button(
                        item["action"],
                        className="fx-link-button",
                        type="button",
                    )
                    if item.get("action")
                    else None,
                ],
            ),
        ],
    )


def approval_step_content():
    return html.Div(
        className="fx-approval-layout",
        children=[
            html.Div(
                className="fx-card fx-submit-card",
                children=[
                    html.Div("Submit Trade for Approval", className="fx-card-title"),
                    form_field(
                        "Account Number",
                        dcc.Input(
                            className="fx-input",
                            placeholder="e.g. 100234",
                            type="text",
                            id="fx-approval-account",
                        ),
                        required=True,
                    ),
                    form_field(
                        "Notional Value",
                        dcc.Input(
                            className="fx-input",
                            placeholder="BRL amount",
                            type="text",
                            id="fx-approval-notional",
                        ),
                        required=True,
                    ),
                    form_field(
                        "Nature",
                        dcc.Dropdown(
                            className="fx-select",
                            options=[
                                {
                                    "label": "Importacao de Bens",
                                    "value": "importacao",
                                },
                                {
                                    "label": "Exportacao de Bens",
                                    "value": "exportacao",
                                },
                                {
                                    "label": "Remessa para Exterior",
                                    "value": "remessa",
                                },
                                {
                                    "label": "Operacoes Financeiras",
                                    "value": "operacoes",
                                },
                            ],
                            placeholder="Select nature...",
                            clearable=False,
                            id="fx-approval-nature",
                        ),
                        required=True,
                    ),
                    form_field(
                        "Trade Description",
                        dcc.Textarea(
                            className="fx-input fx-textarea",
                            placeholder=(
                                "Brief description of the trade purpose and context..."
                            ),
                            id="fx-approval-description",
                        ),
                    ),
                    html.Div(
                        className="fx-field",
                        children=[
                            html.Label("Documents", className="fx-label"),
                            dcc.Upload(
                                id="fx-document-upload",
                                className="fx-upload",
                                children=[
                                    html.Span("^", className="fx-upload-icon"),
                                    html.Span("Click to upload or drag & drop"),
                                ],
                                multiple=True,
                            ),
                        ],
                    ),
                    html.Button(
                        children=[html.Span("+", className="fx-button-icon"), "Submit for Approval"],
                        className="fx-primary-button",
                        id="fx-submit-approval",
                        type="button",
                    ),
                ],
            ),
            html.Div(
                className="fx-card fx-queue-card",
                children=[
                    html.Div(
                        className="fx-card-header",
                        children=[
                            html.Div("Approval Queue", className="fx-card-title"),
                            html.Div("3 pending", className="fx-card-count"),
                        ],
                    ),
                    html.Div(
                        className="fx-approval-list",
                        children=[approval_queue_item(item) for item in APPROVAL_ITEMS],
                    ),
                ],
            ),
        ],
    )


def execution_step_content():
    return html.Div(
        className="fx-card fx-execution-card",
        children=[
            section_label("Client / Segmentation"),
            html.Div(
                className="fx-client-grid",
                children=[
                    form_field(
                        "Account",
                        dcc.Input(
                            className="fx-input",
                            placeholder="Account #",
                            type="text",
                            id="fx-execution-account",
                        ),
                        required=True,
                    ),
                    form_field(
                        "Client",
                        dcc.Input(
                            className="fx-input",
                            placeholder="Auto-filled from account",
                            type="text",
                            disabled=True,
                            id="fx-execution-client",
                        ),
                    ),
                    form_field(
                        "Banker",
                        dcc.Input(
                            className="fx-input",
                            placeholder="Auto-filled",
                            type="text",
                            disabled=True,
                            id="fx-execution-banker",
                        ),
                    ),
                    form_field(
                        "Segment",
                        dcc.Input(
                            className="fx-input",
                            placeholder="Auto-filled",
                            type="text",
                            disabled=True,
                            id="fx-execution-segment",
                        ),
                    ),
                    html.Div(
                        className="fx-field fx-direction-field",
                        children=[
                            html.Label("Direction *", className="fx-label"),
                            dmc.SegmentedControl(
                                data=[
                                    {"value": "Buy", "label": "Buy"},
                                    {"value": "Sell", "label": "Sell"},
                                ],
                                value="Buy",
                                fullWidth=True,
                                className="fx-segment fx-direction-segment",
                                id="fx-direction",
                            ),
                        ],
                    ),
                ],
            ),
            section_label("Trade Economics"),
            html.Div(
                className="fx-economics-grid",
                children=[
                    form_field(
                        "Nature",
                        dcc.Dropdown(
                            className="fx-select",
                            options=[
                                {
                                    "label": "Importacao de Bens",
                                    "value": "importacao",
                                },
                                {
                                    "label": "Exportacao de Bens",
                                    "value": "exportacao",
                                },
                                {
                                    "label": "Remessa para Exterior",
                                    "value": "remessa",
                                },
                                {
                                    "label": "Manutencao de Conta",
                                    "value": "manutencao",
                                },
                            ],
                            placeholder="Select nature...",
                            clearable=False,
                            id="fx-execution-nature",
                        ),
                        required=True,
                        class_name="fx-nature-field",
                    ),
                    form_field(
                        "Destination",
                        dcc.Dropdown(
                            className="fx-select",
                            options=[
                                {"label": "UBS", "value": "UBS"},
                                {"label": "United States", "value": "US"},
                                {"label": "Europe", "value": "EU"},
                                {"label": "United Kingdom", "value": "UK"},
                            ],
                            value="UBS",
                            clearable=False,
                            id="fx-destination",
                        ),
                        required=True,
                    ),
                    form_field(
                        "Settlement",
                        dmc.SegmentedControl(
                            data=[
                                {"value": "D0", "label": "D0"},
                                {"value": "D1", "label": "D1"},
                                {"value": "D2", "label": "D2"},
                            ],
                            value="D2",
                            fullWidth=True,
                            className="fx-segment",
                            id="fx-settlement",
                        ),
                        required=True,
                    ),
                    form_field(
                        "FX Rate",
                        dcc.Input(
                            className="fx-input",
                            placeholder="e.g. 5.2000",
                            type="text",
                            id="fx-rate",
                        ),
                    ),
                    form_field(
                        "IOF (%)",
                        dcc.Input(
                            className="fx-input",
                            value="0.38",
                            type="text",
                            id="fx-iof",
                        ),
                    ),
                ],
            ),
            html.Div(
                className="fx-notional-label",
                children="Notional - Client pays BRL, receives USD *",
            ),
            html.Div(
                className="fx-notional-row",
                children=[
                    html.Div(
                        className="fx-currency-input",
                        children=[
                            html.Span("BRL", className="fx-currency-code"),
                            html.Span(className="fx-currency-divider"),
                            dcc.Input(
                                className="fx-currency-field",
                                placeholder="Amount",
                                type="text",
                                id="fx-brl-notional",
                            ),
                        ],
                    ),
                    html.Div(className="fx-notional-arrow", children="->"),
                    html.Div(
                        className="fx-currency-input fx-currency-input-active",
                        children=[
                            html.Span("USD", className="fx-currency-code"),
                            html.Span(className="fx-currency-divider"),
                            dcc.Input(
                                className="fx-currency-field",
                                placeholder="Amount",
                                type="text",
                                id="fx-foreign-notional",
                            ),
                        ],
                    ),
                ],
            ),
            html.Div(
                className="fx-foreign-row",
                children=[
                    html.Span("Foreign currency:", className="fx-foreign-label"),
                    dcc.Dropdown(
                        className="fx-mini-select",
                        options=[
                            {"label": "USD", "value": "USD"},
                            {"label": "EUR", "value": "EUR"},
                            {"label": "GBP", "value": "GBP"},
                        ],
                        value="USD",
                        clearable=False,
                        id="fx-foreign-currency",
                    ),
                ],
            ),
            form_field(
                "Observation",
                dcc.Input(
                    className="fx-input",
                    placeholder="Optional notes",
                    type="text",
                    id="fx-observation",
                ),
            ),
            section_label("Summary"),
            html.Div(
                className="fx-summary-box",
                children="Fill in account and notional values to generate summary",
            ),
            html.Div(
                className="fx-execution-actions",
                children=[
                    html.Button(
                        "Execute Trade",
                        className="fx-primary-button fx-action-button",
                        id="fx-execute-trade",
                        type="button",
                    ),
                    html.Button("Clear", className="fx-secondary-button", type="button"),
                ],
            ),
        ],
    )


def settlement_row(item):
    row_class = "fx-settlement-row"
    if item["status"] == "Complete":
        row_class += " fx-settlement-row-complete"
    return html.Tr(
        className=row_class,
        children=[
            html.Td(item["id"], className="fx-mono"),
            html.Td(item["banker"]),
            html.Td(item["cam"]),
            html.Td(item["account"]),
            html.Td(item["trade_date"]),
            html.Td(item["settlement_date"]),
            html.Td(item["ccy"]),
            html.Td(item["foreign"], className="fx-align-right"),
            html.Td(item["brl"], className="fx-align-right"),
            html.Td(status_badge(item["contract"])),
            html.Td(status_badge(item["funding"])),
            html.Td(status_badge(item["status"])),
        ],
    )


def settlement_step_content():
    columns = [
        "ID",
        "Banker",
        "CAM",
        "Account",
        "Trade Date",
        "Settlement Date",
        "CCY",
        "Notional Foreign",
        "Notional BRL",
        "FX Contract",
        "Trade Funding",
        "Status",
    ]
    return html.Div(
        className="fx-card fx-settlement-card",
        children=[
            html.Div(
                className="fx-card-header",
                children=[
                    html.Div("Settlement Queue", className="fx-card-title"),
                    html.Div(
                        className="fx-card-count",
                        children=[
                            html.Span("3 pending settlement"),
                            html.Span("/", className="fx-dot"),
                            html.Span("2 complete"),
                        ],
                    ),
                ],
            ),
            html.Div(
                className="fx-table-wrap",
                children=html.Table(
                    className="fx-settlement-table",
                    children=[
                        html.Thead(
                            html.Tr([html.Th(column) for column in columns])
                        ),
                        html.Tbody([settlement_row(item) for item in SETTLEMENT_ITEMS]),
                    ],
                ),
            ),
        ],
    )


def stepper(active_step=0):
    return dmc.Stepper(
        id="fx-stepper",
        active=active_step,
        color="blue",
        radius="xl",
        iconSize=48,
        size="sm",
        allowNextStepsSelect=True,
        className="fx-stepper",
        children=[
            dmc.StepperStep(
                label="Step 1 - Trade Approval",
                description="GWM CAMs submit trade documentation for GT MO review",
                icon=DashIconify(
                    icon="lucide:upload",
                    width=26,
                    className="fx-stepper-icon",
                ),
                completedIcon=DashIconify(
                    icon="lucide:check",
                    width=26,
                    className="fx-stepper-icon",
                ),
                allowStepSelect=True,
            ),
            dmc.StepperStep(
                label="Step 2 - Trade Execution",
                description="Record executed FX trades against client accounts",
                icon=DashIconify(
                    icon="mdi:currency-usd",
                    width=26,
                    className="fx-stepper-icon",
                ),
                completedIcon=DashIconify(
                    icon="lucide:check",
                    width=26,
                    className="fx-stepper-icon",
                ),
                allowStepSelect=True,
            ),
            dmc.StepperStep(
                label="Step 3 - Settlement",
                description="Monitor FX contract and funding confirmation status",
                icon=DashIconify(
                    icon="lucide:circle-check",
                    width=26,
                    className="fx-stepper-icon",
                ),
                completedIcon=DashIconify(
                    icon="lucide:check",
                    width=26,
                    className="fx-stepper-icon",
                ),
                allowStepSelect=True,
            ),
        ],
    )


layout = dbc.Container(
    fluid=True,
    className="page-container fx-page",
    children=[
        html.Div(
            className="fx-page-header",
            children=[
                html.H3("FX Trading Activity", className="fx-page-title"),
                html.Div(
                    "Manage the full FX trade lifecycle from approval through settlement",
                    className="fx-page-subtitle",
                ),
            ],
        ),
        html.Div(className="fx-stepper-card", children=stepper()),
        html.Div(id="fx-step-content", children=approval_step_content()),
    ],
)


@callback(
    Output("fx-step-content", "children"),
    Input("fx-stepper", "active"),
)
def render_step_content(active_step):
    if active_step == 1:
        return execution_step_content()
    if active_step == 2:
        return settlement_step_content()
    return approval_step_content()


@callback(
    Output("fx-stepper", "active"),
    Input("fx-submit-approval", "n_clicks"),
    Input("fx-execute-trade", "n_clicks"),
    State("fx-stepper", "active"),
    prevent_initial_call=True,
)
def advance_step(_approval_clicks, _execution_clicks, active_step):
    if active_step == 0:
        return 1
    if active_step == 1:
        return 2
    return active_step
