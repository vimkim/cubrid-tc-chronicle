import contextlib
import io
import json
import socket
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from chronicle.cli import main
from chronicle.network import interface_addresses
from chronicle.web import create_server, serve


class NetworkTests(unittest.TestCase):
    def test_active_addresses_include_secondary_vpn_ipv6_and_bridge_interfaces(self):
        def entry(name, *addresses, state="UP"):
            return {
                "ifname": name,
                "flags": ["UP"],
                "operstate": state,
                "addr_info": [
                    {
                        "family": "inet6" if ":" in address else "inet",
                        "scope": "global",
                        "local": address,
                    }
                    for address in addresses
                ],
            }

        data = [
            entry("lo", "127.0.0.1", "::1", state="UNKNOWN"),
            entry("eno1", "192.0.2.10", "192.0.2.11", "fe80::1", "0.0.0.0", "224.0.0.1"),
            entry("vpn0", "100.64.1.2", "fd00::2", state="UNKNOWN"),
            entry("podman0", "10.88.0.1"),
            entry("duplicate", "192.0.2.10"),
            entry("down", "198.51.100.1", state="DOWN"),
        ]
        with patch(
            "chronicle.network.subprocess.run",
            return_value=SimpleNamespace(stdout=json.dumps(data)),
        ):
            addresses = interface_addresses()
        self.assertEqual(
            addresses,
            [
                ("eno1", "192.0.2.10"),
                ("eno1", "192.0.2.11"),
                ("podman0", "10.88.0.1"),
                ("vpn0", "100.64.1.2"),
                ("vpn0", "fd00::2"),
                ("lo", "127.0.0.1"),
                ("lo", "::1"),
            ],
        )

    def test_unavailable_discovery_has_an_actionable_error(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("ip", 5)):
            with self.subTest(error=type(error).__name__):
                with patch("chronicle.network.subprocess.run", side_effect=error):
                    with self.assertRaisesRegex(ValueError, "iproute2|ip -j address"):
                        interface_addresses()

    def test_invalid_discovery_does_not_invent_addresses(self):
        for output in ("not JSON", "{}", '[{"flags":["UP"],"addr_info":[null]}]'):
            with self.subTest(output=output):
                with patch(
                    "chronicle.network.subprocess.run", return_value=SimpleNamespace(stdout=output)
                ):
                    with self.assertRaises(ValueError):
                        interface_addresses()

    def test_local_mode_works_without_network_discovery(self):
        with patch("chronicle.web.interface_addresses", side_effect=AssertionError("discovery")):
            with create_server(Path("/unused"), port=0) as server:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                self.assertEqual(server.access_addresses, [("lo", "127.0.0.1")])

    def test_ipv4_fallback_only_advertises_addresses_it_can_serve(self):
        with (
            patch("chronicle.web.socket.has_dualstack_ipv6", return_value=False),
            patch(
                "chronicle.web.interface_addresses",
                return_value=[("lan", "192.0.2.10"), ("vpn", "fd00::2")],
            ),
            create_server(Path("/unused"), port=0, local=False) as server,
        ):
            self.assertEqual(server.server_address[0], "0.0.0.0")
            self.assertEqual(server.access_addresses, [("lan", "192.0.2.10"), ("lo", "127.0.0.1")])

    @unittest.skipUnless(socket.has_dualstack_ipv6(), "IPv6 dual-stack sockets unavailable")
    def test_lan_listener_accepts_ipv4_and_ipv6(self):
        with (
            patch("chronicle.web.interface_addresses", return_value=[("vpn", "fd00::2")]),
            create_server(Path("/unused"), port=0, local=False) as server,
        ):
            self.assertEqual(server.server_address[0], "::")
            self.assertEqual(server.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY), 0)
            self.assertIn(("vpn", "fd00::2"), server.access_addresses)

    def test_startup_prints_usable_authenticated_urls_with_actual_port(self):
        addresses = [("lan", "192.0.2.10"), ("vpn", "100.64.1.2"), ("vpn", "fd00::2")]
        output = io.StringIO()
        with (
            patch("chronicle.web.interface_addresses", return_value=addresses),
            patch("chronicle.web.ThreadingHTTPServer.serve_forever", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(output),
        ):
            serve(Path("/unused"), port=0)
        links = [
            urlsplit(line.split(": ", 1)[1])
            for line in output.getvalue().splitlines()
            if "http://" in line
        ]
        expected_hosts = {"192.0.2.10", "100.64.1.2", "127.0.0.1"}
        if socket.has_dualstack_ipv6():
            expected_hosts.update(("fd00::2", "::1"))
        self.assertEqual({link.hostname for link in links}, expected_hosts)
        self.assertEqual(len({link.port for link in links}), 1)
        self.assertGreater(links[0].port, 0)
        tokens = {parse_qs(link.query)["token"][0] for link in links}
        self.assertEqual(len(tokens), 1)
        self.assertGreater(len(tokens.pop()), 30)

    def test_cli_defaults_to_lan_and_exposes_explicit_local_mode(self):
        for flags, local in (([], False), (["--local"], True)):
            with self.subTest(local=local):
                with patch("chronicle.web.serve") as start:
                    self.assertEqual(main(["serve", "--port", "8877", *flags]), 0)
                    self.assertEqual(start.call_args.kwargs, {"local": local})
                    self.assertEqual(start.call_args.args[1], 8877)


if __name__ == "__main__":
    unittest.main()
