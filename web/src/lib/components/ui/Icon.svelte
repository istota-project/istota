<svelte:options runes={true} />

<!--
  One lucide glyph in a box sized by `--icon-*`. The glyph draws at 100% of
  the box, so it is sized in rem and follows the text-scale preference, where
  lucide's own `size` writes pixels that do not.
-->
<script lang="ts" module>
  export type IconSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl';
</script>

<script lang="ts">
  import type { Component } from 'svelte';

  interface Props {
    icon: Component;
    /** `xs` 12, `sm` 14, `md` 16, `lg` 20, `xl` 28 at the unscaled root. */
    size?: IconSize;
    /** Stroke width, lucide's 2 when omitted. */
    stroke?: number;
    /** A named image. Without it the glyph is decoration and hidden. */
    label?: string;
    /** A filled glyph (a set star, the play badge). */
    fill?: string;
    /** Placement only. */
    class?: string;
  }

  let { icon: Glyph, size = 'sm', stroke, label, fill, class: className = '' }: Props = $props();
</script>

<span
  class="ui-icon ui-icon-{size} {className}"
  role={label ? 'img' : undefined}
  aria-label={label}
  aria-hidden={label ? undefined : 'true'}
  ><Glyph size="100%" strokeWidth={stroke} fill={fill ?? 'none'} aria-hidden="true" /></span
>

<style>
  .ui-icon {
    display: inline-flex;
    flex: none;
    align-items: center;
    justify-content: center;
  }
  .ui-icon-xs {
    width: var(--icon-xs);
    height: var(--icon-xs);
  }
  .ui-icon-sm {
    width: var(--icon-sm);
    height: var(--icon-sm);
  }
  .ui-icon-md {
    width: var(--icon-md);
    height: var(--icon-md);
  }
  .ui-icon-lg {
    width: var(--icon-lg);
    height: var(--icon-lg);
  }
  .ui-icon-xl {
    width: var(--icon-xl);
    height: var(--icon-xl);
  }
</style>
