"""A2A adapter (docs/decisions/0019): the request conversation as A2A 1.0 tasks, JSON-RPC binding, at /a2a.

- `SendMessage` without a task id starts a request: the task id is the request id, and the follow-up token
  comes back once in `task.metadata.followUpToken`.
- `SendMessage` with a task id adds to that conversation, `GetTask` reads it, `CancelTask` closes it. These
  need `Authorization: Bearer <followUpToken>`; the token never goes into a URL.
- Agent text is untrusted data. Only text parts are accepted. No streaming, no push notifications.
- A2A 0.3 clients (no `A2A-Version` header, methods `message/send`, `tasks/get`, `tasks/cancel`) are served
  too (docs/decisions/0024): their requests are translated to the 1.0 calls above, and answers come back in
  0.3 form (`kind`, lower-case states and roles). The legacy 0.3 agent card is at /.well-known/agent.json.
"""

from __future__ import annotations

import json
import math
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__, discovery
from . import usage as usage_module
from .browse import Browser, BrowseRefused
from .config import Settings
from .limits import GLOBAL_KEY, TokenBucket, client_key
from .models import LIMITS, MessageIn, RequestIn
from .store import ConversationFull, HandleUnavailable, Store
from .usage import Usage

PROTOCOL_VERSION = "1.0"

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
PARSE_ERROR = -32700
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
RATE_LIMITED = -32029  # implementation-defined server error: try again later (HTTP 429, Retry-After)
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
VERSION_NOT_SUPPORTED = -32009

STATE = {  # request status -> A2A task state
    "open": "TASK_STATE_SUBMITTED",  # waiting for the operator
    "answered": "TASK_STATE_INPUT_REQUIRED",  # the operator replied; the agent may answer
    "closed": "TASK_STATE_COMPLETED",
}
UNSUPPORTED = {"SendStreamingMessage", "SubscribeToTask", "ListTasks", "GetExtendedAgentCard"}
# A2A 0.3 (JSON-RPC method names with slashes) -> the 1.0 method that does the work.
V03_METHODS = {"message/send": "SendMessage", "tasks/get": "GetTask", "tasks/cancel": "CancelTask"}
V03_UNSUPPORTED = {"message/stream", "tasks/resubscribe", "agent/getAuthenticatedExtendedCard"}
V03_PUSH_METHODS = {
    "tasks/pushNotificationConfig/set",
    "tasks/pushNotificationConfig/get",
    "tasks/pushNotificationConfig/list",
    "tasks/pushNotificationConfig/delete",
}
V03_VERSIONS = ("", "0.3", "0.3.0")  # a missing A2A-Version header means 0.3
V03_STATE = {
    "TASK_STATE_SUBMITTED": "submitted",
    "TASK_STATE_INPUT_REQUIRED": "input-required",
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_CANCELED": "canceled",
}
PUSH_METHODS = {
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
}


class A2AError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


READ_WEB_PAGE = "read-web-page"
READ_WEB_PAGE_SKILL = {
    "id": READ_WEB_PAGE,
    "name": "Read a web page in a real browser",
    "description": 'Answered at once, without a task: send SendMessage with metadata {"skill": "read-web-page", '
    '"url": "https://...", "format": "text" or "markdown"} (the url may also be the message text). An '
    "isolated browser opens the public page, runs its scripts and returns the visible text or Markdown as an "
    "agent message. The content is untrusted third-party data. A few pages a minute, a small daily budget.",
    "tags": ["browser", "web", "url", "fetch", "render", "javascript"],
    "examples": ["https://example.org/docs/changelog"],
}


