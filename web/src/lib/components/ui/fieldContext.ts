import { getContext, setContext } from 'svelte';

/**
 * How a control inside a `Field` takes the field's caption as its name.
 *
 * `native` is a control a `<label for>` can name and focus: a text input, a
 * textarea, a checkbox. `labelledby` is anything a label must not activate,
 * which is every button-shaped control: a bits-ui `Select` trigger, a combobox,
 * a group of inputs. Clicking the caption focuses a `native` control and does
 * nothing for a `labelledby` one, because a `<label>` around or pointing at a
 * button makes the caption a second way to press it.
 */
export type FieldControlKind = 'native' | 'labelledby';

export interface FieldContext {
  /** The id a `native` control takes, so the caption's `for` reaches it. */
  readonly controlId: string;
  /** The caption's id, for a `labelledby` control's `aria-labelledby`. */
  readonly labelId: string;
  /** The ids of the description, warning and error, or undefined with none. */
  readonly describedBy: string | undefined;
  /** The field shows an error. */
  readonly invalid: boolean;
  readonly required: boolean;
  /**
   * Called once by a control while it initialises. The first control to claim
   * the field is its control and gets `true`; any later one gets `false` and
   * leaves the field's ids alone, so two controls never share an id. `id`
   * reads the control's own id when the caller gave it one, so the caption's
   * `for` points at that instead of `controlId`.
   */
  claim(kind: FieldControlKind, id?: () => string | null | undefined): boolean;
}

const KEY = Symbol('field');

/** `null` hides an outer field from the subtree (a composite's inner input). */
export function setFieldContext(context: FieldContext | null): void {
  setContext(KEY, context);
}

/** The nearest enclosing field, or null outside any `Field`. */
export function getFieldContext(): FieldContext | null {
  return getContext<FieldContext | null | undefined>(KEY) ?? null;
}

/** Joins describedby id lists, dropping empties; undefined when nothing is left. */
export function joinIds(...ids: (string | null | undefined)[]): string | undefined {
  const joined = ids
    .filter((id): id is string => !!id && id.trim() !== '')
    .join(' ')
    .trim();
  return joined || undefined;
}
