<script lang="ts">
  import Field from '../ui/Field.svelte';
  import Input from '../ui/Input.svelte';
  import IconButton from '../ui/IconButton.svelte';

  interface Props {
    label: string;
    type?: 'text' | 'email' | 'password' | 'url';
    configured: boolean;
    value: string;
    disabled?: boolean;
    onValueChange: (next: string) => void;
    onRequestClear?: () => void;
  }

  let {
    label,
    type = 'password',
    configured,
    value,
    disabled = false,
    onValueChange,
    onRequestClear,
  }: Props = $props();

  let showClear = $derived(configured && !!onRequestClear);
</script>

<!--
  Field + Input + IconButton. This used to restate Field's descendant rule
  declaration-for-declaration while dropping the two lines that are not
  obvious — the tier `min-height` and the `line-height` that has to be pinned
  beside `font: inherit` — so on touch it computed ~34.8px against a 32px tier
  and stood proud of every control beside it. It renders on five settings
  pages, which made it the widest copy of that bug in the tree.

  `labelled` is false only while the clear button is present: a <button> is a
  labelable element, so inside a <label> it would become the label's implicit
  control and clicking the field's caption would clear the stored secret. With
  no button there is nothing to steal the association, and clicking the caption
  should focus the input as it does in every other field.
-->
<Field {label} labelled={!showClear}>
  <div class="secret-row">
    <Input
      {type}
      {value}
      {disabled}
      autocomplete="new-password"
      placeholder={configured ? '•••• stored — enter to replace' : 'Enter value'}
      aria-label={showClear ? label : undefined}
      oninput={(e: Event) => onValueChange((e.currentTarget as HTMLInputElement).value)}
    />
    {#if showClear}
      <IconButton
        label="Clear stored {label}"
        title="Clear stored value"
        danger
        {disabled}
        onclick={onRequestClear}>×</IconButton
      >
    {:else}
      <!--
        The button's slot is held open when there is nothing stored to clear.
        Both rows end at 24rem either way, so without this the *input* absorbed
        the button's width and an unconfigured field rendered visibly wider
        than a configured one in the same card — which is how the ntfy card
        read, its unset access token stretching past every field above it.
        A mirror of the glyph rather than a measured width, so it cannot drift
        from what IconButton actually renders.
      -->
      <span class="clear-placeholder" aria-hidden="true">×</span>
    {/if}
  </div>
</Field>

<style>
  /* Carries the input's own 24rem cap so the button rides inside it rather
     than pushing past — a field with a clear button and one without have to
     end at the same edge, or the card's right margin moves per row. */
  .secret-row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    max-width: 24rem;
  }

  /* IconButton's `md` padding, and the same glyph at the same inherited font,
     so it occupies exactly the width the real button would. */
  .clear-placeholder {
    flex: none;
    visibility: hidden;
    padding: var(--space-1);
  }
</style>
