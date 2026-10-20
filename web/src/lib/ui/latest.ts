export interface Latest {
  /** Start a request; the returned check is true while no later one has started. */
  next(): () => boolean;
  /**
   * A check on the newest request without starting one, for work that rides on
   * it rather than replacing it: a page appended to its listing, a delete in
   * its folder, uploads that may overlap each other but not a room switch.
   */
  peek(): () => boolean;
  /**
   * End the current request without starting another (a room switch, a
   * logout, a cancel): every outstanding check turns false, and `next` and
   * `peek` work as before afterwards.
   */
  invalidate(): void;
  /**
   * On unmount: every outstanding check turns false, and so does every check
   * `next` or `peek` hands out afterwards. A latch rather than one more bump,
   * because a request started after an await (a refresh after a save) would
   * otherwise get a check that passes on the dead page.
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
  const checkFor = (mine: number) => () => !disposed && mine === seq;
  return {
    next() {
      seq += 1;
      return checkFor(seq);
    },
    peek() {
      return checkFor(seq);
    },
    invalidate() {
      seq += 1;
    },
    dispose() {
      disposed = true;
    },
  };
}
