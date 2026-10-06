#!/usr/bin/env node
/** Entry point for the emws CLI; all behaviour lives in lib/cli.ts. */
import { main } from './lib/cli.ts';

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString('utf8');
}

try {
  process.exitCode = await main(process.argv.slice(2), {
    stdout: (s) => process.stdout.write(s),
    stderr: (s) => process.stderr.write(s),
    cwd: process.cwd(),
    env: process.env,
    stdin: readStdin,
  });
} catch (err) {
  process.stderr.write(`emws internal error (a bug in the CLI, not an API failure):\n${(err as Error).stack ?? String(err)}\n`);
  process.exitCode = 1;
}
