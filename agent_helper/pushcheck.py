"""Destination checks for push notifications (docs/decisions/0020), shared by the service and the relay.

The service only checks what it can without the network (`check_url`). The relay additionally resolves the
name with its own resolver, refuses any non-public address (`public_addresses`) and connects to exactly the
address it checked (`pinned_post`), so DNS rebinding cannot swap the target between check and connect.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_URL_LENGTH = 512
LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa", ".localhost", ".onion", ".test", ".invalid")
TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 1024

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Refused in addition to everything `is_global` already rejects; kept explicit so newer or easily
# overlooked blocks are covered whatever the Python version says.
BLOCKED_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",  # IPv4-mapped
        "::/96",  # IPv4-compatible (deprecated)
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",  # NAT64 local use
        "100::/64",  # discard
        "2001::/32",  # Teredo
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated)
        "ff00::/8",  # multicast
        "fd00:ec2::254/128",  # cloud metadata
    )
)


class DestinationRefused(Exception):
    """The destination must not be contacted. The message is for logs and the operator, never for agents."""


@dataclass(frozen=True)
class Destination:
    url: str
    host: str
    port: int
    path: str  # path and query


def normalize_host(host: str) -> str:
    """IDNA (UTS 46 where available) to ASCII, lower case, no trailing dot."""
    host = host.strip().rstrip(".")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise DestinationRefused("host name is not valid IDNA") from None
    return ascii_host.lower()


def matches(host: str, patterns: Iterable[str]) -> bool:
    """`host` equals a pattern, or a pattern `*.example.org` covers it (including example.org itself)."""
    for pattern in patterns:
        pattern = pattern.strip().lower().rstrip(".")
        if not pattern:
            continue
        if pattern.startswith("*."):
            base = pattern[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == pattern:
            return True
    return False


def parse_list(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def check_url(
    url: str,
    *,
    allowed_ports: Iterable[int] = (443,),
    deny_domains: Iterable[str] = (),
    allow_domains: Iterable[str] | None = None,
) -> Destination:
    """Syntax and policy checks that need no network. Raises DestinationRefused."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        raise DestinationRefused(f"url must be at most {MAX_URL_LENGTH} characters")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise DestinationRefused("url must not contain spaces or control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise DestinationRefused("url must use https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise DestinationRefused("url must not contain user information")
    if parts.fragment:
        raise DestinationRefused("url must not contain a fragment")
    try:
        port = parts.port or 443
    except ValueError:
        raise DestinationRefused("url has an invalid port") from None
    if port not in set(allowed_ports):
        raise DestinationRefused("this port is not allowed")
    if not parts.hostname:
        raise DestinationRefused("url needs a host name")
    host = normalize_host(parts.hostname)
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise DestinationRefused("use a host name, not an IP address")
    if "." not in host or host.endswith(LOCAL_SUFFIXES):
        raise DestinationRefused("the host name must be a public DNS name")
    if matches(host, deny_domains):
        raise DestinationRefused("this host is not allowed")
    if allow_domains is not None and not matches(host, allow_domains):
        raise DestinationRefused("this host is not on the allowlist")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Destination(url=url, host=host, port=port, path=path)


def address_refused(addr: IPAddress, deny_nets: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    if not addr.is_global:
        return True
    if any(addr in net for net in BLOCKED_NETS if net.version == addr.version):
        return True
    return any(addr in net for net in deny_nets if net.version == addr.version)


Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def public_addresses(
    host: str,
    port: int,
    deny_nets: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
    resolver: Resolver = system_resolver,
) -> list[IPAddress]:
    """All addresses of `host`, if every one of them is public and allowed. Raises DestinationRefused."""
    try:
        raw = resolver(host, port)
    except OSError:
        raise DestinationRefused("name does not resolve") from None
    if not raw:
        raise DestinationRefused("name does not resolve")
    addresses = []
    for value in raw:
        addr = ipaddress.ip_address(value.split("%")[0])
        if address_refused(addr, tuple(deny_nets)):
            raise DestinationRefused("name resolves to a refused address")
        addresses.append(addr)
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to one already checked address; the host name is used only for SNI, certificate checks and
    the Host header."""

    def __init__(self, host: str, address: str, port: int, context: ssl.SSLContext, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._ssl_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(sock, server_hostname=self.host)


@dataclass(frozen=True)
class Attempt:
    status: int | None  # HTTP status, or None if no response
    tls_failure: bool = False


def pinned_post(
    dest: Destination,
    address: IPAddress,
    headers: dict[str, str],
    body: bytes,
    context: ssl.SSLContext | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> Attempt:
    """POST with an overall deadline; the response body is read up to MAX_RESPONSE_BYTES and discarded."""
    context = context or ssl.create_default_context()
    conn = _PinnedHTTPSConnection(dest.host, str(address), dest.port, context, timeout)
    result: list[Attempt] = []

    def run() -> None:
        try:
            all_headers = {**headers, "Host": dest.host if dest.port == 443 else f"{dest.host}:{dest.port}"}
            all_headers["Connection"] = "close"
            conn.request("POST", dest.path, body=body, headers=all_headers)
            response = conn.getresponse()
            response.read(MAX_RESPONSE_BYTES)
            result.append(Attempt(status=response.status))
        except (ssl.SSLError, ssl.CertificateError):
            result.append(Attempt(status=None, tls_failure=True))
        except Exception:  # noqa: BLE001 (every other failure is just "no response")
            result.append(Attempt(status=None))

    worker = threading.Thread(target=run, name="agent-helper-push-post", daemon=True)
    worker.start()
    worker.join(timeout + 1)
    if not result:
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        conn.close()
        worker.join(1)
        return Attempt(status=None)
    conn.close()
    return result[0]
