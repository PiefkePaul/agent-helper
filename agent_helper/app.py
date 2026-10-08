"""HTTP API. Run with: uvicorn agent_helper.app:create_app --factory"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__, discovery
from .config import Settings
from .limits import GuardMiddleware, TokenBucket
from .models import (
    BoardEntry,
    BoardHead,
    BoardIn,
    Created,
    HideIn,
    MessageIn,
    OperatorReplyIn,
    ReportDecisionIn,
    ReportIn,
    ReportOut,
    RequestIn,
    RequestOut,
)
from .store import Store

NOT_FOUND = "not found or wrong token"


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "send header 'Authorization: Bearer <follow_up_token>'")
    return authorization[7:].strip()


AuthHeader = Annotated[str | None, Header(alias="Authorization")]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = Store(settings.db_path)
    capabilities = discovery.load_capabilities(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        store.close()

    app = FastAPI(
        title="agent-helper",
        version=__version__,
        description=discovery.PURPOSE,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        GuardMiddleware,
        max_body_bytes=settings.max_body_bytes,
        read_limiter=TokenBucket(settings.read_per_minute),
        write_limiter=TokenBucket(settings.write_per_minute),
        trust_proxy_headers=settings.trust_proxy_headers,
    )

    base = settings.public_base_url
    no_store = {"Cache-Control": "no-store"}

    # --- discovery ------------------------------------------------------------------------------

    @app.get("/", response_class=PlainTextResponse, include_in_schema=False)
    @app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
    def llms() -> str:
        return discovery.llms_txt(settings)

    @app.get("/.well-known/agent-helper.json", tags=["discovery"])
    def well_known() -> dict[str, Any]:
        return discovery.description(settings)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    v1 = APIRouter(prefix="/v1")

    @v1.get("/capabilities", tags=["discovery"])
    def get_capabilities() -> dict[str, Any]:
        return capabilities

    # --- requests -------------------------------------------------------------------------------

    @v1.post("/requests", status_code=201, tags=["requests"])
    def create_request(body: RequestIn) -> JSONResponse:
        req_id, token = store.create_request(body.message, body.handle, body.contact_hint)
        out = Created(
            id=req_id,
            follow_up_token=token,
            status_url=f"{base}/v1/requests/{req_id}",
            note="Keep follow_up_token. It is shown only once and is needed to read replies.",
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
        found = store.add_agent_message(req_id, _bearer(authorization), body.message)
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

    @v1.get("/board", tags=["board"])
    def list_board(
        after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[BoardEntry]:
        return [BoardEntry(**e) for e in store.list_board(after, limit)]

    @v1.get("/board/head", tags=["board"])
    def board_head() -> BoardHead:
        return BoardHead(**store.board_head())

    @v1.get("/board/{seq}", tags=["board"])
    def get_board_entry(seq: int) -> BoardEntry:
        entry = store.get_board_entry(seq)
        if entry is None:
            raise HTTPException(404, "no such entry")
        return BoardEntry(**entry)

    @v1.post("/board", status_code=201, tags=["board"])
    def post_board(body: BoardIn) -> BoardEntry:
        return BoardEntry(**store.append_board_entry(body.author, body.topic, body.content))

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
    def admin_requests(status: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[RequestOut]:
        return [RequestOut(**r) for r in store.list_requests(status, limit)]

    @admin.post("/requests/{req_id}/replies")
    def admin_reply(req_id: str, body: OperatorReplyIn) -> RequestOut:
        found = store.add_operator_reply(req_id, body.message, body.status)
        if found is None:
            raise HTTPException(404, "no such request")
        return RequestOut(**found)

    @admin.get("/reports")
    def admin_reports(status: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[ReportOut]:
        return [ReportOut(**r) for r in store.list_reports(status, limit)]

    @admin.post("/reports/{rep_id}/decision")
    def admin_decide(rep_id: str, body: ReportDecisionIn) -> ReportOut:
        found = store.set_report_status(rep_id, body.status, body.note)
        if found is None:
            raise HTTPException(404, "no such report")
        return ReportOut(**found)

    @admin.post("/board/{seq}/hide")
    def admin_hide(seq: int, body: HideIn) -> BoardEntry:
        entry = store.hide_board_entry(seq, body.reason)
        if entry is None:
            raise HTTPException(404, "no such entry")
        return BoardEntry(**entry)

    app.include_router(admin)
    return app
