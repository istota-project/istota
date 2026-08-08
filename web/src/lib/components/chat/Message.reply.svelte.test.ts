/**
 * Reply affordance and citation rendering on a chat message.
 *
 * Two halves: the button that stages a reply (offered only where a staged
 * reply has somewhere to go), and the quote block a turn carrying a citation
 * renders above its body — clickable for a live parent, muted and inert for a
 * deleted one.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import type { ChatMessage } from '$lib/stores/segments';
import { clearNotices } from '$lib/stores/notices';
import Message from './Message.svelte';

const source = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), 'Message.svelte'),
  'utf8',
);

afterEach(() => {
  cleanup();
  clearNotices();
  vi.restoreAllMocks();
});

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

function userMsg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'user',
    text: 'about that',
    segments: [],
    streaming: false,
    msgId: 41,
    ...over,
  };
}

function replyButton(container: HTMLElement): HTMLButtonElement | null {
  return container.querySelector<HTMLButtonElement>('.turn-action[aria-label="Reply to message"]');
}

describe('reply button', () => {
  it('is offered on a durable row when a handler is passed', () => {
    const { container } = render(Message, { ...base, message: userMsg(), onReply: noop });
    expect(replyButton(container)).not.toBeNull();
  });

  it('is withheld without a handler', () => {
    const { container } = render(Message, { ...base, message: userMsg() });
    expect(replyButton(container)).toBeNull();
  });

  it('is withheld on a row with no durable id', () => {
    // An optimistic row, an in-flight placeholder — nothing a citation could
    // name yet. Same rule star and delete already follow.
    const { container } = render(Message, {
      ...base,
      message: userMsg({ msgId: undefined }),
      onReply: noop,
    });
    expect(replyButton(container)).toBeNull();
  });

  it('is withheld in an aggregate view', () => {
    // All / Starred have no composer, so a staged reply has nowhere to go.
    const { container } = render(Message, {
      ...base,
      message: userMsg(),
      onReply: noop,
      aggregate: true,
    });
    expect(replyButton(container)).toBeNull();
  });

  it('is withheld on a failed send', () => {
    const { container } = render(Message, {
      ...base,
      message: userMsg({ sendState: 'failed', sendError: 'nope' }),
      onReply: noop,
    });
    expect(replyButton(container)).toBeNull();
  });

  it('hands the cid to the handler', async () => {
    const onReply = vi.fn();
    const { container } = render(Message, {
      ...base,
      message: userMsg({ cid: 7 }),
      onReply,
    });
    await fireEvent.click(replyButton(container)!);
    expect(onReply).toHaveBeenCalledWith(7);
  });

  it('ends the row after copy and star but before delete', () => {
    // Ascending consequence: reply stages a new message — more than a private
    // mark, less than a destructive removal.
    const { container } = render(Message, {
      ...base,
      message: userMsg(),
      onReply: noop,
      onToggleStar: noop,
      onDelete: noop,
    });
    const labels = Array.from(
      container.querySelectorAll<HTMLButtonElement>('.turn-actions .turn-action'),
    ).map((b) => b.getAttribute('aria-label'));
    expect(labels).toEqual(['Copy message', 'Star message', 'Reply to message', 'Delete message']);
  });
});

describe('touch targets with a fourth button', () => {
  it('keeps the overlay width derived from the button pitch', () => {
    // The row went from three buttons to four, so the targets got tighter.
    // They still tile exactly rather than overlapping *because* the width is
    // the button plus one gap — at a flat 44px each, four of them would need
    // ~21px between icons to stay apart and would instead overlap, handing a
    // tap in the seam to whichever won the stacking order. Read from the
    // source, since jsdom applies no CSS: the rule is the artefact under test.
    //
    // The arithmetic at the default text scale (root 17.6px): a button is the
    // 15px icon plus 2 × --space-1 = 23.8px, the gap below 768px is --space-2
    // = 8.8px, so four of them span 4 × 23.8 + 3 × 8.8 ≈ 122px and each
    // overlay is 32.6 × 44. On a 375px phone the transcript's content column
    // is ~322px (375 minus the 2.25rem gutter sum and the trailing inset), so
    // the row takes about a third of the narrowest bubble; at the large text
    // scale it is ~127px against ~317px. Comfortable either way.
    expect(source).toContain('width: calc(100% + var(--turn-action-gap));');
    expect(source).toContain('--turn-action-gap: var(--space-2);');
  });
});

describe('citation quote block', () => {
  it('renders the parent excerpt above the body', () => {
    const { container } = render(Message, {
      ...base,
      message: userMsg({
        replyTo: { msgId: 22, role: 'assistant', excerpt: 'the earlier answer' },
      }),
    });
    const quote = container.querySelector('.reply-quote');
    expect(quote?.textContent).toContain('the earlier answer');
  });

  it('clicks through to the cited message', async () => {
    const onJumpToMessage = vi.fn();
    const { container } = render(Message, {
      ...base,
      message: userMsg({
        replyTo: { msgId: 22, role: 'assistant', excerpt: 'the earlier answer' },
      }),
      onJumpToMessage,
    });
    await fireEvent.click(container.querySelector<HTMLElement>('.reply-quote')!);
    expect(onJumpToMessage).toHaveBeenCalledWith(22);
  });

  it('renders a deleted parent muted and inert', () => {
    const { container } = render(Message, {
      ...base,
      message: userMsg({ replyTo: { msgId: 22, deleted: true } }),
      onJumpToMessage: vi.fn(),
    });
    const quote = container.querySelector('.reply-quote');
    expect(quote).not.toBeNull();
    expect(quote?.classList.contains('deleted')).toBe(true);
    // Not a button: there is nothing left to jump to.
    expect(quote?.tagName.toLowerCase()).not.toBe('button');
    expect(quote?.textContent).toContain('Original message deleted');
  });

  it('a staged citation with no `deleted` flag renders as live', () => {
    // Absence counts as live — the composer stages without asserting a state
    // only the server knows, so a missing flag must not render as deleted.
    const { container } = render(Message, {
      ...base,
      message: userMsg({ replyTo: { msgId: 22, role: 'assistant', excerpt: 'earlier' } }),
      onJumpToMessage: vi.fn(),
    });
    expect(container.querySelector('.reply-quote')?.classList.contains('deleted')).toBe(false);
  });

  it('renders nothing for a turn that cites nothing', () => {
    const { container } = render(Message, { ...base, message: userMsg() });
    expect(container.querySelector('.reply-quote')).toBeNull();
  });
});

/**
 * The quote occupies the same slot in a turn as an activity chip does — a block
 * between the author header and the prose — so it takes the chip's spacing and
 * the body's width rather than a second set of numbers. Read from the source,
 * since jsdom applies no CSS: the rule set is the artefact under test, and a
 * computed-style assertion here would pass whatever the file said.
 *
 * The parser is the one in `Composer.sendButton.svelte.test.ts`, trimmed to
 * what these assertions need.
 */
