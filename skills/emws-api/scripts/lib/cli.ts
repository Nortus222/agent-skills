/** Parses arguments, runs one command, prints per the output contract, and logs HTTP calls. */
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { exchangeApiKey, resolveAuth } from './auth.ts';
import { configDir, effectiveAllowWrites, loadConfig, loadContext, loadSecrets, resolveProfileName } from './config.ts';
import { buildKql, classifyStatus, CliError, detectSource, exitCodeFor, hintFor, messageOf, renderError } from './errors.ts';
import type { ErrorEnvelope, RequestInfo, ResponseInfo, SuccessEnvelope } from './errors.ts';
import { isWrite, newCallId, newTraceparent, parseBody, sendRequest, traceIdOf } from './http.ts';
import { appendLog, formatSummary, gitBranch, parseDuration, pruneLogs, queryLogs, readLogs, truncateBody } from './log.ts';
import { createRedactor } from './redact.ts';
import type { Redactor } from './redact.ts';
import { describeRoute, listRoutes, loadSpec, nearestRoutes, pathKnown } from './spec.ts';

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
function fail(err: unknown, io: Io, flags: Flags, redactor: Redactor = createRedactor()): number {
  if (!(err instanceof CliError)) throw err;
  const env = redactor.value(errorEnvelope(err, { request: null, timingMs: 0 }));
  if (flags.json) io.stdout(`${JSON.stringify(env, null, 2)}\n`);
  else io.stderr(renderError(env));
  return exitCodeFor(err.kind);
}

function buildUrl(apiBase: string, p: string, query: string[]): string {
  const base = apiBase.replace(/\/+$/, '');
  const url = new URL(`${base}${p}`);
  if (!url.href.startsWith(`${new URL(base).href.replace(/\/+$/, '')}/`)) {
    throw usageError(`the path resolves outside the environment's apiBase (${base})`);
  }
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
  const dotSegment = p
    .split(/[?#]/)[0]
    .split('/')
    .some((s) => {
      let decoded = s;
      try {
        decoded = decodeURIComponent(s);
      } catch {
        // A malformed escape is left for the server to reject.
      }
      return decoded === '.' || decoded === '..';
    });
  if (dotSegment) throw usageError('dot segments ("." or "..") are not allowed in paths; they could leave the environment');
  return p;
}

const headerLines = (prefix: string, h: Record<string, string>): string =>
  Object.entries(h)
    .map(([k, v]) => `${prefix} ${k}: ${v}\n`)
    .join('');

/** Keeps a logged error envelope within the log's body cap; the parsed body becomes a truncated JSON string. */
function capErrorEnvelope(env: ErrorEnvelope): ErrorEnvelope {
  if (!env.response) return env;
  const serialized = JSON.stringify(env.response.body) ?? '';
  const capped = truncateBody(serialized);
  if (!capped.truncated) return env;
  return { ...env, response: { ...env.response, body: `${capped.body}…[truncated]` } };
}

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
      envelope: safe.ok ? { ...safe, body: undefined } : capErrorEnvelope(safe),
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
    if (body !== undefined && method === 'GET') throw usageError('GET requests cannot carry a body; pass filters with --query');
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
    const message = messageOf(parsed, res.status);
    const correlationId = res.headers['x-correlation-id'];
    const response: ResponseInfo = { status: res.status, headers: res.headers, body: source === 'unknown' ? clip(res.bodyText, RAW_BODY_BYTES) : parsed, source };
    const failed: ErrorEnvelope = {
      ok: false,
      error: {
        kind,
        code,
        message,
        hint: hintFor({ kind, code, status: res.status, source, path: p, routeKnown, suggestions, profile: ctx.profileName, auth: ctx.profile.auth, message }),
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
  const redactor = createRedactor();
  try {
    const ctx = await loadContext({ dir, profile: flags.profile, env: io.env });
    for (const v of Object.values(ctx.secrets)) redactor.add(v);
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
      expires: typeof claims.exp === 'number' ? new Date(claims.exp * 1000).toISOString() : 'none (the token has no exp claim)',
    };
    io.stdout(`${JSON.stringify(out, null, 2)}\n`);
    return 0;
  } catch (err) {
    return fail(err, io, flags, redactor);
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
}
