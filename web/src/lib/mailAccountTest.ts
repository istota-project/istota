import { getMailAccountTest, requestMailAccountTest, type MailAccountTestResult } from '$lib/api';

// The daemon takes a request on its next tick and a sign-in can take its own
// timeout, so two minutes is generous; past it the daemon is not answering.
export const POLL_MS = 2000;
export const WAIT_MS = 120_000;

/** `null` when the daemon did not answer within `WAIT_MS`. */
export type MailAccountTestOutcome = MailAccountTestResult | null;

/**
 * Ask the daemon to test one account and poll until it answers or `WAIT_MS`
 * passes. Errors from either call propagate, and so does a request the server
 * no longer knows, since nothing will ever answer it.
 */
export async function waitForMailAccountTest(accountId: number): Promise<MailAccountTestOutcome> {
  const { request_id } = await requestMailAccountTest(accountId);
  const until = Date.now() + WAIT_MS;
  while (Date.now() < until) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    const state = await getMailAccountTest(request_id);
    if (state.status === 'answered') return state.result;
    if (state.status === 'unknown') throw new Error('The test request was lost. Try again.');
  }
  return null;
}