const here = dirname(fileURLToPath(import.meta.url));
const activityTraceSource = readFileSync(resolve(here, 'ActivityTrace.svelte'), 'utf8');
const tokens = readFileSync(resolve(here, '../../styles/tokens.css'), 'utf8');

const styleOpen = source.indexOf('>', source.indexOf('<style'));
const css = source
  .slice(styleOpen + 1, source.lastIndexOf('</style>'))
  .replace(/\/\*[\s\S]*?\*\//g, '');

type Rule = { selector: string; body: string; atRules: string[] };

/**
 * Every `selector { … }` pair, one entry per selector in a list, each carrying
 * the at-rule preludes it is nested inside.
 *
 * The preludes are what `decl` filters on. Flattened away, a `.body` or
 * `.reply-quote` override written into this file's `@media (max-width: 768px)`
 * block — which precedes every rule asserted on below — would answer in place
 * of the base rule, and the assertions would report the breakpoint's numbers
 * while claiming to test the resting ones.
 */
function parseRules(text: string, atRules: string[] = []): Rule[] {
  const out: Rule[] = [];
  let prelude = '';
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '{') {
      let depth = 1;
      let j = i + 1;
      for (; j < text.length && depth > 0; j++) {
        if (text[j] === '{') depth++;
        else if (text[j] === '}') depth--;
      }
      const body = text.slice(i + 1, j - 1);
      const head = prelude.trim();
      // `@keyframes` holds percentage stops, not selectors.
      if (head.startsWith('@keyframes')) {
        /* skipped */
      } else if (head.startsWith('@')) out.push(...parseRules(body, [...atRules, head]));
      else if (head) {
        for (const one of head.split(',')) {
          if (one.trim()) out.push({ selector: one.trim(), body, atRules });
        }
      }
      i = j - 1;
      prelude = '';
    } else if (text[i] === '}') prelude = '';
    else prelude += text[i];
  }
  return out;
}

