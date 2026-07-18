// Trigger-agnostic autocomplete engine. Owns the active provider, the resolved
// suggestions, the highlighted index, and open/closed state; the composer feeds
// it text+caret on every keystroke/caret move and forwards keydowns. Reactive
// state lives here (a `.svelte.ts` runes module) so a component reading the
// getters re-renders when the engine updates.

import type { CompletionProvider, Suggestion, TriggerMatch } from './types';

export interface AcceptResult {
	text: string;
	caret: number;
}

export interface AutocompleteOptions {
	/** Fired when a suggestion is accepted (keyboard or mouse). */
	onAccept?: (result: AcceptResult) => void;
}

export interface Autocomplete {
	readonly open: boolean;
	readonly suggestions: Suggestion[];
	readonly activeIndex: number;
	/** Recompute the active provider + suggestions from the textarea state. */
	sync(text: string, caret: number): void;
	/** Handle a keydown while the popover may be open. Returns true if consumed
	 *  (caller must preventDefault + skip its own handling). */
	onKeydown(e: KeyboardEvent): boolean;
	/** Apply a suggestion (default = activeIndex). Fires onAccept + closes.
	 *  Returns the spliced {text, caret}, or null when nothing is applicable. */
	accept(index?: number): AcceptResult | null;
	/** Move the highlight (mouse hover). */
	setActive(index: number): void;
	close(): void;
}

export function createAutocomplete(
	providers: CompletionProvider[],
	opts: AutocompleteOptions = {},
): Autocomplete {
	let open = $state(false);
	let suggestions = $state<Suggestion[]>([]);
	let activeIndex = $state(0);

	// Non-reactive request/context bookkeeping.
	let currentText = '';
	let currentMatch: TriggerMatch | null = null;
	let requestSeq = 0; // stale-guard token for async getSuggestions
	// Text at which the popover was dismissed with Escape; suppresses reopen
	// until the text actually changes.
	let suppressedText: string | null = null;

	function reset() {
		open = false;
		suggestions = [];
		activeIndex = 0;
		currentMatch = null;
	}

	function apply(list: Suggestion[]) {
		if (list.length === 0) {
			reset();
			return;
		}
		suggestions = list;
		activeIndex = 0;
		open = true;
	}

	function sync(text: string, caret: number) {
		currentText = text;

		// Escape suppression: don't reopen for the exact text we dismissed on.
		if (suppressedText !== null && text === suppressedText) {
			reset();
			return;
		}
		suppressedText = null;

		let match: TriggerMatch | null = null;
		for (const p of providers) {
			match = p.match(text, caret);
			if (match) {
				currentMatch = match;
				const seq = ++requestSeq;
				const res = p.getSuggestions(match.query);
				if (res instanceof Promise) {
					// Optimistically no change until it resolves; drop if superseded.
					res.then((list) => {
						if (seq === requestSeq) apply(list);
					}).catch(() => {
						if (seq === requestSeq) reset();
					});
				} else {
					apply(res);
				}
				return;
			}
		}
		// No provider matched.
		requestSeq++; // invalidate any in-flight async result
		reset();
	}

	function accept(index?: number): AcceptResult | null {
		if (!open || !currentMatch) return null;
		const i = index ?? activeIndex;
		const s = suggestions[i];
		if (!s) return null;
		const [start, end] = currentMatch.range;
		const text = currentText.slice(0, start) + s.value + currentText.slice(end);
		const caret = start + s.value.length;
		close();
		const result = { text, caret };
		opts.onAccept?.(result);
		return result;
	}

	function close() {
		reset();
	}

	function onKeydown(e: KeyboardEvent): boolean {
		if (!open) return false;
		switch (e.key) {
			case 'ArrowDown':
				activeIndex = (activeIndex + 1) % suggestions.length;
				return true;
			case 'ArrowUp':
				activeIndex = (activeIndex - 1 + suggestions.length) % suggestions.length;
				return true;
			case 'Tab':
			case 'Enter':
				accept();
				return true;
			case 'Escape':
				suppressedText = currentText;
				reset();
				return true;
			default:
				return false;
		}
	}

	function setActive(index: number) {
		if (index >= 0 && index < suggestions.length) activeIndex = index;
	}

	return {
		get open() {
			return open;
		},
		get suggestions() {
			return suggestions;
		},
		get activeIndex() {
			return activeIndex;
		},
		sync,
		onKeydown,
		accept,
		setActive,
		close,
	};
}
