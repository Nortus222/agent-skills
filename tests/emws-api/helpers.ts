import { chmod, mkdtemp, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import type { ServerResponse } from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { main } from '../../skills/emws-api/scripts/lib/cli.ts';

export type Recorded = { method: string; url: string; headers: Record<string, string | string[] | undefined>; body: string };
export type Handler = (req: Recorded, res: ServerResponse) => void;

export const SECRETS = { STG_KEY: 'stg-secret-key-123', PROD_KEY: 'prod-secret-key-456', USER_PW: 'hunter2-password' };

/** A config whose environments point at the fake server: staging at /emws-staging, prod at /emws. */
export function testConfig(base: string) {
  return {
    environments: {
      staging: { apiBase: `${base}/emws-staging`, specUrl: `${base}/spec.json` },
      prod: { apiBase: `${base}/emws`, specUrl: `${base}/spec.json` },
    },
    profiles: {
      stg: { env: 'staging', auth: 'apiKey', secret: 'STG_KEY' },
      prod: { env: 'prod', auth: 'apiKey', secret: 'PROD_KEY' },
      'prod-rw': { env: 'prod', auth: 'apiKey', secret: 'PROD_KEY', allowWrites: true },
      user: { env: 'staging', auth: 'basic', username: 'agent@example.com', secret: 'USER_PW', clientId: 'pocketmanage', dbId: 7 },
    },
    defaultProfile: 'stg',
  };
}

export async function makeConfigDir(config: unknown, secrets: Record<string, string> = SECRETS, envMode = 0o600): Promise<string> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'emws-test-'));
  await writeFile(path.join(dir, 'profiles.json'), JSON.stringify(config));
  const envFile = path.join(dir, '.env');
  await writeFile(envFile, `${Object.entries(secrets).map(([k, v]) => `${k}=${v}`).join('\n')}\n`);
  await chmod(envFile, envMode);
  return dir;
}

export async function startServer(handler: Handler) {
  const requests: Recorded[] = [];
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const rec: Recorded = { method: req.method ?? '', url: req.url ?? '', headers: req.headers, body: Buffer.concat(chunks).toString('utf8') };
    requests.push(rec);
    handler(rec, res);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as { port: number };
  return {
    url: `http://127.0.0.1:${port}`,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}

export function json(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { 'content-type': 'application/json', ...headers });
  res.end(JSON.stringify(body));
}

export function makeJwt(claims: Record<string, unknown>): string {
  const enc = (o: object) => Buffer.from(JSON.stringify(o)).toString('base64url');
  return `${enc({ alg: 'none', typ: 'JWT' })}.${enc(claims)}.sig`;
}

export async function runCli(
  argv: string[],
  o: { dir: string; env?: Record<string, string>; stdin?: string; now?: () => number; cwd?: string },
): Promise<{ code: number; stdout: string; stderr: string }> {
  let stdout = '';
  let stderr = '';
  const code = await main(argv, {
    stdout: (s) => {
      stdout += s;
    },
    stderr: (s) => {
      stderr += s;
    },
    cwd: o.cwd ?? o.dir,
    env: { EMWS_CONFIG_DIR: o.dir, ...o.env },
    stdin: async () => o.stdin ?? '',
    now: o.now,
  });
  return { code, stdout, stderr };
}
