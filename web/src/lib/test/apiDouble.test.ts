/**
 * The shared `$lib/api` test double, and the rule that there is only one.
 *
 * A hand-written factory replaces the whole module, so every export it does not
 * name is `undefined` — and the type system cannot see it, because a factory's
 * return type is never checked against the module's. Twenty-odd files carried
 * such a list and none of them listed `AuthError`, so each was one
 * `e instanceof AuthError` away from `Right-hand side of 'instanceof' is not
 * callable`, at whatever future moment its component started handling a 401
 * (ISSUE-468).
 *
 * The first group below is what makes that impossible rather than merely
 * fixed: the double takes its export list from the module itself. The last
 * group is what keeps it that way — a new hand-rolled list fails here, once,
 * instead of lying dormant in the file that added it.
 */
import { describe, it, expect, vi } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { apiDouble, fillApiDouble } from './apiDouble';

// `import.meta.url` is an http URL under jsdom, so the tree is reached from the
// vitest root instead — which is this directory, since the config lives here.
// The `scanned` floor in the last test is what catches a run rooted elsewhere.
const ROOT = process.cwd();

/** The value exports of `api.ts`, read off the source rather than off the double. */
function apiSourceExports(): string[] {
  const source = readFileSync(join(ROOT, 'src/lib/api.ts'), 'utf8');
  const names = new Set<string>();
  for (const m of source.matchAll(/^export (?:async function|function|const|class) (\w+)/gm)) {
    names.add(m[1]);
  }
  // `export { AuthError };` — a value re-export. `export type {` does not match.
  for (const m of source.matchAll(/^export \{([^}]*)\};/gm)) {
    for (const part of m[1].split(',')) {
      const name = part.trim().split(/\s+/).pop();
      if (name) names.add(name);
    }
  }
  return [...names].sort();
}

describe('the $lib/api double', () => {
  it('carries every value export of api.ts, taken from the module and not a list', async () => {
    // The oracle is the module's own source, read independently of the
    // derivation — so this fails both if `apiDouble` regresses to a written
    // list and if an export stops arriving on the double.
    const double = await apiDouble();

    expect(Object.keys(double).sort()).toEqual(apiSourceExports());
    // The one this was filed over, named so a regression reads plainly.
    expect(double).toHaveProperty('AuthError');
  });

  it('passes every class through, so instanceof holds across the seam', async () => {
    // A per-file stand-in class only ever worked because both sides read it off
    // the same mock. The real class is what a component compiled against the
    // real module checks, and what `money/api.ts` re-exports (S18).
    const actual = await vi.importActual<Record<string, unknown>>('$lib/api');
    const double = (await apiDouble()) as unknown as Record<string, unknown>;

    // Asserted as a set rather than one or two names: a class the derivation
    // started stubbing would turn every `instanceof` against it false, silently.
    const passedThrough = Object.keys(actual).filter(
      (name) => typeof actual[name] === 'function' && !vi.isMockFunction(double[name]),
    );
    expect(passedThrough.sort()).toEqual([
      'AuthError',
      'ChatMemoryBusyError',
      'ChatMemoryConflictError',
      'ChatMessageBusyError',
      'ChatRoomBusyError',
      'UploadUnreachableError',
    ]);
    for (const name of passedThrough) expect(double[name]).toBe(actual[name]);

    const { AuthError, ChatRoomBusyError } = actual as {
      AuthError: new () => Error;
      ChatRoomBusyError: new () => Error;
    };
    expect(new (double.AuthError as new () => Error)()).toBeInstanceOf(AuthError);
    expect(new (double.ChatRoomBusyError as new () => Error)()).toBeInstanceOf(ChatRoomBusyError);
  });

  it('stubs the function exports, and each is controllable per test', async () => {
    const double = await apiDouble();

    expect(vi.isMockFunction(double.getChatRooms)).toBe(true);
    expect(vi.isMockFunction(double.getMe)).toBe(true);

    double.getChatRooms.mockResolvedValue({ rooms: [] });
    await expect(double.getChatRooms()).resolves.toEqual({ rooms: [] });
  });

  it('leaves the non-function exports alone', async () => {
    const actual = await vi.importActual<typeof import('$lib/api')>('$lib/api');
    const double = await apiDouble();

    expect(double.AVATAR_ACCEPT).toBe(actual.AVATAR_ACCEPT);
  });

  it('takes overrides, including a name the module does not export', async () => {
    // Two files need this: a default implementation the component reads at
    // mount, and a spy for a function that was deliberately *removed* from the
    // module — a spy that does not exist cannot record not being called.
    const double = await apiDouble({
      getChatConfig: vi.fn(async () => ({ client_poll_interval_ms: 1500 })),
      listPendingConfirmations: vi.fn(),
    });

    await expect(double.getChatConfig()).resolves.toEqual({ client_poll_interval_ms: 1500 });
    expect(vi.isMockFunction(double.listPendingConfirmations)).toBe(true);
    expect(double.AuthError).toBeTypeOf('function');
  });

  it('gives each caller its own spies', async () => {
    const one = await apiDouble();
    const two = await apiDouble();

    one.getChatRooms.mockResolvedValue({ rooms: [] });
    await one.getChatRooms();

    expect(two.getChatRooms).not.toHaveBeenCalled();
  });
});

