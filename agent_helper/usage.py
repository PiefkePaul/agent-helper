"""Anonymous usage counts (docs/decisions/0023): is anyone, and which kind of client, finding the service?

Only daily totals are kept: per day, per counted thing (an endpoint group, an MCP tool, an A2A method, a probe
for a well-known file this service does not have) and per client family (a coarse class derived from the
User-Agent header, such as "ai_crawler" or "http_library"). No address, no full User-Agent, no time of day,
no request content, nothing per client. Counts live in memory and are added to the database periodically.

Everything counted here is self-reported by clients and trivially faked; the numbers show tendencies, not
proof that a particular agent came by.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from starlette.types import Scope

# Client families, checked in this order; the first match wins. Matched case-insensitively as substrings.
AI_CRAWLERS = (
    "gptbot", "oai-searchbot", "claudebot", "claude-searchbot", "anthropic-ai", "perplexitybot", "ccbot",
    "google-extended", "applebot-extended", "bytespider", "amazonbot", "meta-externalagent", "cohere-ai",
    "diffbot", "youbot", "exabot", "timpibot", "ai2bot",
)  # fmt: skip
# Fetchers that act for a person or an agent in the moment, as their operators document them.
AI_AGENTS = ("chatgpt-user", "claude-user", "perplexity-user", "mistralai-user", "duckassistbot")
SEARCH_CRAWLERS = (
    "googlebot", "bingbot", "duckduckbot", "yandexbot", "baiduspider", "applebot", "seznambot", "petalbot",
    "qwantbot", "mojeekbot", "yeti/",
)  # fmt: skip
MONITORS = ("uptime", "monitor", "pingdom", "statuscake", "healthcheck")
HTTP_LIBRARIES = (
    "python-requests", "python-httpx", "python-urllib", "aiohttp", "httpx", "axios", "node-fetch", "undici",
    "node", "curl/", "wget/", "go-http-client", "okhttp", "java/", "libwww", "httpie", "postmanruntime",
    "deno", "bun/", "reqwest", "ruby", "php", "dart",
)  # fmt: skip
OTHER_BOT_HINTS = ("bot", "crawler", "spider", "scraper", "fetch")

FAMILIES = ("ai_crawler", "ai_agent", "search_crawler", "monitor", "http_library", "browser", "other_bot", "none")

# Well-known files and other paths that agents and tools are known to probe for. A 404 on one of these is
# counted by name (it shows what visitors expect); any other 404 is counted as "not_found".
PROBES = {
    "/.well-known/ai-plugin.json": "probe:ai-plugin.json",
    "/.well-known/agents.json": "probe:agents.json",
    "/.well-known/agent.json": "probe:agent.json",
    "/.well-known/mcp.json": "probe:mcp.json",
    "/.well-known/mcp": "probe:mcp",
    "/.well-known/oauth-protected-resource": "probe:oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp": "probe:oauth-protected-resource",
    "/.well-known/oauth-authorization-server": "probe:oauth-authorization-server",
    "/.well-known/openid-configuration": "probe:openid-configuration",
    "/.well-known/security.txt": "probe:security.txt",
    "/.well-known/llms.txt": "probe:well-known-llms.txt",
    "/agents.json": "probe:root-agents.json",
    "/agents.txt": "probe:agents.txt",
    "/ai.txt": "probe:ai.txt",
    "/llms-full.txt": "probe:llms-full.txt",
    "/openapi.yaml": "probe:openapi.yaml",
    "/sse": "probe:sse",
    "/mcp/sse": "probe:sse",
    "/favicon.ico": "probe:favicon.ico",
}

# Exact paths counted by name. Everything under /v1/ is counted by its first segment and method.
NAMED = {
    "/llms.txt": "llms.txt",
    "/robots.txt": "robots.txt",
    "/sitemap.xml": "sitemap.xml",
    "/openapi.json": "openapi.json",
    "/.well-known/agent-helper.json": "well-known:agent-helper.json",
    "/.well-known/agent-card.json": "well-known:agent-card.json",
    "/.well-known/api-catalog": "well-known:api-catalog",
    "/.well-known/mcp/server-card.json": "well-known:mcp-server-card",
    # Answered since docs/decisions/0024 (before, they were 404 probes).
    "/.well-known/agent.json": "compat:agent.json",
    "/.well-known/mcp.json": "compat:mcp.json",
    "/mcp.json": "compat:mcp.json",
    "/.well-known/ai-plugin.json": "compat:ai-plugin.json",
    "/.well-known/agents.json": "compat:agents.json",
    "/agents.json": "compat:agents.json",
    "/agents.txt": "compat:agents.txt",
    "/.well-known/llms.txt": "compat:well-known-llms.txt",
    # Answered since docs/decisions/0028.
    "/favicon.ico": "favicon.ico",
    "/a2a/health": "a2a:health",
    "/a2a/.well-known/agent-card.json": "compat:a2a-agent-card",
    "/a2a/v1/message:send": "a2a:rest:send",
    "/.well-known/agent-card.json/v1/message:send": "a2a:rest:send",
    "/mcp/server-card": "compat:mcp-server-card",
    "/mcp/server-card.json": "compat:mcp-server-card",
    "/.well-known/mcp": "compat:mcp-server-card",
    "/.well-known/glama.json": "compat:glama.json",
    "/.well-known/ard.json": "compat:index",
    "/.well-known/agent-directory.json": "compat:index",
    "/.well-known/ai-catalog.json": "compat:index",
    "/.well-known/x402": "compat:payment",
    "/.well-known/x402.json": "compat:payment",
    "/.well-known/payment-manifest.json": "compat:payment",
    "/.well-known/mpp.json": "compat:payment",
    # Answered since docs/decisions/0032.
    "/agent-directory.json": "compat:index",
    "/.well-known/payment-manifest": "compat:payment",
    "/.well-known/mpp": "compat:payment",
    "/pricing": "compat:payment",
    **dict.fromkeys(
        ("/docs", "/documentation", "/about", "/help", "/faq", "/features", "/support", "/contact"), "compat:help-page"
    ),
}
# Other URLs clients try for the MCP endpoint (docs/decisions/0028), counted together by method.
MCP_ALIAS_PATHS = frozenset(
    {
        "/mcp/sse", "/sse", "/api/mcp", "/mcp/mcp", "/a2a/mcp", "/llms.txt/mcp", "/.well-known/agent-card.json/mcp",
        "/.well-known/agent-helper.json/mcp", "/.well-known/mcp.json/mcp", "/.well-known/mcp/server-card.json/mcp",
    }
)  # fmt: skip
V1_SEGMENT = re.compile(r"^/v1/([a-z][a-z0-9-]{0,31})(?:/|$)")
# Methods are counted by name only when they are ordinary HTTP methods, so a caller cannot invent metrics.
METHODS = frozenset({"get", "head", "post", "put", "patch", "delete", "options"})

# Self-reported MCP client names are kept only when they start with one of these known client names, and
# then only as that name; anything else counts as "other". A free-form name could carry an agent's own
# handle and give it a trace per day, and random names would grow the table.
KNOWN_MCP_CLIENTS = (
    "claude-ai", "claude-code", "claude-desktop", "claude", "chatgpt", "openai", "codex", "cursor", "vscode",
    "visual-studio-code", "github-copilot", "copilot", "windsurf", "cline", "roo-code", "continue", "zed",
    "jetbrains", "goose", "gemini-cli", "gemini", "amazon-q", "kiro", "warp", "raycast", "librechat",
    "open-webui", "langchain", "langgraph", "llamaindex", "fast-agent", "mcp-inspector", "inspector",
    "mcp-remote", "mcp-use", "smithery", "glama", "pulsemcp", "mistral", "le-chat", "perplexity",
    "postman", "opencode", "crush", "amp", "witsy", "5ire", "cherry-studio", "lm-studio", "msty",
)  # fmt: skip
CLIENT_NAME = re.compile(r"[^a-z0-9]+")


def client_family(user_agent: str) -> str:
    ua = user_agent.lower()
    if not ua.strip():
        return "none"
    for family, needles in (
        ("ai_agent", AI_AGENTS),
        ("ai_crawler", AI_CRAWLERS),
        ("search_crawler", SEARCH_CRAWLERS),
        ("monitor", MONITORS),
    ):
        if any(n in ua for n in needles):
            return family
    if any(ua.startswith(n) or f" {n}" in ua for n in HTTP_LIBRARIES):
        return "http_library"
    if any(n in ua for n in OTHER_BOT_HINTS):
        return "other_bot"
    if ua.startswith("mozilla/"):
        return "browser"
    return "other_bot"


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")[:512]
    return ""


def endpoint_of(scope: Scope, status: int, v1_segments: frozenset[str] = frozenset()) -> str | None:
    """What a request is counted as, or None when it is not counted (operator pages, health checks).

    Every part of the result comes from a fixed set: path segments under /v1/ only when the service has a
    route there (``v1_segments``), methods only when they are ordinary HTTP methods."""
    path = scope["path"]
    method = scope["method"].lower()
    method = method if method in METHODS else "other"
    if path == "/admin" or path.startswith("/admin/") or path == "/healthz":
        return None
    if status == 404 and method in ("get", "head"):
        return PROBES.get(path, "not_found")
    if path == "/":
        return "landing:html" if "text/html" in _header(scope, b"accept") else "landing:text"
    if path in NAMED:
        return NAMED[path]
    if path in ("/mcp", "/a2a"):
        return f"{path[1:]}:{method}"
    if path in MCP_ALIAS_PATHS:
        return f"mcp-alias:{method}"
    if path.startswith("/a2a/v1/tasks/"):
        return f"a2a:rest:tasks:{method}"
    match = V1_SEGMENT.match(path)
    if match:
        return f"v1:{match.group(1)}:{method}" if match.group(1) in v1_segments else "v1:other"
    if path.startswith("/v1/"):
        return "v1:other"
    return PROBES.get(path, "other")


def normalize_client_name(name: object) -> str | None:
    """A self-reported client name (MCP clientInfo.name) reduced to a known client name, or "other"."""
    if not isinstance(name, str):
        return None
    label = CLIENT_NAME.sub("-", name.strip().lower()[:200]).strip("-")
    if not label:
        return None
    for known in KNOWN_MCP_CLIENTS:
        if label == known or label.startswith(known + "-"):
            return known
    return "other"


# Refused calls (docs/decisions/0025): which protocol, which method, why. Every part from a fixed set.
JSONRPC_ERRORS = {
    -32700: "parse_error",
    -32600: "invalid_request",
    -32601: "method_not_found",
    -32602: "invalid_params",
    -32603: "internal_error",
    -32001: "task_not_found",
    -32002: "task_not_cancelable",
    -32003: "push_not_supported",
    -32004: "unsupported_operation",
    -32005: "content_type_not_supported",
    -32009: "version_not_supported",
    -32020: "header_mismatch",
    -32022: "unsupported_protocol_version",
    -32029: "rate_limited",
}
A2A_METHODS = frozenset(
    {
        "SendMessage", "GetTask", "CancelTask", "SendStreamingMessage", "SubscribeToTask", "ListTasks",
        "GetExtendedAgentCard", "CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs", "DeleteTaskPushNotificationConfig", "message/send", "message/stream",
        "tasks/get", "tasks/cancel", "tasks/resubscribe", "agent/getAuthenticatedExtendedCard",
        "tasks/pushNotificationConfig/set", "tasks/pushNotificationConfig/get",
        "tasks/pushNotificationConfig/list", "tasks/pushNotificationConfig/delete",
    }
)  # fmt: skip
MCP_METHODS = frozenset(
    {
        "initialize", "ping", "server/discover", "tools/list", "tools/call", "resources/list",
        "resources/read", "resources/templates/list", "prompts/list", "prompts/get", "completion/complete",
        "logging/setLevel", "notifications/initialized",
    }
)  # fmt: skip
REFUSAL_PREFIX = "refused:"


def refusal_metric(protocol: str, method: object, code: object) -> str:
    """`refused:<protocol>:<method>:<reason>` for a JSON-RPC error, with every part from a fixed set."""
    known = A2A_METHODS if protocol == "a2a" else MCP_METHODS
    label = method if isinstance(method, str) and method in known else "unknown"
    reason = JSONRPC_ERRORS.get(code, "other") if isinstance(code, int) else "other"
    return f"{REFUSAL_PREFIX}{protocol}:{label}:{reason}"


class Usage:
    """Thread-safe in-memory daily counters, flushed into the store."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._counts: Counter[tuple[str, str, str]] = Counter()
        # Path segments under /v1/ that the service has routes for; set once the app is built.
        self.v1_segments: frozenset[str] = frozenset()

    @staticmethod
    def _day() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def count(self, metric: str, family: str = "") -> None:
        if not self.enabled:
            return
        with self._lock:
            self._counts[(self._day(), metric, family)] += 1

    def record_http(self, scope: Scope, status: int) -> None:
        if not self.enabled or scope.get("type") != "http":
            return
        endpoint = endpoint_of(scope, status, self.v1_segments)
        if endpoint is not None:
            family = client_family(_header(scope, b"user-agent"))
            self.count(endpoint, family)
            # Refused calls on the HTTP API: the endpoint group and the status code, nothing else. (A 404 is
            # counted as a probe above; for /v1/ it is usually a wrong token, so it is a refusal too.)
            if 400 <= status < 500 and scope["path"].startswith("/v1/"):
                group = endpoint_of(scope, 200, self.v1_segments)
                self.count(f"{REFUSAL_PREFIX}http:{group}:{status}", family)

    def record_client_name(self, protocol: str, name: object) -> None:
        """The software name a client reports about itself (for example in MCP initialize)."""
        label = normalize_client_name(name)
        if label is not None:
            self.count(f"{protocol}:client", label)

    def take(self) -> list[tuple[str, str, str, int]]:
        """Remove and return the counts gathered so far."""
        with self._lock:
            counts, self._counts = self._counts, Counter()
        return [(day, metric, family, n) for (day, metric, family), n in counts.items()]

    def flush(self, write: Callable[[list[tuple[str, str, str, int]]], None]) -> None:
        rows = self.take()
        if rows:
            write(rows)
