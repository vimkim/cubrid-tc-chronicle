"""Read-only HTTP adapter over Chronicle's existing query interface."""

import ipaddress
import json
import secrets
import socket
import sqlite3
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .models import SUITES
from .network import interface_addresses
from .query import AmbiguousIdentity, HistoryQuery, Queries
from .storage import Index

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class _DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def create_server(data_dir: Path, port=8765, repository="CUBRID/cubrid", *, local=True):
    """Bind loopback for internal callers, or all interfaces when local=False.

    Each request reads one consistent SQLite snapshot.
    No route starts collection or accepts filesystem paths. Evidence URLs accept
    only an indexed snapshot and a filename already recorded in its manifest.
    """
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    dual_stack = not local and socket.has_dualstack_ipv6()
    addresses = [("lo", "127.0.0.1")] if local else interface_addresses()
    if not dual_stack:
        addresses = [(name, address) for name, address in addresses if ":" not in address]
    for address in ("127.0.0.1", "::1") if dual_stack else ("127.0.0.1",):
        if not any(value == address for _, value in addresses):
            addresses.append(("lo", address))
    allowed_hosts = {address for _, address in addresses} | {"localhost"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Search navigation can cancel an in-flight read.

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, dict):
                body = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self'; style-src 'self'; img-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def error(self, status, message, **extra):
            self.send(status, {"schema_version": 1, "error": message, **extra})

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                host = urlsplit("http://" + self.headers.get("Host", ""))
                hostname = host.hostname or ""
                try:
                    hostname = str(ipaddress.ip_address(hostname))
                except ValueError:
                    pass  # The localhost alias also supports SSH forwarding.
                if (
                    hostname not in allowed_hosts
                    or host.username is not None
                    or host.path
                    or host.query
                    or host.fragment
                ):
                    self.error(HTTPStatus.FORBIDDEN, "Use one of the printed access URLs.")
                    return
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{host.netloc}":
                    self.error(HTTPStatus.FORBIDDEN, "Cross-origin requests are not supported.")
                    return
                url = urlsplit(self.path)
                args = parse_qs(url.query, keep_blank_values=True, max_num_fields=25)
                if any(len(values) != 1 for values in args.values()):
                    raise ValueError("query parameters must appear once")
                args = {key: values[0] for key, values in args.items()}
                cookie_name = f"chronicle-{self.server.server_port}"
                if "token" in args:
                    if not secrets.compare_digest(
                        args.pop("token").encode(), self.server.access_token.encode()
                    ):
                        self.error(401, "Open the access URL printed by cubrid-tcc serve.")
                        return
                    self.send_response(303)
                    self.send_header(
                        "Set-Cookie",
                        f"{cookie_name}={self.server.access_token}; "
                        "Path=/; HttpOnly; SameSite=Strict",
                    )
                    self.send_header("Location", url.path + ("?" + urlencode(args) if args else ""))
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                session = cookie.get(cookie_name)
                if session is None or not secrets.compare_digest(
                    session.value.encode(), self.server.access_token.encode()
                ):
                    self.error(401, "Open the access URL printed by cubrid-tcc serve.")
                    return
                if url.path in ASSETS:
                    filename, mime = ASSETS[url.path]
                    self.send(
                        200, files("chronicle").joinpath("static", filename).read_bytes(), mime
                    )
                    return
                if len(url.query) > 8192:
                    raise ValueError("query is too long")
                suite = args.get("suite") or None
                if suite is not None and suite not in SUITES:
                    raise ValueError("invalid suite")
                with Index(data_dir) as index:
                    query = Queries(index, repository)
                    if url.path == "/api/overview":
                        result = query.overview(suite)
                    elif url.path == "/api/cases":
                        if args.get("failed_only", "true") not in ("true", "false"):
                            raise ValueError("failed_only must be true or false")
                        result = query.catalog(
                            args.get("q", ""),
                            suite,
                            failed_only=args.get("failed_only", "true") == "true",
                            limit=int(args.get("limit", "40")),
                            offset=int(args.get("offset", "0")),
                        )
                    elif url.path == "/api/coverage":
                        result = {
                            "schema_version": 1,
                            **query.coverage(args.get("since"), args.get("until"), suite),
                        }
                    elif url.path == "/api/history":
                        if not args.get("case"):
                            raise ValueError("case is required")
                        values = {
                            key: value
                            for key, value in args.items()
                            if key
                            in (
                                "case",
                                "suite",
                                "since",
                                "until",
                                "engine_sha",
                                "branch",
                                "variant",
                                "outcome",
                                "run_id",
                                "execution_id",
                                "ordinal",
                                "pr",
                            )
                            and value
                        }
                        for key in ("run_id", "execution_id", "ordinal", "pr"):
                            if key in values:
                                values[key] = int(values[key])
                                if values[key] < 1:
                                    raise ValueError(f"{key} must be positive")
                        if values.get("outcome") not in (
                            None,
                            "PASS",
                            "FAIL",
                            "ERROR",
                            "SKIP",
                            "NOT_RUN",
                        ):
                            raise ValueError("invalid outcome")
                        result = query.history(HistoryQuery(**values))
                    elif url.path == "/api/evidence":
                        bundle = index.bundle(int(args.get("execution", "0")))
                        if args.get("file"):
                            name = args["file"]
                            if name not in bundle.files:
                                self.error(404, "Evidence file is not retained.")
                                return
                            self.send(200, bundle.files[name], "text/plain; charset=utf-8")
                            return
                        result = {
                            "schema_version": 1,
                            "run_id": bundle.run_id,
                            "suite": bundle.suite,
                            "shard": bundle.shard,
                            "metadata": bundle.metadata,
                            "files": [
                                {
                                    "name": name,
                                    "bytes": len(data),
                                    "source": bundle.sources.get(name),
                                }
                                for name, data in sorted(bundle.files.items())
                            ],
                        }
                    else:
                        self.error(404, "Route not found.")
                        return
                self.send(200, result)
            except AmbiguousIdentity as e:
                self.error(409, str(e), candidates=e.candidates)
            except (BrokenPipeError, ConnectionResetError):
                return
            except ValueError as e:
                self.error(400, str(e))
            except (OSError, sqlite3.Error):
                self.error(
                    503, "The local index is unavailable. Run cubrid-tcc sync-data, then retry."
                )

    if dual_stack:
        server = _DualStackServer(("::", port), Handler)
    else:
        server = ThreadingHTTPServer(("127.0.0.1" if local else "0.0.0.0", port), Handler)
    server.access_token = secrets.token_urlsafe(32)
    server.access_addresses = addresses
    return server


def serve(data_dir: Path, port=8765, repository="CUBRID/cubrid", *, local=False):
    with create_server(data_dir, port, repository, local=local) as server:
        print(f"Chronicle access URLs ({'local only' if local else 'all interfaces'}):", flush=True)
        for interface, address in server.access_addresses:
            host = f"[{address}]" if ":" in address else address
            print(
                f"  {interface}: http://{host}:{server.server_port}/?token={server.access_token}",
                flush=True,
            )
        print("Read-only retained evidence. Press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nChronicle stopped.")
