<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { Icon, AppShell, ShellHeader, HeaderNav, Chip } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { Cog } from '@lucide/svelte';

  let { children } = $props();

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${base}${path}`);
  }

  function isExactActive(path: string): boolean {
    const current = page.url.pathname;
    return current === `${base}${path}` || current === `${base}${path}/`;
  }

  const navItems = $derived([
    {
      href: `${base}/health`,
      label: 'Stats',
      active: isExactActive('/health') || isActive('/health/stats'),
    },
    { href: `${base}/health/history`, label: 'History', active: isActive('/health/history') },
    {
      href: `${base}/health/immunizations`,
      label: 'Immunizations',
      active: isActive('/health/immunizations'),
    },
    { href: `${base}/health/labs`, label: 'Labs', active: isActive('/health/labs') },
    {
      href: `${base}/health/documents`,
      label: 'Documents',
      active: isActive('/health/documents'),
    },
  ]);

  const onSettings = $derived(page.url.pathname.startsWith(`${base}/health/settings`));

  function toggleSettings() {
    if (onSettings) goto(`${base}/health`);
    else goto(`${base}/health/settings`);
  }
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader title="Health">
      {#snippet nav()}
        <HeaderNav items={navItems} ariaLabel="Health section" />
      {/snippet}
      {#snippet tools()}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Health settings">
          <Icon icon={Cog} size="sm" />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  <div class="content-frame health-frame">
    {@render children()}
  </div>
</AppShell>

<style>
  /* The column geometry — the cap, the centring and the growing flex column —
	   comes from `.content-frame` (app.css), which health shares with money's
	   portfolio. What is left here is health's own inset and the module shell
	   below it. The class stays on a real element in this file rather than
	   moving into a component, because the `:global()` rules under it are
	   pruned the moment Svelte can no longer see their subject in the markup. */
  .health-frame {
    padding: var(--space-4);
  }

  /* The .msg boxes this replaced each carried their own margin-bottom —
     0.75rem on six pages, 0.5rem on one. Restored once for the module rather
     than as twelve page rules re-forking the primitive; the majority value
     wins, so the labs toolbar's notice gains 0.25rem. */
  .health-frame :global(.banner) {
    margin-bottom: var(--space-3);
  }

  @media (max-width: 768px) {
    .health-frame {
      /* Match ShellHeader's mobile padding so the page heading lines
			   up with the subnav title above it. */
      padding: var(--space-2) var(--space-3);
    }
  }
</style>
