"""Five routine tables: metadata, activities, tool permissions, executions, chat.

The schedule is part of an activity. Completion is a property of an execution.
Only genuinely variable collections/payloads use JSON; common settings are columns.
"""

import json
import sqlite3

from services import routine_tools

TABLES = {
    "UGM_ROUTINES": """(
        ID_ROUTINE TEXT PRIMARY KEY,
        NM_ROUTINE TEXT NOT NULL,
        DS_ROUTINE TEXT NOT NULL DEFAULT '',
        NM_GROUP TEXT NOT NULL DEFAULT 'Operations',
        LAYOUT_KEY TEXT NOT NULL,
        FL_ACTIVE INTEGER NOT NULL DEFAULT 1 CHECK(FL_ACTIVE IN (0,1)),
        DISPLAY_ORDER INTEGER NOT NULL DEFAULT 0)""",
    "UGM_ROUTINE_ACTIVITIES": """(
        ID_ACTIVITY TEXT PRIMARY KEY,
        ID_ROUTINE TEXT NOT NULL REFERENCES UGM_ROUTINES(ID_ROUTINE),
        NM_ACTIVITY TEXT NOT NULL,
        DS_ACTIVITY TEXT NOT NULL DEFAULT '',
        PROMPT_FEATURE TEXT NOT NULL DEFAULT 'routines',
        PROMPT_KEY TEXT NOT NULL,
        WORKFLOW TEXT NOT NULL DEFAULT 'assistant' CHECK(WORKFLOW IN ('assistant','quote_import')),
        QUOTE_MODE TEXT NOT NULL DEFAULT 'RATE' CHECK(QUOTE_MODE IN ('RATE','DI_SPREAD')),
        SOURCES_JSON TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(SOURCES_JSON) AND json_type(SOURCES_JSON)='array'),
        INSTRUMENT_TYPES_JSON TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(INSTRUMENT_TYPES_JSON) AND json_type(INSTRUMENT_TYPES_JSON)='array'),
        EXCLUDED_INSTRUMENT_TYPES_JSON TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(EXCLUDED_INSTRUMENT_TYPES_JSON) AND json_type(EXCLUDED_INSTRUMENT_TYPES_JSON)='array'),
        DEADLINE_TIME TEXT CHECK(DEADLINE_TIME IS NULL OR
            (length(DEADLINE_TIME)=5 AND DEADLINE_TIME GLOB '[0-2][0-9]:[0-5][0-9]' AND substr(DEADLINE_TIME,1,2)<'24')),
        RECURRENCE TEXT NOT NULL DEFAULT 'MANUAL' CHECK(RECURRENCE IN ('DAILY','WEEKDAYS','MANUAL')),
        COMPLETION_ACTION TEXT,
        OPTIONS_JSON TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(OPTIONS_JSON) AND json_type(OPTIONS_JSON)='object'),
        FL_ACTIVE INTEGER NOT NULL DEFAULT 1 CHECK(FL_ACTIVE IN (0,1)),
        DISPLAY_ORDER INTEGER NOT NULL DEFAULT 0,
        UNIQUE(ID_ROUTINE,ID_ACTIVITY))""",
    "UGM_ROUTINE_ACTIVITY_TOOLS": """(
        ID_ACTIVITY TEXT NOT NULL REFERENCES UGM_ROUTINE_ACTIVITIES(ID_ACTIVITY),
        NM_TOOL TEXT NOT NULL,
        REVIEW_POLICY TEXT NOT NULL CHECK(REVIEW_POLICY IN ('AUTO','REVIEW','CONFIRM')),
        FL_ACTIVE INTEGER NOT NULL DEFAULT 1 CHECK(FL_ACTIVE IN (0,1)),
        CONFIG_JSON TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(CONFIG_JSON) AND json_type(CONFIG_JSON)='object'),
        PRIMARY KEY(ID_ACTIVITY,NM_TOOL))""",
    "UGM_ROUTINE_EXECUTIONS": """(
        ID_EXECUTION INTEGER PRIMARY KEY AUTOINCREMENT,
        ID_ROUTINE TEXT NOT NULL,
        ID_ACTIVITY TEXT NOT NULL,
        DT_REF TEXT NOT NULL,
        USERNAME TEXT NOT NULL,
        NM_ACTION TEXT NOT NULL,
        FL_COMPLETES_ACTIVITY INTEGER NOT NULL DEFAULT 0 CHECK(FL_COMPLETES_ACTIVITY IN (0,1)),
        TS_REQUEST TEXT NOT NULL,
        TS_EXECUTION TEXT,
        STATUS TEXT NOT NULL CHECK(STATUS IN ('PENDING_REVIEW','EXECUTING','SUCCESS','FAILED','CANCELLED')),
        INPUT_JSON TEXT NOT NULL,
        RESULT_JSON TEXT,
        ERROR_MESSAGE TEXT,
        FOREIGN KEY(ID_ROUTINE,ID_ACTIVITY) REFERENCES UGM_ROUTINE_ACTIVITIES(ID_ROUTINE,ID_ACTIVITY))""",
    "UGM_ROUTINE_CHAT": """(
        ID_MESSAGE INTEGER PRIMARY KEY AUTOINCREMENT,
        ID_ROUTINE TEXT NOT NULL,
        ID_ACTIVITY TEXT NOT NULL,
        USERNAME TEXT NOT NULL,
        DT_REF TEXT NOT NULL,
        TS_MESSAGE TEXT NOT NULL,
        TS_RESPONSE TEXT,
        USER_MESSAGE TEXT NOT NULL DEFAULT '',
        ASSISTANT_MESSAGE TEXT NOT NULL DEFAULT '',
        STATUS TEXT NOT NULL DEFAULT 'COMPLETED',
        ID_EXECUTION INTEGER REFERENCES UGM_ROUTINE_EXECUTIONS(ID_EXECUTION) ON DELETE SET NULL,
        FOREIGN KEY(ID_ROUTINE,ID_ACTIVITY) REFERENCES UGM_ROUTINE_ACTIVITIES(ID_ROUTINE,ID_ACTIVITY))""",
}
OBSOLETE_TABLES = ("UGM_ROUTINE_SCHEDULE", "UGM_ROUTINE_ACTIVITY_COMPLETIONS", "UGM_ROUTINE_TOOLS")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def activity_values(config):
    """Translate configuration to visible columns; extensions stay in OPTIONS_JSON."""
    config = dict(config)
    result = {}
    for key, column, default in (
        ('prompt_feature', 'PROMPT_FEATURE', 'routines'), ('workflow', 'WORKFLOW', 'assistant'),
        ('quote_mode', 'QUOTE_MODE', 'RATE'), ('sources', 'SOURCES_JSON', []),
        ('instrument_types', 'INSTRUMENT_TYPES_JSON', []),
        ('excluded_instrument_types', 'EXCLUDED_INSTRUMENT_TYPES_JSON', []),
    ):
        value = config.pop(key, default)
        result[column] = json_text(value) if column.endswith('_JSON') else value
    config.pop('group', None)
    result['COMPLETION_ACTION'] = config.pop('completion_action', None)
    result['OPTIONS_JSON'] = json_text(config)
    return result


