import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRedactor, MASK } from '../../skills/emws-api/scripts/lib/redact.ts';

test('masks secret values inside strings and nested objects', () => {
  const r = createRedactor(['stg-secret-key-123']);
  assert.equal(r.text('key=stg-secret-key-123;'), `key=${MASK};`);
  assert.deepEqual(r.value({ a: ['x stg-secret-key-123 y'], b: { c: 'stg-secret-key-123' }, n: 5 }), {
    a: [`x ${MASK} y`],
    b: { c: MASK },
    n: 5,
  });
});

test('masks credential keys regardless of case', () => {
  const r = createRedactor();
  assert.deepEqual(r.value({ Password: 'p', authorization: 'Bearer t', 'X-Api-Key': 'k', name: 'n' }), {
    Password: MASK,
    authorization: MASK,
    'X-Api-Key': MASK,
    name: 'n',
  });
});

test('ignores secrets shorter than four characters', () => {
  const r = createRedactor(['a1']);
  assert.equal(r.text('a1 stays'), 'a1 stays');
});

test('add() masks a value learned later, such as a fresh token', () => {
  const r = createRedactor();
  r.add('eyJhbGciOi.token.sig');
  assert.equal(r.text('Bearer eyJhbGciOi.token.sig'), `Bearer ${MASK}`);
});

test('masks the longest secret first so no fragment survives', () => {
  const r = createRedactor(['abcd', 'abcdef']);
  assert.equal(r.text('abcdef'), MASK);
});
