import { describe, expect, it } from 'vitest';
import { createDraft, trackChanges } from './draft.svelte';

type Prefs = { name: string; tags: string[]; routing: Record<string, string> };

const loaded = (): Prefs => ({ name: 'Ada', tags: ['a'], routing: { alert: 'talk' } });

describe('createDraft', () => {
  it('starts clean and turns dirty on an edit, nested ones included', () => {
    const draft = createDraft(loaded());
    expect(draft.dirty).toBe(false);
    draft.value.name = 'Grace';
    expect(draft.dirty).toBe(true);
    draft.value.name = 'Ada';
    expect(draft.dirty).toBe(false);
    draft.value.tags.push('b');
    expect(draft.dirty).toBe(true);
  });

  it('is dirty when a field is rebound to a different array or object', () => {
    const draft = createDraft(loaded());
    draft.value.routing = { alert: 'email' };
    expect(draft.dirty).toBe(true);
  });

  it('reports only the fields that differ from the saved copy', () => {
    const draft = createDraft(loaded());
    draft.value.tags = ['a', 'b'];
    expect(draft.changed()).toEqual({ tags: ['a', 'b'] });
  });

  it('diffs a record the caller shapes against the same saved copy', () => {
    // The settings layout sends a normalised subset of the profile, not the
    // whole draft, and must still send only what differs.
    const draft = createDraft(loaded());
    expect(draft.changed({ name: 'Ada', tags: ['a', 'c'] })).toEqual({ tags: ['a', 'c'] });
  });

  it('reset() takes the current value as saved', () => {
    const draft = createDraft(loaded());
    draft.value.name = 'Grace';
    draft.reset();
    expect(draft.dirty).toBe(false);
    expect(draft.value.name).toBe('Grace');
    expect(draft.saved.name).toBe('Grace');
  });

  it('reset(next) replaces the value and the saved copy together', () => {
    const draft = createDraft(loaded());
    draft.value.name = 'Grace';
    draft.reset({ name: 'Linus', tags: [], routing: {} });
    expect(draft.value.name).toBe('Linus');
    expect(draft.dirty).toBe(false);
    draft.value.tags.push('x');
    expect(draft.dirty).toBe(true);
  });

  it('keeps no reference to the saved copy', () => {
    const next = loaded();
    const draft = createDraft(loaded());
    draft.reset(next);
    next.name = 'mutated outside';
    expect(draft.dirty).toBe(false);
    expect(draft.saved.name).toBe('Ada');
  });

  it('holds a value that is not loaded yet as clean', () => {
    const draft = createDraft<Prefs | null>(null);
    expect(draft.dirty).toBe(false);
    expect(draft.changed()).toEqual({});
    draft.reset(loaded());
    expect(draft.dirty).toBe(false);
  });
});

describe('trackChanges', () => {
  it('follows a value derived from several pieces of state', () => {
    let height = $state('178');
    let unit = $state<'cm' | 'ft_in'>('cm');
    const tracker = trackChanges(() => ({ height, unit }));
    expect(tracker.dirty).toBe(false);
    height = '180';
    expect(tracker.dirty).toBe(true);
    tracker.mark();
    expect(tracker.dirty).toBe(false);
    expect(tracker.saved).toEqual({ height: '180', unit: 'cm' });
    unit = 'ft_in';
    expect(tracker.changed()).toEqual({ unit: 'ft_in' });
  });
});
