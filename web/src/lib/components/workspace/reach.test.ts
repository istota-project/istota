import { describe, expect, it } from 'vitest';
import type { WorkspaceAccess, WorkspacePlace } from '$lib/api';
import {
  ACCESS_LABELS,
  accessVariant,
  accessLabel,
  placeForRef,
  reachesTasks,
  taskReachDescription,
  taskReachNote,
} from './reach';

function place(
  id: string,
  view_root: string | null,
  overrides: Partial<WorkspacePlace> = {},
): WorkspacePlace {
  return {
    id,
    kind: 'linked',
    label: id,
    browsable: true,
    view_root,
    reach: { private: 'rw', shared_member: 'rw', restricted: 'none', note: null },
    ...overrides,
  };
}

describe('reachesTasks', () => {
  it('is true for every access a task can use and false for none', () => {
    for (const access of ['rw', 'ro', 'skill', 'conditional'] as const) {
      expect(reachesTasks(access)).toBe(true);
    }
    expect(reachesTasks('none')).toBe(false);
    expect(reachesTasks('later' as WorkspaceAccess)).toBe(false);
  });
});

describe('taskReachDescription', () => {
  it('gives one sentence per reachable access and null otherwise', () => {
    expect(taskReachDescription('rw')).toBe('Istota can read and change these files');
    expect(taskReachDescription('ro')).toBe('Istota can read these files');
    expect(taskReachDescription('skill')).toBe('Istota reaches this through a skill');
    expect(taskReachDescription('conditional')).toBe('Istota’s access depends on the room');
    expect(taskReachDescription('none')).toBeNull();
    expect(taskReachDescription('later' as WorkspaceAccess)).toBeNull();
    expect(taskReachDescription('toString' as WorkspaceAccess)).toBeNull();
  });

  it('limits a room folder to that room', () => {
    expect(taskReachDescription('rw', 'room')).toBe(
      'Istota can read and change these files, in this room only',
    );
    expect(taskReachDescription('none', 'room')).toBeNull();
  });

  it('says how the rooms index and a group folder are limited', () => {
    expect(taskReachDescription('rw', 'rooms')).toBe(
      'Istota can read and change these files, each only in its own room',
    );
    expect(taskReachDescription('ro', 'group')).toBe(
      'Istota can read these files, in a shared room only when everyone there is in the group',
    );
  });
});

describe('accessLabel', () => {
  it('falls back to the raw value for an unknown or inherited key', () => {
    expect(accessLabel('rw')).toBe('Read-write');
    expect(accessLabel('later' as WorkspaceAccess)).toBe('later');
    expect(accessLabel('constructor' as WorkspaceAccess)).toBe('constructor');
  });
});

describe('ACCESS_LABELS', () => {
  it('labels every access kind', () => {
    expect(ACCESS_LABELS).toEqual({
      rw: 'Read-write',
      ro: 'Read-only',
      skill: 'Via skill',
      none: 'No access',
      conditional: 'Depends on room',
    });
  });
});

describe('placeForRef', () => {
  const home = place('home', '/Users/u', { kind: 'home' });
  const linked = place('linked:cfg:abc', '/Users/u/Projects/site');
  const short = place('linked:cfg:f', '/Users/u/f');
  const talk = place('talk', '/Talk', { kind: 'talk', browsable: false });
  const nextcloud = place('nextcloud', null, { kind: 'nextcloud' });
  const places = [home, linked, short, talk, nextcloud];

  it('matches a place by its exact view root', () => {
    expect(placeForRef(places, '/Users/u')).toBe(home);
    expect(placeForRef(places, '/Users/u/Projects/site')).toBe(linked);
  });

  it('picks the longest view root containing a nested ref', () => {
    expect(placeForRef(places, '/Users/u/Projects/site/docs/a.md')).toBe(linked);
    expect(placeForRef(places, '/Users/u/Projects/other.md')).toBe(home);
  });

  it('only matches a prefix that ends at a path separator', () => {
    expect(placeForRef(places, '/Users/u/foo')).toBe(home);
    expect(placeForRef([short], '/Users/u/foo')).toBeNull();
  });

  it('skips places that are not browsable', () => {
    expect(placeForRef(places, '/Talk/room/a.png')).toBeNull();
  });

  it('returns null when nothing contains the ref', () => {
    expect(placeForRef(places, '/Groups/g/a.md')).toBeNull();
    expect(placeForRef([], '/Users/u')).toBeNull();
  });
});

describe('accessVariant', () => {
  it('colours access from most to least reach', () => {
    expect(
      ['rw', 'ro', 'skill', 'conditional', 'none'].map((a) => accessVariant(a as WorkspaceAccess)),
    ).toEqual(['success', 'info', 'info', 'warn', 'danger']);
  });

  it('leaves an unknown access kind neutral', () => {
    expect(accessVariant('later' as WorkspaceAccess)).toBe('neutral');
    expect(accessVariant('toString' as WorkspaceAccess)).toBe('neutral');
  });
});

describe('taskReachNote', () => {
  it('names the bot and where the list reaches', () => {
    expect(taskReachNote('rw', 'Helper')).toBe(
      'Helper can read and change these files, in any of your rooms.',
    );
    expect(taskReachNote('ro', 'Helper', 'group')).toBe(
      'Helper can read these files, in a shared room only when everyone there is in the group.',
    );
    expect(taskReachNote('skill', 'Helper')).toBeNull();
    expect(taskReachNote('none', 'Helper', 'rooms')).toBeNull();
  });
});
