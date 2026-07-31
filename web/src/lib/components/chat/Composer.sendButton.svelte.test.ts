import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

vi.mock('$lib/api', () => ({
  uploadChatAttachment: vi.fn(),
  fetchChatCommands: vi.fn(async () => ({ commands: [], model_aliases: [] })),
  chatConfigOnce: vi.fn(() => new Promise(() => {})),
}));
vi.mock('$lib/platform/nativePicker', () => ({
  nativePickersAvailable: vi.fn(() => false),
  takePhoto: vi.fn(),
  pickPhotos: vi.fn(),
  pickDocuments: vi.fn(),
  pickedFromFile: (f: File) => ({ name: f.name, type: f.type, size: f.size, blob: f }),
}));

import Composer from './Composer.svelte';

/**
 * The send/stop control's invariants are "colour and glyph always agree" and
 * "the swap is not animated" — and both live in CSS, which jsdom does not
 * apply. Worse, jsdom's own cascade walks stylesheets in document order without
 * weighing specificity, so a computed-style assertion would have reported the
 * button red in exactly the state the browser painted it blue. So these read
 * the component's own style block, in the spirit of the token invariants in
 * `lib/styles`: the rule set is the artefact under test.
 *
 * Resolved off this module's directory rather than `new URL('./…',
 * import.meta.url)` — Vite rewrites that exact pattern into an asset reference,
 * which under the test server resolves to an http: URL rather than the file.
 */
const source = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), 'Composer.svelte'),
  'utf8',
);
// From the end of the opening tag, not from a literal `<style>`: a `lang=` or a
// `module` attribute would otherwise leave the slice empty and every assertion
// below vacuously true.
const styleOpen = source.indexOf('>', source.indexOf('<style'));
const styleBlock = source.slice(styleOpen + 1, source.lastIndexOf('</style>'));
const css = styleBlock.replace(/\/\*[\s\S]*?\*\//g, '');

type Rule = { selector: string; body: string; atRules: string[] };

/**
 * Every style rule in declaration order, one entry per selector in a list, each
 * carrying the at-rule preludes it is nested inside.
 *
 * Both of those matter to what is asserted below. A comma-separated list is one
 * rule to the parser but several to the cascade, so folding it would hide an
 * offending selector behind a well-behaved one; and the at-rules are what the
 * hover guard is expressed in, so discarding them would let that half of the
 * fix be deleted with every test still passing.
 */
function parseRules(text: string, atRules: string[] = []): Rule[] {
  const rules: Rule[] = [];
  let prelude = '';
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '{') {
      const head = prelude.trim();
      // Walk to the matching close brace, counting nesting.
      let depth = 1;
      let j = i + 1;
      for (; j < text.length && depth > 0; j++) {
        if (text[j] === '{') depth++;
        else if (text[j] === '}') depth--;
      }
      const body = text.slice(i + 1, j - 1);
      // `@keyframes` holds percentage stops, not selectors — recursing into it
      // would put a rule called `50%` in the list.
      if (head.startsWith('@keyframes')) {
        /* skipped */
      } else if (head.startsWith('@')) rules.push(...parseRules(body, [...atRules, head]));
      else if (head) {
        for (const one of head.split(',')) {
          if (one.trim()) rules.push({ selector: one.trim(), body, atRules });
        }
      }
      i = j - 1;
      prelude = '';
    } else if (ch === '}') {
      prelude = '';
    } else {
      prelude += ch;
    }
  }
  return rules;
}

/**
 * The interaction states, and deliberately not `:disabled`.
 *
 * A disabled send button has to be grey, and `.icon-btn.send:disabled` is the
 * one state rule allowed to say so — it is not an interaction the mode can
 * disagree with, it is the absence of one. The alternative would be a
 * `:disabled` variant of each mode rule, doubling the count the rule below
 * exists to hold at two. `web/AGENTS.md` carries the same carve-out.
 */
const STATE_PSEUDO = /:(hover|active|focus|focus-visible|focus-within)\b/;

/**
 * Rules that can style the send button.
 *
 * Keyed on the *subject* compound — the last one, the element the rule actually
 * styles — and on any of the three classes that button carries, because
 * `.tools .send:hover` and `.icon-btn:hover` are both spellings that reach it
 * and either would reintroduce the defect. `:not(.send)` is what opts a rule
 * out, and it is tested per compound rather than per rule: a selector list is
 * several rules to the cascade, so one excluded branch must not exempt its
 * siblings.
 */
function sendButtonRules(): Rule[] {
  return parseRules(css).filter((r) => {
    const subject = r.selector.split(/[\s>+~]+/).pop() ?? '';
    if (subject.includes(':not(.send)')) return false;
    return /\.(icon-btn|send|stop)\b/.test(subject);
  });
}

