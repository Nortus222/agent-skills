# EMWS API CLI skill design

## Goal

Give agents a command-line client for the e-manage|ONE API (EMWS) so they can verify their work against
production and staging without asking Ihor for credentials. Credentials are configured once on the machine.
Every failure produces a structured, complete error report, and every call is logged so an agent can inspect
what happened after the fact.

Success means an agent can:

- Call any EMWS route on staging or production through APIM with a named profile.
- Tell from the output alone whether a failure came from configuration, authentication, a blocked write, the
  network, APIM, or the API, and what to do next.
- Find a past call, including its full request and response, from the call log.
- Never see or print a secret.

## Package

Add `skills/emws-api/` to `Nortus222/agent-skills`. Installing the repository places it at
`~/.agents/skills/emws-api`.

```
skills/emws-api/
  SKILL.md
  agents/openai.yaml
  references/profiles.example.json
  scripts/emws.ts          # entry: argument parsing and dispatch
  scripts/lib/config.ts    # profiles.json and .env loading, permission check, validation
  scripts/lib/auth.ts      # API-key header, Basic login, token cache
  scripts/lib/http.ts      # request, retries, traceparent, timeout
  scripts/lib/errors.ts    # error envelope, source detection, hint table, exit codes
  scripts/lib/redact.ts    # secret masking shared by output and log
  scripts/lib/log.ts       # JSONL write, retention, queries
  scripts/lib/spec.ts      # swagger fetch and cache, routes, describe, nearest route
tests/emws-api/*.test.ts
```

The CLI is TypeScript run directly by Node 24 through native type stripping:
`node ~/.agents/skills/emws-api/scripts/emws.ts …`. It has **no runtime dependencies**. `npx skills add` copies
files and never runs `npm install`, so only Node built-ins are available (`fetch`, `util.parseArgs`,
`node:test`, `node:fs`, `node:crypto`). `SKILL.md` documents an optional `emws` shell alias; the rest of this
document writes commands as `emws`.

## Configuration

Configuration lives in `~/.config/emws/`, outside the skill, so it survives `npx skills update` and is never in
the public repository.

| Path | Contents | Mode |
| --- | --- | --- |
| `profiles.json` | Environments and profiles. No secrets. | any |
| `.env` | Secret values, `NAME=value` per line. | must be `600` |
| `tokens/<profile>.json` | Cached JWT for Basic profiles. | `600` |
| `cache/spec-<env>.json` | Cached swagger document. | any |
| `logs/YYYY-MM-DD.jsonl` | Call log. | `600` |

The CLI refuses to run (exit 2, code `ENV_PERMS`) when `.env` is readable by group or others.

### profiles.json

```json
{
  "environments": {
    "prod":    { "apiBase": "https://emanageone.azure-api.net/emws",
                 "specUrl": "https://emws-data.azurewebsites.net/api/swagger.json" },
    "staging": { "apiBase": "https://emanageone.azure-api.net/emws-staging",
                 "specUrl": "https://emws-data-staging.azurewebsites.net/api/swagger.json" },
    "local":   { "apiBase": "http://localhost:7071/api",
                 "specUrl": "http://localhost:7071/api/swagger.json" }
  },
  "profiles": {
    "staging-admin": { "env": "staging", "auth": "apiKey", "secret": "STAGING_ADMIN_KEY" },
    "prod-user":     { "env": "prod", "auth": "basic", "username": "agent@example.com",
                       "secret": "PROD_USER_PASSWORD", "clientId": "…", "dbId": "…" },
    "prod-admin":    { "env": "prod", "auth": "apiKey", "secret": "PROD_KEY" }
  },
  "defaultProfile": "staging-admin"
}
```

- An environment is data: a base URL for calls and a URL for the OpenAPI document. Adding a host, such as a
  direct Function App URL that bypasses APIM, needs no code change.
- The swagger document is not published through APIM; it is served unauthenticated by the Function App at
  `/api/swagger.json`. That is why `specUrl` is separate from `apiBase`.
- `secret` names a variable in `.env`; the value never appears in `profiles.json`.
- `allowWrites` is optional. It defaults to `false` for an environment named `prod` and `true` otherwise.
- Validation is hand-written and reports the exact path of a bad field, for example
  `profiles.prod-user.dbId is required for auth "basic"` (exit 2, code `CONFIG_INVALID`).

`references/profiles.example.json` ships the structure above with placeholder credentials.

### Profile selection

`-p <profile>`, then the `EMWS_PROFILE` environment variable, then `defaultProfile`.

## Authentication

The APIM gateway accepts either `Authorization: Bearer <jwt>` (validated against the
`emwsauthentication.azurewebsites.net` issuer) or `x-api-key`. No subscription key is required.

