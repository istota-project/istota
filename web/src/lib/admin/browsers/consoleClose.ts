/**
 * What the page says when the browser console's websocket closes, by close
 * code. The relay's side of the table is `CLOSE_CODES` in
 * `src/istota/webui/admin_browser_console.py`; the two are kept in step by hand.
 *
 * `opened` is whether the relay had sent `ok`: a 1006 before it is a refused
 * upgrade (often an outer proxy that does not forward websockets), after it a
 * dropped network.
 */

export const CLOSE_NOT_RUNNING = 4404;

export interface ConsoleClose {
  message: string;
  retry: boolean;
}

const CLOSED = 'The console closed.';

const BY_CODE: Record<number, ConsoleClose> = {
  1000: { message: CLOSED, retry: true },
  1005: { message: CLOSED, retry: true },
  1011: { message: CLOSED, retry: true },
  4400: { message: 'The console could not start. Try again.', retry: true },
  4401: { message: 'Your session ended. Sign in again to reopen the console.', retry: false },
  4403: { message: 'You need to be signed in as an admin to open a console.', retry: false },
  [CLOSE_NOT_RUNNING]: { message: 'This browser is not running.', retry: false },
  4408: { message: 'The console closed after an hour. Reconnect to keep going.', retry: true },
  4429: { message: 'Too many consoles are open. Close one and try again.', retry: true },
};

export function describeClose(code: number, opened: boolean): ConsoleClose {
  if (code === 1006) {
    if (opened) return { message: CLOSED, retry: true };
    return {
      message:
        'The console could not connect. If Istota is behind your own proxy, it must forward websocket connections.',
      retry: true,
    };
  }
  return BY_CODE[code] ?? { message: CLOSED, retry: true };
}
