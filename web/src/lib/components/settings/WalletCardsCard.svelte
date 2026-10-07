<script lang="ts">
  import { updateWalletCard, removeWalletCard, type WalletCard } from '$lib/api';
  import { Badge, Button, ConfirmDialog, KebabMenu, type KebabItem } from '$lib/components/ui';
  import SettingsCard from './SettingsCard.svelte';
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
  let removing: WalletCard | null = $state(null);
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
        onSelect: () => {
          removing = card;
        },
      },
    ];
  }
</script>

<SettingsCard title="Cards">
  {#snippet actions()}<Button
      variant="primary"
      size="sm"
      disabled={!writable || busy}
      onclick={() => {
        form = { card: null };
      }}>Add card</Button
    >{/snippet}
  {#if cards.length === 0}<p class="empty">No cards yet.</p>{/if}
  <ul class="card-list">
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
</SettingsCard>
{#if form}<CardFormModal
    card={form.card}
    onClose={() => {
      form = null;
    }}
    {onSaved}
    {onError}
  />{/if}
<ConfirmDialog
  open={removing !== null}
  title="Remove card"
  message="Remove {removing?.label}? Open purchases on this card will be cancelled."
  confirmLabel="Remove"
  confirmDisabled={busy}
  onCancel={() => {
    removing = null;
  }}
  onConfirm={() => {
    const card = removing;
    removing = null;
    if (card) void mutate(() => removeWalletCard(card.id));
  }}
/>

<style>
  .card-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .card-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) 0;
  }
  .card-row + .card-row {
    border-top: 1px solid var(--border-subtle);
  }
  .card-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
