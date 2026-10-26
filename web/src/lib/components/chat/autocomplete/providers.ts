// Concrete completion providers + the shared per-session catalogue cache.
// commandProvider drives the bare `!command` trigger; modelAliasProvider drives
// the `!model <alias>` prefix. Both are fed by one cached GET /chat/commands.
// mentionProvider drives `@name`, fed by the open room's members from the page.

import { fetchChatCommands, type ChatCommands, type SelectableBrain } from '$lib/api';
import type { ModelChoices } from '../modelPicker';
import type { CompletionProvider, Suggestion } from './types';
import { codeRanges, inCode, mentionText } from '$lib/mentions';
import { latest } from '@minot/ui/ui/latest';

const EMPTY: ChatCommands = { commands: [], model_aliases: [], selectable_brains: [] };

let cataloguePromise: Promise<ChatCommands> | null = null;

/**
 * The names that may be sent while a turn is running (ISSUE-300), as a plain
 * snapshot of the last resolved catalogue.
 *
 * Both readers are synchronous — a `$derived` in the composer, a guard on the
 * store's send path — and neither can await a fetch. Empty until the catalogue
 * lands, which is the safe answer: it keeps the mode gate shut rather than
 * guessing at a name the server may not have.
 */
let commandNames: ReadonlySet<string> = new Set();

/** Advanced by every `loadCatalogue` and invalidated by `resetCommandCatalogue`,
 *  so a fetch that resolves after its session ended does not publish its names. */
const catalogueLoads = latest();

/** Every name `dispatch` will resolve to a command, lowercased: the registered
 *  names plus the hidden aliases (`inject` → `steer`, `yes` → `confirm`, …).
 *
 *  The aliases are here and *not* in the suggestion providers, which read
 *  `catalogue.commands` directly. That split is the whole point — the alias
 *  table is kept out of `!help` and autocomplete on purpose, but a name the
 *  server dispatches is still a command for routing, and conflating the two
 *  questions is what sent a mid-turn `!inject` into the send queue (ISSUE-350).
 *
 *  Model aliases are deliberately *not* excluded. The prefix the endpoint
 *  resolves ahead of command dispatch is the literal `!model <alias>`
 *  (`commands.parse_model_prefix`), not `!<alias>` — so an alias name shadows
 *  nothing, and filtering on it would only refuse a genuine command that
 *  happened to share a name with a role alias (`fast`, `smart`). `!model …`
 *  itself is refused by this set on its own terms: no command is registered
 *  under `model`, and that refusal is load-bearing rather than incidental —
 *  `!model opus <prompt>` creates a task, so it has to keep taking the turn
 *  path rather than being answered inline. */
function commandNamesOf(c: ChatCommands): ReadonlySet<string> {
  // Defensive about the payload's shape rather than trusting the type: this
  // runs inside `loadCatalogue`'s `.then`, which sits *after* its `.catch`, so
  // a throw here rejects the cached promise for the life of the session — and
  // `getSuggestions` and `getModelAliases` both await it bare. `command_aliases`
  // is the likeliest field to arrive misshapen, being the optional one a
  // mismatched server or proxy may omit or mangle.
  const names = new Set<string>();
  for (const x of Array.isArray(c.commands) ? c.commands : []) {
    if (typeof x?.name === 'string') names.add(x.name.toLowerCase());
  }
  for (const x of Array.isArray(c.command_aliases) ? c.command_aliases : []) {
    if (typeof x?.alias === 'string') names.add(x.alias.toLowerCase());
  }
  return names;
}

/** Fetch the command/alias catalogue once per session; a failure degrades to an
 *  empty catalogue (the popover simply never opens) and is cached so we don't
 *  hammer a down endpoint on every keystroke. */
function loadCatalogue(): Promise<ChatCommands> {
  if (!cataloguePromise) {
    // Which fetch this is. `resetCommandCatalogue` invalidates it, so an in-flight
    // fetch started before a teardown cannot assign over the names a fetch
    // started after it already published — which would leave the routing set
    // empty for the session and queue every command, `!stop` included.
    const current = catalogueLoads.next();
    cataloguePromise = fetchChatCommands()
      .catch((e) => {
        console.warn('command autocomplete: catalogue fetch failed', e);
        return EMPTY;
      })
      .then((c) => {
        if (current()) commandNames = commandNamesOf(c);
        return c;
      });
  }
  return cataloguePromise;
}

/** Drop the cached catalogue (call on session init/teardown to refetch). */
export function resetCommandCatalogue(): void {
  cataloguePromise = null;
  roomCatalogues.clear();
  commandNames = new Set();
  catalogueLoads.invalidate();
}

