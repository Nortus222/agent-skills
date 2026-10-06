import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import {
  describeRoute,
  listRoutes,
  loadSpec,
  nearestRoutes,
  normalizePath,
  pathKnown,
  SPEC_TTL_MS,
} from '../../skills/emws-api/scripts/lib/spec.ts';
import type { OpenApiDoc } from '../../skills/emws-api/scripts/lib/spec.ts';
import { json, startServer } from './helpers.ts';

const doc = JSON.parse(await readFile(new URL('./fixtures/spec.json', import.meta.url), 'utf8')) as OpenApiDoc;

test('normalizes the /api prefix, query, and trailing slash', () => {
  assert.equal(normalizePath('/api/projects/1/?x=1'), '/projects/1');
  assert.equal(normalizePath('projects'), '/projects');
  assert.equal(normalizePath('/apiary/x'), '/apiary/x');
});

test('lists routes sorted, filtered case-insensitively', () => {
  assert.deepEqual(listRoutes(doc).map((r) => `${r.method} ${r.path}`), [
    'GET /management/keys',
    'GET /projects',
    'POST /projects',
    'GET /projects/{id}',
    'GET /receiving/{poId}/lines',
  ]);
  assert.deepEqual(listRoutes(doc, 'RECEIV').map((r) => r.summary), ['List receiving lines']);
});

test('knows whether a concrete path matches any route', () => {
  assert.equal(pathKnown(doc, '/projects/123'), true);
  assert.equal(pathKnown(doc, '/project/123'), false);
});

test('suggests the closest routes for a mistyped path', () => {
  assert.equal(nearestRoutes(doc, 'GET', '/project/123')[0], 'GET /projects/{id}');
  assert.equal(nearestRoutes(doc, 'GET', '/receving/9/lines')[0], 'GET /receiving/{poId}/lines');
  assert.equal(nearestRoutes(doc, 'GET', '/x').length, 3);
});

test('describes a route with references resolved and cycles cut', () => {
  const d = describeRoute(doc, 'get', '/projects/{id}') as Record<string, any>;
  assert.equal(d.method, 'GET');
  assert.equal(d.path, '/projects/{id}');
  assert.equal(d.parameters[0].name, 'id');
  const schema = d.responses['200'].content['application/json'].schema;
  assert.equal(schema.properties.name.type, 'string');
  assert.equal(schema.properties.parent.note, 'recursive reference not expanded');
  assert.equal(describeRoute(doc, 'DELETE', '/projects/1'), undefined);
});

test('caches the spec for ten minutes; --refresh refetches', async () => {
  const srv = await startServer((_req, res) => json(res, 200, doc));
  const dir = await mkdtemp(path.join(os.tmpdir(), 'emws-spec-'));
  const base = { dir, envName: 'staging', specUrl: `${srv.url}/spec.json` };
  try {
    await loadSpec({ ...base, now: () => 0 });
    await loadSpec({ ...base, now: () => SPEC_TTL_MS - 1 });
    assert.equal(srv.requests.length, 1);
    await loadSpec({ ...base, now: () => SPEC_TTL_MS - 1, refresh: true });
    assert.equal(srv.requests.length, 2);
    await loadSpec({ ...base, now: () => SPEC_TTL_MS * 3 });
    assert.equal(srv.requests.length, 3);
  } finally {
    await srv.close();
  }
});

test('an unreachable spec is SPEC_UNAVAILABLE', async () => {
  const srv = await startServer((_req, res) => json(res, 500, {}));
  const dir = await mkdtemp(path.join(os.tmpdir(), 'emws-spec-'));
  try {
    await assert.rejects(loadSpec({ dir, envName: 'staging', specUrl: `${srv.url}/spec.json` }), { kind: 'network', code: 'SPEC_UNAVAILABLE' });
  } finally {
    await srv.close();
  }
});
