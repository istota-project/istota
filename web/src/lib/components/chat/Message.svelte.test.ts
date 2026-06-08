import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { writable, type Writable } from 'svelte/store';
import { tick } from 'svelte';
import { applyEvent, type ChatMessage, type Segment } from '$lib/stores/segments';
import StreamHarness from './StreamHarness.svelte';

afterEach(cleanup);

function assistant(): ChatMessage {
	return { cid: 1, role: 'assistant', text: '', segments: [], streaming: true };
}

// Mirror chat.ts updateMsg: mutate via the real reducer, then rebuild references
// at every keyed level so Svelte's `{#each}` re-renders.
const cloneSeg = (s: Segment): Segment =>
	s.kind === 'tool' ? { ...s, tool: { ...s.tool } } : { ...s };

function freshRefsUpdate(store: Writable<ChatMessage[]>, kind: string, payload: Record<string, unknown>) {
	store.update((arr) => {
		const m = arr[0];
		applyEvent(m, kind, payload);
		const next = arr.slice();
		next[0] = { ...m, segments: m.segments.map(cloneSeg) };
		return next;
	});
}

// The pre-fix path: mutate in place, return the same array (same message + seg refs).
function inPlaceUpdate(store: Writable<ChatMessage[]>, kind: string, payload: Record<string, unknown>) {
	store.update((arr) => {
		applyEvent(arr[0], kind, payload);
		return arr;
	});
}

describe('live streaming reaches the DOM (Message + keyed each)', () => {
	it('result overwrites the streamed answer in the DOM (fresh-ref update)', async () => {
		const store = writable<ChatMessage[]>([assistant()]);
		const { container } = render(StreamHarness, { store });

		freshRefsUpdate(store, 'text_delta', { text: 'I understand.\n\n**' });
		await tick();
		expect(container.textContent).toContain('I understand.');

		freshRefsUpdate(store, 'tool_start', { tool_name: 'Bash', description: 'do thing', tool_call_id: 'c1' });
		freshRefsUpdate(store, 'tool_end', { tool_call_id: 'c1', success: true });
		freshRefsUpdate(store, 'text_delta', { text: 'Round 1: 42' });
		await tick();

		// The canonical result must overwrite the partial streamed answer live.
		freshRefsUpdate(store, 'result', { text: 'Final answer: **42**' });
		freshRefsUpdate(store, 'done', { duration_seconds: 3 });
		await tick();

		expect(container.textContent).toContain('Final answer: 42');
		// Markdown is parsed (bold rendered), not shown raw.
		expect(container.querySelector('strong')?.textContent).toBe('42');
		expect(container.textContent).not.toContain('**');
		// A tool chip rendered inline.
		expect(container.textContent).toContain('do thing');
	});

	it('regression guard: in-place mutation (same refs) never reaches the DOM', async () => {
		const store = writable<ChatMessage[]>([assistant()]);
		const { container } = render(StreamHarness, { store });

		// Mutating the message + its segments in place and returning the same array
		// does not re-render the streamed content: the keyed message reference is
		// unchanged, so Svelte treats it as untouched. This is the bug the fresh-ref
		// update fixes — see the test above.
		inPlaceUpdate(store, 'text_delta', { text: 'partial' });
		inPlaceUpdate(store, 'result', { text: 'THE REAL ANSWER' });
		inPlaceUpdate(store, 'done', { duration_seconds: 1 });
		await tick();

		expect(container.textContent).not.toContain('THE REAL ANSWER');
		expect(container.textContent).not.toContain('partial');
	});

	it('reasoning is shown nowhere; the chip carries only the tool action', async () => {
		const store = writable<ChatMessage[]>([assistant()]);
		const { container } = render(StreamHarness, { store });

		// A turn: reasoning lead-in → tool → answer → result.
		freshRefsUpdate(store, 'thinking', { text: 'REASONING_LEADIN' });
		freshRefsUpdate(store, 'tool_start', { tool_name: 'WebSearch', description: 'web search', tool_call_id: 'c1' });
		freshRefsUpdate(store, 'tool_end', { tool_call_id: 'c1', success: true });
		freshRefsUpdate(store, 'text_delta', { text: 'PROMINENT_ANSWER' });
		freshRefsUpdate(store, 'result', { text: 'PROMINENT_ANSWER' });
		freshRefsUpdate(store, 'done', { duration_seconds: 2 });
		await tick();

		// The answer lives in the prominent `.body.markdown` area.
		const body = container.querySelector('.body.markdown');
		expect(body?.textContent).toContain('PROMINENT_ANSWER');
		// The reasoning must NOT be in the prominent answer area …
		expect(body?.textContent ?? '').not.toContain('REASONING_LEADIN');
		// … nor in the activity chip (reasoning is not rendered anywhere) …
		const chip = container.querySelector('.activity');
		expect(chip?.textContent ?? '').not.toContain('REASONING_LEADIN');
		// … the chip carries only the tool action.
		expect(chip?.textContent ?? '').toContain('web search');
	});
});
