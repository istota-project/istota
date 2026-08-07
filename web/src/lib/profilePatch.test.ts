import { describe, it, expect } from 'vitest';
import { changedProfileFields } from './profilePatch';

const loaded = {
  display_name: 'Ada',
  timezone: 'America/Los_Angeles',
  email_addresses: ['a@example.com'],
  disabled_modules: [],
  routing: { alert: 'talk' },
  timezone_follow_location: false,
};

describe('changedProfileFields', () => {
  it('sends only what the page edited', () => {
    const patch = changedProfileFields(
      { ...loaded, display_name: 'Ada L' },
      JSON.stringify(loaded),
    );

    expect(patch).toEqual({ display_name: 'Ada L' });
  });

  it('leaves out a timezone the page never touched', () => {
    // The whole point: the scheduler may have written `timezone` since this
    // page loaded, and saving an unrelated toggle must not revert it.
    const patch = changedProfileFields(
      { ...loaded, timezone_follow_location: true },
      JSON.stringify(loaded),
    );

    expect(patch).toEqual({ timezone_follow_location: true });
    expect('timezone' in patch).toBe(false);
  });

  it('does send a timezone the user did change', () => {
    const patch = changedProfileFields(
      { ...loaded, timezone: 'Europe/Warsaw' },
      JSON.stringify(loaded),
    );

    expect(patch).toEqual({ timezone: 'Europe/Warsaw' });
  });

  it('compares arrays and objects by value, not by identity', () => {
    const patch = changedProfileFields(
      {
        ...loaded,
        email_addresses: ['a@example.com'],
        routing: { alert: 'talk' },
      },
      JSON.stringify(loaded),
    );

    expect(patch).toEqual({});
  });

  it('notices a changed array', () => {
    const patch = changedProfileFields(
      { ...loaded, email_addresses: ['a@example.com', 'b@example.com'] },
      JSON.stringify(loaded),
    );

    expect(patch).toEqual({ email_addresses: ['a@example.com', 'b@example.com'] });
  });

  it('notices a field cleared to false', () => {
    const patch = changedProfileFields(
      { ...loaded, timezone_follow_location: false },
      JSON.stringify({ ...loaded, timezone_follow_location: true }),
    );

    expect(patch).toEqual({ timezone_follow_location: false });
  });

  it('falls back to the full set when there is no snapshot', () => {
    const patch = changedProfileFields({ display_name: 'Ada' }, '');

    expect(patch).toEqual({ display_name: 'Ada' });
  });

  it('falls back to the full set when the snapshot will not parse', () => {
    const patch = changedProfileFields({ display_name: 'Ada' }, '{not json');

    expect(patch).toEqual({ display_name: 'Ada' });
  });
});
