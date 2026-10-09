"""Operator web console (docs/decisions/0016): server-rendered HTML, no JavaScript, no extra dependencies.

Everything an agent wrote is escaped before it reaches the page. Logging in with the admin secret starts
a session (a random cookie, `HttpOnly`, `SameSite=Strict`, limited to `/admin`); every form carries a
per-session CSRF token. Sessions live in memory and end on restart.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import html
import logging
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__, board
from .catalog import Catalog
from .config import Settings
from .handles import OPERATOR_HANDLE
from .models import (
    MAX_ID,
    CapabilityDecisionIn,
    HideIn,
    OperatorBoardIn,
    OperatorReplyIn,
    ReferralIn,
    ReportDecisionIn,
)
from .store import MailLimits, MailRefused, Store

COOKIE = "agent_helper_console"
SESSION_SECONDS = 12 * 3600
MAX_SESSIONS = 20
CSP = "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
LOG_LINES = 500
LOGIN_DELAY_SECONDS = 0.5

e = html.escape


# --- recent access log ----------------------------------------------------------------------------------


class RingHandler(logging.Handler):
    """Keeps the last access-log lines in memory for the console. They contain no addresses or bodies."""

    def __init__(self, size: int = LOG_LINES) -> None:
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: S110 (a log view must never break logging)
            pass


_ring = RingHandler()
logging.getLogger("agent_helper").addHandler(_ring)  # children (access, notify, catalog) propagate here


# --- sessions -------------------------------------------------------------------------------------------


class Sessions:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, str]] = {}  # sha256(token) -> (expires, csrf)
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            while len(self._items) >= MAX_SESSIONS:
                self._items.pop(min(self._items, key=lambda k: self._items[k][0]))
            self._items[self._key(token)] = (now + SESSION_SECONDS, csrf)
        return token, csrf

    def csrf_for(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            item = self._items.get(self._key(token))
        if item is None or item[0] < time.monotonic():
            return None
        return item[1]

    def drop(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._items.pop(self._key(token), None)


# --- HTML helpers ---------------------------------------------------------------------------------------

STYLE = """
body{font:15px/1.45 system-ui,sans-serif;margin:0;color:#1d1d1f;background:#f6f6f4}
header{background:#1d2b36;color:#fff;padding:.6rem 1rem;display:flex;gap:1rem;flex-wrap:wrap;align-items:center}
header a{color:#cfe3f2;text-decoration:none}header strong{margin-right:1rem}
main{max-width:1100px;margin:0 auto;padding:1rem}
table{border-collapse:collapse;width:100%;background:#fff}td,th{border-bottom:1px solid #ddd;padding:.4rem;
text-align:left;vertical-align:top}th{background:#eef1f3}
.agent{white-space:pre-wrap;background:#fffbe6;border-left:3px solid #e0b100;padding:.4rem .6rem;margin:.3rem 0;
overflow-wrap:anywhere}
.p{color:#8a6d00;font-size:.8em;user-select:all}
.a{background:#fff3c4;border-bottom:1px dotted #b38600;padding:0 .2em;overflow-wrap:anywhere}
.op{white-space:pre-wrap;background:#e9f4ff;border-left:3px solid #2b7bd0;padding:.4rem .6rem;margin:.3rem 0}
.note{color:#555;font-size:.9em}.flash{background:#e7f7e9;border:1px solid #9cd3a3;padding:.5rem;margin-bottom:1rem}
.err{background:#fdecec;border-color:#e3a1a1}form.inline{display:inline}
textarea{width:100%;min-height:6rem;font:inherit}input[type=text]{width:100%}
.cards{display:flex;gap:1rem;flex-wrap:wrap}.card{background:#fff;padding:1rem;border:1px solid #ddd;min-width:12rem}
.card b{font-size:1.6rem;display:block}pre{white-space:pre-wrap;font-size:.85em;background:#fff;padding:.6rem}
button{cursor:pointer}
"""

NAV = [
    ("Overview", "/admin/console"),
    ("Requests", "/admin/console/requests"),
    ("Reports", "/admin/console/reports"),
    ("Capabilities", "/admin/console/capabilities"),
    ("Directory", "/admin/console/directory"),
    ("Board", "/admin/console/board"),
    ("Log", "/admin/console/log"),
]


AGENT_PREFIX = "[agent] "


def agent_text(text: str | None) -> str:
    """Untrusted text, escaped and marked as written by an agent, also in plain text so copying keeps it."""
    return f'<div class="agent"><span class="p">{AGENT_PREFIX}</span>{e(text or "")}</div>'


def agent_word(text: str | None) -> str:
    """A short agent-written value (handle, title, tag) inside a line: escaped and marked."""
    if not text:
        return '<span class="note">none</span>'
    return f'<span class="a" title="written by an agent"><span class="p">{AGENT_PREFIX}</span>{e(text)}</span>'


def _page(title: str, body: str, csrf: str | None, msg: str | None = None, error: bool = False) -> HTMLResponse:
    nav = "".join(f'<a href="{href}">{label}</a>' for label, href in NAV)
    logout = (
        f'<form class="inline" method="post" action="/admin/logout">{_csrf(csrf)}<button>Log out</button></form>'
        if csrf
        else ""
    )
    flash = f'<div class="flash{" err" if error else ""}">{e(msg)}</div>' if msg else ""
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{e(title)} · agent-helper console</title>'
        '<link rel="stylesheet" href="/admin/console.css"></head><body>'
        f"<header><strong>agent-helper {e(__version__)}</strong>{nav if csrf else ''}{logout}</header>"
        f"<main><h1>{e(title)}</h1>{flash}{body}</main></body></html>"
    )
    return HTMLResponse(doc, headers={"Content-Security-Policy": CSP, "Cache-Control": "no-store"})


def _csrf(token: str | None) -> str:
    return f'<input type="hidden" name="csrf" value="{e(token or "")}">'


def _options(values: list[str], selected: str | None = None) -> str:
    return "".join(f"<option{' selected' if v == selected else ''}>{e(v)}</option>" for v in values)


_FLASH_KEY = secrets.token_bytes(32)


FLASH_SECONDS = 120


def _flash_sig(msg: str, error: bool, issued: int) -> str:
    data = f"{issued}:{int(error)}:{msg}".encode()
    return hmac.new(_FLASH_KEY, data, hashlib.sha256).hexdigest()[:32]


def _redirect(path: str, msg: str | None = None, error: bool = False) -> RedirectResponse:
    """Redirect after a form. The message is signed, so a crafted link cannot put text into the console."""
    if msg:
        msg = msg[:300]
        issued = int(time.time())
        path += ("&" if "?" in path else "?") + f"msg={quote(msg)}&t={issued}&sig={_flash_sig(msg, error, issued)}"
        path += "&err=1" if error else ""
    return RedirectResponse(path, status_code=303)


async def _form(request: Request) -> dict[str, str]:
    raw = await request.body()
    try:
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=50)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "bad form") from None
    return {k: v[-1] for k, v in parsed.items()}


def _problem(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc'])) or 'input'}: {err['msg']}" for err in exc.errors())


def _page_of(action_path: str) -> str:
    """The page a form was posted from, so an error can be shown there."""
    parts = action_path.rstrip("/").split("/")  # ['', 'admin', 'console', collection, id?, action?]
    if len(parts) <= 3:
        return "/admin/console"
    collection = {"capability-requests": "capabilities"}.get(parts[3], parts[3])
    if collection == "requests" and len(parts) >= 5:
        return f"/admin/console/requests/{quote(parts[4])}"
    return f"/admin/console/{collection}"


def _int_or_raw(value: str) -> int | str | None:
    """An integer if `value` is a plain decimal number, else the raw text (which then fails validation)."""
    if not value:
        return None
    try:
        return int(value) if value.isascii() and value.isdigit() and len(value) <= 6 else value
    except ValueError:
        return value


def _latest(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    if last["sender"] == "operator":
        return f'<span class="note">you:</span> {e(last["body"][:120])}'
    return agent_word(last["body"][:120])


def _parse[M: BaseModel](model: type[M], data: dict[str, Any]) -> M:
    return model.model_validate(data)


# --- the router -----------------------------------------------------------------------------------------


def build_console(
    settings: Settings, store: Store, catalog: Catalog, notifier: Callable[[], Any], mail_limits: MailLimits
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    sessions = Sessions()

    def secure_cookie(request: Request) -> bool:
        """`Secure` by default. The one automatic exception is a plain-http login on the admin port, which is
        published on the host only (reached by SSH tunnel or a LAN port forward; Safari drops Secure cookies
        over plain http). Anything else needs ADMIN_COOKIE_SECURE=false: a host name or the absence of proxy
        headers proves nothing, since a plain proxy_pass to 127.0.0.1 looks exactly like a tunnel."""
        if settings.admin_cookie_secure is not None:
            return settings.admin_cookie_secure
        if request.url.scheme == "https":
            return True
        server = request.scope.get("server")
        on_admin_port = settings.admin_port is not None and server is not None and server[1] == settings.admin_port
        return not on_admin_port

    def enabled() -> None:
        if not settings.admin_enabled:
            raise HTTPException(404, "Not Found")

    def session(request: Request) -> str | None:
        return sessions.csrf_for(request.cookies.get(COOKIE))

    def flash(request: Request) -> tuple[str | None, bool]:
        msg, error = request.query_params.get("msg"), request.query_params.get("err") == "1"
        try:
            issued = int(request.query_params.get("t", ""))
        except ValueError:
            return None, False
        if not msg or not 0 <= time.time() - issued <= FLASH_SECONDS:
            return None, False
        if not hmac.compare_digest(
            request.query_params.get("sig", "").encode(), _flash_sig(msg, error, issued).encode()
        ):
            return None, False
        return msg, error

    def view(handler: Callable[[Request, str], HTMLResponse]) -> Callable[[Request], Any]:
        """A GET page that needs a session."""

        def wrapped(request: Request) -> Response:
            enabled()
            csrf = session(request)
            if csrf is None:
                return RedirectResponse("/admin/login", status_code=303)
            return handler(request, csrf)

        return wrapped

    def action(handler: Callable[[Request, dict[str, str]], Response]) -> Callable[[Request], Any]:
        """A POST form that needs a session and a matching CSRF token."""

        async def wrapped(request: Request) -> Response:
            enabled()
            csrf = session(request)
            if csrf is None:
                return RedirectResponse("/admin/login", status_code=303)
            form = await _form(request)
            if not hmac.compare_digest(form.get("csrf", "").encode(), csrf.encode()):
                raise HTTPException(403, "invalid form token; reload the page")
            try:
                # Handlers use the database and may call the webhook; keep them off the event loop.
                return await run_in_threadpool(handler, request, form)
            except ValidationError as exc:
                return _redirect(_page_of(request.url.path), _problem(exc), error=True)
            except MailRefused as exc:
                return _redirect(_page_of(request.url.path), str(exc), error=True)

        return wrapped

    # --- login ----------------------------------------------------------------------------------------

    @router.get("/admin/console.css")
    def stylesheet() -> Response:
        enabled()
        return Response(STYLE, media_type="text/css", headers={"Cache-Control": "max-age=3600"})

    @router.get("/admin/")
    @router.get("/admin")
    def admin_root(request: Request) -> Response:
        enabled()
        return RedirectResponse("/admin/console" if session(request) else "/admin/login", status_code=303)

    @router.get("/admin/login")
    def login_page(request: Request) -> Response:
        enabled()
        msg, err = flash(request)
        body = (
            '<form method="post" action="/admin/login"><p><label>Admin secret<br>'
            '<input type="password" name="secret" autocomplete="current-password" required></label></p>'
            "<p><button>Log in</button></p></form>"
            '<p class="note">The session ends after 12 hours or when the service restarts.</p>'
        )
        return _page("Log in", body, None, msg, err)

    @router.post("/admin/login")
    async def login(request: Request) -> Response:
        enabled()
        form = await _form(request)
        given = form.get("secret", "").encode()
        if not hmac.compare_digest(given, (settings.admin_secret or "").encode()):
            await asyncio.sleep(LOGIN_DELAY_SECONDS)  # on top of the write rate limit; never blocks others
            return _redirect("/admin/login", "Wrong secret.", error=True)
        token, _ = sessions.create()
        response = RedirectResponse("/admin/console", status_code=303)
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_SECONDS,
            path="/admin",
            httponly=True,
            secure=secure_cookie(request),
            samesite="strict",
        )
        return response

    @router.post("/admin/logout")
    async def logout(request: Request) -> Response:
        enabled()
        csrf = session(request)
        form = await _form(request)
        if csrf and hmac.compare_digest(form.get("csrf", "").encode(), csrf.encode()):
            sessions.drop(request.cookies.get(COOKIE))
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(COOKIE, path="/admin")
        return response

    # --- overview -------------------------------------------------------------------------------------

    def overview(request: Request, csrf: str) -> HTMLResponse:
        counts = [
            ("Open requests", len(store.list_requests("open", 500)), "/admin/console/requests?status=open"),
            ("Quarantined reports", len(store.list_reports("quarantined", 500)), "/admin/console/reports"),
            (
                "Open capability requests",
                len([r for r in store.list_capability_requests_admin("open", 500) if not r["hidden_reason"]]),
                "/admin/console/capabilities",
            ),
            ("Directory profiles", len(store.list_profiles_admin(500)), "/admin/console/directory"),
            ("Board entries", store.board_head()["seq"], "/admin/console/board"),
        ]
        cards = "".join(f'<a class="card" href="{href}">{e(label)}<b>{n}</b></a>' for label, n, href in counts)
        notify = "on" if notifier().enabled else "off (NOTIFY_WEBHOOK_URL not set)"
        body = (
            f'<div class="cards">{cards}</div><h2>Notifications</h2><p>Webhook: {e(notify)}</p>'
            f'<form method="post" action="/admin/console/notify-test">{_csrf(csrf)}'
            "<button>Send a test notification</button></form>"
        )
        msg, err = flash(request)
        return _page("Overview", body, csrf, msg, err)

    router.add_api_route("/admin/console", view(overview), methods=["GET"])

    def notify_test(request: Request, form: dict[str, str]) -> Response:
        result = notifier().send_test()
        if result.ok:
            return _redirect("/admin/console", "Test notification delivered.")
        return _redirect("/admin/console", f"Not delivered: {result.error or result.status}", error=True)

    router.add_api_route("/admin/console/notify-test", action(notify_test), methods=["POST"])

    # --- requests -------------------------------------------------------------------------------------

    def requests_page(request: Request, csrf: str) -> HTMLResponse:
        status = request.query_params.get("status") or None
        if status not in (None, "open", "answered", "closed"):
            status = None
        rows = "".join(
            f'<tr><td><a href="/admin/console/requests/{e(r["id"])}">{e(r["id"])}</a></td><td>{e(r["status"])}</td>'
            f"<td>{e(r['created_at'])}</td><td>{agent_word(r['handle'])}</td><td>{len(r['messages'])}</td>"
            f"<td>{_latest(r['messages'])}</td></tr>"
            for r in store.list_requests(status, 200)
        )
        filters = " · ".join(
            f'<a href="/admin/console/requests{"?status=" + s if s else ""}">{s or "all"}</a>'
            for s in ("", "open", "answered", "closed")
        )
        body = (
            f'<p>{filters}</p><p class="note">"open" means the agent wrote last and waits for you.</p>'
            "<table><tr><th>Id</th><th>Status</th><th>Created</th><th>Handle</th><th>Messages</th>"
            f"<th>Latest message</th></tr>{rows}</table>"
        )
        msg, err = flash(request)
        return _page("Requests", body, csrf, msg, err)

    router.add_api_route("/admin/console/requests", view(requests_page), methods=["GET"])

    def request_page(request: Request, csrf: str) -> HTMLResponse:
        req_id = request.path_params["req_id"]
        found = store.get_request_admin(req_id)
        if found is None:
            raise HTTPException(404, "no such request")
        convo = "".join(
            f'<p class="note">{e(m["sender"])}, {e(m["created_at"])}</p>'
            + (agent_text(m["body"]) if m["sender"] == "agent" else f'<div class="op">{e(m["body"])}</div>')
            for m in found["messages"]
        )
        base = f"/admin/console/requests/{e(req_id)}"
        body = (
            f"<p>Status: <b>{e(found['status'])}</b> · handle: {agent_word(found['handle'])} · contact hint: "
            f"{agent_word(found['contact_hint'])}</p>{convo}"
            f'<h2>Reply</h2><form method="post" action="{base}/reply">{_csrf(csrf)}'
            '<textarea name="message" required maxlength="8000"></textarea>'
            f'<p>Set status: <select name="status">{_options(["answered", "open", "closed"], "answered")}</select> '
            "<button>Send reply</button></p></form>"
            f'<h2>Refer to another agent</h2><form method="post" action="{base}/refer">{_csrf(csrf)}'
            '<p><label>Handle <input type="text" name="to" required maxlength="64"></label></p>'
            '<textarea name="note" required maxlength="8000" placeholder="Why this agent can help"></textarea>'
            '<p><label><input type="checkbox" name="include_request_text" value="1"> share the request text</label> '
            '<label><input type="checkbox" name="include_requester_handle" value="1"> share the requester\'s handle'
            "</label> <button>Refer</button></p></form>"
        )
        msg, err = flash(request)
        return _page(f"Request {req_id}", body, csrf, msg, err)

    router.add_api_route("/admin/console/requests/{req_id}", view(request_page), methods=["GET"])

    def reply(request: Request, form: dict[str, str]) -> Response:
        req_id = request.path_params["req_id"]
        body = _parse(OperatorReplyIn, {"message": form.get("message", ""), "status": form.get("status", "answered")})
        if store.add_operator_reply(req_id, body.message, body.status) is None:
            raise HTTPException(404, "no such request")
        return _redirect(f"/admin/console/requests/{req_id}", "Reply sent.")

    router.add_api_route("/admin/console/requests/{req_id}/reply", action(reply), methods=["POST"])

    def refer(request: Request, form: dict[str, str]) -> Response:
        req_id = request.path_params["req_id"]
        body = _parse(
            ReferralIn,
            {
                "to": form.get("to", ""),
                "note": form.get("note", ""),
                "include_request_text": form.get("include_request_text") == "1",
                "include_requester_handle": form.get("include_requester_handle") == "1",
            },
        )
        try:
            found = store.refer_request(
                req_id, body.to, body.note, body.include_request_text, mail_limits, body.include_requester_handle
            )
        except MailRefused as exc:
            return _redirect(f"/admin/console/requests/{req_id}", str(exc), error=True)
        if found is None:
            raise HTTPException(404, "no such request")
        return _redirect(f"/admin/console/requests/{req_id}", f"Referred to {body.to}.")

    router.add_api_route("/admin/console/requests/{req_id}/refer", action(refer), methods=["POST"])

    # --- reports --------------------------------------------------------------------------------------

    def reports_page(request: Request, csrf: str) -> HTMLResponse:
        status = request.query_params.get("status", "quarantined")
        if status not in ("quarantined", "accepted", "rejected", "duplicate", "all"):
            status = "quarantined"
        items = store.list_reports(None if status == "all" else status, 200)
        rows = "".join(
            f"<tr><td>{e(r['id'])}<br><span class='note'>{e(r['created_at'])} · {e(r['kind'])} · "
            f"{e(r['status'])}</span>{agent_text(r['body'])}"
            f'<form method="post" action="/admin/console/reports/{e(r["id"])}/decision">{_csrf(csrf)}'
            f'<select name="status">{_options(["accepted", "rejected", "duplicate", "quarantined"], r["status"])}'
            f'</select> <input type="text" name="note" maxlength="2000" placeholder="note" '
            f'value="{e(r["operator_note"] or "")}"> <button>Save</button></form></td></tr>'
            for r in items
        )
        filters = " · ".join(
            f'<a href="/admin/console/reports?status={s}">{s}</a>'
            for s in ("quarantined", "accepted", "rejected", "duplicate", "all")
        )
        body = (
            f"<p>{filters}</p><p class='note'>Reports are untrusted. Nothing here is published "
            f"automatically.</p><table>{rows}</table>"
        )
        msg, err = flash(request)
        return _page("Reports", body, csrf, msg, err)

    router.add_api_route("/admin/console/reports", view(reports_page), methods=["GET"])

    def decide_report(request: Request, form: dict[str, str]) -> Response:
        rep_id = request.path_params["rep_id"]
        body = _parse(ReportDecisionIn, {"status": form.get("status"), "note": form.get("note") or None})
        if store.set_report_status(rep_id, body.status, body.note) is None:
            raise HTTPException(404, "no such report")
        return _redirect("/admin/console/reports", f"Report {rep_id}: {body.status}.")

    router.add_api_route("/admin/console/reports/{rep_id}/decision", action(decide_report), methods=["POST"])

    # --- capabilities ---------------------------------------------------------------------------------

    def capabilities_page(request: Request, csrf: str) -> HTMLResponse:
        statuses = ["open", "planned", "in_progress", "available", "declined", "duplicate"]
        rows = []
        for r in store.list_capability_requests_admin(None, 200):
            base = f"/admin/console/capability-requests/{e(r['id'])}"
            hidden = (
                f"<p class='note'>Hidden: {e(r['hidden_reason'])}</p>"
                f'<form class="inline" method="post" action="{base}/unhide">{_csrf(csrf)}<button>Unhide</button></form>'
                if r["hidden_reason"]
                else f'<form class="inline" method="post" action="{base}/hide">{_csrf(csrf)}'
                '<input type="text" name="reason" required maxlength="2000" placeholder="reason to hide"> '
                "<button>Hide</button></form>"
            )
            rows.append(
                f"<tr><td><span class='note'>{e(r['id'])} · {r['votes']} vote(s) · by "
                f"{agent_word(r['requested_by'])}</span>{agent_text(r['title'])}{agent_text(r['description'])}"
                f'<form method="post" action="{base}/decision">{_csrf(csrf)}'
                f'<select name="status">{_options(statuses, r["status"])}</select> '
                f'<input type="text" name="note" maxlength="2000" placeholder="note" '
                f'value="{e(r["operator_note"] or "")}"> '
                f'<input type="text" name="capability_id" maxlength="64" placeholder="catalog id" '
                f'value="{e(r["capability_id"] or "")}"> <button>Save</button></form>{hidden}</td></tr>'
            )
        catalog_rows = "".join(
            f"<tr><td>{e(c.id)}</td><td>{e(c.title)}</td><td>{e(c.category)}</td><td>{e(c.availability)}</td>"
            f"<td>{e(c.source)}</td></tr>"
            for c in catalog.entries()
        )
        body = (
            "<h2>Requested by agents (most votes first)</h2>"
            f"<table>{''.join(rows)}</table><h2>Catalog</h2>"
            "<p class='note'>Add or change entries with PUT /admin/v1/capabilities/{id}.</p>"
            "<table><tr><th>Id</th><th>Title</th><th>Category</th><th>Availability</th><th>Source</th></tr>"
            f"{catalog_rows}</table>"
        )
        msg, err = flash(request)
        return _page("Capabilities", body, csrf, msg, err)

    router.add_api_route("/admin/console/capabilities", view(capabilities_page), methods=["GET"])

    def decide_capability(request: Request, form: dict[str, str]) -> Response:
        req_id = request.path_params["req_id"]
        body = _parse(
            CapabilityDecisionIn,
            {
                "status": form.get("status"),
                "note": form.get("note") or None,
                "capability_id": form.get("capability_id") or None,
            },
        )
        if body.capability_id is not None and catalog.get(body.capability_id) is None:
            return _redirect("/admin/console/capabilities", "That catalog id does not exist.", error=True)
        changes = {"status": body.status, "operator_note": body.note, "capability_id": body.capability_id}
        if store.update_capability_request(req_id, changes) is None:
            raise HTTPException(404, "no such capability request")
        return _redirect("/admin/console/capabilities", "Saved.")

    def hide_capability(request: Request, form: dict[str, str]) -> Response:
        body = _parse(HideIn, {"reason": form.get("reason", "")})
        if store.update_capability_request(request.path_params["req_id"], {"hidden_reason": body.reason}) is None:
            raise HTTPException(404, "no such capability request")
        return _redirect("/admin/console/capabilities", "Hidden.")

    def unhide_capability(request: Request, form: dict[str, str]) -> Response:
        if store.update_capability_request(request.path_params["req_id"], {"hidden_reason": None}) is None:
            raise HTTPException(404, "no such capability request")
        return _redirect("/admin/console/capabilities", "Visible again.")

    for suffix, handler in (("decision", decide_capability), ("hide", hide_capability), ("unhide", unhide_capability)):
        router.add_api_route(
            f"/admin/console/capability-requests/{{req_id}}/{suffix}", action(handler), methods=["POST"]
        )

    # --- directory ------------------------------------------------------------------------------------

    def directory_page(request: Request, csrf: str) -> HTMLResponse:
        rows = []
        for p in store.list_profiles_admin(500):
            base = f"/admin/console/directory/{quote(p['handle'])}"
            toggle = (
                f'<form method="post" action="{base}/unhide">{_csrf(csrf)}<span class="note">Hidden: '
                f"{e(p['hidden_reason'])}</span> <button>Unhide</button></form>"
                if p["hidden_reason"]
                else f'<form method="post" action="{base}/hide">{_csrf(csrf)}'
                '<input type="text" name="reason" required maxlength="2000" placeholder="reason to hide"> '
                "<button>Hide</button></form>"
            )
            details = "Offers: " + ", ".join(p["offers"]) + "\nNeeds: " + ", ".join(p["needs"])
            details += "\nTags: " + ", ".join(p["tags"]) + "\nContact: "
            details += ", ".join(f"{c['kind']}: {c['value']}" for c in p["contact"])
            rows.append(
                f"<tr><td>{agent_word(p['handle'])} <span class='note'>updated {e(p['updated_at'])}</span>"
                f"{agent_text(p['summary'])}{agent_text(details)}{toggle}</td></tr>"
            )
        body = f"<p class='note'>Profiles are written by agents and unverified.</p><table>{''.join(rows)}</table>"
        msg, err = flash(request)
        return _page("Directory", body, csrf, msg, err)

    router.add_api_route("/admin/console/directory", view(directory_page), methods=["GET"])

    def hide_profile(request: Request, form: dict[str, str]) -> Response:
        body = _parse(HideIn, {"reason": form.get("reason", "")})
        if not store.set_profile_hidden(request.path_params["handle"], body.reason):
            return _redirect("/admin/console/directory", "No such profile.", error=True)
        return _redirect("/admin/console/directory", "Profile hidden.")

    def unhide_profile(request: Request, form: dict[str, str]) -> Response:
        if not store.set_profile_hidden(request.path_params["handle"], None):
            return _redirect("/admin/console/directory", "No such profile.", error=True)
        return _redirect("/admin/console/directory", "Profile visible again.")

    router.add_api_route("/admin/console/directory/{handle}/hide", action(hide_profile), methods=["POST"])
    router.add_api_route("/admin/console/directory/{handle}/unhide", action(unhide_profile), methods=["POST"])

    # --- board ----------------------------------------------------------------------------------------

    def board_page(request: Request, csrf: str) -> HTMLResponse:
        head = store.board_head()
        try:
            before = int(request.query_params.get("before", head["seq"] + 1))
        except ValueError:
            before = head["seq"] + 1
        before = min(max(before, 1), head["seq"] + 1)
        start = max(0, before - 51)
        entries = list(reversed(store.list_board(start, max(1, before - 1 - start))))
        rows = []
        for x in entries:
            state = "hidden: " + (x["hidden_reason"] or "no reason") if x["hidden"] else ""
            state = state or ("expired" if x["expired"] else "")
            hide = (
                ""
                if x["hidden"] or x["expired"]
                else f'<form method="post" action="/admin/console/board/{x["seq"]}/hide">{_csrf(csrf)}'
                '<input type="text" name="reason" required maxlength="2000" placeholder="public reason to hide"> '
                "<button>Hide</button></form>"
            )
            author = "operator" if x["author"] == OPERATOR_HANDLE else agent_word(x["author"])
            meta = f"#{x['seq']} · v{x['v']} · {e(x['created_at'])} · {author}"
            meta += f" · topic: {agent_word(x['topic'])} · tags: {agent_word(', '.join(x['tags'] or []))}"
            meta += f" · expires: {e(x['expires_at'] or 'never')} {e(state)}"
            text = (
                ""
                if x["content"] is None
                else (
                    f'<div class="op">{e(x["content"])}</div>'
                    if x["author"] == OPERATOR_HANDLE
                    else agent_text(x["content"])
                )
            )
            rows.append(f"<tr><td><span class='note'>{meta}</span>{text}{hide}</td></tr>")
        older = f'<p><a href="/admin/console/board?before={start + 1}">older entries</a></p>' if start > 0 else ""
        body = (
            f"<p>Head: #{head['seq']} <code>{e(head['entry_hash'])}</code></p>"
            f'<form method="post" action="/admin/console/board/verify">{_csrf(csrf)}<button>Verify the whole chain'
            "</button></form>"
            f'<h2>Post as operator</h2><form method="post" action="/admin/console/board">{_csrf(csrf)}'
            '<p><input type="text" name="topic" maxlength="100" placeholder="topic (optional)"></p>'
            '<textarea name="content" required maxlength="4000"></textarea>'
            '<p><input type="text" name="tags" maxlength="440" placeholder="tags, comma separated (optional)"> '
            '<input type="text" name="expires_in_days" maxlength="4" placeholder="expires in days (optional)"> '
            "<button>Post publicly</button></p></form>"
            f"<h2>Entries</h2><table>{''.join(rows)}</table>{older}"
        )
        msg, err = flash(request)
        return _page("Board", body, csrf, msg, err)

    router.add_api_route("/admin/console/board", view(board_page), methods=["GET"])

    def post_board(request: Request, form: dict[str, str]) -> Response:
        tags = [t.strip() for t in form.get("tags", "").split(",") if t.strip()]
        days = form.get("expires_in_days", "").strip()
        body = _parse(
            OperatorBoardIn,
            {
                "content": form.get("content", ""),
                "topic": form.get("topic") or None,
                "tags": tags,
                "expires_in_days": _int_or_raw(days),
            },
        )
        entry, _ = store.append_board_entry(
            OPERATOR_HANDLE,
            body.topic,
            body.content,
            as_operator=True,
            tags=body.tags,
            expires_in_days=body.expires_in_days,
        )
        return _redirect("/admin/console/board", f"Posted as entry #{entry['seq']}.")

    def hide_board(request: Request, form: dict[str, str]) -> Response:
        body = _parse(HideIn, {"reason": form.get("reason", "")})
        seq = int(request.path_params["seq"])
        if seq > MAX_ID or store.hide_board_entry(seq, body.reason) is None:
            raise HTTPException(404, "no such entry")
        return _redirect("/admin/console/board", "Entry hidden; its hashes stay public.")

    def verify(request: Request, form: dict[str, str]) -> Response:
        entries: list[dict[str, Any]] = []
        after = 0
        while True:
            page = store.list_board(after, 200)
            entries += page
            if len(page) < 200:
                break
            after = page[-1]["seq"]
        result = board.verify_chain(entries)
        if result.ok and result.warnings:
            listed = "; ".join(result.warnings[:5]) + (" …" if len(result.warnings) > 5 else "")
            msg = f"Chain verified: {result.checked} entries, but {len(result.warnings)} warning(s): {listed}"
            return _redirect("/admin/console/board", msg, error=True)
        if result.ok:
            return _redirect("/admin/console/board", f"Chain verified: {result.checked} entries, head matches.")
        return _redirect("/admin/console/board", f"Chain broken at #{result.failed_seq}: {result.error}", error=True)

    router.add_api_route("/admin/console/board", action(post_board), methods=["POST"])
    router.add_api_route("/admin/console/board/verify", action(verify), methods=["POST"])
    router.add_api_route("/admin/console/board/{seq:int}/hide", action(hide_board), methods=["POST"])

    # --- log ------------------------------------------------------------------------------------------

    def log_page(request: Request, csrf: str) -> HTMLResponse:
        lines = "\n".join(reversed(_ring.lines))
        body = (
            "<p class='note'>The most recent log lines of this process, newest first. Method, path, status, "
            f"duration; no bodies, tokens or addresses (decision 0008).</p><pre>{e(lines)}</pre>"
        )
        return _page("Log", body, csrf)

    router.add_api_route("/admin/console/log", view(log_page), methods=["GET"])
    return router
