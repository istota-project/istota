<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  interface Props extends Omit<HTMLInputAttributes, 'value' | 'size'> {
    value?: string | number | null;
    /** Marks the value as rejected — sets aria-invalid and the danger border. */
    invalid?: boolean;
    /** For paths, tokens and ids, where character shape carries meaning. */
    monospace?: boolean;
  }

  let {
    value = $bindable(''),
    type = 'text',
    invalid = false,
    monospace = false,
    ...rest
  }: Props = $props();
</script>

<!--
  A plain styled text input. There was no primitive for this: the appearance
  lived in SettingsField's descendant rules, so a control outside a settings
  page had to hand-roll it, and six form components did — with the same
  declarations and three different radii.

  Unbound from any layout, because the label + hint + warning arrangement is
  Field's job. Use them together for a form row; use this alone in a toolbar,
  a table cell, or anywhere a bare control is the whole control.

  `type` is set via an attribute rather than the shorthand `type={type}` on
  purpose: Svelte refuses to bind:value on an input whose type is dynamic, and
  this component's entire point is to take a type from its caller.
-->
<input
  {...rest}
  {type}
  bind:value
  class="ui-input"
  class:mono={monospace}
  class:invalid
  aria-invalid={invalid || undefined}
/>

<style>
  .ui-input {
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
    background: var(--surface-base);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    /* Vertical padding is half the horizontal: the box is set by min-height
       below and the value centres in it, so a taller pad only inflates the
       control. This is also what Field's descendant rules have always given a
       bare <input>, which left an <Input> in a form sitting taller than the
       plain one beside it. */
    padding: var(--space-1) var(--space-2);
    font: inherit;
    font-size: var(--text-sm);
    /* `font: inherit` is a shorthand, so it also pulls in the body's 1.5
       leading — which is what made the box taller than the tier it sits in
       once iOS floored the text at 16px, and it out-specifies any weightless
       rule trying to correct that from outside. Pinned here, next to the
       shorthand that would otherwise win. The value centres in the box that
       min-height gives it, so the leading is not what should set the height. */
    line-height: 1.2;
    /* Inert on its own — the text and padding already exceed this — but it is
       how the input joins a `.control-row`, where the container raises both
       compact tiers to the field height and every control in the row settles
       on the same number. Not on the --control-radius channel: a text input is
       always the field shape, in a row or out of one. */
    min-height: var(--control-height-md);
  }

  .ui-input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .ui-input.mono {
    font-family: var(--font-mono);
  }

  .ui-input.invalid {
    border-color: var(--status-danger-fg);
  }

  /* A checkbox has an intrinsic size and no text of its own, so none of the
     text-input appearance applies to it — only the layout reset does. */
  .ui-input[type='checkbox'],
  .ui-input[type='radio'] {
    width: auto;
    padding: 0;
    border: none;
    background: none;
    /* A checkbox has an intrinsic box, so the field-tier height would stretch
       the hit area into a tall empty strip beside its label. */
    min-height: 0;
  }
</style>
