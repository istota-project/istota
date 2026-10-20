<script lang="ts">
  import { setCurrentUser } from '$lib/userContext';
  import type { User } from '$lib/api';
  import Connections from './connections/+page.svelte';
  let { expireSession = () => {} }: { expireSession?: () => void } = $props();
  setCurrentUser({
    user: { username: 'alice', features: {}, nextcloud_token: null } as unknown as User,
    live: true,
    reload: async () => true,
    expireSession: () => expireSession(),
  });
</script>

<Connections />
