"""The isolated browser for the web page reader (docs/decisions/0029, step 2).

Run with `python -m agent_helper.browser_server` in the browser image. It starts Camoufox as a Playwright
server on a secret WebSocket path. Its container sits only on an internal network with the egress proxy and
the gateway; the preferences below are the first layer, the network layout the second.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Firefox preferences: every connection through the proxy (localhost included), nothing that opens a
# connection or a channel the proxy cannot see, nothing that keeps state or opens files.
FIREFOX_PREFS: dict[str, object] = {
    "network.proxy.allow_hijacking_localhost": True,
    "network.proxy.no_proxies_on": "",
    "network.proxy.failover_direct": False,
    "network.proxy.socks_remote_dns": True,
    "network.trr.mode": 5,  # no DNS over HTTPS
    "network.dns.disablePrefetch": True,
    "network.dns.disablePrefetchFromHTTPS": True,
    "network.prefetch-next": False,
    "network.predictor.enabled": False,
    "network.http.speculative-parallel-limit": 0,
    "media.peerconnection.enabled": False,  # no WebRTC
    "media.navigator.enabled": False,
    "dom.serviceWorkers.enabled": False,
    "dom.webnotifications.enabled": False,
    "dom.push.enabled": False,
    "geo.enabled": False,
    "pdfjs.disabled": True,  # PDFs are downloads, and downloads are refused
    "browser.download.open_pdf_attachments_inline": False,
    "network.protocol-handler.external-default": False,
    "network.protocol-handler.warn-external-default": True,
    "browser.cache.disk.enable": False,
    "browser.sessionstore.resume_from_crash": False,
    "signon.rememberSignons": False,
    "extensions.update.enabled": False,
    "app.update.enabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.enabled": False,
    "browser.safebrowsing.malware.enabled": False,  # would call out to update lists
    "browser.safebrowsing.phishing.enabled": False,
    "captivedetect.canonicalURL": "",
    "network.captive-portal-service.enabled": False,
    "network.connectivity-service.enabled": False,
}


def _digest(expected_sha256: str) -> str:
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise SystemExit("the expected browser digest must be 64 hex digits")
    return expected


def checking(expected_sha256: str, original: Callable[..., Any], checked: list[str]) -> Callable[..., Any]:
    """Wrap camoufox's own digest check so that the downloaded archive itself is hashed and compared with the
    pinned digest before camoufox unpacks it (camoufox alone compares only with what GitHub reports)."""
    expected = _digest(expected_sha256)

    def verify(buffer: Any, reported: Any, *args: Any, **kwargs: Any) -> Any:
        buffer.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: buffer.read(1 << 20), b""):
            digest.update(chunk)
        buffer.seek(0)
        if digest.hexdigest() != expected:
            raise SystemExit(f"the downloaded browser archive has digest {digest.hexdigest()}, not the pinned one")
        checked.append(digest.hexdigest())
        return original(buffer, reported, *args, **kwargs)

    return verify


def fetch_pinned(expected_sha256: str) -> None:
    """`camoufox fetch`, with the archive checked against the pinned digest before it is unpacked."""
    import camoufox.multiversion as multiversion  # only in the browser image
    from camoufox.__main__ import cli

    checked: list[str] = []
    multiversion.verify_sha256 = checking(expected_sha256, multiversion.verify_sha256, checked)
    cli(["fetch"], standalone_mode=False)
    if not checked:
        raise SystemExit("no browser archive was downloaded and checked")
    verify_build(expected_sha256)


def verify_build(expected_sha256: str, install_dir: Path | None = None) -> None:
    """Fail the image build unless the installed browser is recorded with the pinned digest."""
    if install_dir is None:
        from camoufox.pkgman import INSTALL_DIR  # only in the browser image

        install_dir = Path(INSTALL_DIR)
    expected = _digest(expected_sha256)
    found = []
    for version_file in sorted(install_dir.rglob("version.json")):
        try:
            found.append(str(json.loads(version_file.read_text("utf-8")).get("sha256") or "").lower())
        except (OSError, ValueError):
            continue
    if expected not in found:
        raise SystemExit(f"the fetched browser build does not match the pinned digest (found: {found or 'none'})")


def install_model() -> None:
    """Install fpgen's pinned fingerprint model (camoufox checks the archive and each file against its pinned
    sha256). Run as the owner of site-packages, root, while building the image (issue #45)."""
    from camoufox.fpgen_model import ensure_fpgen_model  # only in the browser image

    ensure_fpgen_model()


def check_model() -> None:
    """Fail unless the pinned model is in place and usable by the current (unprivileged) user without writing:
    camoufox calls the same function before every launch."""
    from camoufox.fpgen_model import ensure_fpgen_model, is_pinned  # only in the browser image

    if not is_pinned():
        raise SystemExit("fpgen's pinned model is missing; run --install-model as root first")
    ensure_fpgen_model()  # raises FpgenModelError if this user would have to write anything


def prepare_runtime() -> None:
    """Write the font configurations camoufox would otherwise create at the first launch, one for each OS it
    may pretend to be, while building the image as root: at run time the file system is read-only."""
    from camoufox.utils import get_env_vars  # only in the browser image

    for os_key in ("lin", "mac", "win"):
        get_env_vars({}, os_key)


DEFAULT_LIFETIME = 6 * 3600
MIN_LIFETIME = 300
RESTART_EXIT_CODE = 75  # EX_TEMPFAIL: also restarted by an on-failure policy


def lifetime_seconds(raw: str) -> float:
    """How long one browser server may live (BROWSER_MAX_LIFETIME_SECONDS, default 6 hours, at least 5
    minutes; 0 turns the limit off). A little random spread keeps restarts from lining up."""
    raw = raw.strip()
    if not raw:
        value = DEFAULT_LIFETIME
    elif raw.isascii() and raw.isdecimal():
        value = int(raw)
    else:
        raise SystemExit("BROWSER_MAX_LIFETIME_SECONDS must be a whole number of seconds")
    if value == 0:
        return 0.0
    return max(value, MIN_LIFETIME) * random.uniform(0.9, 1.0)  # noqa: S311 (spread, not security)


def schedule_exit(seconds: float, exit_: Callable[[int], Any] = os._exit) -> threading.Timer | None:
    """End the whole process after `seconds`, so the container's restart policy starts a fresh browser:
    whatever a page managed to leave in the browser does not outlive the limit. os._exit also ends the
    browser it started."""
    if seconds <= 0:
        return None
    timer = threading.Timer(seconds, exit_, args=(RESTART_EXIT_CODE,))
    timer.daemon = True
    timer.start()
    return timer


def main() -> None:
    if sys.argv[1:] == ["--prepare-runtime"]:
        prepare_runtime()
        print("runtime files prepared")
        return
    if sys.argv[1:] == ["--install-model"]:
        install_model()
        print("fpgen model installed and checked")
        return
    if sys.argv[1:] == ["--check-model"]:
        check_model()
        print("fpgen model is pinned and readable")
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--fetch":
        fetch_pinned(sys.argv[2])
        print("browser archive and build match the pinned digest")
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-build":
        verify_build(sys.argv[2])
        print("browser build matches the pinned digest")
        return
    from camoufox.server import launch_server  # only in the browser image

    schedule_exit(lifetime_seconds(os.environ.get("BROWSER_MAX_LIFETIME_SECONDS", "")))
    ws_path = os.environ.get("BROWSER_WS_PATH", "")
    if len(ws_path) < 16:
        raise SystemExit("BROWSER_WS_PATH must be a random path of at least 16 characters")
    launch_server(
        headless=True,
        port=int(os.environ.get("BROWSER_PORT", "9222")),
        ws_path=ws_path.strip("/"),
        # Every page goes through the egress proxy; the gateway sets the same proxy on each context.
        proxy={"server": os.environ.get("EGRESS_PROXY", "http://egress:3128")},
        geoip=False,  # a lookup of the exit address would be one more outbound call
        locale="en-US",
        block_webrtc=True,
        firefox_user_prefs=FIREFOX_PREFS,
        i_know_what_im_doing=True,
    )


if __name__ == "__main__":
    main()
