"""Exercise the daily overview and routine deep links through Dash callbacks."""

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
from unittest.mock import patch
from zoneinfo import ZoneInfo

from services.routines import RoutineService
from routine_test_support import single_ntnb_activity


HAS_DASH = all(importlib.util.find_spec(name) for name in (
    "dash", "dash_ag_grid", "dash_bootstrap_components",
))


def components(value, kind=None):
    """Find serialized components without depending on their nesting depth."""
    found = []
    if isinstance(value, list):
        for child in value:
            found.extend(components(child, kind))
    elif isinstance(value, dict):
        if "props" in value and "type" in value:
            if kind is None or value["type"] == kind:
                found.append(value)
            found.extend(components(value["props"].get("children"), kind))
    return found


@unittest.skipUnless(HAS_DASH, "Run UI tests in the dashboard's Dash environment.")
class RoutineOverviewUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dash import Dash, dcc, html

        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "data").mkdir()
        cls.db = cls.root / "data" / "fixed_income.db"
        cls.now = datetime(2026, 11, 4, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
        cls.engine = RoutineService(cls.db, clock=lambda: cls.now)
        single_ntnb_activity(cls.engine)
        cls.app = Dash("routine_overview_ui_tests", suppress_callback_exceptions=True)
        cls.modules = []

        def load_page(filename):
            page = types.ModuleType("overview_ui_test_" + filename)
            page.__file__ = str(cls.root / "pages" / (filename + ".py"))
            sys.modules[page.__name__] = page
            cls.modules.append(page.__name__)
            source = (Path(__file__).resolve().parents[1] / "pages" / (filename + ".py")).read_text()
            with patch("dash.register_page") as register:
                exec(compile(source, page.__file__, "exec"), page.__dict__)
            page.registered_path = register.call_args.kwargs["path"]
            return page

        cls.page = load_page("routine_overview")
        cls.workspace = load_page("routines")
        cls.app.layout = html.Div([
            dcc.Store(id="current-user", data={"user": "Alice"}), cls.page.layout(),
        ])
        cls.app._setup_server()
        cls.client = cls.app.server.test_client()

    @classmethod
    def tearDownClass(cls):
        for name in cls.modules:
            sys.modules.pop(name, None)
        cls.temp.cleanup()

    def setUp(self):
        self.execute("DELETE FROM UGM_ROUTINE_EXECUTIONS")
        self.execute("DELETE FROM UGM_ROUTINE_ACTIVITY_TOOLS WHERE ID_ACTIVITY NOT LIKE '%:DEFAULT'")
        self.execute("DELETE FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ACTIVITY NOT LIKE '%:DEFAULT'")
        self.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET FL_ACTIVE=1,DEADLINE_TIME=NULL,RECURRENCE='MANUAL'")
        seen=set()
        self.execute("UPDATE UGM_ROUTINES SET FL_ACTIVE=1")
        # Insert deliberately out of chronological order and across categories.
        for step, routine, deadline in [
            ("FECHAMENTO", "NTNF_RUN", "18:00"),
            ("REVISAO", "NTNB_RUN", "10:30"),
            ("TARDE", "NTNB_RUN", "15:30"),
            ("ABERTURA", "DEBTORS", "09:00"),
            ("PROXIMO", "NTNF_RUN", "11:00"),
            ("CONCLUIDA", "NTNB_RUN", "08:00"),
        ]:
            activity_id=f"{routine}:{step}" if routine in seen else f"{routine}:DEFAULT"
            seen.add(routine)
            base=self.engine.load_activity(routine,f"{routine}:DEFAULT")
            self.engine.configure_activity(routine,activity_id,step,prompt_key=routine,
                tools=['save_quotes'] if routine!='DEBTORS' else [],config=base['CONFIG_JSON'],deadline=deadline,recurrence='DAILY')
            self.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET COMPLETION_ACTION='save_quotes' WHERE ID_ACTIVITY=?",(activity_id,))
        self.execution("CONCLUIDA", "NTNB_RUN", "SUCCESS", stamp="2026-11-04T07:59:00-03:00")
        self.execution("REVISAO", "NTNB_RUN", "PENDING_REVIEW")

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute(sql, params).lastrowid

    def execution_count(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS").fetchone()[0]

    def execution(self, step, routine, status, *, stamp=None):
        with closing(sqlite3.connect(self.db)) as conn:
            activity_id=conn.execute('SELECT ID_ACTIVITY FROM UGM_ROUTINE_ACTIVITIES WHERE NM_ACTIVITY=? AND ID_ROUTINE=?',(step,routine)).fetchone()[0]
        return self.execute("""INSERT INTO UGM_ROUTINE_EXECUTIONS
            (ID_ROUTINE,ID_ACTIVITY,FL_COMPLETES_ACTIVITY,DT_REF,USERNAME,NM_ACTION,TS_REQUEST,TS_EXECUTION,STATUS,INPUT_JSON)
            VALUES(?,?,?,'2026-11-04','Bob','save_quotes',?,?,?,'{}')""",
            (routine, activity_id, 1, self.now.isoformat(), stamp or self.now.isoformat(), status))

    def dispatch(self, output_fragment="ugm-overview-agenda.children", values=None):
        values = {"ugm-overview-poll.n_intervals": 0, "current-user.data": {"user": "Alice"},
                  "ugm-overview-filter.value": "all", "ugm-overview-category.value": "all", **(values or {})}
        matches = [(key, spec) for key, spec in self.app.callback_map.items()
                   if output_fragment in key and spec.get("callback")]
        self.assertEqual(len(matches), 1, output_fragment)
        key, spec = matches[0]
        outputs = spec["output"] if isinstance(spec["output"], list) else [spec["output"]]
        output_specs = [{"id": item.component_id, "property": item.component_property} for item in outputs]
        with patch.object(self.page, "service", return_value=self.engine):
            response = self.client.post("/_dash-update-component", json={
                "output": key, "outputs": output_specs if isinstance(spec["output"], list) else output_specs[0],
                "inputs": [{"id": item["id"], "property": item["property"],
                            "value": values.get(item["id"] + "." + item["property"])} for item in spec["inputs"]],
                "state": [], "changedPropIds": [spec["inputs"][0]["id"] + "." + spec["inputs"][0]["property"]],
            })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["response"]

    @staticmethod
    def agenda_titles(response):
        return [item["props"]["children"] for item in components(response["ugm-overview-agenda"]["children"], "H4")]

    @staticmethod
    def metrics(response):
        result = {}
        for metric in response["ugm-overview-summary"]["children"]:
            value = components(metric, "Strong")[0]["props"]["children"]
            label = components(metric, "Small")[0]["props"]["children"]
            result[label] = value
        return result

    def test_page_layout_uses_existing_access_and_never_loads_data_without_user(self):
        self.assertEqual(self.client.get("/_dash-layout").status_code, 200)
        access = self.dispatch("ugm-overview-shell.style", {"current-user.data": None})
        self.assertEqual(access["ugm-overview-shell"]["style"], {"display": "none"})
        with patch.object(self.engine, "get_daily_overview") as read:
            response = self.dispatch(values={"current-user.data": None})
        read.assert_not_called()
        for key in ("ugm-overview-agenda", "ugm-overview-summary", "ugm-overview-attention"):
            self.assertEqual(response[key]["children"], [])
        active = self.dispatch("ugm-overview-shell.style")
        self.assertNotEqual(active["ugm-overview-shell"]["style"].get("display"), "none")

    def test_agenda_is_chronological_with_periods_and_cross_category_tasks(self):
        response = self.dispatch()
        self.assertEqual(self.agenda_titles(response), ["CONCLUIDA", "ABERTURA", "REVISAO", "PROXIMO", "TARDE", "FECHAMENTO"])
        headings = [item["props"]["children"] for item in components(response["ugm-overview-agenda"]["children"], "H3")]
        self.assertEqual(headings, ["Manhã", "Tarde", "Fim do dia"])
        self.assertIn("São Paulo", json.dumps(response["ugm-overview-clock"], ensure_ascii=False))
        self.assertIn("quarta-feira, 4 de novembro", json.dumps(response["ugm-overview-clock"], ensure_ascii=False))

    def test_attention_contains_overdue_review_and_next_hour_but_not_later_or_completed(self):
        response = self.dispatch()
        attention = response["ugm-overview-attention"]["children"]
        cards = [item for item in components(attention, "Link")
                 if item["props"].get("className") == "ugm-overview-attention-item"]
        titles = [components(card, "Strong")[0]["props"]["children"] for card in cards]
        self.assertEqual(titles, ["ABERTURA", "REVISAO", "PROXIMO"])
        labels = json.dumps(cards, ensure_ascii=False)
        for label in ("Atrasada", "Aguardando confirmação", "Na próxima hora"):
            self.assertIn(label, labels)
        self.assertEqual(self.metrics(response)["Precisam de atenção"], "3")
        self.assertEqual(self.metrics(response)["Etapas atrasadas"], "1")
        self.assertEqual(self.metrics(response)["Etapas concluídas"], "1/6")

    def test_filters_keep_completed_separate_from_pending_and_urgent(self):
        attention = self.dispatch(values={"ugm-overview-filter.value": "attention"})
        self.assertEqual(self.agenda_titles(attention), ["ABERTURA", "REVISAO", "PROXIMO"])
        pending = self.dispatch(values={"ugm-overview-filter.value": "pending"})
        self.assertEqual(self.agenda_titles(pending), ["ABERTURA", "REVISAO", "PROXIMO", "TARDE", "FECHAMENTO"])
        completed = self.dispatch(values={"ugm-overview-filter.value": "completed"})
        self.assertEqual(self.agenda_titles(completed), ["CONCLUIDA"])

    def test_category_filter_scopes_agenda_attention_and_metrics(self):
        response = self.dispatch(values={"ugm-overview-category.value": "Operations"})
        self.assertEqual(self.agenda_titles(response), ["ABERTURA"])
        self.assertEqual(self.metrics(response)["Etapas concluídas"], "0/1")
        self.assertEqual(self.metrics(response)["Precisam de atenção"], "1")
        self.assertEqual(response["ugm-overview-unscheduled"]["children"], [])
        self.assertEqual({option["value"] for option in response["ugm-overview-category"]["options"]},
                         {"all", "Market Data", "Operations"})

    def test_poll_observes_another_users_success_without_operating_the_routine(self):
        first = self.dispatch()
        self.assertIn("REVISAO", self.agenda_titles(first))
        with patch.object(self.engine, "prepare_action") as prepare, patch.object(self.engine, "complete_activity") as complete:
            before = self.execution_count()
            self.dispatch(values={"ugm-overview-poll.n_intervals": 1})
            after = self.execution_count()
        prepare.assert_not_called()
        complete.assert_not_called()
        self.assertEqual(before, after)
        self.execution("REVISAO", "NTNB_RUN", "SUCCESS")
        refreshed = self.dispatch(values={"ugm-overview-poll.n_intervals": 2, "ugm-overview-filter.value": "attention"})
        self.assertNotIn("REVISAO", self.agenda_titles(refreshed))
        self.assertEqual(self.metrics(refreshed)["Etapas concluídas"], "2/6")
        self.assertEqual(self.metrics(refreshed)["Precisam de atenção"], "2")

    def test_unconfigured_routines_are_visible_with_links_without_invented_deadlines(self):
        response = self.dispatch()
        others = response["ugm-overview-unscheduled"]["children"]
        cards = [item for item in components(others, "Link") if item["props"].get("className") == "ugm-overview-routine-card"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(components(cards[0], "H4")[0]["props"]["children"], "Importar run")
        self.assertIn("Run Crédito Privado", json.dumps(cards, ensure_ascii=False))
        self.assertEqual(cards[0]["props"]["href"], "/routines/workspace?routine=CREDIT_RUN&activity=CREDIT_RUN%3ADEFAULT")
        self.assertIn("Sem agenda cadastrada", json.dumps(cards, ensure_ascii=False))
        self.assertNotIn("CREDIT_RUN", json.dumps(response["ugm-overview-agenda"]))
        self.assertEqual(self.metrics(response)["Atividades sem agenda"], "1")

    def test_all_agenda_rows_open_their_routine_and_offer_no_completion_action(self):
        response = self.dispatch()
        agenda = response["ugm-overview-agenda"]["children"]
        rows = [item for item in components(agenda, "Link") if "ugm-overview-row " in item["props"].get("className", "")]
        self.assertEqual(len(rows), 6)
        self.assertEqual([row["props"]["href"] for row in rows], [
            f"/routines/workspace?routine={routine}&activity={activity}"
            for routine, activity in [
                ("NTNB_RUN", "NTNB_RUN%3ACONCLUIDA"), ("DEBTORS", "DEBTORS%3ADEFAULT"),
                ("NTNB_RUN", "NTNB_RUN%3ADEFAULT"), ("NTNF_RUN", "NTNF_RUN%3APROXIMO"),
                ("NTNB_RUN", "NTNB_RUN%3ATARDE"), ("NTNF_RUN", "NTNF_RUN%3ADEFAULT"),
            ]
        ])
        self.assertEqual(components(agenda, "Button"), [])

    def test_workspace_deep_link_selects_requested_active_routine_and_invalid_id_falls_back(self):
        from plotly.utils import PlotlyJSONEncoder

        self.assertEqual(self.page.registered_path, "/routines")
        self.assertEqual(self.workspace.registered_path, "/routines/workspace")
        for requested, expected in [("DEBTORS", "DEBTORS"), ("NTNF_RUN", "NTNF_RUN"), ("missing", "NTNB_RUN")]:
            tree = json.loads(json.dumps(self.workspace.layout(routine=requested), cls=PlotlyJSONEncoder))
            selected = next(item for item in components(tree, "Store") if item["props"].get("id") == "routine-selected")
            self.assertEqual(selected["props"]["data"], expected)
            self.assertTrue(any(item["props"].get("href") == "/routines" for item in components(tree, "Link")))

    def test_panorama_is_a_selected_sidebar_option_and_retains_routine_navigation(self):
        from plotly.utils import PlotlyJSONEncoder

        overview = json.loads(json.dumps(self.page.layout(), cls=PlotlyJSONEncoder))
        sidebar = next(item for item in components(overview, "Aside")
                       if item["props"].get("className") == "routine-sidebar")
        panorama = next(item for item in components(sidebar, "Link") if item["props"].get("href") == "/routines")
        self.assertEqual(panorama["props"]["children"], "Panorama do dia")
        self.assertIn("routine-menu-selected", panorama["props"]["className"])

        menu = self.dispatch("ugm-overview-menu.children")["ugm-overview-menu"]["children"]
        self.assertEqual({item["props"]["href"] for item in components(menu, "Link")}, {
            "/routines/workspace?routine=" + routine for routine in ("NTNB_RUN", "NTNF_RUN", "CREDIT_RUN", "DEBTORS")})
        self.execute("UPDATE UGM_ROUTINES SET FL_ACTIVE=0 WHERE ID_ROUTINE='CREDIT_RUN'")
        refreshed = self.dispatch("ugm-overview-menu.children")["ugm-overview-menu"]["children"]
        self.assertNotIn("CREDIT_RUN", json.dumps(refreshed))

        workspace = json.loads(json.dumps(self.workspace.layout(routine="NTNF_RUN"), cls=PlotlyJSONEncoder))
        sidebar = next(item for item in components(workspace, "Aside")
                       if item["props"].get("className") == "routine-sidebar")
        panorama = next(item for item in components(sidebar, "Link") if item["props"].get("href") == "/routines")
        self.assertEqual(panorama["props"]["children"], "Panorama do dia")
        self.assertIn("routine-menu-button", panorama["props"]["className"])
        self.assertNotIn("routine-menu-selected", panorama["props"]["className"])
        self.assertEqual(len(components(sidebar, "Button")), 3)

    def test_database_failure_clears_statistics_and_reports_unavailable_state(self):
        with patch.object(self.engine, "get_daily_overview", side_effect=sqlite3.OperationalError("unavailable")):
            with self.assertLogs(self.page.logger, level="ERROR"):
                response = self.dispatch()
        self.assertEqual(response["ugm-overview-summary"]["children"], [])
        self.assertEqual(response["ugm-overview-agenda"]["children"], [])
        feedback = response["ugm-overview-feedback"]["children"]
        self.assertEqual(feedback["props"]["role"], "alert")
        self.assertIn("Não foi possível atualizar", feedback["props"]["children"])


if __name__ == "__main__":
    unittest.main()
