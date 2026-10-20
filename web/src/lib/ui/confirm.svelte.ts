/**
 * The state behind a confirm dialog that acts on one thing: which thing is
 * being confirmed, and whether the dialog is open. The dialog is open exactly
 * while there is a target, so the two can never disagree.
 *
 * ```svelte
 * const remove = createConfirm<Card>();
 * <ConfirmDialog bind:open={remove.open} onConfirm={() => deleteCard(remove.target!)} … />
 * ```
 *
 * Closing the dialog (a bound `open` set false, or `cancel()`) drops the
 * target. The target is held raw, not as a deep proxy, so it stays the object
 * the page passed in.
 */
export function createConfirm<T>() {
  let target = $state.raw<T | null>(null);
  return {
    get target(): T | null {
      return target;
    },
    get open(): boolean {
      return target !== null;
    },
    set open(next: boolean) {
      if (!next) target = null;
    },
    ask(next: T) {
      target = next;
    },
    cancel() {
      target = null;
    },
  };
}

export type Confirm<T> = ReturnType<typeof createConfirm<T>>;
