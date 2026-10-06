/** Loads ~/.config/emws: validated profiles.json, the mode-600 .env, and the selected profile's context. */
import { readFile, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { CliError } from './errors.ts';

export type Environment = { apiBase: string; specUrl: string };
export type Profile = {
  env: string;
  auth: 'apiKey' | 'basic';
  secret: string;
  username?: string;
  clientId?: string | number;
  dbId?: string | number;
  allowWrites?: boolean;
};
export type Config = { environments: Record<string, Environment>; profiles: Record<string, Profile>; defaultProfile?: string };
export type Context = {
  dir: string;
  config: Config;
  profileName: string;
  profile: Profile;
  envName: string;
  environment: Environment;
  secret: string;
  secrets: Record<string, string>;
  allowWrites: boolean;
};

type Obj = Record<string, unknown>;
const SETUP_HINT = 'The expected layout is in references/profiles.example.json in the emws-api skill.';

export function configDir(env: Record<string, string | undefined> = process.env): string {
  return env.EMWS_CONFIG_DIR || path.join(os.homedir(), '.config', 'emws');
}

const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const invalid = (where: string, problem: string): CliError =>
  new CliError('config', 'CONFIG_INVALID', `profiles.json: ${where} ${problem}`, SETUP_HINT);

export function validateConfig(raw: unknown): Config {
  if (!isObj(raw)) throw invalid('root', 'must be a JSON object');
  const { environments, profiles, defaultProfile } = raw;
  if (!isObj(environments) || Object.keys(environments).length === 0) throw invalid('environments', 'must be a non-empty object');
  for (const [name, e] of Object.entries(environments)) {
    if (!isObj(e)) throw invalid(`environments.${name}`, 'must be an object');
    for (const key of ['apiBase', 'specUrl']) {
      const v = e[key];
      if (typeof v !== 'string' || !/^https?:\/\//.test(v)) throw invalid(`environments.${name}.${key}`, 'must be an http(s) URL');
    }
  }
  if (!isObj(profiles) || Object.keys(profiles).length === 0) throw invalid('profiles', 'must be a non-empty object');
  for (const [name, p] of Object.entries(profiles)) {
    const at = `profiles.${name}`;
    if (!isObj(p)) throw invalid(at, 'must be an object');
    if (typeof p.env !== 'string' || !(p.env in environments)) {
      throw invalid(`${at}.env`, `must name one of: ${Object.keys(environments).join(', ')}`);
    }
    if (p.auth !== 'apiKey' && p.auth !== 'basic') throw invalid(`${at}.auth`, 'must be "apiKey" or "basic"');
    if (typeof p.secret !== 'string' || !p.secret) throw invalid(`${at}.secret`, 'must name a variable in .env');
    if (p.allowWrites !== undefined && typeof p.allowWrites !== 'boolean') throw invalid(`${at}.allowWrites`, 'must be true or false');
    if (p.auth === 'basic') {
      if (typeof p.username !== 'string' || !p.username) throw invalid(`${at}.username`, 'is required for auth "basic"');
      for (const key of ['clientId', 'dbId']) {
        const v = p[key];
        if (typeof v !== 'string' && typeof v !== 'number') throw invalid(`${at}.${key}`, 'is required for auth "basic"');
      }
    }
  }
  if (defaultProfile !== undefined && (typeof defaultProfile !== 'string' || !(defaultProfile in profiles))) {
    throw invalid('defaultProfile', 'must name a profile');
  }
  return raw as Config;
}

export function parseEnvFile(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m) continue;
    let v = m[2];
    if (v.length >= 2 && (v[0] === '"' || v[0] === "'") && v.at(-1) === v[0]) v = v.slice(1, -1);
    out[m[1]] = v;
  }
  return out;
}

export async function loadConfig(dir: string): Promise<Config> {
  const file = path.join(dir, 'profiles.json');
  let text: string;
  try {
    text = await readFile(file, 'utf8');
  } catch {
    throw new CliError('config', 'CONFIG_INVALID', `${file} not found`, SETUP_HINT);
  }
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new CliError('config', 'CONFIG_INVALID', `${file} is not valid JSON: ${(err as Error).message}`, SETUP_HINT);
  }
  return validateConfig(raw);
}

/** Reads .env, refusing it when group or others can read it. A missing file yields no secrets. */
export async function loadSecrets(dir: string): Promise<Record<string, string>> {
  const file = path.join(dir, '.env');
  let mode: number;
  try {
    mode = (await stat(file)).mode;
  } catch {
    return {};
  }
  if (mode & 0o077) {
    throw new CliError('config', 'ENV_PERMS', `${file} is readable by other users (mode ${(mode & 0o777).toString(8)})`, `Run: chmod 600 ${file}`);
  }
  return parseEnvFile(await readFile(file, 'utf8'));
}

export function resolveProfileName(config: Config, flag: string | undefined, env: Record<string, string | undefined>): string {
  const name = flag || env.EMWS_PROFILE || config.defaultProfile;
  if (!name) {
    throw new CliError('config', 'USAGE', 'no profile selected', 'Pass -p <profile>, set EMWS_PROFILE, or set defaultProfile in profiles.json.');
  }
  if (!(name in config.profiles)) {
    throw new CliError('config', 'CONFIG_INVALID', `unknown profile "${name}"`, `Known profiles: ${Object.keys(config.profiles).join(', ')}`);
  }
  return name;
}

export function effectiveAllowWrites(profile: Profile): boolean {
  return profile.allowWrites ?? profile.env !== 'prod';
}

export async function loadContext(opts: { dir: string; profile?: string; env: Record<string, string | undefined> }): Promise<Context> {
  const config = await loadConfig(opts.dir);
  const profileName = resolveProfileName(config, opts.profile, opts.env);
  const profile = config.profiles[profileName];
  const secrets = await loadSecrets(opts.dir);
  const secret = secrets[profile.secret];
  if (!secret) {
    throw new CliError(
      'config',
      'SECRET_MISSING',
      `${profile.secret} is not set in ${path.join(opts.dir, '.env')}`,
      `Ask the user to add ${profile.secret}=<value> to that file (mode 600).`,
    );
  }
  return {
    dir: opts.dir,
    config,
    profileName,
    profile,
    envName: profile.env,
    environment: config.environments[profile.env],
    secret,
    secrets,
    allowWrites: effectiveAllowWrites(profile),
  };
}
