import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { ChatMessage, Segment } from '$lib/stores/segments';
import { SUBSTANTIAL_TEXT_CHARS } from '$lib/stores/segments';
import { clearNotices } from '$lib/stores/notices';
import Message from './Message.svelte';

afterEach(() => {
  cleanup();
  clearNotices();
  vi.restoreAllMocks();
});

function stubClipboard(): string[] {
  const written: string[] = [];
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn(async (t: string) => void written.push(t)) },
    configurable: true,
  });
  return written;
}

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

/** A settled text segment long enough to render as its own prose group. */
function prose(id: string, text: string): Segment {
  return { kind: 'text', id, text, settled: true };
}

function tool(id: string, description: string): Segment {
  return {
    kind: 'tool',
    id,
    tool: { id, name: 'Bash', description, running: false, success: true },
  };
}

const LONG = 'a'.repeat(SUBSTANTIAL_TEXT_CHARS + 10);

function assistant(segments: Segment[]): ChatMessage {
  return { cid: 1, role: 'assistant', text: '', segments, streaming: false };
}

function copyButtons(container: HTMLElement): HTMLButtonElement[] {
  return Array.from(
    container.querySelectorAll<HTMLButtonElement>('.turn-action[aria-label="Copy message"]'),
  );
}

describe('per-turn copy', () => {
  it('gives a multi-block turn one button, not one per block', () => {
    // The affordance moved from the block to the turn: a reply is normally
    // taken whole, and one row per turn is what lets copy and delete sit
    // together instead of in two differently-placed affordances.
    const { container } = render(Message, {
      ...base,
      message: assistant([
        prose('t1', LONG),
        tool('c1', 'ran a thing'),
        prose('t2', 'The answer.'),
      ]),
    });

    expect(copyButtons(container)).toHaveLength(1);
  });

  it('copies every prose block, joined, in render order', async () => {
    const written = stubClipboard();
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', LONG), tool('c1', 'ran a thing'), prose('t2', 'Second.')]),
    });

    copyButtons(container)[0].click();
    await Promise.resolve();

    expect(written).toEqual([`${LONG}\n\nSecond.`]);
  });

  it('copies the source markdown, not the rendered html', async () => {
    const written = stubClipboard();
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', 'The **answer** is 42.')]),
    });

    copyButtons(container)[0].click();
    await Promise.resolve();

    expect(written).toEqual(['The **answer** is 42.']);
  });

  it('leaves the activity trace off the clipboard', async () => {
    // The one property the per-block version really protected: a tool trace is
    // never something anyone wants pasted.
    const written = stubClipboard();
    const { container } = render(Message, {
      ...base,
      message: assistant([
        prose('t1', LONG),
        tool('c1', 'ran a very distinctive thing'),
        prose('t2', 'Done.'),
      ]),
    });

    copyButtons(container)[0].click();
    await Promise.resolve();

    expect(written[0]).not.toContain('distinctive');
  });

  it('offers no copy on a tool-only turn', () => {
    const { container } = render(Message, {
      ...base,
      message: assistant([tool('c1', 'ran a thing')]),
    });

    expect(copyButtons(container)).toHaveLength(0);
  });

  it('copies a user turn body', async () => {
    const written = stubClipboard();
    const { container } = render(Message, {
      ...base,
      message: {
        cid: 2,
        role: 'user',
        text: 'what is 6 times 7?',
        segments: [],
        streaming: false,
      },
    });

    copyButtons(container)[0].click();
    await Promise.resolve();

    expect(written).toEqual(['what is 6 times 7?']);
  });

  it('copies a system row body', async () => {
    const written = stubClipboard();
    const { container } = render(Message, {
      ...base,
      message: { cid: 3, role: 'system', text: '**Rooms**\n- one', segments: [], streaming: false },
    });

    copyButtons(container)[0].click();
    await Promise.resolve();

    expect(written).toEqual(['**Rooms**\n- one']);
  });

  it('offers nothing to copy on an empty user turn', () => {
    // An attachment-only turn has chips but no text body.
    const { container } = render(Message, {
      ...base,
      message: {
        cid: 4,
        role: 'user',
        text: '',
        segments: [],
        streaming: false,
        attachments: ['note.txt'],
      },
    });

    expect(copyButtons(container)).toHaveLength(0);
  });

  it('holds the button back while the turn is still streaming', () => {
    // A half-written turn copies half an answer, and the row would sit under
    // text that is still moving.
    const msg = assistant([prose('t1', 'partial')]);
    msg.streaming = true;
    const { container } = render(Message, { ...base, message: msg });

    expect(copyButtons(container)).toHaveLength(0);
  });

  it('labels the button for screen readers', () => {
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', 'The answer.')]),
    });

    expect(copyButtons(container)[0].getAttribute('aria-label')).toMatch(/copy/i);
  });

  it('keeps the button out of the user turn pre-wrap element', () => {
    // `.user-text` carries `white-space: pre-wrap`, so the newlines and
    // indentation of the markup around a sibling button render as real blank
    // space. Putting the button inside it indents every user message — a
    // regression no assertion on the copied *text* would catch.
    const { container } = render(Message, {
      ...base,
      message: { cid: 5, role: 'user', text: 'hello', segments: [], streaming: false },
    });

    const text = container.querySelector('.user-text')!;
    expect(text.querySelector('.turn-action')).toBeNull();
    expect(text.textContent).toBe('hello');
  });

  it('includes fenced code in the copy', async () => {
    // Why there is no per-code-block copy button: the copy is the markdown
    // *source*, which carries the fences, so copying the turn already hands
    // back the code ready to paste.
    const written = stubClipboard();
    const src = 'Run this:\n\n```bash\nnpm run build\n```\n\nThen reload.';
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', src)]),
    });

    copyButtons(container)[0].click();
    await Promise.resolve();

    expect(written).toEqual([src]);
    // And it really did render as a code block, not as literal backticks.
    expect(container.querySelector('pre code')?.textContent).toContain('npm run build');
  });

  it('keeps the action row out of the rendered body', () => {
    // `.markdown > *:last-child` strips the trailing margin off the last
    // *content* block. A button inside `.body` would be what it matched, so a
    // reply ending in a list or a code block would keep its 1rem margin.
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', 'Line one.\n\n- a\n- b')]),
    });

    const body = container.querySelector('.body.markdown')!;
    expect(body.querySelector('.turn-action')).toBeNull();
    expect(body.lastElementChild?.tagName).toBe('UL');
  });
});

