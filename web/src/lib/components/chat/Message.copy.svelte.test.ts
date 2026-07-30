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
  return Array.from(container.querySelectorAll<HTMLButtonElement>('.copy-block'));
}

describe('per-block copy', () => {
  it('gives each prose group its own button', () => {
    const { container } = render(Message, {
      ...base,
      message: assistant([
        prose('t1', LONG),
        tool('c1', 'ran a thing'),
        prose('t2', 'The answer.'),
      ]),
    });

    expect(copyButtons(container)).toHaveLength(2);
  });

  it('copies that group source markdown, not the rendered html', async () => {
    const written = stubClipboard();
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', 'The **answer** is 42.')]),
    });

    copyButtons(container)[0].click();
    await Promise.resolve();

    expect(written).toEqual(['The **answer** is 42.']);
  });

  it('copies each group independently', async () => {
    const written = stubClipboard();
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', LONG), tool('c1', 'ran a thing'), prose('t2', 'Second.')]),
    });

    copyButtons(container)[1].click();
    await Promise.resolve();

    expect(written).toEqual(['Second.']);
  });

  it('gives an activity chip no copy button', () => {
    // The whole point of hanging copy off the block rather than the turn: a
    // tool trace is never something you want on the clipboard.
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
    // A half-written block copies half an answer, and the button would sit
    // under text that is still moving.
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
    expect(text.querySelector('.copy-block')).toBeNull();
    expect(text.textContent).toBe('hello');
  });

  it('keeps each button a child of the block it copies', () => {
    // The reveal is `.body:hover .copy-block`, and the button is positioned
    // outside its parent's box — so it stays visible while the cursor is on
    // it only because `:hover` follows the DOM ancestor chain. Hoisting the
    // button to a sibling would look identical and un-hover on approach.
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', LONG), tool('c1', 'ran a thing'), prose('t2', 'Second.')]),
    });

    const buttons = copyButtons(container);
    expect(buttons).toHaveLength(2);
    for (const btn of buttons) {
      expect(btn.parentElement?.classList.contains('body')).toBe(true);
    }
  });

  it('includes fenced code in the block copy', async () => {
    // Why there is no per-code-block copy button: the copy is the markdown
    // *source*, which carries the fences, so copying the block already hands
    // back the code ready to paste. A second button would duplicate it.
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

  it('renders the button before the block content', () => {
    // Load-bearing despite the button being absolutely positioned: as a last
    // child it is what `.markdown > *:last-child` matches, and that rule
    // exists to strip the trailing margin off the last *content* block. Move
    // the button back to the end and a reply ending in a list or a code block
    // gets its 1rem margin on top of the reserve.
    const { container } = render(Message, {
      ...base,
      message: assistant([prose('t1', 'Line one.\n\n- a\n- b')]),
    });

    const body = container.querySelector('.body.markdown')!;
    expect(body.firstElementChild?.classList.contains('copy-block')).toBe(true);
    expect(body.lastElementChild?.tagName).toBe('UL');
  });
});
