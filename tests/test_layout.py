import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from test_chronicle import CASE, run_metadata, sample

from chronicle.cli import main
from chronicle.parsing import parse
from chronicle.query import HistoryQuery, Queries
from chronicle.storage import Index

LAYOUT_TABLES = ("run_summary", "run_shard", "discovery_run", "discovery_indexed")


def downgrade(index):
    """Make the index look like one written before the query columns existed."""
    for table in LAYOUT_TABLES:
        index.db.execute(f"DROP TABLE {table}")
    index.db.execute("DELETE FROM meta WHERE key='layout'")
    index.db.commit()


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.index = Index(self.path, writable=True)
        runs = []
        for run_id, outcome in ((1, "PASS"), (2, "FAIL"), (3, "FAIL")):
            run = run_metadata(run_id, created=f"2026-10-15T1{run_id}:00:00Z")
            run["jobs"] = [
                {"name": f"shard shell {run_id}", "conclusion": "success"},
                {"name": "shard shell 9", "conclusion": "skipped"},
                {"name": "build", "conclusion": "success"},
            ]
            runs.append(run)
            self.index.record_run(run, "2026-10-17T00:00:00Z")
            self.index.ingest(parse(sample(run_id, outcome)))
        self.index.record_discovery(
            "CUBRID/cubrid",
            "2026-10-15T00:00:00Z",
            "2026-10-15T23:59:59Z",
            runs + [run_metadata(4, created="2026-10-15T14:00:00Z")],
            True,
        )

    def tearDown(self):
        self.index.__exit__()

    def reports(self):
        with Index(self.path) as index:
            queries = Queries(index)
            return (
                queries.coverage(),
                queries.history(HistoryQuery(CASE)),
            )

    def test_columns_are_written_with_runs_and_discoveries(self):
        db = self.index.db
        self.assertEqual(db.execute("SELECT count(*) FROM run_summary").fetchone()[0], 3)
        self.assertEqual(
            [tuple(r) for r in db.execute("SELECT suite,shard FROM run_shard WHERE run_id=2")],
            [("shell", "2"), ("shell", "9")],
        )
        self.assertEqual(db.execute("SELECT count(*) FROM discovery_run").fetchone()[0], 4)
        self.assertEqual(db.execute("SELECT value FROM meta WHERE key='layout'").fetchone()[0], "2")

    def test_old_index_answers_identically_before_and_after_migrate(self):
        current = self.reports()
        downgrade(self.index)
        with Index(self.path) as index:
            self.assertFalse(index.layout)
        self.assertEqual(self.reports(), current)  # read-only fallback parses JSON
        result = Index.migrate(self.index)
        self.assertTrue(result["migrated"])
        self.assertEqual((result["runs"], result["discoveries"]), (3, 1))
        with Index(self.path) as index:
            self.assertTrue(index.layout)
        self.assertEqual(self.reports(), current)

    def test_migrate_is_idempotent_and_keeps_schema_version(self):
        downgrade(self.index)
        self.assertTrue(self.index.migrate()["migrated"])
        counts = lambda: [  # noqa: E731
            self.index.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in LAYOUT_TABLES
        ]
        first = counts()
        self.assertFalse(self.index.migrate()["migrated"])
        self.assertEqual(counts(), first)
        self.assertEqual(
            self.index.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[
                0
            ],
            "1",
        )

    def test_reader_never_blocks_or_sees_half_a_migration(self):
        downgrade(self.index)
        with Index(self.path) as reader:  # open snapshot predates the migration
            self.index.migrate()
            self.assertFalse(reader._layout_ready())
            self.assertEqual(Queries(reader).coverage()["enumerated_runs"], 4)

    def test_runs_come_back_in_run_id_order_not_insertion_order(self):
        self.index.record_run(run_metadata(0, created="2026-10-15T10:00:00Z"), "x")
        with Index(self.path) as index:
            self.assertEqual(list(Queries(index)._runs()), [0, 1, 2, 3])

    def test_second_writer_is_refused(self):
        with self.assertRaisesRegex(ValueError, "another Chronicle writer"):
            Index(self.path, writable=True)

    def test_run_replaced_by_an_older_build_is_reparsed_not_trusted(self):
        # An older build replaces run.metadata but does not touch the columns.
        run = run_metadata(2, created="2026-10-15T12:00:00Z")
        run["status"] = "in_progress"
        self.index.db.execute(
            "UPDATE run SET metadata=?,metadata_digest='replaced' WHERE run_id=2",
            (json.dumps(run),),
        )
        self.index.db.commit()
        with Index(self.path) as index:
            self.assertIn(2, Queries(index).coverage()["in_progress_runs"])

    def test_migrate_command(self):
        downgrade(self.index)
        self.index.__exit__()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--data-dir", str(self.path), "migrate", "--json"]), 0)
        self.assertTrue(json.loads(output.getvalue())["migrated"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["--data-dir", str(self.path), "migrate", "--json"])
        self.assertFalse(json.loads(output.getvalue())["migrated"])
        self.index = Index(self.path, writable=True)


if __name__ == "__main__":
    unittest.main()
