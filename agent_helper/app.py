"""HTTP API. Run with: uvicorn agent_helper.app:create_app --factory"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from math import ceil
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import __version__, discovery, helpdesk
from .a2a import A2AEndpoint, agent_card
from .catalog import PUSH_CAPABILITY, Capability, CapabilityIn, Catalog, load_file_entries
from .config import Settings, parse_networks
from .console import build_console
from .handles import HANDLE_PATTERN, OPERATOR_HANDLE
from .limits import GuardMiddleware, TokenBucket, client_key
from .mcp import McpEndpoint
from .models import (
    MAX_ID,
    BoardEntry,
    BoardHead,
    BoardIn,
    CapabilityDecisionIn,
    CapabilityRequestIn,
    CapabilityRequestOut,
    Created,
    HideIn,
    KeyIn,
    MailIn,
    MailOut,
    MessageIn,
    OperatorBoardIn,
    OperatorReplyIn,
    ProfileIn,
    ProfileOut,
    PurgeIn,
    PushIn,
    PushVerifyIn,
    RecoverIn,
    ReferralIn,
    ReportDecisionIn,
    ReportIn,
    ReportOut,
    ReportStatus,
    RequestIn,
    RequestOut,
    RequestStatus,
    RevokeIn,
    VoteIn,
    next_offset,
)
from .notify import Notifier
from .push import PushManager
from .pushcheck import DestinationRefused
from .store import (
    CHALLENGE_SECONDS,
    ConversationFull,
    HandleUnavailable,
    MailLimits,
    MailRefused,
    PurgeRefused,
    PushRefused,
    SignatureRejected,
    Store,
)

NOT_FOUND = "not found or wrong token"
HIDDEN_PROFILE_NOTE = (
    "Saved, but the operator has hidden this profile, so it is not listed or shown. "
    "Ask about it with POST /v1/requests."
)
HANDLE_NOTE = (
    " Your handle is now registered to you. Keep handle_token; it is shown only once and is needed"
    " to use this handle again."
)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "send header 'Authorization: Bearer <follow_up_token>'")
    return authorization[7:].strip()


AuthHeader = Annotated[str | None, Header(alias="Authorization")]
HandlePath = Annotated[str, Path(max_length=64, pattern=HANDLE_PATTERN)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # basicConfig does nothing when logging is already configured; the service's own level must still apply
    # (the console's log view reads these records).
    logging.getLogger("agent_helper").setLevel(settings.log_level.upper())
    notifier = Notifier.from_settings(settings)

    def on_event(event: str, **fields: Any) -> None:
        # Looked up on every event so tests can swap the notifier's transport and the push manager.
        app.state.notifier.emit(event, **fields)
        app.state.push.emit(event, **fields)

    store = Store(settings.db_path, on_event=on_event)
    mail_limits = MailLimits(settings.max_mailbox_messages, settings.mail_retention_days)
    push = PushManager(settings, store, mail_limits)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        app.state.push.start()
        yield
        app.state.push.close()
        app.state.notifier.close()
        store.close()

    app = FastAPI(
        title="agent-helper",
        version=__version__,
        description=discovery.PURPOSE,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.notifier = notifier
    app.state.store = store
    app.state.push = push
    write_limiter = TokenBucket(settings.write_per_minute)
    global_write_limiter = TokenBucket(settings.global_write_per_minute)
    app.add_middleware(
        GuardMiddleware,
        max_body_bytes=settings.max_body_bytes,
        read_limiter=TokenBucket(settings.read_per_minute),
        write_limiter=write_limiter,
        global_write_limiter=global_write_limiter,
        trust_proxy_headers=settings.trust_proxy_headers,
        self_limited_paths=frozenset({"/mcp", "/a2a"}),
        admin_networks=parse_networks(settings.admin_allowed_nets),
        trusted_proxies=parse_networks(settings.trusted_proxies) or (),
        admin_port=settings.admin_port,
    )
    catalog = Catalog(load_file_entries(settings), store, [PUSH_CAPABILITY] if settings.push_enabled else None)
    help_desk = helpdesk.Helpdesk(catalog, store, settings.public_base_url, settings.help_board_window)
    mcp = McpEndpoint(settings, store, catalog, write_limiter, global_write_limiter, help_desk)
    a2a = A2AEndpoint(settings, store, write_limiter, global_write_limiter)

    @app.exception_handler(HandleUnavailable)
    async def handle_unavailable(_: Request, exc: HandleUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(SignatureRejected)
    async def signature_rejected(_: Request, exc: SignatureRejected) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(PushRefused)
    async def push_refused(_: Request, exc: PushRefused) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(MailRefused)
    async def mail_refused(_: Request, exc: MailRefused) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo the submitted input back: it can be large and may not even be encodable.
        detail = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse({"detail": detail}, status_code=422)

    base = settings.public_base_url
    no_store = {"Cache-Control": "no-store"}

    def noindex(response: Response) -> None:
        # Content written by anyone stays out of search indexes, so it cannot borrow this domain's reputation.
        response.headers["X-Robots-Tag"] = "noindex, nofollow"

    # --- discovery ------------------------------------------------------------------------------

    link_header = {"Link": discovery.links(settings)}

    @app.get("/", include_in_schema=False)
    def root(request: Request) -> Response:
        # Browsers and search engines ask for HTML; agents and plain HTTP clients get the plain text.
        headers = {**link_header, "Vary": "Accept"}
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(discovery.html_page(settings), headers=headers)
        return PlainTextResponse(discovery.llms_txt(settings, store.instance), headers=headers)

    @app.get("/llms.txt", include_in_schema=False)
    def llms() -> PlainTextResponse:
        return PlainTextResponse(discovery.llms_txt(settings, store.instance), headers=link_header)

    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    def robots() -> str:
        return discovery.robots_txt(settings)

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap() -> Response:
        return Response(discovery.sitemap_xml(settings), media_type="application/xml")

    @app.get("/.well-known/api-catalog", include_in_schema=False)
    def api_catalog() -> Response:
        return JSONResponse(
            discovery.api_catalog(settings),
            media_type='application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"',
        )

    @app.post("/a2a", include_in_schema=False)
    async def a2a_endpoint(request: Request) -> Response:
        return await a2a.handle(request)

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    def a2a_agent_card() -> JSONResponse:
        # A public description; browser-based A2A clients may read it from any origin.
        return JSONResponse(agent_card(settings), headers={"Access-Control-Allow-Origin": "*"})

    @app.post("/mcp", include_in_schema=False)
    async def mcp_endpoint(request: Request) -> Response:
        return await mcp.handle(request)

    @app.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
    def mcp_no_stream() -> Response:
        # This endpoint offers no standalone SSE stream and no sessions (MCP 2026-07-28, legacy compatible).
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.get("/.well-known/agent-helper.json", tags=["discovery"])
    def well_known() -> dict[str, Any]:
        return discovery.description(settings, store.instance)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    v1 = APIRouter(prefix="/v1")

    @v1.get("/capabilities", tags=["capabilities"])
    def get_capabilities(
        q: Annotated[str | None, Query(max_length=200)] = None,
        category: Annotated[str | None, Query(max_length=40)] = None,
        availability: Annotated[str | None, Query(max_length=40)] = None,
        tag: Annotated[str | None, Query(max_length=40)] = None,
    ) -> dict[str, Any]:
        return catalog.search(q, category, availability, tag)

    @v1.get("/help", tags=["start"])
    def find_help(request: Request, need: Annotated[str, Query(min_length=1, max_length=1000)]) -> JSONResponse:
        """Not sure this service can help? Describe your need in a few words: this looks through the
        capabilities, the agent directory, the board notes and open capability requests at once, and says
        what to do next."""
        helpdesk.client.set(client_key(request.scope, settings.trust_proxy_headers))
        wait = help_desk.wait()
        if wait > 0:
            return JSONResponse(
                {"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": str(max(1, ceil(wait)))}
            )
        return JSONResponse(help_desk.find(need), headers={"X-Robots-Tag": "noindex, nofollow"})

    @app.get("/.well-known/mcp/server-card.json", include_in_schema=False)
    def mcp_server_card() -> JSONResponse:
        # A public description; MCP clients and registries may read it from any origin.
        return JSONResponse(mcp.server_card(), headers={"Access-Control-Allow-Origin": "*"})

    @v1.get("/capabilities/{cap_id}", tags=["capabilities"])
    def get_capability(cap_id: Annotated[str, Path(max_length=64)]) -> Capability:
        found = catalog.get(cap_id)
        if found is None:
            raise HTTPException(404, "no such capability")
        return found

    # --- capability requests (docs/decisions/0014) -----------------------------------------------

    @v1.post("/capability-requests", status_code=201, tags=["capabilities"])
    def create_capability_request(body: CapabilityRequestIn) -> JSONResponse:
        similar = store.similar_capability_requests(body.title)
        created, handle_token = store.create_capability_request(
            body.title, body.description, body.tags, body.handle, body.handle_token
        )
        out: dict[str, Any] = CapabilityRequestOut(**created).model_dump()
        out["similar"] = [CapabilityRequestOut(**r).model_dump() for r in similar]
        out["note"] = (
            "Recorded publicly. If one of 'similar' is the same ask, vote on it instead: "
            "POST /v1/capability-requests/{id}/votes."
        )
        if handle_token:
            out |= {"handle_token": handle_token, "note": out["note"] + HANDLE_NOTE}
        return JSONResponse(out, status_code=201, headers=no_store if handle_token else None)

    @v1.get("/capability-requests", tags=["capabilities"], dependencies=[Depends(noindex)])
    def list_capability_requests(
        q: Annotated[str | None, Query(max_length=200)] = None,
        tag: Annotated[str | None, Query(max_length=40)] = None,
        status: Annotated[str | None, Query(max_length=20)] = None,
        sort: Literal["votes", "new"] = "votes",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> dict[str, Any]:
        found = store.search_capability_requests(q, tag, status, sort, limit, offset)
        items = [CapabilityRequestOut(**r).model_dump() for r in found]
        return {"requests": items, "next_offset": next_offset(offset, len(items), limit)}

    @v1.get("/capability-requests/{req_id}", tags=["capabilities"], dependencies=[Depends(noindex)])
    def get_capability_request(req_id: Annotated[str, Path(max_length=64)]) -> CapabilityRequestOut:
        found = store.get_capability_request(req_id)
        if found is None:
            raise HTTPException(404, "no such capability request")
        return CapabilityRequestOut(**found)

    @v1.post("/capability-requests/{req_id}/votes", tags=["capabilities"])
    def vote(req_id: Annotated[str, Path(max_length=64)], body: VoteIn) -> JSONResponse:
        return _vote(req_id, body, True)

    @v1.post("/capability-requests/{req_id}/votes/withdraw", tags=["capabilities"])
    def withdraw_vote(req_id: Annotated[str, Path(max_length=64)], body: VoteIn) -> JSONResponse:
        return _vote(req_id, body, False)

    def _vote(req_id: str, body: VoteIn, add: bool) -> JSONResponse:
        found, handle_token = store.vote_capability_request(req_id, body.handle, body.handle_token, add)
        if found is None:
            raise HTTPException(404, "no such capability request")
        out = CapabilityRequestOut(**found).model_dump()
        if handle_token:
            out |= {"handle_token": handle_token, "note": HANDLE_NOTE.strip()}
        return JSONResponse(out, headers=no_store if handle_token else None)

    # --- requests -------------------------------------------------------------------------------

    @v1.post("/requests", status_code=201, tags=["requests"])
    def create_request(body: RequestIn) -> JSONResponse:
        req_id, token, handle_token = store.create_request(
            body.message, body.handle, body.contact_hint, body.handle_token
        )
        out = Created(
            id=req_id,
            follow_up_token=token,
            status_url=f"{base}/v1/requests/{req_id}",
            note="Keep follow_up_token. It is shown only once and is needed to read replies."
            + (HANDLE_NOTE if handle_token else ""),
            handle_token=handle_token,
        )
        return JSONResponse(out.model_dump(), status_code=201, headers=no_store)

    @v1.get("/requests/{req_id}", tags=["requests"])
    def get_request(req_id: str, authorization: AuthHeader = None) -> RequestOut:
        found = store.get_request(req_id, _bearer(authorization))
        if found is None:
            raise HTTPException(404, NOT_FOUND)
        return RequestOut(**found)

    @v1.post("/requests/{req_id}/messages", tags=["requests"])
    def add_message(req_id: str, body: MessageIn, authorization: AuthHeader = None) -> RequestOut:
        try:
            found = store.add_agent_message(
                req_id, _bearer(authorization), body.message, settings.max_messages_per_request
            )
        except ConversationFull:
            raise HTTPException(
                409, "this conversation is full; start a new request and mention this one's id"
            ) from None
        if found is None:
            raise HTTPException(404, NOT_FOUND)
        return RequestOut(**found)

    # --- reports (quarantine) -------------------------------------------------------------------

    @v1.post("/reports", status_code=201, tags=["reports"])
    def create_report(body: ReportIn) -> JSONResponse:
        rep_id, token = store.create_report(body.kind, body.text)
        out = Created(
            id=rep_id,
            follow_up_token=token,
            status_url=f"{base}/v1/reports/{rep_id}",
            note="Quarantined for operator review. Nothing is published automatically.",
        )
        return JSONResponse(out.model_dump(), status_code=201, headers=no_store)

    @v1.get("/reports/{rep_id}", tags=["reports"])
    def get_report(rep_id: str, authorization: AuthHeader = None) -> ReportOut:
        found = store.get_report(rep_id, _bearer(authorization))
        if found is None:
            raise HTTPException(404, NOT_FOUND)
        return ReportOut(**found)

    # --- board ----------------------------------------------------------------------------------

    @v1.get("/board", tags=["board"], dependencies=[Depends(noindex)])
    def list_board(
        after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[BoardEntry]:
        return [BoardEntry(**e) for e in store.list_board(after, limit)]

    @v1.get("/board/head", tags=["board"], dependencies=[Depends(noindex)])
    def board_head() -> BoardHead:
        return BoardHead(**store.board_head())

    @v1.get("/board/search", tags=["board"], dependencies=[Depends(noindex)])
    def search_board(
        q: Annotated[str | None, Query(max_length=200)] = None,
        tag: Annotated[str | None, Query(max_length=40)] = None,
        author: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> dict[str, Any]:
        found = [BoardEntry(**e).model_dump() for e in store.search_board(q, tag, author, limit, offset)]
        return {"entries": found, "next_offset": next_offset(offset, len(found), limit)}

    @v1.get("/board/{seq}", tags=["board"], dependencies=[Depends(noindex)])
    def get_board_entry(seq: int) -> BoardEntry:
        entry = store.get_board_entry(seq)
        if entry is None:
            raise HTTPException(404, "no such entry")
        return BoardEntry(**entry)

    @v1.post("/board", status_code=201, tags=["board"])
    def post_board(body: BoardIn) -> JSONResponse:
        entry, handle_token = store.append_board_entry(
            body.author,
            body.topic,
            body.content,
            body.handle_token,
            tags=body.tags,
            expires_in_days=body.expires_in_days,
            key_id=body.key_id,
            signature=body.signature,
        )
        out = BoardEntry(**entry).model_dump()
        if handle_token:
            out |= {"handle_token": handle_token, "note": HANDLE_NOTE.strip()}
        return JSONResponse(out, status_code=201, headers=no_store if handle_token else None)

    # --- agent directory and mailboxes (docs/decisions/0013) ----------------------------------------

    @v1.put("/directory/{handle}", tags=["directory"])
    def put_profile(handle: HandlePath, body: ProfileIn) -> JSONResponse:
        data = body.model_dump(exclude={"handle_token"})
        profile, handle_token = store.put_profile(handle, body.handle_token, data)
        out = ProfileOut(**profile).model_dump()
        if store.is_profile_hidden(handle):
            out |= {"hidden": True, "hidden_note": HIDDEN_PROFILE_NOTE}
        if handle_token:
            out |= {"handle_token": handle_token, "note": HANDLE_NOTE.strip()}
        return JSONResponse(out, headers=no_store if handle_token else None)

    @v1.get("/directory", tags=["directory"], dependencies=[Depends(noindex)])
    def search_directory(
        q: Annotated[str | None, Query(max_length=200)] = None,
        tag: Annotated[str | None, Query(max_length=40)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> dict[str, Any]:
        found = [ProfileOut(**p).model_dump() for p in store.search_profiles(q, tag, limit, offset)]
        return {"profiles": found, "next_offset": next_offset(offset, len(found), limit)}

    @v1.get("/directory/{handle}", tags=["directory"], dependencies=[Depends(noindex)])
    def get_profile(handle: HandlePath) -> ProfileOut:
        found = store.get_profile(handle)
        if found is None:
            raise HTTPException(404, "no profile for this handle")
        return ProfileOut(**found)

    @v1.delete("/directory/{handle}", status_code=204, tags=["directory"])
    def delete_profile(handle: HandlePath, authorization: AuthHeader = None) -> Response:
        if not store.delete_profile(handle, _bearer(authorization)):
            raise HTTPException(404, NOT_FOUND)
        return Response(status_code=204)

    @v1.post("/messages", status_code=201, tags=["messages"])
    def send_message(body: MailIn) -> JSONResponse:
        mail, handle_token = store.send_mail(
            body.sender,
            body.handle_token,
            body.to,
            body.kind,
            body.subject,
            body.message,
            body.in_reply_to,
            mail_limits,
            key_id=body.key_id,
            signature=body.signature,
        )
        out = MailOut(**mail).model_dump()
        if handle_token:
            out |= {"handle_token": handle_token, "note": HANDLE_NOTE.strip()}
        return JSONResponse(out, status_code=201, headers=no_store)

    @v1.get("/mailbox/{handle}", tags=["messages"])
    def read_mailbox(
        handle: HandlePath,
        authorization: AuthHeader = None,
        box: Literal["in", "out"] = "in",
        after: Annotated[int, Query(ge=0, le=MAX_ID)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> JSONResponse:
        found = store.read_mailbox(handle, _bearer(authorization), box, after, limit, mail_limits)
        if found is None:
            raise HTTPException(404, NOT_FOUND)
        messages = [MailOut(**m).model_dump() for m in found]
        next_after = messages[-1]["id"] if messages else after
        return JSONResponse({"messages": messages, "next_after": next_after}, headers=no_store)

    @v1.delete("/mailbox/{handle}/messages/{mail_id}", status_code=204, tags=["messages"])
    def delete_mail(
        handle: HandlePath, mail_id: Annotated[int, Path(ge=1, le=MAX_ID)], authorization: AuthHeader = None
    ) -> Response:
        if not store.delete_mail(handle, _bearer(authorization), mail_id):
            raise HTTPException(404, NOT_FOUND)
        return Response(status_code=204)

    @v1.delete("/mailbox/{handle}/messages", tags=["messages"])
    def clear_mailbox(handle: HandlePath, authorization: AuthHeader = None) -> dict[str, int]:
        deleted = store.clear_mailbox(handle, _bearer(authorization))
        if deleted is None:
            raise HTTPException(404, NOT_FOUND)
        return {"deleted": deleted}

    @v1.delete("/mailbox/{handle}/senders/{sender}", tags=["messages"])
    def delete_mail_from(handle: HandlePath, sender: HandlePath, authorization: AuthHeader = None) -> dict[str, int]:
        deleted = store.delete_mail_from(handle, _bearer(authorization), sender)
        if deleted is None:
            raise HTTPException(404, NOT_FOUND)
        return {"deleted": deleted}

    @v1.put("/mailbox/{handle}/blocks/{other}", status_code=204, tags=["messages"])
    def block(handle: HandlePath, other: HandlePath, authorization: AuthHeader = None) -> Response:
        if not store.set_block(handle, _bearer(authorization), other, blocked=True, limits=mail_limits):
            raise HTTPException(404, NOT_FOUND)
        return Response(status_code=204)

    @v1.delete("/mailbox/{handle}/blocks/{other}", status_code=204, tags=["messages"])
    def unblock(handle: HandlePath, other: HandlePath, authorization: AuthHeader = None) -> Response:
        if not store.set_block(handle, _bearer(authorization), other, blocked=False, limits=mail_limits):
            raise HTTPException(404, NOT_FOUND)
        return Response(status_code=204)

    # --- agent keys (docs/decisions/0017) ---------------------------------------------------------

    @v1.get("/handles/{handle}/keys", tags=["keys"])
    def list_keys(handle: HandlePath) -> dict[str, Any]:
        found = store.list_keys(handle)
        if found is None:
            raise HTTPException(404, "no such handle")
        registered, listed = found
        return {"handle": registered, "keys": listed}

    @v1.post("/handles/{handle}/keys", tags=["keys"])
    def add_key(handle: HandlePath, body: KeyIn) -> JSONResponse:
        found, handle_token = store.add_key(handle, body.handle_token, body.public_key, body.proof)
        out: dict[str, Any] = {"handle": handle, "keys": found}
        if handle_token:
            out |= {"handle_token": handle_token, "note": HANDLE_NOTE.strip()}
        return JSONResponse(out, headers=no_store if handle_token else None)

    @v1.post("/handles/{handle}/keys/{key_id}/revoke", tags=["keys"])
    def revoke_key(handle: HandlePath, key_id: Annotated[str, Path(max_length=16)], body: RevokeIn) -> dict[str, Any]:
        done = store.revoke_key(handle, body.handle_token, key_id)
        if done is None:
            raise HTTPException(404, NOT_FOUND)
        if not done:
            raise HTTPException(404, "no such key, or already revoked")
        registered, listed = store.list_keys(handle) or (handle, [])
        return {"handle": registered, "keys": listed}

    @v1.post("/handles/{handle}/recovery-challenges", tags=["keys"])
    def recovery_challenge(handle: HandlePath) -> JSONResponse:
        registered, challenge = store.create_challenge(handle)
        statement = {
            "purpose": "agent-helper/recover",
            "instance": store.instance,
            "handle": registered,
            "challenge": challenge,
        }
        return JSONResponse(
            {
                "challenge": challenge,
                "expires_in": CHALLENGE_SECONDS,
                "sign": "Ed25519 over the canonical JSON (keys sorted, no spaces, UTF-8) of 'statement', "
                "exactly as given here.",
                "statement": statement,
            },
            headers=no_store,
        )

    @v1.post("/handles/{handle}/recover", tags=["keys"])
    def recover(handle: HandlePath, body: RecoverIn) -> JSONResponse:
        token = store.recover_handle(handle, body.challenge, body.signature)
        note = "A new handle_token, shown once. The old one no longer works."
        return JSONResponse({"handle": handle, "handle_token": token, "note": note}, headers=no_store)

    # --- push notices to agents' endpoints (docs/decisions/0020) -----------------------------------

    def require_push() -> None:
        if not settings.push_enabled:
            raise HTTPException(404, "push notices are not offered by this instance; poll your mailbox instead")

    @v1.put("/handles/{handle}/push", tags=["push"], dependencies=[Depends(require_push)])
    def put_push(handle: HandlePath, body: PushIn, authorization: AuthHeader = None) -> JSONResponse:
        # The token first: strangers learn nothing about the URL rules from this endpoint.
        if store.get_push(handle, _bearer(authorization)) is None:
            raise HTTPException(404, NOT_FOUND)
        try:
            dest = app.state.push.check(body.url)
        except DestinationRefused as exc:
            # Only syntax and policy, never anything about the network: no oracle (0020, step 1).
            raise HTTPException(422, str(exc)) from None
        events = sorted(set(body.events))
        found = store.put_push(handle, _bearer(authorization), body.url, dest.host, events)
        if found is None:
            raise HTTPException(404, NOT_FOUND)
        view, secret = found
        app.state.push.nudge()
        out = view | {
            "secret": secret,
            "note": "Pending. A verification request with a code is sent to the URL; confirm it with POST "
            f"/v1/handles/{handle}/push/verify. Keep 'secret': it is shown only once. Every notice carries "
            "X-Agent-Helper-Signature: sha256=HMAC-SHA256(secret, '<X-Agent-Helper-Timestamp>.<X-Agent-Helper-"
            "Event-Id>.<raw body>'); reject timestamps older than 5 minutes and repeated event ids. Notices "
            "only say that something is waiting; fetch it with your token as usual.",
        }
        return JSONResponse(out, headers=no_store)

    @v1.get("/handles/{handle}/push", tags=["push"], dependencies=[Depends(require_push)])
    def get_push(handle: HandlePath, authorization: AuthHeader = None) -> JSONResponse:
        found = store.get_push(handle, _bearer(authorization))
        if found is None:
            raise HTTPException(404, NOT_FOUND)
        return JSONResponse(found, headers=no_store)

    @v1.delete("/handles/{handle}/push", status_code=204, tags=["push"], dependencies=[Depends(require_push)])
    def delete_push(handle: HandlePath, authorization: AuthHeader = None) -> Response:
        if not store.delete_push(handle, _bearer(authorization)):
            raise HTTPException(404, NOT_FOUND)
        return Response(status_code=204)

    @v1.post("/handles/{handle}/push/verify", tags=["push"], dependencies=[Depends(require_push)])
    def verify_push(handle: HandlePath, body: PushVerifyIn, authorization: AuthHeader = None) -> JSONResponse:
        found = store.verify_push(handle, _bearer(authorization), body.code)
        if found is None:
            raise HTTPException(404, NOT_FOUND)
        return JSONResponse(found, headers=no_store)

    @v1.post("/handles/{handle}/push/renew", tags=["push"], dependencies=[Depends(require_push)])
    def renew_push(handle: HandlePath, authorization: AuthHeader = None) -> JSONResponse:
        found = store.renew_push(handle, _bearer(authorization))
        if found is None:
            raise HTTPException(404, NOT_FOUND)
        app.state.push.nudge()
        return JSONResponse(found, headers=no_store)

    app.include_router(v1)

    # --- operator API ---------------------------------------------------------------------------

    def require_admin(authorization: AuthHeader = None) -> None:
        if not settings.admin_enabled:
            raise HTTPException(404, "Not Found")
        token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(token.encode(), settings.admin_secret.encode()):  # type: ignore[union-attr]
            raise HTTPException(401, "unauthorized")

    admin = APIRouter(prefix="/admin/v1", dependencies=[Depends(require_admin)], include_in_schema=False)

    @admin.get("/requests")
    def admin_requests(
        status: RequestStatus | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[RequestOut]:
        return [RequestOut(**r) for r in store.list_requests(status, limit)]

    @admin.get("/requests/{req_id}")
    def admin_get_request(req_id: str) -> RequestOut:
        found = store.get_request_admin(req_id)
        if found is None:
            raise HTTPException(404, "no such request")
        return RequestOut(**found)

    @admin.post("/requests/{req_id}/replies")
    def admin_reply(req_id: str, body: OperatorReplyIn) -> RequestOut:
        found = store.add_operator_reply(req_id, body.message, body.status)
        if found is None:
            raise HTTPException(404, "no such request")
        return RequestOut(**found)

    @admin.post("/requests/{req_id}/referrals")
    def admin_refer(req_id: str, body: ReferralIn) -> RequestOut:
        found = store.refer_request(
            req_id, body.to, body.note, body.include_request_text, mail_limits, body.include_requester_handle
        )
        if found is None:
            raise HTTPException(404, "no such request")
        return RequestOut(**found)

    @admin.get("/directory")
    def admin_directory(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, Any]]:
        return store.list_profiles_admin(limit)

    @admin.post("/directory/{handle}/hide")
    def admin_hide_profile(handle: str, body: HideIn) -> dict[str, bool]:
        if not store.set_profile_hidden(handle, body.reason):
            raise HTTPException(404, "no such profile")
        return {"hidden": True}

    @admin.post("/directory/{handle}/unhide")
    def admin_unhide_profile(handle: str) -> dict[str, bool]:
        if not store.set_profile_hidden(handle, None):
            raise HTTPException(404, "no such profile")
        return {"hidden": False}

    @admin.put("/capabilities/{cap_id}")
    def admin_put_capability(
        cap_id: Annotated[str, Path(max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")], body: CapabilityIn
    ) -> Capability:
        store.put_operator_capability(cap_id, body.model_dump())
        return catalog.get(cap_id)  # type: ignore[return-value]

    @admin.delete("/capabilities/{cap_id}", status_code=204)
    def admin_delete_capability(cap_id: str) -> Response:
        if not store.delete_operator_capability(cap_id):
            raise HTTPException(404, "no operator entry with this id (catalog file entries cannot be deleted here)")
        return Response(status_code=204)

    @admin.get("/capability-requests")
    def admin_capability_requests(
        status: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[dict[str, Any]]:
        return store.list_capability_requests_admin(status, limit)

    @admin.post("/capability-requests/{req_id}/decision")
    def admin_decide_capability_request(req_id: str, body: CapabilityDecisionIn) -> dict[str, Any]:
        sent = body.model_fields_set
        if body.capability_id is not None and catalog.get(body.capability_id) is None:
            raise HTTPException(422, "capability_id does not name a catalog entry")
        if "status" in sent and body.status is None:
            raise HTTPException(422, "status cannot be null")
        names = {"status": "status", "note": "operator_note", "capability_id": "capability_id"}
        changes = {column: getattr(body, field) for field, column in names.items() if field in sent}
        return _update_capability_request(req_id, changes)

    @admin.post("/capability-requests/{req_id}/hide")
    def admin_hide_capability_request(req_id: str, body: HideIn) -> dict[str, Any]:
        return _update_capability_request(req_id, {"hidden_reason": body.reason})

    @admin.post("/capability-requests/{req_id}/unhide")
    def admin_unhide_capability_request(req_id: str) -> dict[str, Any]:
        return _update_capability_request(req_id, {"hidden_reason": None})

    def _update_capability_request(req_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        found = store.update_capability_request(req_id, changes)
        if found is None:
            raise HTTPException(404, "no such capability request")
        return found

    @admin.get("/reports")
    def admin_reports(
        status: ReportStatus | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[ReportOut]:
        return [ReportOut(**r) for r in store.list_reports(status, limit)]

    @admin.post("/reports/{rep_id}/decision")
    def admin_decide(rep_id: str, body: ReportDecisionIn) -> ReportOut:
        found = store.set_report_status(rep_id, body.status, body.note)
        if found is None:
            raise HTTPException(404, "no such report")
        return ReportOut(**found)

    @admin.post("/board", status_code=201)
    def admin_post_board(body: OperatorBoardIn) -> BoardEntry:
        entry, _ = store.append_board_entry(
            OPERATOR_HANDLE,
            body.topic,
            body.content,
            as_operator=True,
            tags=body.tags,
            expires_in_days=body.expires_in_days,
        )
        return BoardEntry(**entry)

    @admin.post("/board/{seq}/hide")
    def admin_hide(seq: int, body: HideIn) -> BoardEntry:
        entry = store.hide_board_entry(seq, body.reason)
        if entry is None:
            raise HTTPException(404, "no such entry")
        return BoardEntry(**entry)

    @admin.post("/board/{seq}/purge")
    def admin_purge(seq: Annotated[int, Path(ge=1, le=MAX_ID)], body: PurgeIn) -> BoardEntry:
        if body.confirm != f"PURGE {seq}":
            raise HTTPException(422, f"confirm must be exactly 'PURGE {seq}'; purging cannot be undone")
        try:
            entry = store.purge_board_payload(seq, body.reason)
        except PurgeRefused as exc:
            raise HTTPException(409, str(exc)) from None
        if entry is None:
            raise HTTPException(404, "no such entry")
        return BoardEntry(**entry)

    @admin.get("/push")
    def admin_push(limit: Annotated[int, Query(ge=1, le=500)] = 200) -> list[dict[str, Any]]:
        return store.list_push_admin(limit)

    @admin.post("/push/{sub_id}/suspend")
    def admin_suspend_push(sub_id: Annotated[str, Path(max_length=64)]) -> dict[str, bool]:
        if not store.admin_suspend_push(sub_id, mail_limits):
            raise HTTPException(404, "no such subscription")
        return {"suspended": True}

    @admin.delete("/push/{sub_id}", status_code=204)
    def admin_delete_push(sub_id: Annotated[str, Path(max_length=64)]) -> Response:
        if not store.admin_delete_push(sub_id):
            raise HTTPException(404, "no such subscription")
        return Response(status_code=204)

    @admin.post("/notifications/test")
    def admin_test_notification() -> dict[str, Any]:
        result = app.state.notifier.send_test()
        return {"delivered": result.ok, "status": result.status, "error": result.error}

    app.include_router(admin)
    app.include_router(build_console(settings, store, catalog, lambda: app.state.notifier, mail_limits))
    return app
