<script lang="ts">
  import { onMount } from 'svelte';
  import { AuthError, getWallet, type WalletSettings } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  import {
    SettingsLayout,
    WalletCardsCard,
    WalletPolicyCard,
    WalletPurchasesCard,
  } from '$lib/components/settings';
  const identity = getCurrentUser();
  let data: WalletSettings | null = $state(null);
  let error = $state('');
  let loading = $state(true);
  async function refresh() {
    try {
      data = await getWallet();
      error = '';
    } catch (e) {
      report(e);
    } finally {
      loading = false;
    }
  }
  function report(e: unknown) {
    if (e instanceof AuthError) identity.expireSession();
    else error = (e as Error).message || 'The wallet could not be loaded.';
  }
  onMount(refresh);
</script>

<SettingsLayout description="Cards, spending limits and recent purchases." {loading} {error}>
  {#if data}
    {@const writable = !data.refusal}
    {#if data.refusal}<p class="banner info">{data.refusal}</p>{/if}
    <WalletCardsCard cards={data.cards} {writable} onSaved={refresh} onError={report} />
    <WalletPolicyCard
      policy={data.policy}
      precision={data.currency_precision}
      {writable}
      onError={report}
    />
    <WalletPurchasesCard
      purchases={data.purchases}
      precision={data.currency_precision}
      {writable}
      moneyEnabled={identity.user.features.money}
      onSaved={refresh}
      onError={report}
    />
  {/if}
</SettingsLayout>
