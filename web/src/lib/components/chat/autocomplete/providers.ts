// Concrete completion providers + the shared per-session catalogue cache.
// commandProvider drives the bare `!command` trigger; modelAliasProvider drives
// the `!model <alias>` prefix. Both are fed by one cached GET /chat/commands.

import { fetchChatCommands, type ChatCommands } from '$lib/api';
import type { CompletionProvider, Suggestion } from './types';

const EMPTY: ChatCommands = { commands: [], model_aliases: [] };

let cataloguePromise: Promise<ChatCommands> | null = null;

/** Fetch the command/alias catalogue once per session; a failure degrades to an
 *  empty catalogue (the popover simply never opens) and is cached so we don't
 *  hammer a down endpoint on every keystroke. */
function loadCatalogue(): Promise<ChatCommands> {
	if (!cataloguePromise) {
		cataloguePromise = fetchChatCommands().catch((e) => {
			console.warn('command autocomplete: catalogue fetch failed', e);
			return EMPTY;
		});
	}
	return cataloguePromise;
}

/** Drop the cached catalogue (call on session init/teardown to refetch). */
export function resetCommandCatalogue(): void {
	cataloguePromise = null;
}

/** The active brain's model aliases (shared cache with the autocomplete), for
 *  the room-settings model picker. Degrades to [] on a failed fetch. */
export async function getModelAliases() {
	return (await loadCatalogue()).model_aliases;
}

/** Base model choices for the room-default picker: one `{value: canonical id,
 *  label: alias}` per distinct model, effort-suffixed aliases excluded (effort
 *  is a separate control). When several aliases map to one model, a provider
 *  alias (its name appears in the canonical id, e.g. `opus` in `claude-opus-4-8`)
 *  is preferred over a role alias like `smart`, so the label reads naturally.
 *  The room header badge and the settings dropdown both consume this, so they
 *  never disagree on how a model is named. Insertion order = first-seen. */
export async function getBaseModelChoices(): Promise<{ value: string; label: string }[]> {
	const labelByTarget = new Map<string, string>();
	for (const a of await getModelAliases()) {
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
				description: a.target
					? `(${a.target}${a.effort ? ` · ${a.effort}` : ''})`
					: '',
				key: `model:${a.alias}`,
			}));
		},
	};
}
