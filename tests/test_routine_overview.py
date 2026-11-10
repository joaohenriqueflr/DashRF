"""Shared daily agenda reads real schedules and executions in temporary SQLite."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

from services.routines import RoutineService
from routine_test_support import single_ntnb_activity


TZ = ZoneInfo("America/Sao_Paulo")


class DailyOverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "overview.db"
        self.now = datetime(2026, 11, 4, 10, 0, tzinfo=TZ)
        self.service = RoutineService(self.db, clock=lambda: self.now)
        single_ntnb_activity(self.service)

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute(sql, params).lastrowid

    def schedule(self, step, routine="NTNB_RUN", deadline="10:30", *, recurrence="WEEKDAYS", order=0, active=1):
        with closing(sqlite3.connect(self.db)) as c:
            c.row_factory=sqlite3.Row
            from services.routine_schema import activity_config
            base={"CONFIG_JSON":activity_config(c.execute("SELECT * FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ACTIVITY=?",(f"{routine}:DEFAULT",)).fetchone())}
        self.service.configure_activity(routine,step,step,prompt_key=routine,tools=['save_quotes'] if routine!='DEBTORS' else [],
            config=base['CONFIG_JSON'],deadline=deadline,recurrence=recurrence,display_order=order)
        self.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET FL_ACTIVE=?,COMPLETION_ACTION='save_quotes' WHERE ID_ACTIVITY=?",(active,step))
        if active:
            self.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET FL_ACTIVE=0 WHERE ID_ACTIVITY=?",(f"{routine}:DEFAULT",))

    def execution(self, step, status, *, routine="NTNB_RUN", day="2026-11-04", stamp=None):
        activity_id=step or f"{routine}:DEFAULT"
        return self.execute("""INSERT INTO UGM_ROUTINE_EXECUTIONS
            (ID_ROUTINE,ID_ACTIVITY,FL_COMPLETES_ACTIVITY,DT_REF,USERNAME,NM_ACTION,TS_REQUEST,TS_EXECUTION,STATUS,INPUT_JSON,RESULT_JSON,ERROR_MESSAGE)
            VALUES(?,?,?,?,'Private user','save_quotes',?,?,?,'{"secret":"input"}','{"secret":"result"}','private error')""",
            (routine, activity_id, 1, day, self.now.isoformat(), stamp or self.now.isoformat(), status))

    def by_step(self, overview=None):
        return {item["ID_ACTIVITY"]: item for item in (overview or self.service.get_daily_overview())["activities"]}

    def test_chronological_agenda_crosses_categories_and_keeps_unspecified_times_last(self):
        self.schedule("b-afternoon", deadline="15:30", order=0)
        self.schedule("debtors-early", routine="DEBTORS", deadline="09:00", order=30)
        self.schedule("f-midday", routine="NTNF_RUN", deadline="12:00", order=0)
        self.schedule("credit-midday", routine="CREDIT_RUN", deadline="12:00", order=0)
        self.schedule("b-later-order", deadline="12:00", order=10)
        self.schedule("b-first-order", deadline="12:00", order=5)
        self.schedule("untimed", routine="DEBTORS", deadline=None)
        overview = self.service.get_daily_overview()
        self.assertEqual([row["ID_ACTIVITY"] for row in overview["activities"]], [
            "debtors-early", "b-first-order", "b-later-order", "f-midday", "credit-midday", "b-afternoon", "untimed"])
        self.assertEqual(overview["dt_ref"], "2026-11-04")
        self.assertEqual(overview["timezone"], "America/Sao_Paulo")
        self.assertEqual(overview["attention_minutes"], 60)
        self.assertEqual(overview["activities"][0]["category"], "Operations")
        self.assertEqual(overview["activities"][-1]["deadline"], None)
        self.assertEqual(overview["activities"][-1]["minutes_to_deadline"], None)

    def test_deadline_attention_uses_configured_window_and_existing_timeline_boundary(self):
        for step, deadline in [("past", "09:59"), ("now", "10:00"), ("near", "10:30"), ("edge", "11:00"), ("future", "11:01")]:
            self.schedule(step, deadline=deadline)
        rows = self.by_step()
        self.assertEqual(rows["past"]["state"], "OVERDUE")
        self.assertEqual(rows["past"]["attention"], "OVERDUE")
        self.assertEqual(rows["past"]["minutes_to_deadline"], -1)
        for step in ("now", "near", "edge"):
            self.assertEqual(rows[step]["state"], "PENDING")
            self.assertEqual(rows[step]["attention"], "DUE_SOON")
        self.assertIsNone(rows["future"]["attention"])
        self.assertIsNone(self.by_step(self.service.get_daily_overview(attention_minutes=15))["near"]["attention"])
        self.assertEqual(self.by_step(self.service.get_daily_overview(attention_minutes=0))["now"]["attention"], "DUE_SOON")

    def test_shared_pending_and_execution_status_never_complete_a_step(self):
        self.schedule("review", deadline="15:00")
        self.schedule("running", routine="NTNF_RUN", deadline="15:00")
        self.schedule("failed", routine="CREDIT_RUN", deadline="15:00")
        self.execution("review", "PENDING_REVIEW")
        self.execution("running", "EXECUTING", routine="NTNF_RUN")
        failed_id = self.execution("failed", "FAILED", routine="CREDIT_RUN")
        rows = self.by_step()
        self.assertEqual(rows["review"]["progress"], "PENDING_REVIEW")
        self.assertEqual(rows["review"]["attention"], "PENDING_REVIEW")
        self.assertEqual(rows["running"]["progress"], "IN_PROGRESS")
        self.assertEqual(rows["running"]["attention"], "IN_PROGRESS")
        self.assertEqual(rows["failed"]["attention"], "FAILED")
        self.assertEqual(rows["failed"]["latest_status"], "FAILED")
        self.assertEqual(rows["failed"]["latest_execution_id"], failed_id)
        self.assertTrue(all(item["state"] == "PENDING" for item in rows.values()))
        self.now = self.now.replace(hour=16)
        self.assertTrue(all(item["attention"] == "OVERDUE" for item in self.by_step().values()))

    def test_success_remains_completed_and_failed_attempt_is_superseded_by_retry(self):
        self.schedule("completed", deadline="09:30")
        self.schedule("late", deadline="09:30")
        self.schedule("retry", deadline="15:00")
        self.execution("completed", "SUCCESS", stamp="2026-11-04T09:29:00-03:00")
        self.execution("completed", "FAILED")
        self.execution("late", "SUCCESS", stamp="2026-11-04T09:31:00-03:00")
        self.execution("retry", "FAILED")
        self.execution("retry", "PENDING_REVIEW")
        rows = self.by_step()
        self.assertEqual(rows["completed"]["state"], "ON_TIME")
        self.assertEqual(rows["late"]["state"], "LATE")
        self.assertIsNone(rows["completed"]["attention"])
        self.assertIsNone(rows["late"]["attention"])
        self.assertEqual(rows["retry"]["attention"], "PENDING_REVIEW")
        self.assertEqual(rows["retry"]["latest_status"], "PENDING_REVIEW")

    def test_no_schedule_is_distinct_from_no_occurrence_on_weekend(self):
        self.schedule("weekday-only")
        self.schedule("daily", routine="DEBTORS", recurrence="DAILY")
        self.schedule("manual", routine="CREDIT_RUN", deadline=None, recurrence="MANUAL")
        self.schedule("inactive-f", routine="NTNF_RUN", active=0)
        overview = self.service.get_daily_overview("2026-11-07")
        self.assertEqual(set(self.by_step(overview)), {"daily"})
        unscheduled = {item["ID_ROUTINE"]: item for item in overview["unscheduled_routines"]}
        self.assertEqual(unscheduled["NTNB_RUN"]["schedule_status"], "NO_STEPS_TODAY")
        self.assertEqual(unscheduled["NTNF_RUN"]["schedule_status"], "NO_SCHEDULE")
        self.assertTrue(all(item["state"] is None and item["deadline"] is None for item in unscheduled.values()))

    def test_unscheduled_routines_expose_shared_operational_state_for_selected_day(self):
        self.execution(None, "FAILED", routine="DEBTORS")
        self.execution(None, "PENDING_REVIEW", routine="NTNB_RUN")
        self.execution(None, "EXECUTING", routine="NTNF_RUN")
        self.execution(None, "FAILED", routine="CREDIT_RUN", day="2026-11-03")
        rows = {item["ID_ROUTINE"]: item for item in self.service.get_daily_overview()["unscheduled_routines"]}
        self.assertEqual(rows["DEBTORS"]["attention"], "FAILED")
        self.assertEqual(rows["NTNB_RUN"]["attention"], "PENDING_REVIEW")
        self.assertEqual(rows["NTNF_RUN"]["attention"], "IN_PROGRESS")
        self.assertIsNone(rows["CREDIT_RUN"]["latest_status"])
        self.assertIsNone(rows["CREDIT_RUN"]["attention"])

    def test_inactive_routines_steps_and_other_days_are_excluded(self):
        self.schedule("active")
        self.schedule("inactive-step", active=0)
        self.schedule("inactive-routine", routine="NTNF_RUN")
        self.execute("UPDATE UGM_ROUTINES SET FL_ACTIVE=0 WHERE ID_ROUTINE='NTNF_RUN'")
        self.execution("active", "SUCCESS", day="2026-11-03")
        overview = self.service.get_daily_overview()
        self.assertEqual(list(self.by_step(overview)), ["active"])
        self.assertEqual(overview["activities"][0]["state"], "PENDING")
        self.assertIsNone(overview["activities"][0]["latest_status"])
        self.assertNotIn("NTNF_RUN", [item["ID_ROUTINE"] for item in overview["unscheduled_routines"]])

    def test_overview_is_read_only_and_has_no_conversation_or_request_payloads(self):
        self.schedule("test")
        self.execution("test", "SUCCESS")
        self.execution(None, "FAILED", routine="DEBTORS")
        before = self.db.read_bytes()
        result = self.service.get_daily_overview()
        self.assertEqual(self.db.read_bytes(), before)
        encoded = json.dumps(result)
        for private in ("Private user", "private error", '"secret"', "USERNAME", "INPUT_JSON", "RESULT_JSON", "completed_by", "COMPLETION_CONFIG_JSON"):
            self.assertNotIn(private, encoded)
        timeline = self.service.get_routine_timeline("NTNB_RUN")[0]
        activity = result["activities"][0]
        for field in ("state", "progress", "deadline", "completed_at"):
            self.assertEqual(activity[field], timeline[field])

    def test_clock_is_converted_to_operational_day_before_selecting_schedule(self):
        self.now = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)
        self.schedule("evening", deadline="22:30")
        result = self.service.get_daily_overview()
        self.assertEqual(result["dt_ref"], "2026-11-04")
        self.assertEqual(result["now"], "2026-11-04T22:00:00-03:00")
        self.assertEqual(result["activities"][0]["minutes_to_deadline"], 30)

    def test_attention_window_validation(self):
        for invalid in (-1, True, "60", 0.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.service.get_daily_overview(attention_minutes=invalid)


if __name__ == "__main__":
    unittest.main()
