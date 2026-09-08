import { describe, it, expect } from 'vitest';
import {
  hasRoom,
  joinDescriptor,
  routeOptions,
  routeRoom,
  routeSurface,
  talkRoomOptions,
  webRoomOptions,
  withSurface,
} from './deliveryDescriptor';

const SURFACES = ['talk', 'email', 'ntfy', 'web'];
const values = (opts: { value: string }[]) => opts.map((o) => o.value);
const labels = (opts: { label: string }[]) => opts.map((o) => o.label);

describe('routeSurface', () => {
  it('reads the surface out of a web route so the room can be picked separately', () => {
    expect(routeSurface('web')).toBe('web');
    expect(routeSurface('web:web-alice-1')).toBe('web');
  });

  it('reads it out of a talk route too, which now has a picker of its own', () => {
    // ISSUE-475: `talk:<token>` used to be kept whole because nothing in the UI
    // could put the token back. The alert and log rows can now, so splitting it
    // is what makes the pinned conversation visible instead of raw.
    expect(routeSurface('talk')).toBe('talk');
    expect(routeSurface('talk:9erk494s')).toBe('talk');
  });

  it('leaves every other descriptor whole', () => {
    expect(routeSurface('email')).toBe('email');
    expect(routeSurface('ntfy:high')).toBe('ntfy:high');
    expect(routeSurface('talk,email')).toBe('talk,email');
    expect(routeSurface('')).toBe('');
  });
});

describe('routeRoom', () => {
  it('reads the room out of a roomed route, and nothing else', () => {
    expect(routeRoom('web:web-alice-1')).toBe('web-alice-1');
    expect(routeRoom('talk:9erk494s')).toBe('9erk494s');
    expect(routeRoom('web')).toBe('');
    expect(routeRoom('talk')).toBe('');
    expect(routeRoom('ntfy:high')).toBe('');
    expect(routeRoom('web:a,email')).toBe('');
  });
});

describe('joinDescriptor', () => {
  it('writes a bare surface when no room is pinned', () => {
    expect(joinDescriptor('web', '')).toBe('web');
    expect(joinDescriptor('web', '   ')).toBe('web');
  });

  it('writes surface:room when one is', () => {
    expect(joinDescriptor('web', 'web-alice-1')).toBe('web:web-alice-1');
  });

  it('is empty for an empty surface, whatever the room says', () => {
    // Clearing the surface clears the route; a room with nothing to deliver to
    // is not a route.
    expect(joinDescriptor('', 'web-alice-1')).toBe('');
  });

  it('round-trips what routeSurface and routeRoom read', () => {
    for (const d of ['web', 'web:web-alice-1', 'talk', 'talk:9erk494s']) {
      expect(joinDescriptor(routeSurface(d), routeRoom(d))).toBe(d);
    }
  });
});

describe('withSurface', () => {
  it('keeps the pinned room while the surface stays put', () => {
    expect(withSurface('web:web-alice-1', 'web')).toBe('web:web-alice-1');
    expect(withSurface('talk:9erk494s', 'talk')).toBe('talk:9erk494s');
  });

  it('drops it when the surface moves to one with no room', () => {
    // ntfy has no room, so carrying the token would write `ntfy:web-alice-1` —
    // a channel the surface would try to resolve and fail on.
    expect(withSurface('web:web-alice-1', 'ntfy')).toBe('ntfy');
    expect(withSurface('talk:9erk494s', 'email')).toBe('email');
  });

  it('drops it when the surface moves between the two roomed ones', () => {
    // A room token addresses one surface: a web room token names nothing on
    // Talk, and a Nextcloud conversation id names no row in the web registry.
    expect(withSurface('web:web-alice-1', 'talk')).toBe('talk');
    expect(withSurface('talk:9erk494s', 'web')).toBe('web');
  });

  it('takes an unroomed value whole, since the dropdown offers whole descriptors', () => {
    expect(withSurface('web', 'talk,email')).toBe('talk,email');
    expect(withSurface('talk', 'none')).toBe('none');
    expect(withSurface('talk', '')).toBe('');
  });

  it('arrives at a bare roomed surface from one that had no room', () => {
    expect(withSurface('ntfy', 'web')).toBe('web');
    expect(withSurface('', 'web')).toBe('web');
    expect(withSurface('email', 'talk')).toBe('talk');
  });
});

