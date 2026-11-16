"""Production catalogue, mock boundaries and reusable Dash import workspaces."""

from contextlib import closing
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from services.prompts import PromptStore
from services.routine_catalog import IMPORT_ACTIVITIES, install_import_catalog, install_mock_prompts
from services.routines import RoutineService


class RoutineCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'catalog.db'
        self.ai = Mock()
        self.engine = RoutineService(self.db, ai_service=self.ai,
            clock=lambda: datetime(2026, 11, 16, 11, 0, tzinfo=ZoneInfo('America/Sao_Paulo')))
        self.engine.ensure_schema()
        install_mock_prompts(PromptStore(self.db), 'configured-model')

    def test_ntnf_has_two_independent_daily_activities_and_di_is_repeatable(self):
        activities = self.engine.list_activities('NTNF_RUN')
        self.assertEqual([(a['NM_ACTIVITY'], a['DEADLINE_TIME'], a['RECURRENCE']) for a in activities],
                         [('Importar Casadas', '09:30', 'DAILY'), ('Importar Cotação DI', '10:30', 'DAILY')])
        self.assertFalse(activities[0]['OPTIONS_JSON']['repeatable'])
        self.assertTrue(activities[1]['OPTIONS_JSON']['repeatable'])
        prompt = self.engine.load_prompt('NTNF_RUN', 'NTNF_DI_QUOTES')
        self.assertIn('substituir a cotação anterior', prompt.instructions)
        self.assertIn('10h30', prompt.instructions)

    def test_bank_and_credit_activities_have_exact_sources_and_no_invented_schedules(self):
        expected = {
            'BANK_RUN': ['C6', 'Caixa', 'Rabobank', 'Bocom BBM', 'BTG', 'ABC', 'Daycoval', 'BV'],
            'CREDIT_RUN': ['xBridge', 'Santander', 'BGC', 'Ativa', 'Inter'],
        }
        for routine_id, sources in expected.items():
            activities = self.engine.list_activities(routine_id)
            self.assertEqual([a['NM_ACTIVITY'] for a in activities], ['Importar ' + source for source in sources])
            self.assertEqual([a['SOURCES_JSON'] for a in activities], [[source] for source in sources])
            self.assertTrue(all(a['DEADLINE_TIME'] is None and a['RECURRENCE'] == 'MANUAL' for a in activities))
        self.assertEqual(self.engine.load_routine('BANK_RUN')['NM_ROUTINE'], 'Run Bancários')

    def test_each_activity_has_its_own_versioned_mock_prompt_and_no_live_tools(self):
        keys = []
        for routine_id, definitions in IMPORT_ACTIVITIES.items():
            for activity_id, name, *_ in definitions:
                prompt = self.engine.load_prompt(routine_id, activity_id)
                self.assertEqual(prompt.key, activity_id)
                self.assertEqual(prompt.model, 'configured-model')
                self.assertIn(name, prompt.instructions)
                self.assertIn('mock', prompt.instructions)
                self.assertEqual(self.engine.load_routine_tools(routine_id, activity_id), [])
                self.assertTrue(self.engine.is_table_import(routine_id, activity_id))
                keys.append(prompt.key)
        self.assertEqual(len(set(keys)), 15)

    def test_mock_messages_are_readable_without_sdk_calls_writes_or_fake_completion(self):
        result = self.engine.process_routine_message('NTNF_RUN', 'Alice', 'Colei uma tabela. Pode confirmar.', activity_id='NTNF_DI_QUOTES')
        self.assertIn('preparação', result['text'])
        self.assertIn('substituindo a cotação anterior', result['text'])
        self.ai.extract.assert_not_called()
        self.ai.converse.assert_not_called()
        self.assertEqual(self.engine.get_routine_executions('NTNF_RUN'), [])
        self.assertIsNone(self.engine.get_activity_completion('NTNF_RUN', 'NTNF_DI_QUOTES'))
        history = self.engine.load_chat_history('NTNF_RUN', 'Alice', activity_id='NTNF_DI_QUOTES')
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['STATUS'], 'COMPLETED')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM AI_REQUEST_LOG').fetchone()[0], 0)

    def test_mock_cannot_execute_or_be_marked_complete_even_if_a_permission_is_added(self):
        with self.engine.connection(immediate=True) as conn:
            conn.execute("INSERT INTO UGM_ROUTINE_ACTIVITY_TOOLS(ID_ACTIVITY,NM_TOOL,REVIEW_POLICY) VALUES('BANK_C6','save_quotes','CONFIRM')")
        self.assertEqual(self.engine.load_routine_tools('BANK_RUN', 'BANK_C6'), [])
        with self.assertRaisesRegex(ValueError, 'preparação'):
            self.engine.prepare_action('BANK_RUN', 'Alice', 'save_quotes', {}, activity_id='BANK_C6')
        with self.assertRaisesRegex(ValueError, 'preparação'):
            self.engine.complete_activity('BANK_RUN', 'BANK_C6', 'Alice')

    def test_install_and_startup_preserve_edits_prompts_and_archived_history(self):
        with self.engine.connection(immediate=True) as conn:
            conn.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET NM_ACTIVITY='Custom C6' WHERE ID_ACTIVITY='BANK_C6'")
            conn.execute("INSERT INTO UGM_ROUTINE_EXECUTIONS(ID_ROUTINE,ID_ACTIVITY,DT_REF,USERNAME,NM_ACTION,TS_REQUEST,STATUS,INPUT_JSON) VALUES('NTNF_RUN','NTNF_RUN:DEFAULT','2026-11-15','Alice','save_quotes','now','SUCCESS','{}')")
            install_import_catalog(conn)
        store = PromptStore(self.db)
        store.save_version('routines', 'BANK_C6', 'Custom mock instructions', 'another-configured-model')
        install_mock_prompts(store, 'initial-model')
        self.engine.ensure_schema()
        self.assertEqual(self.engine.load_activity('BANK_RUN', 'BANK_C6')['NM_ACTIVITY'], 'Custom C6')
        self.assertEqual(self.engine.load_prompt('BANK_RUN', 'BANK_C6').instructions, 'Custom mock instructions')
        self.assertEqual(len(self.engine.get_routine_executions('NTNF_RUN', dt_ref='2026-11-15')), 1)
        with self.engine.connection() as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_overview_includes_only_new_ntnf_schedule_and_all_mock_workspaces(self):
        overview = self.engine.get_daily_overview()
        self.assertEqual([(a['NM_ACTIVITY'], a['DEADLINE_TIME']) for a in overview['activities'] if a['ID_ROUTINE'] == 'NTNF_RUN'],
                         [('Importar Casadas', '09:30'), ('Importar Cotação DI', '10:30')])
        banks = [a for a in overview['unscheduled_routines'] if a['ID_ROUTINE'] == 'BANK_RUN']
        credit = [a for a in overview['unscheduled_routines'] if a['ID_ROUTINE'] == 'CREDIT_RUN']
        self.assertEqual((len(banks), len(credit)), (8, 5))


