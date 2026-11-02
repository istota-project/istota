<script lang="ts">
  import { setCurrentUser } from '$lib/userContext';
  import type { User } from '$lib/api';
  import Jobs from './+page.svelte';
  let {
    expireSession = () => {},
    isAdmin = false,
  }: { expireSession?: () => void; isAdmin?: boolean } = $props();
  // svelte-ignore state_referenced_locally
  const username = { username: 'alice', features: {}, is_admin: isAdmin };
  setCurrentUser({
    user: username as unknown as User,
    live: true,
    reload: async () => true,
    expireSession: () => expireSession(),
  });
</script>

<Jobs />
