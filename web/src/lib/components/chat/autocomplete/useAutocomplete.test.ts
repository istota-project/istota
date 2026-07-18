import { describe, it, expect, vi } from 'vitest';
import { createAutocomplete } from './useAutocomplete.svelte';
import type { CompletionProvider, Suggestion } from './types';

function sug(label: string): Suggestion {
	return { value: label + ' ', label, key: label };
}

/** A provider active whenever the text starts with "!", query = rest. */
function fakeProvider(
	suggestions: (query: string) => Suggestion[] | Promise<Suggestion[]>,
): CompletionProvider {
	return {
		id: 'fake',
		match(text, caret) {
			const before = text.slice(0, caret);
			if (!before.startsWith('!')) return null;
			return { query: before.slice(1), range: [0, caret] };
		},
		getSuggestions: (q) => suggestions(q),
	};
}

describe('createAutocomplete', () => {
	it('opens with suggestions when a provider matches', () => {
		const ac = createAutocomplete([fakeProvider(() => [sug('!a'), sug('!b')])]);
		ac.sync('!', 1);
		expect(ac.open).toBe(true);
		expect(ac.suggestions.map((s) => s.label)).toEqual(['!a', '!b']);
		expect(ac.activeIndex).toBe(0);
	});

	it('stays closed when no provider matches', () => {
		const ac = createAutocomplete([fakeProvider(() => [sug('!a')])]);
		ac.sync('hello', 5);
		expect(ac.open).toBe(false);
	});

	it('closes (silent) on an empty suggestion set', () => {
		const ac = createAutocomplete([fakeProvider(() => [])]);
		ac.sync('!zzz', 4);
		expect(ac.open).toBe(false);
		expect(ac.suggestions).toEqual([]);
	});

	it('ArrowDown/ArrowUp move the highlight and wrap', () => {
		const ac = createAutocomplete([fakeProvider(() => [sug('!a'), sug('!b'), sug('!c')])]);
		ac.sync('!', 1);
		const down = new KeyboardEvent('keydown', { key: 'ArrowDown' });
		expect(ac.onKeydown(down)).toBe(true);
		expect(ac.activeIndex).toBe(1);
		ac.onKeydown(new KeyboardEvent('keydown', { key: 'ArrowDown' }));
		expect(ac.activeIndex).toBe(2);
		ac.onKeydown(new KeyboardEvent('keydown', { key: 'ArrowDown' })); // wrap
		expect(ac.activeIndex).toBe(0);
		ac.onKeydown(new KeyboardEvent('keydown', { key: 'ArrowUp' })); // wrap back
		expect(ac.activeIndex).toBe(2);
	});

	it('onKeydown returns false when closed (so Enter can send)', () => {
		const ac = createAutocomplete([fakeProvider(() => [sug('!a')])]);
		ac.sync('hello', 5); // no match → closed
		expect(ac.onKeydown(new KeyboardEvent('keydown', { key: 'Enter' }))).toBe(false);
		expect(ac.onKeydown(new KeyboardEvent('keydown', { key: 'ArrowDown' }))).toBe(false);
	});

	it('Tab accepts the highlighted suggestion via onAccept + splice', () => {
		const onAccept = vi.fn();
		const ac = createAutocomplete([fakeProvider(() => [sug('!more'), sug('!memory')])], {
			onAccept,
		});
		ac.sync('!m', 2);
		ac.onKeydown(new KeyboardEvent('keydown', { key: 'ArrowDown' })); // → !memory
		const consumed = ac.onKeydown(new KeyboardEvent('keydown', { key: 'Tab' }));
		expect(consumed).toBe(true);
		expect(onAccept).toHaveBeenCalledWith({ text: '!memory ', caret: 8 });
		expect(ac.open).toBe(false);
	});

	it('Enter accepts while open', () => {
		const onAccept = vi.fn();
		const ac = createAutocomplete([fakeProvider(() => [sug('!more')])], { onAccept });
		ac.sync('!mo', 3);
		expect(ac.onKeydown(new KeyboardEvent('keydown', { key: 'Enter' }))).toBe(true);
		expect(onAccept).toHaveBeenCalledWith({ text: '!more ', caret: 6 });
	});

	it('accept() splices into the middle of surrounding text', () => {
		const onAccept = vi.fn();
		// Provider replaces only [0,3] but text has a trailing tail.
		const provider: CompletionProvider = {
			id: 'mid',
			match: () => ({ query: 'mo', range: [0, 3] }),
			getSuggestions: () => [sug('!more')],
		};
		const ac = createAutocomplete([provider], { onAccept });
		ac.sync('!mo tail', 3);
		const r = ac.accept();
		expect(r).toEqual({ text: '!more  tail', caret: 6 });
		expect(onAccept).toHaveBeenCalledWith({ text: '!more  tail', caret: 6 });
	});

	it('Escape closes and suppresses reopen until the text changes', () => {
		const ac = createAutocomplete([fakeProvider(() => [sug('!a')])]);
		ac.sync('!a', 2);
		expect(ac.open).toBe(true);
		expect(ac.onKeydown(new KeyboardEvent('keydown', { key: 'Escape' }))).toBe(true);
		expect(ac.open).toBe(false);
		// Re-sync with identical text (e.g. a keyup) must NOT reopen.
		ac.sync('!a', 2);
		expect(ac.open).toBe(false);
		// Typing another char reopens.
		ac.sync('!ab', 3);
		expect(ac.open).toBe(true);
	});

	it('async suggestions resolve and open the popover', async () => {
		let resolveFn!: (s: Suggestion[]) => void;
		const p = new Promise<Suggestion[]>((res) => (resolveFn = res));
		const ac = createAutocomplete([fakeProvider(() => p)]);
		ac.sync('!', 1);
		expect(ac.open).toBe(false); // not resolved yet
		resolveFn([sug('!a')]);
		await p;
		await Promise.resolve();
		expect(ac.open).toBe(true);
		expect(ac.suggestions.map((s) => s.label)).toEqual(['!a']);
	});

	it('drops a stale async result when the query changed', async () => {
		const resolvers: Array<(s: Suggestion[]) => void> = [];
		const provider = fakeProvider(
			() => new Promise<Suggestion[]>((res) => resolvers.push(res)),
		);
		const ac = createAutocomplete([provider]);
		ac.sync('!a', 2); // request #0
		ac.sync('!ab', 3); // request #1 (current)
		// Resolve the newer request first, then the stale one.
		resolvers[1]([sug('!ab-hit')]);
		await Promise.resolve();
		resolvers[0]([sug('!a-stale')]);
		await Promise.resolve();
		expect(ac.suggestions.map((s) => s.label)).toEqual(['!ab-hit']);
	});
});
