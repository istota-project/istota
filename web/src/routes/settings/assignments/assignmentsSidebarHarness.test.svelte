<script lang="ts">
  import { setCurrentUser } from '$lib/userContext';
  import type { User } from '$lib/api';
  import Layout from '../+layout.svelte';
  let { features }: { features: Record<string, boolean> } = $props();
  // svelte-ignore state_referenced_locally
  const user = { username: 'alice', features } as unknown as User;
  setCurrentUser({ user, live: true, reload: async () => true, expireSession: () => {} });
</script>

<Layout><p>section</p></Layout>
