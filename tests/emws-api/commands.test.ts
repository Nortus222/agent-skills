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

test('whoami masks a secret echoed back by a failed login', async () => {
  await withApi((req, res) => json(res, 401, { statusCode: 401, message: `bad key ${req.headers['x-api-key']}` }), async ({ dir }) => {
    const r = await runCli(['whoami'], { dir });
    assert.equal(r.code, 3);
    assert.ok(!r.stderr.includes(SECRETS.STG_KEY), r.stderr);
    assert.match(r.stderr, /bad key \*\*\*/);
  });
});
