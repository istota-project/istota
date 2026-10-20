export interface Debounced<A extends unknown[]> {
  (...args: A): void;
  /** Drop a pending call. Call it on unmount, or the call lands on a dead page. */
  cancel(): void;
}

/**
 * `fn` called once `ms` after the last call, with that call's arguments.
 * Each copy this replaced kept its own timer variable, and two of them never
 * cleared it on unmount; the timer lives here, so `cancel` is the whole cleanup.
 */
export function debounce<A extends unknown[]>(fn: (...args: A) => void, ms: number): Debounced<A> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const debounced = (...args: A) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = undefined;
      fn(...args);
    }, ms);
  };
  debounced.cancel = () => {
    clearTimeout(timer);
    timer = undefined;
  };
  return debounced;
}
