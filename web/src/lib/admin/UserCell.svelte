<script lang="ts">
  import { Avatar } from '$lib/components/ui';

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
    <Avatar kind="user" {userId} label={displayName || userId} />
  </span>
  <span class="username">{displayName || userId}</span>
  {#if isAdmin}<span class="admin-badge">admin</span>{/if}
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
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .admin-badge {
    display: inline-block;
    flex-shrink: 0;
    margin-left: var(--space-2);
    /* design-lint-allow: Preserve the compact badge used by the Status users table. */
    padding: 0.05rem var(--space-2);
    font-size: var(--text-2xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-radius: var(--radius-pill);
    background: color-mix(in srgb, var(--accent-amber) 18%, transparent);
    color: var(--accent-amber);
  }
  @media (max-width: 640px) {
    .admin-badge {
      display: none;
    }
  }
</style>
