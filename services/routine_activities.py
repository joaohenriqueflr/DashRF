"""Activity settings and permissions, without separate schedule/completion rows."""

from datetime import time
import json

from services import routine_tools
from services.routine_schema import activity_config, activity_values


def activity_filter(alias=""):
    return f"{alias + '.' if alias else ''}ID_ACTIVITY=?"


class RoutineActivities:
    @staticmethod
    def _decode_activity(row):
        result = dict(row)
        for name in ('SOURCES_JSON','INSTRUMENT_TYPES_JSON','EXCLUDED_INSTRUMENT_TYPES_JSON','OPTIONS_JSON'):
            result[name] = json.loads(result[name])
        return result

    def list_activities(self, routine_id):
        with self.connection() as conn:
            self._routine(conn, routine_id)
            return [self._decode_activity(row) for row in conn.execute("""SELECT * FROM UGM_ROUTINE_ACTIVITIES
                WHERE ID_ROUTINE=? AND FL_ACTIVE=1 ORDER BY DISPLAY_ORDER,ID_ACTIVITY""", (routine_id,))]

    def _activity_scope(self, conn, routine_id, activity_id=None):
        routine = self._routine(conn, routine_id)
        query = 'SELECT * FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ROUTINE=? AND FL_ACTIVE=1'
        params = [routine_id]
        if activity_id is not None:
            query += ' AND ID_ACTIVITY=?'
            params.append(activity_id)
        rows = conn.execute(query, params).fetchall()
        if len(rows)!=1:
            message = 'Selecione uma atividade desta rotina antes de continuar.' if activity_id is None else 'A atividade não está disponível nesta rotina.'
            raise ValueError(message)
        activity = self._decode_activity(rows[0])
        return {**routine, **activity, 'CONFIG_JSON': activity_config(activity)}

    def load_activity(self, routine_id, activity_id):
        with self.connection() as conn:
            return self._activity_scope(conn, routine_id, activity_id)

    def get_activity_completion(self, routine_id, activity_id, dt_ref=None):
        with self.connection() as conn:
            self._activity_scope(conn, routine_id, activity_id)
            row = conn.execute("""SELECT * FROM UGM_ROUTINE_EXECUTIONS WHERE ID_ACTIVITY=? AND DT_REF=?
                AND STATUS='SUCCESS' AND FL_COMPLETES_ACTIVITY=1""", (activity_id, dt_ref or self.today())).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _assert_activity_open(conn, activity_id, dt_ref):
        if conn.execute("""SELECT 1 FROM UGM_ROUTINE_EXECUTIONS WHERE ID_ACTIVITY=? AND DT_REF=?
                AND STATUS='SUCCESS' AND FL_COMPLETES_ACTIVITY=1""", (activity_id, dt_ref)).fetchone():
            raise ValueError('Esta atividade já foi concluída neste dia. Selecione outra atividade para uma nova execução.')

    def configure_activity(self, routine_id, activity_id, name, *, prompt_key, tools,
                           description='', config=None, display_order=0, deadline=None,
                           recurrence=None, completion_action=None):
        """Save one activity and its explicit tool list; expand common config to columns."""
        if any(not isinstance(value,str) or not value.strip() for value in (activity_id,name,prompt_key)):
            raise ValueError('Activity ID, name and prompt key are required.')
        if (config is not None and not isinstance(config,dict)) or not isinstance(tools,(list,tuple)):
            raise ValueError('Provide an activity config object and a list of tool names.')
        for tool in tools:
            routine_tools.get_tool(tool)
        if deadline is not None:
            parsed = time.fromisoformat(deadline)
            if parsed.tzinfo:
                raise ValueError('Use local Sao Paulo time, without a timezone suffix.')
            deadline = parsed.isoformat(timespec='minutes')
        with self.connection(immediate=True) as conn:
            self._routine(conn,routine_id)
            existing = conn.execute('SELECT * FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ACTIVITY=?',(activity_id,)).fetchone()
            if existing and existing['ID_ROUTINE']!=routine_id:
                raise ValueError('Activity ID already belongs to another routine.')
            settings = activity_config(existing) if existing else {}
            settings.update(config or {})
            values = activity_values(settings)
            if completion_action is not None:
                values['COMPLETION_ACTION']=completion_action
            if values['COMPLETION_ACTION'] and values['COMPLETION_ACTION']!='manual_complete':
                routine_tools.get_tool(values['COMPLETION_ACTION'])
            values.update(ID_ACTIVITY=activity_id,ID_ROUTINE=routine_id,NM_ACTIVITY=name,
                DS_ACTIVITY=description,PROMPT_KEY=prompt_key,DISPLAY_ORDER=display_order)
            if deadline is not None:
                values['DEADLINE_TIME']=deadline
            if recurrence is not None or deadline is not None:
                values['RECURRENCE']=recurrence or 'DAILY'
            fields=','.join(values)
            updates=','.join(f'{key}=excluded.{key}' for key in values if key not in ('ID_ACTIVITY','ID_ROUTINE'))
            conn.execute(f"INSERT INTO UGM_ROUTINE_ACTIVITIES({fields}) VALUES({','.join('?' for _ in values)}) ON CONFLICT(ID_ACTIVITY) DO UPDATE SET {updates}",tuple(values.values()))
            previous={row['NM_TOOL']:dict(row) for row in conn.execute('SELECT * FROM UGM_ROUTINE_ACTIVITY_TOOLS WHERE ID_ACTIVITY=?',(activity_id,))}
            conn.execute('DELETE FROM UGM_ROUTINE_ACTIVITY_TOOLS WHERE ID_ACTIVITY=?',(activity_id,))
            for tool in dict.fromkeys(tools):
                policy=previous.get(tool,{}).get('REVIEW_POLICY','CONFIRM' if routine_tools.get_tool(tool).write else 'AUTO')
                options=previous.get(tool,{}).get('CONFIG_JSON','{}')
                conn.execute("""INSERT INTO UGM_ROUTINE_ACTIVITY_TOOLS(ID_ACTIVITY,NM_TOOL,REVIEW_POLICY,CONFIG_JSON)
                    VALUES(?,?,?,?)""",(activity_id,tool,policy,options))
        return self.load_activity(routine_id,activity_id)

    def configure_activity_schedule(self, routine_id, activity_id, *, deadline=None,
                                    recurrence='MANUAL', completion_action=None):
        """Edit the one optional schedule directly on its activity."""
        if deadline is not None:
            parsed=time.fromisoformat(deadline)
            if parsed.tzinfo:
                raise ValueError('Use a local time without timezone.')
            deadline=parsed.isoformat(timespec='minutes')
        with self.connection(immediate=True) as conn:
            self._activity_scope(conn,routine_id,activity_id)
            if completion_action and completion_action!='manual_complete' and not conn.execute(
                    'SELECT 1 FROM UGM_ROUTINE_ACTIVITY_TOOLS WHERE ID_ACTIVITY=? AND NM_TOOL=? AND FL_ACTIVE=1',
                    (activity_id,completion_action)).fetchone():
                raise ValueError('The completion action must be enabled for this activity.')
            conn.execute("""UPDATE UGM_ROUTINE_ACTIVITIES SET DEADLINE_TIME=?,RECURRENCE=?,COMPLETION_ACTION=?
                WHERE ID_ACTIVITY=?""",(deadline,recurrence,completion_action,activity_id))
