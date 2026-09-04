// Concrete completion providers + the shared per-session catalogue cache.
// commandProvider drives the bare `!command` trigger; modelAliasProvider drives
// the `!model <alias>` prefix. Both are fed by one cached GET /chat/commands.

import { fetchChatCommands, type ChatCommands, type SelectableBrain } from '$lib/api';
import type { CompletionProvider, Suggestion } from './types';

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

/** Bumped by every `loadCatalogue` and by `resetCommandCatalogue`, so a fetch
 *  that resolves after its session ended does not publish its names. */
let catalogueGeneration = 0;

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
    // Which fetch this is. `resetCommandCatalogue` bumps it, so an in-flight
    // fetch started before a teardown cannot assign over the names a fetch
    // started after it already published — which would leave the routing set
    // empty for the session and queue every command, `!stop` included.
    const gen = ++catalogueGeneration;
    cataloguePromise = fetchChatCommands()
      .catch((e) => {
        console.warn('command autocomplete: catalogue fetch failed', e);
        return EMPTY;
      })
      .then((c) => {
        if (gen === catalogueGeneration) commandNames = commandNamesOf(c);
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
  catalogueGeneration++;
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
const roomCatalogues = new Map<number, Promise<ChatCommands>>();

function loadRoomCatalogue(roomId: number): Promise<ChatCommands> {
  let p = roomCatalogues.get(roomId);
  if (!p) {
    p = fetchChatCommands(roomId).catch((e) => {
      console.warn('room model catalogue fetch failed', e);
      return loadCatalogue();
    });
    roomCatalogues.set(roomId, p);
  }
  return p;
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
    (b) => typeof b?.kind === 'string' && typeof b?.model_namespace === 'string',
  );
}

/** Base model choices for the room-default picker: one `{value: canonical id,
 *  label: alias}` per distinct model, effort-suffixed aliases excluded (effort
 *  is a separate control). When several aliases map to one model, a provider
 *  alias (its name appears in the canonical id, e.g. `opus` in `claude-opus-4-8`)
 *  is preferred over a role alias like `smart`, so the label reads naturally.
 *  The room header badge and the settings dropdown both consume this, so they
 *  never disagree on how a model is named. Insertion order = first-seen.
 *
 *  `roomId` scopes the list to that room's own brain (D5 Rule 2: a surface that
 *  offers a model name lists the aliases of the brain that would have to run
 *  it). Omit it for the deployment default. */
export async function getBaseModelChoices(
  roomId?: number,
): Promise<{ value: string; label: string }[]> {
  const catalogue = roomId === undefined ? await loadCatalogue() : await loadRoomCatalogue(roomId);
  const labelByTarget = new Map<string, string>();
  for (const a of catalogue.model_aliases ?? []) {
    if (!a.target || a.effort !== null) continue;
    const cur = labelByTarget.get(a.target);
    if (cur === undefined) labelByTarget.set(a.target, a.alias);
    else if (!a.target.includes(cur) && a.target.includes(a.alias)) {
      labelByTarget.set(a.target, a.alias);
    }
  }
  return [...labelByTarget].map(([value, label]) => ({ value, label }));
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