/** The command names, once the catalogue has landed — for a caller that holds
 *  its own reactive copy rather than reading the snapshot below. */
export async function loadCommandNames(): Promise<ReadonlySet<string>> {
  await loadCatalogue();
  return commandNames;
}

/**
 * Whether `text` opens with a `!command` the server registers.
 *
 * The name grammar follows the server's own parser (`commands.parse_command`):
 * `!` then word characters, lowercased. Not byte-for-byte — JS `\w` is ASCII
 * where Python's is Unicode, so `!steerü` extracts `steer` here and `steerü`
 * there. That divergence can only make this stricter than the server in
 * practice, since an unregistered name is answered inline too ("Unknown
 * command"), and the refusal it produces is the same one an unknown name gets.
 *
 * The catalogue is the only evidence available client-side, which is the real
 * reason an unregistered `!word` is refused while a turn runs — not that the
 * server would make a task of it. It would answer that one inline as well, so
 * what the refusal costs is latency: the message queues and is answered a turn
 * later. Since ISSUE-350 the catalogue carries the hidden aliases too, so that
 * gap is now only genuinely unknown names, and closing it would mean
 * reproducing the `!model <alias> <prompt>` grammar here — a second copy of a
 * server rule, which is the shape of the bug this set was widened to fix.
 *
 * `names` defaults to the module snapshot, which is what a synchronous caller
 * outside a reactive scope wants — and today that is the only use: `send()` is
 * the one non-test caller and passes no set of its own. The parameter is kept
 * for a caller holding a reactive copy; the composer used to be one, through
 * `isInlineCommand`, which went with the mode gate (ISSUE-238).
 */
export function isKnownCommand(text: string, names: ReadonlySet<string> = commandNames): boolean {
  const m = /^!(\w+)/.exec(text.trim());
  return m !== null && names.has(m[1].toLowerCase());
}

/** The active brain's model aliases (shared cache with the autocomplete), for
 *  the room-settings model picker. Degrades to [] on a failed fetch. */
export async function getModelAliases() {
  return (await loadCatalogue()).model_aliases;
}

/** Model aliases scoped to one room's own brain.
 *
 *  Its own small cache rather than a room key on `loadCatalogue`'s: that promise
 *  publishes `commandNames`, which the send path reads synchronously and which
 *  does not vary by room, so keying it would make the routing set depend on
 *  which room happened to fetch last. Nothing here touches those names.
 *
 *  Cleared by `resetCommandCatalogue` alongside the session catalogue, so a
 *  room whose brain changed on another device is re-read on the next session
 *  rather than for the life of the tab. Degrades to the unscoped catalogue,
 *  which is the answer this surface gave before rooms could pin a brain. */
// Keyed `<roomId>` for the room's own brain and `<roomId>:<kind>` for a brain
// the caller is considering (ISSUE-417), so a pending selection's models are
// cached beside the room's rather than replacing them — the settings modal
// switches back and forth as the user tries brains.
const roomCatalogues = new Map<string, Promise<ChatCommands>>();

function loadRoomCatalogue(roomId: number, brain?: string): Promise<ChatCommands> {
  const key = brain ? `${roomId}:${brain}` : String(roomId);
  let p = roomCatalogues.get(key);
  if (!p) {
    p = fetchChatCommands(roomId, brain).catch((e) => {
      console.warn('room model catalogue fetch failed', e);
      // Drop the entry before answering, or one blip pins the deployment
      // default to this room for the whole session — which for a room on
      // another brain means every id the picker offers is one the save
      // refuses. The fallback is for this call; the next one retries.
      roomCatalogues.delete(key);
      return loadCatalogue();
    });
    roomCatalogues.set(key, p);
  }
  return p;
}

/** Forget one room's model catalogue.
 *
 *  Called when a room's brain changes, which is the one thing that invalidates
 *  it: the aliases were resolved through the brain the room had at fetch time,
 *  and the session cache would otherwise go on offering them until teardown —
 *  so the modal's own "pick a new one after saving" caption sends the user
 *  back to a list the save then rejects with a 400. Deterministic rather than
 *  a race; the caption is what walks into it. */
export function dropRoomCatalogue(roomId: number): void {
  // Every key for this room, not just its unscoped one: a brain the user tried
  // in the modal left a `<roomId>:<kind>` entry behind, and those were resolved
  // through the brain the room had at fetch time exactly as the plain one was.
  const prefix = `${roomId}:`;
  for (const key of [...roomCatalogues.keys()]) {
    if (key === String(roomId) || key.startsWith(prefix)) roomCatalogues.delete(key);
  }
}

