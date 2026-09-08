/**
 * Reading and writing the two halves of a delivery descriptor — the surface and,
 * for `web`, the room it names.
 *
 * The server's grammar is a comma list of `surface[:channel]` leaves
 * (`transport/routing.py:parse_output_target`). The settings page offers one
 * surface per route plus, for `web`, a room; everything else — a `talk:<token>`
 * set from the CLI, a `talk,email` pair — is offered back whole as its own
 * option, because there is no control that could put the other half back
 * (ISSUE-473).
 */

/** The value for the surface dropdown: `web` for a web route, the descriptor
 * itself for anything else. */
export function routeSurface(descriptor: string): string {
  return webRoute(descriptor) === null ? descriptor : 'web';
}

/** The room token a web route names, or '' — for a bare `web`, and for every
 * descriptor that is not a single web leaf. */
export function routeRoom(descriptor: string): string {
  return webRoute(descriptor) ?? '';
}

/** `descriptor` moved onto `surface`, keeping the room only while the surface
 * stays `web` — a room names nothing on any other surface, and every non-web
 * value the surface dropdown offers is already a whole descriptor. */
export function withSurface(descriptor: string, surface: string): string {
  return surface === 'web' ? joinDescriptor('web', routeRoom(descriptor)) : surface;
}

/** The descriptor for a surface and a room. A room without a surface is not a
 * route, and an unpinned room is the bare surface. */
export function joinDescriptor(surface: string, room: string): string {
  const s = (surface || '').trim();
  if (!s) return '';
  const r = (room || '').trim();
  return r ? `${s}:${r}` : s;
}

/** The room half of a single `web` leaf ('' when unpinned), or null when the
 * descriptor is not one. */
function webRoute(descriptor: string): string | null {
  const d = (descriptor || '').trim();
  if (d.includes(',')) return null;
  if (d === 'web') return '';
  return d.startsWith('web:') ? d.slice('web:'.length) : null;
}

/** One entry of a dropdown. Structurally `SelectOption`; spelled out here so a
 * plain data module does not import a component type. */
export interface RouteOption {
  value: string;
  label: string;
}

/** A room a `web:<token>` route may name, as the server reports it. */
export interface WebRoom {
  token: string;
  name: string;
  /** A bare `web` route lands here. */
  default: boolean;
  /** Somebody else is in it. */
  shared: boolean;
  /** The user's machine-owned log or alerts room. */
  channel: boolean;
}

/**
 * The surface dropdown for one route.
 *
 * `emptyValue`/`emptyLabel` is the leading no-op option; `talkLabel` spells out
 * where a bare `talk` resolves for this purpose (the logs room against the
 * alerts channel), which the bare word does not say. `omit` drops a surface the
 * purpose has no use for — the execution log omits `web`, because web chat
 * already shows a task's tool calls in the turn itself and the surface is
 * non-edit, so a log route there posts a second copy of the final summary
 * alone.
 *
 * A `current` that is not among the offered surfaces is kept as its own option:
 * a CLI-set `talk:<token>` or `talk,email`, or an omitted surface someone set
 * before it was withdrawn. Without that it would show as blank and be rewritten
 * on the next save.
 */
export function routeOptions(
  surfaces: string[],
  current: string,
  opts: {
    emptyValue?: string;
    emptyLabel?: string;
    talkLabel?: string;
    omit?: string[];
  } = {},
): RouteOption[] {
  const { emptyValue = '', emptyLabel = '(default)', talkLabel = 'talk', omit = [] } = opts;
  const offered = surfaces.filter((s) => !omit.includes(s));
  const out: RouteOption[] = [{ value: emptyValue, label: emptyLabel }];
  for (const s of offered) out.push({ value: s, label: s === 'talk' ? talkLabel : s });
  if (current && current !== emptyValue && !offered.includes(current))
    out.push({ value: current, label: current });
  return out;
}

/**
 * The room dropdown shown beside a `web` route.
 *
 * The leading option is the room a bare `web` lands in, named rather than left
 * as "wherever the server picks" — which was the complaint ISSUE-473 was filed
 * on. When the server names none the user has no room that qualifies and
 * delivery will make one, so the option says only "Default room".
 *
 * A shared or machine-owned room is offered and marked. The server refuses both
 * as the *implicit* default — an alert delivered into a room another person
 * reads is delivered in front of them — but pinning one is a deliberate choice,
 * and the mark is what makes it an informed one. A `current` the user can no
 * longer see (a room set from the CLI or config, since archived) is kept for the
 * same reason `routeOptions` keeps a withdrawn surface.
 */
export function webRoomOptions(rooms: WebRoom[], current: string): RouteOption[] {
  const fallback = rooms.find((r) => r.default);
  const out: RouteOption[] = [
    { value: '', label: fallback ? `Default room (${fallback.name})` : 'Default room' },
  ];
  for (const r of rooms) out.push({ value: r.token, label: webRoomLabel(r) });
  if (current && !rooms.some((r) => r.token === current))
    out.push({ value: current, label: current });
  return out;
}

function webRoomLabel(room: WebRoom): string {
  if (room.channel) return `${room.name} (bot's own channel)`;
  if (room.shared) return `${room.name} (shared)`;
  return room.name;
}
