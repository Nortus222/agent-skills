# EMWS API CLI Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `emws-api` skill whose zero-dependency TypeScript CLI lets agents call the EMWS API on staging and production through APIM with locally configured profiles, structured errors, and a redacted call log.

**Architecture:** One entry script (`scripts/emws.ts`) calls `main(argv, io)` in `lib/cli.ts`, which composes small single-purpose modules: configuration, authentication, HTTP, error envelope, redaction, logging, and OpenAPI discovery. `main` takes all I/O (stdout, stderr, env, cwd, stdin, fetch, clock) as an argument, so tests run it in-process against a local `http` fake server and a temporary config directory.

**Tech Stack:** Node 24 with native TypeScript type stripping, Node built-ins only (`fetch`, `node:util` `parseArgs`, `node:test`, `node:fs`, `node:crypto`, `node:http`), Agent Skills Markdown and YAML.

**Spec:** `docs/superpowers/specs/2026-10-06-emws-api-cli-design.md`

## Global Constraints

- Work only in the `agent-skills` worktree `.claude/worktrees/emws-api-cli` on branch `feat/emws-api-cli`. PR target: `main`.
- No runtime or dev dependencies, no `node_modules`, no build step. `npx skills add` only copies files.
- Node 24 type stripping rules: no `enum`, no `namespace`, no constructor parameter properties, every type-only import uses `import type`, every relative import ends in `.ts`.
- Do not reference `NodeJS.*` or other `@types/node` names in types; use plain structural types (for example `Record<string, string | undefined>` for env).
- Configuration directory: `~/.config/emws/`, overridable only through `EMWS_CONFIG_DIR` (a test seam, not documented for users).
- `.env` must be mode `600`; anything readable by group or others fails with `ENV_PERMS`.
- `allowWrites` defaults to `false` for an environment named `prod` and `true` otherwise.
- Exit codes: 0 success, 2 usage/config, 3 auth, 4 blocked write, 5 client error, 6 server error (5xx, 422), 7 network/timeout. Exit 1 only for an internal CLI bug.
- Retries: `GET` only, on network errors and 502/503/504, at most 2 retries. Never retry writes, 4xx, or timeouts.
- Limits: request body preview 2 KB, raw unknown-source error body 4 KB, logged response body 64 KB, log retention 14 days, spec cache 10 minutes, default timeout 30 s, token refresh 60 s before `exp`.
- Never print or log a secret: `.env` values, cached tokens, `authorization`/`x-api-key` headers, and any `password` field are masked as `***`.
- The public repository must contain no real credentials, usernames, client ids, or database ids.
- Test command: `node --test 'tests/emws-api/*.test.ts'` from the worktree root.

**Deviation from the spec, approved in planning:** `whoami` on an API-key profile calls `POST /authenticateWithApiKey` instead of an arbitrary GET. It is the one authenticated call that both proves the key and returns the tenant (`database` claim), and it is a token exchange, not a data write, so write protection does not apply to it.

## Review Focus

1. An agent passes a full URL (`emws get https://…/projects/1`) or keeps the `/api` prefix (`/api/projects/1`). Expected: a usage error (exit 2) that says paths are relative to the environment, before any network call. Tested in Task 8.
2. A success response that is binary (the pallet-label PDF endpoint). Expected: stdout gets a one-line placeholder naming content type and size, never raw bytes. Tested in Task 4 (HTTP) and Task 8 (CLI).
3. A `204 No Content` or empty-body success, typical for `DELETE`. Expected: exit 0, empty stdout, summary on stderr, no parse error. Tested in Task 8.
4. A very large success response. Expected: stdout gets the whole body; the log entry is truncated to 64 KB and marked `responseTruncated: true`. Tested in Task 8.
5. A malformed or partially written log line (two agents appending at once, a crash mid-write). Expected: `emws log` skips the bad line and still lists the rest. Tested in Task 6.

---

### Task 1: Package skeleton and redaction

**Files:**
- Create: `skills/emws-api/package.json`
- Create: `tests/emws-api/package.json`
- Create: `skills/emws-api/scripts/lib/redact.ts`
- Test: `tests/emws-api/redact.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `export const MASK = '***'`
  - `export type Redactor = { add(secret: string | undefined): void; text(s: string): string; value<T>(v: T): T }`
  - `export function createRedactor(secrets?: Iterable<string>): Redactor`

- [ ] **Step 1: Create the two package.json files**

`skills/emws-api/package.json`:

```json
{ "name": "emws-api-skill", "private": true, "type": "module" }
```

`tests/emws-api/package.json`:

```json
{ "private": true, "type": "module" }
```

- [ ] **Step 2: Write the failing test**

`tests/emws-api/redact.test.ts`:

```ts
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRedactor, MASK } from '../../skills/emws-api/scripts/lib/redact.ts';

test('masks secret values inside strings and nested objects', () => {
  const r = createRedactor(['stg-secret-key-123']);
  assert.equal(r.text('key=stg-secret-key-123;'), `key=${MASK};`);
  assert.deepEqual(r.value({ a: ['x stg-secret-key-123 y'], b: { c: 'stg-secret-key-123' }, n: 5 }), {
    a: [`x ${MASK} y`],
    b: { c: MASK },
    n: 5,
  });
});

test('masks credential keys regardless of case', () => {
  const r = createRedactor();
  assert.deepEqual(r.value({ Password: 'p', authorization: 'Bearer t', 'X-Api-Key': 'k', name: 'n' }), {
    Password: MASK,
    authorization: MASK,
    'X-Api-Key': MASK,
    name: 'n',
  });
});

test('ignores secrets shorter than four characters', () => {
  const r = createRedactor(['a1']);
  assert.equal(r.text('a1 stays'), 'a1 stays');
});

test('add() masks a value learned later, such as a fresh token', () => {
  const r = createRedactor();
  r.add('eyJhbGciOi.token.sig');
  assert.equal(r.text('Bearer eyJhbGciOi.token.sig'), `Bearer ${MASK}`);
});

test('masks the longest secret first so no fragment survives', () => {
  const r = createRedactor(['abcd', 'abcdef']);
  assert.equal(r.text('abcdef'), MASK);
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/redact.test.ts'`
Expected: FAIL with `Cannot find module …/redact.ts`.

- [ ] **Step 4: Implement**

`skills/emws-api/scripts/lib/redact.ts`:

```ts
/** Masks secret values, credential headers, and password fields before anything is printed or logged. */
export type Redactor = {
  add(secret: string | undefined): void;
  text(s: string): string;
  value<T>(v: T): T;
};

export const MASK = '***';
const SECRET_KEYS = new Set(['password', 'authorization', 'x-api-key']);
const MIN_SECRET_LENGTH = 4;

export function createRedactor(secrets: Iterable<string> = []): Redactor {
  const values = new Set<string>();
  const add = (s: string | undefined): void => {
    if (s && s.length >= MIN_SECRET_LENGTH) values.add(s);
  };
  for (const s of secrets) add(s);

  const text = (s: string): string => {
    let out = s;
    for (const v of [...values].sort((a, b) => b.length - a.length)) out = out.split(v).join(MASK);
    return out;
  };

  const value = <T>(v: T): T => {
    if (typeof v === 'string') return text(v) as T;
    if (Array.isArray(v)) return v.map((x) => value(x)) as T;
    if (v !== null && typeof v === 'object') {
      const out: Record<string, unknown> = {};
      for (const [k, x] of Object.entries(v)) out[k] = SECRET_KEYS.has(k.toLowerCase()) ? MASK : value(x);
      return out as T;
    }
    return v;
  };

  return { add, text, value };
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `node --test 'tests/emws-api/redact.test.ts'`
Expected: PASS, 5 tests.

- [ ] **Step 6: Commit**

```bash
git add skills/emws-api/package.json tests/emws-api/package.json skills/emws-api/scripts/lib/redact.ts tests/emws-api/redact.test.ts
git commit -m "feat(emws-api): add secret redaction"
```

---

### Task 2: Error envelope, classification, hints, exit codes

**Files:**
- Create: `skills/emws-api/scripts/lib/errors.ts`
- Test: `tests/emws-api/errors.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `export type ErrorKind = 'config' | 'auth' | 'blocked' | 'network' | 'timeout' | 'client' | 'server'`
  - `export type ResponseSource = 'api' | 'apim' | 'unknown'`
  - `export type RequestInfo = { method: string; url: string; profile: string; env: string; headers: Record<string, string>; bodyPreview: string | null }`
  - `export type ResponseInfo = { status: number; headers: Record<string, string>; body: unknown; source: ResponseSource }`
  - `export type Correlation = { callId: string; traceparent: string; correlationId?: string; kql?: string }`
  - `export type ErrorEnvelope = { ok: false; error: { kind: ErrorKind; code: string; message: string; hint: string }; request: RequestInfo | null; response: ResponseInfo | null; correlation: Correlation | null; timingMs: number }`
  - `export type SuccessEnvelope = { ok: true; status: number; headers: Record<string, string>; body: unknown; timingMs: number; callId: string; profile: string; env: string }`
  - `export class CliError extends Error` with fields `kind: ErrorKind`, `code: string`, `hint?: string`, `response?: ResponseInfo`; constructor `(kind, code, message, hint?)`
  - `export function exitCodeFor(kind: ErrorKind): number`
  - `export function classifyStatus(status: number): { kind: ErrorKind; code: string }`
  - `export function detectSource(body: unknown): ResponseSource`
  - `export function messageOf(body: unknown, status: number): string`
  - `export type HintInput = { kind: ErrorKind; code: string; status?: number; source?: ResponseSource; path?: string; routeKnown?: boolean; suggestions?: string[]; profile?: string }`
  - `export function hintFor(i: HintInput): string`
  - `export function buildKql(c: { traceId?: string; correlationId?: string }): string | undefined`
  - `export function renderError(env: ErrorEnvelope): string`

- [ ] **Step 1: Write the failing test**

`tests/emws-api/errors.test.ts`:

```ts
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  buildKql,
  classifyStatus,
  CliError,
  detectSource,
  exitCodeFor,
  hintFor,
  messageOf,
  renderError,
} from '../../skills/emws-api/scripts/lib/errors.ts';
import type { ErrorEnvelope } from '../../skills/emws-api/scripts/lib/errors.ts';

test('exit codes follow the documented table', () => {
  assert.deepEqual(
    (['config', 'auth', 'blocked', 'client', 'server', 'network', 'timeout'] as const).map(exitCodeFor),
    [2, 3, 4, 5, 6, 7, 7],
  );
});

test('classifies HTTP status into kind and code', () => {
  assert.deepEqual(classifyStatus(401), { kind: 'auth', code: 'HTTP_401' });
  assert.deepEqual(classifyStatus(404), { kind: 'client', code: 'HTTP_404' });
  assert.deepEqual(classifyStatus(422), { kind: 'server', code: 'HTTP_422' });
  assert.deepEqual(classifyStatus(503), { kind: 'server', code: 'HTTP_503' });
});

test('detects whether the API or APIM produced an error body', () => {
  assert.equal(detectSource({ StatusCode: 500, Message: 'x', ErrorCode: null }), 'api');
  assert.equal(detectSource({ statusCode: 400, message: 'x', errorCode: 12 }), 'api');
  assert.equal(detectSource({ statusCode: 401, message: 'Invalid JWT.' }), 'apim');
  assert.equal(detectSource('<html>Bad Gateway</html>'), 'unknown');
  assert.equal(detectSource({ title: 'problem' }), 'unknown');
  assert.equal(detectSource(null), 'unknown');
});

test('extracts a message from any casing, text, or HTML', () => {
  assert.equal(messageOf({ Message: 'boom' }, 500), 'boom');
  assert.equal(messageOf({ message: 'nope' }, 401), 'nope');
  assert.equal(messageOf('<html><body><h1>502 Bad Gateway</h1></body></html>', 502), '502 Bad Gateway');
  assert.equal(messageOf(null, 503), 'HTTP 503');
});

test('hints name the next action for each failure', () => {
  assert.match(hintFor({ kind: 'auth', code: 'HTTP_401', status: 401, source: 'apim' }), /emws whoami/);
  assert.match(hintFor({ kind: 'auth', code: 'HTTP_401', status: 401, source: 'api' }), /secret/);
  assert.match(hintFor({ kind: 'client', code: 'HTTP_403', status: 403, path: '/management/keys' }), /admin-scoped API-key/);
  assert.match(
    hintFor({ kind: 'client', code: 'HTTP_404', status: 404, routeKnown: false, suggestions: ['GET /projects/{id}'] }),
    /GET \/projects\/\{id\}/,
  );
  assert.match(hintFor({ kind: 'client', code: 'HTTP_404', status: 404, routeKnown: true }), /record/);
  assert.match(hintFor({ kind: 'server', code: 'HTTP_422', status: 422 }), /Unhandled server exception/);
  assert.match(hintFor({ kind: 'network', code: 'CONN_REFUSED' }), /apiBase/);
  assert.match(hintFor({ kind: 'blocked', code: 'WRITE_BLOCKED', profile: 'prod' }), /"prod".*allowWrites/);
});

test('builds a KQL query from whichever ids exist', () => {
  assert.equal(buildKql({}), undefined);
  const kql = buildKql({ traceId: 'abc', correlationId: 'inv-1' }) ?? '';
  assert.match(kql, /operation_Id == 'abc'/);
  assert.match(kql, /InvocationId\) == 'inv-1'/);
});

test('CliError carries kind, code, and hint', () => {
  const e = new CliError('config', 'ENV_PERMS', 'bad mode', 'chmod 600');
  assert.equal(e.kind, 'config');
  assert.equal(e.code, 'ENV_PERMS');
  assert.equal(e.hint, 'chmod 600');
  assert.ok(e instanceof Error);
});

test('renders a readable error with the next steps', () => {
  const env: ErrorEnvelope = {
    ok: false,
    error: { kind: 'server', code: 'HTTP_500', message: 'boom', hint: 'look it up' },
    request: { method: 'GET', url: 'https://h/x', profile: 'stg', env: 'staging', headers: {}, bodyPreview: null },
    response: { status: 500, headers: {}, body: { Message: 'boom' }, source: 'api' },
    correlation: { callId: 'c_1', traceparent: '00-a-b-01', correlationId: 'inv-1', kql: 'KQL' },
    timingMs: 12,
  };
  const text = renderError(env);
  for (const part of ['error server HTTP_500', 'GET https://h/x', 'boom', 'hint: look it up', 'inv-1', 'kql: KQL', 'emws log c_1']) {
    assert.ok(text.includes(part), `missing "${part}" in:\n${text}`);
  }
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/errors.test.ts'`
Expected: FAIL with `Cannot find module …/errors.ts`.

- [ ] **Step 3: Implement**

`skills/emws-api/scripts/lib/errors.ts`:

```ts
/** The single error shape every failure produces, plus classification, hints, and exit codes. */
export type ErrorKind = 'config' | 'auth' | 'blocked' | 'network' | 'timeout' | 'client' | 'server';
export type ResponseSource = 'api' | 'apim' | 'unknown';

export type RequestInfo = {
  method: string;
  url: string;
  profile: string;
  env: string;
  headers: Record<string, string>;
  bodyPreview: string | null;
};
export type ResponseInfo = { status: number; headers: Record<string, string>; body: unknown; source: ResponseSource };
export type Correlation = { callId: string; traceparent: string; correlationId?: string; kql?: string };

export type ErrorEnvelope = {
  ok: false;
  error: { kind: ErrorKind; code: string; message: string; hint: string };
  request: RequestInfo | null;
  response: ResponseInfo | null;
  correlation: Correlation | null;
  timingMs: number;
};

export type SuccessEnvelope = {
  ok: true;
  status: number;
  headers: Record<string, string>;
  body: unknown;
  timingMs: number;
  callId: string;
  profile: string;
  env: string;
};

/** A failure the CLI understands and reports through the error envelope. */
export class CliError extends Error {
  kind: ErrorKind;
  code: string;
  hint?: string;
  response?: ResponseInfo;

  constructor(kind: ErrorKind, code: string, message: string, hint?: string) {
    super(message);
    this.kind = kind;
    this.code = code;
    this.hint = hint;
  }
}

const EXIT_CODES: Record<ErrorKind, number> = {
  config: 2,
  auth: 3,
  blocked: 4,
  client: 5,
  server: 6,
  network: 7,
  timeout: 7,
};

export function exitCodeFor(kind: ErrorKind): number {
  return EXIT_CODES[kind];
}

/** EMWS returns 422 for unhandled exceptions, so it counts as a server error. */
export function classifyStatus(status: number): { kind: ErrorKind; code: string } {
  const code = `HTTP_${status}`;
  if (status === 401) return { kind: 'auth', code };
  if (status === 422 || status >= 500) return { kind: 'server', code };
  return { kind: 'client', code };
}

/** `api` for the EMWS `{ StatusCode, Message, ErrorCode }` body, `apim` for the gateway's own `{ statusCode, message }`. */
export function detectSource(body: unknown): ResponseSource {
  if (body === null || typeof body !== 'object' || Array.isArray(body)) return 'unknown';
  const keys = Object.keys(body);
  const lower = new Set(keys.map((k) => k.toLowerCase()));
  if (keys.includes('StatusCode') || lower.has('errorcode')) return 'api';
  if (lower.has('statuscode') && lower.has('message')) return 'apim';
  return 'unknown';
}

export function messageOf(body: unknown, status: number): string {
  if (body !== null && typeof body === 'object' && !Array.isArray(body)) {
    for (const [k, v] of Object.entries(body)) {
      if (k.toLowerCase() === 'message' && typeof v === 'string' && v) return v;
    }
  }
  if (typeof body === 'string') {
    const text = body.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    if (text) return text.slice(0, 300);
  }
  return `HTTP ${status}`;
}

export type HintInput = {
  kind: ErrorKind;
  code: string;
  status?: number;
  source?: ResponseSource;
  path?: string;
  routeKnown?: boolean;
  suggestions?: string[];
  profile?: string;
};

const SERVER_HINT = 'Look up the correlation id in App Insights with the query in correlation.kql.';

export function hintFor(i: HintInput): string {
  if (i.code === 'WRITE_BLOCKED') {
    return `Profile "${i.profile}" is read-only. Only the user can allow writes, by setting "allowWrites": true on it in ~/.config/emws/profiles.json.`;
  }
  if (i.code === 'DNS' || i.code === 'CONN_REFUSED') {
    return "Host unreachable: check the environment's apiBase, or start the local Functions host for a local environment.";
  }
  if (i.code === 'TLS') return "TLS handshake failed: check the environment's apiBase scheme and host.";
  if (i.code === 'TIMEOUT') return 'No response before the timeout; retry with --timeout 60s, or check the host.';
  if (i.kind === 'network') return 'The connection failed; retry, and check the host if it repeats.';
  if (i.status === 401 && i.source === 'apim') {
    return 'APIM rejected the credentials (expired token or wrong issuer). Run `emws whoami` for this profile.';
  }
  if (i.status === 401) return "The API rejected the credentials; check the profile's secret in ~/.config/emws/.env and run `emws whoami`.";
  if (i.status === 403 && i.path?.startsWith('/management/')) return 'Management routes need an admin-scoped API-key profile.';
  if (i.status === 403) return 'Authenticated, but this identity may not access the resource; try a profile with broader access.';
  if (i.status === 404 && i.routeKnown === false) {
    const closest = i.suggestions?.length ? ` Closest routes: ${i.suggestions.join(', ')}.` : '';
    return `No route matches this path.${closest} Run \`emws routes <filter>\` to search.`;
  }
  if (i.status === 404 && i.routeKnown) return 'The route exists, so the record was not found or is outside this identity\'s access scope.';
  if (i.status === 404) return 'Not found; the route list was unavailable, so run `emws routes <filter>` to check the path.';
  if (i.status === 422) return `Unhandled server exception. ${SERVER_HINT}`;
  if (i.kind === 'server') return `Server-side error. ${SERVER_HINT}`;
  if (i.status === 400) return 'The API rejected the request; read error.message and compare the body with `emws describe`.';
  if (i.kind === 'config') return 'Fix ~/.config/emws/profiles.json or .env; the layout is in references/profiles.example.json.';
  if (i.kind === 'auth') return 'Authentication failed; run `emws whoami` for this profile.';
  return 'Read error.message and the response body; `emws log <callId>` shows the full call.';
}

