export interface Latest {
  /** Start a request; the returned check is true while no later one has started. */
  next(): () => boolean;
  /**
   * On unmount: every outstanding check turns false, and so does every check
   * `next` hands out afterwards. A latch rather than one more bump, because a
   * request started after an await (a refresh after a save) would otherwise
   * get a check that passes on the dead page.
   */
  dispose(): void;
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
  let disposed = false;
  return {
    next() {
      const mine = ++seq;
      return () => !disposed && mine === seq;
    },
    dispose() {
      disposed = true;
    },
  };
}
