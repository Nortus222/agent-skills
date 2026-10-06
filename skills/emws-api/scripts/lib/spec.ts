/** Reads the Function App's OpenAPI document: cached fetch, route listing, route matching, and $ref-resolved descriptions. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { CliError } from './errors.ts';

export type OpenApiDoc = { paths?: Record<string, Record<string, unknown>>; [k: string]: unknown };
export type Route = { method: string; path: string; summary: string };
type Operation = { summary?: string; operationId?: string; parameters?: unknown[]; requestBody?: unknown; responses?: unknown };

export const SPEC_TTL_MS = 10 * 60 * 1000;
const METHODS = ['get', 'post', 'put', 'patch', 'delete'];
const MAX_REF_DEPTH = 12;
const FETCH_TIMEOUT_MS = 15_000;

/** Spec paths and CLI paths compare without the Functions `/api` prefix, query, or trailing slash. */
export function normalizePath(p: string): string {
  let out = p.split('?')[0];
  if (!out.startsWith('/')) out = `/${out}`;
  out = out.replace(/^\/api(?=\/|$)/i, '');
  out = out.replace(/\/+$/, '');
  return out || '/';
}

const segments = (p: string): string[] => normalizePath(p).split('/').filter(Boolean);
const isParam = (s: string): boolean => /^\{.+\}$/.test(s);

function templateMatches(template: string, actual: string): boolean {
  const t = segments(template);
  const a = segments(actual);
  return t.length === a.length && t.every((s, i) => isParam(s) || s.toLowerCase() === a[i].toLowerCase());
}

export function listRoutes(doc: OpenApiDoc, filter?: string): Route[] {
  const routes: Route[] = [];
  for (const [rawPath, ops] of Object.entries(doc.paths ?? {})) {
    for (const method of METHODS) {
      const op = ops[method] as Operation | undefined;
      if (op) routes.push({ method: method.toUpperCase(), path: normalizePath(rawPath), summary: op.summary ?? op.operationId ?? '' });
    }
  }
  routes.sort((a, b) => a.path.localeCompare(b.path) || METHODS.indexOf(a.method.toLowerCase()) - METHODS.indexOf(b.method.toLowerCase()));
  if (!filter) return routes;
  const needle = filter.toLowerCase();
  return routes.filter((r) => `${r.method} ${r.path} ${r.summary}`.toLowerCase().includes(needle));
}

export function pathKnown(doc: OpenApiDoc, p: string): boolean {
  return Object.keys(doc.paths ?? {}).some((t) => templateMatches(t, p));
}

function levenshtein(a: string, b: string): number {
  const row = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    let prev = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const tmp = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1));
      prev = tmp;
    }
  }
  return row[b.length];
}

function segmentCost(t: string, a: string): number {
  if (isParam(t) || t.toLowerCase() === a.toLowerCase()) return 0;
  return levenshtein(t.toLowerCase(), a.toLowerCase()) / Math.max(t.length, a.length);
}

/** Edit distance over path segments: a parameter matches anything, a misspelt segment costs its relative edit distance. */
function pathDistance(template: string, actual: string): number {
  const t = segments(template);
  const a = segments(actual);
  const d = Array.from({ length: t.length + 1 }, (_, i) => Array.from({ length: a.length + 1 }, (_, j) => (i === 0 ? j : j === 0 ? i : 0)));
  for (let i = 1; i <= t.length; i++) {
    for (let j = 1; j <= a.length; j++) {
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + segmentCost(t[i - 1], a[j - 1]));
    }
  }
  return d[t.length][a.length];
}

export function nearestRoutes(doc: OpenApiDoc, method: string, p: string, n = 3): string[] {
  const wanted = method.toUpperCase();
  return listRoutes(doc)
    .map((r) => ({ r, d: pathDistance(r.path, p) + (r.method === wanted ? 0 : 0.25) }))
    .sort((x, y) => x.d - y.d)
    .slice(0, n)
    .map(({ r }) => `${r.method} ${r.path}`);
}

function resolvePointer(doc: OpenApiDoc, ref: string): unknown {
  return ref
    .slice(2)
    .split('/')
    .map((s) => s.replace(/~1/g, '/').replace(/~0/g, '~'))
    .reduce<unknown>((node, key) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : undefined), doc);
}

function resolveRefs(node: unknown, doc: OpenApiDoc, seen: string[] = []): unknown {
  if (Array.isArray(node)) return node.map((x) => resolveRefs(x, doc, seen));
  if (node === null || typeof node !== 'object') return node;
  const ref = (node as Record<string, unknown>).$ref;
  if (typeof ref === 'string' && ref.startsWith('#/')) {
    if (seen.includes(ref) || seen.length >= MAX_REF_DEPTH) return { $ref: ref, note: 'recursive reference not expanded' };
    return resolveRefs(resolvePointer(doc, ref), doc, [...seen, ref]);
  }
  return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, resolveRefs(v, doc, seen)]));
}

export function describeRoute(doc: OpenApiDoc, method: string, p: string): Record<string, unknown> | undefined {
  for (const [rawPath, ops] of Object.entries(doc.paths ?? {})) {
    if (!templateMatches(rawPath, p)) continue;
    const op = ops[method.toLowerCase()] as Operation | undefined;
    if (!op) continue;
    const shared = (ops.parameters as unknown[] | undefined) ?? [];
    const resolved = resolveRefs({ parameters: [...shared, ...(op.parameters ?? [])], requestBody: op.requestBody, responses: op.responses }, doc) as Record<string, unknown>;
    return { method: method.toUpperCase(), path: normalizePath(rawPath), summary: op.summary ?? op.operationId ?? '', ...resolved };
  }
  return undefined;
}

export async function loadSpec(o: {
  dir: string;
  envName: string;
  specUrl: string;
  refresh?: boolean;
  fetchImpl?: typeof fetch;
  now?: () => number;
}): Promise<OpenApiDoc> {
  const now = (o.now ?? Date.now)();
  const file = path.join(o.dir, 'cache', `spec-${o.envName}.json`);
  if (!o.refresh) {
    try {
      const cached = JSON.parse(await readFile(file, 'utf8')) as { fetchedAt: number; doc: OpenApiDoc };
      if (now - cached.fetchedAt < SPEC_TTL_MS) return cached.doc;
    } catch {
      // No usable cache; fetch below.
    }
  }
  let doc: OpenApiDoc;
  try {
    const res = await (o.fetchImpl ?? fetch)(o.specUrl, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    doc = (await res.json()) as OpenApiDoc;
  } catch (err) {
    throw new CliError(
      'network',
      'SPEC_UNAVAILABLE',
      `could not load the OpenAPI document from ${o.specUrl}: ${(err as Error).message}`,
      "Check the environment's specUrl; route discovery needs the Function App to be reachable.",
    );
  }
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, JSON.stringify({ fetchedAt: now, doc }));
  return doc;
}
