import { describe, it, expect } from 'vitest';
import type { ChatRoom } from '$lib/api';
import { sortRoomsByActivity, touchRoomActivity } from './roomOrder';

function room(id: number, token: string, last_activity?: string): ChatRoom {
  return {
    id,
    token,
    name: token,
    archived: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    last_activity,
  };
}

describe('sortRoomsByActivity', () => {
  it('puts the most recently active room first', () => {
    const sorted = sortRoomsByActivity([
      room(1, 'a', '2026-05-01T00:00:00Z'),
      room(2, 'b', '2026-07-01T00:00:00Z'),
      room(3, 'c', '2026-06-01T00:00:00Z'),
    ]);
    expect(sorted.map((r) => r.token)).toEqual(['b', 'c', 'a']);
  });

  it('does not mutate the array it was given', () => {
    const input = [room(1, 'a', '2026-05-01T00:00:00Z'), room(2, 'b', '2026-07-01T00:00:00Z')];
    sortRoomsByActivity(input);
    expect(input.map((r) => r.token)).toEqual(['a', 'b']);
  });

  it('keeps server order for equal stamps', () => {
    const sorted = sortRoomsByActivity([
      room(1, 'a', '2026-05-01T00:00:00Z'),
      room(2, 'b', '2026-05-01T00:00:00Z'),
    ]);
    expect(sorted.map((r) => r.token)).toEqual(['a', 'b']);
  });

  it('sinks stampless rooms to the bottom in the order they arrived', () => {
    // An older backend sends no `last_activity`; the list must still be the
    // server's rather than reversed by a missing key comparing as newest.
    const sorted = sortRoomsByActivity([
      room(1, 'a'),
      room(2, 'b', '2026-05-01T00:00:00Z'),
      room(3, 'c'),
    ]);
    expect(sorted.map((r) => r.token)).toEqual(['b', 'a', 'c']);
  });
});

describe('touchRoomActivity', () => {
  it('lifts the named room to the top', () => {
    const next = touchRoomActivity(
      [room(1, 'a', '2026-05-01T00:00:00Z'), room(2, 'b', '2026-07-01T00:00:00Z')],
      'a',
      '2026-08-01T00:00:00Z',
    );
    expect(next.map((r) => r.token)).toEqual(['a', 'b']);
    expect(next[0].last_activity).toBe('2026-08-01T00:00:00Z');
  });

  it('never moves a room backwards on a late or replayed row', () => {
    // Gap recovery and the reconnect replay both redeliver older rows; a stamp
    // that went backwards would demote a room the user just spoke in.
    const input = [room(1, 'a', '2026-07-01T00:00:00Z'), room(2, 'b', '2026-06-01T00:00:00Z')];
    const next = touchRoomActivity(input, 'a', '2026-01-01T00:00:00Z');
    expect(next).toBe(input);
    expect(next[0].last_activity).toBe('2026-07-01T00:00:00Z');
  });

  it('stamps a room that had none', () => {
    const next = touchRoomActivity(
      [room(1, 'a'), room(2, 'b', '2026-06-01T00:00:00Z')],
      'a',
      '2026-07-01T00:00:00Z',
    );
    expect(next.map((r) => r.token)).toEqual(['a', 'b']);
  });

  it('is a no-op for an unknown token or a missing stamp', () => {
    const input = [room(1, 'a', '2026-05-01T00:00:00Z')];
    expect(touchRoomActivity(input, 'nope', '2026-08-01T00:00:00Z')).toBe(input);
    expect(touchRoomActivity(input, 'a', undefined)).toBe(input);
  });

  it('leaves the object identity of every other room alone', () => {
    const b = room(2, 'b', '2026-06-01T00:00:00Z');
    const next = touchRoomActivity(
      [room(1, 'a', '2026-05-01T00:00:00Z'), b],
      'a',
      '2026-08-01T00:00:00Z',
    );
    expect(next.find((r) => r.token === 'b')).toBe(b);
  });
});