/** Brain kinds this user may pin to a room. Empty where the operator has listed
 *  none and empty for a non-admin — the server collapses the two deliberately,
 *  so the modal decides by emptiness alone. Shares the session catalogue with
 *  the autocomplete, and does not vary by room. */
export async function getSelectableBrains(): Promise<SelectableBrain[]> {
  const brains = (await loadCatalogue()).selectable_brains;
  // Defensive about the shape rather than trusting the type, for the reason
  // `commandNamesOf` is: an older server omits the field entirely.
  if (!Array.isArray(brains)) return [];
  return brains.filter(
    (b) =>
      typeof b?.kind === 'string' &&
      typeof b?.label === 'string' &&
      typeof b?.model_namespace === 'string',
  );
}

/** Every buildable kind's model namespace, and the brain an unpinned room
 *  inherits. Both are what the modal needs to decide whether a pending change
 *  crosses a namespace, and neither is answerable from `selectable_brains`
 *  alone: the brain being moved *away from* need not be on the menu. Answering
 *  "unknown" for it makes the modal disagree with the server's own clearing
 *  rule, in the direction that drops an edit the server would have kept. */
export async function getBrainNamespaces(): Promise<Record<string, string>> {
  const map = (await loadCatalogue()).brain_namespaces;
  if (!map || typeof map !== 'object' || Array.isArray(map)) return {};
  const out: Record<string, string> = {};
  for (const [kind, ns] of Object.entries(map)) {
    if (typeof kind === 'string' && typeof ns === 'string') out[kind] = ns;
  }
  return out;
}

export async function getInheritedBrain(): Promise<SelectableBrain | null> {
  const b = (await loadCatalogue()).inherited_brain;
  if (!b || typeof b.kind !== 'string' || typeof b.model_namespace !== 'string') {
    return null;
  }
  return b;
}

/** Everything the room-settings model picker offers, from one catalogue read:
 *  the aliases, the viewer's models (the whole catalogue for an admin, the
 *  enabled set otherwise, grouped by provider), what an unpinned room runs,
 *  and what to make of the room's stored model.
 *
 *  Scoped to the room's own brain (D5 Rule 2: a surface that offers a model
 *  name lists what the brain that would run it can run). `brain` overrides that
 *  with a kind the caller is *considering*: the room holds its old brain until
 *  the save lands, so the settings modal passes its pending selection
 *  (ISSUE-417). Defensive about the shape, since an older server sends no
 *  `models`. */
export async function getRoomModelChoices(roomId: number, brain?: string): Promise<ModelChoices> {
  const catalogue = await loadRoomCatalogue(roomId, brain);
  return {
    aliases: Array.isArray(catalogue.model_aliases) ? catalogue.model_aliases : [],
    models: Array.isArray(catalogue.models)
      ? catalogue.models.filter((m) => typeof m?.ref === 'string')
      : [],
    defaultModel: catalogue.default_model ?? null,
    roomModel: catalogue.room_model ?? null,
  };
}

/** Prefix matches first, then substring matches; input order preserved within
 *  each group (the catalogue is already sorted server-side). */
function rank<T>(items: T[], query: string, keyOf: (item: T) => string): T[] {
  const q = query.toLowerCase();
  if (!q) return items;
  const prefix: T[] = [];
  const substr: T[] = [];
  for (const item of items) {
    const k = keyOf(item).toLowerCase();
    if (k.startsWith(q)) prefix.push(item);
    else if (k.includes(q)) substr.push(item);
  }
  return [...prefix, ...substr];
}

export function commandProvider(): CompletionProvider {
  return {
    id: 'command',
    match(text, caret) {
      const before = text.slice(0, caret);
      const m = /^!(\w*)$/.exec(before);
      if (!m) return null;
      // Extend the replaceable range over any word tail past the caret so a
      // mid-token accept replaces the whole command name, not just the prefix.
      const tail = /^\w*/.exec(text.slice(caret))![0];
      return { query: m[1], range: [0, caret + tail.length] };
    },
    async getSuggestions(query): Promise<Suggestion[]> {
      const { commands } = await loadCatalogue();
      return rank(commands, query, (c) => c.name).map((c) => ({
        value: `!${c.name} `,
        label: `!${c.name}`,
        description: c.help,
        key: `cmd:${c.name}`,
      }));
    },
  };
}

