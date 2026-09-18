import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { WHATSAPP_PAIRING_TERMINAL_STATES } from '$lib/api';

/**
 * Cross-implementation parity for the pairing row's closed states.
 *
 * The set exists in two languages: `db.WHATSAPP_PAIRING_TERMINAL_STATES`, which
 * decides whether the start route refuses and whether the reader vetoes a live
 * relay, and the TypeScript mirror the card falls back to for a stream frame,
 * which carries no `terminal` field of its own. A name added on one side and
 * not the other is a card that offers a start against a row the server
 * considers open, or withholds one against a row it considers closed.
 *
 * Read off `db.py` rather than captured as a table, so the pin cannot go stale
 * against the thing it is pinning. The Python side needs no counterpart test:
 * it is the authority, and this is the copy.
 */
// `process.cwd()` is `web/` under vitest, which is how `lib/styles/cascade.ts`
// reaches the stylesheet; the Python package is its sibling.
const DB_PY = join(process.cwd(), '..', 'src', 'istota', 'db.py');

function pythonTerminalStates(): string[] {
  const source = readFileSync(DB_PY, 'utf8');
  const block = /WHATSAPP_PAIRING_TERMINAL_STATES\s*=\s*frozenset\(\{([\s\S]*?)\}\)/.exec(source);
  expect(
    block,
    'db.py no longer declares WHATSAPP_PAIRING_TERMINAL_STATES as a frozenset literal',
  ).not.toBeNull();
  // The literal names constants (`WHATSAPP_PAIRING_PAIRED`), so each is
  // resolved back to the string it is assigned a few lines above.
  const names = [...block![1].matchAll(/WHATSAPP_PAIRING_[A-Z_]+/g)].map((m) => m[0]);
  expect(names.length).toBeGreaterThan(0);
  return names.map((name) => {
    const assignment = new RegExp(`^${name}\\s*=\\s*"([a-z_]+)"`, 'm').exec(source);
    expect(
      assignment,
      `db.py declares ${name} in the frozenset but assigns it no string`,
    ).not.toBeNull();
    return assignment![1];
  });
}

describe('the pairing terminal states are one set in two languages', () => {
  it('matches db.WHATSAPP_PAIRING_TERMINAL_STATES exactly', () => {
    expect([...WHATSAPP_PAIRING_TERMINAL_STATES].sort()).toEqual(pythonTerminalStates().sort());
  });

  it('reads a non-empty set out of db.py', () => {
    // Guards the regex rather than the product: a pattern that quietly stops
    // matching would compare two empty sets and pass.
    expect(pythonTerminalStates().length).toBe(3);
  });
});
