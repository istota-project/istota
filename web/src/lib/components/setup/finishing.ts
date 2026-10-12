/**
 * The Finishing screen's wait for the finished stack.
 *
 * Once `/finish` answers, the setup server exits and `istota-stack` installs
 * what it wrote and starts the stack, with nothing left to report progress.
 * The page polls the address the stack will answer at instead.
 */

export const POLL_INTERVAL_MS = 3000;
/** After this long the screen says so and names what to run on the server. */
export const LONG_WAIT_MS = 15 * 60 * 1000;

/** The login page, with the email filled in for the person who just set up. */
export function loginTarget(loginUrl: string, email: string): string {
  if (!email) return loginUrl;
  const join = loginUrl.includes('?') ? '&' : '?';
  return `${loginUrl}${join}email=${encodeURIComponent(email)}`;
}

/**
 * Whether the stack answers at `url`.
 *
 * Another origin answers opaquely, so any answer there counts. The same origin
 * is the case where the setup page was opened at the final address: there the
 * setup server, until it exits, answers the login page with a 404, which must
 * not count as the stack being up.
 */
export async function stackAnswers(url: string): Promise<boolean> {
  try {
    const resp = await fetch(url, { mode: 'no-cors', cache: 'no-store', credentials: 'omit' });
    return resp.type === 'opaque' || resp.ok;
  } catch {
    return false;
  }
}

/** Where the browser goes when the stack is up; a seam for the tests. */
export const browserNavigation = {
  assign(url: string): void {
    window.location.assign(url);
  },
};
