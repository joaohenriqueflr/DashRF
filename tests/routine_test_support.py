"""Deliberate minimal routine configuration for focused service/UI fixtures."""


def single_ntnb_activity(engine):
    engine.ensure_schema()
    with engine.connection(immediate=True) as conn:
        conn.execute("DELETE FROM UGM_ROUTINE_ACTIVITY_TOOLS WHERE ID_ACTIVITY IN(SELECT ID_ACTIVITY FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ROUTINE='NTNB_RUN')")
        conn.execute("DELETE FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ROUTINE='NTNB_RUN'")
    engine.configure_activity('NTNB_RUN','NTNB_RUN:DEFAULT','Importar run',prompt_key='NTNB_RUN',tools=['save_quotes'],
        config=dict(workflow='quote_import',sources=['XP'],instrument_types=['NTN-B'],completion_action='save_quotes'))
