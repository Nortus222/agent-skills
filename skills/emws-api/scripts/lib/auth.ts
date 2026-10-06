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
