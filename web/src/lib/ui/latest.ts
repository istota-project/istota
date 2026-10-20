export interface Latest {
  /** Start a request; the returned check is true while no later one has started. */
  next(): () => boolean;
  /** Make every outstanding check false, without starting a request (on unmount). */
  invalidate(): void;
}

/**
 * The stale-response guard. A slower response landing after a quicker, later
 * one would otherwise overwrite it:
 *
 *   const loads = latest();
 *   async function load() {
 *     const current = loads.next();
 *     const body = await fetchIt();
 *     if (!current()) return;
 *     …
 *   }
 */
export function latest(): Latest {
  let seq = 0;
  return {
    next() {
      const mine = ++seq;
      return () => mine === seq;
    },
    invalidate() {
      seq += 1;
    },
  };
}
