import { describe, expect, it } from 'vitest';
import { createConfirm } from './confirm.svelte';

describe('createConfirm', () => {
  it('is closed with no target until asked', () => {
    const confirm = createConfirm<{ id: number }>();
    expect(confirm.target).toBeNull();
    expect(confirm.open).toBe(false);
  });

  it('opens on ask and keeps the very object it was given', () => {
    const confirm = createConfirm<{ id: number }>();
    const row = { id: 7 };
    confirm.ask(row);
    expect(confirm.open).toBe(true);
    // Not a proxy: a page compares rows by identity.
    expect(confirm.target).toBe(row);
  });

  it('drops the target on cancel and when open is set false (a bound dialog closing)', () => {
    const confirm = createConfirm<string>();
    confirm.ask('a');
    confirm.cancel();
    expect(confirm.target).toBeNull();
    confirm.ask('b');
    confirm.open = false;
    expect(confirm.target).toBeNull();
    expect(confirm.open).toBe(false);
  });

  it('ignores open = true, which has no target to open on', () => {
    const confirm = createConfirm<string>();
    confirm.open = true;
    expect(confirm.open).toBe(false);
  });
});