HAS_DASH = all(importlib.util.find_spec(name) for name in ('dash', 'dash_ag_grid', 'dash_bootstrap_components'))


@unittest.skipUnless(HAS_DASH, 'Run in the dashboard environment.')
class RoutineCatalogUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dash import Dash, dcc, html
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / 'catalog.db'
        cls.engine = RoutineService(cls.db, ai_service=Mock())
        cls.engine.ensure_schema()
        install_mock_prompts(PromptStore(cls.db), 'configured-model')
        cls.app = Dash('routine_catalog_tests', suppress_callback_exceptions=True)
        cls.page = types.ModuleType('routine_catalog_ui_page')
        cls.page.__file__ = str(Path(__file__).resolve().parents[1] / 'pages/routines.py')
        sys.modules[cls.page.__name__] = cls.page
        with patch('dash.register_page'):
            exec(compile(Path(cls.page.__file__).read_text(), cls.page.__file__, 'exec'), cls.page.__dict__)
        cls.service_patch = patch.object(cls.page, 'service', return_value=cls.engine)
        cls.service_patch.start()
        cls.app.layout = html.Div([dcc.Store(id='current-user', data={'user': 'Alice'}), cls.page.layout()])
        cls.app._setup_server()
        cls.client = cls.app.server.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.service_patch.stop()
        sys.modules.pop(cls.page.__name__, None)
        cls.temp.cleanup()

    def dispatch(self, fragment, routine_id='BANK_RUN', activity_id='BANK_C6', **values):
        values = {'routine-selected.data': routine_id, 'current-user.data': {'user': 'Alice'},
                  'routine-activity-selected.data': {'routine': routine_id, 'activity': activity_id},
                  'routine-refresh.data': 0, **values}
        matches = [(key, spec) for key, spec in self.app.callback_map.items() if fragment in key and spec.get('callback')]
        self.assertEqual(len(matches), 1)
        key, spec = matches[0]
        def item(entry):
            return {**entry, 'value': values.get(entry['id'] + '.' + entry['property'])}
        outputs = spec['output'] if isinstance(spec['output'], list) else [spec['output']]
        serialized = [{'id': output.component_id, 'property': output.component_property} for output in outputs]
        response = self.client.post('/_dash-update-component', json={
            'output': key, 'outputs': serialized if isinstance(spec['output'], list) else serialized[0],
            'inputs': [item(entry) for entry in spec['inputs']], 'state': [item(entry) for entry in spec['state']],
            'changedPropIds': ['routine-send.n_clicks'] if fragment == 'routine-chat-feedback.children' else ['routine-selected.data']})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json['response']

    def test_all_fifteen_workspaces_have_chat_vertical_timeline_and_no_upload(self):
        for routine_id, definitions in IMPORT_ACTIVITIES.items():
            for activity_id, name, *_ in definitions:
                with self.subTest(activity=activity_id):
                    view = self.dispatch('routine-title.children', routine_id, activity_id)
                    self.assertEqual(view['routine-chat-heading']['children'], name)
                    self.assertEqual(view['routine-workspace']['className'], 'routine-workspace routine-workspace-import')
                    self.assertEqual(view['routine-timeline']['className'], 'routine-timeline routine-timeline-vertical')
                    for panel in ('routine-import-panel', 'routine-data-panel', 'routine-quote-summary-panel'):
                        self.assertEqual(view[panel]['style'], {'display': 'none'})
                    self.assertIn('Em preparação', view['routine-configuration']['children'])

    def test_bank_menu_and_di_repeat_guidance_are_available(self):
        menu = self.dispatch('routine-activity-menu.children')['routine-activity-menu']['children']
        self.assertEqual(len(menu), 8)
        view = self.dispatch('routine-title.children', 'NTNF_RUN', 'NTNF_DI_QUOTES')
        self.assertIn('10h30', view['routine-chat-hint']['children'])
        self.assertIn('substituir a cotação anterior', view['routine-chat-hint']['children'])

    def test_mock_chat_shows_disabled_review_preview_and_logs_only_human_reply(self):
        self.dispatch('routine-chat-feedback.children', **{'routine-send.n_clicks': 1, 'routine-chat-text.value': 'Como funciona esta atividade?'})
        result = self.dispatch('routine-chat.children')
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertIn('Prévia da importação', rendered)
        self.assertIn('Confirmar importação', rendered)
        self.assertIn('"disabled": true', rendered)
        self.assertIn('em preparação', rendered)
        self.assertEqual(self.engine.get_routine_executions('BANK_RUN'), [])
        self.assertEqual(self.client.get('/_dash-layout').status_code, 200)
        self.assertEqual(self.client.get('/_dash-dependencies').status_code, 200)
