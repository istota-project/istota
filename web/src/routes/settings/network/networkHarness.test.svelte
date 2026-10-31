<script lang="ts">
  import { setCurrentUser } from '$lib/userContext';
  import type { User } from '$lib/api';
  import Network from './+page.svelte';
  let { expireSession = () => {} }: { expireSession?: () => void } = $props();
  setCurrentUser({
    user: { username: 'alice', features: {} } as unknown as User,
    live: true,
    reload: async () => true,
    expireSession: () => expireSession(),
  });
</script>

<Network />
