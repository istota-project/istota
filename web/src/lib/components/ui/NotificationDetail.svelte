<script lang="ts">
  /**
   * One notification, opened.
   *
   * The full body, the `status_note` where there is one, and **every** action —
   * the row above shows at most two. Dismiss is always offered, including on a
   * row whose source is no longer registered: a notification nobody can explain
   * is still one the user should be able to clear.
   *
   * **No `{@html}`, here or anywhere in this feature.** `title` and `body` carry
   * a stranger's subject line and a bot-composed question built from it, so both
   * are text nodes. The body additionally sets `white-space: pre-wrap`, which
   * renders its line breaks without turning any of it into markup.
   */
  import { base } from '$app/paths';
  import { isSafeActionPath, type NotificationAction, type ResolvedNotification } from '$lib/api';
  import Button from './Button.svelte';
  import Modal from './Modal.svelte';

  interface Props {
    item: ResolvedNotification | null;
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onAction: (item: ResolvedNotification, action: NotificationAction) => void;
    onDismiss: (item: ResolvedNotification) => void;
  }

  let { item, open, onOpenChange, onAction, onDismiss }: Props = $props();

  const variants: Record<string, 'primary' | 'secondary' | 'danger'> = {
    primary: 'primary',
    default: 'secondary',
    danger: 'danger',
  };

  /** Only the actions that can actually be issued.
   *
   * The modal renders every action the row has, unlike the row itself which
   * takes the first two — so this drops nothing a user could have used. An
   * action whose path fails the allowlist is not offered rather than rendered
   * as a button that refuses when pressed. */
  const actions = $derived(
    (item?.actions ?? []).filter(
      (a) =>
        (a.method === 'POST' && isSafeActionPath(a.endpoint)) ||
        (a.method === 'LINK' && isSafeActionPath(a.href)),
    ),
  );
</script>

{#if item}
  <Modal {open} {onOpenChange} title={item.title} width="30rem">
    {#if item.body}
      <p class="detail-body">{item.body}</p>
    {/if}
    {#if item.status_note}
      <!-- Says *why* there are no actions. An empty action list on its own
           conflates "this draft is mid-send" with "nobody registered this
           source", which are different things to tell someone. -->
      <p class="banner info detail-note">{item.status_note}</p>
    {/if}
    {#if isSafeActionPath(item.link)}
      <!-- Checked, not merely truthy-tested. `link` and `href` are the *worse*
           pair of the three URL fields: they land in an anchor, where a
           text-node rule buys nothing and a `javascript:` or off-origin
           absolute URL would sail straight through. The server validates every
           view it emits, but the browser is the side that follows the link, so
           it checks too. A path that fails is simply not offered. -->
      <p class="detail-link"><a href="{base}{item.link}">Open</a></p>
    {/if}
    {#snippet footer()}
      <Button variant="subtle" size="sm" onclick={() => onDismiss(item!)}>Dismiss</Button>
      {#each actions as action (action.id)}
        {#if action.method === 'LINK' && action.href}
          <Button
            variant={variants[action.kind] ?? 'secondary'}
            size="sm"
            href="{base}{action.href}">{action.label}</Button
          >
        {:else if action.method === 'POST'}
          <Button
            variant={variants[action.kind] ?? 'secondary'}
            size="sm"
            onclick={() => onAction(item!, action)}>{action.label}</Button
          >
        {/if}
      {/each}
    {/snippet}
  </Modal>
{/if}

<style>
  .detail-body {
    margin: 0;
    /* The stored text is what a producer wrote; its line breaks are part of it.
       pre-wrap renders them without any of it becoming markup. */
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-secondary);
    font-size: var(--text-sm);
    line-height: 1.5;
  }

  .detail-note {
    margin: var(--space-3) 0 0;
  }

  .detail-link {
    margin: var(--space-3) 0 0;
    font-size: var(--text-sm);
  }
</style>
