import { describe, it, expect, vi } from 'vitest';
import { roomMenuItems } from './roomMenu';

function actions() {
  return { settings: vi.fn(), memory: vi.fn(), notes: vi.fn() };
}

describe('roomMenuItems', () => {
  it('a shared room offers Room notes and My notes', () => {
    const a = actions();
    const items = roomMenuItems({ shared: true, has_my_notes: false }, a);
    expect(items.map((i) => i.label)).toEqual(['Settings', 'Room notes', 'My notes']);
    items[1].onSelect();
    items[2].onSelect();
    expect(a.memory).toHaveBeenCalledOnce();
    expect(a.notes).toHaveBeenCalledOnce();
  });

  it('a private room offers the single Memory item', () => {
    const items = roomMenuItems({ shared: false, has_my_notes: false }, actions());
    expect(items.map((i) => i.label)).toEqual(['Settings', 'Memory']);
  });

  it('a room the user keeps notes about still offers them after it stops being shared', () => {
    const items = roomMenuItems({ shared: false, has_my_notes: true }, actions());
    expect(items.map((i) => i.label)).toContain('My notes');
  });
});
