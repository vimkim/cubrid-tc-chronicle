import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from test_chronicle import CASE, run_metadata, sample

from chronicle.parsing import parse
from chronicle.query import Queries
from chronicle.storage import Index
from chronicle.web import create_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        with Index(self.root, writable=True) as index:
            self.failure = index.ingest(parse(sample(1, at="2026-10-15T10:00:00Z")))["execution_id"]
            index.ingest(parse(sample(2, at="2026-10-15T12:00:00Z")))
            index.ingest(parse(sample(3, "PASS", suite="medium")))
        self.server = create_server(self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temporary.cleanup()

    def fetch(self, path, **kwargs):
        kwargs["headers"] = {
            "Cookie": f"chronicle-{self.server.server_port}={self.server.access_token}",
            **kwargs.get("headers", {}),
        }
        request = urllib.request.Request(self.url + path, **kwargs)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_catalog_overview_and_suite_filters(self):
        status, _, body = self.fetch("/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["counts"]["observations"], 3)
        status, _, body = self.fetch("/api/cases?q=shared&suite=sql&limit=1")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["cases"][0]["failures"], 2)
        self.assertEqual(data["cases"][0]["failed_commits"], 1)
        self.assertEqual(json.loads(self.fetch("/api/cases?suite=medium")[2])["total"], 0)
        self.assertEqual(
            json.loads(self.fetch("/api/cases?suite=medium&failed_only=false&q=ShArEd")[2])[
                "total"
            ],
            1,
        )

    def test_http_history_matches_cli_query_policy(self):
        with Index(self.root) as index:
            from chronicle.query import HistoryQuery

            expected = Queries(index).history(HistoryQuery(CASE, suite="sql", variant="unknown"))
        with patch("chronicle.collection.GitHub.get", side_effect=AssertionError("network")):
            status, _, body = self.fetch(f"/api/history?case={CASE}&suite=sql&variant=unknown")
        actual = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["history_result"], "SEEN_BEFORE")

    def test_next_request_sees_a_concurrent_collectors_committed_snapshot(self):
        before = json.loads(self.fetch("/api/overview")[2])
        # Serving cannot hold the writer lock or pin a stale transaction across
        # HTTP requests. Collection and the UI's Refresh action stay independent.
        with Index(self.root, writable=True) as index:
            index.ingest(parse(sample(4, "PASS")))
        after = json.loads(self.fetch("/api/overview")[2])
        self.assertEqual(after["counts"]["observations"], before["counts"]["observations"] + 1)
        self.assertGreater(after["coverage"]["generation"], before["coverage"]["generation"])

    def test_history_exposes_retained_pr_details_and_pr_filter_without_network(self):
        run = {
            **run_metadata(2),
            "display_title": "PR #42 · A fix",
            "chronicle_pr": {
                "number": 42,
                "title": "A fix",
                "base_ref": "develop",
                "head_ref": "fix/something",
                "author": "pr-author",
                "author_url": "https://github.com/pr-author",
            },
        }
        with Index(self.root, writable=True) as index:
            index.record_run(run, "2030-01-01T00:00:00Z")
        with patch("chronicle.collection.GitHub.get", side_effect=AssertionError("network")):
            status, _, body = self.fetch(f"/api/history?case={CASE}&suite=sql&pr=42")
        report = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(report["occurrences"]), 1)
        self.assertEqual(report["target"]["pull_request"]["number"], 42)
        self.assertEqual(report["target"]["pull_request"]["author"], "pr-author")
        self.assertEqual(report["target"]["pull_request"]["title"], "A fix")
        self.assertEqual(report["target"]["pull_request"]["base_ref"], "develop")
        self.assertEqual(report["target"]["pull_request"]["head_ref"], "fix/something")

    def test_unknown_results_are_successful_queries(self):
        status, _, body = self.fetch("/api/history?case=not-indexed.sql")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["history_result"], "UNKNOWN_HISTORY")

    def test_evidence_is_retained_bytes_and_cannot_read_arbitrary_paths(self):
        status, _, body = self.fetch(f"/api/evidence?execution={self.failure}")
        self.assertEqual(status, 200)
        self.assertIn("summary_info", [file["name"] for file in json.loads(body)["files"]])
        status, headers, body = self.fetch(
            f"/api/evidence?execution={self.failure}&file=summary_info"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/plain")
        self.assertEqual(body, sample().files["summary_info"])
        self.assertEqual(
            self.fetch(f"/api/evidence?execution={self.failure}&file=../../etc/passwd")[0], 404
        )
        self.assertEqual(self.fetch("/../../etc/passwd")[0], 404)

    def test_ambiguous_identity_returns_candidates(self):
        # Public/private suites and variants must remain distinct in the web UI.
        status, _, body = self.fetch("/api/history?case=shared.sql")
        self.assertEqual(status, 409)
        self.assertEqual(len(json.loads(body)["candidates"]), 2)

    def test_invalid_filters_are_errors_and_do_not_mutate_the_index(self):
        for path in (
            "/api/history",
            "/api/history?case=shared&suite=invalid",
            "/api/history?case=shared&execution_id=bad",
            "/api/cases?limit=101",
            "/api/cases?offset=-1",
            "/api/cases?q=a&q=b",
            "/api/cases?failed_only=maybe",
            "/api/history?case=shared&outcome=GREEN",
            "/api/coverage?since=no-date",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 400)
        self.assertEqual(self.fetch("/api/sync", method="POST")[0], 501)
        with Index(self.root) as index:
            self.assertEqual(index.db.execute("SELECT count(*) FROM execution").fetchone()[0], 3)

    def test_security_headers_host_origin_and_forwarded_ports(self):
        status, headers, body = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn(b"CUBRID TC Chronicle", body)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(
            self.fetch("/api/overview", headers={"Origin": "https://example.test"})[0], 403
        )
        self.assertEqual(self.fetch("/", headers={"Host": "example.test"})[0], 403)
        self.assertEqual(self.fetch("/", headers={"Host": "localhost:9876"})[0], 200)
        self.assertEqual(self.fetch("/app.js", method="HEAD")[2], b"")

    def test_lan_hosts_work_with_authentication_and_same_origin_checks(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        with patch(
            "chronicle.web.interface_addresses",
            return_value=[("lan", "192.0.2.10"), ("vpn", "100.64.1.2"), ("vpn", "fd00::2")],
        ):
            self.server = create_server(self.root, port=0, local=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        hosts = ["192.0.2.10:9876", "100.64.1.2:9876"]
        if self.server.address_family == socket.AF_INET6:
            hosts.append("[fd00:0:0:0:0:0:0:2]:9876")
        for host in hosts:
            with self.subTest(host=host):
                headers = {"Host": host, "Origin": f"http://{host}"}
                self.assertEqual(self.fetch("/api/overview", headers=headers)[0], 200)
                self.assertEqual(
                    self.fetch("/api/overview", headers={**headers, "Cookie": ""})[0], 401
                )
                self.assertEqual(
                    self.fetch("/api/overview", headers={**headers, "Origin": "http://other"})[0],
                    403,
                )
        self.assertEqual(self.fetch("/", headers={"Host": "192.0.2.99"})[0], 403)

    def test_private_evidence_requires_the_per_session_access_link(self):
        status, _, body = self.fetch("/api/overview", headers={"Cookie": ""})
        self.assertEqual(status, 401)
        self.assertNotIn(b"observations", body)
        status, _, _ = self.fetch("/?token=wrong", headers={"Cookie": ""})
        self.assertEqual(status, 401)
        self.assertEqual(self.fetch("/?token=%ED%95%9C%EA%B8%80", headers={"Cookie": ""})[0], 401)
        # Inspect the bootstrap response without following its redirect.
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("GET", f"/?token={self.server.access_token}&q=shared")
        response = connection.getresponse()
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/?q=shared")
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])
        self.assertNotIn("token=", response.headers["Location"])
        response.read()
        connection.close()

    def test_uninitialized_index_keeps_static_ui_available(self):
        (self.root / "history.sqlite3").unlink()
        self.assertEqual(self.fetch("/")[0], 200)
        status, _, body = self.fetch("/api/overview")
        self.assertEqual(status, 503)
        self.assertIn("tcc sync", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
