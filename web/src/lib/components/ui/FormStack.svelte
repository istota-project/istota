<svelte:options runes={true} />

<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    /** `fieldset` (the default) for a group of fields, `form` for one that submits. */
    as?: 'form' | 'fieldset';
    /** A step of the spacing ramp between the fields. */
    gap?: 2 | 3 | 4 | 6;
    /** Disables every control in the stack: the busy state while a save is out. */
    disabled?: boolean;
    /** The group's visible name, drawn as a `Field` label. Fieldset only. */
    legend?: string;
    /** As a form: called with the default already prevented. */
    onsubmit?: (e: SubmitEvent) => void;
    /** As a form: leave validation to the handler rather than the browser. */
    novalidate?: boolean;
    /** Placement only (a margin, a grid span), never appearance. */
    class?: string;
    children: Snippet;
    [attr: `data-${string}` | `aria-${string}`]: unknown;
  }

  let {
    as = 'fieldset',
    gap = 3,
    disabled = false,
    legend,
    onsubmit,
    novalidate = false,
    class: className = '',
    children,
    ...rest
  }: Props = $props();

  const GAPS = {
    2: 'var(--space-2)',
    3: 'var(--space-3)',
    4: 'var(--space-4)',
    6: 'var(--space-6)',
  } as const;

  function submit(e: SubmitEvent) {
    e.preventDefault();
    onsubmit?.(e);
  }
</script>

<!--
  A column of fields with the fieldset reset (no border, padding or margin,
  and the min-width a fieldset otherwise floors at its content), which six
  files restated; nine of the app's fourteen fieldsets existed only to disable
  a form while its save was out. A plain column inside something that is already the form (a
  FormModal body) is `.vstack`, not this.
-->
{#if as === 'form'}
  <!-- The stack is a fieldset inside the form, always rendered rather than
       added while busy: swapping the wrapper would remount every field and
       take focus out of the one being typed in. -->
  <form class={className || undefined} onsubmit={submit} {novalidate} {...rest}>
    <fieldset class="ui-form-stack" style:gap={GAPS[gap]} {disabled}>
      {@render children()}
    </fieldset>
  </form>
{:else}
  <fieldset class="ui-form-stack {className}" style:gap={GAPS[gap]} {disabled} {...rest}>
    {#if legend}<legend class="ui-form-stack-legend muted">{legend}</legend>{/if}
    {@render children()}
  </fieldset>
{/if}

<style>
  .ui-form-stack {
    display: flex;
    flex-direction: column;
    /* A fieldset floors at its content's min-content width without this,
       and a long value pushes the form past its card. */
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  /* The Field label's size, on `.muted` for its colour. A rendered legend
     sits outside the flex layout, so the gap never reaches it. */
  .ui-form-stack-legend {
    padding: 0;
    margin-bottom: var(--space-1);
    font-size: var(--text-sm);
  }
</style>
