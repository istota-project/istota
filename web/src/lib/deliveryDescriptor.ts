/**
 * Reading and writing the two halves of a delivery descriptor — the surface and,
 * on the surfaces that have rooms, the room it names.
 *
 * The server's grammar is a comma list of `surface[:channel]` leaves
 * (`transport/routing.py:parse_output_target`). The settings page offers one
 * surface per route plus, for `web` and `talk`, a room; everything else — a
 * `talk,email` pair, an `ntfy:<topic>` — is offered back whole as its own
 * option, because there is no control that could put the other half back
 * (ISSUE-473, ISSUE-475).
 */

/** The surfaces whose descriptor carries a room the page can pick.
 *
 * `talk` joined `web` here in ISSUE-475: it was kept whole while nothing in the
 * UI could put a split-off token back, and the alert and log rows now can.
 * `email` and `ntfy` are not rooms — an `ntfy:<topic>` is a topic name with no
 * list to offer — so they stay whole. */
const ROOMED_SURFACES = ['web', 'talk'];

/** The value for the surface dropdown: the surface for a single roomed leaf,
 * the descriptor itself for anything else. */
export function routeSurface(descriptor: string): string {
  return roomedLeaf(descriptor)?.surface ?? descriptor;
}

/** The room token a roomed route names, or '' — for a bare `web` / `talk`, and
 * for every descriptor that is not a single roomed leaf. */
export function routeRoom(descriptor: string): string {
  return roomedLeaf(descriptor)?.room ?? '';
}

/** Whether this route lands in a room the page can offer a picker for. The
 * predicate lives here rather than in the page for the reason the option lists
 * do: it is one of the rules about the grammar, and a second spelling beside
 * `ROOMED_SURFACES` would leave a third roomed surface rendering no picker. */
export function hasRoom(descriptor: string): boolean {
  return roomedLeaf(descriptor) !== null;
}

/** `descriptor` moved onto `surface`, keeping the room only while the surface
 * does not change — a web room token names nothing on Talk, a Nextcloud
 * conversation id names no row in the web registry, and the unroomed surfaces
 * have nowhere to put either. Every other value the dropdown offers is already
 * a whole descriptor. */
export function withSurface(descriptor: string, surface: string): string {
  if (!ROOMED_SURFACES.includes(surface)) return surface;
  const room = routeSurface(descriptor) === surface ? routeRoom(descriptor) : '';
  return joinDescriptor(surface, room);
}

/** The descriptor for a surface and a room. A room without a surface is not a
 * route, and an unpinned room is the bare surface. */
export function joinDescriptor(surface: string, room: string): string {
  const s = (surface || '').trim();
  if (!s) return '';
  const r = (room || '').trim();
  return r ? `${s}:${r}` : s;
}

/** The surface and room of a single roomed leaf, or null when the descriptor is
 * not one. */
