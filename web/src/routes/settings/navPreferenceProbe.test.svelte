<script lang="ts">
  import { getCurrentUser } from '$lib/userContext';
  import Preferences from './preferences/+page.svelte';

  const identity = getCurrentUser();
</script>

<output aria-label="Confirmed navigation preference"
  >{String(identity.user?.show_nav_unread_badges ?? false)}</output
>
<Preferences />
