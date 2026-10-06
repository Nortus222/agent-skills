/** The local call log: one redacted JSON line per call in logs/YYYY-MM-DD.jsonl, kept 14 days. */
import { execFileSync } from 'node:child_process';
import { appendFile, mkdir, readdir, readFile, unlink } from 'node:fs/promises';
import path from 'node:path';
import { CliError } from './errors.ts';

export type LogEntry = {
  ts: string;
  callId: string;
  ok: boolean;
  status: number | null;
  code: string | null;
  method: string;
  path: string;
  profile: string;
  env: string;
  timingMs: number;
  cwd: string;
  branch: string | null;
  agent: string | null;
  envelope: unknown;
  requestBody: string | null;
  responseBody: string | null;
  responseTruncated: boolean;
};

export type LogFilter = { callId?: string; errors?: boolean; last?: number; sinceMs?: number; profile?: string; path?: string; cwd?: string; now?: number };

export const MAX_LOGGED_RESPONSE_BYTES = 64 * 1024;
const RETENTION_DAYS = 14;
const DEFAULT_LAST = 10;
const LOG_FILE = /^(\d{4}-\d{2}-\d{2})\.jsonl$/;

const logsDir = (dir: string): string => path.join(dir, 'logs');

export function truncateBody(text: string | null): { body: string | null; truncated: boolean } {
  if (text === null) return { body: null, truncated: false };
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length <= MAX_LOGGED_RESPONSE_BYTES) return { body: text, truncated: false };
  return { body: bytes.subarray(0, MAX_LOGGED_RESPONSE_BYTES).toString('utf8'), truncated: true };
}

/** One appendFile per entry, so concurrent agents interleave whole lines. */
export async function appendLog(dir: string, entry: LogEntry): Promise<void> {
  await mkdir(logsDir(dir), { recursive: true, mode: 0o700 });
  await appendFile(path.join(logsDir(dir), `${entry.ts.slice(0, 10)}.jsonl`), `${JSON.stringify(entry)}\n`, { mode: 0o600 });
}

function isLogEntry(v: unknown): v is LogEntry {
  if (v === null || typeof v !== 'object') return false;
  const e = v as Record<string, unknown>;
  return ['ts', 'callId', 'method', 'path', 'profile'].every((k) => typeof e[k] === 'string');
}

export async function readLogs(dir: string): Promise<LogEntry[]> {
  let names: string[];
  try {
    names = (await readdir(logsDir(dir))).filter((n) => LOG_FILE.test(n)).sort();
  } catch {
    return [];
  }
  const entries: LogEntry[] = [];
  for (const name of names) {
    for (const line of (await readFile(path.join(logsDir(dir), name), 'utf8')).split('\n')) {
      if (!line.trim()) continue;
      try {
        const parsed: unknown = JSON.parse(line);
        if (isLogEntry(parsed)) entries.push(parsed);
      } catch {
        // A torn or corrupt line is skipped so the rest of the log stays usable.
      }
    }
  }
  return entries.sort((a, b) => a.ts.localeCompare(b.ts));
}

export function queryLogs(entries: LogEntry[], f: LogFilter): LogEntry[] {
  if (f.callId) return entries.filter((e) => e.callId === f.callId);
  const now = f.now ?? Date.now();
  const matched = entries.filter(
    (e) =>
      (!f.errors || !e.ok) &&
      (!f.profile || e.profile === f.profile) &&
      (!f.path || e.path.includes(f.path)) &&
      (!f.cwd || e.cwd === f.cwd) &&
      (f.sinceMs === undefined || Date.parse(e.ts) >= now - f.sinceMs),
  );
  return matched.slice(-(f.last ?? DEFAULT_LAST));
}

export async function pruneLogs(dir: string, now: number, days = RETENTION_DAYS): Promise<void> {
  let names: string[];
  try {
    names = await readdir(logsDir(dir));
  } catch {
    return;
  }
  const cutoff = now - days * 86_400_000;
  for (const name of names) {
    const m = LOG_FILE.exec(name);
    if (m && Date.parse(`${m[1]}T00:00:00Z`) < cutoff) await unlink(path.join(logsDir(dir), name));
  }
}

const UNITS: Record<string, number> = { s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000 };

export function parseDuration(s: string): number {
  const m = /^(\d+)([smhd])$/.exec(s.trim());
  if (!m) throw new CliError('config', 'USAGE', `"${s}" is not a duration`, 'Use a number and a unit, such as 30s, 15m, 2h, or 1d.');
  return Number(m[1]) * UNITS[m[2]];
}

export function formatSummary(e: LogEntry): string {
  return `${e.ts.slice(0, 19).replace('T', ' ')}  ${String(e.status ?? e.code).padEnd(12)} ${e.method.padEnd(6)} ${e.path} · ${e.profile} · ${e.timingMs}ms · ${e.callId}`;
}

export function gitBranch(cwd: string): string | null {
  try {
    const out = execFileSync('git', ['-C', cwd, 'branch', '--show-current'], { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
    return out || null;
  } catch {
    return null;
  }
}
