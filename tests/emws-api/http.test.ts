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
