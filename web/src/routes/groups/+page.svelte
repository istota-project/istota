<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    HeaderNav,
    Button,
    EmptyState,
  } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { getGroups, getGroup, AuthError, type GroupSummary, type GroupDetail } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import GroupNotebook from '$lib/components/groups/GroupNotebook.svelte';
  import GroupMembers from '$lib/components/groups/GroupMembers.svelte';
  type View = 'memory' | 'members' | 'rooms';
  type Selection = { group: string; view: View };
  const current = getCurrentUser();
  const user = $derived(current.user);
  let groups = $state<GroupSummary[]>([]);
  let detail = $state<GroupDetail | null>(null);
  let selected = $state<Selection | null>(null);
  let loading = $state(true);
  let detailLoading = $state(false);
  let error = $state('');
  let sidebarOpen = $state(false);
  let active = true;
  let request = 0;
  const selection = createUrlSelection<Selection>({
    key: 'groups',
    params: ['group', 'view'],
    encode: (s) => ({ group: s.group, view: s.view }),
    decode: (p) =>
      p.group
        ? { group: p.group, view: p.view === 'members' || p.view === 'rooms' ? p.view : 'memory' }
        : null,
    read: () => selected,
    apply: select,
  });
  selection.start();
  const navItems = $derived(
    selected
      ? [
          ...(['memory', 'members', 'rooms'] as View[]).map((view) => ({
            href: `${base}/groups/?${new URLSearchParams({ group: selected!.group, view })}`,
            label: view[0].toUpperCase() + view.slice(1),
            active: selected!.view === view,
          })),
          {
            href: `${base}/files?${new URLSearchParams({ place: `group:${selected.group}` })}`,
            label: 'Files',
            active: false,
          },
        ]
      : [],
  );
  function message(e: unknown) {
    if (e instanceof AuthError) current.expireSession();
    return e instanceof Error ? e.message : 'Could not load groups.';
  }
  async function select(next: Selection) {
    sidebarOpen = false;
    if (selected?.group === next.group && detail) {
      selected = next;
      return;
    }
    selected = next;
    detail = null;
    error = '';
    detailLoading = true;
    const id = ++request;
    try {
      const result = await getGroup(next.group);
      if (active && id === request) detail = result;
    } catch (e) {
      if (active && id === request) error = message(e);
    } finally {
      if (active && id === request) detailLoading = false;
    }
  }
  async function load(resetSelection = false) {
    loading = true;
    error = '';
    try {
      const result = await getGroups();
      if (!active) return;
      groups = result.groups;
      if (groups.length)
        await select(
          (!resetSelection ? selection.current() : null) ?? {
            group: groups[0].group_id,
            view: 'memory',
          },
        );
      else {
        selected = null;
        detail = null;
      }
    } catch (e) {
      if (active) error = message(e);
    } finally {
      if (active) loading = false;
    }
  }
  async function changed(left = false) {
    if (left) {
      detail = null;
      selected = null;
      await current.reload();
      await load(true);
      return;
    }
    const prior = selected;
    if (!prior) return;
    const id = ++request;
    try {
      const updated = await getGroup(prior.group);
      const result = await getGroups();
      if (active && id === request) {
        detail = updated;
        groups = result.groups;
      }
    } catch (e) {
      if (active && id === request) error = message(e);
    }
  }
  onMount(() => {
    load();
    return () => {
      active = false;
      ++request;
    };
  });
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title="Groups"
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="Toggle groups"
    >
      {#snippet leading()}<SidebarToggle
          label="Toggle groups"
          open={sidebarOpen}
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />{/snippet}
      {#snippet nav()}<HeaderNav items={navItems} ariaLabel="Group view" />{/snippet}
    </ShellHeader>
  {/snippet}
  {#snippet sidebar()}
    <Sidebar
      title="Groups"
      count={groups.length}
      open={sidebarOpen}
      onClose={() => (sidebarOpen = false)}
    >
      <div class="views">
        {#each groups as group (group.group_id)}
          <a
            class="view-btn"
            class:active={selected?.group === group.group_id}
            href="{base}/groups/?group={encodeURIComponent(group.group_id)}"
            onclick={(event) => {
              if (
                !event.ctrlKey &&
                !event.metaKey &&
                !event.shiftKey &&
                !event.altKey &&
                event.button === 0
              ) {
                event.preventDefault();
                selection.push({ group: group.group_id, view: 'memory' });
              }
            }}><span class="view-name">{group.display_name}</span></a
          >
        {/each}
      </div>
    </Sidebar>
  {/snippet}
  {#if loading || detailLoading}<p class="center-msg">Loading groups…</p>
  {:else if error}<div class="center-msg error">
      <p>{error}</p>
      <Button onclick={() => load()}>Retry</Button>
    </div>
  {:else if !groups.length}<EmptyState
      title="No groups"
      actionLabel={user.is_admin ? 'Manage groups' : undefined}
      actionHref={user.is_admin ? `${base}/admin/groups/` : undefined}
      >You do not belong to any groups.</EmptyState
    >
  {:else if detail && selected}
    <div class="settings groups-page">
      {#key detail.group_id}
        {#if selected.view === 'memory'}<GroupNotebook groupId={detail.group_id} />
        {:else if selected.view === 'members'}
          <SettingsCard title="Members">
            <GroupMembers group={detail} onChanged={changed} />
          </SettingsCard>
        {:else}
          <SettingsCard
            title="Linked rooms"
            description="Chat rooms linked to this group that you can open."
          >
            {#if detail.rooms.length}<ul class="rooms">
                {#each detail.rooms as room (room.token)}<li>
                    <a href="{base}/chat?room={encodeURIComponent(room.token)}">{room.name}</a>
                  </li>{/each}
              </ul>
            {:else}<EmptyState size="sm">No linked rooms you can access.</EmptyState>{/if}
          </SettingsCard>
        {/if}
      {/key}
    </div>
  {/if}
</AppShell>

<style>
  .rooms {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .rooms li {
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
  .rooms li:last-child {
    border-bottom: none;
  }
</style>
