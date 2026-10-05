<script lang="ts">
  import { Mail } from '@lucide/svelte';
  import { base } from '$app/paths';
  import { chatFileUrl } from '$lib/api';
  import { copyText } from '$lib/clipboard';
  import { KebabMenu, type KebabItem } from '$lib/components/ui';
  import { renderUntrustedMarkdown } from '$lib/markdown';
  import {
    addressLabel,
    formatMailDate,
    formatSize,
    MAIL_STATE_LABELS,
    senderBadge,
    trustedButFailed,
    type MailCardData,
  } from './mailCard';
  import type { MailAddress, MailDiscuss } from '$lib/api';

  /** Addresses a list shows before it folds the rest into "+N more". */
  const SHOWN_ADDRESSES = 3;

  let {
    card,
    collapsed = false,
    onDiscuss,
  }: {
    card: MailCardData;
    // The one-line form used under a private note; it expands to the card.
    collapsed?: boolean;
    // Opens the private room with the composer linked to the thread, for a
    // card with no note to land on (section 0c).
    onDiscuss?: (discuss: MailDiscuss) => void;
  } = $props();

  // Seeded from the prop once: the reader's expand is theirs from then on.
  // svelte-ignore state_referenced_locally
  let folded = $state(collapsed);
  let quotedShown = $state(false);
  let headersShown = $state(false);
  let expandedLists = $state<Record<string, boolean>>({});

  const incoming = $derived(card.direction === 'in');
  const directionLabel = $derived(incoming ? 'Received by email' : 'Sent by email');
  const badge = $derived(senderBadge(card));
  const stateLabel = $derived(card.state ? MAIL_STATE_LABELS[card.state] : null);
  const date = $derived(formatMailDate(card.date));
  const counterpart = $derived.by(() => {
    if (incoming) return card.from ? `From ${addressLabel(card.from, card.labels)}` : '';
    const first = card.to[0] ?? card.cc[0];
    return first ? `To ${addressLabel(first, card.labels)}` : '';
  });
  const copyAddress = $derived(incoming ? card.from?.address : (card.to[0] ?? card.cc[0])?.address);
  // Headers are the stored metadata, so a card with none offers none.
  const hasHeaders = $derived(incoming && !card.fallback);
  // A received body is read as markdown, so an HTML mail's converted links and
  // quotes render. A sent one stays as typed: it was mailed as plain text.
  const bodyHtml = $derived(incoming ? renderUntrustedMarkdown(card.body) : '');
  const restHtml = $derived(incoming ? renderUntrustedMarkdown(card.rest) : '');

  const menu = $derived.by(() => {
    const items: KebabItem[] = [];
    if (copyAddress) {
      const address = copyAddress;
      items.push({
        label: 'Copy address',
        onSelect: () => void copyText(address, { label: 'Address copied' }),
      });
    }
    if (card.discuss && onDiscuss) {
      const discuss = card.discuss;
      items.push({
        label: 'Discuss in private chat',
        onSelect: () => onDiscuss(discuss),
      });
    } else if (card.notePath) {
      items.push({
        label: 'Discuss in private chat',
        href: `${base}${card.notePath}`,
        reload: true,
      });
    }
    if (hasHeaders) {
      items.push({
        label: headersShown ? 'Hide headers' : 'Show headers',
        onSelect: () => (headersShown = !headersShown),
      });
    }
    return items;
  });

  const checkLabel: Record<string, string> = {
    verified: 'Verified',
    failed: 'Failed',
    none: 'Not checked',
  };
</script>