describe('per-message delete', () => {
  function deleteButtons(container: HTMLElement): HTMLButtonElement[] {
    return Array.from(
      container.querySelectorAll<HTMLButtonElement>('.turn-action[aria-label="Delete message"]'),
    );
  }

  const durable = (over: Partial<ChatMessage> = {}): ChatMessage => ({
    cid: 9,
    role: 'assistant',
    text: '',
    segments: [prose('t1', 'The answer.')],
    streaming: false,
    msgId: 42,
    ...over,
  });

  it('sits in the same row as copy', () => {
    const { container } = render(Message, {
      ...base,
      message: durable(),
      onDelete: noop,
    });

    const row = container.querySelector('.turn-actions')!;
    expect(row.querySelectorAll('.turn-action')).toHaveLength(2);
  });

  it('fires with the message cid', () => {
    const onDelete = vi.fn();
    const { container } = render(Message, { ...base, message: durable(), onDelete });

    deleteButtons(container)[0].click();

    expect(onDelete).toHaveBeenCalledWith(9);
  });

  it('renders nothing without a handler', () => {
    // The surface decides whether delete is offered; the component only draws
    // the affordance. Without a handler there is no confirmation either.
    const { container } = render(Message, { ...base, message: durable() });

    expect(deleteButtons(container)).toHaveLength(0);
  });

  it('renders nothing for a row with no durable id', () => {
    // A live placeholder isn't stored yet, so there is nothing to delete.
    const { container } = render(Message, {
      ...base,
      message: durable({ msgId: undefined }),
      onDelete: noop,
    });

    expect(deleteButtons(container)).toHaveLength(0);
  });

  it('stays available on a streaming turn once it has a durable id', () => {
    // Copy is withheld while the text is still moving; delete is not tied to
    // the text at all, and a turn you want gone is often one still going.
    const { container } = render(Message, {
      ...base,
      message: durable({ streaming: true }),
      onDelete: noop,
    });

    expect(copyButtons(container)).toHaveLength(0);
    expect(deleteButtons(container)).toHaveLength(1);
  });

  it('is offered on a system row too', () => {
    const { container } = render(Message, {
      ...base,
      message: {
        cid: 11,
        role: 'system',
        text: 'Alert body',
        segments: [],
        streaming: false,
        msgId: 77,
      },
      onDelete: noop,
    });

    expect(deleteButtons(container)).toHaveLength(1);
  });
});
