/**
 * The one `$lib/api` test double.
 *
 * `vi.mock('$lib/api', () => ({ ... }))` replaces the whole module, so a
 * hand-written factory is a second copy of the export list — one that nothing
 * checks and that drifts silently, because a factory's return type is never
 * compared against the module's. Every name it leaves out is `undefined` at
 * runtime and correct-looking in the editor.
 *
 * So this does not write the list down. It reads the real module and mirrors
 * it: each function export becomes a bare `vi.fn()`, and everything else — the
 * error classes, the string constants — is passed straight through. A new
 * export is on the double the moment it is on the module, which is what makes
 * the omission class impossible rather than merely fixed (ISSUE-468).
 *
 * Passing the classes through rather than restating them is what makes
 * `instanceof` mean something. A test that rejects with `new api.AuthError()`
 * throws the class the component is actually checking against, instead of a
 * same-named stand-in that only lined up because both sides read it off the
 * same mock.
 *
 * `extra` is how a file keeps control of its own mock: a default implementation
 * a component reads at mount, a canned return value, or a spy for a name the
 * module does not export at all.
 *
 *     const api = vi.hoisted(() => ({}) as ApiDouble);
 *     vi.mock('$lib/api', () => api);
 *     await fillApiDouble(api);
 *
 * The handle is an empty object filled a moment later, rather than the double
 * itself, because `vi.hoisted` runs above the imports — where nothing is loaded
 * and `vi.importActual` cannot be awaited. The fill is a top-level `await`, so
 * it lands before any test body; a module imported *statically* by the test
 * file is initialized before it and still sees the spies, since a named import
 * reads through the namespace at each use rather than copying it.
 *
 * One consequence worth knowing: `vi.importActual` evaluates the real `api.ts`
 * and everything it imports — `$app/paths`, `$lib/platform/nativePicker`,
 * `$lib/stores/connectivity`, `$lib/basemap` — in every file that mocks the
 * module, most of which never loaded it before. Nothing in that graph does work
 * at module scope today. Anything added there will run in some forty test files.
 */
import { vi, type Mock } from 'vitest';

type ApiModule = typeof import('$lib/api');

/**
 * Every function export as a loose `Mock`, everything else — the classes, the
 * constants — as itself.
 *
 * Loose deliberately: `MockedFunction<typeof getRoomMessages>` would type
 * `mockResolvedValue` against the real return type, and every fixture in these
 * files is a partial one. That is worth tightening, but it is a different
 * change from this one and a far bigger diff. A bare `vi.fn()` is what they
 * had, and this is its type.
 */
export type ApiDouble = {
  [K in keyof ApiModule]: ApiModule[K] extends (...args: never[]) => unknown ? Mock : ApiModule[K];
};

/** No extras. `keyof` it is `never`, so `Omit` over it keeps the whole double. */
type NoExtra = Record<never, never>;

/** A class compiles to a function; only its source text tells the two apart. */
function isClass(value: unknown): boolean {
  return typeof value === 'function' && /^class[\s{]/.test(Function.prototype.toString.call(value));
}

export async function apiDouble<Extra extends Record<string, unknown> = NoExtra>(
  extra?: Extra,
): Promise<Omit<ApiDouble, keyof Extra> & Extra> {
  const actual = await vi.importActual<Record<string, unknown>>('$lib/api');
  const double: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(actual)) {
    double[name] = typeof value === 'function' && !isClass(value) ? vi.fn() : value;
  }
  return Object.assign(double, extra) as Omit<ApiDouble, keyof Extra> & Extra;
}

const building = new WeakMap<object, Promise<unknown>>();

/**
 * Fill a hoisted handle with the double, in place.
 *
 * The handle is what survives `vi.resetModules()` — which most of the store
 * tests call between cases. The mock factory hands back that same object, so
 * the spies a test configured are still on it afterwards. Deriving a second
 * double onto the handle would replace them, so a repeat fill re-uses the first
 * one and applies only whatever `extra` it was given.
 */
export function fillApiDouble<Extra extends Record<string, unknown> = NoExtra>(
  target: object,
  extra?: Extra,
): Promise<Omit<ApiDouble, keyof Extra> & Extra> {
  const filled = building.get(target);
  const pending = filled
    ? filled.then((double) => Object.assign(double as object, extra))
    : apiDouble(extra).then((double) => Object.assign(target, double));
  if (!filled) building.set(target, pending);
  return pending as Promise<Omit<ApiDouble, keyof Extra> & Extra>;
}
