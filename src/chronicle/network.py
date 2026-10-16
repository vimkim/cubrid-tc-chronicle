"""Enumerate usable addresses on active Linux network interfaces."""

import ipaddress
import json
import subprocess


def interface_addresses():
    """Return deduplicated (interface, address) pairs, with loopback last.

    iproute2 supplies every address, including secondary and VPN addresses.
    Link-local addresses require interface-specific routing and are omitted.
    """
    try:
        result = subprocess.run(
            ["ip", "-j", "address", "show", "up"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as e:
        raise ValueError("LAN address discovery requires iproute2 (the ip command).") from e
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError("Could not discover LAN addresses with ip -j address show up.") from e
    try:
        interfaces = json.loads(result.stdout)
        if not isinstance(interfaces, list):
            raise ValueError("expected an interface list")
        found = {}
        for interface in interfaces:
            if "UP" not in interface.get("flags", []) or interface.get("operstate") == "DOWN":
                continue
            for info in interface.get("addr_info", []):
                if info.get("family") not in ("inet", "inet6") or info.get("scope") not in (
                    "global",
                    "host",
                ):
                    continue
                if info.get("tentative") or info.get("dadfailed"):
                    continue
                address = ipaddress.ip_address(info["local"])
                if address.is_unspecified or address.is_multicast or address.is_link_local:
                    continue
                found.setdefault(str(address), interface["ifname"])
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise ValueError("ip returned invalid network address data.") from e
    return [
        (found[address], address)
        for address in sorted(
            found,
            key=lambda value: (
                ipaddress.ip_address(value).is_loopback,
                found[value],
                ipaddress.ip_address(value).version,
                ipaddress.ip_address(value).packed,
            ),
        )
    ]