- **`apiKey` profiles** send `x-api-key: <secret>` on every call. No token exchange; the server resolves the
  tenant from the key.
- **`basic` profiles** call `POST {apiBase}/authenticateBasic` with JSON
  `{ "username", "password", "clientId", "dbId" }`. The response body is the raw JWT as text. The CLI decodes
  the `exp` claim (one hour after issue) and caches the token until 60 seconds before it expires. On a 401
  from a data call it logs in once more and retries the call once; a second 401 is reported.
- A failed login is an `auth` error with code `LOGIN_FAILED` and includes the login response envelope.

`emws whoami -p <profile>` verifies credentials. For a JWT it prints the decoded `sub`, `database`,
`client_id`, and `exp`. For an API key it makes one cheap authenticated GET and reports success and the
resolved tenant when the response exposes it.

## Write protection

A request with method `POST`, `PUT`, `PATCH`, or `DELETE` on a profile whose effective `allowWrites` is
`false` fails before any network call: kind `blocked`, code `WRITE_BLOCKED`, exit 4. The message names the
profile and the setting that would allow it. The login call for `basic` profiles is exempt. When a production
profile does allow writes, each write prints a warning line on stderr.

## Commands

```
emws <get|post|put|patch|delete> <path> [-p profile] [--query k=v ...]
     [--body @file | --body - | --body '<json>'] [--header K:V ...] [--timeout 30s] [--json] [-v]
emws routes [filter] [-p profile] [--refresh]
emws describe <METHOD> <path> [-p profile] [--refresh]
emws whoami [-p profile]
emws profiles
emws log [<callId>] [--errors] [--last N] [--since 15m] [--profile P] [--path substr] [--here] [--json]
```

Paths are the API's routes as written (`/projects/123`). The CLI appends them to `apiBase` and adds nothing.

## Request flow

1. Load and validate configuration; check `.env` permissions.
2. Apply write protection.
3. Resolve credentials (header, cached token, or login).
4. Build the URL from `apiBase`, the path, and `--query` pairs.
5. Send a fresh W3C `traceparent` and an `x-emws-cli-call-id` header so the call can be correlated with APIM
   and App Insights.
6. Send the request with the timeout (default 30 s).
7. Write the log line, then print the result.

Retries: only for `GET`, only on network errors and 502/503/504, at most two retries with backoff. Writes and
4xx responses are never retried. There is no pagination helper; callers pass `--query start=… --query limit=…`.

## Output contract

Agents parse stdout, so it is stable.

- **Success, default:** stdout is the response body only (JSON pretty-printed, anything else verbatim). stderr
  gets one summary line: `200 GET /projects/123 · 142ms · staging-admin · call c_8f2a`. Exit 0.
- **Success, `--json`:** stdout is one envelope `{ ok: true, status, headers, body, timingMs, callId, profile, env }`.
- **Failure, default:** a readable rendering of the error envelope on stderr; stdout is empty.
- **Failure, `--json`:** the error envelope on stdout.
- `-v` adds request and response headers to stderr. Secrets are masked in all cases.

## Error envelope

Every failure, from configuration through the API, produces the same shape:

```json
{
  "ok": false,
  "error": {
    "kind": "server",
    "code": "HTTP_500",
    "message": "Object reference not set to an instance of an object.",
    "hint": "Server-side exception. Look up the correlation id in App Insights (query below)."
  },
  "request": {
    "method": "GET", "url": "https://…/emws-staging/projects/123",
    "profile": "staging-admin", "env": "staging",
    "headers": { "x-api-key": "***" }, "bodyPreview": null
  },
  "response": {
    "status": 500, "headers": { },
    "body": { "StatusCode": 500, "Message": "…", "ErrorCode": null },
    "source": "api"
  },
  "correlation": {
    "callId": "c_8f2a", "traceparent": "00-…", "correlationId": "…",
    "kql": "union requests, exceptions, traces | where …"
  },
  "timingMs": 412
}
```

### Fields

- `kind` is one of `config`, `auth`, `blocked`, `network`, `timeout`, `client`, `server`.
- `code` is specific: `CONFIG_INVALID`, `ENV_PERMS`, `SECRET_MISSING`, `LOGIN_FAILED`, `WRITE_BLOCKED`, `DNS`,
  `CONN_REFUSED`, `TLS`, `TIMEOUT`, or `HTTP_<status>`.
- `response.source`:
  - `api` when the body matches the EMWS error shape `{ StatusCode, Message, ErrorCode }` in any key casing.
  - `apim` when it matches APIM's own `{ statusCode, message }` rejection.
  - `unknown` otherwise; the raw body is included up to 4 KB.
