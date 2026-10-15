<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    Button,
    EmptyState,
    PageLoading,
  } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { getGroups, getGroup, AuthError, type GroupSummary, type GroupDetail } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import GroupNotebook from '$lib/components/groups/GroupNotebook.svelte';
  import GroupMembers from '$lib/components/groups/GroupMembers.svelte';
  import GroupRooms from '$lib/components/groups/GroupRooms.svelte';
  import GroupFiles from '$lib/components/groups/GroupFiles.svelte';
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
  let active = true;
  let request = 0;
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
                selection.push({ group: group.group_id });
              }
            }}><span class="view-name">{group.display_name}</span></a
          >
        {/each}
      </div>
    </Sidebar>
  {/snippet}
  {#if loading || detailLoading}<PageLoading label="Loading groups…" />
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
        {#if detail.system_key === 'all_users'}
          <p class="caption">
            The notebook, files and retained briefing history are shared with all current and future
            installation users, including editions published before they joined.
          </p>
        {/if}
        <div class="overview card-grid">
          <div class="wide"><GroupNotebook groupId={detail.group_id} /></div>
          <SettingsCard title="Members">
            {#snippet status()}<span class="caption">{detail?.members.length}</span>{/snippet}
            <GroupMembers group={detail} onChanged={changed} />
          </SettingsCard>
          <GroupRooms rooms={detail.rooms} />
          {#if user.features.workspace}<GroupFiles groupId={detail.group_id} />{/if}
        </div>
      {/key}
    </div>
  {/if}
</AppShell>

<style>
  .settings.groups-page {
    max-width: var(--content-max);
  }
  .overview {
    --card-min: 320px;
    --card-gap: var(--space-4);
  }
  .wide {
    display: flex;
    flex-direction: column;
  }
  /* Two tracks exist from about 660px of column (two 320px minimums and the
     gap); below that a span would add an implicit column and overflow. */
  @container settings (min-width: 680px) {
    .wide {
      grid-column: span 2;
    }
  }
</style>
