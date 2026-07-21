// Generic prefix-autocomplete primitive for the chat composer. The engine
// (useAutocomplete) is trigger-agnostic; each trigger is described by a
// CompletionProvider. Adding a new trigger (a `!command`, a `!model` alias, a
// future `@mention`) is a new provider object, not a composer change.

export interface Suggestion {
  /** Text spliced in place of the match range when accepted, e.g. "!more ". */
  value: string;
  /** Primary label shown in the row, e.g. "!more". */
  label: string;
  /** Secondary muted line (e.g. command help text). Optional. */
  description?: string;
  /** Stable key for keyed rendering + ARIA option ids. */
  key: string;
}

export interface TriggerMatch {
  /** The query token after the trigger, e.g. "mo" for input "!mo". */
  query: string;
  /** [start, end] char offsets in the full text to replace on accept. */
  range: [number, number];
}

export interface CompletionProvider {
  id: string;
  /**
   * Return a TriggerMatch if this provider is active for (text, caret), else
   * null. Must be cheap and synchronous — called on every keystroke/caret move.
   */
  match(text: string, caret: number): TriggerMatch | null;
  /**
   * Suggestions for the query. May be sync (in-memory filter) or async (first
   * call fetches + caches). The engine drops a resolved result whose query is
   * no longer current (stale-guard).
   */
  getSuggestions(query: string): Suggestion[] | Promise<Suggestion[]>;
}