/** A ready-to-run App Insights query; the trace id is the traceparent's, the correlation id is the Functions InvocationId. */
export function buildKql(c: { traceId?: string; correlationId?: string }): string | undefined {
  const clauses: string[] = [];
  if (c.traceId) clauses.push(`operation_Id == '${c.traceId}'`);
  if (c.correlationId) clauses.push(`tostring(customDimensions.InvocationId) == '${c.correlationId}'`);
  if (clauses.length === 0) return undefined;
  return `union requests, exceptions, traces, dependencies | where timestamp > ago(1d) | where ${clauses.join(' or ')} | order by timestamp asc`;
}

export function renderError(env: ErrorEnvelope): string {
  const e = env.error;
  const where = env.request ? ` · ${env.request.method} ${env.request.url} · ${env.request.profile}` : '';
  const lines = [`error ${e.kind} ${e.code}${where} · ${env.timingMs}ms`, `  ${e.message}`, `  hint: ${e.hint}`];
  if (env.response) {
    const body = typeof env.response.body === 'string' ? env.response.body : JSON.stringify(env.response.body);
    lines.push(`  response ${env.response.status} from ${env.response.source}: ${body}`);
  }
  if (env.correlation?.correlationId) lines.push(`  correlation id: ${env.correlation.correlationId}`);
  if (env.correlation?.kql) lines.push(`  kql: ${env.correlation.kql}`);
  if (env.correlation?.callId) lines.push(`  full call: emws log ${env.correlation.callId}`);
  return `${lines.join('\n')}\n`;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test 'tests/emws-api/errors.test.ts'`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/emws-api/scripts/lib/errors.ts tests/emws-api/errors.test.ts
git commit -m "feat(emws-api): add error envelope, hints, and exit codes"
```

---

### Task 3: Configuration and test helpers

**Files:**
- Create: `skills/emws-api/scripts/lib/config.ts`
- Create: `tests/emws-api/helpers.ts`
- Test: `tests/emws-api/config.test.ts`

**Interfaces:**
- Consumes: `CliError` from Task 2.
- Produces (config.ts):
  - `export type Environment = { apiBase: string; specUrl: string }`
  - `export type Profile = { env: string; auth: 'apiKey' | 'basic'; secret: string; username?: string; clientId?: string | number; dbId?: string | number; allowWrites?: boolean }`
  - `export type Config = { environments: Record<string, Environment>; profiles: Record<string, Profile>; defaultProfile?: string }`
  - `export type Context = { dir: string; config: Config; profileName: string; profile: Profile; envName: string; environment: Environment; secret: string; secrets: Record<string, string>; allowWrites: boolean }`
  - `export function configDir(env?: Record<string, string | undefined>): string`
  - `export function validateConfig(raw: unknown): Config`
  - `export function parseEnvFile(text: string): Record<string, string>`
  - `export async function loadConfig(dir: string): Promise<Config>`
  - `export async function loadSecrets(dir: string): Promise<Record<string, string>>`
  - `export function resolveProfileName(config: Config, flag: string | undefined, env: Record<string, string | undefined>): string`
  - `export function effectiveAllowWrites(profile: Profile): boolean`
  - `export async function loadContext(opts: { dir: string; profile?: string; env: Record<string, string | undefined> }): Promise<Context>`
- Produces (helpers.ts, used by every later test): `SECRETS`, `testConfig(base)`, `makeConfigDir(config, secrets?, envMode?)`, `startServer(handler)`, `json(res, status, body, headers?)`, `makeJwt(claims)`, `Recorded`, `Handler`. `runCli` is added in Task 8.

- [ ] **Step 1: Write the test helpers**

`tests/emws-api/helpers.ts`:

```ts
import { chmod, mkdtemp, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import type { ServerResponse } from 'node:http';
import os from 'node:os';
import path from 'node:path';

export type Recorded = { method: string; url: string; headers: Record<string, string | string[] | undefined>; body: string };
export type Handler = (req: Recorded, res: ServerResponse) => void;

export const SECRETS = { STG_KEY: 'stg-secret-key-123', PROD_KEY: 'prod-secret-key-456', USER_PW: 'hunter2-password' };

/** A config whose environments point at the fake server: staging at /emws-staging, prod at /emws. */
export function testConfig(base: string) {
  return {
    environments: {
      staging: { apiBase: `${base}/emws-staging`, specUrl: `${base}/spec.json` },
      prod: { apiBase: `${base}/emws`, specUrl: `${base}/spec.json` },
    },
    profiles: {
      stg: { env: 'staging', auth: 'apiKey', secret: 'STG_KEY' },
      prod: { env: 'prod', auth: 'apiKey', secret: 'PROD_KEY' },
      'prod-rw': { env: 'prod', auth: 'apiKey', secret: 'PROD_KEY', allowWrites: true },
      user: { env: 'staging', auth: 'basic', username: 'agent@example.com', secret: 'USER_PW', clientId: 'pocketmanage', dbId: 7 },
    },
    defaultProfile: 'stg',
  };
}

export async function makeConfigDir(config: unknown, secrets: Record<string, string> = SECRETS, envMode = 0o600): Promise<string> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'emws-test-'));
  await writeFile(path.join(dir, 'profiles.json'), JSON.stringify(config));
  const envFile = path.join(dir, '.env');
  await writeFile(envFile, `${Object.entries(secrets).map(([k, v]) => `${k}=${v}`).join('\n')}\n`);
  await chmod(envFile, envMode);
  return dir;
}

export async function startServer(handler: Handler) {
  const requests: Recorded[] = [];
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const rec: Recorded = { method: req.method ?? '', url: req.url ?? '', headers: req.headers, body: Buffer.concat(chunks).toString('utf8') };
    requests.push(rec);
    handler(rec, res);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as { port: number };
  return {
    url: `http://127.0.0.1:${port}`,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}

export function json(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { 'content-type': 'application/json', ...headers });
  res.end(JSON.stringify(body));
}

export function makeJwt(claims: Record<string, unknown>): string {
  const enc = (o: object) => Buffer.from(JSON.stringify(o)).toString('base64url');
  return `${enc({ alg: 'none', typ: 'JWT' })}.${enc(claims)}.sig`;
}
```

- [ ] **Step 2: Write the failing test**

`tests/emws-api/config.test.ts`:

```ts
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  configDir,
  effectiveAllowWrites,
  loadConfig,
  loadContext,
  parseEnvFile,
  validateConfig,
} from '../../skills/emws-api/scripts/lib/config.ts';
import { makeConfigDir, SECRETS, testConfig } from './helpers.ts';

const base = 'https://example.test';

test('configDir defaults to ~/.config/emws and honours EMWS_CONFIG_DIR', () => {
  assert.match(configDir({}), /\.config[\\/]emws$/);
  assert.equal(configDir({ EMWS_CONFIG_DIR: '/tmp/x' }), '/tmp/x');
});

test('selects the profile by flag, then EMWS_PROFILE, then defaultProfile', async () => {
  const dir = await makeConfigDir(testConfig(base));
  assert.equal((await loadContext({ dir, env: {} })).profileName, 'stg');
  assert.equal((await loadContext({ dir, env: { EMWS_PROFILE: 'prod' } })).profileName, 'prod');
  assert.equal((await loadContext({ dir, profile: 'user', env: { EMWS_PROFILE: 'prod' } })).profileName, 'user');
});

test('the context carries the environment and the resolved secret', async () => {
  const dir = await makeConfigDir(testConfig(base));
  const ctx = await loadContext({ dir, profile: 'stg', env: {} });
  assert.equal(ctx.envName, 'staging');
  assert.equal(ctx.environment.apiBase, `${base}/emws-staging`);
  assert.equal(ctx.secret, SECRETS.STG_KEY);
});

test('reports the exact field that is invalid', () => {
  const cfg = testConfig(base);
  delete (cfg.profiles.user as { dbId?: number }).dbId;
  assert.throws(() => validateConfig(cfg), { code: 'CONFIG_INVALID', message: /profiles\.user\.dbId is required for auth "basic"/ });
  const bad = testConfig(base);
  (bad.profiles.stg as { env: string }).env = 'qa';
  assert.throws(() => validateConfig(bad), { message: /profiles\.stg\.env must name one of: staging, prod/ });
});

test('rejects an unknown profile and lists the known ones', async () => {
  const dir = await makeConfigDir(testConfig(base));
  await assert.rejects(loadContext({ dir, profile: 'nope', env: {} }), { code: 'CONFIG_INVALID', hint: /stg, prod, prod-rw, user/ });
});

test('refuses a .env readable by others', async () => {
  const dir = await makeConfigDir(testConfig(base), SECRETS, 0o644);
  await assert.rejects(loadContext({ dir, env: {} }), { kind: 'config', code: 'ENV_PERMS', message: /mode 644/, hint: /chmod 600/ });
});

test('reports a secret missing from .env', async () => {
  const dir = await makeConfigDir(testConfig(base), { STG_KEY: SECRETS.STG_KEY });
  await assert.rejects(loadContext({ dir, profile: 'prod', env: {} }), { code: 'SECRET_MISSING', message: /PROD_KEY/ });
});

test('reports a missing or unparseable profiles.json', async () => {
  await assert.rejects(loadConfig('/nonexistent-emws-dir'), { code: 'CONFIG_INVALID', message: /not found/ });
});

test('writes default to off for prod and on elsewhere', () => {
  assert.equal(effectiveAllowWrites({ env: 'prod', auth: 'apiKey', secret: 'X' }), false);
  assert.equal(effectiveAllowWrites({ env: 'staging', auth: 'apiKey', secret: 'X' }), true);
  assert.equal(effectiveAllowWrites({ env: 'prod', auth: 'apiKey', secret: 'X', allowWrites: true }), true);
  assert.equal(effectiveAllowWrites({ env: 'staging', auth: 'apiKey', secret: 'X', allowWrites: false }), false);
});