/** A fill can be painted by any of these, or smuggled in through a variable. */
const FILL_PROPS = /(^|[;{\s])(background(-color|-image)?|--[\w-]+)\s*:/;
const declaresFill = (body: string) => FILL_PROPS.test(body.replace(/\n/g, ' '));

/** The declared value of a longhand-free `background:`, for the value checks. */
function fillOf(selector: string): string | undefined {
  const rule = sendButtonRules().find((r) => r.selector === selector);
  return rule?.body.match(/(?:^|[;{\s])background\s*:\s*([^;]+)/)?.[1].trim();
}

describe('send/stop control styling', () => {
  afterEach(() => cleanup());

  it('parses the rules it is about to assert on', () => {
    // The failure mode of a hand-rolled parser is reporting nothing wrong
    // because it saw nothing at all. Pin that it finds the rules by name.
    const seen = sendButtonRules().map((r) => r.selector);
    expect(seen).toContain('.icon-btn.send');
    expect(seen).toContain('.icon-btn.send.stop');
    expect(seen).toContain('.icon-btn.send:disabled');
  });

  it('paints the two modes in two different colours', () => {
    // The symptom itself, rather than the mechanism below it: a stop button
    // that renders in the idle blue is the whole of ISSUE-201, and every
    // structural assertion here is equally happy with both modes blue.
    expect(fillOf('.icon-btn.send')).toBe('var(--accent-blue)');
    expect(fillOf('.icon-btn.send.stop')).toBe('var(--status-danger-fg)');
    expect(fillOf('.icon-btn.send')).not.toBe(fillOf('.icon-btn.send.stop'));
  });

  it('lets nothing but the mode and :disabled decide the send button fill', () => {
    // The defect: `.icon-btn.send:hover:not(:disabled)` re-declared the blue
    // fill at a higher specificity than `.icon-btn.send.stop`, so hovering the
    // stop button painted it blue under the stop glyph. On iOS that is not a
    // transient hover — Safari synthesizes :hover on tap and leaves it applied
    // until another tap displaces it, so the button stayed blue-with-a-stop
    // for the whole turn. A state rule must never touch the fill — including
    // through a custom property, which is the quiet way back to the same place.
    const offenders = sendButtonRules()
      .filter((r) => declaresFill(r.body))
      .filter((r) => STATE_PSEUDO.test(r.selector));
    expect(offenders.map((r) => r.selector)).toEqual([]);
  });

  it('resolves the stop fill last, so a tie cannot go the other way', () => {
    // `.icon-btn.send:disabled` and `.icon-btn.send.stop` have equal
    // specificity, so document order is what decides them. Unreachable today —
    // the stop mode is never disabled — but if it ever is, order is the
    // difference between a red stop button and a grey one wearing a stop glyph,
    // which is the state this issue exists about.
    const fills = sendButtonRules()
      .filter((r) => declaresFill(r.body))
      .map((r) => r.selector);
    expect(fills[fills.length - 1]).toContain('.stop');
  });

  it('does not animate the send button between its two modes', () => {
    // alice's call, and the right one: the fill *is* the mode, so easing it
    // means the colour and the glyph disagree for the length of the easing.
    const send = sendButtonRules().find((r) => r.selector === '.icon-btn.send');
    expect(send).toBeTruthy();
    expect(send!.body).toMatch(/transition:\s*none/);
  });

  it('gates every composer hover rule on the device having a pointer', () => {
    // iOS synthesizes :hover on tap and leaves it applied, so an unguarded
    // hover rule is worn by the last control the finger touched.
    //
    // Every hover rule in the file, not only the ones on `.icon-btn`: the
    // attachment menu row and the chip's remove button were written outside
    // the guard and nothing reported it, since `lint:design` has no rule for
    // this. `web/AGENTS.md` states the invariant for the whole component.
    const unguarded = parseRules(css)
      .filter((r) => r.selector.includes(':hover'))
      .filter((r) => !r.atRules.some((a) => /hover\s*:\s*hover/.test(a)));
    expect(unguarded.map((r) => r.selector)).toEqual([]);
  });

  it('carries the stop class exactly when it shows the stop glyph', async () => {
    const { container, rerender } = render(Composer, {
      props: { onSend: () => {}, onCancel: () => {}, busy: false },
    });
    const primary = () =>
      container.querySelector('.icon-btn.send:not([aria-label="Finish recording"])')!;

    expect(primary().classList.contains('stop')).toBe(false);
    expect(primary().getAttribute('aria-label')).toBe('Send');
    expect(primary().querySelector('svg')?.getAttribute('class')).toContain('arrow-up');

    await rerender({ onSend: () => {}, onCancel: () => {}, busy: true });
    expect(primary().classList.contains('stop')).toBe(true);
    expect(primary().getAttribute('aria-label')).toBe('Stop');
    expect(primary().querySelector('svg')?.getAttribute('class')).toContain('square');
  });

  it('keeps whatever is typed when the turn is stopped', async () => {
    // The issue's second invariant: a stop is not a discard. Whatever was
    // typed while the turn ran is still there to edit and resend.
    const onCancel = vi.fn();
    const { container, rerender } = render(Composer, {
      props: { onSend: () => {}, onCancel, busy: true },
    });
    const field = container.querySelector('textarea')!;
    await fireEvent.input(field, { target: { value: 'second thought' } });

    await fireEvent.click(container.querySelector('.icon-btn.send.stop')!);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(field.value).toBe('second thought');

    // And still there once the cancel is confirmed and the control reverts.
    await rerender({ onSend: () => {}, onCancel, busy: false });
    expect(container.querySelector('textarea')!.value).toBe('second thought');
  });
});