describe('filling a hoisted handle', () => {
  it('writes the double onto the callers own object', async () => {
    // The handle is what `vi.mock` hands back, so the fill has to land on it
    // rather than on a copy.
    const handle = {} as Record<string, unknown>;
    const filled = await fillApiDouble(handle);

    expect(filled).toBe(handle);
    expect(vi.isMockFunction(handle.getMe)).toBe(true);
  });

  it('keeps the spies a test already configured when it is filled again', async () => {
    // The store tests call `vi.resetModules()` between cases and the handle
    // outlives it. A second derivation would replace every spy on it.
    const handle = {} as Record<string, unknown>;
    const first = await fillApiDouble(handle);
    first.getChatRooms.mockResolvedValue({ rooms: [] });

    const second = await fillApiDouble(handle);

    expect(second.getChatRooms).toBe(first.getChatRooms);
    await expect(second.getChatRooms()).resolves.toEqual({ rooms: [] });
  });

  it('applies extras handed to a later fill', async () => {
    const handle = {} as Record<string, unknown>;
    await fillApiDouble(handle);

    const again = await fillApiDouble(handle, { listPendingConfirmations: vi.fn() });

    expect(vi.isMockFunction(again.listPendingConfirmations)).toBe(true);
  });
});

/** Every `*.test.ts` under a root vitest collects from. */
function testFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...testFiles(path));
    else if (entry.name.endsWith('.test.ts')) found.push(path);
  }
  return found;
}

describe('the rule', () => {
  it('is the only $lib/api factory in the tree', () => {
    const offenders: string[] = [];
    let scanned = 0;

    // Both roots of `vitest.config.ts`'s `include`.
    for (const root of ['src', 'scripts']) {
      for (const path of testFiles(join(ROOT, root))) {
        const source = readFileSync(path, 'utf8');

        for (const line of source.split('\n')) {
          // A real call is a top-level statement. Anchoring at column 0 is what
          // keeps prose out of the scan — this file's own header quotes the
          // very shape the rule rejects.
          if (!line.startsWith("vi.mock('$lib/api'")) continue;
          scanned += 1;

          // Decided from the factory's head, never from a slice of the file: a
          // hand-rolled factory closes with `}));`, so a span-based scan runs
          // straight past it and into the next mock's `importOriginal`.
          const handle = /^vi\.mock\('\$lib\/api', \(\) => (\w+)\);$/.exec(line)?.[1];
          if (handle) {
            if (!source.includes(`fillApiDouble(${handle}`))
              offenders.push(path.slice(ROOT.length));
            continue;
          }

          // The other safe shape: a factory built on the real module, which
          // cannot omit an export either. Its body is the lines up to the first
          // column-0 brace, so the check is bounded by the statement.
          if (/^vi\.mock\('\$lib\/api', async /.test(line)) {
            const at = source.indexOf(line);
            const body = source.slice(at).split('\n');
            const end = body.findIndex((l, i) => i > 0 && l.startsWith('}'));
            const factory = body.slice(0, end === -1 ? 1 : end + 1).join('\n');
            if (/importOriginal|importActual/.test(factory)) continue;
          }

          offenders.push(path.slice(ROOT.length));
        }
      }
    }

    expect(offenders).toEqual([]);
    // Proof the walk found the tree. A run rooted anywhere else reads no test
    // files at all and would otherwise report a clean sweep of nothing.
    expect(scanned).toBeGreaterThan(40);
  });
});