{#snippet people(label: string, list: MailAddress[])}
  {#if list.length}
    {@const open = !!expandedLists[label]}
    {@const shown = open ? list : list.slice(0, SHOWN_ADDRESSES)}
    <div class="mail-people">
      {label}:{' '}{#each shown as person, i (i)}<span class="mail-person" title={person.address}
          >{addressLabel(person, card.labels)}</span
        >{i < shown.length - 1 ? ', ' : ''}{/each}
      {#if !open && list.length > SHOWN_ADDRESSES}
        {' '}<button
          class="mail-link"
          type="button"
          onclick={() => (expandedLists = { ...expandedLists, [label]: true })}
          >+{list.length - SHOWN_ADDRESSES} more</button
        >
      {/if}
    </div>
  {/if}
{/snippet}

<div class="mail-card" data-testid="mail-card" data-direction={card.direction}>
  {#if folded}
    <button
      class="mail-collapsed"
      data-testid="mail-collapsed"
      type="button"
      aria-expanded="false"
      onclick={() => (folded = false)}
    >
      <span class="mail-mark" aria-hidden="true"><Mail size={13} /></span>
      <span>{directionLabel}</span>
      {#if counterpart}<span>· {counterpart}</span>{/if}
      {#if card.subject}<span class="mail-subject-inline">· {card.subject}</span>{/if}
      {#if stateLabel}<span class="mail-badge">{stateLabel}</span>{/if}
    </button>
  {:else}
    <div class="mail-head">
      <span class="mail-mark" aria-hidden="true"><Mail size={13} /></span>
      <span>{directionLabel}</span>
      {#if date}<span class="mail-date">{date}</span>{/if}
      <span class="mail-menu">
        <KebabMenu items={menu} ariaLabel="Mail actions" />
      </span>
    </div>
    {#if card.subject}
      <div class="mail-subject">{card.subject}</div>
    {/if}
    {#if incoming && card.from}
      <div class="mail-people">
        From: <span class="mail-person">{addressLabel(card.from, card.labels)}</span>
        {#if addressLabel(card.from, card.labels) !== card.from.address}
          <span class="mail-address">&lt;{card.from.address}&gt;</span>
        {/if}
      </div>
    {/if}
    {@render people('To', card.to)}
    {@render people('Cc', card.cc)}
    {#if badge || stateLabel}
      <div class="mail-badges">
        {#if badge}
          <span
            class="mail-badge"
            class:mail-badge-warn={card.senderCheck === 'failed' && !card.trusted}
            data-testid="sender-badge">{badge}</span
          >
          {#if trustedButFailed(card)}
            <span class="mail-badge mail-badge-warn" data-testid="sender-check-failed"
              >Failed sender check</span
            >
          {/if}
        {/if}
        {#if stateLabel}
          <span
            class="mail-badge"
            class:mail-badge-warn={card.state === 'failed'}
            data-testid="mail-state">{stateLabel}</span
          >
        {/if}
        {#if card.state === 'held' && card.notePath}
          <a class="mail-link" href="{base}{card.notePath}" data-sveltekit-reload
            >Open in your private chat</a
          >
        {/if}
      </div>
    {/if}
    {#if card.body}
      {#if incoming}
        <div class="mail-body markdown">{@html bodyHtml}</div>
      {:else}
        <div class="mail-body mail-plain">{card.body}</div>
      {/if}
    {/if}
    {#if card.rest}
      <button
        class="mail-link mail-quoted-toggle"
        type="button"
        aria-expanded={quotedShown}
        onclick={() => (quotedShown = !quotedShown)}
        >{quotedShown ? 'Hide quoted text' : 'Show quoted text'}</button
      >
      {#if quotedShown}
        <div class="mail-body markdown mail-quoted">{@html restHtml}</div>
      {/if}
    {/if}
    {#if card.attachments.length}
      <div class="mail-attachments">
        {#each card.attachments as item, i (i)}
          {@const size = formatSize(item.size)}
          {#if item.path}
            <a class="mail-attachment" href={chatFileUrl(item.path)} download={item.filename}
              >📎 {item.filename}{#if size}<span class="mail-size"> {size}</span>{/if}</a
            >
          {:else}
            <span class="mail-attachment"
              >📎 {item.filename}{#if size}<span class="mail-size"> {size}</span>{/if}</span
            >
          {/if}
        {/each}
      </div>
    {/if}
    {#if hasHeaders && headersShown}
      <dl class="mail-headers" data-testid="mail-headers">
        <dt>Message-ID</dt>
        <dd>{card.messageId || '—'}</dd>
        <dt>In-Reply-To</dt>
        <dd>{card.inReplyTo || '—'}</dd>
        <dt>Date</dt>
        <dd>{card.date || '—'}</dd>
        <dt>Sender check</dt>
        <dd>{checkLabel[card.senderCheck ?? 'none']} ({badge})</dd>
        <dt>Trusted</dt>
        <dd>{card.trusted ? 'Yes' : 'No'}</dd>
      </dl>
    {/if}
  {/if}
</div>

<style>
  /* The external-turn surface (`Message.svelte`'s `.external`), set as its own
     block so the card reads the same in a thread room, the private email
     room and under a note. */
  .mail-card {
    width: 100%;
    max-width: var(--chat-body-max);
    padding: var(--space-2);
    background: var(--surface-badge);
    border-left: 2px solid var(--border-hover);
    border-radius: var(--radius-sm);
  }
  .mail-head,
  .mail-collapsed {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
  .mail-collapsed {
    width: 100%;
    padding: 0;
    background: none;
    border: none;
    font: inherit;
    font-size: var(--text-xs);
    text-align: left;
    cursor: pointer;
  }
  .mail-mark {
    display: inline-flex;
    align-self: center;
    color: var(--text-muted);
  }
  .mail-menu {
    margin-left: auto;
    align-self: center;
  }
  /* Attacker-supplied and routinely long, so it clips in the one-line form. */
  .mail-subject-inline {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-secondary);
  }
  .mail-subject {
    margin-top: var(--space-1);
    font-size: var(--text-sm);
    font-weight: 600;
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }
  .mail-people {
    margin-top: var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-secondary);
    overflow-wrap: anywhere;
  }
  .mail-address {
    color: var(--text-muted);
  }
  .mail-badges {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2);
    margin-top: var(--space-1);
  }
  .mail-badge {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }
  .mail-badge-warn {
    color: var(--status-danger-fg);
  }
  .mail-link {
    padding: 0;
    background: none;
    border: none;
    color: var(--link);
    font: inherit;
    font-size: var(--text-xs);
    cursor: pointer;
  }
  .mail-link:hover {
    text-decoration: underline;
  }
  .mail-body {
    margin-top: var(--space-2);
    font-size: var(--text-sm);
    line-height: 1.5;
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }
  .mail-plain {
    white-space: pre-wrap;
  }
  .mail-quoted,
  .mail-quoted :global(blockquote) {
    color: var(--text-muted);
  }
  .mail-body :global(.md-link-dest) {
    color: var(--text-muted);
  }
  .mail-quoted-toggle {
    display: block;
    margin-top: var(--space-1);
  }
  .mail-attachments {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }
  .mail-attachment {
    font-size: var(--text-xs);
    color: var(--text-secondary);
  }
  a.mail-attachment {
    color: var(--link);
  }
  .mail-size {
    color: var(--text-muted);
  }
  .mail-headers {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: var(--space-1) var(--space-2);
    margin: var(--space-2) 0 0;
    font-size: var(--text-xs);
    color: var(--text-secondary);
  }
  .mail-headers dt {
    color: var(--text-muted);
  }
  .mail-headers dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
</style>
