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
 * Rules that can style either filled control — Send or Stop.
 *
 * Keyed on the *subject* compound — the last one, the element the rule actually
 * styles — and on any of the classes those buttons carry, because
 * `.tools .send:hover` and `.icon-btn:hover` are both spellings that reach one
 * and either would reintroduce the defect.
 *
 * A rule opts out only by excluding **both**, and that is the half ISSUE-238
 * added: Stop used to be a second mode of the send button, so `:not(.send)`
 * was enough to say "not a filled control". Now that it is its own element,
 * `.icon-btn:not(.send):hover` reaches it and would paint it grey — so a rule
 * naming only one exclusion is still in scope here. Tested per compound rather
 * than per rule: a selector list is several rules to the cascade, so one
 * excluded branch must not exempt its siblings.
 */
function sendButtonRules(): Rule[] {
  return parseRules(css).filter((r) => {
    const subject = r.selector.split(/[\s>+~]+/).pop() ?? '';
    if (subject.includes(':not(.send)') && subject.includes(':not(.stop)')) return false;
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
    expect(seen).toContain('.icon-btn.stop');
    expect(seen).toContain('.icon-btn.send:disabled');
  });

  it('paints the two controls in two different colours', () => {
    // The symptom itself, rather than the mechanism below it: a stop button
    // that renders in the idle blue is the whole of ISSUE-201, and every
    // structural assertion here is equally happy with both of them blue.
    expect(fillOf('.icon-btn.send')).toBe('var(--accent-blue)');
    expect(fillOf('.icon-btn.stop')).toBe('var(--status-danger-fg)');
    expect(fillOf('.icon-btn.send')).not.toBe(fillOf('.icon-btn.stop'));
  });

  it('lets nothing but the button and :disabled decide a filled control fill', () => {
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

  it('keeps the two fills on two elements, so neither can outrank the other', () => {
    // What replaced the old document-order tie-break (ISSUE-238). While Send
    // and Stop were one element in two modes, `.icon-btn.send:disabled` and
    // `.icon-btn.send.stop` had equal specificity and order was the difference
    // between a red stop button and a grey one wearing a stop glyph. Two
    // elements means no selector reaches both, so the tie cannot arise — and
    // this is the assertion that says so, since re-merging them would restore
    // it silently.
    const fills = sendButtonRules().filter((r) => declaresFill(r.body));
    for (const r of fills) {
      const subject = r.selector.split(/[\s>+~]+/).pop() ?? '';
      expect([subject, subject.includes('.send') && subject.includes('.stop')]).toEqual([
        subject,
        false,
      ]);
    }
  });

  it('does not animate either filled control', () => {
    // alice's call, and the right one: the fill says which button this is, so
    // easing it means the colour and the glyph disagree for the length of it.
    for (const selector of ['.icon-btn.send', '.icon-btn.stop']) {
      const rule = sendButtonRules().find((r) => r.selector === selector);
      expect(rule).toBeTruthy();
      expect(rule!.body).toMatch(/transition:\s*none/);
    }
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

  it('is always Send, and grows a Stop beside it while a turn runs', async () => {
    const { container, rerender } = render(Composer, {
      props: { onSend: () => {}, onCancel: () => {}, busy: false },
    });
    const send = () =>
      container.querySelector('.icon-btn.send:not([aria-label="Finish recording"])')!;
    const stop = () => container.querySelector('.icon-btn.stop');

    expect(send().getAttribute('aria-label')).toBe('Send');
    expect(send().querySelector('svg')?.getAttribute('class')).toContain('arrow-up');
    expect(stop()).toBeNull();

    await rerender({ onSend: () => {}, onCancel: () => {}, busy: true });
    // Send is still Send. The mode flip is what ISSUE-238 removed: a control
    // that meant Stop while a turn ran is a control that cannot send the
    // message the queue exists to accept.
    expect(send().getAttribute('aria-label')).toBe('Send');
    expect(send().querySelector('svg')?.getAttribute('class')).toContain('arrow-up');
    expect(stop()).toBeTruthy();
    expect(stop()!.getAttribute('aria-label')).toBe('Stop');
    expect(stop()!.querySelector('svg')?.getAttribute('class')).toContain('square');

    await rerender({ onSend: () => {}, onCancel: () => {}, busy: false });
    expect(stop()).toBeNull();
  });

  it('is not offered without something to cancel', async () => {
    // Same condition the old mode carried: no `onCancel` prop, no Stop. A
    // button that cannot stop anything is worse than no button.
    const { container } = render(Composer, { props: { onSend: () => {}, busy: true } });
    expect(container.querySelector('.icon-btn.stop')).toBeNull();
    expect(container.querySelector('.icon-btn.send')).toBeTruthy();
  });

  it('leaves Send where it was when Stop appears beside it', async () => {
    // The reason Stop is rendered *before* Send rather than after it. iOS
    // Safari re-hit-tests when it delivers a tap's synthesized click, so a
    // Send that moved because a neighbour appeared mid-gesture would take the
    // tap on whatever slid into its place. Growing the row leftwards leaves
    // Send at the end of it in both states.
    const { container, rerender } = render(Composer, {
      props: { onSend: () => {}, onCancel: () => {}, busy: false },
    });
    const tools = () => container.querySelector('.tools')!;
    // The mic is conditional on the browser being able to record, so the row
    // is read by position rather than by an exact list of its children.
    const order = () => [...tools().children].map((c) => c.getAttribute('aria-label'));

    expect(order().indexOf('Send')).toBe(order().length - 1);
    expect(order()).not.toContain('Stop');

    await rerender({ onSend: () => {}, onCancel: () => {}, busy: true });
    expect(order().indexOf('Send')).toBe(order().length - 1);
    expect(order().indexOf('Stop')).toBe(order().length - 2);
  });

  it('never cancels from the send button, whatever the turn is doing', async () => {
    // The whole of the mode-flip hazard, restated as the property that
    // replaced it: one button, one meaning, so a duplicate tap delivered after
    // `busy` flipped cannot arrive as the opposite command. There is no
    // `MODE_FLIP_GUARD_MS` any more because there is nothing left to guard.
    const onSend = vi.fn();
    const onCancel = vi.fn();
    const { container, rerender } = render(Composer, {
      props: { onSend, onCancel, busy: false },
    });
    const field = container.querySelector('textarea')!;
    const send = container.querySelector<HTMLButtonElement>(
      '.icon-btn.send:not([aria-label="Finish recording"])',
    )!;
    await fireEvent.input(field, { target: { value: 'one' } });
    await fireEvent.click(send);
    expect(onSend).toHaveBeenCalledTimes(1);

    // The parent flips busy inside that same click; the duplicate delivery
    // lands on the same element, which still says Send.
    await rerender({ onSend, onCancel, busy: true });
    await fireEvent.input(field, { target: { value: 'two' } });
    await fireEvent.click(send);
    expect(onCancel).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it('sends while a turn is running, so the message can be queued', async () => {
    const onSend = vi.fn();
    const { container } = render(Composer, {
      props: { onSend, onCancel: () => {}, busy: true },
    });
    const field = container.querySelector('textarea')!;
    await fireEvent.input(field, { target: { value: 'and another thing' } });

    const send = container.querySelector<HTMLButtonElement>(
      '.icon-btn.send:not([aria-label="Finish recording"])',
    )!;
    expect(send.disabled).toBe(false);
    await fireEvent.click(send);
    expect(onSend).toHaveBeenCalledWith('and another thing', [], null);
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

    await fireEvent.click(container.querySelector('.icon-btn.stop')!);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(field.value).toBe('second thought');

    // And still there once the cancel is confirmed and the control goes.
    await rerender({ onSend: () => {}, onCancel, busy: false });
    expect(container.querySelector('textarea')!.value).toBe('second thought');
  });

  it('refuses a send into a full queue, with the reason on screen', async () => {
    const onSend = vi.fn();
    const { container } = render(Composer, {
      props: { onSend, onCancel: () => {}, busy: true, queueFull: true },
    });
    const field = container.querySelector('textarea')!;
    await fireEvent.input(field, { target: { value: 'the eleventh' } });

    const send = container.querySelector<HTMLButtonElement>(
      '.icon-btn.send:not([aria-label="Finish recording"])',
    )!;
    expect(send.disabled).toBe(true);
    expect(container.querySelector('.notice-row')?.textContent).toContain(
      'Too many messages waiting to send',
    );

    // The text stays where the user can see it, and the keyboard send path
    // falls through to the browser rather than going dead.
    const notPrevented = await fireEvent.keyDown(field, { key: 'Enter', metaKey: true });
    expect(onSend).not.toHaveBeenCalled();
    expect(notPrevented).toBe(true);
    expect(field.value).toBe('the eleventh');
  });

  it('takes the refusal from the caller, so an idle room can still send', async () => {
    // The cap governs the queue, and the queue is only consulted while a turn
    // is running — an idle room's send takes the ordinary path and is capped by
    // nothing. The page gates `queueFull` on `busy` for that reason, and this
    // is the composer's half of it: handed false, it refuses nothing and shows
    // nothing, however many held rows are sitting in the transcript.
    const onSend = vi.fn();
    const { container } = render(Composer, {
      props: { onSend, busy: false, queueFull: false },
    });
    const field = container.querySelector('textarea')!;
    await fireEvent.input(field, { target: { value: 'send it' } });

    expect(container.querySelector('.notice-row')).toBeNull();
    const send = container.querySelector<HTMLButtonElement>(
      '.icon-btn.send:not([aria-label="Finish recording"])',
    )!;
    expect(send.disabled).toBe(false);
    await fireEvent.click(send);
    expect(onSend).toHaveBeenCalledWith('send it', [], null);
  });
});