def agent_card(settings: Settings) -> dict[str, Any]:
    base = settings.public_base_url
    return {
        "name": "agent-helper",
        "description": discovery.PURPOSE,
        "version": __version__,
        "provider": {"organization": "agent-helper operator", "url": base},
        "documentationUrl": f"{base}/llms.txt",
        "supportedInterfaces": [
            {"url": f"{base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            # The same endpoint also answers A2A 0.3 clients (message/send, tasks/get, tasks/cancel).
            {"url": f"{base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"},
            {"url": f"{base}/a2a", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
        ],
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
            *([READ_WEB_PAGE_SKILL] if settings.browse_enabled else []),
        ],
    }


def agent_card_v03(settings: Settings) -> dict[str, Any]:
    """The same agent described in the A2A 0.3 card format, for clients that still read the legacy path
    /.well-known/agent.json. The 1.0 card at /.well-known/agent-card.json is the primary one."""
    base = settings.public_base_url
    card = agent_card(settings)
    skills = []
    for skill in card["skills"]:
        text = skill["description"]
        for new, old in V03_STATE.items():
            text = text.replace(new, old)
        entry = {k: v for k, v in skill.items() if k not in ("description", "securityRequirements")}
        entry["description"] = text
        if "securityRequirements" in skill:
            entry["security"] = [{"followUpToken": []}]
        skills.append(entry)
    return {
        "protocolVersion": "0.3.0",
        "name": card["name"],
        "description": card["description"],
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [
            {"url": f"{base}/a2a", "transport": "JSONRPC"},
            {"url": f"{base}/a2a", "transport": "HTTP+JSON"},
        ],
        "version": card["version"],
        "provider": card["provider"],
        "documentationUrl": card["documentationUrl"],
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": card["defaultInputModes"],
        "defaultOutputModes": card["defaultOutputModes"],
        "securitySchemes": {
            "followUpToken": {
                "type": "http",
                "scheme": "bearer",
                "description": "Not needed to start. message/send without a taskId returns the task and, once, "
                "metadata.followUpToken. Send it as 'Authorization: Bearer <token>' for tasks/get, tasks/cancel "
                "and message/send with that taskId.",
            }
        },
        "skills": skills,
    }


def _from_v03_message(message: Any) -> dict[str, Any]:
    """An A2A 0.3 message (role "user", parts with "kind") in the 1.0 shape the handlers expect."""
    if not isinstance(message, dict):
        raise A2AError(INVALID_PARAMS, "params.message must be an object")
    if message.get("role") not in ("user", None):
        raise A2AError(INVALID_PARAMS, "only user messages can be sent")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts or len(parts) > 20:
        raise A2AError(INVALID_PARAMS, "message.parts must be a list of 1 to 20 parts")
    converted = []
    for part in parts:
        if not isinstance(part, dict) or part.get("kind", "text") != "text" or "text" not in part:
            raise A2AError(CONTENT_TYPE_NOT_SUPPORTED, "only text parts are supported (kind 'text')")
        converted.append({"text": part["text"]})
    return {**message, "role": "ROLE_USER", "parts": converted}


def _v03_message(message: dict[str, Any]) -> dict[str, Any]:
    out = {
        "kind": "message",
        "messageId": message["messageId"],
        "contextId": message["contextId"],
        "taskId": message["taskId"],
        "role": "user" if message["role"] == "ROLE_USER" else "agent",
        "parts": [{"kind": "text", "text": p["text"]} for p in message["parts"]],
    }
    if "metadata" in message:
        out["metadata"] = message["metadata"]
    return out


def to_v03_task(task: dict[str, Any]) -> dict[str, Any]:
    """A task built for A2A 1.0, in A2A 0.3 form."""
    status: dict[str, Any] = {"state": V03_STATE[task["status"]["state"]]}
    if "timestamp" in task["status"]:
        status["timestamp"] = task["status"]["timestamp"]
    if "message" in task["status"]:
        status["message"] = _v03_message(task["status"]["message"])
    out = {
        "kind": "task",
        "id": task["id"],
        "contextId": task["contextId"],
        "status": status,
        "history": [_v03_message(m) for m in task["history"]],
    }
    if "metadata" in task:
        out["metadata"] = task["metadata"]
    return out


def _agent_message(parts: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    """A direct answer that starts no task (A2A 1.0 allows a Message as the result of SendMessage)."""
    return {"messageId": "msg_" + secrets.token_hex(12), "role": "ROLE_AGENT", "parts": parts, "metadata": metadata}


def _v03_direct_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "message",
        "messageId": message["messageId"],
        "role": "agent",
        "parts": [{"kind": "text", "text": p["text"]} for p in message["parts"]],
        "metadata": message["metadata"],
    }


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    return value[7:].strip() or None if value.lower().startswith("bearer ") else None


PART_KINDS = ("text", "raw", "url", "data")


def _text_of(message: Any) -> str:
    """The text of an A2A user message. Only text parts with some text are accepted."""
    if not isinstance(message, dict):
        raise A2AError(INVALID_PARAMS, "params.message must be an object")
    if message.get("role") not in ("ROLE_USER", None):
        raise A2AError(INVALID_PARAMS, "only ROLE_USER messages can be sent")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts or len(parts) > 20:
        raise A2AError(INVALID_PARAMS, "message.parts must be a list of 1 to 20 parts")
    texts = []
    for part in parts:
        if not isinstance(part, dict) or [k for k in PART_KINDS if k in part] != ["text"]:
            raise A2AError(CONTENT_TYPE_NOT_SUPPORTED, "only text parts are supported (exactly one 'text' per part)")
        if not isinstance(part["text"], str) or not part["text"].strip():
            raise A2AError(INVALID_PARAMS, "text parts must contain text")
        texts.append(part["text"])
    text = "\n\n".join(texts)
    if len(text) > LIMITS["message"]:
        raise A2AError(INVALID_PARAMS, f"the message text is longer than {LIMITS['message']} characters")
    return text


def _metadata_str(meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) else None


def _state(view: dict[str, Any]) -> str:
    if view["status"] == "closed":
        return "TASK_STATE_CANCELED" if view.get("closed_by") == "agent" else "TASK_STATE_COMPLETED"
    return STATE[view["status"]]


def _task(view: dict[str, Any], history_length: int | None = None) -> dict[str, Any]:
    history = [
        {
            "messageId": f"{view['id']}-{i}",
            "contextId": view["id"],
            "taskId": view["id"],
            "role": "ROLE_USER" if m["sender"] == "agent" else "ROLE_AGENT",
            "parts": [{"text": m["body"]}],
            "metadata": {
                "createdAt": m["created_at"],
                "sender": m["sender"],
                "automatic": bool(m.get("automatic")),
                **({"author": m["author"]} if m.get("author") else {}),
            },
        }
        for i, m in enumerate(view["messages"])
    ]
    if history_length is not None:
        history = history[-history_length:] if history_length > 0 else []
    status: dict[str, Any] = {"state": _state(view)}
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
                **({"metadata": {"author": last["author"]}} if last.get("author") else {}),
            }
    return {"id": view["id"], "contextId": view["id"], "status": status, "history": history}


