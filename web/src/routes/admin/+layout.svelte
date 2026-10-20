<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { AppShell, ShellHeader, Sidebar, SidebarNav, SidebarToggle } from '$lib/components/ui';
  import { createDrawerClose } from '$lib/ui/drawerClose';
  import { HeaderSave } from '$lib/components/settings';
  import {
    Activity,
    CalendarClock,
    Monitor,
    Newspaper,
    Plug,
    ScrollText,
    SlidersHorizontal,
    Stethoscope,
    Users,
    UsersRound,
  } from '@lucide/svelte';

  let { children } = $props();

  // The admin pane's sections. Logs is the payload that justified the frame
  // (ISSUE-203); Configuration is read-only for now and shaped so it can become
  // the config editor without the nav changing. Health sits next to it because
  // it answers the neighbouring question: Configuration says what the process
  // loaded, Health says whether the machine actually has what that describes.
  const SECTIONS = [
    { href: '', label: 'Status', icon: Activity },
    { href: '/users', label: 'Users', icon: Users },
    { href: '/groups', label: 'Groups', icon: UsersRound },
    { href: '/briefings', label: 'Briefings', icon: Newspaper },
    { href: '/jobs', label: 'Scheduled jobs', icon: CalendarClock },
    { href: '/health', label: 'Health', icon: Stethoscope },
    { href: '/config', label: 'Configuration', icon: SlidersHorizontal },
    { href: '/connections', label: 'Connections', icon: Plug },
    { href: '/browsers', label: 'Browsers', icon: Monitor },
    { href: '/logs', label: 'Logs', icon: ScrollText },
  ];

  let sidebarOpen = $state(false);
  const drawer = createDrawerClose(
    () => sidebarOpen,
    () => (sidebarOpen = false),
  );
  onDestroy(drawer.cancel);

  // `/admin` is a prefix of every sub-route, so the root entry matches exactly
  // rather than by prefix — otherwise Status stays lit on every page.
  function isActive(href: string): boolean {
    const path = page.url.pathname.replace(/\/$/, '');
    const target = `${base}/admin${href}`.replace(/\/$/, '');
    return path === target;
  }

  let navItems = $derived(
    SECTIONS.map((s) => ({
      href: `${base}/admin${s.href}`,
      label: s.label,
      icon: s.icon,
      active: isActive(s.href),
    })),
  );

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
      {#snippet tools()}
        <HeaderSave />
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    <Sidebar title="Admin" open={sidebarOpen} onClose={() => (sidebarOpen = false)}>
      <SidebarNav
        items={navItems}
        label="Admin sections"
        onnavigate={(item, event) => drawer.link(event, () => void goto(item.href))}
      />
    </Sidebar>
  {/snippet}

  {@render children()}
</AppShell>
