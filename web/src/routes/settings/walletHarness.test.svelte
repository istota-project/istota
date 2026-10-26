<script lang="ts">
  import { setCurrentUser } from '$lib/userContext';
  import type { User } from '$lib/api';
  import Layout from './+layout.svelte';
  import Wallet from './wallet/+page.svelte';
  setCurrentUser({
    user: { username: 'alice', features: { money: true } } as User,
    live: true,
    reload: async () => true,
    expireSession: () => {},
  });
</script>

<Layout><Wallet /></Layout>
