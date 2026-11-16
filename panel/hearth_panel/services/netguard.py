"""Is an address on the internet, or local to this machine and its network?"""

from __future__ import annotations

import ipaddress

LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")
CGNAT = ipaddress.ip_network("100.64.0.0/10")


def ip_is_local(value: str) -> bool:
    """Loopback, private, link-local or carrier-grade NAT. Unparseable input counts as not local."""
    try:
        ip = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in CGNAT


def host_is_local(host: str) -> bool:
    """For a hostname or IP from a URL, without a DNS lookup. Single-label names ("agent",
    "localhost") are Docker or LAN names; names under a LAN-only suffix count as local too."""
    host = host.strip().strip("[]").rstrip(".").lower()
    if ip_is_local(host):
        return True
    try:
        ipaddress.ip_address(host)
        return False  # a public IP
    except ValueError:
        pass
    return "." not in host or host.endswith(LOCAL_SUFFIXES)


async def resolve(host: str) -> list[str]:
    """Every address a hostname resolves to (the IP itself for an IP)."""
    import asyncio
    import socket

    host = host.strip("[]")
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def public_destination(url: str, resolver=resolve) -> str | None:
    """None if every address the URL's host resolves to is on the internet; otherwise why not.
    Checked right before each request, so a name that later points inside the network is caught."""
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    if not host or host_is_local(host):
        return f"{host or 'that address'} is on this machine or the local network"
    try:
        addresses = await resolver(host)
    except OSError:
        return f"couldn't look up {host}"
    inside = [a for a in addresses if ip_is_local(a) or ipaddress.ip_address(a).is_reserved
              or ipaddress.ip_address(a).is_multicast or ipaddress.ip_address(a).is_unspecified]
    if not addresses or inside:
        return f"{host} points inside the network ({', '.join(inside) or 'no address'})"
    return None
