<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarNav,
    SidebarToggle,
    Button,
    CardGrid,
    EmptyState,
    LoadState,
    Card,
  } from '$lib/components/ui';
  import { getGroups, getGroup, AuthError, type GroupSummary, type GroupDetail } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import { latest } from '$lib/ui/latest';
  import GroupNotebook from '$lib/components/groups/GroupNotebook.svelte';
  import GroupMembers from '$lib/components/groups/GroupMembers.svelte';
  import GroupRooms from '$lib/components/groups/GroupRooms.svelte';
  import GroupFiles from '$lib/components/groups/GroupFiles.svelte';
  import GroupBriefings from '$lib/components/groups/GroupBriefings.svelte';
  type Selection = { group: string };
  const current = getCurrentUser();
  const user = $derived(current.user);
  let groups = $state<GroupSummary[]>([]);
  let detail = $state<GroupDetail | null>(null);
  let selected = $state<Selection | null>(null);
  let loading = $state(true);
  let detailLoading = $state(false);
  let error = $state('');
  let sidebarOpen = $state(false);
  const selectedGroup = $derived(groups.find((group) => group.group_id === selected?.group));
  let active = true;
  const details = latest();
  // A link from before the overview may still carry `view=`; it is ignored.
  const selection = createUrlSelection<Selection>({
    key: 'groups',
    params: ['group'],
    encode: (s) => ({ group: s.group }),
    decode: (p) => (p.group ? { group: p.group } : null),
    read: () => selected,
    apply: select,
  });
  selection.start();
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
    const isCurrent = details.next();
    try {
      const result = await getGroup(next.group);
      if (isCurrent()) detail = result;
    } catch (e) {
      if (isCurrent()) error = message(e);
    } finally {
      if (isCurrent()) detailLoading = false;
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
          (!resetSelection ? selection.current() : null) ?? { group: groups[0].group_id },
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
    const isCurrent = details.next();
    try {
      const updated = await getGroup(prior.group);
      const result = await getGroups();
      if (isCurrent()) {
        detail = updated;
        groups = result.groups;
      }
    } catch (e) {
      if (isCurrent()) error = message(e);
    }
  }
  onMount(() => {
    load();
    return () => {
      active = false;
      details.dispose();
    };
  });
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title={selectedGroup?.display_name ?? 'Groups'}
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="Toggle groups"
    >
      {#snippet leading()}<SidebarToggle
          label="Toggle groups"
          open={sidebarOpen}
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />{/snippet}
    </ShellHeader>
  {/snippet}
  {#snippet sidebar()}
    <Sidebar
      title="Groups"
      count={groups.length}
      open={sidebarOpen}
      onClose={() => (sidebarOpen = false)}
    >
      <SidebarNav
        items={groups.map((group) => ({
          href: `${base}/groups/?group=${encodeURIComponent(group.group_id)}`,
          label: group.display_name,
          active: selected?.group === group.group_id,
          group: group.group_id,
        }))}
        label="Groups"
        onnavigate={(item, event) => {
          if (
            !event.ctrlKey &&
            !event.metaKey &&
            !event.shiftKey &&
            !event.altKey &&
            event.button === 0
          ) {
            event.preventDefault();
            selection.push({ group: item.group });
          }
        }}
      />
    </Sidebar>
  {/snippet}
  <LoadState
    loading={loading || detailLoading}
    {error}
    loadingLabel="Loading groups…"
    onRetry={() => load()}
    empty={!groups.length}
  >
    {#snippet emptyState()}
      <EmptyState
        title="No groups"
        actionLabel={user.is_admin ? 'Manage groups' : undefined}
        actionHref={user.is_admin ? `${base}/admin/groups/` : undefined}
        >You do not belong to any groups.</EmptyState
      >
    {/snippet}
    {#if detail && selected}
      <div class="settings groups-page card-pane">
        {#key detail.group_id}
          {#if detail.system_key === 'all_users'}
            <p class="caption">
              The notebook, files and retained briefing history are shared with all current and
              future installation users, including editions published before they joined.
            </p>
          {/if}
          <CardGrid masonry>
            <div class="wide" data-width="wide"><GroupNotebook groupId={detail.group_id} /></div>
            <Card title="Members">
              {#snippet status()}<span class="caption">{detail?.members.length}</span>{/snippet}
              <GroupMembers group={detail} onChanged={changed} />
            </Card>
            <GroupRooms rooms={detail.rooms} />
            {#if detail.capabilities.read_briefings}<GroupBriefings
                groupId={detail.group_id}
                canAuthor={!!detail.capabilities.author_briefings}
              />{/if}
            {#if user.features.workspace}<GroupFiles groupId={detail.group_id} />{/if}
          </CardGrid>
        {/key}
      </div>
    {/if}
  </LoadState>
</AppShell>

<style>
  /* A column so the notebook's card takes the height masonry snaps it to. */
  .wide {
    display: flex;
    flex-direction: column;
  }
</style>
