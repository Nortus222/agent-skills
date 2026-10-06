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
