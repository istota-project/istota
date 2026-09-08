import { describe, it, expect } from 'vitest';
import {
  joinDescriptor,
  routeOptions,
  routeRoom,
  routeSurface,
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

  it('leaves every other descriptor whole', () => {
    // A `talk:<token>` set from the CLI is offered back as its own option. If
    // this returned the surface alone the token would be dropped on re-save —
    // the settings page has no Talk room picker to put it back.
    expect(routeSurface('talk')).toBe('talk');
    expect(routeSurface('talk:9erk494s')).toBe('talk:9erk494s');
    expect(routeSurface('talk,email')).toBe('talk,email');
    expect(routeSurface('')).toBe('');
  });
});

describe('routeRoom', () => {
  it('reads the room out of a web route, and nothing else', () => {
    expect(routeRoom('web:web-alice-1')).toBe('web-alice-1');
    expect(routeRoom('web')).toBe('');
    expect(routeRoom('talk:9erk494s')).toBe('');
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
    for (const d of ['web', 'web:web-alice-1']) {
      expect(joinDescriptor(routeSurface(d), routeRoom(d))).toBe(d);
    }
  });
});

describe('withSurface', () => {
  it('keeps the pinned room while the surface stays web', () => {
    expect(withSurface('web:web-alice-1', 'web')).toBe('web:web-alice-1');
  });

  it('drops it when the surface moves off web', () => {
    // ntfy has no room, so carrying the token would write `ntfy:web-alice-1` —
    // a channel the surface would try to resolve and fail on.
    expect(withSurface('web:web-alice-1', 'ntfy')).toBe('ntfy');
    expect(withSurface('web:web-alice-1', 'talk')).toBe('talk');
  });

  it('takes a non-web value whole, since the dropdown offers whole descriptors', () => {
    expect(withSurface('web', 'talk:9erk494s')).toBe('talk:9erk494s');
    expect(withSurface('talk', 'none')).toBe('none');
    expect(withSurface('talk', '')).toBe('');
  });

  it('arrives at a bare web from a surface that had no room', () => {
    expect(withSurface('ntfy', 'web')).toBe('web');
    expect(withSurface('', 'web')).toBe('web');
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

  it('spells out where a bare talk lands for this purpose', () => {
    // `talk` alone does not say whether it means the logs room or the alerts
    // channel, and the two are different rooms.
    expect(labels(routeOptions(SURFACES, '', { talkLabel: 'talk (logs channel)' }))).toContain(
      'talk (logs channel)',
    );
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
