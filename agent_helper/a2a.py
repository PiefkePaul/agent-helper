"""A2A adapter (docs/decisions/0019): the request conversation as A2A 1.0 tasks, JSON-RPC binding, at /a2a.

- `SendMessage` without a task id starts a request: the task id is the request id, and the follow-up token
  comes back once in `task.metadata.followUpToken`.
- `SendMessage` with a task id adds to that conversation, `GetTask` reads it, `CancelTask` closes it. These
  need `Authorization: Bearer <followUpToken>`; the token never goes into a URL.
- Agent text is untrusted data. Only text parts are accepted. No streaming, no push notifications.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__, discovery
from .config import Settings
from .limits import GLOBAL_KEY, TokenBucket, client_key
from .models import LIMITS, MessageIn, RequestIn
from .store import ConversationFull, HandleUnavailable, Store

PROTOCOL_VERSION = "1.0"

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
PARSE_ERROR = -32700
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
VERSION_NOT_SUPPORTED = -32009

STATE = {  # request status -> A2A task state
    "open": "TASK_STATE_SUBMITTED",  # waiting for the operator
    "answered": "TASK_STATE_INPUT_REQUIRED",  # the operator replied; the agent may answer
    "closed": "TASK_STATE_COMPLETED",
}
UNSUPPORTED = {
    "SendStreamingMessage",
    "SubscribeToTask",
    "ListTasks",
    "GetExtendedAgentCard",
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
}


class A2AError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def agent_card(settings: Settings) -> dict[str, Any]:
    base = settings.public_base_url
    return {
        "name": "agent-helper",
        "description": discovery.PURPOSE,
        "version": __version__,
        "provider": {"organization": "agent-helper operator", "url": base},
        "documentationUrl": f"{base}/llms.txt",
        "supportedInterfaces": [{"url": f"{base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {
            "followUpToken": {
                "httpAuthSecurityScheme": {
                    "scheme": "Bearer",
                    "description": "Not needed to start. SendMessage without a taskId returns the task and, once, "
                    "metadata.followUpToken. Send it as 'Authorization: Bearer <token>' for GetTask, CancelTask "
                    "and SendMessage with that taskId.",
                }
            }
        },
        "skills": [
            {
                "id": "describe-need",
                "name": "Describe what you need",
                "description": "Describe a goal, problem, missing capability, tool, resource or piece of "
                "information in your own words. A human operator reads it and answers; this can take hours or "
                "days. No account or justification is needed. The task stays TASK_STATE_SUBMITTED until the "
                "operator replies (then TASK_STATE_INPUT_REQUIRED).",
                "tags": ["help", "human-in-the-loop", "missing-capability", "request"],
                "examples": ["I need someone to scan a paper form in Berlin and send me the text."],
            },
            {
                "id": "follow-up",
                "name": "Follow up on your request",
                "description": "Read replies (GetTask) or answer the operator (SendMessage with the taskId), "
                "authenticated with the followUpToken. Check back now and then, not in a tight loop.",
                "tags": ["follow-up"],
                "securityRequirements": [{"schemes": {"followUpToken": {"list": []}}}],
            },
        ],
    }


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    return value[7:].strip() or None if value.lower().startswith("bearer ") else None


def _text_of(message: Any) -> str:
    """The text of an A2A user message. Only text parts are accepted."""
    if not isinstance(message, dict):
        raise A2AError(INVALID_PARAMS, "params.message must be an object")
    if message.get("role") not in ("ROLE_USER", None):
        raise A2AError(INVALID_PARAMS, "only ROLE_USER messages can be sent")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts or len(parts) > 20:
        raise A2AError(INVALID_PARAMS, "message.parts must be a list of 1 to 20 parts")
    texts = []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            raise A2AError(CONTENT_TYPE_NOT_SUPPORTED, "only text parts are supported")
        texts.append(part["text"])
    text = "\n\n".join(texts)
    if len(text) > LIMITS["message"]:
        raise A2AError(INVALID_PARAMS, f"the message text is longer than {LIMITS['message']} characters")
    return text


def _metadata_str(meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) else None


def _task(view: dict[str, Any], history_length: int | None = None) -> dict[str, Any]:
    history = [
        {
            "messageId": f"{view['id']}-{i}",
            "contextId": view["id"],
            "taskId": view["id"],
            "role": "ROLE_USER" if m["sender"] == "agent" else "ROLE_AGENT",
            "parts": [{"text": m["body"]}],
            "metadata": {"createdAt": m["created_at"], "sender": m["sender"]},
        }
        for i, m in enumerate(view["messages"])
    ]
    if history_length is not None:
        history = history[-history_length:] if history_length > 0 else []
    status: dict[str, Any] = {"state": STATE[view["status"]]}
    last = view["messages"][-1] if view["messages"] else None
    if last is not None:
        status["timestamp"] = last["created_at"]
        if last["sender"] == "operator":
            status["message"] = {
                "messageId": f"{view['id']}-{len(view['messages']) - 1}",
                "contextId": view["id"],
                "taskId": view["id"],
                "role": "ROLE_AGENT",
                "parts": [{"text": last["body"]}],
            }
    return {"id": view["id"], "contextId": view["id"], "status": status, "history": history}


class A2AEndpoint:
    def __init__(
        self, settings: Settings, store: Store, write_limiter: TokenBucket, global_write_limiter: TokenBucket
    ) -> None:
        self.settings = settings
        self.store = store
        self.write_limiter = write_limiter
        self.global_write_limiter = global_write_limiter
        parts = urlsplit(settings.public_base_url)
        self.allowed_origin = f"{parts.scheme}://{parts.netloc}".lower()

    async def handle(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin.rstrip("/").lower() != self.allowed_origin:
            return _error(None, INVALID_REQUEST, "origin not allowed", 403)
        version = request.headers.get("a2a-version") or request.query_params.get("A2A-Version")
        try:
            msg = json.loads(await request.body())
            json.dumps(msg, ensure_ascii=False).encode("utf-8")  # rejects lone surrogates
        except (ValueError, UnicodeError, RecursionError):
            return _error(None, PARSE_ERROR, "parse error: send one JSON-RPC 2.0 request", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(None, INVALID_REQUEST, "invalid request: send one JSON-RPC 2.0 request per POST", 400)
        msg_id = msg.get("id")
        if not (isinstance(msg_id, str) and len(msg_id) <= 256) and not (
            isinstance(msg_id, int) and not isinstance(msg_id, bool)
        ):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer", 400)
        if version and version.strip() != PROTOCOL_VERSION:
            return _error(msg_id, VERSION_NOT_SUPPORTED, f"A2A version {version[:20]!r} is not supported; use 1.0")
        params = msg.get("params", {})
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        method = msg["method"]
        try:
            if method == "SendMessage":
                self._charge_write(request)
                result = await run_in_threadpool(self._send_message, params, _bearer(request))
            elif method == "GetTask":
                result = await run_in_threadpool(self._get_task, params, _bearer(request))
            elif method == "CancelTask":
                self._charge_write(request)
                result = await run_in_threadpool(self._cancel_task, params, _bearer(request))
            elif method in UNSUPPORTED:
                raise A2AError(UNSUPPORTED_OPERATION, f"{method} is not supported by this agent")
            else:
                raise A2AError(METHOD_NOT_FOUND, f"Method not found: {method[:64]}")
        except A2AError as exc:
            return _error(msg_id, exc.code, str(exc))
        except HandleUnavailable as exc:
            return _error(msg_id, INVALID_PARAMS, str(exc))
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result}, headers={"Cache-Control": "no-store"})

    def _charge_write(self, request: Request) -> None:
        # The guard middleware counts POSTs here as reads; writes are charged when the method is known.
        wait = self.write_limiter.take(client_key(request.scope, self.settings.trust_proxy_headers))
        if wait == 0:
            wait = self.global_write_limiter.take(GLOBAL_KEY)
        if wait > 0:
            raise A2AError(INVALID_REQUEST, f"rate limit exceeded; retry in {max(1, int(wait))} s")

    def _send_message(self, params: dict[str, Any], token: str | None) -> dict[str, Any]:
        message = params.get("message")
        text = _text_of(message)
        task_id = message.get("taskId") or message.get("contextId")  # type: ignore[union-attr]
        if task_id:
            if not isinstance(task_id, str) or len(task_id) > 64:
                raise A2AError(INVALID_PARAMS, "taskId must be a string")
            if token is None:
                raise A2AError(TASK_NOT_FOUND, "task not found; send 'Authorization: Bearer <followUpToken>'")
            try:
                body = MessageIn.model_validate({"message": text})
            except ValidationError:
                raise A2AError(INVALID_PARAMS, "the message contains characters that are not allowed") from None
            try:
                view = self.store.add_agent_message(
                    task_id, token, body.message, self.settings.max_messages_per_request
                )
            except ConversationFull:
                raise A2AError(UNSUPPORTED_OPERATION, "this task is full; start a new task and mention it") from None
            if view is None:
                raise A2AError(TASK_NOT_FOUND, "task not found or wrong token")
            return {"task": _task(view)}

        meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}  # type: ignore[union-attr]
        try:
            body_in = RequestIn.model_validate(
                {
                    "message": text,
                    "handle": _metadata_str(meta, "handle"),
                    "handle_token": _metadata_str(meta, "handleToken"),
                    "contact_hint": _metadata_str(meta, "contactHint"),
                }
            )
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            raise A2AError(INVALID_PARAMS, f"invalid message: {problems}") from None
        req_id, follow_up_token, handle_token = self.store.create_request(
            body_in.message, body_in.handle, body_in.contact_hint, body_in.handle_token
        )
        view = self.store.get_request(req_id, follow_up_token)
        assert view is not None
        task = _task(view)
        task["metadata"] = {
            "followUpToken": follow_up_token,
            "note": "Keep followUpToken; it is shown only once. Send it as 'Authorization: Bearer <token>' with "
            "GetTask to read replies. A human operator answers; this can take hours or days.",
        }
        if handle_token:
            task["metadata"]["handleToken"] = handle_token
        return {"task": task}

    def _get_task(self, params: dict[str, Any], token: str | None) -> dict[str, Any]:
        task_id = params.get("id")
        if not isinstance(task_id, str) or len(task_id) > 64:
            raise A2AError(INVALID_PARAMS, "params.id must be the task id")
        history_length = params.get("historyLength")
        if history_length is not None and (
            not isinstance(history_length, int) or isinstance(history_length, bool) or history_length < 0
        ):
            raise A2AError(INVALID_PARAMS, "historyLength must be a non-negative integer")
        view = self.store.get_request(task_id, token) if token else None
        if view is None:
            raise A2AError(TASK_NOT_FOUND, "task not found or wrong token")
        return _task(view, history_length)

    def _cancel_task(self, params: dict[str, Any], token: str | None) -> dict[str, Any]:
        task_id = params.get("id")
        if not isinstance(task_id, str) or len(task_id) > 64:
            raise A2AError(INVALID_PARAMS, "params.id must be the task id")
        view = self.store.close_request(task_id, token) if token else None
        if view is None:
            raise A2AError(TASK_NOT_FOUND, "task not found or wrong token")
        return _task(view)  # closed requests are TASK_STATE_COMPLETED, also when the agent closed them


def _error(msg_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    body: dict[str, Any] = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    if msg_id is not None:
        body["id"] = msg_id
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
