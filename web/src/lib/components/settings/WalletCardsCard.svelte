<script lang="ts">
  import { AuthError, updateWalletCard, removeWalletCard, type WalletCard } from '$lib/api';
  import {
    Badge,
    Button,
    ConfirmDialog,
    EmptyState,
    KebabMenu,
    type KebabItem,
    Card,
  } from '@minot/ui';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import CardFormModal from './CardFormModal.svelte';
  let {
    cards,
    writable,
    onSaved,
    onError,
  }: {
    cards: WalletCard[];
    writable: boolean;
    onSaved: () => void;
    onError: (e: unknown) => void;
  } = $props();
  let form: { card: WalletCard | null } | null = $state(null);
  const removing = createConfirm<WalletCard>();
  let busy = $state(false);
  function cardState(card: WalletCard) {
    const now = new Date();
    if (
      card.exp_year < now.getUTCFullYear() ||
      (card.exp_year === now.getUTCFullYear() && card.exp_month < now.getUTCMonth() + 1)
    )
      return 'Expired';
    return card.state === 'paused' ? 'Paused' : 'Active';
  }
  async function mutate(action: () => Promise<unknown>) {
    if (busy || !writable) return;
    busy = true;
    try {
      await action();
      onSaved();
    } catch (e) {
      onError(e);
    } finally {
      busy = false;
    }
  }
  // A refusal rejects into the dialog, which stays open to say so; an expired
  // session goes to onError, the one place that ends it.
  async function remove(card: WalletCard) {
    if (busy || !writable) return;
    busy = true;
    try {
      await removeWalletCard(card.id);
      onSaved();
    } catch (e) {
      if (e instanceof AuthError) {
        onError(e);
        return;
      }
      throw e;
    } finally {
      busy = false;
    }
  }
  function menu(card: WalletCard): KebabItem[] {
    return [
      {
        label: 'Edit details',
        disabled: busy || !writable,
        onSelect: () => {
          form = { card };
        },
      },
      {
        label: card.state === 'active' ? 'Pause' : 'Resume',
        disabled: busy || !writable,
        onSelect: () =>
          mutate(() =>
            updateWalletCard(card.id, { state: card.state === 'active' ? 'paused' : 'active' }),
          ),
      },
      {
        label: 'Remove',
        danger: true,
        disabled: busy || !writable,
        onSelect: () => removing.ask(card),
      },
    ];
  }
</script>

<Card title="Cards">
  {#snippet actions()}<Button
      variant="primary"
      size="sm"
      disabled={!writable || busy}
      onclick={() => {
        form = { card: null };
      }}>Add card</Button
    >{/snippet}
  {#if cards.length === 0}<EmptyState>No cards yet.</EmptyState>{/if}
  <ul class="divided-list">
    {#each cards as card (card.id)}
      <li class="card-row">
        <div class="card-main">
          <span>{card.label}</span><span class="caption"
            >{card.brand} · •••• {card.last_four} · {String(card.exp_month).padStart(
              2,
              '0',
            )}/{card.exp_year}</span
          >
        </div>
        <Badge size="sm" variant={cardState(card) === 'Active' ? 'success' : 'warn'}
          >{cardState(card)}</Badge
        >
        <KebabMenu items={menu(card)} ariaLabel="Actions for {card.label}" />
      </li>
    {/each}
  </ul>
  <p class="caption">
    These limits control what Istota will fill, not what a merchant charges. Use a card with its own
    issuer-side spending limit.
  </p>
</Card>
{#if form}<CardFormModal
    card={form.card}
    onClose={() => {
      form = null;
    }}
    {onSaved}
    {onError}
  />{/if}
<ConfirmDialog
  bind:open={removing.open}
  title="Remove card"
  message="Remove {removing.target?.label}? Open purchases on this card will be cancelled."
  confirmLabel="Remove"
  busyLabel="Removing…"
  onConfirm={() => removing.target && remove(removing.target)}
/>

<style>
  .card-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .card-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
