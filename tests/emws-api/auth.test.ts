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
