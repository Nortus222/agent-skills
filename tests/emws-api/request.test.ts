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

test('a path that climbs out of the environment is a usage error before any request', async () => {
  await withApi((_req, res) => json(res, 200, {}), async ({ dir, requests }) => {
    for (const p of ['/../emws/projects/1', '/%2e%2e/emws/projects/1', '/projects/./1']) {
      const r = await runCli(['delete', p, '--json'], { dir });
      assert.equal(r.code, 2, p);
      assert.match(JSON.parse(r.stdout).error.message, /dot segments/, p);
    }
    assert.equal(requests.length, 0);
  });
});

test('a GET with a body is a usage error, not a network failure', async () => {
  await withApi((_req, res) => json(res, 200, {}), async ({ dir, requests }) => {
    const r = await runCli(['get', '/projects', '--body', '{}', '--json'], { dir });
    assert.equal(r.code, 2);
    const env = JSON.parse(r.stdout);
    assert.equal(env.error.code, 'USAGE');
    assert.match(env.error.message, /GET.*body/);
    assert.equal(requests.length, 0);
  });
});

test('integers beyond 2^53 are printed exactly as the API sent them', async () => {
  await withApi(
    (_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{"id":9007199254740993,"ids":[12345678901234567890],"name":"x"}');
    },
    async ({ dir }) => {
      const plain = await runCli(['get', '/projects/1'], { dir });
      assert.match(plain.stdout, /"id": 9007199254740993/);
      assert.match(plain.stdout, /12345678901234567890/);
      const env = await runCli(['get', '/projects/1', '--json'], { dir });
      assert.match(env.stdout, /"id": 9007199254740993/);
    },
  );
});
