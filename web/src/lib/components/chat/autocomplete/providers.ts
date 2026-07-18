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
				description: a.target ?? '',
				key: `model:${a.alias}`,
			}));
		},
	};
}
