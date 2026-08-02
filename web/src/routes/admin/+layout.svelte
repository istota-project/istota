<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { AppShell, ShellHeader, Sidebar, SidebarToggle } from '$lib/components/ui';
  import { Activity, ScrollText, SlidersHorizontal } from 'lucide-svelte';

  let { children } = $props();

  // The admin pane's sections. Logs is the payload that justified the frame
  // (ISSUE-203); Configuration is read-only for now and shaped so it can become
  // the config editor without the nav changing.
  const SECTIONS = [
    { href: '', label: 'Status', icon: Activity },
    { href: '/config', label: 'Configuration', icon: SlidersHorizontal },
    { href: '/logs', label: 'Logs', icon: ScrollText },
  ];

  let sidebarOpen = $state(false);

  // `/admin` is a prefix of every sub-route, so the root entry matches exactly
  // rather than by prefix — otherwise Status stays lit on every page.
  function isActive(href: string): boolean {
    const path = page.url.pathname.replace(/\/$/, '');
    const target = `${base}/admin${href}`.replace(/\/$/, '');
    return path === target;
  }

  let activeLabel = $derived(SECTIONS.find((s) => isActive(s.href))?.label ?? 'Admin');
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title={activeLabel}
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open admin sections"
    >
      {#snippet leading()}
        <SidebarToggle
          open={sidebarOpen}
          label="Admin"
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    <Sidebar title="Admin" open={sidebarOpen} onClose={() => (sidebarOpen = false)}>
      <div class="views">
        {#each SECTIONS as section (section.href)}
          {@const Icon = section.icon}
          <a
            class="view-btn"
            class:active={isActive(section.href)}
            href="{base}/admin{section.href}"
            aria-current={isActive(section.href) ? 'page' : undefined}
            onclick={() => (sidebarOpen = false)}
          >
            <Icon size={14} />
            <span class="view-name">{section.label}</span>
          </a>
        {/each}
      </div>
    </Sidebar>
  {/snippet}

  {@render children()}
</AppShell>

<style>
  /* The admin column, defined once for the three pages instead of three times.
     Each of them wears the settings shell — `class="settings admin-page"` —
     because they are built out of its .card / .section-header / .grid
     primitives, and each then widened its 980px form column by hand, all three
     landing on the same 1100px with nothing saying they had to agree.

     It reads --content-max, so "how wide may a dashboard column get" has one
     answer here and on health, money's portfolio and money's reports rather
     than two. Admin is the last module that was answering it privately.

     :global because the subject is on the page's own element, one level down,
     and Svelte prunes a selector whose subject it cannot see in this file. */
  :global(.settings.admin-page),
  :global(.settings.config-page),
  :global(.settings.logs-page) {
    max-width: var(--content-max);
  }
</style>