describe('routeOptions', () => {
  it('leads with the no-op option and then every surface', () => {
    expect(values(routeOptions(SURFACES, ''))).toEqual(['', 'talk', 'email', 'ntfy', 'web']);
  });

  it('omits a surface the purpose has no use for', () => {
    // The execution log omits `web`: web chat already shows a task's tool calls
    // in the turn itself, and the surface is non-edit, so a log route there
    // posts a second copy of the final summary alone.
    const opts = routeOptions(SURFACES, 'talk', {
      emptyValue: 'none',
      emptyLabel: '(off)',
      omit: ['web'],
    });
    expect(values(opts)).toEqual(['none', 'talk', 'email', 'ntfy']);
  });

  it('still offers a value that is no longer among the surfaces', () => {
    // Someone set `web` for the log before it was withdrawn, or set a
    // `talk:<token>` from the CLI. Dropping it here would show the control
    // blank and rewrite the route on the next save.
    expect(values(routeOptions(SURFACES, 'web', { omit: ['web'] }))).toContain('web');
    expect(values(routeOptions(SURFACES, 'talk:9erk494s'))).toContain('talk:9erk494s');
    expect(values(routeOptions(SURFACES, 'talk,email'))).toContain('talk,email');
  });

  it('does not repeat the no-op option as a kept value', () => {
    expect(values(routeOptions(SURFACES, 'none', { emptyValue: 'none' }))).toEqual([
      'none',
      'talk',
      'email',
      'ntfy',
      'web',
    ]);
  });

  it('labels talk with the bare word, leaving the room to the room picker', () => {
    // It used to read `talk (alerts channel)` / `talk (logs channel)`, because
    // nothing else said which conversation a bare `talk` meant. The room
    // dropdown beside it says exactly that now, so the two selects sat side by
    // side stating one fact twice (ISSUE-475).
    expect(labels(routeOptions(SURFACES, ''))).toEqual([
      '(default)',
      'talk',
      'email',
      'ntfy',
      'web',
    ]);
  });
});

describe('webRoomOptions', () => {
  const rooms = [
    { token: 'web-alice-general', name: 'general', default: true, shared: false, channel: false },
    { token: 'web-alice-ideas', name: 'ideas', default: false, shared: false, channel: false },
  ];

  it('names the room a bare web route lands in', () => {
    // "Wherever the server picks" was the complaint the issue was filed on.
    expect(labels(webRoomOptions(rooms, ''))[0]).toBe('Default room (general)');
  });

  it('says only "Default room" when the server named none', () => {
    // A user with no qualifying room: delivery provisions one, so there is no
    // name to give yet.
    expect(labels(webRoomOptions([], ''))).toEqual(['Default room']);
  });

  it('lists the rooms after it', () => {
    expect(values(webRoomOptions(rooms, ''))).toEqual(['', 'web-alice-general', 'web-alice-ideas']);
  });

  it('keeps a pinned room the user can no longer see', () => {
    expect(values(webRoomOptions(rooms, 'web-alice-archived'))).toContain('web-alice-archived');
  });

  it('marks it unavailable when the server says that room is dead', () => {
    // ISSUE-478: a delivery pinned to an archived, deleted or hidden room lands
    // where nothing renders it and reports success. Keeping the value is right;
    // letting it read like the named rooms above it is not.
    const opts = webRoomOptions(rooms, 'web-alice-archived', ['web-alice-archived']);
    expect(opts[opts.length - 1]).toEqual({
      value: 'web-alice-archived',
      label: 'web-alice-archived (unavailable)',
    });
  });

  it('leaves a pin the server did not name as a bare token', () => {
    // The load-bearing control. Absence from `rooms` is three states, not one:
    // this list is handle-driven, so a room with no handle yet and one with a
    // stale archived handle are both missing from it and both deliver — and an
    // empty list is also what a failed lookup returns. Marking on absence would
    // call a working route broken, which is what the Talk picker was spared.
    expect(labels(webRoomOptions(rooms, 'web-alice-nohandle'))).toContain('web-alice-nohandle');
    expect(labels(webRoomOptions([], 'web-alice-general'))).toEqual([
      'Default room',
      'web-alice-general',
    ]);
  });

  it('marks nothing when the pinned room is one of the offered ones', () => {
    // The other control: a room the server named cannot also be offered, so the
    // mark must never reach an option built from `rooms`.
    expect(labels(webRoomOptions(rooms, 'web-alice-ideas', ['web-alice-ideas']))).toEqual([
      'Default room (general)',
      'general',
      'ideas',
    ]);
  });
});

