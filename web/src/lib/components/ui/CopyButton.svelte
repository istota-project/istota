<script lang="ts">
  import { Copy } from '@lucide/svelte';
  import { copyText } from '$lib/clipboard';
  import Button from './Button.svelte';
  import IconButton from './IconButton.svelte';

  interface Props {
    /** What goes on the clipboard, or a function that reads it at click time. */
    text: string | (() => string);
    /** The button's name, and its visible text as a `button`. */
    label?: string;
    /** `icon` is a glyph with `label` as its name; `button` a secondary button. */
    variant?: 'icon' | 'button';
    size?: 'sm' | 'md';
    disabled?: boolean;
    /** The confirmation's wording, when "Copied" says too little. */
    copiedLabel?: string;
  }

  let {
    text,
    label = 'Copy',
    variant = 'icon',
    size = 'sm',
    disabled = false,
    copiedLabel,
  }: Props = $props();

  function copy() {
    const value = typeof text === 'function' ? text() : text;
    void (copiedLabel ? copyText(value, { label: copiedLabel }) : copyText(value));
  }
</script>

<!--
  One button that copies. It goes through `copyText`, which raises the
  confirmation and reports the two failures a bare `navigator.clipboard` call
  passes off as success, so the button keeps no "Copied!" state of its own.
-->
{#if variant === 'button'}
  <Button variant="secondary" {size} {disabled} onclick={copy}>{label}</Button>
{:else}
  <IconButton {label} title={label} {size} {disabled} onclick={copy}>
    <Copy size={14} />
  </IconButton>
{/if}