def _origin(value: str) -> str:
    """scheme://host:port with the default port filled in, for comparing origins."""
    parts = urlsplit(value.strip().rstrip("/").lower())
    try:
        port = parts.port
    except ValueError:
        return ""
    port = port or {"https": 443, "http": 80}.get(parts.scheme)
    return f"{parts.scheme}://{parts.hostname}:{port}"


class RateLimited(Exception):
    def __init__(self, wait: float) -> None:
        super().__init__("rate limit exceeded")
        self.wait = wait


class A2AEndpoint:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        write_limiter: TokenBucket,
        global_write_limiter: TokenBucket,
        usage: Usage | None = None,
        browser: Browser | None = None,
    ) -> None:
        self.settings = settings
        self.usage = usage or Usage(enabled=False)
        self.browser = browser
        self.store = store
        self.write_limiter = write_limiter
        self.global_write_limiter = global_write_limiter
        self.allowed_origin = _origin(settings.public_base_url)

    async def handle(self, request: Request) -> Response:
        response = await self._handle(request)
        self._count_refusal(request, response)
        return response

    def _count_refusal(self, request: Request, response: Response) -> None:
        """Count a JSON-RPC error by method and reason (docs/decisions/0025); never its content."""
        if not self.usage.enabled or not isinstance(response, JSONResponse):
            return
        try:
            error = json.loads(response.body).get("error")
        except (ValueError, AttributeError):
            return
        if isinstance(error, dict):
            method = getattr(request.state, "rpc_method", None)
            self.usage.count(usage_module.refusal_metric("a2a", method, error.get("code")))

    async def _handle(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and _origin(origin) != self.allowed_origin:
            return _error(None, INVALID_REQUEST, "origin not allowed", 403)
        try:
            msg = json.loads(await request.body())
            json.dumps(msg, ensure_ascii=False).encode("utf-8")  # rejects lone surrogates
        except (ValueError, UnicodeError, RecursionError):
            return _error(None, PARSE_ERROR, "parse error: send one JSON-RPC 2.0 request", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(None, INVALID_REQUEST, "invalid request: send one JSON-RPC 2.0 request per POST", 400)
        if "id" not in msg:
            return Response(status_code=202)  # a notification: no answer, and no method here changes anything
        msg_id = msg["id"]
        if not (isinstance(msg_id, str) and len(msg_id) <= 256) and not (
            isinstance(msg_id, int) and not isinstance(msg_id, bool)
        ):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer", 400)
        # A2A 1.0 clients send the version; an empty or missing one means 0.3, served with the 0.3 method names.
        version = (request.headers.get("a2a-version") or request.query_params.get("A2A-Version") or "").strip()
        method = msg["method"]
        request.state.rpc_method = method
        v03 = version in V03_VERSIONS and (method in V03_METHODS or method in V03_UNSUPPORTED | V03_PUSH_METHODS)
        if version != PROTOCOL_VERSION and not v03:
            shown = version[:20] or "missing (means 0.3)"
            self.usage.count("a2a:version-refused:" + ("0.3" if version in V03_VERSIONS else "other"))
            return _error(
                msg_id,
                VERSION_NOT_SUPPORTED,
                f"A2A version {shown} is not supported for {method[:64]}; send A2A-Version: 1.0 with the 1.0 "
                "methods, or use the 0.3 methods message/send, tasks/get and tasks/cancel",
            )
        params = msg.get("params", {})
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        if v03:
            self.usage.count("a2a:dialect:0.3")
            if method in V03_PUSH_METHODS:
                return _error(msg_id, PUSH_NOT_SUPPORTED, "push notifications are not supported by this agent")
            if method in V03_UNSUPPORTED:
                return _error(msg_id, UNSUPPORTED_OPERATION, f"{method} is not supported by this agent")
            method = V03_METHODS[method]
            if method == "SendMessage":
                try:
                    params = {**params, "message": _from_v03_message(params.get("message"))}
                except A2AError as exc:
                    return _error(msg_id, exc.code, str(exc))
        token = _bearer(request)
        known = method in ("SendMessage", "GetTask", "CancelTask") or method in PUSH_METHODS or method in UNSUPPORTED
        self.usage.count(f"a2a:method:{method if known else 'unknown'}")
        try:
            if method == "SendMessage":
                result = await run_in_threadpool(self._send_message, request, params, token)
            elif method == "GetTask":
                result = await run_in_threadpool(self._get_task, params, token)
            elif method == "CancelTask":
                result = await run_in_threadpool(self._cancel_task, request, params, token)
            elif method in PUSH_METHODS:
                raise A2AError(PUSH_NOT_SUPPORTED, "push notifications are not supported by this agent")
            elif method in UNSUPPORTED:
                raise A2AError(UNSUPPORTED_OPERATION, f"{method} is not supported by this agent")
            else:
                raise A2AError(METHOD_NOT_FOUND, f"Method not found: {method[:64]}")
        except A2AError as exc:
            return _error(msg_id, exc.code, str(exc))
        except HandleUnavailable as exc:
            return _error(msg_id, INVALID_PARAMS, str(exc))
        except RateLimited as exc:
            retry = max(1, math.ceil(exc.wait))
            response = _error(msg_id, RATE_LIMITED, f"rate limit exceeded; retry in {retry} s", 429)
            response.headers["Retry-After"] = str(retry)
            return response
        if v03:
            # 0.3 returns the task itself, also for message/send.
            if method == "SendMessage" and "message" in result:
                result = _v03_direct_message(result["message"])
            else:
                result = to_v03_task(result["task"] if method == "SendMessage" else result)
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result}, headers={"Cache-Control": "no-store"})

    async def handle_rest(self, request: Request, action: str, task_id: str | None = None) -> Response:
        """The HTTP+JSON binding (docs/decisions/0028): POST .../v1/message:send, GET .../v1/tasks/{id},
        POST .../v1/tasks/{id}:cancel. Same handlers, limits and token as JSON-RPC; A2A-Version 1.0 gets 1.0
        shapes, a missing version (0.3) gets 0.3 shapes."""
        origin = request.headers.get("origin")
        if origin is not None and _origin(origin) != self.allowed_origin:
            return _rest_error(INVALID_REQUEST, "origin not allowed")
        version = (request.headers.get("a2a-version") or request.query_params.get("A2A-Version") or "").strip()
        method = {"send": "SendMessage", "get": "GetTask", "cancel": "CancelTask"}[action]
        if version != PROTOCOL_VERSION and version not in V03_VERSIONS:
            self.usage.count(usage_module.refusal_metric("a2a", method, VERSION_NOT_SUPPORTED))
            return _rest_error(VERSION_NOT_SUPPORTED, f"A2A version {version[:20]} is not supported; use 1.0")
        v03 = version != PROTOCOL_VERSION
        self.usage.count(f"a2a:rest:{method}" + (":0.3" if v03 else ""))
        token = _bearer(request)
        try:
            if action == "send":
                try:
                    params = json.loads(await request.body())
                    json.dumps(params, ensure_ascii=False).encode("utf-8")  # rejects lone surrogates
                except (ValueError, UnicodeError, RecursionError):
                    raise A2AError(INVALID_PARAMS, "the body must be one JSON object") from None
                if not isinstance(params, dict):
                    raise A2AError(INVALID_PARAMS, "the body must be one JSON object")
                if v03:
                    params = {**params, "message": _from_v03_message(params.get("message"))}
                result = await run_in_threadpool(self._send_message, request, params, token)
                body: dict[str, Any] = to_v03_task(result["task"]) if v03 else result
            elif action == "get":
                raw = request.query_params.get("historyLength")
                plain = raw is not None and raw.isascii() and raw.isdecimal() and len(raw) <= 9
                history = int(raw) if plain else None  # type: ignore[arg-type]
                if raw is not None and history is None:
                    raise A2AError(INVALID_PARAMS, "historyLength must be a non-negative integer")
                task = await run_in_threadpool(self._get_task, {"id": task_id, "historyLength": history}, token)
                body = to_v03_task(task) if v03 else task
            else:
                task = await run_in_threadpool(self._cancel_task, request, {"id": task_id}, token)
                body = to_v03_task(task) if v03 else task
        except A2AError as exc:
            self.usage.count(usage_module.refusal_metric("a2a", method, exc.code))
            return _rest_error(exc.code, str(exc))
        except HandleUnavailable as exc:
            return _rest_error(INVALID_PARAMS, str(exc))
        except RateLimited as exc:
            retry = max(1, math.ceil(exc.wait))
            self.usage.count(usage_module.refusal_metric("a2a", method, RATE_LIMITED))
            response = JSONResponse(
                {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "rate limit exceeded"}},
                status_code=429,
            )
            response.headers["Retry-After"] = str(retry)
            return response
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    def _charge_write(self, request: Request) -> None:
        """Charge one write, after the call has been validated (the middleware counted the POST as a read)."""
        wait = self.write_limiter.take(client_key(request.scope, self.settings.trust_proxy_headers))
        if wait == 0:
            wait = self.global_write_limiter.take(GLOBAL_KEY)
        if wait > 0:
            raise RateLimited(wait)

    def _send_message(self, request: Request, params: dict[str, Any], token: str | None) -> dict[str, Any]:
        message = params.get("message")
        text = _text_of(message)
        assert isinstance(message, dict)
        configuration = params.get("configuration")
        if isinstance(configuration, dict):
            modes = configuration.get("acceptedOutputModes")
            if isinstance(modes, list) and modes and "text/plain" not in modes:
                raise A2AError(CONTENT_TYPE_NOT_SUPPORTED, "this agent only answers in text/plain")
        task_id, context_id = message.get("taskId"), message.get("contextId")
        if not task_id and token is None:
            # A first message: many clients put a contextId of their own on it. Without a taskId and a token it
            # cannot be a follow-up here, so it starts a new task (whose contextId is then the task id).
            context_id = None
        if task_id and context_id and task_id != context_id:
            raise A2AError(INVALID_PARAMS, "taskId and contextId must match (they are the same here)")
        task_id = task_id or context_id
        meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if not task_id and meta.get("skill") == READ_WEB_PAGE and self.browser is not None:
            return {"message": self._read_web_page(request, meta, text)}
        if task_id:
            if not isinstance(task_id, str) or len(task_id) > 64:
                raise A2AError(INVALID_PARAMS, "taskId must be a string")
            try:
                body = MessageIn.model_validate({"message": text})
            except ValidationError:
                raise A2AError(INVALID_PARAMS, "the message contains characters that are not allowed") from None
            current = self.store.get_request(task_id, token) if token else None
            if current is None:
                raise A2AError(TASK_NOT_FOUND, "task not found or wrong token")
            if current["status"] == "closed":
                raise A2AError(UNSUPPORTED_OPERATION, "this task is closed; start a new task and mention its id")
            self._charge_write(request)
            try:
                view = self.store.add_agent_message(
                    task_id, token, body.message, self.settings.max_messages_per_request
                )
            except ConversationFull:
                raise A2AError(UNSUPPORTED_OPERATION, "this task is full; start a new task and mention it") from None
            if view is None:
                raise A2AError(TASK_NOT_FOUND, "task not found or wrong token")
            return {"task": _task(view)}

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
        self.store.check_handle(body_in.handle, body_in.handle_token)  # refuse before charging the budget
        self._charge_write(request)
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

    def _read_web_page(self, request: Request, meta: dict[str, Any], text: str) -> dict[str, Any]:
        """The read-web-page skill (docs/decisions/0029): answered with a direct message, nothing is stored."""
        url = _metadata_str(meta, "url") or text.strip()
        fmt = _metadata_str(meta, "format") or "text"
        if fmt not in ("text", "markdown"):
            raise A2AError(CONTENT_TYPE_NOT_SUPPORTED, "format must be 'text' or 'markdown' here")
        client = client_key(request.scope, self.settings.trust_proxy_headers)
        try:
            found = self.browser.read(client, url, fmt)  # type: ignore[union-attr]
        except BrowseRefused as exc:
            if exc.outcome == "refused":
                raise A2AError(INVALID_PARAMS, str(exc)) from None
            if exc.outcome in ("limited", "busy"):
                raise RateLimited(exc.retry_after or 60) from None
            parts = [{"text": f"The page could not be read ({exc.outcome}): {exc}"}]
            return _agent_message(parts, {"outcome": exc.outcome})
        header = f"{found['note']}\nPage: {found['final_url'] or found['url']} (HTTP {found['status']})"
        if found["title"]:
            header += f"\nTitle: {found['title']}"
        info = {k: found[k] for k in ("url", "final_url", "status", "title", "format", "truncated", "fetched_at")}
        parts = [{"text": header}, {"text": found["content"] or "(no visible text)"}]
        return _agent_message(parts, info | {"untrusted": True})

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

    def _cancel_task(self, request: Request, params: dict[str, Any], token: str | None) -> dict[str, Any]:
        task_id = params.get("id")
        if not isinstance(task_id, str) or len(task_id) > 64:
            raise A2AError(INVALID_PARAMS, "params.id must be the task id")
        current = self.store.get_request(task_id, token) if token else None
        if current is None or token is None:
            raise A2AError(TASK_NOT_FOUND, "task not found or wrong token")
        if current["status"] == "closed":
            raise A2AError(TASK_NOT_CANCELABLE, "this task is already closed")
        self._charge_write(request)
        view = self.store.close_request(task_id, token)
        if view is None:
            raise A2AError(TASK_NOT_FOUND, "task not found or wrong token")
        return _task(view)


# A2A errors in the HTTP+JSON (REST) binding: the HTTP status, and the A2A error by name.
REST_ERRORS = {
    INVALID_PARAMS: (400, "INVALID_PARAMS"),
    TASK_NOT_FOUND: (404, "TASK_NOT_FOUND"),
    TASK_NOT_CANCELABLE: (409, "TASK_NOT_CANCELABLE"),
    PUSH_NOT_SUPPORTED: (400, "PUSH_NOTIFICATION_NOT_SUPPORTED"),
    UNSUPPORTED_OPERATION: (400, "UNSUPPORTED_OPERATION"),
    CONTENT_TYPE_NOT_SUPPORTED: (415, "CONTENT_TYPE_NOT_SUPPORTED"),
    VERSION_NOT_SUPPORTED: (400, "VERSION_NOT_SUPPORTED"),
}


def _rest_error(code: int, message: str) -> JSONResponse:
    status, reason = REST_ERRORS.get(code, (400, "INVALID_REQUEST"))
    body = {"error": {"code": status, "status": reason, "message": message}}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _error(msg_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
