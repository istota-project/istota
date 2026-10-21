import { describe, it, expect } from 'vitest';
import { changedFields } from './changedFields';

const loaded = {
  name: 'Ada',
  zone: 'America/Los_Angeles',
  addresses: ['a@example.com'],
  tags: [],
  routing: { alert: 'chat' },
  follow: false,
};

describe('changedFields', () => {
  it('sends only what the page edited', () => {
    const patch = changedFields({ ...loaded, name: 'Ada L' }, JSON.stringify(loaded));

    expect(patch).toEqual({ name: 'Ada L' });
  });

  it('leaves out a field the page never touched', () => {
    // The point of a patch: something else may have written `zone` since the
    // page loaded, and saving an unrelated toggle must not revert it.
    const patch = changedFields({ ...loaded, follow: true }, JSON.stringify(loaded));

    expect(patch).toEqual({ follow: true });
    expect('zone' in patch).toBe(false);
  });

  it('compares arrays and objects by value, not by identity', () => {
    const patch = changedFields(
      { ...loaded, addresses: ['a@example.com'], routing: { alert: 'chat' } },
      JSON.stringify(loaded),
    );

    expect(patch).toEqual({});
  });

  it('notices a changed array', () => {
    const patch = changedFields(
      { ...loaded, addresses: ['a@example.com', 'b@example.com'] },
      JSON.stringify(loaded),
    );

    expect(patch).toEqual({ addresses: ['a@example.com', 'b@example.com'] });
  });

  it('notices a field cleared to false', () => {
    const patch = changedFields(
      { ...loaded, follow: false },
      JSON.stringify({ ...loaded, follow: true }),
    );

    expect(patch).toEqual({ follow: false });
  });

  it('falls back to the full set when there is no snapshot', () => {
    expect(changedFields({ name: 'Ada' }, '')).toEqual({ name: 'Ada' });
  });

  it('falls back to the full set when the snapshot will not parse', () => {
    expect(changedFields({ name: 'Ada' }, '{not json')).toEqual({ name: 'Ada' });
  });
});
