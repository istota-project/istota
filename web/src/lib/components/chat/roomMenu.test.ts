import { describe, it, expect, vi } from 'vitest';
import { roomMenuItems } from './roomMenu';

function actions() {
  return { settings: vi.fn(), memory: vi.fn(), notes: vi.fn(), toggleListed: vi.fn() };
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

  it('a hidden email thread offers to show it in the room list', () => {
    const a = actions();
    const items = roomMenuItems({ email_thread: true, listed: false }, a);
    const item = items.find((i) => i.label === 'Show in room list');
    expect(item).toBeTruthy();
    item!.onSelect();
    expect(a.toggleListed).toHaveBeenCalledOnce();
  });

  it('a listed email thread offers to hide it again', () => {
    const items = roomMenuItems({ email_thread: true, listed: true }, actions());
    const labels = items.map((i) => i.label);
    expect(labels).toContain('Hide from room list');
    expect(labels).not.toContain('Show in room list');
  });

  it('any other room offers neither', () => {
    const labels = roomMenuItems({ shared: false }, actions()).map((i) => i.label);
    expect(labels).not.toContain('Show in room list');
    expect(labels).not.toContain('Hide from room list');
  });
});
