<script lang="ts">
  import type { Snippet } from 'svelte';
  import Field from '../ui/Field.svelte';

  interface Props {
    label: string;
    /** Guidance shown in a popover behind a "?" beside the label. Optional
     *  reading — anything the user must see belongs in `warning` or `error`. */
    hint?: string;
    /** A condition the user needs to notice but which doesn't block saving —
     *  a stored value that no longer means what it says, say. Rendered inline
     *  precisely because a hover popover is discoverable, not seen. */
    warning?: string;
    error?: string;
    wide?: boolean;
    checkbox?: boolean;
    /** See `Field` — render a `<div>` rather than a `<label>`. */
    labelled?: boolean;
    children: Snippet;
  }

  let {
    label,
    hint,
    warning,
    error,
    wide = false,
    checkbox = false,
    labelled = true,
    children,
  }: Props = $props();
</script>

<!--
  Kept as the name settings pages use, but the layout now lives in ui/Field so
  a form outside a settings page can have it too. Same props, same markup —
  this is a rename-free delegation, not a new component.
-->
<Field {label} {hint} {warning} {error} {wide} {checkbox} {labelled}>
  {@render children()}
</Field>
