/** Masks secret values, credential headers, and password fields before anything is printed or logged. */
export type Redactor = {
  add(secret: string | undefined): void;
  text(s: string): string;
  value<T>(v: T): T;
};

export const MASK = '***';
const SECRET_KEYS = new Set(['password', 'authorization', 'x-api-key']);
const MIN_SECRET_LENGTH = 4;

export function createRedactor(secrets: Iterable<string> = []): Redactor {
  const values = new Set<string>();
  const add = (s: string | undefined): void => {
    if (s && s.length >= MIN_SECRET_LENGTH) values.add(s);
  };
  for (const s of secrets) add(s);

  const text = (s: string): string => {
    let out = s;
    for (const v of [...values].sort((a, b) => b.length - a.length)) out = out.split(v).join(MASK);
    return out;
  };

  const value = <T>(v: T): T => {
    if (typeof v === 'string') return text(v) as T;
    if (Array.isArray(v)) return v.map((x) => value(x)) as T;
    if (v !== null && typeof v === 'object') {
      const out: Record<string, unknown> = {};
      for (const [k, x] of Object.entries(v)) out[k] = SECRET_KEYS.has(k.toLowerCase()) ? MASK : value(x);
      return out as T;
    }
    return v;
  };

  return { add, text, value };
}