function roomedLeaf(descriptor: string): { surface: string; room: string } | null {
  const d = (descriptor || '').trim();
  if (d.includes(',')) return null;
  for (const surface of ROOMED_SURFACES) {
    if (d === surface) return { surface, room: '' };
    if (d.startsWith(`${surface}:`)) return { surface, room: d.slice(surface.length + 1) };
  }
  return null;
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

/** A conversation a `talk:<token>` route may name, as the server reports it.
 *
 * No `default` flag, unlike a web room: a bare `talk` resolves per purpose —
 * alerts to the alerts channel, the log to the logs one — so which conversation
 * it means is the row's business, and each row says so in its surface label. */
export interface TalkRoom {
  token: string;
  name: string;
  /** One of the two the bot provisioned for this user. */
  channel: boolean;
}

/**
 * The surface dropdown for one route.
 *
 * `emptyValue`/`emptyLabel` is the leading no-op option. `omit` drops a surface
 * the purpose has no use for — the execution log omits `web`, because web chat
 * already shows a task's tool calls in the turn itself and the surface is
 * non-edit, so a log route there posts a second copy of the final summary
 * alone.
 *
 * Every surface is labelled by its own name. `talk` used to be spelled out per
 * purpose — `talk (alerts channel)`, `talk (logs channel)` — because the bare
 * word did not say which conversation it meant, and there was no other control
 * that could. The room dropdown beside it is that control now and its leading
 * option carries the same sentence, so the parenthetical said it twice in two
 * adjacent selects (ISSUE-475).
 *
 * A `current` that is not among the offered surfaces is kept as its own option:
 * a `talk,email` pair, or an omitted surface someone set before it was
 * withdrawn. Without that it would show as blank and be rewritten on the next
 * save.
 */
export function routeOptions(
  surfaces: string[],
  current: string,
  opts: {
    emptyValue?: string;
    emptyLabel?: string;
    omit?: string[];
  } = {},
): RouteOption[] {
  const { emptyValue = '', emptyLabel = '(default)', omit = [] } = opts;
  const offered = surfaces.filter((s) => !omit.includes(s));
  const out: RouteOption[] = [{ value: emptyValue, label: emptyLabel }];
  for (const s of offered) out.push({ value: s, label: s });
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
 * and the mark is what makes it an informed one. A `current` that is not among
 * them is kept for the same reason `routeOptions` keeps a withdrawn surface.
 *
 * `unavailable` is the server's list of this profile's own pins that will
 * swallow a delivery, and a `current` on it is marked (ISSUE-478). It is a
 * parameter rather than a test of `rooms`, because absence from `rooms` is not
 * evidence: that list is handle-driven and degrades to `[]` on a database
 * error, so a room with no handle yet, one with a stale archived handle, and a
 * failed lookup all look identical to a missing room from here — and marking on
 * absence would call two working routes broken, which is the thing the Talk
 * picker was deliberately spared. See `_unavailable_web_room_pins`.
 *
 * Two rooms may legitimately carry one name — a promoted room and its Talk twin,
 * or two the user simply called the same thing — so a label that would otherwise
 * repeat gets a piece of its token (ISSUE-474). Only where it repeats: a token
 * fragment beside every name is noise, and the marks already tell some pairs
 * apart on their own.
 *
 * `emptyLabel` overrides the leading option, and one caller needs it: the
 * default room picker itself (ISSUE-477), where naming the default room as the
 * way to leave the default room unset would be circular. Everywhere else the
 * default is what this function works out.
 *
 * `ignored` is that same picker's pin when the server says it is not being
 * honoured, and it takes a different mark from `unavailable` (ISSUE-479). Two
 * different things are being reported: an unavailable *route* swallows the
 * delivery, while an ignored `default_room` means the pin does nothing and the
 * delivery lands somewhere else, visibly, through the heuristic. The server
 * answers them with separate predicates for the same reason — see
 * `_ignored_default_room_pin`. It is one token rather than a list because one
 * profile has one `default_room`.
 *
 * The two inputs are **mutually exclusive at today's call sites** — the route
 * rows pass `unavailable` and no `ignored`, the default room picker the reverse —
 * so the ordering below decides nothing yet. It is written `ignored` first
 * anyway, because that is the answer a caller passing both would want: the
 * field's own verdict on its own pin outranks a route's verdict on the same
 * token. Kept as a guard rather than removed, so a later caller that does pass
 * both gets the right label instead of whichever branch happened to be first.
 */
export function webRoomOptions(
  rooms: WebRoom[],
  current: string,
  emptyLabel?: string,
  unavailable: string[] = [],
  ignored = '',
): RouteOption[] {
  const label = (token: string) =>
    token && token === ignored
      ? ignoredRoomLabel(token)
      : unavailable.includes(token)
        ? unavailableRoomLabel(token)
        : token;
  return roomOptions(rooms, current, emptyLabel ?? defaultRoomLabel(rooms), webRoomMarks, label);
}

/**
 * The conversation dropdown shown beside a `talk` route (ISSUE-475).
 *
 * `emptyLabel` is the leading option, and the caller supplies it because a bare
 * `talk` does not mean one conversation for every purpose the way a bare `web`
 * means one room: alerts fall to the alerts channel and the execution log to
 * the logs one. Each row already spells that out in its surface label, so this
 * repeats the row's own wording rather than naming a room the server picked.
 *
 * A conversation the bot provisioned is marked, on the same reasoning as the
 * web picker's machine-owned rooms: pinning one is allowed and worth knowing
 * about. There is no `shared` mark — a Talk conversation is shared by
 * definition, and marking every entry says nothing.
 *
 * A `current` the list does not carry is kept, as `routeOptions` keeps a
 * withdrawn surface: an operator-set token for a conversation the room registry
 * has not seen must stay visible and editable rather than showing blank.
 */
export function talkRoomOptions(
  rooms: TalkRoom[],
  current: string,
  emptyLabel: string,
): RouteOption[] {
  return roomOptions(rooms, current, emptyLabel, talkRoomMarks);
}

/** The shared body of the two room dropdowns: a leading option, then one entry
 * per room, then whatever is currently pinned if the list does not carry it.
 *
 * The two passes are what disambiguates: a label that would otherwise appear
 * twice gets a piece of its token, and only such a label does — a fragment
 * beside every name is noise (ISSUE-474). The hinted label is then checked
 * again, because a hint is a slice rather than the whole token and two
 * conversations can share a tail — a Nextcloud id is eight characters, so the
 * six-character hint leaves a genuine collision available — and a label that is
 * still ambiguous falls back to the token, which is unique by definition. */
function roomOptions<T extends { token: string; name: string }>(
  rooms: T[],
  current: string,
  emptyLabel: string,
  marksOf: (room: T) => string[],
  unknownLabel: (token: string) => string = (token) => token,
): RouteOption[] {
  const label = (r: T, extra?: string) => roomLabel(r, extra ? [...marksOf(r), extra] : marksOf(r));

  const plain = tally(rooms.map((r) => label(r)));
  const hinted = tally(rooms.map((r) => label(r, tokenHint(r.token))));

  const out: RouteOption[] = [{ value: '', label: emptyLabel }];
  for (const r of rooms) {
    if ((plain.get(label(r)) ?? 0) < 2) out.push({ value: r.token, label: label(r) });
    else if ((hinted.get(label(r, tokenHint(r.token))) ?? 0) < 2)
      out.push({ value: r.token, label: label(r, tokenHint(r.token)) });
    else out.push({ value: r.token, label: label(r, r.token) });
  }
  if (current && !rooms.some((r) => r.token === current))
    out.push({ value: current, label: unknownLabel(current) });
  return out;
}

/** The label for a pinned web room the server has told us is dead — the user is
 * a member and the room is archived, gone, or one they hid, so a delivery
 * pinned there lands in a transcript no surface renders and reports success
 * (ISSUE-478).
 *
 * The option is still kept and still editable, for the reason `routeOptions`
 * keeps a withdrawn surface: dropping it would blank the field and rewrite the
 * route on the next save, losing the only trace of where the alerts were going.
 * What it must not do is read like the rooms above it — a bare token is what it
 * said before, and a token is not a name, so it does not tell a user which room
 * this is, let alone that it is broken. There is no name to offer instead: the
 * room is not in `web_rooms` precisely because it is not offerable.
 *
 * Applied only to a token the server named, never to any `current` the offered
 * list happens to lack — see `webRoomOptions`. Talk takes no equivalent: a
 * conversation absent from its list is the expected operator-set case and
 * delivers perfectly well (ISSUE-475), so the mark there would say something
 * false about a working route. */
function unavailableRoomLabel(token: string): string {
  return `${token} (unavailable)`;
}

/** The label for a `default_room` pin the server has told us is not being
 * honoured — the room is archived, gone, or no longer the user's, so every
 * reader discards the value and the bare-`web` heuristic answers instead
 * (ISSUE-479).
 *
 * Deliberately **not** `(unavailable)`, which is the neighbour's word for a
 * route. That mark says the delivery is swallowed: it goes nowhere and the user
 * sees nothing. This one says the opposite — the delivery arrives, in whichever
 * room the heuristic picks, and what has stopped working is the setting. Reusing
 * the route's wording would tell a user their alerts were being lost when they
 * are merely landing somewhere they did not choose.
 *
 * The option is kept and editable for the same reason the route mark's is:
 * dropping it blanks the field and rewrites the setting on the next save,
 * losing the only trace of which room was pinned. There is no name to show
 * beside the token — the room is absent from `web_rooms` precisely because it is
 * not offerable. A pin that is merely hidden gets no mark at all: the next
 * delivery un-hides the room, so it is working. */
function ignoredRoomLabel(token: string): string {
  return `${token} (ignored)`;
}

function tally(labels: string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const l of labels) counts.set(l, (counts.get(l) ?? 0) + 1);
  return counts;
}

/** The leading option: the room a bare `web` route lands in, named.
 *
 * It carries no mark — the server refuses a shared or machine-owned room as the
 * *implicit* default — so the only thing that can make it ambiguous is another
 * room of the same name, and the fragment goes beside the name rather than in a
 * second bracket. */
function defaultRoomLabel(rooms: WebRoom[]): string {
  const fallback = rooms.find((r) => r.default);
  if (!fallback) return 'Default room';
  const twin = rooms.some((r) => r.token !== fallback.token && r.name === fallback.name);
  const name = twin ? `${fallback.name}, ${tokenHint(fallback.token)}` : fallback.name;
  return `Default room (${name})`;
}

/** What the room is, beyond its name. At most one — a channel room is the bot's
 * whether or not anyone else is in it. */
function webRoomMarks(room: WebRoom): string[] {
  if (room.channel) return ["bot's own channel"];
  if (room.shared) return ['shared'];
  return [];
}

/** The same, for a Talk conversation. Only the one mark: every Talk
 * conversation is shared, so a `shared` mark on all of them says nothing. */
function talkRoomMarks(room: TalkRoom): string[] {
  return room.channel ? ["bot's own channel"] : [];
}

/** The tail of a room token. The tail rather than the head because a web room's
 * token is `web-<user>-<random>` — the head is the same for every room the user
 * has, and only the tail tells them apart. A Talk-backed room's token is an
 * opaque Nextcloud id with no such structure, where any slice would do. */
function tokenHint(token: string): string {
  return token.slice(-6);
}

function roomLabel(room: { name: string }, marks: string[]): string {
  return marks.length ? `${room.name} (${marks.join(', ')})` : room.name;
}