test('parses .env lines with comments, export, and quotes', () => {
  assert.deepEqual(parseEnvFile('# c\nA=1\nexport B="two words"\nC=\'x=y\'\n\nbad line\n'), { A: '1', B: 'two words', C: 'x=y' });
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/config.test.ts'`
Expected: FAIL with `Cannot find module …/config.ts`.

- [ ] **Step 4: Implement**

`skills/emws-api/scripts/lib/config.ts`:

```ts
/** Loads ~/.config/emws: validated profiles.json, the mode-600 .env, and the selected profile's context. */
import { readFile, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { CliError } from './errors.ts';

export type Environment = { apiBase: string; specUrl: string };
export type Profile = {
  env: string;
  auth: 'apiKey' | 'basic';
  secret: string;
  username?: string;
  clientId?: string | number;
  dbId?: string | number;
  allowWrites?: boolean;
};
export type Config = { environments: Record<string, Environment>; profiles: Record<string, Profile>; defaultProfile?: string };
export type Context = {
  dir: string;
  config: Config;
  profileName: string;
  profile: Profile;
  envName: string;
  environment: Environment;
  secret: string;
  secrets: Record<string, string>;
  allowWrites: boolean;
};

type Obj = Record<string, unknown>;
const SETUP_HINT = 'The expected layout is in references/profiles.example.json in the emws-api skill.';

export function configDir(env: Record<string, string | undefined> = process.env): string {
  return env.EMWS_CONFIG_DIR || path.join(os.homedir(), '.config', 'emws');
}

const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const invalid = (where: string, problem: string): CliError =>
  new CliError('config', 'CONFIG_INVALID', `profiles.json: ${where} ${problem}`, SETUP_HINT);

export function validateConfig(raw: unknown): Config {
  if (!isObj(raw)) throw invalid('root', 'must be a JSON object');
  const { environments, profiles, defaultProfile } = raw;
  if (!isObj(environments) || Object.keys(environments).length === 0) throw invalid('environments', 'must be a non-empty object');
  for (const [name, e] of Object.entries(environments)) {
    if (!isObj(e)) throw invalid(`environments.${name}`, 'must be an object');
    for (const key of ['apiBase', 'specUrl']) {
      const v = e[key];
      if (typeof v !== 'string' || !/^https?:\/\//.test(v)) throw invalid(`environments.${name}.${key}`, 'must be an http(s) URL');
    }
  }
  if (!isObj(profiles) || Object.keys(profiles).length === 0) throw invalid('profiles', 'must be a non-empty object');
  for (const [name, p] of Object.entries(profiles)) {
    const at = `profiles.${name}`;
    if (!isObj(p)) throw invalid(at, 'must be an object');
    if (typeof p.env !== 'string' || !(p.env in environments)) {
      throw invalid(`${at}.env`, `must name one of: ${Object.keys(environments).join(', ')}`);
    }
    if (p.auth !== 'apiKey' && p.auth !== 'basic') throw invalid(`${at}.auth`, 'must be "apiKey" or "basic"');
    if (typeof p.secret !== 'string' || !p.secret) throw invalid(`${at}.secret`, 'must name a variable in .env');
    if (p.allowWrites !== undefined && typeof p.allowWrites !== 'boolean') throw invalid(`${at}.allowWrites`, 'must be true or false');
    if (p.auth === 'basic') {
      if (typeof p.username !== 'string' || !p.username) throw invalid(`${at}.username`, 'is required for auth "basic"');
      for (const key of ['clientId', 'dbId']) {
        const v = p[key];
        if (typeof v !== 'string' && typeof v !== 'number') throw invalid(`${at}.${key}`, 'is required for auth "basic"');
      }
    }
  }
  if (defaultProfile !== undefined && (typeof defaultProfile !== 'string' || !(defaultProfile in profiles))) {
    throw invalid('defaultProfile', 'must name a profile');
  }
  return raw as Config;
}

export function parseEnvFile(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m) continue;
    let v = m[2];
    if (v.length >= 2 && (v[0] === '"' || v[0] === "'") && v.at(-1) === v[0]) v = v.slice(1, -1);
    out[m[1]] = v;
  }
  return out;
}

export async function loadConfig(dir: string): Promise<Config> {
  const file = path.join(dir, 'profiles.json');
  let text: string;
  try {
    text = await readFile(file, 'utf8');
  } catch {
    throw new CliError('config', 'CONFIG_INVALID', `${file} not found`, SETUP_HINT);
  }
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new CliError('config', 'CONFIG_INVALID', `${file} is not valid JSON: ${(err as Error).message}`, SETUP_HINT);
  }
  return validateConfig(raw);
}

/** Reads .env, refusing it when group or others can read it. A missing file yields no secrets. */
export async function loadSecrets(dir: string): Promise<Record<string, string>> {
  const file = path.join(dir, '.env');
  let mode: number;
  try {
    mode = (await stat(file)).mode;
  } catch {
    return {};
  }
  if (mode & 0o077) {
    throw new CliError('config', 'ENV_PERMS', `${file} is readable by other users (mode ${(mode & 0o777).toString(8)})`, `Run: chmod 600 ${file}`);
  }
  return parseEnvFile(await readFile(file, 'utf8'));
}

export function resolveProfileName(config: Config, flag: string | undefined, env: Record<string, string | undefined>): string {
  const name = flag || env.EMWS_PROFILE || config.defaultProfile;
  if (!name) {
    throw new CliError('config', 'USAGE', 'no profile selected', 'Pass -p <profile>, set EMWS_PROFILE, or set defaultProfile in profiles.json.');
  }
  if (!(name in config.profiles)) {
    throw new CliError('config', 'CONFIG_INVALID', `unknown profile "${name}"`, `Known profiles: ${Object.keys(config.profiles).join(', ')}`);
  }
  return name;
}

export function effectiveAllowWrites(profile: Profile): boolean {
  return profile.allowWrites ?? profile.env !== 'prod';
}

export async function loadContext(opts: { dir: string; profile?: string; env: Record<string, string | undefined> }): Promise<Context> {
  const config = await loadConfig(opts.dir);
  const profileName = resolveProfileName(config, opts.profile, opts.env);
  const profile = config.profiles[profileName];
  const secrets = await loadSecrets(opts.dir);
  const secret = secrets[profile.secret];
  if (!secret) {
    throw new CliError(
      'config',
      'SECRET_MISSING',
      `${profile.secret} is not set in ${path.join(opts.dir, '.env')}`,
      `Ask the user to add ${profile.secret}=<value> to that file (mode 600).`,
    );
  }
  return {
    dir: opts.dir,
    config,
    profileName,
    profile,
    envName: profile.env,
    environment: config.environments[profile.env],
    secret,
    secrets,
    allowWrites: effectiveAllowWrites(profile),
  };
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `node --test 'tests/emws-api/config.test.ts'`
Expected: PASS, 10 tests.

- [ ] **Step 6: Commit**

```bash
git add skills/emws-api/scripts/lib/config.ts tests/emws-api/helpers.ts tests/emws-api/config.test.ts
git commit -m "feat(emws-api): load and validate profiles and secrets"
```

---

### Task 4: HTTP transport

**Files:**
- Create: `skills/emws-api/scripts/lib/http.ts`
- Test: `tests/emws-api/http.test.ts`

**Interfaces:**
- Consumes: `CliError` from Task 2; `startServer`, `json` from Task 3 helpers.
- Produces:
  - `export type RawResponse = { status: number; headers: Record<string, string>; bodyText: string; contentType: string; attempts: number }`
  - `export type SendInput = { method: string; url: string; headers: Record<string, string>; body?: string; timeoutMs: number; fetchImpl?: typeof fetch; sleep?: (ms: number) => Promise<void> }`
  - `export function isWrite(method: string): boolean`
  - `export function newCallId(): string` (`c_` + 8 hex)
  - `export function newTraceparent(): string` (W3C `00-<32 hex>-<16 hex>-01`)
  - `export function traceIdOf(traceparent: string): string`
  - `export function parseBody(text: string, contentType: string): unknown`
  - `export async function sendRequest(i: SendInput): Promise<RawResponse>`; throws `CliError` with kind `network` (codes `DNS`, `CONN_REFUSED`, `TLS`, `NETWORK`) or `timeout` (`TIMEOUT`).

- [ ] **Step 1: Write the failing test**

`tests/emws-api/http.test.ts`:

```ts
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { isWrite, newCallId, newTraceparent, parseBody, sendRequest, traceIdOf } from '../../skills/emws-api/scripts/lib/http.ts';
import { json, startServer } from './helpers.ts';

const noSleep = async () => {};

test('returns status, headers, and body text', async () => {
  const srv = await startServer((_req, res) => json(res, 200, { id: 1 }, { 'x-thing': 'y' }));
  try {
    const r = await sendRequest({ method: 'GET', url: `${srv.url}/a`, headers: { accept: 'application/json' }, timeoutMs: 2000 });
    assert.equal(r.status, 200);
    assert.equal(r.headers['x-thing'], 'y');
    assert.deepEqual(parseBody(r.bodyText, r.contentType), { id: 1 });
    assert.equal(r.attempts, 1);
  } finally {
    await srv.close();
  }
});

test('retries a GET on 503, then succeeds', async () => {
  let n = 0;
  const srv = await startServer((_req, res) => (++n < 2 ? json(res, 503, {}) : json(res, 200, { ok: true })));
  try {
    const r = await sendRequest({ method: 'GET', url: srv.url, headers: {}, timeoutMs: 2000, sleep: noSleep });
    assert.equal(r.status, 200);
    assert.equal(r.attempts, 2);
  } finally {
    await srv.close();
  }
});

test('never retries a write or a 4xx', async () => {
  const srv = await startServer((req, res) => json(res, req.method === 'POST' ? 503 : 400, {}));
  try {
    assert.equal((await sendRequest({ method: 'POST', url: srv.url, headers: {}, body: '{}', timeoutMs: 2000, sleep: noSleep })).attempts, 1);
    assert.equal((await sendRequest({ method: 'GET', url: srv.url, headers: {}, timeoutMs: 2000, sleep: noSleep })).attempts, 1);
    assert.equal(srv.requests.length, 2);
  } finally {
    await srv.close();
  }
});

test('a refused connection is a network error, retried for GET', async () => {
  const srv = await startServer(() => {});
  await srv.close();
  let calls = 0;
  const counting: typeof fetch = (input, init) => {
    calls++;
    return fetch(input, init);
  };
  await assert.rejects(sendRequest({ method: 'GET', url: srv.url, headers: {}, timeoutMs: 2000, sleep: noSleep, fetchImpl: counting }), {
    kind: 'network',
    code: 'CONN_REFUSED',
  });
  assert.equal(calls, 3);
});

test('a DNS failure is reported as DNS', async () => {
  const failing: typeof fetch = async () => {
    throw Object.assign(new TypeError('fetch failed'), { cause: { code: 'ENOTFOUND' } });
  };
  await assert.rejects(sendRequest({ method: 'POST', url: 'https://nohost.test/x', headers: {}, timeoutMs: 2000, fetchImpl: failing }), {
    kind: 'network',
    code: 'DNS',
    message: /ENOTFOUND reaching nohost\.test/,
  });
});

test('a slow server is a timeout and is not retried', async () => {
  const srv = await startServer(() => {});
  try {
    await assert.rejects(sendRequest({ method: 'GET', url: srv.url, headers: {}, timeoutMs: 150, sleep: noSleep }), { kind: 'timeout', code: 'TIMEOUT' });
    assert.equal(srv.requests.length, 1);
  } finally {
    await srv.close();
  }
});

test('a binary body becomes a placeholder, never raw bytes', async () => {
  const srv = await startServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'application/pdf' });
    res.end(Buffer.from([0x25, 0x50, 0x44, 0x46, 0x00, 0xff]));
  });
  try {
    const r = await sendRequest({ method: 'GET', url: srv.url, headers: {}, timeoutMs: 2000 });
    assert.equal(r.bodyText, '<application/pdf body, 6 bytes, not shown>');
  } finally {
    await srv.close();
  }
});

