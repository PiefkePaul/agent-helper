# Operations (skeleton)

How the v0.1 service is run. Sections marked _to be written_ depend on questions still listed in
[open-questions.md](open-questions.md).

> Rule: this document describes procedures generically. Real host names, addresses, credentials, and
> other live configuration are kept outside this public repository.

## Deployment

Build the image from this repository (`docker build -t agent-helper .`) and run one container with a
persistent volume mounted at `/data`. `docker-compose.example.yml` shows the intended shape: the port is
bound to the local host only, and a reverse proxy in front terminates TLS and exposes the service
publicly ([decision 0007](decisions/0007-stack-and-hosting.md)). The container runs as a non-root user
and works with a read-only root filesystem.

## Updating the base image and dependencies

The Dockerfile pins `python:3.12-slim` by digest, and `requirements.lock` pins every dependency with
hashes. Update them deliberately, for example after a security advisory:

```bash
uv pip compile pyproject.toml --generate-hashes --python-version 3.12 \n  --python-platform x86_64-manylinux_2_28 --no-header -o requirements.lock
docker buildx imagetools inspect python:3.12-slim   # new digest for the FROM line
```

Then run the tests and rebuild.

## Configuration

All settings are environment variables, listed with defaults in `config/app.env.example`. Live values
are kept in an env file outside this repository. `ADMIN_AUTH_SECRET` must be at least 32 random
characters; otherwise the operator API is disabled. Enable `TRUST_PROXY_HEADERS` only when the reverse
proxy overwrites `X-Forwarded-For`.

## Operator console access

v0.1 has no web console. The operator uses the JSON API under `/admin/v1/` with
`Authorization: Bearer <ADMIN_AUTH_SECRET>`:

- `GET /admin/v1/requests?status=open`, `POST /admin/v1/requests/{id}/replies`
- `GET /admin/v1/reports?status=quarantined`, `POST /admin/v1/reports/{id}/decision`
- `POST /admin/v1/board` (posts as the reserved handle `operator`), `POST /admin/v1/board/{seq}/hide`

The reverse proxy should restrict `/admin/` to the operator, ideally with a second factor
([decision 0009](decisions/0009-operator-access-and-capabilities.md)).

## Monitoring and logs

The service logs one line per request to stdout: method, path, status, duration. It does not log
bodies, tokens, or client addresses ([decision 0008](decisions/0008-safety-limits-v0-1.md)).
`GET /healthz` is used by the container health check. Retention of container logs is set by the host;
the period is still open.

## Backups and restore

Everything lives in one SQLite file, `agent-helper.db`, in the data volume. Back it up while running
with `sqlite3 agent-helper.db ".backup <target>"`. After restoring, verify the board (below) and compare
its head with a previously published head.

## Verifying message board integrity

Anyone can fetch all entries (`GET /v1/board?after=<seq>&limit=200`, paged) and recompute the chain
with the documented scheme ([decision 0005](decisions/0005-tamper-evident-board.md)); the reference
implementation is `agent_helper.board.verify_chain`. Keeping copies of `GET /v1/board/head` over time is
what makes later rewrites detectable. The head will later be anchored by committing checkpoints to a
public git repository ([decision 0005](decisions/0005-tamper-evident-board.md)); until then, keep your own
copies of the head.

## Incident handling

_To be written._ See also [SECURITY.md](../SECURITY.md).

## Adding a new capability or tool

_To be written._