/**
 * The declared value of one longhand on one unconditional rule, or undefined if
 * neither the rule nor the declaration exists.
 *
 * The last such rule wins, the way the cascade resolves a tie. Longhands only:
 * a `margin:` shorthand would set a top margin this reads as absent, which is
 * why the assertions below check for the shorthand separately rather than
 * trusting an `undefined`.
 */
function decl(selector: string, prop: string): string | undefined {
  const matches = parseRules(css).filter((r) => r.selector === selector && r.atRules.length === 0);
  const rule = matches[matches.length - 1];
  return rule?.body.match(new RegExp(`(?:^|[;{\\s])${prop}\\s*:\\s*([^;]+)`))?.[1].trim();
}

describe('citation quote geometry', () => {
  it('parses the rules it is about to assert on', () => {
    // A hand-rolled parser fails by finding nothing and reporting nothing
    // wrong. Pin that it sees every selector the assertions below name.
    const seen = parseRules(css).map((r) => r.selector);
    expect(seen).toEqual(
      expect.arrayContaining([
        '.reply-quote',
        '.reply-quote.under-meta',
        '.body',
        '.chip-slot.gap-below',
        '.meta + .chip-slot',
      ]),
    );
  });

  it('caps at the same width as the message body', () => {
    expect(decl('.reply-quote', 'max-width')).toBe('var(--chat-body-max)');
    expect(decl('.body', 'max-width')).toBe(decl('.reply-quote', 'max-width'));
  });

  it('no block in the content column restates the cap', () => {
    // The token exists because the number was written out three times over two
    // files, agreeing only by comment. A fourth copy — or a breakpoint block
    // overriding one of them — is invisible until you put two turns side by
    // side on a wide monitor, which is the same reason `contentFrame.test.ts`
    // guards `--content-max` this way.
    const value = tokens.match(/--chat-body-max:\s*([^;]+)/)?.[1].trim();
    expect(value).toMatch(/^\d+px$/);
    for (const [name, text] of [
      ['Message.svelte', source],
      ['ActivityTrace.svelte', activityTraceSource],
    ] as const) {
      expect(text, `${name} must read the token, not restate ${value}`).not.toContain(value!);
    }
  });

  it('takes the chip slot’s gap below, its neighbour being a prose block', () => {
    // Anchored on the chip slot's own value as well as on the equality: `decl`
    // returns undefined both for "rule absent" and "declaration absent", so a
    // bare equality passes when the pair loses the declaration together.
    expect(decl('.chip-slot.gap-below', 'margin-bottom')).toBe('var(--space-3)');
    expect(decl('.reply-quote', 'margin-bottom')).toBe(
      decl('.chip-slot.gap-below', 'margin-bottom'),
    );
  });

  it('takes the chip slot’s half gap under the author header', () => {
    expect(decl('.meta + .chip-slot', 'margin-top')).toBe('calc(var(--space-3) / 2)');
    expect(decl('.reply-quote.under-meta', 'margin-top')).toBe(
      decl('.meta + .chip-slot', 'margin-top'),
    );
  });

  it('carries no top margin where no header precedes it', () => {
    // The chip slot's base is flush and its gap is neighbour-aware; the quote
    // follows the same rule, so a continuation row's quote sits tight the way a
    // tool-first chip does. The shorthand is checked too — `decl` reads
    // longhands, and a `margin:` would set a top margin it reports as absent.
    expect(decl('.reply-quote', 'margin-top')).toBeUndefined();
    expect(decl('.reply-quote', 'margin')).toBeUndefined();
  });

  it('marks the header case on the element rather than by DOM adjacency', () => {
    // A class rather than `.meta + .reply-quote`, because the condition is
    // `!continuation` — the same variable that decides whether the header
    // renders at all — and not a DOM adjacency that happens to follow from it.
    const withHeader = render(Message, {
      ...base,
      message: userMsg({ replyTo: { msgId: 22, role: 'assistant', excerpt: 'earlier' } }),
    });
    expect(
      withHeader.container.querySelector('.reply-quote')?.classList.contains('under-meta'),
    ).toBe(true);
    cleanup();

    const continued = render(Message, {
      ...base,
      message: userMsg({ replyTo: { msgId: 22, role: 'assistant', excerpt: 'earlier' } }),
      continuation: true,
    });
    expect(
      continued.container.querySelector('.reply-quote')?.classList.contains('under-meta'),
    ).toBe(false);
  });
});
