"""Measure offline history on a synthetic SQLite corpus. No source network requests.

Run with `uv run python scripts/benchmark.py --observations 10000000`.
The temporary corpus is deleted after the measurement. Raw evidence is shared
across synthetic executions, so archive growth is measured by the live pilot.
"""

import argparse
import json
import tempfile
import time
from pathlib import Path

from chronicle.cli import read_bundle
from chronicle.parsing import parse
from chronicle.query import HistoryQuery, Queries
from chronicle.storage import Index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=int, default=10000000)
    parser.add_argument("--temp-dir", type=Path)
    args = parser.parse_args()
    if args.observations < 1000 or args.observations % 1000:
        parser.error("--observations must be a positive multiple of 1000")
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/sql-recorded/bundle.json"
    with tempfile.TemporaryDirectory(prefix="tcc-benchmark-", dir=args.temp_dir) as temporary:
        root = Path(temporary)
        started = time.perf_counter()
        with Index(root, writable=True) as index:
            index.ingest(parse(read_bundle(fixture)))
            with index.db:
                index.db.execute("DELETE FROM observation")
                index.db.execute("DELETE FROM testcase")
                index.db.executemany(
                    "INSERT INTO testcase(repository,suite,path) "
                    "VALUES ('CUBRID/cubrid-testcases','sql',?)",
                    (
                        (f"sql/_benchmark/cases/case_{i}.sql",)
                        for i in range(args.observations // 1000)
                    ),
                )
                columns = [
                    r[1] for r in index.db.execute("PRAGMA table_info(execution)") if r[1] != "id"
                ]
                prototype = dict(index.db.execute("SELECT * FROM execution LIMIT 1").fetchone())
                index.db.execute("DELETE FROM execution")
                values = []
                for i in range(1000):
                    row = {
                        **prototype,
                        "snapshot_key": f"synthetic-{i}",
                        "run_id": i,
                        "completed_at": f"2026-10-{1 + i % 15:02}T12:00:00Z",
                    }
                    values.append(tuple(row[column] for column in columns))
                index.db.executemany(
                    f"INSERT INTO execution({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    values,
                )
                index.db.execute("""INSERT INTO observation
                    SELECT e.id,t.id,1,CASE WHEN e.id % 20=0 THEN 'FAIL' ELSE 'PASS' END,
                    12,'synthetic','synthetic','summary_info','synthetic'
                    FROM execution e CROSS JOIN testcase t""")
            index.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        import_seconds = time.perf_counter() - started
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            with Index(root) as index:
                report = Queries(index).history(HistoryQuery("sql/_benchmark/cases/case_0.sql"))
            samples.append(time.perf_counter() - started)
        result = {
            "schema_version": 1,
            "corpus": "synthetic; shared recorded evidence",
            "observations": args.observations,
            "executions": 1000,
            "history_occurrences": len(report["occurrences"]),
            "build_seconds": round(import_seconds, 3),
            "history_seconds": [round(s, 6) for s in samples],
            "subsecond": all(s < 1 for s in samples),
            "database_bytes": (root / "history.sqlite3").stat().st_size,
        }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
