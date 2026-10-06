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
