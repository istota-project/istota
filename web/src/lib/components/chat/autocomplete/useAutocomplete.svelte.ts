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
  /** The text most recently passed to `sync`. Whatever state the engine holds
   *  — the match range `accept` splices into, the suggestion list, the open
   *  flag — was computed from this string and no other, so a caller whose field
   *  now holds something else is looking at state that has gone stale.
   *  `close()` deliberately leaves this value standing: a caller comparing its
   *  field against it and closing on a mismatch would otherwise be writing one
   *  of its own dependencies. */
  readonly syncedText: string;
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

  // Reactive so a caller can compare its own field against it and notice the
  // engine is holding state for text that has since been replaced.
  let currentText = $state('');

  // Non-reactive request/context bookkeeping.
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
    // Drop any in-flight request as well. Every real provider is `async`, so
    // there is always a pending one between a keystroke and the rows appearing,
    // and a result that lands after the state was cleared would reopen the
    // popover on its own — holding no match, which is worse than leaving it
    // open: `accept` returns null while `onKeydown` still eats the Enter.
    requestSeq++;
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
          res
            .then((list) => {
              if (seq === requestSeq) apply(list);
            })
            .catch(() => {
              if (seq === requestSeq) reset();
            });
        } else {
          apply(res);
        }
        return;
      }
    }
    // No provider matched.
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
    // Unlike Escape (which resets *and* records what it dismissed), an explicit
    // close means the caller has moved on — a different room, a different
    // message. Holding the suppression past that would keep the popover shut
    // for text the caller has since gone back to.
    suppressedText = null;
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
        // The unmodified key only. Shift+Enter is the composer's one newline
        // now that a bare Enter sends, so eating it here would leave the field
        // with no way to break a line while the popover happens to be open —
        // and a modified key was never how a row gets accepted.
        if (e.shiftKey || e.altKey) return false;
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
    get syncedText() {
      return currentText;
    },
    sync,
    onKeydown,
    accept,
    setActive,
    close,
  };
}
