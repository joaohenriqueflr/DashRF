"""Real SQLite cutover and invariants of the readable five-table routine model."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from services.routines import RoutineService
from services.routine_schema import TABLES, OBSOLETE_TABLES


class RoutineSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'schema.db'

    def legacy_database(self, *, bad_deadline=False):
        with closing(sqlite3.connect(self.db)) as c,c:
            c.executescript((Path(__file__).parent/'fixtures/routines_before_simplification.sql').read_text())
            config=json.dumps(dict(group='Market Data',sources=['XP'],instrument_types=['NTN-B'],workflow='quote_import'))
            c.execute("INSERT INTO UGM_ROUTINES VALUES('NTNB_RUN','Run NTN-B','Existing description','NTNB_RUN','market_run',?,1,0)",(config,))
            c.execute("""INSERT INTO UGM_ROUTINE_TOOLS VALUES('NTNB_RUN','save_quotes','save_quotes','{}',?, 'CONFIRM',1)""",(json.dumps({'allowed_sources':['XP']}),))
            c.execute("INSERT INTO UGM_ROUTINE_TOOLS VALUES('NTNB_RUN','query_quotes','query_quotes','{}','{}','AUTO',0)")
            for index,(activity,deadline) in enumerate([('B1030','25:00' if bad_deadline else '10:30'),('B1230','12:30'),('B1530','15:30')]):
                c.execute("INSERT INTO UGM_ROUTINE_ACTIVITIES VALUES(?,'NTNB_RUN',?,'Activity description','NTNB_RUN','{}',1,?)",(activity,activity,index))
                c.execute("INSERT INTO UGM_ROUTINE_ACTIVITY_TOOLS VALUES(?,'NTNB_RUN','save_quotes')",(activity,))
                c.execute("INSERT INTO UGM_ROUTINE_ACTIVITY_TOOLS VALUES(?,'NTNB_RUN','query_quotes')",(activity,))
                c.execute("""INSERT INTO UGM_ROUTINE_SCHEDULE(ID_STEP,ID_ROUTINE,NM_STEP,DEADLINE_TIME,RECURRENCE,COMPLETION_ACTION,ID_ACTIVITY)
                    VALUES(?,'NTNB_RUN','Old stage name',?,'DAILY','save_quotes',?)""",('obsolete-'+activity,deadline,activity))
            for identifier,action,status in [(1,'save_quotes','SUCCESS'),(2,'query_quotes','SUCCESS'),(3,'save_quotes','SUCCESS'),(4,'save_quotes','PENDING_REVIEW')]:
                activity='B1530' if identifier==4 else 'B1030'
                c.execute("""INSERT INTO UGM_ROUTINE_EXECUTIONS(ID_EXECUTION,ID_ROUTINE,ID_ACTIVITY,ID_STEP,DT_REF,USERNAME,NM_ACTION,TS_REQUEST,TS_EXECUTION,STATUS,INPUT_JSON,RESULT_JSON)
                    VALUES(?,'NTNB_RUN',?,?,'2026-11-03','Test user',?,'2026-11-03T10:00:00-03:00',?,?,'{}','{}')""",
                    (identifier,activity,'obsolete-'+activity,action,None if status=='PENDING_REVIEW' else '2026-11-03T10:01:00-03:00',status))
            c.execute("INSERT INTO UGM_ROUTINE_ACTIVITY_COMPLETIONS VALUES('B1030','2026-11-03',1,'2026-11-03T10:01:00-03:00')")
            c.execute("""INSERT INTO UGM_ROUTINE_CHAT(ID_MESSAGE,ID_ROUTINE,ID_ACTIVITY,USERNAME,DT_REF,TS_MESSAGE,USER_MESSAGE,ASSISTANT_MESSAGE,ID_EXECUTION)
                VALUES(100,'NTNB_RUN','B1030','Test user','2026-11-03','2026-11-03T10:00:00-03:00','Original message','Original reply',1)""")
            c.execute("CREATE TABLE FIXED_INCOME_BROKER_DATA(DT_REF TEXT,NM_SECURITY TEXT,TX_QUOTE REAL)")
            c.execute("INSERT INTO FIXED_INCOME_BROKER_DATA VALUES('2026-11-03','Existing quote',0.075)")

    def test_fresh_database_has_five_routine_tables_and_three_ntnb_activities(self):
        engine=RoutineService(self.db)
        engine.ensure_schema()
        with closing(sqlite3.connect(self.db)) as c:
            actual={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'UGM_ROUTINE%' AND name!='UGM_ROUTINES_DEVEDORES'")}
            self.assertEqual(actual,set(TABLES))
            self.assertEqual(c.execute("SELECT DEADLINE_TIME FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ROUTINE='NTNB_RUN' ORDER BY DEADLINE_TIME").fetchall(),[('10:30',),('12:30',),('15:30',)])
            for name in TABLES:
                columns={r[1] for r in c.execute(f'PRAGMA table_info({name})')}
                self.assertNotIn('ID_STEP',columns)
            columns={r[1] for r in c.execute('PRAGMA table_info(UGM_ROUTINE_ACTIVITY_TOOLS)')}
            self.assertNotIn('INPUT_SCHEMA_JSON',columns)
            self.assertNotIn('HANDLER_KEY',columns)

    def test_cutover_consolidates_settings_permissions_and_completion_without_losing_logs(self):
        self.legacy_database()
        engine=RoutineService(self.db)
        engine.ensure_schema()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT NM_GROUP FROM UGM_ROUTINES').fetchone()[0],'Market Data')
            rows=c.execute('SELECT ID_ACTIVITY,PROMPT_KEY,WORKFLOW,DEADLINE_TIME,RECURRENCE,COMPLETION_ACTION,SOURCES_JSON FROM UGM_ROUTINE_ACTIVITIES ORDER BY DEADLINE_TIME').fetchall()
            self.assertEqual(len(rows),3)
            self.assertTrue(all(r[1:3]==('NTNB_RUN','quote_import') and r[4:]==('DAILY','save_quotes','["XP"]') for r in rows))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM UGM_ROUTINE_ACTIVITY_TOOLS').fetchone()[0],3)
            self.assertEqual(c.execute('SELECT ID_EXECUTION,FL_COMPLETES_ACTIVITY FROM UGM_ROUTINE_EXECUTIONS ORDER BY ID_EXECUTION').fetchall(),[(1,1),(2,0),(3,0),(4,1)])
            self.assertEqual(c.execute('SELECT ID_MESSAGE,USER_MESSAGE,ASSISTANT_MESSAGE,ID_EXECUTION FROM UGM_ROUTINE_CHAT').fetchone(),(100,'Original message','Original reply',1))
            self.assertEqual(c.execute('SELECT TX_QUOTE FROM FIXED_INCOME_BROKER_DATA').fetchone()[0],0.075)
            for name in OBSOLETE_TABLES:
                self.assertIsNone(c.execute('SELECT name FROM sqlite_master WHERE name=?',(name,)).fetchone())
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
        engine.ensure_schema()
        self.assertEqual(engine.get_activity_completion('NTNB_RUN','B1030','2026-11-03')['ID_EXECUTION'],1)
        self.assertEqual(len(engine.load_chat_history('NTNB_RUN','Test user','2026-11-03')),1)

    def test_invalid_cutover_rolls_back_the_entire_schema_change(self):
        self.legacy_database(bad_deadline=True)
        with self.assertRaises(sqlite3.IntegrityError):
            RoutineService(self.db).ensure_schema()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertIsNotNone(c.execute("SELECT name FROM sqlite_master WHERE name='UGM_ROUTINE_SCHEDULE'").fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS').fetchone()[0],4)
            self.assertFalse(c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'NEW_%'").fetchall())

    def test_daily_completion_uniqueness_is_enforced_by_sqlite_itself(self):
        self.legacy_database()
        RoutineService(self.db).ensure_schema()
        with closing(sqlite3.connect(self.db)) as c,c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute('UPDATE UGM_ROUTINE_EXECUTIONS SET FL_COMPLETES_ACTIVITY=1 WHERE ID_EXECUTION=3')
            c.execute("UPDATE UGM_ROUTINE_EXECUTIONS SET FL_COMPLETES_ACTIVITY=1,DT_REF='2026-11-04' WHERE ID_EXECUTION=3")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS WHERE STATUS='SUCCESS' AND FL_COMPLETES_ACTIVITY=1").fetchone()[0],2)

    def test_typed_activity_settings_can_be_changed_without_a_json_object_or_join(self):
        engine=RoutineService(self.db)
        engine.ensure_schema()
        with closing(sqlite3.connect(self.db)) as c,c:
            c.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET DEADLINE_TIME='10:45',QUOTE_MODE='RATE' WHERE ID_ACTIVITY='NTNB_RUN_1030'")
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET DEADLINE_TIME='26:00' WHERE ID_ACTIVITY='NTNB_RUN_1030'")
        self.assertEqual(engine.load_activity('NTNB_RUN','NTNB_RUN_1030')['DEADLINE_TIME'],'10:45')


if __name__=='__main__':
    unittest.main()