describe('webRoomOptions marks', () => {
  it('a room somebody else reads, and the ones the bot owns', () => {
    // Both are refused as the implicit default; pinning one is allowed, and the
    // mark is what makes that an informed choice rather than a surprise.
    const marked = [
      { token: 't1', name: 'team', default: false, shared: true, channel: false },
      { token: 't2', name: 'logs', default: false, shared: false, channel: true },
    ];
    expect(labels(webRoomOptions(marked, ''))).toEqual([
      'Default room',
      'team (shared)',
      "logs (bot's own channel)",
    ]);
  });

  it('tells two same-named rooms apart by a piece of their tokens', () => {
    // Two rooms may legitimately carry one name — a promoted room and its Talk
    // twin, or two the user simply called the same thing (ISSUE-474).
    const twins = [
      { token: 'web-alice-aaa111', name: 'notes', default: false, shared: false, channel: false },
      { token: 'web-alice-bbb222', name: 'notes', default: false, shared: false, channel: false },
    ];
    expect(labels(webRoomOptions(twins, '')).slice(1)).toEqual([
      'notes (aaa111)',
      'notes (bbb222)',
    ]);
  });

  it('leaves a name alone when its mark already tells the two apart', () => {
    const twins = [
      { token: 'web-alice-aaa111', name: 'notes', default: false, shared: false, channel: false },
      { token: 'web-alice-bbb222', name: 'notes', default: false, shared: true, channel: false },
    ];
    expect(labels(webRoomOptions(twins, '')).slice(1)).toEqual(['notes', 'notes (shared)']);
  });

  it('disambiguates the default room too, since naming it is the whole point', () => {
    const twins = [
      { token: 'web-alice-aaa111', name: 'notes', default: true, shared: false, channel: false },
      { token: 'web-alice-bbb222', name: 'notes', default: false, shared: false, channel: false },
    ];
    expect(labels(webRoomOptions(twins, ''))[0]).toBe('Default room (notes, aaa111)');
  });

  it('keeps the mark beside the token fragment when both are needed', () => {
    const twins = [
      { token: 'web-alice-aaa111', name: 'notes', default: false, shared: true, channel: false },
      { token: 'web-alice-bbb222', name: 'notes', default: false, shared: true, channel: false },
    ];
    expect(labels(webRoomOptions(twins, '')).slice(1)).toEqual([
      'notes (shared, aaa111)',
      'notes (shared, bbb222)',
    ]);
  });
});

describe('talkRoomOptions', () => {
  const rooms = [
    { token: 'alerts-tok', name: 'Alerts channel', channel: true },
    { token: 'logs-tok', name: 'Logs channel', channel: true },
    { token: 'conv-team', name: 'team', channel: false },
  ];

  it('leads with the label the purpose gives a bare talk', () => {
    // Unlike web, a bare `talk` does not resolve to one room for every purpose —
    // alerts land in the alerts channel and the log in the logs one — so the
    // leading option takes the row's own wording rather than naming a room.
    expect(labels(talkRoomOptions(rooms, '', 'Alerts channel (default)'))[0]).toBe(
      'Alerts channel (default)',
    );
  });

  it('lists the conversations after it', () => {
    expect(values(talkRoomOptions(rooms, '', 'Default'))).toEqual([
      '',
      'alerts-tok',
      'logs-tok',
      'conv-team',
    ]);
  });

  it('marks the ones the bot provisioned, as the web picker marks its own', () => {
    expect(labels(talkRoomOptions(rooms, '', 'Default')).slice(1)).toEqual([
      "Alerts channel (bot's own channel)",
      "Logs channel (bot's own channel)",
      'team',
    ]);
  });

  it('keeps a pinned conversation that is not among them', () => {
    // An operator-set token for a conversation the registry has not seen. The
    // save refuses a *new* one, but the row must still show what is stored.
    expect(values(talkRoomOptions(rooms, 'conv-old', 'Default'))).toContain('conv-old');
  });

  it('leaves it a bare token, since that conversation delivers perfectly well', () => {
    // The other half of ISSUE-478's mark, and the reason it is the web
    // picker's rather than `roomOptions`': here an absent conversation is the
    // expected operator-set case, so the same mark would call a working route
    // broken.
    const opts = talkRoomOptions(rooms, 'conv-old', 'Default');
    expect(opts[opts.length - 1]).toEqual({ value: 'conv-old', label: 'conv-old' });
  });

  it('tells two same-named conversations apart by a piece of their tokens', () => {
    const twins = [
      { token: 'conv-aaa111', name: 'notes', channel: false },
      { token: 'conv-bbb222', name: 'notes', channel: false },
    ];
    expect(labels(talkRoomOptions(twins, '', 'Default')).slice(1)).toEqual([
      'notes (aaa111)',
      'notes (bbb222)',
    ]);
  });

  it('falls back to the whole token when the fragments collide too', () => {
    // A hint is the last six characters and a Nextcloud conversation id is
    // eight, so two ids sharing a tail is reachable rather than theoretical —
    // and two identical labels is the one outcome the disambiguation exists to
    // prevent.
    const twins = [
      { token: 'ab123456', name: 'notes', channel: false },
      { token: 'cd123456', name: 'notes', channel: false },
    ];
    expect(labels(talkRoomOptions(twins, '', 'Default')).slice(1)).toEqual([
      'notes (ab123456)',
      'notes (cd123456)',
    ]);
  });
});

describe('hasRoom', () => {
  it('is true for the surfaces whose descriptor names a room', () => {
    for (const d of ['web', 'web:tok', 'talk', 'talk:9erk494s']) {
      expect(hasRoom(d)).toBe(true);
    }
  });

  it('is false for everything else, so those rows show the surface alone', () => {
    // The page asks this instead of restating `['web', 'talk']`, so a third
    // roomed surface cannot arrive in the lib and render no picker.
    for (const d of ['', 'email', 'ntfy', 'ntfy:high', 'none', 'talk,email']) {
      expect(hasRoom(d)).toBe(false);
    }
  });
});
