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
}
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
            self.count(endpoint, client_family(_header(scope, b"user-agent")))

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
