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

test('an APIM 401 on an API-key profile blames the key, not a token', () => {
  const hint = hintFor({ kind: 'auth', code: 'HTTP_401', status: 401, source: 'apim', auth: 'apiKey' });
  assert.match(hint, /API key/);
  assert.doesNotMatch(hint, /issuer/);
});

test('the post-deploy warm-up 500 says to retry, not to report a regression', () => {
  const hint = hintFor({ kind: 'server', code: 'HTTP_500', status: 500, message: 'Request recording is temporarily unavailable' });
  assert.match(hint, /warming up after a deploy/);
});
