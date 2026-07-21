import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { writable, type Writable } from 'svelte/store';
import { tick } from 'svelte';
import { applyEvent, type ChatMessage, type Segment } from '$lib/stores/segments';
import Message from './Message.svelte';
import StreamHarness from './StreamHarness.svelte';

afterEach(cleanup);

function assistant(): ChatMessage {
  return { cid: 1, role: 'assistant', text: '', segments: [], streaming: true };
}

// Mirror chat.ts updateMsg: mutate via the real reducer, then rebuild references
// at every keyed level so Svelte's `{#each}` re-renders.
const cloneSeg = (s: Segment): Segment =>
  s.kind === 'tool' ? { ...s, tool: { ...s.tool } } : { ...s };

function freshRefsUpdate(
  store: Writable<ChatMessage[]>,
  kind: string,
  payload: Record<string, unknown>,
) {
  store.update((arr) => {
    const m = arr[0];
    applyEvent(m, kind, payload);
    const next = arr.slice();
    next[0] = { ...m, segments: m.segments.map(cloneSeg) };
    return next;
  });
}

// The pre-fix path: mutate in place, return the same array (same message + seg refs).
function inPlaceUpdate(
  store: Writable<ChatMessage[]>,
  kind: string,
  payload: Record<string, unknown>,
) {
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

    freshRefsUpdate(store, 'tool_start', {
      tool_name: 'Bash',
      description: 'do thing',
      tool_call_id: 'c1',
    });
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

  it('the body recomputes from segments on every store emit (no stale memo barrier)', async () => {
    const store = writable<ChatMessage[]>([assistant()]);
    const { container } = render(StreamHarness, { store });

    // The body is derived through `renderGroups(message)`, which reads the
    // segment list afresh on each re-render — there is no intermediate
    // reference-equal `$derived(message.segments)` hop to short-circuit
    // propagation. So even an in-place mutation that returns the same array
    // reaches the DOM once the store emits. (Production still rebuilds refs in
    // chat.ts:updateMsg for the page-level keyed `{#each}` — belt and braces.)
    inPlaceUpdate(store, 'result', { text: 'THE REAL ANSWER' });
    inPlaceUpdate(store, 'done', { duration_seconds: 1 });
    await tick();

    expect(container.textContent).toContain('THE REAL ANSWER');
  });

  it('a substantial intermediate block survives a tool boundary into the body', async () => {
    // The 179848 regression: meaty analysis → edit tool → terse final summary.
    // The analysis must stay visible (its own prose block), not vanish into the
    // tool-only chip when the edit settles it.
    const meaty = 'Two sharpenings worth making explicit. ' + 'detail '.repeat(40);
    const store = writable<ChatMessage[]>([assistant()]);
    const { container } = render(StreamHarness, { store });

    freshRefsUpdate(store, 'text_delta', { text: meaty });
    freshRefsUpdate(store, 'tool_start', {
      tool_name: 'Edit',
      description: 'edit note',
      tool_call_id: 'e1',
    });
    freshRefsUpdate(store, 'tool_end', { tool_call_id: 'e1', success: true });
    freshRefsUpdate(store, 'result', { text: 'Added it as the Guiding principle.' });
    freshRefsUpdate(store, 'done', { duration_seconds: 4 });
    await tick();

    // Both the meaty analysis and the final summary are present in the body.
    expect(container.textContent).toContain('Two sharpenings worth making explicit.');
    expect(container.textContent).toContain('Added it as the Guiding principle.');
    // Two distinct prose bodies (intermediate + answer), plus the edit chip.
    expect(container.querySelectorAll('.body.markdown').length).toBe(2);
    expect(container.querySelector('.activity')?.textContent ?? '').toContain('edit note');
  });

  it('a short lead-in before a tool is NOT shown in the body', async () => {
    const store = writable<ChatMessage[]>([assistant()]);
    const { container } = render(StreamHarness, { store });

    freshRefsUpdate(store, 'text_delta', { text: 'Let me check the calendar.' });
    freshRefsUpdate(store, 'tool_start', {
      tool_name: 'Bash',
      description: 'calendar list',
      tool_call_id: 'c1',
    });
    freshRefsUpdate(store, 'tool_end', { tool_call_id: 'c1', success: true });
    freshRefsUpdate(store, 'result', { text: 'You have 2 events today.' });
    freshRefsUpdate(store, 'done', { duration_seconds: 1 });
    await tick();

    const body = container.querySelector('.body.markdown');
    expect(body?.textContent).toContain('You have 2 events today.');
    // Only the answer renders as a body; the lead-in is dropped.
    expect(container.querySelectorAll('.body.markdown').length).toBe(1);
    expect(container.textContent).not.toContain('Let me check the calendar.');
  });

  it('reasoning is shown nowhere; the chip carries only the tool action', async () => {
    const store = writable<ChatMessage[]>([assistant()]);
    const { container } = render(StreamHarness, { store });

    // A turn: reasoning lead-in → tool → answer → result.
    freshRefsUpdate(store, 'thinking', { text: 'REASONING_LEADIN' });
    freshRefsUpdate(store, 'tool_start', {
      tool_name: 'WebSearch',
      description: 'web search',
      tool_call_id: 'c1',
    });
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

// ---------------------------------------------------------------------------
// Per-message starring + cross-room labels
// ---------------------------------------------------------------------------

function finished(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'assistant',
    text: 'the answer',
    streaming: false,
    segments: [{ kind: 'text', id: 's1', text: 'the answer', settled: false }],
    createdAt: '2026-07-10T12:00:00Z',
    ...over,
  };
}

const noop = () => {};

describe('star affordance', () => {
  it('renders no star button without msgId', () => {
    const { container } = render(Message, {
      message: finished(),
      onConfirm: noop,
      onReject: noop,
      onToggleStar: noop,
    });
    expect(container.querySelector('.star-btn')).toBeNull();
  });

  it('renders no star button without an onToggleStar handler', () => {
    const { container } = render(Message, {
      message: finished({ msgId: 42 }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.star-btn')).toBeNull();
  });

  it('renders a hover-revealed star button for a durable message', () => {
    const { container } = render(Message, {
      message: finished({ msgId: 42, starred: false }),
      onConfirm: noop,
      onReject: noop,
      onToggleStar: noop,
    });
    const btn = container.querySelector('.star-btn');
    expect(btn).not.toBeNull();
    expect(btn?.getAttribute('aria-label')).toBe('Star message');
    expect(btn?.getAttribute('aria-pressed')).toBe('false');
    // Hidden at rest (hover/focus reveals it via CSS); not marked starred.
    expect(btn?.classList.contains('starred')).toBe(false);
  });

  it('shows the filled / at-rest state when starred', () => {
    const { container } = render(Message, {
      message: finished({ msgId: 42, starred: true }),
      onConfirm: noop,
      onReject: noop,
      onToggleStar: noop,
    });
    const btn = container.querySelector('.star-btn');
    expect(btn?.classList.contains('starred')).toBe(true);
    expect(btn?.getAttribute('aria-label')).toBe('Unstar message');
    expect(btn?.getAttribute('aria-pressed')).toBe('true');
    expect(btn?.querySelector('svg')?.getAttribute('fill')).toBe('currentColor');
  });

  it('fires onToggleStar with the message cid', async () => {
    const onToggleStar = vi.fn();
    const { container } = render(Message, {
      message: finished({ cid: 7, msgId: 42 }),
      onConfirm: noop,
      onReject: noop,
      onToggleStar,
    });
    await fireEvent.click(container.querySelector('.star-btn')!);
    expect(onToggleStar).toHaveBeenCalledWith(7);
  });
});

describe('room label chip (aggregate views)', () => {
  it('renders a clickable room chip when roomName is set and a handler is passed', async () => {
    const onRoomClick = vi.fn();
    const { container } = render(Message, {
      message: finished({ msgId: 42, roomToken: 'tok-1', roomName: 'general' }),
      onConfirm: noop,
      onReject: noop,
      onRoomClick,
    });
    const chip = container.querySelector('.room-chip');
    expect(chip).not.toBeNull();
    expect(chip?.textContent).toContain('general');
    await fireEvent.click(chip!);
    expect(onRoomClick).toHaveBeenCalledWith('tok-1');
  });

  it('renders no room chip without a handler (room mode)', () => {
    const { container } = render(Message, {
      message: finished({ msgId: 42, roomToken: 'tok-1', roomName: 'general' }),
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.room-chip')).toBeNull();
  });

  it('renders the room name without a leading hash', () => {
    const { container } = render(Message, {
      message: finished({ msgId: 42, roomToken: 'tok-1', roomName: 'general' }),
      onConfirm: noop,
      onReject: noop,
      onRoomClick: noop,
    });
    expect(container.querySelector('.room-chip')?.textContent?.trim()).toBe('general');
  });
});

describe('hover metadata', () => {
  const withMeta = () =>
    finished({ taskId: 7, model: 'anthropic/claude-opus-4-8', durationSeconds: 12 });

  it('shows task, model and duration in room mode', () => {
    const { container } = render(Message, {
      message: withMeta(),
      onConfirm: noop,
      onReject: noop,
    });
    const footer = container.querySelector('.meta-footer')?.textContent ?? '';
    expect(footer).toContain('#7');
    expect(footer).toContain('opus-4-8');
    expect(footer).toContain('12s');
  });

  it('shows only the task number in the aggregate views', () => {
    const { container } = render(Message, {
      message: withMeta(),
      onConfirm: noop,
      onReject: noop,
      aggregate: true,
    });
    expect(container.querySelector('.meta-footer')?.textContent?.trim()).toBe('#7');
  });
});

describe('search results + anchors', () => {
  function systemSearch(over: Partial<ChatMessage> = {}): ChatMessage {
    return {
      cid: 9,
      role: 'system',
      text: 'fallback text',
      streaming: false,
      segments: [],
      searchResults: {
        kind: 'search_results',
        query: 'falcon',
        text: 'fallback text',
        results: [
          {
            source_type: 'conversation',
            summary: 'falcon migration timeline',
            date: '2026-07-15',
            room_token: 'room1',
            room_name: 'Falcon planning',
            task_id: 42,
            talk_message_id: null,
            talk_link: null,
          },
        ],
      },
      ...over,
    };
  }

  it('renders SearchResults cards when a system row carries search_results data', () => {
    const { container } = render(Message, {
      message: systemSearch(),
      onConfirm: noop,
      onReject: noop,
      onJump: noop,
    });
    expect(container.querySelector('.search-results')).not.toBeNull();
    expect(container.textContent).toContain('falcon migration timeline');
    // The markdown fallback body is not rendered when cards are shown.
    expect(container.querySelector('.cmd-output')).toBeNull();
  });

  it('renders markdown for a system row without search_results data', () => {
    const { container } = render(Message, {
      message: { cid: 3, role: 'system', text: '**hi there**', streaming: false, segments: [] },
      onConfirm: noop,
      onReject: noop,
    });
    expect(container.querySelector('.search-results')).toBeNull();
    expect(container.querySelector('.cmd-output')).not.toBeNull();
    expect(container.textContent).toContain('hi there');
  });

  it('exposes a data-cid anchor on the assistant message root', () => {
    const { container } = render(Message, {
      message: finished({ cid: 77, taskId: 55 }),
      onConfirm: noop,
      onReject: noop,
    });
    const el = container.querySelector('[data-cid="77"]');
    expect(el).not.toBeNull();
    expect(el?.getAttribute('data-task-id')).toBe('55');
  });

  it('exposes a data-cid anchor on the system row root', () => {
    const { container } = render(Message, {
      message: systemSearch({ cid: 88 }),
      onConfirm: noop,
      onReject: noop,
      onJump: noop,
    });
    expect(container.querySelector('[data-cid="88"]')).not.toBeNull();
  });
});
