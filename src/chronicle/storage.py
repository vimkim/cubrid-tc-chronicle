"""Content-addressed evidence and atomic, idempotent SQLite imports."""

import fcntl
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .models import Bundle, ParsedExecution, testcase_repository
from .parsing import parse

SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Archive:
    def __init__(self, root: Path):
        self.root = root

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        folder = self.root / digest[:2]
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / (digest + ".gz")
        if target.exists():
            self.read(digest)
            return digest
        fd, temporary = tempfile.mkstemp(dir=folder, prefix=".write-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(gzip.compress(data, mtime=0))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.read(digest)
        for directory in (folder, self.root):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return digest

    def read(self, digest: str) -> bytes:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid archive digest")
        data = gzip.decompress((self.root / digest[:2] / (digest + ".gz")).read_bytes())
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"archive checksum mismatch: {digest}")
        return data


_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '1'), ('generation', '0');
CREATE TABLE run (
    repository TEXT NOT NULL, run_id INTEGER NOT NULL, metadata TEXT NOT NULL,
    metadata_digest TEXT NOT NULL, checked_at TEXT NOT NULL, revisit_at TEXT NOT NULL,
    PRIMARY KEY(repository, run_id)
);
CREATE TABLE execution (
    id INTEGER PRIMARY KEY, snapshot_key TEXT NOT NULL, parser_version INTEGER NOT NULL,
    repository TEXT NOT NULL, run_id INTEGER NOT NULL, suite TEXT NOT NULL, shard TEXT NOT NULL,
    engine_sha TEXT, testcase_sha TEXT, testcase_branch TEXT, build_mode TEXT,
    completed_at TEXT, attempt INTEGER, job_id INTEGER,
    complete INTEGER NOT NULL, diagnostics TEXT NOT NULL, metadata TEXT NOT NULL,
    manifest_digest TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, UNIQUE(snapshot_key, parser_version)
);
CREATE TABLE testcase (
    id INTEGER PRIMARY KEY, repository TEXT NOT NULL, suite TEXT NOT NULL,
    path TEXT NOT NULL, variant TEXT NOT NULL DEFAULT '',
    UNIQUE(repository, suite, path, variant)
);
CREATE TABLE observation (
    execution_id INTEGER NOT NULL REFERENCES execution(id),
    testcase_id INTEGER NOT NULL REFERENCES testcase(id), ordinal INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('PASS','FAIL','SKIP','ERROR','NOT_RUN')),
    duration_ms REAL, original TEXT NOT NULL, raw_verdict TEXT NOT NULL,
    evidence TEXT NOT NULL, locator TEXT NOT NULL,
    PRIMARY KEY(execution_id, testcase_id, ordinal)
);
CREATE INDEX observation_history ON observation(testcase_id, outcome, execution_id);
CREATE INDEX execution_time ON execution(completed_at);
CREATE INDEX execution_run ON execution(repository, run_id, suite);
CREATE INDEX execution_sha ON execution(engine_sha);
CREATE TABLE discovery (
    id INTEGER PRIMARY KEY, repository TEXT NOT NULL, since TEXT NOT NULL, until TEXT NOT NULL,
    checked_at TEXT NOT NULL, complete INTEGER NOT NULL, run_count INTEGER NOT NULL,
    diagnostic TEXT NOT NULL, evidence_digest TEXT NOT NULL
);
CREATE TABLE availability (
    repository TEXT NOT NULL, run_id INTEGER NOT NULL, suite TEXT NOT NULL,
    location TEXT NOT NULL, reason TEXT NOT NULL, checked_at TEXT NOT NULL,
    PRIMARY KEY(repository, run_id, suite, location)
);
"""


class Index:
    """One writer. Read-only callers can hold a consistent SQLite snapshot."""

    def __init__(self, root: Path, *, writable: bool = False):
        self.root = root.expanduser().resolve()
        self.archive = Archive(self.root / "objects")
        database = self.root / "history.sqlite3"
        if writable:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._lock = os.fdopen(
                os.open(self.root / "writer.lock", os.O_RDWR | os.O_CREAT, 0o600), "rb"
            )
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                self._lock.close()
                raise ValueError("another Chronicle writer is active; retry later") from e
            # New files contain private shell evidence. Existing permissions are preserved.
            if not database.exists():
                database.touch(mode=0o600)
            self.db = sqlite3.connect(database)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            if not self.db.execute("SELECT name FROM sqlite_master WHERE name='meta'").fetchone():
                self.db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nCOMMIT;")
        else:
            self.db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            self.db.execute("BEGIN")
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not version or int(version[0]) != SCHEMA_VERSION:
            self.db.close()
            raise ValueError("unsupported index schema version")
        if writable:
            # Additive indexes remain compatible with the original schema and CLI.
            self.db.execute("""CREATE INDEX IF NOT EXISTS observation_failures
                ON observation(testcase_id,execution_id) WHERE outcome IN ('FAIL','ERROR')""")
            self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()
        if hasattr(self, "_lock"):
            self._lock.close()

    def _advance(self):
        self.db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            self.db.execute("SAVEPOINT ingest")
            try:
                yield
                self.db.execute("RELEASE SAVEPOINT ingest")
            except BaseException:
                self.db.execute("ROLLBACK TO SAVEPOINT ingest")
                self.db.execute("RELEASE SAVEPOINT ingest")
                raise
        else:
            with self.db:
                yield

    def ingest(self, parsed: ParsedExecution) -> dict:
        bundle = parsed.bundle
        repository = bundle.metadata.get("repository", "CUBRID/cubrid")
        files = {name: self.archive.put(data) for name, data in sorted(bundle.files.items())}
        # Retrieval time and changing GitHub job lists do not manufacture new executions.
        snapshot = hashlib.sha256(
            encode(
                [
                    repository,
                    bundle.run_id,
                    bundle.suite,
                    bundle.shard,
                    bundle.metadata.get("execution_variant"),
                    files,
                ]
            )
        ).hexdigest()
        manifest = {
            "run_id": bundle.run_id,
            "suite": bundle.suite,
            "shard": bundle.shard,
            "files": files,
            "metadata": bundle.metadata,
            "sources": bundle.sources,
        }
        timestamp = now()
        with self._transaction():
            prior = self.db.execute(
                "SELECT id,complete,completed_at,metadata,manifest_digest FROM execution "
                "WHERE snapshot_key=? AND parser_version=?",
                (snapshot, parsed.parser_version),
            ).fetchone()
            if prior:
                metadata = bundle.metadata
                if (
                    prior["completed_at"] is None
                    and metadata.get("completed_at")
                    and metadata.get("run_attempts") == 1
                    and metadata.get("ordering") == "unique_job"
                ):
                    # A later consistency check can establish ordering after the
                    # sole job completes. Keep the first provenance object too.
                    metadata = {
                        **json.loads(prior["metadata"]),
                        **metadata,
                        "previous_manifest_digest": prior["manifest_digest"],
                    }
                    manifest["metadata"] = metadata
                    digest = self.archive.put(encode(manifest))
                    self.db.execute(
                        "UPDATE execution SET completed_at=?,metadata=?,"
                        "manifest_digest=? WHERE id=?",
                        (metadata["completed_at"], encode(metadata).decode(), digest, prior["id"]),
                    )
                self.db.execute(
                    "UPDATE execution SET last_seen=? WHERE id=?", (timestamp, prior[0])
                )
                self._advance()
                return {
                    "execution_id": prior[0],
                    "inserted": False,
                    "observations": 0,
                    "complete": bool(prior[1]),
                }
            manifest_digest = self.archive.put(encode(manifest))
            self.db.execute("UPDATE execution SET active=0 WHERE snapshot_key=?", (snapshot,))
            m = bundle.metadata
            cursor = self.db.execute(
                """INSERT INTO execution
                (snapshot_key,parser_version,repository,run_id,suite,shard,engine_sha,testcase_sha,
                 testcase_branch,build_mode,completed_at,attempt,job_id,complete,diagnostics,
                 metadata,manifest_digest,first_seen,last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot,
                    parsed.parser_version,
                    repository,
                    bundle.run_id,
                    bundle.suite,
                    bundle.shard,
                    parsed.engine_sha,
                    parsed.testcase_sha,
                    parsed.testcase_branch,
                    parsed.build_mode,
                    m.get("completed_at"),
                    m.get("execution_attempt"),
                    m.get("execution_job_id"),
                    int(parsed.complete),
                    encode([asdict(d) for d in parsed.diagnostics]).decode(),
                    encode(m).decode(),
                    manifest_digest,
                    timestamp,
                    timestamp,
                ),
            )
            execution_id = cursor.lastrowid
            testcase_repo = testcase_repository(bundle.suite)
            variant = m.get("execution_variant") or ""
            paths = sorted({o.path for o in parsed.observations})
            self.db.executemany(
                "INSERT OR IGNORE INTO testcase(repository,suite,path,variant) VALUES (?,?,?,?)",
                ((testcase_repo, bundle.suite, path, variant) for path in paths),
            )
            ids = {
                r["path"]: r["id"]
                for r in self.db.execute(
                    "SELECT id,path FROM testcase WHERE repository=? AND suite=? AND variant=?",
                    (testcase_repo, bundle.suite, variant),
                )
            }
            self.db.executemany(
                "INSERT INTO observation VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    (
                        execution_id,
                        ids[o.path],
                        o.ordinal,
                        o.outcome,
                        o.duration_ms,
                        o.original,
                        o.raw_verdict,
                        o.evidence,
                        o.locator,
                    )
                    for o in parsed.observations
                ),
            )
            self._advance()
        return {
            "execution_id": execution_id,
            "inserted": True,
            "observations": len(parsed.observations),
            "complete": parsed.complete,
            "diagnostics": [asdict(d) for d in parsed.diagnostics],
        }

    def bundle(self, execution_id: int) -> Bundle:
        row = self.db.execute(
            "SELECT manifest_digest FROM execution WHERE id=?", (execution_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"execution {execution_id} is not indexed")
        manifest = json.loads(self.archive.read(row[0]))
        manifest["files"] = {k: self.archive.read(v) for k, v in manifest["files"].items()}
        return Bundle(**manifest)

    def reparse(self) -> dict:
        ids = [r[0] for r in self.db.execute("SELECT id FROM execution WHERE active=1")]
        # Stream bundles inside one transaction. Queries see the old generation
        # until every checksum and parse succeeds, without retaining the corpus in RAM.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            receipts = []
            for old_id in ids:
                receipt = self.ingest(parse(self.bundle(old_id)))
                receipts.append(receipt)
                if receipt["execution_id"] != old_id:
                    self.db.execute("UPDATE execution SET active=0 WHERE id=?", (old_id,))
                    self._advance()
        return {"snapshots": len(ids), "inserted": sum(r["inserted"] for r in receipts)}

    def record_run(self, metadata: dict, revisit_at: str):
        digest = self.archive.put(encode(metadata))
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO run VALUES (?,?,?,?,?,?)",
                (
                    metadata["repository"],
                    metadata["id"],
                    encode(metadata).decode(),
                    digest,
                    now(),
                    revisit_at,
                ),
            )
            self._advance()
        return digest

    def record_discovery(self, repository, since, until, runs, complete, diagnostic=""):
        digest = self.archive.put(encode(runs))
        with self.db:
            self.db.execute(
                "INSERT INTO discovery VALUES (NULL,?,?,?,?,?,?,?,?)",
                (repository, since, until, now(), int(complete), len(runs), diagnostic, digest),
            )
            self._advance()

    def availability(self, repository, run_id, suite, location, reason):
        with self.db:
            if reason:
                self.db.execute(
                    "INSERT OR REPLACE INTO availability VALUES (?,?,?,?,?,?)",
                    (repository, run_id, suite, location, reason, now()),
                )
            else:
                changed = self.db.execute(
                    "DELETE FROM availability WHERE repository=? AND run_id=? "
                    "AND suite=? AND location=?",
                    (repository, run_id, suite, location),
                ).rowcount
                if not changed:
                    return
            self._advance()