export function modelAliasProvider(): CompletionProvider {
  return {
    id: 'model-alias',
    match(text, caret) {
      const before = text.slice(0, caret);
      const m = /^(!model\s+)(\S*)$/.exec(before);
      if (!m) return null;
      const start = m[1].length;
      const tail = /^\S*/.exec(text.slice(caret))![0];
      return { query: m[2], range: [start, caret + tail.length] };
    },
    async getSuggestions(query): Promise<Suggestion[]> {
      const { model_aliases } = await loadCatalogue();
      return rank(model_aliases, query, (a) => a.alias).map((a) => ({
        value: `${a.alias} `,
        label: a.alias,
        // Show the canonical model the alias resolves to (with effort) in
        // parens, so opaque role aliases like `smart` are legible.
        description: a.target ? `(${a.target}${a.effort ? ` · ${a.effort}` : ''})` : '',
        key: `model:${a.alias}`,
      }));
    },
  };
}

/** Someone the composer may offer after `@`: the name the mention is written
 *  with (a user id, or the bot's name) and what to show beside it. */
export interface MentionCandidate {
  name: string;
  display?: string;
  /** The viewer, who is never offered: nobody mentions themselves. */
  self?: boolean;
}

/**
 * `@name` suggestions (ISSUE-580).
 *
 * Takes its list from the caller rather than fetching, because the page
 * already holds the open room's members for rendering mentions (ISSUE-578)
 * and the two have to be one list: a suggestion inserting a name the renderer
 * would not match leaves plain text behind. The page passes an empty list for
 * a private room, where the only candidate would be the bot, so the popover
 * never opens there.
 *
 * Fires on an `@` at the start of the text or after whitespace, through to the
 * caret. Not inside backtick code, where the renderer would not match either,
 * and not in a `!command`, which is answered as a command and never shown as a
 * message. The query takes Unicode letters, so a display name like "Zoë" can
 * be typed into without the list closing.
 *
 * Stays closed once the name is typed in full and nothing else could still
 * match, so a message ending `… @istota` sends on Enter. Ending on the bot's
 * name is how a turn addresses it under the speech gate's mention mode, and
 * an Enter that only completed the name would cost a second press every time.
 */
export function mentionProvider(candidates: () => readonly MentionCandidate[]): CompletionProvider {
  return {
    id: 'mention',
    match(text, caret) {
      if (text.trimStart().startsWith('!')) return null;
      const before = text.slice(0, caret);
      const m = /(^|\s)@([\p{L}\p{N}\p{M}_.-]*)$/u.exec(before);
      if (!m) return null;
      const at = m.index + m[1].length;
      if (insideCode(before, at)) return null;
      const others = candidates().filter((c) => !c.self);
      if (others.length === 0) return null;
      const tail = /^[\p{L}\p{N}\p{M}_.-]*/u.exec(text.slice(caret))![0];
      const q = m[2].toLowerCase();
      if (!tail && q && others.some((c) => c.name.toLowerCase() === q)) {
        const longer = others.some(
          (c) =>
            c.name.toLowerCase() !== q &&
            [c.name, c.display ?? ''].some((k) => k.toLowerCase().startsWith(q)),
        );
        if (!longer) return null;
      }
      return { query: m[2], range: [at, caret + tail.length] };
    },
    getSuggestions(query): Suggestion[] {
      const q = query.toLowerCase();
      const prefix: MentionCandidate[] = [];
      const substr: MentionCandidate[] = [];
      for (const c of candidates()) {
        if (c.self) continue;
        const keys = [c.name, c.display ?? ''].map((k) => k.toLowerCase());
        if (!q || keys.some((k) => k.startsWith(q))) prefix.push(c);
        else if (keys.some((k) => k.includes(q))) substr.push(c);
      }
      return [...prefix, ...substr].map((c) => ({
        value: `${mentionText(c.name)} `,
        label: mentionText(c.name),
        description: c.display && c.display !== c.name ? c.display : undefined,
        // Encoded: the key becomes an element id that `aria-activedescendant`
        // names, and a bot name or user id may hold a space.
        key: `mention:${encodeURIComponent(c.name)}`,
      }));
    },
  };
}

/** Whether position `at` in `text` is in backtick code, closed or still being
 *  typed: a closed span by `codeRanges`, an open one by a backtick before `at`
 *  that no closed span accounts for. */
function insideCode(text: string, at: number): boolean {
  const ranges = codeRanges(text);
  if (inCode(ranges, at)) return true;
  for (let i = 0; i < at; i++) {
    if (text[i] === '`' && !inCode(ranges, i)) return true;
  }
  return false;
}
