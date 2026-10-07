<script lang="ts">
  import { base } from '$app/paths';
  import { cancelWalletPurchase, type WalletPurchase, type WalletSettings } from '$lib/api';
  import { Badge, Button } from '$lib/components/ui';
  import SettingsCard from './SettingsCard.svelte';
  let {
    purchases,
    precision,
    writable,
    moneyEnabled,
    onSaved,
    onError,
  }: {
    purchases: WalletPurchase[];
    precision: WalletSettings['currency_precision'];
    writable: boolean;
    moneyEnabled: boolean;
    onSaved: () => void;
    onError: (e: unknown) => void;
  } = $props();
  let busy = $state(false);
  function amount(purchase: WalletPurchase) {
    const digits = precision.exceptions[purchase.currency] ?? precision.default;
    const format = new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: purchase.currency,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    return format.format(purchase.amount_cents / 10 ** digits);
  }
  async function cancel(id: number) {
    if (busy || !writable) return;
    busy = true;
    try {
      await cancelWalletPurchase(id);
      onSaved();
    } catch (e) {
      onError(e);
    } finally {
      busy = false;
    }
  }
</script>

<SettingsCard
  title="Purchases"
  description="The last 50 purchases. Amounts and outcomes are reported by the task."
>
  {#snippet actions()}{#if moneyEnabled}<Button
        href="{base}/money/transactions"
        variant="ghost"
        size="sm">Money transactions</Button
      >{/if}{/snippet}
  {#if purchases.length === 0}<p class="empty">No purchases yet.</p>{/if}
  <ul class="purchase-list">
    {#each purchases as purchase (purchase.id)}
      <li class="purchase-row">
        <div class="purchase-main">
          <span>{purchase.merchant_host} · {amount(purchase)}</span>
          <span class="caption"
            >{purchase.created_at} · {purchase.card_label}{purchase.approval === 'auto'
              ? ' · Auto'
              : purchase.approval === 'user'
                ? ' · Approved'
                : ''}</span
          >
          {#if purchase.room_token}<a
              href="{base}/chat/r/{encodeURIComponent(purchase.room_token)}/t/{purchase.task_id}"
              >Task {purchase.task_id}</a
            >{/if}
        </div>
        <Badge size="sm">{purchase.state[0].toUpperCase() + purchase.state.slice(1)}</Badge>
        {#if ['held', 'authorized', 'filled'].includes(purchase.state)}<Button
            variant="ghost"
            size="sm"
            disabled={!writable || busy}
            onclick={() => cancel(purchase.id)}
            ariaLabel="Cancel purchase {purchase.id}">Cancel</Button
          >{/if}
      </li>
    {/each}
  </ul>
</SettingsCard>

<style>
  .purchase-list {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .purchase-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
    padding: var(--space-2) 0;
  }
  .purchase-row + .purchase-row {
    border-top: 1px solid var(--border-subtle);
  }
  .purchase-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