test('helpers: write methods, ids, traceparent, body parsing', () => {
  assert.deepEqual(['GET', 'post', 'PUT', 'patch', 'DELETE'].map(isWrite), [false, true, true, true, true]);
  assert.match(newCallId(), /^c_[0-9a-f]{8}$/);
  const tp = newTraceparent();
  assert.match(tp, /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  assert.equal(traceIdOf(tp), tp.split('-')[1]);
  assert.equal(parseBody('', 'application/json'), null);
  assert.equal(parseBody('plain', 'text/plain'), 'plain');
  assert.deepEqual(parseBody('[1]', ''), [1]);
  assert.equal(parseBody('{not json', 'application/json'), '{not json');
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/http.test.ts'`
Expected: FAIL with `Cannot find module …/http.ts`.

- [ ] **Step 3: Implement**

`skills/emws-api/scripts/lib/http.ts`:

```ts
/** Sends one HTTP request with a timeout, GET-only retries, and network failures mapped to CliError. */
import { randomBytes } from 'node:crypto';
import { CliError } from './errors.ts';

export type RawResponse = { status: number; headers: Record<string, string>; bodyText: string; contentType: string; attempts: number };
export type SendInput = {
  method: string;
  url: string;
  headers: Record<string, string>;
  body?: string;
  timeoutMs: number;
  fetchImpl?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
};

const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const RETRY_STATUSES = new Set([502, 503, 504]);
const MAX_ATTEMPTS = 3;

export function isWrite(method: string): boolean {
  return WRITE_METHODS.has(method.toUpperCase());
}

export function newCallId(): string {
  return `c_${randomBytes(4).toString('hex')}`;
}

export function newTraceparent(): string {
  return `00-${randomBytes(16).toString('hex')}-${randomBytes(8).toString('hex')}-01`;
}

export function traceIdOf(traceparent: string): string {
  return traceparent.split('-')[1] ?? '';
}

const isTextual = (contentType: string): boolean =>
  contentType === '' || /json|text|xml|javascript|x-www-form-urlencoded/i.test(contentType);

/** JSON when it parses, the text otherwise, null when empty. */
export function parseBody(text: string, contentType: string): unknown {
  if (text === '') return null;
  if (/json/i.test(contentType) || /^\s*[[{]/.test(text)) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

function toCliError(err: unknown, url: string): CliError {
  const e = err as { name?: string; message?: string; code?: string; cause?: { code?: string; message?: string; errors?: { code?: string }[] } };
  const host = new URL(url).host;
  if (e.name === 'TimeoutError' || e.name === 'AbortError') return new CliError('timeout', 'TIMEOUT', `no response from ${host} before the timeout`);
  const code = e.cause?.code ?? e.cause?.errors?.[0]?.code ?? e.code ?? '';
  const detail = `${code || e.cause?.message || e.message} reaching ${host}`;
  if (code === 'ENOTFOUND' || code === 'EAI_AGAIN') return new CliError('network', 'DNS', detail);
  if (code === 'ECONNREFUSED') return new CliError('network', 'CONN_REFUSED', detail);
  if (code.startsWith('ERR_TLS') || code.includes('CERT') || code.startsWith('UNABLE_TO')) return new CliError('network', 'TLS', detail);
  return new CliError('network', 'NETWORK', detail);
}

export async function sendRequest(i: SendInput): Promise<RawResponse> {
  const fetchImpl = i.fetchImpl ?? fetch;
  const sleep = i.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const maxAttempts = i.method.toUpperCase() === 'GET' ? MAX_ATTEMPTS : 1;
  for (let attempt = 1; ; attempt++) {
    try {
      const res = await fetchImpl(i.url, { method: i.method, headers: i.headers, body: i.body, signal: AbortSignal.timeout(i.timeoutMs) });
      const contentType = res.headers.get('content-type') ?? '';
      const bytes = Buffer.from(await res.arrayBuffer());
      if (attempt < maxAttempts && RETRY_STATUSES.has(res.status)) {
        await sleep(250 * 2 ** (attempt - 1));
        continue;
      }
      const bodyText = isTextual(contentType) ? bytes.toString('utf8') : `<${contentType} body, ${bytes.length} bytes, not shown>`;
      return { status: res.status, headers: Object.fromEntries(res.headers), bodyText, contentType, attempts: attempt };
    } catch (err) {
      const e = toCliError(err, i.url);
      if (attempt < maxAttempts && e.kind === 'network') {
        await sleep(250 * 2 ** (attempt - 1));
        continue;
      }
      throw e;
    }
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test 'tests/emws-api/http.test.ts'`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/emws-api/scripts/lib/http.ts tests/emws-api/http.test.ts
git commit -m "feat(emws-api): add HTTP transport with retries and network errors"
```

---

### Task 5: Authentication and token cache

**Files:**
- Create: `skills/emws-api/scripts/lib/auth.ts`
- Test: `tests/emws-api/auth.test.ts`

**Interfaces:**
- Consumes: `Context`, `loadContext` (Task 3); `CliError`, `detectSource`, `messageOf` (Task 2); `sendRequest`, `parseBody` (Task 4); helpers `makeConfigDir`, `testConfig`, `startServer`, `json`, `makeJwt`, `SECRETS`.
- Produces:
  - `export type AuthOptions = { fetchImpl?: typeof fetch; now?: () => number; forceLogin?: boolean; timeoutMs?: number }`
  - `export type AuthResult = { headers: Record<string, string>; token?: string; claims?: Record<string, unknown> }`
  - `export function decodeJwt(token: string): Record<string, unknown>`
  - `export async function resolveAuth(ctx: Context, o?: AuthOptions): Promise<AuthResult>` — `apiKey`: `{ headers: { 'x-api-key': secret } }`; `basic`: cached or fresh JWT with `{ headers: { authorization: 'Bearer …' }, token, claims }`.
  - `export async function exchangeApiKey(ctx: Context, o?: AuthOptions): Promise<{ token: string; claims: Record<string, unknown> }>`
  - Cache file: `<dir>/tokens/<profile>.json`, mode 600, `{ "token": string, "exp": number }`.

- [ ] **Step 1: Write the failing test**

`tests/emws-api/auth.test.ts`:

```ts
import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { decodeJwt, exchangeApiKey, resolveAuth } from '../../skills/emws-api/scripts/lib/auth.ts';
import { loadContext } from '../../skills/emws-api/scripts/lib/config.ts';
import { json, makeConfigDir, makeJwt, SECRETS, startServer, testConfig } from './helpers.ts';

const NOW = 1_800_000_000_000;
const exp = (secondsFromNow: number) => Math.floor(NOW / 1000) + secondsFromNow;

async function setup(handler: Parameters<typeof startServer>[0]) {
  const srv = await startServer(handler);
  const dir = await makeConfigDir(testConfig(srv.url));
  return { srv, dir, ctx: (profile: string) => loadContext({ dir, profile, env: {} }) };
}

test('an apiKey profile sends x-api-key and makes no call', async () => {
  const { srv, ctx } = await setup((_req, res) => json(res, 500, {}));
  try {
    assert.deepEqual(await resolveAuth(await ctx('stg')), { headers: { 'x-api-key': SECRETS.STG_KEY } });
    assert.equal(srv.requests.length, 0);
  } finally {
    await srv.close();
  }
});

test('a basic profile logs in once and caches the token with mode 600', async () => {
  const token = makeJwt({ sub: 'agent', database: 'TenantA', exp: exp(3600) });
  const { srv, dir, ctx } = await setup((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end(token);
  });
  try {
    const c = await ctx('user');
    const first = await resolveAuth(c, { now: () => NOW });
    assert.deepEqual(first.headers, { authorization: `Bearer ${token}` });
    assert.equal(first.claims?.database, 'TenantA');
    const login = srv.requests[0];
    assert.equal(login.method, 'POST');
    assert.equal(login.url, '/emws-staging/authenticateBasic');
    assert.deepEqual(JSON.parse(login.body), { username: 'agent@example.com', password: SECRETS.USER_PW, clientId: 'pocketmanage', dbId: 7 });

    const second = await resolveAuth(c, { now: () => NOW + 60_000 });
    assert.equal(second.token, token);
    assert.equal(second.claims?.sub, 'agent');
    assert.equal(srv.requests.length, 1);

    const file = path.join(dir, 'tokens', 'user.json');
    assert.equal((await stat(file)).mode & 0o777, 0o600);
    assert.equal(JSON.parse(await readFile(file, 'utf8')).token, token);
  } finally {
    await srv.close();
  }
});

test('logs in again within 60 seconds of expiry, or when forced', async () => {
  let n = 0;
  const { srv, ctx } = await setup((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end(makeJwt({ sub: `s${++n}`, exp: exp(30) }));
  });
  try {
    const c = await ctx('user');
    await resolveAuth(c, { now: () => NOW });
    await resolveAuth(c, { now: () => NOW });
    assert.equal(srv.requests.length, 2, 'a token 30s from expiry is refreshed');
    await resolveAuth(c, { now: () => NOW, forceLogin: true });
    assert.equal(srv.requests.length, 3);
  } finally {
    await srv.close();
  }
});

test('a rejected login is LOGIN_FAILED and carries the response', async () => {
  const { srv, ctx } = await setup((_req, res) => json(res, 401, { StatusCode: 401, Message: 'Invalid credentials', ErrorCode: null }));
  try {
    await assert.rejects(resolveAuth(await ctx('user'), { now: () => NOW }), (err: { kind: string; code: string; message: string; response?: { status: number; source: string } }) => {
      assert.equal(err.kind, 'auth');
      assert.equal(err.code, 'LOGIN_FAILED');
      assert.match(err.message, /Invalid credentials/);
      assert.equal(err.response?.status, 401);
      assert.equal(err.response?.source, 'api');
      return true;
    });
  } finally {
    await srv.close();
  }
});

test('a login response that is not a JWT is LOGIN_FAILED', async () => {
  const { srv, ctx } = await setup((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end('not-a-token');
  });
  try {
    await assert.rejects(resolveAuth(await ctx('user'), { now: () => NOW }), { code: 'LOGIN_FAILED', message: /not a JWT/ });
  } finally {
    await srv.close();
  }
});

test('exchangeApiKey posts the key and returns the claims', async () => {
  const token = makeJwt({ database: 'TenantB', exp: exp(3600) });
  const { srv, ctx } = await setup((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end(`"${token}"`);
  });
  try {
    const r = await exchangeApiKey(await ctx('stg'));
    assert.equal(r.claims.database, 'TenantB');
    assert.equal(srv.requests[0].url, '/emws-staging/authenticateWithApiKey');
    assert.equal(srv.requests[0].headers['x-api-key'], SECRETS.STG_KEY);
  } finally {
    await srv.close();
  }
});

test('decodeJwt reads the payload', () => {
  assert.deepEqual(decodeJwt(makeJwt({ a: 1 })), { a: 1 });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/auth.test.ts'`
Expected: FAIL with `Cannot find module …/auth.ts`.

- [ ] **Step 3: Implement**

`skills/emws-api/scripts/lib/auth.ts`:

```ts
/** Credentials per profile: the API key as a header, or a Basic-login JWT cached until shortly before it expires. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import type { Context } from './config.ts';
import { CliError, detectSource, messageOf } from './errors.ts';
import { parseBody, sendRequest } from './http.ts';

export type AuthOptions = { fetchImpl?: typeof fetch; now?: () => number; forceLogin?: boolean; timeoutMs?: number };
export type AuthResult = { headers: Record<string, string>; token?: string; claims?: Record<string, unknown> };

const REFRESH_MARGIN_MS = 60_000;
const DEFAULT_TIMEOUT_MS = 30_000;

export function decodeJwt(token: string): Record<string, unknown> {
  const payload = token.split('.')[1];
  try {
    if (!payload) throw new Error('missing payload');
    return JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) as Record<string, unknown>;
  } catch {
    throw new CliError('auth', 'LOGIN_FAILED', 'the login response is not a JWT');
  }
}

const tokenFile = (ctx: Context): string => path.join(ctx.dir, 'tokens', `${ctx.profileName}.json`);
const bearer = (token: string, claims: Record<string, unknown>): AuthResult => ({ headers: { authorization: `Bearer ${token}` }, token, claims });

async function readCachedToken(ctx: Context): Promise<{ token: string; exp: number } | undefined> {
  try {
    const cached = JSON.parse(await readFile(tokenFile(ctx), 'utf8')) as { token?: unknown; exp?: unknown };
    if (typeof cached.token === 'string' && typeof cached.exp === 'number') return { token: cached.token, exp: cached.exp };
  } catch {
    // A missing or corrupt cache means a fresh login.
  }
  return undefined;
}

async function writeCachedToken(ctx: Context, token: string, exp: number): Promise<void> {
  await mkdir(path.dirname(tokenFile(ctx)), { recursive: true, mode: 0o700 });
  await writeFile(tokenFile(ctx), JSON.stringify({ token, exp }), { mode: 0o600 });
}

/** POSTs to an auth route and returns the JWT from its text body, or throws LOGIN_FAILED with the response. */
async function postForToken(ctx: Context, route: string, headers: Record<string, string>, body: string | undefined, o: AuthOptions): Promise<string> {
  const res = await sendRequest({
    method: 'POST',
    url: `${ctx.environment.apiBase}${route}`,
    headers,
    body,
    timeoutMs: o.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    fetchImpl: o.fetchImpl,
  });
  const parsed = parseBody(res.bodyText, res.contentType);
  if (res.status !== 200) {
    const err = new CliError(
      'auth',
      'LOGIN_FAILED',
      `${route} for profile "${ctx.profileName}" returned ${res.status}: ${messageOf(parsed, res.status)}`,
      'Check the credentials configured for this profile; run `emws whoami`.',
    );
    err.response = { status: res.status, headers: res.headers, body: parsed, source: detectSource(parsed) };
    throw err;
  }
  return res.bodyText.trim().replace(/^"|"$/g, '');
}

export async function resolveAuth(ctx: Context, o: AuthOptions = {}): Promise<AuthResult> {
  if (ctx.profile.auth === 'apiKey') return { headers: { 'x-api-key': ctx.secret } };
  const now = (o.now ?? Date.now)();
  if (!o.forceLogin) {
    const cached = await readCachedToken(ctx);
    if (cached && cached.exp * 1000 - REFRESH_MARGIN_MS > now) return bearer(cached.token, decodeJwt(cached.token));
  }
  const body = JSON.stringify({ username: ctx.profile.username, password: ctx.secret, clientId: ctx.profile.clientId, dbId: ctx.profile.dbId });
  const token = await postForToken(ctx, '/authenticateBasic', { 'content-type': 'application/json' }, body, o);
  const claims = decodeJwt(token);
  await writeCachedToken(ctx, token, Number(claims.exp ?? 0));
  return bearer(token, claims);
}

/** Exchanges the profile's API key for a JWT; used by whoami to prove the key and show its tenant. */
export async function exchangeApiKey(ctx: Context, o: AuthOptions = {}): Promise<{ token: string; claims: Record<string, unknown> }> {
  const token = await postForToken(ctx, '/authenticateWithApiKey', { 'x-api-key': ctx.secret }, undefined, o);
  return { token, claims: decodeJwt(token) };
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test 'tests/emws-api/auth.test.ts'`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/emws-api/scripts/lib/auth.ts tests/emws-api/auth.test.ts
git commit -m "feat(emws-api): add API-key and Basic auth with token cache"
```

---

### Task 6: Call log

**Files:**
- Create: `skills/emws-api/scripts/lib/log.ts`
- Test: `tests/emws-api/log.test.ts`

**Interfaces:**
- Consumes: `CliError` (Task 2).
- Produces:
  - `export type LogEntry = { ts: string; callId: string; ok: boolean; status: number | null; code: string | null; method: string; path: string; profile: string; env: string; timingMs: number; cwd: string; branch: string | null; agent: string | null; envelope: unknown; requestBody: string | null; responseBody: string | null; responseTruncated: boolean }`
  - `export type LogFilter = { callId?: string; errors?: boolean; last?: number; sinceMs?: number; profile?: string; path?: string; cwd?: string; now?: number }`
  - `export const MAX_LOGGED_RESPONSE_BYTES = 65536`
  - `export function truncateBody(text: string | null): { body: string | null; truncated: boolean }`
  - `export async function appendLog(dir: string, entry: LogEntry): Promise<void>` — file `logs/<entry.ts date>.jsonl`, mode 600, one `appendFile` per entry.
  - `export async function readLogs(dir: string): Promise<LogEntry[]>` — oldest first, malformed lines skipped.
  - `export function queryLogs(entries: LogEntry[], f: LogFilter): LogEntry[]` — `callId` matches exactly; otherwise filters, then the last `last ?? 10`.
  - `export async function pruneLogs(dir: string, now: number, days?: number): Promise<void>`
  - `export function parseDuration(s: string): number` — `30s`, `15m`, `2h`, `1d` to ms; throws `CliError('config', 'USAGE', …)`.
  - `export function formatSummary(e: LogEntry): string`
  - `export function gitBranch(cwd: string): string | null`

- [ ] **Step 1: Write the failing test**

`tests/emws-api/log.test.ts`:

```ts
import assert from 'node:assert/strict';
import { appendFile, mkdir, mkdtemp, readdir, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import {
  appendLog,
  formatSummary,
  MAX_LOGGED_RESPONSE_BYTES,
  parseDuration,
  pruneLogs,
  queryLogs,
  readLogs,
  truncateBody,
} from '../../skills/emws-api/scripts/lib/log.ts';
import type { LogEntry } from '../../skills/emws-api/scripts/lib/log.ts';

const tmp = () => mkdtemp(path.join(os.tmpdir(), 'emws-log-'));

function entry(over: Partial<LogEntry>): LogEntry {
  return {
    ts: '2026-10-06T12:00:00.000Z',
    callId: 'c_00000000',
    ok: true,
    status: 200,
    code: null,
    method: 'GET',
    path: '/projects/1',
    profile: 'stg',
    env: 'staging',
    timingMs: 10,
    cwd: '/w/a',
    branch: null,
    agent: null,
    envelope: {},
    requestBody: null,
    responseBody: null,
    responseTruncated: false,
    ...over,
  };
}

test('appends JSON lines with mode 600 and reads them back in order', async () => {
  const dir = await tmp();
  await appendLog(dir, entry({ callId: 'c_1', ts: '2026-10-06T12:00:00.000Z' }));
  await appendLog(dir, entry({ callId: 'c_2', ts: '2026-10-06T12:00:01.000Z' }));
  assert.deepEqual((await readLogs(dir)).map((e) => e.callId), ['c_1', 'c_2']);
  assert.equal((await stat(path.join(dir, 'logs', '2026-10-06.jsonl'))).mode & 0o777, 0o600);
});

test('skips a malformed line and keeps the rest', async () => {
  const dir = await tmp();
  await appendLog(dir, entry({ callId: 'c_1' }));
  await appendFile(path.join(dir, 'logs', '2026-10-06.jsonl'), '{"callId":"c_broken", "ok": tr\n');
  await appendLog(dir, entry({ callId: 'c_2', ts: '2026-10-06T12:00:05.000Z' }));
  assert.deepEqual((await readLogs(dir)).map((e) => e.callId), ['c_1', 'c_2']);
});

test('readLogs returns nothing when there is no log directory', async () => {
  assert.deepEqual(await readLogs(await tmp()), []);
});

test('filters by call id, errors, profile, path, cwd, since, and last', () => {
  const now = Date.parse('2026-10-06T12:30:00.000Z');
  const all = [
    entry({ callId: 'c_a', ts: '2026-10-06T11:00:00.000Z' }),
    entry({ callId: 'c_b', ts: '2026-10-06T12:20:00.000Z', ok: false, status: 500, code: 'HTTP_500', profile: 'prod' }),
    entry({ callId: 'c_c', ts: '2026-10-06T12:25:00.000Z', path: '/receiving/4/lines', cwd: '/w/b' }),
  ];
  assert.deepEqual(queryLogs(all, { callId: 'c_b' }).map((e) => e.callId), ['c_b']);
  assert.deepEqual(queryLogs(all, { errors: true }).map((e) => e.callId), ['c_b']);
  assert.deepEqual(queryLogs(all, { profile: 'prod' }).map((e) => e.callId), ['c_b']);
  assert.deepEqual(queryLogs(all, { path: 'receiving' }).map((e) => e.callId), ['c_c']);
  assert.deepEqual(queryLogs(all, { cwd: '/w/a' }).map((e) => e.callId), ['c_a', 'c_b']);
  assert.deepEqual(queryLogs(all, { sinceMs: 15 * 60_000, now }).map((e) => e.callId), ['c_b', 'c_c']);
  assert.deepEqual(queryLogs(all, { last: 1 }).map((e) => e.callId), ['c_c']);
});

test('keeps the last ten by default', () => {
  const many = Array.from({ length: 12 }, (_, i) => entry({ callId: `c_${i}` }));
  assert.deepEqual(queryLogs(many, {}).map((e) => e.callId), many.slice(2).map((e) => e.callId));
});

test('prunes log files older than 14 days and leaves other files', async () => {
  const dir = await tmp();
  await mkdir(path.join(dir, 'logs'));
  for (const name of ['2026-09-01.jsonl', '2026-09-30.jsonl', 'notes.txt']) await writeFile(path.join(dir, 'logs', name), '');
  await pruneLogs(dir, Date.parse('2026-10-06T00:00:00Z'));
  assert.deepEqual((await readdir(path.join(dir, 'logs'))).sort(), ['2026-09-30.jsonl', 'notes.txt']);
});

test('truncates bodies over 64 KB', () => {
  assert.deepEqual(truncateBody('small'), { body: 'small', truncated: false });
  const big = truncateBody('x'.repeat(MAX_LOGGED_RESPONSE_BYTES + 10));
  assert.equal(big.truncated, true);
  assert.equal(Buffer.byteLength(big.body ?? ''), MAX_LOGGED_RESPONSE_BYTES);
  assert.deepEqual(truncateBody(null), { body: null, truncated: false });
});

test('parses durations and rejects nonsense', () => {
  assert.deepEqual(['30s', '15m', '2h', '1d'].map(parseDuration), [30_000, 900_000, 7_200_000, 86_400_000]);
  assert.throws(() => parseDuration('soon'), { code: 'USAGE' });
});

test('a summary line names status, method, path, profile, and call id', () => {
  const line = formatSummary(entry({ callId: 'c_9', ok: false, status: null, code: 'CONN_REFUSED' }));
  for (const part of ['CONN_REFUSED', 'GET', '/projects/1', 'stg', 'c_9']) assert.ok(line.includes(part), line);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/log.test.ts'`
Expected: FAIL with `Cannot find module …/log.ts`.

- [ ] **Step 3: Implement**

`skills/emws-api/scripts/lib/log.ts`:

```ts
/** The local call log: one redacted JSON line per call in logs/YYYY-MM-DD.jsonl, kept 14 days. */
import { execFileSync } from 'node:child_process';
import { appendFile, mkdir, readdir, readFile, unlink } from 'node:fs/promises';
import path from 'node:path';
import { CliError } from './errors.ts';

export type LogEntry = {
  ts: string;
  callId: string;
  ok: boolean;
  status: number | null;
  code: string | null;
  method: string;
  path: string;
  profile: string;
  env: string;
  timingMs: number;
  cwd: string;
  branch: string | null;
  agent: string | null;
  envelope: unknown;
  requestBody: string | null;
  responseBody: string | null;
  responseTruncated: boolean;
};

export type LogFilter = { callId?: string; errors?: boolean; last?: number; sinceMs?: number; profile?: string; path?: string; cwd?: string; now?: number };

export const MAX_LOGGED_RESPONSE_BYTES = 64 * 1024;
const RETENTION_DAYS = 14;
const DEFAULT_LAST = 10;
const LOG_FILE = /^(\d{4}-\d{2}-\d{2})\.jsonl$/;

const logsDir = (dir: string): string => path.join(dir, 'logs');

export function truncateBody(text: string | null): { body: string | null; truncated: boolean } {
  if (text === null) return { body: null, truncated: false };
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length <= MAX_LOGGED_RESPONSE_BYTES) return { body: text, truncated: false };
  return { body: bytes.subarray(0, MAX_LOGGED_RESPONSE_BYTES).toString('utf8'), truncated: true };
}

/** One appendFile per entry, so concurrent agents interleave whole lines. */
export async function appendLog(dir: string, entry: LogEntry): Promise<void> {
  await mkdir(logsDir(dir), { recursive: true, mode: 0o700 });
  await appendFile(path.join(logsDir(dir), `${entry.ts.slice(0, 10)}.jsonl`), `${JSON.stringify(entry)}\n`, { mode: 0o600 });
}

export async function readLogs(dir: string): Promise<LogEntry[]> {
  let names: string[];
  try {
    names = (await readdir(logsDir(dir))).filter((n) => LOG_FILE.test(n)).sort();
  } catch {
    return [];
  }
  const entries: LogEntry[] = [];
  for (const name of names) {
    for (const line of (await readFile(path.join(logsDir(dir), name), 'utf8')).split('\n')) {
      if (!line.trim()) continue;
      try {
        entries.push(JSON.parse(line) as LogEntry);
      } catch {
        // A torn or corrupt line is skipped so the rest of the log stays usable.
      }
    }
  }
  return entries.sort((a, b) => a.ts.localeCompare(b.ts));
}

export function queryLogs(entries: LogEntry[], f: LogFilter): LogEntry[] {
  if (f.callId) return entries.filter((e) => e.callId === f.callId);
  const now = f.now ?? Date.now();
  const matched = entries.filter(
    (e) =>
      (!f.errors || !e.ok) &&
      (!f.profile || e.profile === f.profile) &&
      (!f.path || e.path.includes(f.path)) &&
      (!f.cwd || e.cwd === f.cwd) &&
      (f.sinceMs === undefined || Date.parse(e.ts) >= now - f.sinceMs),
  );
  return matched.slice(-(f.last ?? DEFAULT_LAST));
}

export async function pruneLogs(dir: string, now: number, days = RETENTION_DAYS): Promise<void> {
  let names: string[];
  try {
    names = await readdir(logsDir(dir));
  } catch {
    return;
  }
  const cutoff = now - days * 86_400_000;
  for (const name of names) {
    const m = LOG_FILE.exec(name);
    if (m && Date.parse(`${m[1]}T00:00:00Z`) < cutoff) await unlink(path.join(logsDir(dir), name));
  }
}

const UNITS: Record<string, number> = { s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000 };

export function parseDuration(s: string): number {
  const m = /^(\d+)([smhd])$/.exec(s.trim());
  if (!m) throw new CliError('config', 'USAGE', `"${s}" is not a duration`, 'Use a number and a unit, such as 30s, 15m, 2h, or 1d.');
  return Number(m[1]) * UNITS[m[2]];
}

export function formatSummary(e: LogEntry): string {
  return `${e.ts.slice(0, 19).replace('T', ' ')}  ${String(e.status ?? e.code).padEnd(12)} ${e.method.padEnd(6)} ${e.path} · ${e.profile} · ${e.timingMs}ms · ${e.callId}`;
}

export function gitBranch(cwd: string): string | null {
  try {
    const out = execFileSync('git', ['-C', cwd, 'branch', '--show-current'], { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
    return out || null;
  } catch {
    return null;
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test 'tests/emws-api/log.test.ts'`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/emws-api/scripts/lib/log.ts tests/emws-api/log.test.ts
git commit -m "feat(emws-api): add the call log with filters and retention"
```

---

### Task 7: OpenAPI discovery

**Files:**
- Create: `skills/emws-api/scripts/lib/spec.ts`
- Create: `tests/emws-api/fixtures/spec.json`
- Test: `tests/emws-api/spec.test.ts`

**Interfaces:**
- Consumes: `CliError` (Task 2); helpers `startServer`, `json`.
- Produces:
  - `export type OpenApiDoc = { paths?: Record<string, Record<string, unknown>>; [k: string]: unknown }`
  - `export type Route = { method: string; path: string; summary: string }`
  - `export const SPEC_TTL_MS = 600000`
  - `export function normalizePath(p: string): string` — drops the query, a leading `/api` segment, and a trailing slash.
  - `export function listRoutes(doc: OpenApiDoc, filter?: string): Route[]`
  - `export function pathKnown(doc: OpenApiDoc, path: string): boolean`
  - `export function nearestRoutes(doc: OpenApiDoc, method: string, path: string, n?: number): string[]` — formatted `METHOD /path`.
  - `export function describeRoute(doc: OpenApiDoc, method: string, path: string): Record<string, unknown> | undefined`
  - `export async function loadSpec(o: { dir: string; envName: string; specUrl: string; refresh?: boolean; fetchImpl?: typeof fetch; now?: () => number }): Promise<OpenApiDoc>` — cache `cache/spec-<env>.json`; failure throws `CliError('network', 'SPEC_UNAVAILABLE', …)`.

- [ ] **Step 1: Write the fixture**

`tests/emws-api/fixtures/spec.json`:

```json
{
  "openapi": "3.0.1",
  "paths": {
    "/api/projects": {
      "get": { "summary": "List projects", "responses": { "200": { "description": "ok" } } },
      "post": {
        "summary": "Create project",
        "requestBody": { "content": { "application/json": { "schema": { "$ref": "#/components/schemas/Project" } } } },
        "responses": { "201": { "description": "created" } }
      }
    },
    "/api/projects/{id}": {
      "parameters": [{ "name": "id", "in": "path", "required": true, "schema": { "type": "integer" } }],
      "get": {
        "summary": "Get project",
        "responses": { "200": { "description": "ok", "content": { "application/json": { "schema": { "$ref": "#/components/schemas/Project" } } } } }
      }
    },
    "/api/receiving/{poId}/lines": { "get": { "summary": "List receiving lines", "responses": { "200": { "description": "ok" } } } },
    "/api/management/keys": { "get": { "summary": "List API keys", "responses": { "200": { "description": "ok" } } } }
  },
  "components": {
    "schemas": {
      "Project": {
        "type": "object",
        "properties": { "id": { "type": "integer" }, "name": { "type": "string" }, "parent": { "$ref": "#/components/schemas/Project" } }
      }
    }
  }
}
```

- [ ] **Step 2: Write the failing test**

`tests/emws-api/spec.test.ts`:

```ts
import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import {
  describeRoute,
  listRoutes,
  loadSpec,
  nearestRoutes,
  normalizePath,
  pathKnown,
  SPEC_TTL_MS,
} from '../../skills/emws-api/scripts/lib/spec.ts';
import type { OpenApiDoc } from '../../skills/emws-api/scripts/lib/spec.ts';
import { json, startServer } from './helpers.ts';

const doc = JSON.parse(await readFile(new URL('./fixtures/spec.json', import.meta.url), 'utf8')) as OpenApiDoc;

test('normalizes the /api prefix, query, and trailing slash', () => {
  assert.equal(normalizePath('/api/projects/1/?x=1'), '/projects/1');
  assert.equal(normalizePath('projects'), '/projects');
  assert.equal(normalizePath('/apiary/x'), '/apiary/x');
});

test('lists routes sorted, filtered case-insensitively', () => {
  assert.deepEqual(listRoutes(doc).map((r) => `${r.method} ${r.path}`), [
    'GET /management/keys',
    'GET /projects',
    'POST /projects',
    'GET /projects/{id}',
    'GET /receiving/{poId}/lines',
  ]);
  assert.deepEqual(listRoutes(doc, 'RECEIV').map((r) => r.summary), ['List receiving lines']);
});

test('knows whether a concrete path matches any route', () => {
  assert.equal(pathKnown(doc, '/projects/123'), true);
  assert.equal(pathKnown(doc, '/project/123'), false);
});

test('suggests the closest routes for a mistyped path', () => {
  assert.equal(nearestRoutes(doc, 'GET', '/project/123')[0], 'GET /projects/{id}');
  assert.equal(nearestRoutes(doc, 'GET', '/receving/9/lines')[0], 'GET /receiving/{poId}/lines');
  assert.equal(nearestRoutes(doc, 'GET', '/x').length, 3);
});

test('describes a route with references resolved and cycles cut', () => {
  const d = describeRoute(doc, 'get', '/projects/{id}') as Record<string, any>;
  assert.equal(d.method, 'GET');
  assert.equal(d.path, '/projects/{id}');
  assert.equal(d.parameters[0].name, 'id');
  const schema = d.responses['200'].content['application/json'].schema;
  assert.equal(schema.properties.name.type, 'string');
  assert.equal(schema.properties.parent.note, 'recursive reference not expanded');
  assert.equal(describeRoute(doc, 'DELETE', '/projects/1'), undefined);
});

test('caches the spec for ten minutes; --refresh refetches', async () => {
  const srv = await startServer((_req, res) => json(res, 200, doc));
  const dir = await mkdtemp(path.join(os.tmpdir(), 'emws-spec-'));
  const base = { dir, envName: 'staging', specUrl: `${srv.url}/spec.json` };
  try {
    await loadSpec({ ...base, now: () => 0 });
    await loadSpec({ ...base, now: () => SPEC_TTL_MS - 1 });
    assert.equal(srv.requests.length, 1);
    await loadSpec({ ...base, now: () => SPEC_TTL_MS - 1, refresh: true });
    assert.equal(srv.requests.length, 2);
    await loadSpec({ ...base, now: () => SPEC_TTL_MS * 3 });
    assert.equal(srv.requests.length, 3);
  } finally {
    await srv.close();
  }
});

test('an unreachable spec is SPEC_UNAVAILABLE', async () => {
  const srv = await startServer((_req, res) => json(res, 500, {}));
  const dir = await mkdtemp(path.join(os.tmpdir(), 'emws-spec-'));
  try {
    await assert.rejects(loadSpec({ dir, envName: 'staging', specUrl: `${srv.url}/spec.json` }), { kind: 'network', code: 'SPEC_UNAVAILABLE' });
  } finally {
    await srv.close();
  }
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/spec.test.ts'`
Expected: FAIL with `Cannot find module …/spec.ts`.

- [ ] **Step 4: Implement**

`skills/emws-api/scripts/lib/spec.ts`:

```ts
/** Reads the Function App's OpenAPI document: cached fetch, route listing, route matching, and $ref-resolved descriptions. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { CliError } from './errors.ts';

export type OpenApiDoc = { paths?: Record<string, Record<string, unknown>>; [k: string]: unknown };
export type Route = { method: string; path: string; summary: string };
type Operation = { summary?: string; operationId?: string; parameters?: unknown[]; requestBody?: unknown; responses?: unknown };

export const SPEC_TTL_MS = 10 * 60 * 1000;
const METHODS = ['get', 'post', 'put', 'patch', 'delete'];
const MAX_REF_DEPTH = 12;
const FETCH_TIMEOUT_MS = 15_000;

/** Spec paths and CLI paths compare without the Functions `/api` prefix, query, or trailing slash. */
export function normalizePath(p: string): string {
  let out = p.split('?')[0];
  if (!out.startsWith('/')) out = `/${out}`;
  out = out.replace(/^\/api(?=\/|$)/i, '');
  out = out.replace(/\/+$/, '');
  return out || '/';
}

const segments = (p: string): string[] => normalizePath(p).split('/').filter(Boolean);
const isParam = (s: string): boolean => /^\{.+\}$/.test(s);

function templateMatches(template: string, actual: string): boolean {
  const t = segments(template);
  const a = segments(actual);
  return t.length === a.length && t.every((s, i) => isParam(s) || s.toLowerCase() === a[i].toLowerCase());
}

export function listRoutes(doc: OpenApiDoc, filter?: string): Route[] {
  const routes: Route[] = [];
  for (const [rawPath, ops] of Object.entries(doc.paths ?? {})) {
    for (const method of METHODS) {
      const op = ops[method] as Operation | undefined;
      if (op) routes.push({ method: method.toUpperCase(), path: normalizePath(rawPath), summary: op.summary ?? op.operationId ?? '' });
    }
  }
  routes.sort((a, b) => a.path.localeCompare(b.path) || METHODS.indexOf(a.method.toLowerCase()) - METHODS.indexOf(b.method.toLowerCase()));
  if (!filter) return routes;
  const needle = filter.toLowerCase();
  return routes.filter((r) => `${r.method} ${r.path} ${r.summary}`.toLowerCase().includes(needle));
}

export function pathKnown(doc: OpenApiDoc, p: string): boolean {
  return Object.keys(doc.paths ?? {}).some((t) => templateMatches(t, p));
}

function levenshtein(a: string, b: string): number {
  const row = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    let prev = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const tmp = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1));
      prev = tmp;
    }
  }
  return row[b.length];
}

function segmentCost(t: string, a: string): number {
  if (isParam(t) || t.toLowerCase() === a.toLowerCase()) return 0;
  return levenshtein(t.toLowerCase(), a.toLowerCase()) / Math.max(t.length, a.length);
}

/** Edit distance over path segments: a parameter matches anything, a misspelt segment costs its relative edit distance. */
function pathDistance(template: string, actual: string): number {
  const t = segments(template);
  const a = segments(actual);
  const d = Array.from({ length: t.length + 1 }, (_, i) => Array.from({ length: a.length + 1 }, (_, j) => (i === 0 ? j : j === 0 ? i : 0)));
  for (let i = 1; i <= t.length; i++) {
    for (let j = 1; j <= a.length; j++) {
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + segmentCost(t[i - 1], a[j - 1]));
    }
  }
  return d[t.length][a.length];
}

export function nearestRoutes(doc: OpenApiDoc, method: string, p: string, n = 3): string[] {
  const wanted = method.toUpperCase();
  return listRoutes(doc)
    .map((r) => ({ r, d: pathDistance(r.path, p) + (r.method === wanted ? 0 : 0.25) }))
    .sort((x, y) => x.d - y.d)
    .slice(0, n)
    .map(({ r }) => `${r.method} ${r.path}`);
}

function resolvePointer(doc: OpenApiDoc, ref: string): unknown {
  return ref
    .slice(2)
    .split('/')
    .map((s) => s.replace(/~1/g, '/').replace(/~0/g, '~'))
    .reduce<unknown>((node, key) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : undefined), doc);
}

function resolveRefs(node: unknown, doc: OpenApiDoc, seen: string[] = []): unknown {
  if (Array.isArray(node)) return node.map((x) => resolveRefs(x, doc, seen));
  if (node === null || typeof node !== 'object') return node;
  const ref = (node as Record<string, unknown>).$ref;
  if (typeof ref === 'string' && ref.startsWith('#/')) {
    if (seen.includes(ref) || seen.length >= MAX_REF_DEPTH) return { $ref: ref, note: 'recursive reference not expanded' };
    return resolveRefs(resolvePointer(doc, ref), doc, [...seen, ref]);
  }
  return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, resolveRefs(v, doc, seen)]));
}

export function describeRoute(doc: OpenApiDoc, method: string, p: string): Record<string, unknown> | undefined {
  for (const [rawPath, ops] of Object.entries(doc.paths ?? {})) {
    if (!templateMatches(rawPath, p)) continue;
    const op = ops[method.toLowerCase()] as Operation | undefined;
    if (!op) continue;
    const shared = (ops.parameters as unknown[] | undefined) ?? [];
    const resolved = resolveRefs({ parameters: [...shared, ...(op.parameters ?? [])], requestBody: op.requestBody, responses: op.responses }, doc) as Record<string, unknown>;
    return { method: method.toUpperCase(), path: normalizePath(rawPath), summary: op.summary ?? op.operationId ?? '', ...resolved };
  }
  return undefined;
}

export async function loadSpec(o: {
  dir: string;
  envName: string;
  specUrl: string;
  refresh?: boolean;
  fetchImpl?: typeof fetch;
  now?: () => number;
}): Promise<OpenApiDoc> {
  const now = (o.now ?? Date.now)();
  const file = path.join(o.dir, 'cache', `spec-${o.envName}.json`);
  if (!o.refresh) {
    try {
      const cached = JSON.parse(await readFile(file, 'utf8')) as { fetchedAt: number; doc: OpenApiDoc };
      if (now - cached.fetchedAt < SPEC_TTL_MS) return cached.doc;
    } catch {
      // No usable cache; fetch below.
    }
  }
  let doc: OpenApiDoc;
  try {
    const res = await (o.fetchImpl ?? fetch)(o.specUrl, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    doc = (await res.json()) as OpenApiDoc;
  } catch (err) {
    throw new CliError(
      'network',
      'SPEC_UNAVAILABLE',
      `could not load the OpenAPI document from ${o.specUrl}: ${(err as Error).message}`,
      "Check the environment's specUrl; route discovery needs the Function App to be reachable.",
    );
  }
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, JSON.stringify({ fetchedAt: now, doc }));
  return doc;
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `node --test 'tests/emws-api/spec.test.ts'`
Expected: PASS, 7 tests.

- [ ] **Step 6: Commit**

```bash
git add skills/emws-api/scripts/lib/spec.ts tests/emws-api/fixtures/spec.json tests/emws-api/spec.test.ts
git commit -m "feat(emws-api): add OpenAPI route discovery"
```

---

### Task 8: Request command, output contract, and entry point

**Files:**
- Create: `skills/emws-api/scripts/lib/cli.ts`
- Create: `skills/emws-api/scripts/emws.ts`
- Modify: `tests/emws-api/helpers.ts` (add `runCli`)
- Test: `tests/emws-api/request.test.ts`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces:
  - `export type Io = { stdout: (s: string) => void; stderr: (s: string) => void; cwd: string; env: Record<string, string | undefined>; stdin: () => Promise<string>; fetchImpl?: typeof fetch; now?: () => number }`
  - `export async function main(argv: string[], io: Io): Promise<number>`
  - `export const USAGE_TEXT: string`
  - Internal to `cli.ts`, used again in Task 9: `type Flags`, `usageError(message)`, `fail(err, io, flags)`, `errorEnvelope(err, c)`.
  - helpers: `runCli(argv, { dir, env?, stdin?, now?, cwd? }): Promise<{ code: number; stdout: string; stderr: string }>`

- [ ] **Step 1: Add `runCli` to the helpers**

Append to `tests/emws-api/helpers.ts`:

```ts
import { main } from '../../skills/emws-api/scripts/lib/cli.ts';

export async function runCli(
  argv: string[],
  o: { dir: string; env?: Record<string, string>; stdin?: string; now?: () => number; cwd?: string },
): Promise<{ code: number; stdout: string; stderr: string }> {
  let stdout = '';
  let stderr = '';
  const code = await main(argv, {
    stdout: (s) => {
      stdout += s;
    },
    stderr: (s) => {
      stderr += s;
    },
    cwd: o.cwd ?? o.dir,
    env: { EMWS_CONFIG_DIR: o.dir, ...o.env },
    stdin: async () => o.stdin ?? '',
    now: o.now,
  });
  return { code, stdout, stderr };
}
```

Move the new `import` line to the top of the file with the other imports.

- [ ] **Step 2: Write the failing test**

`tests/emws-api/request.test.ts`:

```ts
import assert from 'node:assert/strict';
import { readdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import type { ServerResponse } from 'node:http';
import { readLogs } from '../../skills/emws-api/scripts/lib/log.ts';
import type { Recorded } from './helpers.ts';
import { json, makeConfigDir, makeJwt, runCli, SECRETS, startServer, testConfig } from './helpers.ts';

const spec = JSON.parse(await readFile(new URL('./fixtures/spec.json', import.meta.url), 'utf8'));

async function withApi(handler: (req: Recorded, res: ServerResponse) => void, fn: (h: { dir: string; url: string; requests: Recorded[] }) => Promise<void>, envMode = 0o600) {
  const srv = await startServer((req, res) => (req.url === '/spec.json' ? json(res, 200, spec) : handler(req, res)));
  const dir = await makeConfigDir(testConfig(srv.url), SECRETS, envMode);
  try {
    await fn({ dir, url: srv.url, requests: srv.requests });
  } finally {
    await srv.close();
  }
}

const allLogText = async (dir: string) => {
  const names = await readdir(path.join(dir, 'logs'));
  return (await Promise.all(names.map((n) => readFile(path.join(dir, 'logs', n), 'utf8')))).join('');
};

test('GET prints the body to stdout and a summary to stderr', async () => {
  await withApi((_req, res) => json(res, 200, { id: 1, name: 'A' }), async ({ dir, requests }) => {
    const r = await runCli(['get', '/projects/1', '--query', 'start=0', '--query', 'limit=5'], { dir });
    assert.equal(r.code, 0);
    assert.deepEqual(JSON.parse(r.stdout), { id: 1, name: 'A' });
    assert.match(r.stderr, /^200 GET \/projects\/1 · \d+ms · stg · call c_[0-9a-f]{8}\n$/);
    const req = requests[0];
    assert.equal(req.url, '/emws-staging/projects/1?start=0&limit=5');
    assert.equal(req.headers['x-api-key'], SECRETS.STG_KEY);
    assert.match(String(req.headers.traceparent), /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
    assert.match(String(req.headers['x-emws-cli-call-id']), /^c_[0-9a-f]{8}$/);
  });
});

test('--json prints one success envelope', async () => {
  await withApi((_req, res) => json(res, 200, [1, 2]), async ({ dir }) => {
    const r = await runCli(['get', '/projects', '--json', '-p', 'prod'], { dir });
    const env = JSON.parse(r.stdout);
    assert.equal(env.ok, true);
    assert.equal(env.status, 200);
    assert.deepEqual(env.body, [1, 2]);
    assert.equal(env.profile, 'prod');
    assert.equal(env.env, 'prod');
    assert.match(env.callId, /^c_/);
  });
});

test('an API 500 yields the full error envelope with correlation and no secrets', async () => {
  await withApi(
    (_req, res) => json(res, 500, { StatusCode: 500, Message: 'Object reference not set', ErrorCode: null }, { 'x-correlation-id': 'inv-1' }),
    async ({ dir }) => {
      const r = await runCli(['get', '/projects/1', '--json'], { dir });
      assert.equal(r.code, 6);
      const env = JSON.parse(r.stdout);
      assert.equal(env.error.kind, 'server');
      assert.equal(env.error.code, 'HTTP_500');
      assert.equal(env.error.message, 'Object reference not set');
      assert.equal(env.response.source, 'api');
      assert.equal(env.correlation.correlationId, 'inv-1');
      assert.match(env.correlation.kql, /inv-1/);
      assert.match(env.correlation.kql, new RegExp(env.correlation.traceparent.split('-')[1]));
      assert.equal(env.request.headers['x-api-key'], '***');
      assert.ok(!r.stdout.includes(SECRETS.STG_KEY));
    },
  );
});

test('without --json the error goes to stderr and stdout stays empty', async () => {
  await withApi((_req, res) => json(res, 500, { StatusCode: 500, Message: 'boom', ErrorCode: null }), async ({ dir }) => {
    const r = await runCli(['get', '/projects/1'], { dir });
    assert.equal(r.code, 6);
    assert.equal(r.stdout, '');
    assert.match(r.stderr, /error server HTTP_500/);
    assert.match(r.stderr, /full call: emws log c_/);
  });
});

test('an APIM 401 is an auth error pointing at whoami', async () => {
  await withApi((_req, res) => json(res, 401, { statusCode: 401, message: 'Invalid JWT.' }), async ({ dir }) => {
    const r = await runCli(['get', '/projects/1', '--json'], { dir });
    assert.equal(r.code, 3);
    const env = JSON.parse(r.stdout);
    assert.equal(env.response.source, 'apim');
    assert.match(env.error.hint, /emws whoami/);
  });
});

test('an HTML 502 on a POST is reported raw and not retried', async () => {
  await withApi(
    (_req, res) => {
      res.writeHead(502, { 'content-type': 'text/html' });
      res.end('<html><body>Bad Gateway</body></html>');
    },
    async ({ dir, requests }) => {
      const r = await runCli(['post', '/projects', '--body', '{"name":"x"}', '--json'], { dir });
      assert.equal(r.code, 6);
      const env = JSON.parse(r.stdout);
      assert.equal(env.response.source, 'unknown');
      assert.match(env.response.body, /<html>/);
      assert.equal(env.error.message, 'Bad Gateway');
      assert.equal(requests.length, 1);
    },
  );
});

test('a 404 on an unknown path suggests the closest routes', async () => {
  await withApi((_req, res) => json(res, 404, { StatusCode: 404, Message: 'Not Found', ErrorCode: null }), async ({ dir }) => {
    const env = JSON.parse((await runCli(['get', '/project/123', '--json'], { dir })).stdout);
    assert.match(env.error.hint, /GET \/projects\/\{id\}/);
  });
});

test('a 404 on a known route says the record was not found', async () => {
  await withApi((_req, res) => json(res, 404, { StatusCode: 404, Message: 'Project 999 not found', ErrorCode: null }), async ({ dir }) => {
    const r = await runCli(['get', '/projects/999', '--json'], { dir });
    assert.equal(r.code, 5);
    assert.match(JSON.parse(r.stdout).error.hint, /record was not found/);
  });
});

test('a write on a read-only prod profile is blocked before any request', async () => {
  await withApi((_req, res) => json(res, 200, {}), async ({ dir, requests }) => {
    const r = await runCli(['delete', '/projects/1', '-p', 'prod', '--json'], { dir });
    assert.equal(r.code, 4);
    const env = JSON.parse(r.stdout);
    assert.equal(env.error.code, 'WRITE_BLOCKED');
    assert.match(env.error.hint, /"prod"/);
    assert.equal(requests.length, 0);
  });
});

test('a prod profile that allows writes warns on stderr', async () => {
  await withApi((_req, res) => json(res, 201, { id: 9 }), async ({ dir }) => {
    const r = await runCli(['post', '/projects', '-p', 'prod-rw', '--body', '{"name":"x"}'], { dir });
    assert.equal(r.code, 0);
    assert.match(r.stderr, /warning: POST against production through profile "prod-rw"/);
  });
});

test('a basic profile logs in again once after a 401', async () => {
  let logins = 0;
  let calls = 0;
  await withApi(
    (req, res) => {
      if (req.url.endsWith('/authenticateBasic')) {
        logins++;
        res.writeHead(200, { 'content-type': 'text/plain' });
        res.end(makeJwt({ sub: `s${logins}`, exp: Math.floor(Date.now() / 1000) + 3600 }));
        return;
      }
      calls++;
      if (calls === 1) json(res, 401, { statusCode: 401, message: 'Invalid JWT.' });
      else json(res, 200, { ok: true });
    },
    async ({ dir }) => {
      const r = await runCli(['get', '/projects/1', '-p', 'user'], { dir });
      assert.equal(r.code, 0);
      assert.equal(logins, 2);
      assert.equal(calls, 2);
    },
  );
});

test('a refused connection is a network error naming apiBase', async () => {
  const srv = await startServer(() => {});
  await srv.close();
  const dir = await makeConfigDir(testConfig(srv.url));
  const r = await runCli(['get', '/projects/1', '--json'], { dir });
  assert.equal(r.code, 7);
  const env = JSON.parse(r.stdout);
  assert.equal(env.error.code, 'CONN_REFUSED');
  assert.match(env.error.hint, /apiBase/);
});

test('the log records the call with every secret masked', async () => {
  await withApi(
    (req, res) => json(res, 200, { echoedKey: req.headers['x-api-key'] }),
    async ({ dir }) => {
      const r = await runCli(['post', '/things', '--body', JSON.stringify({ password: SECRETS.USER_PW, note: SECRETS.STG_KEY })], { dir });
      assert.equal(r.code, 0);
      assert.ok(!r.stdout.includes(SECRETS.STG_KEY), 'stdout is masked');
      const text = await allLogText(dir);
      for (const secret of Object.values(SECRETS)) assert.ok(!text.includes(secret), `log leaked ${secret}`);
      const [entry] = await readLogs(dir);
      assert.equal(entry.method, 'POST');
      assert.equal(entry.path, '/things');
      assert.equal(entry.profile, 'stg');
      assert.equal(entry.ok, true);
      assert.match(entry.requestBody ?? '', /"password":"\*\*\*"/);
    },
  );
});

test('a body that is not JSON is a usage error and sends nothing', async () => {
  await withApi((_req, res) => json(res, 200, {}), async ({ dir, requests }) => {
    const r = await runCli(['post', '/projects', '--body', '{name:', '--json'], { dir });
    assert.equal(r.code, 2);
    assert.equal(JSON.parse(r.stdout).error.code, 'USAGE');
    assert.equal(requests.length, 0);
  });
});

test('reads the body from a file or stdin', async () => {
  await withApi((_req, res) => json(res, 200, {}), async ({ dir, requests }) => {
    await writeFile(path.join(dir, 'payload.json'), '{"from":"file"}');
    await runCli(['post', '/projects', '--body', '@payload.json'], { dir });
    await runCli(['post', '/projects', '--body', '-'], { dir, stdin: '{"from":"stdin"}' });
    assert.deepEqual(requests.map((r) => JSON.parse(r.body).from), ['file', 'stdin']);
  });
});

test('a world-readable .env stops the call with chmod advice', async () => {
  await withApi(
    (_req, res) => json(res, 200, {}),
    async ({ dir, requests }) => {
      const r = await runCli(['get', '/projects/1'], { dir });
      assert.equal(r.code, 2);
      assert.match(r.stderr, /ENV_PERMS/);
      assert.match(r.stderr, /chmod 600/);
      assert.equal(requests.length, 0);
    },
    0o644,
  );
});

// Review Focus 1
test('a full URL or an /api prefix is a usage error before any request', async () => {
  await withApi((_req, res) => json(res, 200, {}), async ({ dir, url, requests }) => {
    const full = await runCli(['get', `${url}/emws-staging/projects/1`, '--json'], { dir });
    assert.equal(full.code, 2);
    assert.match(JSON.parse(full.stdout).error.message, /relative to the environment/);
    const api = await runCli(['get', '/api/projects/1', '--json'], { dir });
    assert.equal(api.code, 2);
    assert.match(JSON.parse(api.stdout).error.message, /\/api prefix/);
    assert.equal(requests.length, 0);
  });
});

// Review Focus 2
test('a binary success prints a placeholder, not bytes', async () => {
  await withApi(
    (_req, res) => {
      res.writeHead(200, { 'content-type': 'application/pdf' });
      res.end(Buffer.from([0x25, 0x50, 0x44, 0x46, 0x00]));
    },
    async ({ dir }) => {
      const r = await runCli(['get', '/receiving/1/pallet-label'], { dir });
      assert.equal(r.code, 0);
      assert.equal(r.stdout, '<application/pdf body, 5 bytes, not shown>\n');
    },
  );
});

// Review Focus 3
test('a 204 prints nothing to stdout and exits 0', async () => {
  await withApi(
    (_req, res) => {
      res.writeHead(204);
      res.end();
    },
    async ({ dir }) => {
      const r = await runCli(['delete', '/projects/1'], { dir });
      assert.equal(r.code, 0);
      assert.equal(r.stdout, '');
      assert.match(r.stderr, /^204 DELETE/);
    },
  );
});

// Review Focus 4
test('a large response is printed whole and logged truncated', async () => {
  const big = { rows: 'x'.repeat(100_000) };
  await withApi((_req, res) => json(res, 200, big), async ({ dir }) => {
    const r = await runCli(['get', '/projects'], { dir });
    assert.deepEqual(JSON.parse(r.stdout), big);
    const [entry] = await readLogs(dir);
    assert.equal(entry.responseTruncated, true);
  });
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/request.test.ts'`
Expected: FAIL with `Cannot find module …/cli.ts`.

- [ ] **Step 4: Implement `cli.ts`**

`skills/emws-api/scripts/lib/cli.ts`:

```ts
/** Parses arguments, runs one command, prints per the output contract, and logs HTTP calls. */
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { resolveAuth } from './auth.ts';
import { configDir, loadContext } from './config.ts';
import { buildKql, classifyStatus, CliError, detectSource, exitCodeFor, hintFor, messageOf, renderError } from './errors.ts';
import type { ErrorEnvelope, RequestInfo, ResponseInfo, SuccessEnvelope } from './errors.ts';
import { isWrite, newCallId, newTraceparent, parseBody, sendRequest, traceIdOf } from './http.ts';
import { appendLog, gitBranch, parseDuration, pruneLogs, truncateBody } from './log.ts';
import { createRedactor } from './redact.ts';
import type { Redactor } from './redact.ts';
import { loadSpec, nearestRoutes, pathKnown } from './spec.ts';

export type Io = {
  stdout: (s: string) => void;
  stderr: (s: string) => void;
  cwd: string;
  env: Record<string, string | undefined>;
  stdin: () => Promise<string>;
  fetchImpl?: typeof fetch;
  now?: () => number;
};

type Flags = {
  profile?: string;
  query?: string[];
  body?: string;
  header?: string[];
  timeout?: string;
  json?: boolean;
  verbose?: boolean;
  refresh?: boolean;
  errors?: boolean;
  last?: string;
  since?: string;
  path?: string;
  here?: boolean;
  help?: boolean;
};

type CallMeta = { callId: string; method: string; path: string; profile: string; env: string; requestBody: string | null; responseBody: string | null };

const OPTIONS = {
  profile: { type: 'string', short: 'p' },
  query: { type: 'string', multiple: true },
  body: { type: 'string' },
  header: { type: 'string', multiple: true },
  timeout: { type: 'string' },
  json: { type: 'boolean' },
  verbose: { type: 'boolean', short: 'v' },
  refresh: { type: 'boolean' },
  errors: { type: 'boolean' },
  last: { type: 'string' },
  since: { type: 'string' },
  path: { type: 'string' },
  here: { type: 'boolean' },
  help: { type: 'boolean', short: 'h' },
} as const;

const HTTP_METHODS = new Set(['get', 'post', 'put', 'patch', 'delete']);
const BODY_PREVIEW_BYTES = 2048;
const RAW_BODY_BYTES = 4096;
const DEFAULT_TIMEOUT_MS = 30_000;

export const USAGE_TEXT = `emws: call the e-manage|ONE API (EMWS) through APIM with a configured profile.

Usage:
  emws <get|post|put|patch|delete> <path> [-p profile] [--query k=v ...]
       [--body @file | --body - | --body '<json>'] [--header K:V ...] [--timeout 30s] [--json] [-v]
  emws routes [filter] [-p profile] [--refresh] [--json]
  emws describe <METHOD> <path> [-p profile] [--refresh]
  emws whoami [-p profile]
  emws profiles [--json]
  emws log [<callId>] [--errors] [--last N] [--since 15m] [--profile P] [--path text] [--here] [--json]

Paths are API routes relative to the environment, such as /projects/123, without a host or /api prefix.
Configuration: ~/.config/emws/profiles.json and ~/.config/emws/.env (mode 600).
Exit codes: 0 ok, 2 usage or config, 3 auth, 4 blocked write, 5 client error, 6 server error, 7 network or timeout.
`;

const usageError = (message: string): CliError => new CliError('config', 'USAGE', message, 'Run `emws --help` for usage.');

function clip(text: string, maxBytes: number): string {
  const bytes = Buffer.from(text, 'utf8');
  return bytes.length <= maxBytes ? text : `${bytes.subarray(0, maxBytes).toString('utf8')}…[truncated]`;
}

function formatBody(body: unknown): string {
  if (body === null || body === undefined || body === '') return '';
  const text = typeof body === 'string' ? body : JSON.stringify(body, null, 2);
  return text.endsWith('\n') ? text : `${text}\n`;
}

/** Masks a request or response body, field-aware when it is JSON. */
function redactBody(redactor: Redactor, text: string | null): string | null {
  if (text === null) return null;
  try {
    return JSON.stringify(redactor.value(JSON.parse(text)));
  } catch {
    return redactor.text(text);
  }
}

function errorEnvelope(
  err: CliError,
  c: { request: RequestInfo | null; callId?: string; traceparent?: string; timingMs: number },
): ErrorEnvelope {
  return {
    ok: false,
    error: { kind: err.kind, code: err.code, message: err.message, hint: err.hint ?? hintFor({ kind: err.kind, code: err.code, profile: c.request?.profile }) },
    request: c.request,
    response: err.response ?? null,
    correlation: c.callId && c.traceparent ? { callId: c.callId, traceparent: c.traceparent } : null,
    timingMs: c.timingMs,
  };
}

/** Reports a failure of a command that makes no logged HTTP call. */
function fail(err: unknown, io: Io, flags: Flags): number {
  if (!(err instanceof CliError)) throw err;
  const env = errorEnvelope(err, { request: null, timingMs: 0 });
  if (flags.json) io.stdout(`${JSON.stringify(env, null, 2)}\n`);
  else io.stderr(renderError(env));
  return exitCodeFor(err.kind);
}

function buildUrl(apiBase: string, p: string, query: string[]): string {
  const url = new URL(`${apiBase.replace(/\/+$/, '')}${p}`);
  for (const pair of query) {
    const i = pair.indexOf('=');
    if (i <= 0) throw usageError(`--query expects key=value, got "${pair}"`);
    url.searchParams.append(pair.slice(0, i), pair.slice(i + 1));
  }
  return url.toString();
}

function parseHeaders(pairs: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const pair of pairs) {
    const i = pair.indexOf(':');
    if (i <= 0) throw usageError(`--header expects Name:value, got "${pair}"`);
    out[pair.slice(0, i).trim().toLowerCase()] = pair.slice(i + 1).trim();
  }
  return out;
}

async function readBody(spec: string | undefined, io: Io): Promise<string | undefined> {
  if (spec === undefined) return undefined;
  let text: string;
  if (spec === '-') {
    text = await io.stdin();
  } else if (spec.startsWith('@')) {
    const file = path.resolve(io.cwd, spec.slice(1));
    try {
      text = await readFile(file, 'utf8');
    } catch {
      throw usageError(`cannot read the body file ${file}`);
    }
  } else {
    text = spec;
  }
  try {
    JSON.parse(text);
  } catch (err) {
    throw usageError(`the request body is not valid JSON: ${(err as Error).message}`);
  }
  return text;
}

function checkPath(p: string | undefined): string {
  if (!p) throw usageError('a path is required, for example: emws get /projects/123');
  if (/^https?:\/\//i.test(p)) throw usageError('paths are relative to the environment; pass /projects/123, not a full URL');
  if (!p.startsWith('/')) throw usageError('the path must start with "/", for example: /projects/123');
  if (/^\/api(\/|$)/i.test(p)) throw usageError('drop the /api prefix; paths are relative to the environment, for example: /projects/123');
  return p;
}

const headerLines = (prefix: string, h: Record<string, string>): string =>
  Object.entries(h)
    .map(([k, v]) => `${prefix} ${k}: ${v}\n`)
    .join('');

/** Prints the envelope per the output contract, appends the redacted log line, and returns the exit code. */
async function finish(envelope: ErrorEnvelope | SuccessEnvelope, meta: CallMeta, io: Io, flags: Flags, redactor: Redactor, dir: string): Promise<number> {
  const safe = redactor.value(envelope);
  if (flags.json) {
    io.stdout(`${JSON.stringify(safe, null, 2)}\n`);
  } else if (safe.ok) {
    io.stdout(formatBody(safe.body));
    io.stderr(`${safe.status} ${meta.method} ${meta.path} · ${safe.timingMs}ms · ${safe.profile} · call ${safe.callId}\n`);
  } else {
    io.stderr(renderError(safe));
  }
  const response = truncateBody(redactBody(redactor, meta.responseBody));
  try {
    await appendLog(dir, {
      ts: new Date((io.now ?? Date.now)()).toISOString(),
      callId: meta.callId,
      ok: safe.ok,
      status: safe.ok ? safe.status : (safe.response?.status ?? null),
      code: safe.ok ? null : safe.error.code,
      method: meta.method,
      path: meta.path,
      profile: meta.profile,
      env: meta.env,
      timingMs: safe.timingMs,
      cwd: io.cwd,
      branch: gitBranch(io.cwd),
      agent: io.env.EMWS_AGENT ?? io.env.CLAUDE_SESSION_ID ?? null,
      envelope: safe.ok ? { ...safe, body: undefined } : safe,
      requestBody: redactBody(redactor, meta.requestBody),
      responseBody: response.body,
      responseTruncated: response.truncated,
    });
  } catch (err) {
    io.stderr(`warning: could not write the call log: ${(err as Error).message}\n`);
  }
  return safe.ok ? 0 : exitCodeFor(safe.error.kind);
}

async function runRequest(method: string, rawPath: string | undefined, flags: Flags, io: Io, dir: string): Promise<number> {
  const now = io.now ?? Date.now;
  const started = now();
  const callId = newCallId();
  const traceparent = newTraceparent();
  const redactor = createRedactor();
  const meta: CallMeta = { callId, method, path: rawPath ?? '', profile: flags.profile ?? io.env.EMWS_PROFILE ?? '?', env: '?', requestBody: null, responseBody: null };
  let request: RequestInfo | null = null;
  try {
    const p = checkPath(rawPath);
    const ctx = await loadContext({ dir, profile: flags.profile, env: io.env });
    for (const v of Object.values(ctx.secrets)) redactor.add(v);
    meta.profile = ctx.profileName;
    meta.env = ctx.envName;
    const url = buildUrl(ctx.environment.apiBase, p, flags.query ?? []);
    const body = await readBody(flags.body, io);
    meta.requestBody = body ?? null;
    const timeoutMs = flags.timeout ? parseDuration(flags.timeout) : DEFAULT_TIMEOUT_MS;
    const baseHeaders: Record<string, string> = {
      accept: 'application/json',
      'user-agent': 'emws-cli/0.1',
      traceparent,
      'x-emws-cli-call-id': callId,
      ...(body !== undefined ? { 'content-type': 'application/json' } : {}),
      ...parseHeaders(flags.header ?? []),
    };
    request = { method, url, profile: ctx.profileName, env: ctx.envName, headers: baseHeaders, bodyPreview: body === undefined ? null : clip(body, BODY_PREVIEW_BYTES) };

    if (isWrite(method) && !ctx.allowWrites) {
      throw new CliError('blocked', 'WRITE_BLOCKED', `${method} is blocked: profile "${ctx.profileName}" on environment "${ctx.envName}" does not allow writes`);
    }
    if (isWrite(method) && ctx.envName === 'prod') io.stderr(`warning: ${method} against production through profile "${ctx.profileName}"\n`);

    let auth = await resolveAuth(ctx, { fetchImpl: io.fetchImpl, now: io.now });
    redactor.add(auth.token);
    const send = () => sendRequest({ method, url, headers: { ...baseHeaders, ...auth.headers }, body, timeoutMs, fetchImpl: io.fetchImpl });
    let res = await send();
    if (res.status === 401 && ctx.profile.auth === 'basic') {
      auth = await resolveAuth(ctx, { fetchImpl: io.fetchImpl, now: io.now, forceLogin: true });
      redactor.add(auth.token);
      res = await send();
    }
    request.headers = { ...baseHeaders, ...auth.headers };
    if (flags.verbose) {
      io.stderr(`> ${method} ${url}\n${headerLines('>', redactor.value(request.headers))}< ${res.status}\n${headerLines('<', redactor.value(res.headers))}`);
    }
    meta.responseBody = res.bodyText;
    const parsed = parseBody(res.bodyText, res.contentType);
    const timingMs = now() - started;

    if (res.status >= 200 && res.status < 300) {
      const ok: SuccessEnvelope = { ok: true, status: res.status, headers: res.headers, body: parsed, timingMs, callId, profile: ctx.profileName, env: ctx.envName };
      return await finish(ok, meta, io, flags, redactor, dir);
    }

    const { kind, code } = classifyStatus(res.status);
    const source = detectSource(parsed);
    let routeKnown: boolean | undefined;
    let suggestions: string[] | undefined;
    if (res.status === 404) {
      try {
        const doc = await loadSpec({ dir, envName: ctx.envName, specUrl: ctx.environment.specUrl, fetchImpl: io.fetchImpl, now: io.now });
        routeKnown = pathKnown(doc, p);
        if (!routeKnown) suggestions = nearestRoutes(doc, method, p);
      } catch {
        // The spec only improves the hint; its absence is reported by the hint itself.
      }
    }
    const correlationId = res.headers['x-correlation-id'];
    const response: ResponseInfo = { status: res.status, headers: res.headers, body: source === 'unknown' ? clip(res.bodyText, RAW_BODY_BYTES) : parsed, source };
    const failed: ErrorEnvelope = {
      ok: false,
      error: {
        kind,
        code,
        message: messageOf(parsed, res.status),
        hint: hintFor({ kind, code, status: res.status, source, path: p, routeKnown, suggestions, profile: ctx.profileName }),
      },
      request,
      response,
      correlation: { callId, traceparent, correlationId, kql: buildKql({ traceId: traceIdOf(traceparent), correlationId }) },
      timingMs,
    };
    return await finish(failed, meta, io, flags, redactor, dir);
  } catch (err) {
    if (!(err instanceof CliError)) throw err;
    return finish(errorEnvelope(err, { request, callId, traceparent, timingMs: now() - started }), meta, io, flags, redactor, dir);
  }
}

/** Runs one CLI invocation and returns its exit code. */
export async function main(argv: string[], io: Io): Promise<number> {
  const dir = configDir(io.env);
  await pruneLogs(dir, (io.now ?? Date.now)()).catch(() => {});
  let positionals: string[];
  let flags: Flags;
  try {
    const parsed = parseArgs({ args: argv, options: OPTIONS, allowPositionals: true, strict: true });
    positionals = parsed.positionals;
    flags = parsed.values as Flags;
  } catch (err) {
    return fail(usageError((err as Error).message), io, { json: argv.includes('--json') });
  }
  const [command, ...rest] = positionals;
  if (command === undefined || command === 'help' || flags.help) {
    io.stdout(USAGE_TEXT);
    return 0;
  }
  if (HTTP_METHODS.has(command.toLowerCase())) return runRequest(command.toUpperCase(), rest[0], flags, io, dir);
  return fail(usageError(`unknown command "${command}"`), io, flags);
}
```

- [ ] **Step 5: Implement the entry point**

`skills/emws-api/scripts/emws.ts`:

```ts
#!/usr/bin/env node
/** Entry point for the emws CLI; all behaviour lives in lib/cli.ts. */
import { main } from './lib/cli.ts';

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString('utf8');
}

try {
  process.exitCode = await main(process.argv.slice(2), {
    stdout: (s) => process.stdout.write(s),
    stderr: (s) => process.stderr.write(s),
    cwd: process.cwd(),
    env: process.env,
    stdin: readStdin,
  });
} catch (err) {
  process.stderr.write(`emws internal error (a bug in the CLI, not an API failure):\n${(err as Error).stack ?? String(err)}\n`);
  process.exitCode = 1;
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `node --test 'tests/emws-api/*.test.ts'`
Expected: PASS, every test in all files.

- [ ] **Step 7: Smoke the entry point**

Run: `node skills/emws-api/scripts/emws.ts --help; echo "exit $?"`
Expected: the usage text, then `exit 0`.

Run: `EMWS_CONFIG_DIR=/nonexistent node skills/emws-api/scripts/emws.ts get /projects/1; echo "exit $?"`
Expected: `error config CONFIG_INVALID …profiles.json not found`, then `exit 2`.

- [ ] **Step 8: Commit**

```bash
git add skills/emws-api/scripts/lib/cli.ts skills/emws-api/scripts/emws.ts tests/emws-api/helpers.ts tests/emws-api/request.test.ts
git commit -m "feat(emws-api): add the request command and output contract"
```

---

### Task 9: Discovery, identity, profiles, and log commands

**Files:**
- Modify: `skills/emws-api/scripts/lib/cli.ts` (imports and the dispatch in `main`; new `runRoutes`, `runDescribe`, `runWhoami`, `runProfiles`, `runLog`)
- Test: `tests/emws-api/commands.test.ts`

**Interfaces:**
- Consumes: `loadConfig`, `loadSecrets`, `resolveProfileName`, `effectiveAllowWrites`, `loadContext` (Task 3); `resolveAuth`, `exchangeApiKey` (Task 5); `readLogs`, `queryLogs`, `formatSummary`, `parseDuration` (Task 6); `loadSpec`, `listRoutes`, `describeRoute`, `nearestRoutes` (Task 7); `fail`, `usageError`, `Flags`, `Io` (Task 8).
- Produces: the `routes`, `describe`, `whoami`, `profiles`, and `log` commands. They print but do not write call-log entries.

- [ ] **Step 1: Write the failing test**

`tests/emws-api/commands.test.ts`:

```ts
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import type { ServerResponse } from 'node:http';
import type { Recorded } from './helpers.ts';
import { json, makeConfigDir, makeJwt, runCli, SECRETS, startServer, testConfig } from './helpers.ts';

const spec = JSON.parse(await readFile(new URL('./fixtures/spec.json', import.meta.url), 'utf8'));

async function withApi(handler: (req: Recorded, res: ServerResponse) => void, fn: (h: { dir: string; requests: Recorded[] }) => Promise<void>, secrets = SECRETS) {
  const srv = await startServer((req, res) => (req.url === '/spec.json' ? json(res, 200, spec) : handler(req, res)));
  const dir = await makeConfigDir(testConfig(srv.url), secrets);
  try {
    await fn({ dir, requests: srv.requests });
  } finally {
    await srv.close();
  }
}

const token = (claims: Record<string, unknown>) => makeJwt({ exp: Math.floor(Date.now() / 1000) + 3600, ...claims });
const text = (res: ServerResponse, body: string) => {
  res.writeHead(200, { 'content-type': 'text/plain' });
  res.end(body);
};

test('routes lists and filters', async () => {
  await withApi((_req, res) => json(res, 404, {}), async ({ dir }) => {
    const all = await runCli(['routes'], { dir });
    assert.equal(all.code, 0);
    assert.match(all.stdout, /GET\s+\/projects\/\{id\}\s+Get project/);
    const some = await runCli(['routes', 'receiving', '--json'], { dir });
    assert.deepEqual(JSON.parse(some.stdout), [{ method: 'GET', path: '/receiving/{poId}/lines', summary: 'List receiving lines' }]);
  });
});

test('describe prints a resolved route; an unknown route is ROUTE_UNKNOWN with suggestions', async () => {
  await withApi((_req, res) => json(res, 404, {}), async ({ dir }) => {
    const ok = await runCli(['describe', 'GET', '/projects/{id}'], { dir });
    assert.equal(ok.code, 0);
    assert.equal(JSON.parse(ok.stdout).responses['200'].content['application/json'].schema.properties.name.type, 'string');
    const bad = await runCli(['describe', 'GET', '/project/1', '--json'], { dir });
    assert.equal(bad.code, 5);
    const env = JSON.parse(bad.stdout);
    assert.equal(env.error.code, 'ROUTE_UNKNOWN');
    assert.match(env.error.hint, /GET \/projects\/\{id\}/);
  });
});

test('whoami on a basic profile shows the token claims and no password', async () => {
  await withApi((_req, res) => text(res, token({ sub: 'agent', database: 'TenantA', client_id: 'pocketmanage', name: 'Agent' })), async ({ dir }) => {
    const r = await runCli(['whoami', '-p', 'user'], { dir });
    assert.equal(r.code, 0);
    const out = JSON.parse(r.stdout);
    assert.equal(out.profile, 'user');
    assert.equal(out.env, 'staging');
    assert.equal(out.auth, 'basic');
    assert.equal(out.database, 'TenantA');
    assert.equal(out.sub, 'agent');
    assert.match(out.expires, /^\d{4}-\d{2}-\d{2}T/);
    assert.ok(!r.stdout.includes(SECRETS.USER_PW));
  });
});

test('whoami on an apiKey profile exchanges the key and shows the tenant', async () => {
  await withApi((_req, res) => text(res, token({ database: 'TenantB' })), async ({ dir, requests }) => {
    const r = await runCli(['whoami'], { dir });
    assert.equal(r.code, 0);
    assert.equal(JSON.parse(r.stdout).database, 'TenantB');
    assert.equal(requests[0].url, '/emws-staging/authenticateWithApiKey');
    assert.ok(!r.stdout.includes(SECRETS.STG_KEY));
  });
});

test('whoami reports a rejected key as LOGIN_FAILED', async () => {
  await withApi((_req, res) => json(res, 401, { statusCode: 401, message: 'Access denied' }), async ({ dir }) => {
    const r = await runCli(['whoami', '--json'], { dir });
    assert.equal(r.code, 3);
    assert.equal(JSON.parse(r.stdout).error.code, 'LOGIN_FAILED');
  });
});

test('profiles lists every profile with write mode and secret presence, never values', async () => {
  await withApi(
    (_req, res) => json(res, 200, {}),
    async ({ dir }) => {
      const r = await runCli(['profiles', '--json'], { dir });
      assert.equal(r.code, 0);
      const rows = JSON.parse(r.stdout) as { name: string; default: boolean; allowWrites: boolean; secret: string }[];
      const byName = Object.fromEntries(rows.map((x) => [x.name, x]));
      assert.equal(byName.stg.default, true);
      assert.equal(byName.prod.allowWrites, false);
      assert.equal(byName['prod-rw'].allowWrites, true);
      assert.equal(byName.stg.secret, 'set');
      assert.equal(byName.user.secret, 'missing');
      for (const v of Object.values(SECRETS)) assert.ok(!r.stdout.includes(v));
      const plain = await runCli(['profiles'], { dir });
      assert.match(plain.stdout, /^\* stg/m);
    },
    { STG_KEY: SECRETS.STG_KEY, PROD_KEY: SECRETS.PROD_KEY },
  );
});

test('log lists recent calls, filters errors, and shows one call in full', async () => {
  let n = 0;
  await withApi((_req, res) => (++n === 2 ? json(res, 500, { StatusCode: 500, Message: 'boom', ErrorCode: null }) : json(res, 200, { n })), async ({ dir }) => {
    await runCli(['get', '/projects/1'], { dir });
    await runCli(['get', '/projects/2'], { dir });
    const list = await runCli(['log'], { dir });
    assert.equal(list.code, 0);
    assert.equal(list.stdout.trim().split('\n').length, 2);
    const errors = await runCli(['log', '--errors', '--json'], { dir });
    const [failed] = errors.stdout.trim().split('\n').map((l) => JSON.parse(l));
    assert.equal(failed.path, '/projects/2');
    const one = await runCli(['log', failed.callId], { dir });
    assert.equal(JSON.parse(one.stdout).envelope.error.message, 'boom');
    const missing = await runCli(['log', 'c_deadbeef', '--json'], { dir });
    assert.equal(missing.code, 5);
    assert.equal(JSON.parse(missing.stdout).error.code, 'CALL_UNKNOWN');
  });
});

test('help prints usage; an unknown command is a usage error', async () => {
  const dir = await makeConfigDir(testConfig('http://127.0.0.1:9'));
  const help = await runCli(['--help'], { dir });
  assert.equal(help.code, 0);
  assert.match(help.stdout, /Usage:/);
  const bad = await runCli(['frobnicate'], { dir });
  assert.equal(bad.code, 2);
  assert.match(bad.stderr, /unknown command "frobnicate"/);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test 'tests/emws-api/commands.test.ts'`
Expected: FAIL; `routes` and the others report `unknown command`.

- [ ] **Step 3: Implement the commands**

In `skills/emws-api/scripts/lib/cli.ts`, replace these three import lines:

```ts
import { resolveAuth } from './auth.ts';
import { configDir, loadContext } from './config.ts';
import { appendLog, gitBranch, parseDuration, pruneLogs, truncateBody } from './log.ts';
import { loadSpec, nearestRoutes, pathKnown } from './spec.ts';
```

with:

```ts
import { exchangeApiKey, resolveAuth } from './auth.ts';
import { configDir, effectiveAllowWrites, loadConfig, loadContext, loadSecrets, resolveProfileName } from './config.ts';
import { appendLog, formatSummary, gitBranch, parseDuration, pruneLogs, queryLogs, readLogs, truncateBody } from './log.ts';
import { describeRoute, listRoutes, loadSpec, nearestRoutes, pathKnown } from './spec.ts';
```

Add these functions above `main`:

```ts
/** The selected profile's environment and its OpenAPI document; needs no secrets. */
async function specFor(flags: Flags, io: Io, dir: string) {
  const config = await loadConfig(dir);
  const envName = config.profiles[resolveProfileName(config, flags.profile, io.env)].env;
  return loadSpec({ dir, envName, specUrl: config.environments[envName].specUrl, refresh: flags.refresh, fetchImpl: io.fetchImpl, now: io.now });
}

async function runRoutes(filter: string | undefined, flags: Flags, io: Io, dir: string): Promise<number> {
  try {
    const routes = listRoutes(await specFor(flags, io, dir), filter);
    if (flags.json) {
      io.stdout(`${JSON.stringify(routes, null, 2)}\n`);
    } else {
      for (const r of routes) io.stdout(`${r.method.padEnd(6)} ${r.path}${r.summary ? `  ${r.summary}` : ''}\n`);
      if (routes.length === 0) io.stderr(`no routes match "${filter}"\n`);
    }
    return 0;
  } catch (err) {
    return fail(err, io, flags);
  }
}

async function runDescribe(method: string | undefined, p: string | undefined, flags: Flags, io: Io, dir: string): Promise<number> {
  try {
    if (!method || !p) throw usageError('describe needs a method and a path, for example: emws describe GET /projects/{id}');
    const doc = await specFor(flags, io, dir);
    const route = describeRoute(doc, method, p);
    if (!route) {
      throw new CliError('client', 'ROUTE_UNKNOWN', `no ${method.toUpperCase()} route matches ${p}`, `Closest routes: ${nearestRoutes(doc, method, p).join(', ')}`);
    }
    io.stdout(`${JSON.stringify(route, null, 2)}\n`);
    return 0;
  } catch (err) {
    return fail(err, io, flags);
  }
}

async function runWhoami(flags: Flags, io: Io, dir: string): Promise<number> {
  try {
    const ctx = await loadContext({ dir, profile: flags.profile, env: io.env });
    const claims =
      ctx.profile.auth === 'basic'
        ? (await resolveAuth(ctx, { fetchImpl: io.fetchImpl, now: io.now })).claims ?? {}
        : (await exchangeApiKey(ctx, { fetchImpl: io.fetchImpl })).claims;
    const out = {
      profile: ctx.profileName,
      env: ctx.envName,
      apiBase: ctx.environment.apiBase,
      auth: ctx.profile.auth,
      allowWrites: ctx.allowWrites,
      sub: claims.sub ?? null,
      name: claims.name ?? null,
      database: claims.database ?? null,
      clientId: claims.client_id ?? null,
      expires: typeof claims.exp === 'number' ? new Date(claims.exp * 1000).toISOString() : null,
    };
    io.stdout(`${JSON.stringify(out, null, 2)}\n`);
    return 0;
  } catch (err) {
    return fail(err, io, flags);
  }
}

async function runProfiles(flags: Flags, io: Io, dir: string): Promise<number> {
  try {
    const config = await loadConfig(dir);
    const secrets = await loadSecrets(dir);
    const selected = io.env.EMWS_PROFILE || config.defaultProfile;
    const rows = Object.entries(config.profiles).map(([name, p]) => ({
      name,
      default: name === selected,
      env: p.env,
      auth: p.auth,
      allowWrites: effectiveAllowWrites(p),
      secret: secrets[p.secret] ? 'set' : 'missing',
      apiBase: config.environments[p.env].apiBase,
    }));
    if (flags.json) {
      io.stdout(`${JSON.stringify(rows, null, 2)}\n`);
    } else {
      const width = Math.max(...rows.map((r) => r.name.length));
      for (const r of rows) {
        io.stdout(`${r.default ? '*' : ' '} ${r.name.padEnd(width)}  ${r.env.padEnd(8)} ${r.auth.padEnd(6)} writes:${r.allowWrites ? 'on ' : 'off'}  secret:${r.secret.padEnd(7)}  ${r.apiBase}\n`);
      }
    }
    return 0;
  } catch (err) {
    return fail(err, io, flags);
  }
}

async function runLog(callId: string | undefined, flags: Flags, io: Io, dir: string): Promise<number> {
  try {
    const last = flags.last === undefined ? undefined : Number(flags.last);
    if (last !== undefined && (!Number.isInteger(last) || last < 1)) throw usageError(`--last expects a positive whole number, got "${flags.last}"`);
    const found = queryLogs(await readLogs(dir), {
      callId,
      errors: flags.errors,
      last,
      sinceMs: flags.since ? parseDuration(flags.since) : undefined,
      profile: flags.profile,
      path: flags.path,
      cwd: flags.here ? io.cwd : undefined,
      now: (io.now ?? Date.now)(),
    });
    if (callId) {
      if (found.length === 0) throw new CliError('client', 'CALL_UNKNOWN', `no logged call ${callId}`, 'Run `emws log` to list recent calls; logs are kept 14 days.');
      io.stdout(`${JSON.stringify(found[0], null, 2)}\n`);
      return 0;
    }
    for (const e of found) io.stdout(`${flags.json ? JSON.stringify(e) : formatSummary(e)}\n`);
    return 0;
  } catch (err) {
    return fail(err, io, flags);
  }
}
```

In `main`, replace:

```ts
  if (HTTP_METHODS.has(command.toLowerCase())) return runRequest(command.toUpperCase(), rest[0], flags, io, dir);
  return fail(usageError(`unknown command "${command}"`), io, flags);
```

with:

```ts
  if (HTTP_METHODS.has(command.toLowerCase())) return runRequest(command.toUpperCase(), rest[0], flags, io, dir);
  switch (command) {
    case 'routes':
      return runRoutes(rest[0], flags, io, dir);
    case 'describe':
      return runDescribe(rest[0], rest[1], flags, io, dir);
    case 'whoami':
      return runWhoami(flags, io, dir);
    case 'profiles':
      return runProfiles(flags, io, dir);
    case 'log':
      return runLog(rest[0], flags, io, dir);
    default:
      return fail(usageError(`unknown command "${command}"`), io, flags);
  }
```

- [ ] **Step 4: Run all tests**

Run: `node --test 'tests/emws-api/*.test.ts'`
Expected: PASS, every test in all files.

- [ ] **Step 5: Commit**

```bash
git add skills/emws-api/scripts/lib/cli.ts tests/emws-api/commands.test.ts
git commit -m "feat(emws-api): add routes, describe, whoami, profiles, and log commands"
```

---

### Task 10: Skill documentation, example config, and the staging smoke run

**Files:**
- Create: `skills/emws-api/SKILL.md`
- Create: `skills/emws-api/agents/openai.yaml`
- Create: `skills/emws-api/references/profiles.example.json`
- Modify: `README.md` (the Skills table)

**Interfaces:**
- Consumes: the finished CLI.
- Produces: the installable skill.

- [ ] **Step 1: Write the example configuration**

`skills/emws-api/references/profiles.example.json`:

```json
{
  "environments": {
    "prod": {
      "apiBase": "https://emanageone.azure-api.net/emws",
      "specUrl": "https://emws-data.azurewebsites.net/api/swagger.json"
    },
    "staging": {
      "apiBase": "https://emanageone.azure-api.net/emws-staging",
      "specUrl": "https://emws-data-staging.azurewebsites.net/api/swagger.json"
    },
    "local": {
      "apiBase": "http://localhost:7071/api",
      "specUrl": "http://localhost:7071/api/swagger.json"
    }
  },
  "profiles": {
    "staging-admin": { "env": "staging", "auth": "apiKey", "secret": "STAGING_ADMIN_KEY" },
    "staging-user": {
      "env": "staging",
      "auth": "basic",
      "username": "user@example.com",
      "secret": "STAGING_USER_PASSWORD",
      "clientId": "<client id>",
      "dbId": "<database id>"
    },
    "prod-admin": { "env": "prod", "auth": "apiKey", "secret": "PROD_ADMIN_KEY" },
    "local": { "env": "local", "auth": "apiKey", "secret": "LOCAL_KEY" }
  },
  "defaultProfile": "staging-admin"
}
```

- [ ] **Step 2: Write SKILL.md**

`skills/emws-api/SKILL.md`:

````markdown
---
name: emws-api
description: "Use when you need to call, test, or verify the e-manage|ONE API (EMWS) on staging or production: checking an endpoint's response, reproducing an API error, or confirming a deployed change."
---

# EMWS API

`emws` calls the e-manage|ONE API through APIM with credentials already configured on this
machine. Never ask the user for keys, passwords, or tokens, and never read
`~/.config/emws/.env` or `~/.config/emws/tokens/`.

Requires Node 24 or newer. Run it as:

```bash
node ~/.agents/skills/emws-api/scripts/emws.ts <command> ...
```

Use `emws` instead when that alias exists. The examples below use `emws`.

## Start here

```bash
emws profiles            # profiles, their environment, write mode, and whether the secret is set
emws whoami -p <name>    # proves the credentials work and shows the tenant
```

If no profile works, stop and tell the user that setup is needed: `~/.config/emws/profiles.json`
and `~/.config/emws/.env` (mode 600), with the layout in `references/profiles.example.json`.
Do not create or edit those files yourself.

## Calling the API

```bash
emws routes receiving                          # search the route list
emws describe GET /projects/{id}               # parameters and schemas for one route
emws get /projects/123 -p staging-admin
emws get /projects --query start=0 --query limit=20
emws post /things --body @payload.json -p staging-admin
```

- Paths are relative to the environment. Never add a host or an `/api` prefix.
- Default to a staging profile. Use production only to read, and only when the task needs it.
- stdout is the response body; one summary line goes to stderr. `--json` prints one envelope
  with status, headers, body, and call id instead.

## When a call fails

| Exit | Meaning |
| --- | --- |
| 2 | Usage or configuration |
| 3 | Authentication |
| 4 | Write blocked by the profile |
| 5 | Client error (4xx) |
| 6 | Server error (5xx, or 422 for an unhandled exception) |
| 7 | Network or timeout |

Read `error.message` and `error.hint` first. `response.source` says who rejected the call:
`apim` is the gateway (usually credentials), `api` is EMWS itself. For a server error, put
`correlation.correlationId` and `correlation.kql` in your report so the failure can be found
in App Insights.

```bash
emws log --errors          # recent failures, from every agent on this machine
emws log <callId>          # one call in full: request, response, timing, correlation
emws log --here --last 5   # only calls made from this working directory
```

## Rules

- A `WRITE_BLOCKED` result is the answer, not an obstacle. Do not switch profiles or edit the
  configuration to get around it; ask the user.
- Staging runs what has merged to the `staging` branch and deployed. Your unmerged change is
  not there yet. To verify unmerged work, run the Functions host locally and use a profile on
  the `local` environment.
- Report what you verified with the call id, so the user can inspect it with `emws log`.
````

- [ ] **Step 3: Write the agent metadata**

`skills/emws-api/agents/openai.yaml`:

```yaml
interface:
  display_name: "EMWS API"
  short_description: "Call and verify the e-manage|ONE API"
  default_prompt: "Use $emws-api to verify the change against the staging API."
```

- [ ] **Step 4: Add the README row**

In `README.md`, add this row to the Skills table, after the `deploy-mobile-apps` row:

```markdown
| [emws-api](skills/emws-api/) | You need to call or verify the e-manage\|ONE API on staging or production with locally configured credentials, structured errors, and a call log. |
```

- [ ] **Step 5: Run the full test suite**

Run: `node --test 'tests/emws-api/*.test.ts'`
Expected: PASS, every test in all files.

- [ ] **Step 6: Check the package for secrets and stray files**

Run: `git status --short && grep -rnE 'stg-secret|prod-secret|hunter2' skills/ || echo "no test secrets in skills/"`
Expected: only the files of this task as new or modified, and `no test secrets in skills/`.

- [ ] **Step 7: Commit**

```bash
git add skills/emws-api/SKILL.md skills/emws-api/agents/openai.yaml skills/emws-api/references/profiles.example.json README.md
git commit -m "docs(emws-api): add the skill guide and example configuration"
```

- [ ] **Step 8: Staging smoke run (manual, needs the user's configuration)**

If `~/.config/emws/profiles.json` does not exist, skip this step and report the smoke run as
not done: the user has to create the configuration first. Otherwise, from the worktree root:

```bash
node skills/emws-api/scripts/emws.ts profiles
node skills/emws-api/scripts/emws.ts whoami -p <a staging profile>
node skills/emws-api/scripts/emws.ts routes project -p <a staging profile>
node skills/emws-api/scripts/emws.ts get <a GET route from the list> -p <a staging profile>
node skills/emws-api/scripts/emws.ts get /projectz/1 -p <a staging profile> --json
node skills/emws-api/scripts/emws.ts log --last 3
```

Record, separately from the automated tests:
- whether `whoami` returned a `database` claim;
- whether the Function App spec paths carry an `/api` prefix (either form works; note which);
- whether the deliberate 404 was classed `api` or `apim`, and whether its hint listed routes;
- whether the real EMWS error body uses `StatusCode` or `statusCode` casing.

Fix anything that contradicts the design as a new commit with a test, and report the rest.
