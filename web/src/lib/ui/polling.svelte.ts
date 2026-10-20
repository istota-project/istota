import { onDocumentVisible } from './visibility';

export interface PollingOptions {
  /**
   * Stop while the document is hidden, and run once as soon as it is visible
   * again before resuming the interval. On by default: a hidden tab's timer is
   * throttled anyway, and the return is when a fresh reading is worth having.
   */
  pauseWhenHidden?: boolean;
}

/**
 * Call `fn` every `ms` until the returned function is called. The first call
 * is after `ms`, not immediately: every caller already loads once on mount.
 */
export function startPolling(
  fn: () => unknown,
  ms: number,
  { pauseWhenHidden = true }: PollingOptions = {},
): () => void {
  let timer: ReturnType<typeof setInterval> | undefined;
  const run = () => void fn();
  const start = () => {
    if (timer === undefined) timer = setInterval(run, ms);
  };
  const stop = () => {
    clearInterval(timer);
    timer = undefined;
  };

  if (!pauseWhenHidden || typeof document === 'undefined') {
    start();
    return stop;
  }

  const onHidden = () => {
    if (document.visibilityState === 'hidden') stop();
  };
  document.addEventListener('visibilitychange', onHidden);
  const offVisible = onDocumentVisible(() => {
    if (timer !== undefined) return;
    run();
    start();
  });
  if (document.visibilityState !== 'hidden') start();

  return () => {
    stop();
    document.removeEventListener('visibilitychange', onHidden);
    offVisible();
  };
}

/** `startPolling` for the life of the component that calls it. */
export function usePolling(fn: () => unknown, ms: number, options?: PollingOptions): void {
  $effect(() => startPolling(fn, ms, options));
}
