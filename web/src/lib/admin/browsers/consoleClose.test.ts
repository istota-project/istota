import { describe, expect, it } from 'vitest';
import { describeClose } from './consoleClose';

const CLOSED = 'The console closed.';

describe('describeClose', () => {
  it.each([
    [1000, CLOSED, true],
    [1005, CLOSED, true],
    [1011, CLOSED, true],
    [4400, 'The console could not start. Try again.', true],
    [4401, 'Your session ended. Sign in again to reopen the console.', false],
    [4403, 'You need to be signed in as an admin to open a console.', false],
    [4404, 'This browser is not running.', false],
    [4408, 'The console closed after an hour. Reconnect to keep going.', true],
    [4429, 'Too many consoles are open. Close one and try again.', true],
  ])('maps %i', (code, message, retry) => {
    expect(describeClose(code, true)).toEqual({ message, retry });
  });

  it('names the proxy for a 1006 before ok', () => {
    expect(describeClose(1006, false)).toEqual({
      message:
        'The console could not connect. If Istota is behind your own proxy, it must forward websocket connections.',
      retry: true,
    });
  });

  it('reads a 1006 after ok as a closed console', () => {
    expect(describeClose(1006, true)).toEqual({ message: CLOSED, retry: true });
  });

  it('falls back to a closed console with retry for an unknown code', () => {
    expect(describeClose(4999, true)).toEqual({ message: CLOSED, retry: true });
    expect(describeClose(4999, false)).toEqual({ message: CLOSED, retry: true });
  });
});
