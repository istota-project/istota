<script lang="ts">
  import { Badge } from '@minot/ui';
  import UserAvatar from '$lib/components/UserAvatar.svelte';

  let {
    userId,
    displayName,
    isAdmin = false,
  }: {
    userId: string;
    displayName: string;
    isAdmin?: boolean;
  } = $props();
</script>

<span class="user-cell" title={userId}>
  <span class="user-face">
    <UserAvatar kind="user" {userId} label={displayName || userId} />
  </span>
  <span class="username">{displayName || userId}</span>
  {#if isAdmin}<Badge size="sm" class="admin-badge">admin</Badge>{/if}
</span>

<style>
  .user-cell {
    display: flex;
    align-items: center;
    min-width: 0;
  }
  .user-face {
    --avatar-size: 1.5rem;
    display: flex;
    flex: 0 0 auto;
    margin-right: var(--space-2);
  }
  .username {
    font-weight: var(--weight-medium);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* The bot's amber marks the role, a kind rather than a severity. */
  .user-cell :global(.admin-badge) {
    flex-shrink: 0;
    margin-left: var(--space-2);
    --badge-bg: color-mix(in srgb, var(--accent-amber) 18%, transparent);
    --badge-fg: var(--accent-amber);
  }
  @media (max-width: 640px) {
    .user-cell :global(.admin-badge) {
      display: none;
    }
  }
</style>
