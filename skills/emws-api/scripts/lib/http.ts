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

type RawJson = { rawJSON: (text: string) => unknown };

/** Keeps integers beyond 2^53 exact, so re-serialised bodies show the IDs the API sent. */
function exactIntegers(_key: string, value: unknown, context?: { source?: string }): unknown {
  if (typeof value === 'number' && !Number.isSafeInteger(value) && /^-?\d+$/.test(context?.source ?? '')) {
    return (JSON as unknown as RawJson).rawJSON(context?.source ?? '');
  }
  return value;
}

/** JSON when it parses, the text otherwise, null when empty. */
export function parseBody(text: string, contentType: string): unknown {
  if (text === '') return null;
  if (/json/i.test(contentType) || /^\s*[[{]/.test(text)) {
    try {
      return JSON.parse(text, exactIntegers as (key: string, value: unknown) => unknown);
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