def activity_config(row):
    """Build the existing handler context from the activity's explicit settings."""
    config = json.loads(row['OPTIONS_JSON']) if isinstance(row['OPTIONS_JSON'], str) else dict(row['OPTIONS_JSON'])
    for key, column in (
        ('prompt_feature', 'PROMPT_FEATURE'), ('workflow', 'WORKFLOW'), ('quote_mode', 'QUOTE_MODE'),
        ('sources', 'SOURCES_JSON'), ('instrument_types', 'INSTRUMENT_TYPES_JSON'),
        ('excluded_instrument_types', 'EXCLUDED_INSTRUMENT_TYPES_JSON'), ('completion_action', 'COMPLETION_ACTION'),
    ):
        value = row[column]
        config[key] = json.loads(value) if column.endswith('_JSON') and isinstance(value, str) else value
    return config


def _insert(conn, table, values):
    fields = ','.join(values)
    conn.execute(f"INSERT INTO {table}({fields}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))


def _indexes(conn):
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_daily_completion
        ON UGM_ROUTINE_EXECUTIONS(ID_ACTIVITY,DT_REF)
        WHERE STATUS='SUCCESS' AND FL_COMPLETES_ACTIVITY=1""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_executions_day ON UGM_ROUTINE_EXECUTIONS(ID_ACTIVITY,DT_REF,ID_EXECUTION)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routine_executions_day ON UGM_ROUTINE_EXECUTIONS(ID_ROUTINE,DT_REF)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_chat_day ON UGM_ROUTINE_CHAT(ID_ACTIVITY,USERNAME,DT_REF,ID_MESSAGE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routine_chat_day ON UGM_ROUTINE_CHAT(ID_ROUTINE,USERNAME,DT_REF,ID_MESSAGE)")


def _seed(conn):
    routines = [
        ('NTNB_RUN','Run NTN-B','Importação e revisão das runs XP de NTN-B.','Market Data','market_run'),
        ('NTNF_RUN','Run NTN-F','Spreads XP e composição determinística com a curva DI.','Market Data','market_run'),
        ('CREDIT_RUN','Run Crédito Privado','Runs Santander, Ativa, BGC e xBridge.','Market Data','market_run'),
        ('DEBTORS','Devedores','Acompanhamento compartilhado e fechamento diário de devedores.','Operations','debtors'),
    ]
    for order, (routine, name, description, group, layout) in enumerate(routines):
        _insert(conn, 'UGM_ROUTINES', dict(ID_ROUTINE=routine,NM_ROUTINE=name,DS_ROUTINE=description,
            NM_GROUP=group,LAYOUT_KEY=layout,DISPLAY_ORDER=order))
        config = {
            'NTNB_RUN': dict(workflow='quote_import',sources=['XP'],instrument_types=['NTN-B']),
            'NTNF_RUN': dict(workflow='quote_import',sources=['XP'],instrument_types=['NTN-F'],quote_mode='DI_SPREAD'),
            'CREDIT_RUN': dict(sources=['Santander','Ativa','BGC','xBridge'],excluded_instrument_types=['NTN-B','NTN-F','LTN','LFT','NTN-C']),
            'DEBTORS': {},
        }[routine]
        slots = [('NTNB_RUN_1030','Run NTN-B — 1ª importação','10:30'),
                 ('NTNB_RUN_1230','Run NTN-B — 2ª importação','12:30'),
                 ('NTNB_RUN_1530','Run NTN-B — 3ª importação','15:30')] if routine=='NTNB_RUN' else [
                 (routine+':DEFAULT','Acompanhar devedores' if routine=='DEBTORS' else 'Importar run',None)]
        for slot_order, (activity, activity_name, deadline) in enumerate(slots):
            values = dict(ID_ACTIVITY=activity,ID_ROUTINE=routine,NM_ACTIVITY=activity_name,
                DS_ACTIVITY=description,PROMPT_KEY=routine,DISPLAY_ORDER=slot_order,**activity_values(config))
            values.update(DEADLINE_TIME=deadline,RECURRENCE='DAILY' if deadline else 'MANUAL',
                          COMPLETION_ACTION='save_quotes' if layout=='market_run' else None)
            _insert(conn, 'UGM_ROUTINE_ACTIVITIES', values)
            tools = ['save_quotes'] if config.get('workflow')=='quote_import' else routine_tools.default_tool_names(routine)
            for tool in tools:
                _insert(conn, 'UGM_ROUTINE_ACTIVITY_TOOLS', dict(ID_ACTIVITY=activity,NM_TOOL=tool,
                    REVIEW_POLICY='CONFIRM' if routine_tools.get_tool(tool).write else 'AUTO'))


def _migrate_current_schema(conn):
    """One cutover from the project's previous schema; no schedule history retained."""
    old = {name: [dict(row) for row in conn.execute(f'SELECT * FROM {name}')]
           for name in (*TABLES, *OBSOLETE_TABLES)}
    sequences = dict(conn.execute('SELECT name,seq FROM sqlite_sequence'))
    for name, ddl in TABLES.items():
        conn.execute(f'CREATE TABLE NEW_{name} {ddl}')
    routines = {row['ID_ROUTINE']: row for row in old['UGM_ROUTINES']}
    configs = {}
    for row in routines.values():
        config = json.loads(row['CONFIG_JSON'])
        _insert(conn, 'NEW_UGM_ROUTINES', {key: row[key] for key in ('ID_ROUTINE','NM_ROUTINE','DS_ROUTINE','LAYOUT_KEY','FL_ACTIVE','DISPLAY_ORDER')} |
                dict(NM_GROUP=config.get('group','Operations')))
    for row in old['UGM_ROUTINE_ACTIVITIES']:
        schedules = [s for s in old['UGM_ROUTINE_SCHEDULE'] if s['ID_ACTIVITY']==row['ID_ACTIVITY'] and s['FL_ACTIVE']]
        if len(schedules)>1:
            raise ValueError('Each activity must have one schedule before this schema cutover.')
        config = json.loads(routines[row['ID_ROUTINE']]['CONFIG_JSON']) | json.loads(row['CONFIG_JSON'])
        if 'workflow' not in config and row['ID_ROUTINE'] in ('NTNB_RUN','NTNF_RUN'):
            config['workflow']='quote_import'
        values = {key: row[key] for key in ('ID_ACTIVITY','ID_ROUTINE','NM_ACTIVITY','DS_ACTIVITY','PROMPT_KEY','FL_ACTIVE','DISPLAY_ORDER')}
        values.update(activity_values(config))
        values.update(DEADLINE_TIME=None,RECURRENCE='MANUAL')
        if schedules:
            schedule=schedules[0]
            values.update(DEADLINE_TIME=schedule['DEADLINE_TIME'],RECURRENCE=schedule['RECURRENCE'],COMPLETION_ACTION=schedule['COMPLETION_ACTION'])
        configs[row['ID_ACTIVITY']] = values
        _insert(conn, 'NEW_UGM_ROUTINE_ACTIVITIES', values)
    tool_names = {}
    parent_tools = {(row['ID_ROUTINE'],row['NM_TOOL']):row for row in old['UGM_ROUTINE_TOOLS']}
    for link in old['UGM_ROUTINE_ACTIVITY_TOOLS']:
        tool = parent_tools[(link['ID_ROUTINE'],link['NM_TOOL'])]
        name = tool['HANDLER_KEY']
        routine_tools.get_tool(name)
        tool_names[(link['ID_ROUTINE'],tool['NM_TOOL'])] = name
        if not tool['FL_ACTIVE']:
            continue
        config = json.loads(tool['CONFIG_JSON'])
        sources=json.loads(configs[link['ID_ACTIVITY']]['SOURCES_JSON'])
        if config.get('allowed_sources')==sources:
            config.pop('allowed_sources')
        _insert(conn, 'NEW_UGM_ROUTINE_ACTIVITY_TOOLS', dict(ID_ACTIVITY=link['ID_ACTIVITY'],NM_TOOL=name,
            REVIEW_POLICY=tool['REVIEW_POLICY'],FL_ACTIVE=tool['FL_ACTIVE'],CONFIG_JSON=json_text(config)))
    completion_ids={row['ID_EXECUTION'] for row in old['UGM_ROUTINE_ACTIVITY_COMPLETIONS']}
    for activity in configs.values():
        action=activity['COMPLETION_ACTION']
        canonical=tool_names.get((activity['ID_ROUTINE'],action),action)
        activity['COMPLETION_ACTION']=canonical
        conn.execute('UPDATE NEW_UGM_ROUTINE_ACTIVITIES SET COMPLETION_ACTION=? WHERE ID_ACTIVITY=?',
                     (canonical,activity['ID_ACTIVITY']))
    claimed={(row['ID_ACTIVITY'],row['DT_REF']) for row in old['UGM_ROUTINE_ACTIVITY_COMPLETIONS']}
    for row in sorted(old['UGM_ROUTINE_EXECUTIONS'],key=lambda row:(row['TS_EXECUTION'] or row['TS_REQUEST'],row['ID_EXECUTION'])):
        row=dict(row)
        row.pop('ID_STEP',None)
        action=tool_names.get((row['ID_ROUTINE'],row['NM_ACTION']),row['NM_ACTION'])
        row['NM_ACTION']=action
        spec=routine_tools.TOOLS.get(action)
        completes=action=='manual_complete' or bool(spec and spec.write) or configs[row['ID_ACTIVITY']]['COMPLETION_ACTION']==action
        if row['STATUS']=='SUCCESS':
            key=(row['ID_ACTIVITY'],row['DT_REF'])
            completes=row['ID_EXECUTION'] in completion_ids or (completes and key not in claimed)
            if completes:
                claimed.add(key)
        row['FL_COMPLETES_ACTIVITY']=int(completes)
        _insert(conn,'NEW_UGM_ROUTINE_EXECUTIONS',row)
    for row in old['UGM_ROUTINE_CHAT']:
        _insert(conn,'NEW_UGM_ROUTINE_CHAT',row)
    for name in ('UGM_ROUTINE_CHAT','UGM_ROUTINE_ACTIVITY_COMPLETIONS','UGM_ROUTINE_EXECUTIONS',
                 'UGM_ROUTINE_ACTIVITY_TOOLS','UGM_ROUTINE_SCHEDULE','UGM_ROUTINE_TOOLS','UGM_ROUTINE_ACTIVITIES','UGM_ROUTINES'):
        conn.execute(f'DROP TABLE {name}')
    for name in TABLES:
        conn.execute(f'ALTER TABLE NEW_{name} RENAME TO {name}')
        if name in sequences:
            conn.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?',(sequences[name],name))


def ensure_routine_schema(database_path):
    conn=sqlite3.connect(database_path,timeout=30)
    conn.row_factory=sqlite3.Row
    try:
        # FK enforcement is restored for runtime connections. Rebuilding related
        # tables happens in one transaction and is checked before committing.
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('PRAGMA legacy_alter_table=ON')
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            columns={row[1] for row in conn.execute('PRAGMA table_info(UGM_ROUTINES)')}
            if 'CONFIG_JSON' in columns:
                _migrate_current_schema(conn)
            for name,ddl in TABLES.items():
                conn.execute(f'CREATE TABLE IF NOT EXISTS {name} {ddl}')
            _indexes(conn)
            if not conn.execute('SELECT 1 FROM UGM_ROUTINES LIMIT 1').fetchone():
                _seed(conn)
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Routine schema contains an invalid foreign key.')
    finally:
        conn.close()
