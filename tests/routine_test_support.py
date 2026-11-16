"""Deliberate minimal routine configuration for focused service/UI fixtures."""


def single_ntnb_activity(engine):
    engine.ensure_schema()
    with engine.connection(immediate=True) as conn:
        # Core-engine fixtures deliberately retain the original four-routine
        # contracts; catalogue coverage uses the full production seed separately.
        from services.routine_catalog import IMPORT_ACTIVITIES
        for rows in IMPORT_ACTIVITIES.values():
            for activity_id, *_rest in rows:
                conn.execute('DELETE FROM UGM_ROUTINE_ACTIVITY_TOOLS WHERE ID_ACTIVITY=?', (activity_id,))
                conn.execute('DELETE FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ACTIVITY=?', (activity_id,))
        conn.execute("DELETE FROM UGM_ROUTINES WHERE ID_ROUTINE='BANK_RUN'")
        conn.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET FL_ACTIVE=1 WHERE ID_ACTIVITY IN ('CREDIT_RUN:DEFAULT','NTNF_RUN:DEFAULT')")
        conn.execute("DELETE FROM UGM_ROUTINE_ACTIVITY_TOOLS WHERE ID_ACTIVITY IN(SELECT ID_ACTIVITY FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ROUTINE='NTNB_RUN')")
        conn.execute("DELETE FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ROUTINE='NTNB_RUN'")
    engine.configure_activity('NTNB_RUN','NTNB_RUN:DEFAULT','Importar run',prompt_key='NTNB_RUN',tools=['save_quotes'],
        config=dict(workflow='quote_import',sources=['XP'],instrument_types=['NTN-B'],completion_action='save_quotes'))
