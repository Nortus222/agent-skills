import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  configDir,
  effectiveAllowWrites,
  loadConfig,
  loadContext,
  parseEnvFile,
  validateConfig,
} from '../../skills/emws-api/scripts/lib/config.ts';
import { makeConfigDir, SECRETS, testConfig } from './helpers.ts';

const base = 'https://example.test';

test('configDir defaults to ~/.config/emws and honours EMWS_CONFIG_DIR', () => {
  assert.match(configDir({}), /\.config[\\/]emws$/);
  assert.equal(configDir({ EMWS_CONFIG_DIR: '/tmp/x' }), '/tmp/x');
});

test('selects the profile by flag, then EMWS_PROFILE, then defaultProfile', async () => {
  const dir = await makeConfigDir(testConfig(base));
  assert.equal((await loadContext({ dir, env: {} })).profileName, 'stg');
  assert.equal((await loadContext({ dir, env: { EMWS_PROFILE: 'prod' } })).profileName, 'prod');
  assert.equal((await loadContext({ dir, profile: 'user', env: { EMWS_PROFILE: 'prod' } })).profileName, 'user');
});

test('the context carries the environment and the resolved secret', async () => {
  const dir = await makeConfigDir(testConfig(base));
  const ctx = await loadContext({ dir, profile: 'stg', env: {} });
  assert.equal(ctx.envName, 'staging');
  assert.equal(ctx.environment.apiBase, `${base}/emws-staging`);
  assert.equal(ctx.secret, SECRETS.STG_KEY);
});

test('reports the exact field that is invalid', () => {
  const cfg = testConfig(base);
  delete (cfg.profiles.user as { dbId?: number }).dbId;
  assert.throws(() => validateConfig(cfg), { code: 'CONFIG_INVALID', message: /profiles\.user\.dbId is required for auth "basic"/ });
  const bad = testConfig(base);
  (bad.profiles.stg as { env: string }).env = 'qa';
  assert.throws(() => validateConfig(bad), { message: /profiles\.stg\.env must name one of: staging, prod/ });
});

test('rejects an unknown profile and lists the known ones', async () => {
  const dir = await makeConfigDir(testConfig(base));
  await assert.rejects(loadContext({ dir, profile: 'nope', env: {} }), { code: 'CONFIG_INVALID', hint: /stg, prod, prod-rw, user/ });
});

test('refuses a .env readable by others', async () => {
  const dir = await makeConfigDir(testConfig(base), SECRETS, 0o644);
  await assert.rejects(loadContext({ dir, env: {} }), { kind: 'config', code: 'ENV_PERMS', message: /mode 644/, hint: /chmod 600/ });
});

test('reports a secret missing from .env', async () => {
  const dir = await makeConfigDir(testConfig(base), { STG_KEY: SECRETS.STG_KEY });
  await assert.rejects(loadContext({ dir, profile: 'prod', env: {} }), { code: 'SECRET_MISSING', message: /PROD_KEY/ });
});

test('reports a missing or unparseable profiles.json', async () => {
  await assert.rejects(loadConfig('/nonexistent-emws-dir'), { code: 'CONFIG_INVALID', message: /not found/ });
});

test('writes default to off for prod and on elsewhere', () => {
  assert.equal(effectiveAllowWrites({ env: 'prod', auth: 'apiKey', secret: 'X' }), false);
  assert.equal(effectiveAllowWrites({ env: 'staging', auth: 'apiKey', secret: 'X' }), true);
  assert.equal(effectiveAllowWrites({ env: 'prod', auth: 'apiKey', secret: 'X', allowWrites: true }), true);
  assert.equal(effectiveAllowWrites({ env: 'staging', auth: 'apiKey', secret: 'X', allowWrites: false }), false);
});

test('parses .env lines with comments, export, and quotes', () => {
  assert.deepEqual(parseEnvFile('# c\nA=1\nexport B="two words"\nC=\'x=y\'\n\nbad line\n'), { A: '1', B: 'two words', C: 'x=y' });
});
