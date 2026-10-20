// The one rule `Button` and `IconButton` share for which element they render.
// An action that navigates is a real `<a>` (middle-click, open in a new tab,
// the status-bar URL); a link has no disabled state, so a disabled or busy one
// falls back to a disabled `<button>` rather than an inert-looking anchor that
// still navigates. The two used to write this out separately and disagreed:
// `Button` fell back on `disabled || loading`, `IconButton` on `disabled` only.
//
// A function rather than a component: each caller renders the element itself
// through `<svelte:element>`, so its scoped styles still reach it.

export interface LinkOrButtonInput {
  href?: string;
  download?: string;
  target?: string;
  rel?: string;
  /** Full navigation, for a path the server answers itself (a redirect). */
  reload?: boolean;
  type?: 'button' | 'submit' | 'reset';
  disabled?: boolean;
  loading?: boolean;
}

export interface LinkOrButton {
  tag: 'a' | 'button';
  attrs: Record<string, unknown>;
}

export function linkOrButton(input: LinkOrButtonInput): LinkOrButton {
  const inert = Boolean(input.disabled || input.loading);
  if (input.href && !inert) {
    return {
      tag: 'a',
      attrs: {
        href: input.href,
        download: input.download,
        target: input.target,
        rel: input.rel,
        'data-sveltekit-reload': input.reload ? '' : undefined,
      },
    };
  }
  return {
    tag: 'button',
    attrs: {
      // A link that fell back is never a form's submit button.
      type: input.href ? 'button' : (input.type ?? 'button'),
      disabled: inert,
      'aria-busy': input.loading || undefined,
    },
  };
}
