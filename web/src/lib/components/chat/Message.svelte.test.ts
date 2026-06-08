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
	s.kind === 'text' ? { ...s } : { ...s, tool: { ...s.tool } };

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

	it('regression guard: in-place mutation (same refs) leaves the DOM stale', async () => {
		const store = writable<ChatMessage[]>([assistant()]);
		const { container } = render(StreamHarness, { store });

		inPlaceUpdate(store, 'text_delta', { text: 'partial' });
		await tick();
		expect(container.textContent).toContain('partial');

		// Overwrite the answer in place — the inner `{#each segments}` sees the same
		// segment reference and does NOT re-render the TextSegment, so the answer
		// text stays frozen at "partial" even though the result arrived. (The
		// message-level re-render still picks up scalar changes like the duration
		// footer; only the nested keyed segment children go stale.) This is the bug
		// the fresh-ref update fixes — see the test above.
		inPlaceUpdate(store, 'result', { text: 'THE REAL ANSWER' });
		inPlaceUpdate(store, 'done', { duration_seconds: 1 });
		await tick();

		expect(container.textContent).not.toContain('THE REAL ANSWER');
		expect(container.textContent).toContain('partial');
	});
});