- `request.bodyPreview` holds at most 2 KB of the request body, masked. The full body goes to the log.
- `correlation.correlationId` is the `x-correlation-id` response header, which EMWS sets on error responses to
  the Functions invocation id.
- `correlation.kql` is a ready-to-run App Insights query built from the correlation id and traceparent, present
  when either exists.
- Status 422 is classed as `server`: EMWS returns it for unhandled exceptions.

### Hints

`hint` comes from a table keyed on kind, status, source, and `ErrorCode`, with a generic fallback. Initial
entries:

| Condition | Hint |
| --- | --- |
| 401 from `apim` | Token expired or issued by the wrong issuer; run `emws whoami`. |
| 401 from `api` | Credentials rejected by the API; check the profile's secret. |
| 403 on `/management/*` | Needs an admin-scoped API-key profile. |
| 404 and the path matches no route in the spec | Lists the closest routes from the cached spec. |
| 422 | Unhandled server exception; use the correlation id. |
| `DNS` or `CONN_REFUSED` | Wrong `apiBase`, or the local Functions host is not running. |
| `WRITE_BLOCKED` | Names the profile and `allowWrites`. |

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Usage or configuration |
| 3 | Authentication (401, login failure) |
| 4 | Blocked write |
| 5 | Client error (4xx other than 401) |
| 6 | Server error (5xx, 422) |
| 7 | Network or timeout |

## Logging

Each call appends one line to `~/.config/emws/logs/YYYY-MM-DD.jsonl`: the success or error envelope, the full
request body, and the response body up to 64 KB (marked when truncated). Each line also records `cwd`, the git
branch of `cwd` when there is one, and `EMWS_AGENT` or `CLAUDE_SESSION_ID` when set, so calls can be traced to
a worktree and session.

Masking happens before writing: `Authorization`, `x-api-key`, the `password` field of the login body, and any
value present in `.env`. The same masking applies to everything printed.

Log files older than 14 days are deleted at startup.

`emws log`:

- No arguments: the last 10 calls across all working directories, one line each (status, method, path, profile,
  ms, call id).
- `<callId>`: the full stored envelope.
- `--errors`, `--last N`, `--since <duration>`, `--profile`, `--path <substring>`: filters.
- `--here`: only calls made from the current working directory.
- `--json`: JSON lines instead of text.

There is no remote log shipping and no log level; everything is recorded.

## OpenAPI discovery

`spec.ts` fetches `specUrl` and caches it for 10 minutes per environment (`--refresh` bypasses the cache).

- `emws routes [filter]` lists method, path, and summary, filtered by substring.
- `emws describe <METHOD> <path>` prints parameters, request body schema, and response schema for one route,
  resolving `$ref`s.
- Nearest-route matching compares a requested path against the spec's route templates segment by segment and
  powers the 404 hint.

A spec fetch failure does not fail a data call; it only removes the route suggestions from the hint.

## SKILL.md

The skill is model-invoked when an agent needs to call or verify behaviour of the EMWS API. `SKILL.md` covers:

- Use it to verify a change against the API; default to staging.
- Never ask the user for credentials; run `emws whoami` first, and if no profile works, report that setup is
  needed.
- Read the error envelope and exit code; after a failure, run `emws log --errors` or `emws log <callId>`.
- Production profiles are read-only unless configured otherwise; do not work around a blocked write.
- Staging reflects a branch only after it merges to `staging` and deploys. To verify an unmerged change, run
  the Functions host locally and use a `local` profile.
- The `emws` alias, and the full `node …/emws.ts` form when the alias is absent.

`agents/openai.yaml` supplies the skill-list metadata, as in `deploy-mobile-apps`.

## Testing

`node --test tests/emws-api/` with no network access. A local `http.createServer` fake plays APIM and the API,
and configuration lives in a temporary directory selected with `EMWS_CONFIG_DIR` (a test seam, not a
documented feature).

Covered:

- Envelope, source detection, hint, and exit code for an API error body, an APIM 401, an HTML 502, a refused
  connection, and a timeout.
- Write protection, including the per-environment default.
- Secret masking in stdout, stderr, and log lines.
- Refusal of a group- or world-readable `.env`.
- Basic login, token caching, expiry, and the single re-login on 401.
- Configuration validation messages.
- Log filters and retention.
- Nearest-route matching and `describe` against a spec fixture.

After the automated tests, one manual smoke run against staging: `whoami`, `routes`, a GET, and a deliberate
404. Its result is reported separately from the automated tests.

## Out of scope for v1

- `emws trace <callId>`: fetching server-side exceptions and dependencies from Log Analytics. The log already
  stores the call id, traceparent, and correlation id it would need.
- Typed per-endpoint commands generated from OpenAPI.
- Pagination helpers.
- A Keychain secret backend.
