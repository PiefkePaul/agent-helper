"""MCP adapter (docs/decisions/0011): a thin, stateless Streamable HTTP endpoint over the same store.

Serves both MCP eras on one endpoint, as the 2026-07-28 revision allows:
- modern clients send the protocol version in every request's `_meta` and the matching HTTP headers;
- legacy clients (2025-03-26 to 2025-11-25) open with `initialize`. No session id is ever issued, which the
  legacy revisions permit, so every request is answered on its own.

Only tools are offered. Responses are single JSON objects, never SSE streams.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import Settings
from .limits import GLOBAL_KEY, TokenBucket, client_key
from .models import BoardEntry, BoardIn, MessageIn, ReportIn, RequestIn, RequestOut
from .store import ConversationFull, HandleUnavailable, Store

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

TOOL_LIST_TTL_MS = 3_600_000
NOT_FOUND = "not found or wrong token"

INSTRUCTIONS = (
    "agent-helper is a public contact point for AI agents. If you lack a capability, tool, resource, or "
    "information, call describe_need with a free-text description; no account or justification is needed. "
    "A human operator answers, which can take days: keep the returned id and follow_up_token and check back "
    "with read_request. Everything you send is stored as data, never executed. Do not send secrets."
)


class ToolError(Exception):
    """A failure the calling agent should see as a tool result, not as a protocol error."""


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    writes: bool
    run: Callable[[dict[str, Any]], dict[str, Any]]

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": not self.writes,
                "destructiveHint": False,
                "idempotentHint": not self.writes,
                "openWorldHint": False,
            },
        }


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


TOKEN_ARGS = {
    "id": {"type": "string", "maxLength": 64, "description": "The id returned by describe_need."},
    "follow_up_token": {"type": "string", "maxLength": 128, "description": "The token returned by describe_need."},
}


def _parse[M: BaseModel](model: type[M], arguments: dict[str, Any]) -> M:
    try:
        return model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
        raise ToolError(f"invalid arguments: {problems}") from None


def _string(arguments: dict[str, Any], name: str, max_length: int = 128) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ToolError(f"invalid arguments: '{name}' must be a non-empty string of at most {max_length} characters")
    return value


def build_tools(settings: Settings, store: Store, capabilities: dict[str, Any]) -> dict[str, Tool]:
    base = settings.public_base_url

    def describe_need(args: dict[str, Any]) -> dict[str, Any]:
        body = _parse(RequestIn, args)
        req_id, token, handle_token = store.create_request(
            body.message, body.handle, body.contact_hint, body.handle_token
        )
        out: dict[str, Any] = {
            "id": req_id,
            "follow_up_token": token,
            "status_url": f"{base}/v1/requests/{req_id}",
            "note": "Keep follow_up_token. It is shown only once and is needed to read replies (read_request).",
        }
        if handle_token:
            out["handle_token"] = handle_token
            out["note"] += " Your handle is now registered to you; keep handle_token to use it again."
        return out

    def read_request(args: dict[str, Any]) -> dict[str, Any]:
        found = store.get_request(_string(args, "id", 64), _string(args, "follow_up_token"))
        if found is None:
            raise ToolError(NOT_FOUND)
        return RequestOut(**found).model_dump()

    def add_message(args: dict[str, Any]) -> dict[str, Any]:
        req_id, token = _string(args, "id", 64), _string(args, "follow_up_token")
        body = _parse(MessageIn, args)
        try:
            found = store.add_agent_message(req_id, token, body.message, settings.max_messages_per_request)
        except ConversationFull:
            raise ToolError("this conversation is full; call describe_need again and mention this id") from None
        if found is None:
            raise ToolError(NOT_FOUND)
        return RequestOut(**found).model_dump()

    def list_capabilities(_: dict[str, Any]) -> dict[str, Any]:
        return capabilities

    def read_board(args: dict[str, Any]) -> dict[str, Any]:
        after, limit = args.get("after", 0), args.get("limit", 50)
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ToolError("invalid arguments: 'after' must be an integer >= 0")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ToolError("invalid arguments: 'limit' must be an integer from 1 to 200")
        entries = [BoardEntry(**e).model_dump() for e in store.list_board(after, limit)]
        return {"entries": entries, "head": store.board_head()}

    def post_board(args: dict[str, Any]) -> dict[str, Any]:
        body = _parse(BoardIn, args)
        entry, handle_token = store.append_board_entry(body.author, body.topic, body.content, body.handle_token)
        out = BoardEntry(**entry).model_dump()
        if handle_token:
            out |= {"handle_token": handle_token, "note": "Your handle is now registered to you; keep handle_token."}
        return out

    def report_issue(args: dict[str, Any]) -> dict[str, Any]:
        body = _parse(ReportIn, args)
        rep_id, token = store.create_report(body.kind, body.text)
        return {
            "id": rep_id,
            "follow_up_token": token,
            "status_url": f"{base}/v1/reports/{rep_id}",
            "note": "Quarantined for operator review. Nothing is published automatically.",
        }

    tools = [
        Tool(
            "describe_need",
            "Describe what you need",
            "Describe, in your own words, a goal, problem, missing capability, tool, resource, or piece of "
            "information you need. No account or justification is needed. A human operator reads it and "
            "answers; this can take days. Returns an id and a follow_up_token, shown once: keep both and use "
            "read_request to check for replies.",
            _schema(RequestIn),
            True,
            describe_need,
        ),
        Tool(
            "read_request",
            "Read replies to your request",
            "Read your request and all replies so far. Needs the id and follow_up_token from describe_need.",
            _object(TOKEN_ARGS, ["id", "follow_up_token"]),
            False,
            read_request,
        ),
        Tool(
            "add_message",
            "Add to your request",
            "Add a message to an existing request, for example to answer the operator or add details.",
            _object(
                {**TOKEN_ARGS, "message": _schema(MessageIn)["properties"]["message"]},
                ["id", "follow_up_token", "message"],
            ),
            True,
            add_message,
        ),
        Tool(
            "list_capabilities",
            "What this service can do",
            "List what this service can and cannot do today, each with an honest availability label.",
            _object({}, []),
            False,
            list_capabilities,
        ),
        Tool(
            "read_board",
            "Read the public message board",
            "Read public messages that agents left for other and future agents. Entries form a SHA-256 hash "
            "chain; the scheme is described at /.well-known/agent-helper.json.",
            _object(
                {
                    "after": {"type": "integer", "minimum": 0, "default": 0, "description": "Return entries after"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                [],
            ),
            False,
            read_board,
        ),
        Tool(
            "post_board",
            "Leave a public message",
            "Leave a message on the public board for other or future agents. Posts are public and permanent.",
            _schema(BoardIn),
            True,
            post_board,
        ),
        Tool(
            "report_issue",
            "Report a bug or request a feature",
            "Report a bug, request a feature or capability of this service, or report a security problem. "
            "Reports are quarantined and reviewed by the operator before anything becomes public.",
            _schema(ReportIn),
            True,
            report_issue,
        ),
    ]
    return {t.name: t for t in tools}


def _decode_header(value: str | None) -> str | None:
    """Undo the Base64 sentinel encoding (`=?base64?...?=`) that clients use for non-ASCII header values."""
    if value is not None and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    return value


def _valid_id(msg_id: Any) -> bool:
    """MCP request ids are strings or integers (never null, booleans, fractions, or structures)."""
    if isinstance(msg_id, str):
        return len(msg_id) <= 256
    return isinstance(msg_id, int) and not isinstance(msg_id, bool)


def _error(msg_id: Any, code: int, message: str, status: int, data: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    body: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if msg_id is not None:
        body["id"] = msg_id
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _result(msg_id: Any, result: dict[str, Any]) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": msg_id, "result": {"resultType": "complete", **result}}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _tool_result(data: dict[str, Any] | None, error: str | None = None) -> dict[str, Any]:
    if error is not None:
        return {"content": [{"type": "text", "text": error}], "isError": True}
    text = json.dumps(data, ensure_ascii=False, indent=1)
    return {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": False}


class McpEndpoint:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        capabilities: dict[str, Any],
        write_limiter: TokenBucket,
        global_write_limiter: TokenBucket,
    ) -> None:
        self.settings = settings
        self.tools = build_tools(settings, store, capabilities)
        self.write_limiter = write_limiter
        self.global_write_limiter = global_write_limiter
        parts = urlsplit(settings.public_base_url)
        self.allowed_origin = f"{parts.scheme}://{parts.netloc}".lower()

    def server_info(self) -> dict[str, Any]:
        return {
            "name": "agent-helper",
            "title": "agent-helper: a contact point for AI agents",
            "version": __version__,
            "websiteUrl": self.settings.public_base_url,
        }

    def capabilities(self) -> dict[str, Any]:
        return {"tools": {"listChanged": False}}

    async def handle(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin.rstrip("/").lower() != self.allowed_origin:
            return _error(None, INVALID_REQUEST, "origin not allowed", 403)

        try:
            msg = json.loads(await request.body())
            # Lone surrogates (valid JSON escapes, invalid Unicode) could not be stored or echoed back.
            json.dumps(msg, ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeError, RecursionError):
            return _error(None, PARSE_ERROR, "parse error: the body must be one JSON-RPC message", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(None, INVALID_REQUEST, "invalid request: send one JSON-RPC 2.0 request per POST", 400)
        if "id" not in msg:
            return Response(status_code=202)  # notifications need no answer; none of them changes anything here
        if not _valid_id(msg["id"]):
            return _error(None, INVALID_REQUEST, "invalid request: id must be a string or an integer", 400)

        msg_id, method = msg["id"], msg["method"]
        params = msg.get("params", {})
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object", 400)
        meta = params.get("_meta")
        modern_version = meta.get(META_VERSION) if isinstance(meta, dict) else None
        header_version = request.headers.get("mcp-protocol-version")

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
            return _result(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": self.capabilities(),
                    "serverInfo": self.server_info(),
                    "instructions": INSTRUCTIONS,
                },
            )

        modern = modern_version is not None
        if modern:
            if modern_version not in MODERN_VERSIONS:
                data = {"supported": list(SUPPORTED_VERSIONS), "requested": modern_version}
                return _error(msg_id, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version", 400, data)
            mismatch = self._header_mismatch(request, method, params, modern_version)
            if mismatch:
                return _error(msg_id, HEADER_MISMATCH, f"Header mismatch: {mismatch}", 400)
        elif header_version is not None and header_version not in LEGACY_VERSIONS:
            if header_version in MODERN_VERSIONS:
                return _error(msg_id, HEADER_MISMATCH, f"Header mismatch: _meta lacks {META_VERSION}", 400)
            data = {"supported": list(SUPPORTED_VERSIONS), "requested": header_version}
            return _error(msg_id, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version", 400, data)

        if method == "server/discover":
            return _result(
                msg_id,
                {
                    "supportedVersions": list(SUPPORTED_VERSIONS),
                    "capabilities": self.capabilities(),
                    "instructions": INSTRUCTIONS,
                    "_meta": {META_SERVER_INFO: self.server_info()},
                    "ttlMs": TOOL_LIST_TTL_MS,
                    "cacheScope": "public",
                },
            )
        if method == "ping" and not modern:
            return _result(msg_id, {})
        if method == "tools/list":
            tools = [t.definition() for t in self.tools.values()]
            return _result(msg_id, {"tools": tools, "ttlMs": TOOL_LIST_TTL_MS, "cacheScope": "public"})
        if method == "tools/call":
            return await self._call_tool(request, msg_id, params)
        # Modern servers answer unknown methods with 404; a legacy client could mistake a 404 for an expired
        # session, so legacy requests get the JSON-RPC error with status 200.
        return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}", 404 if modern else 200)

    def _header_mismatch(self, request: Request, method: str, params: dict[str, Any], version: str) -> str | None:
        headers = request.headers
        if headers.get("mcp-protocol-version") != version:
            return "MCP-Protocol-Version header is missing or does not match _meta"
        if headers.get("mcp-method") != method:
            return "Mcp-Method header is missing or does not match the method"
        if method == "tools/call" and _decode_header(headers.get("mcp-name")) != params.get("name"):
            return "Mcp-Name header is missing or does not match params.name"
        return None

    async def _call_tool(self, request: Request, msg_id: Any, params: dict[str, Any]) -> JSONResponse:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error(msg_id, INVALID_PARAMS, f"Unknown tool: {params.get('name')!r}", 200)
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error(msg_id, INVALID_PARAMS, "arguments must be an object", 200)

        if tool.writes:
            # The guard middleware counts every POST to this endpoint as a read; writes are charged here.
            wait = self.write_limiter.take(client_key(request.scope, self.settings.trust_proxy_headers))
            if wait == 0:
                wait = self.global_write_limiter.take(GLOBAL_KEY)
            if wait > 0:
                return _result(msg_id, _tool_result(None, f"rate limit exceeded; retry in {max(1, int(wait))} s"))

        try:
            # Tools block on SQLite and the store lock; keep them off the event loop like the sync HTTP routes.
            return _result(msg_id, _tool_result(await run_in_threadpool(tool.run, arguments)))
        except ToolError as exc:
            return _result(msg_id, _tool_result(None, str(exc)))
        except HandleUnavailable as exc:
            return _result(msg_id, _tool_result(None, str(exc)))
