<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    disconnectNextcloudToken,
    getNextcloudSharing,
    saveNextcloudSharing,
    type NextcloudCollection,
    type NextcloudKindState,
    type NextcloudShareKind,
    type NextcloudShareLevel,
    type NextcloudSharing,
    type NextcloudTokenStatus,
  } from '$lib/api';
  import {
    Badge,
    Button,
    ConfirmDialog,
    Select,
    type SelectOption,
    Card,
    Field,
  } from '$lib/components/ui';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { offSiteRoutesOpenInBrowser } from '$lib/platform/nativeConnection';

  interface Props {
    token: NextcloudTokenStatus;
    /** The signed-in user, to tell the bot's own account apart. */
    username?: string;
    /** Called once the token is gone, so the page can show Not connected. */
    onDisconnected?: () => void;
  }

  let { token, username = '', onDisconnected }: Props = $props();

  const LEVEL_LABELS: Record<NextcloudShareLevel, string> = {
    off: 'No access',
    readonly: 'Read-only',
    full: 'Read and write',
  };
  const RANK: Record<NextcloudShareLevel, number> = { off: 0, readonly: 1, full: 2 };
  const LEVELS: NextcloudShareLevel[] = ['off', 'readonly', 'full'];

  const GROUPS: { kind: NextcloudShareKind; title: string; empty: string }[] = [
    { kind: 'calendars', title: 'Calendars', empty: 'You have no calendars in Nextcloud.' },
    { kind: 'contacts', title: 'Address books', empty: 'You have no address books in Nextcloud.' },
  ];

  let loading = $state(false);
  let loadError = $state('');
  let sharing: NextcloudSharing | null = $state(null);
  /** The picker's working copy, by collection id. */
  let pending: Record<string, NextcloudShareLevel> = $state({});
  /** Per-row reasons from the last save. */
  let failed: Record<string, string> = $state({});
  let saving = $state(false);
  let busy = $state(false);
  let error = $state('');
  let info = $state('');
  let confirmingDisconnect = $state(false);
  let disconnectError = $state('');

  function adopt(next: NextcloudSharing) {
    sharing = next;
    pending = Object.fromEntries(next.collections.map((c) => [c.id, c.level]));
  }

  async function load() {
    loading = true;
    loadError = '';
    try {
      adopt(await getNextcloudSharing());
    } catch (e) {
      sharing = null;
      loadError =
        e instanceof Error ? e.message : 'Could not load your calendars and address books';
    } finally {
      loading = false;
    }
  }

  // `$derived.by`: a function body is what makes TS read `sharing`'s declared
  // type rather than the `null` it is narrowed to here.
  const isBotAccount = $derived.by(() => {
    const bot = sharing?.bot ?? '';
    return !!bot && !!username && bot.toLowerCase() === username.toLowerCase();
  });

  /** Whether the card may offer raising access in this kind at all. */
  function offered(state: NextcloudKindState): boolean {
    return state.available && state.ceiling !== 'off';
  }

  /**
   * Levels up to the ceiling. A share made in Nextcloud above it, or in a kind
   * the card does not offer, is added as a disabled option so the select shows
   * the real level rather than its placeholder. Lowering is always allowed.
   */
  function levelOptions(state: NextcloudKindState, current: NextcloudShareLevel): SelectOption[] {
    const ceiling = offered(state) ? state.ceiling : 'off';
    const options: SelectOption[] = LEVELS.filter((l) => RANK[l] <= RANK[ceiling]).map((l) => ({
      value: l,
      label: LEVEL_LABELS[l],
    }));
    if (RANK[current] > RANK[ceiling]) {
      options.push({ value: current, label: LEVEL_LABELS[current], disabled: true });
    }
    return options;
  }

  function aboveLimit(state: NextcloudKindState, c: NextcloudCollection): boolean {
    return offered(state) && RANK[c.level] > RANK[state.ceiling];
  }

  /** Rows per kind: every collection when offered, else only those still shared. */
  function rowsFor(kind: NextcloudShareKind): NextcloudCollection[] {
    if (!sharing) return [];
    const all = sharing.collections.filter((c) => c.kind === kind);
    return offered(sharing.kinds[kind]) ? all : all.filter((c) => c.level !== 'off');
  }

  function groupNote(state: NextcloudKindState): string | undefined {
    if (!state.available) return 'Istota does not use these on this instance.';
    if (state.ceiling === 'off') return 'This instance does not allow sharing these.';
    return undefined;
  }

  const visibleGroups = $derived.by(() => {
    if (!sharing || isBotAccount) return [];
    const loaded = sharing;
    return GROUPS.filter((g) => offered(loaded.kinds[g.kind]) || rowsFor(g.kind).length > 0);
  });

  const anyShared = $derived.by(() => (sharing?.collections ?? []).some((c) => c.level !== 'off'));

  const changes = $derived.by(() => {
    const out: Record<string, NextcloudShareLevel> = {};
    for (const c of sharing?.collections ?? []) {
      const want = pending[c.id] ?? c.level;
      if (want !== c.level) out[c.id] = want;
    }
    return out;
  });

  const dirty = $derived(Object.keys(changes).length > 0);

  function setLevel(id: string, level: string) {
    pending = { ...pending, [id]: level as NextcloudShareLevel };
  }

  async function save() {
    if (!dirty) return;
    saving = true;
    error = '';
    info = '';
    try {
      const result = await saveNextcloudSharing(changes);
      adopt(result);
      failed = Object.fromEntries(result.failed.map((f) => [f.id, f.reason]));
      if (result.failed.length === 0) info = 'Saved.';
      else error = 'Some changes were not applied.';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Save failed';
    } finally {
      saving = false;
    }
  }

  useSettingsSave(() => (token.connected && sharing ? { dirty, saving, save } : null));

  // A full page navigation: `/reconnect` is a server auth route that answers
  // with a redirect to Nextcloud, so the client router has nothing to resolve.
  // In the iOS app it opens in the browser instead (`offSiteRoutesOpenInBrowser`).
  const reconnectUrl = `${base}/reconnect`;
  const inBrowser = offSiteRoutesOpenInBrowser();
  const reconnectLink = inBrowser
    ? { href: reconnectUrl, target: '_blank', rel: 'noopener noreferrer' }
    : { onclick: reconnect };

  function reconnect() {
    window.location.href = reconnectUrl;
  }

  /**
   * Straight through only when the listing loaded and nothing is shared. A
   * listing that failed cannot say, so the choice is the user's.
   */
  function startDisconnect() {
    disconnectError = '';
    if (sharing && !anyShared) void disconnect(false);
    else confirmingDisconnect = true;
  }

  /** False on a refusal, which the dialog shows and stays open for. */
  async function disconnect(unshare: boolean): Promise<boolean> {
    busy = true;
    error = '';
    info = '';
    disconnectError = '';
    try {
      await disconnectNextcloudToken(unshare);
      confirmingDisconnect = false;
      sharing = null;
      pending = {};
      failed = {};
      info = 'Nextcloud connection removed.';
      onDisconnected?.();
      return true;
    } catch (e) {
      const body = (e as { body?: { still_shared?: unknown } | null }).body;
      const still = Array.isArray(body?.still_shared) ? (body.still_shared as string[]) : null;
      if (still) {
        disconnectError =
          `Could not stop sharing: ${still.join(', ')}. ` +
          'Your Nextcloud connection was kept so you can try again.';
        // Removals that succeeded stay removed; show where things now stand.
        void load();
      } else {
        disconnectError = e instanceof Error ? e.message : 'Disconnect failed';
      }
      return false;
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    if (token.connected) void load();
  });
</script>

{#snippet browserNote()}
  <p class="caption note">Opens in the browser. You may need to sign in to Istota there first.</p>
{/snippet}

{#snippet statusPill()}
  <Badge tone="subtle" uppercase={false} variant={token.connected ? 'success' : 'neutral'}>
    {token.connected ? 'Connected' : 'Not connected'}
  </Badge>
{/snippet}

<Card
  title="Nextcloud"
  description="When connected, messages you send from web chat appear in Nextcloud Talk under your own name, and read state syncs between web and Talk."
  status={statusPill}
>
  {#if token.connected}
    {#if loading && !sharing}
      <p class="caption">Loading…</p>
    {:else if loadError}
      <div class="banner error">{loadError}</div>
    {:else if isBotAccount}
      <p class="caption note">
        This is Istota's own Nextcloud account, so there is nothing to share with it.
      </p>
    {:else if sharing}
      {@const loaded = sharing}
      {#each visibleGroups as group (group.kind)}
        {@const state = loaded.kinds[group.kind]}
        {@const rows = rowsFor(group.kind)}
        {@const note = groupNote(state)}
        <div
          class="group vstack"
          style:--vstack-gap="var(--space-2)"
          role="group"
          aria-labelledby="nc-share-{group.kind}"
        >
          <h3 id="nc-share-{group.kind}" class="micro-label">{group.title}</h3>
          {#if note}<p class="caption note">{note}</p>{/if}
          {#if rows.length === 0}
            <p class="caption note">{group.empty}</p>
          {/if}
          {#each rows as c (c.id)}
            <Field
              label={c.name}
              warning={aboveLimit(state, c) ? "Above this instance's limit." : undefined}
              error={failed[c.id]}
            >
              <Select
                value={pending[c.id] ?? c.level}
                options={levelOptions(state, c.level)}
                disabled={saving || busy}
                ariaLabel="{c.name} access level"
                onValueChange={(v) => setLevel(c.id, v)}
                fullWidth
              />
            </Field>
          {/each}
        </div>
      {/each}
      {#if visibleGroups.length > 0}
        <p class="caption note">
          Istota uses these through its own Nextcloud account ({loaded.bot}). You can also change
          sharing in the Nextcloud Calendar and Contacts apps.
        </p>
      {/if}
    {/if}

    <div class="card-actions">
      <Button variant="secondary" size="sm" {...reconnectLink}>Reconnect</Button>
      <Button
        variant="secondary"
        size="sm"
        onclick={startDisconnect}
        loading={busy}
        loadingLabel="Disconnecting…"
      >
        Disconnect
      </Button>
    </div>
    {#if inBrowser}{@render browserNote()}{/if}
    {#if disconnectError && !confirmingDisconnect}
      <div class="banner error">{disconnectError}</div>
    {/if}
  {:else}
    <div class="card-actions">
      <Button variant="primary" size="sm" {...reconnectLink}>Connect</Button>
    </div>
    {#if inBrowser}{@render browserNote()}{/if}
    <p class="caption note">
      Connecting signs you in to Nextcloud again and brings you back here. Your session stays as it
      is. Calendars and address books you shared with Istota stay shared until you remove them in
      Nextcloud or connect again.
    </p>
  {/if}

  {#if error}
    <div class="banner error">{error}</div>
  {/if}
  {#if info}
    <div class="banner success">{info}</div>
  {/if}
</Card>

<ConfirmDialog
  bind:open={confirmingDisconnect}
  title="Disconnect Nextcloud"
  width="420px"
  error={disconnectError}
  secondaryLabel="Disconnect, keep sharing"
  secondaryBusyLabel="Disconnecting…"
  onSecondary={() => disconnect(false)}
  confirmLabel="Disconnect and stop sharing"
  busyLabel="Disconnecting…"
  confirmVariant="primary"
  onConfirm={() => disconnect(true)}
>
  {#snippet body()}
    <p class="modal-text">
      {#if sharing}
        Some of your calendars or address books are shared with Istota.
      {:else}
        Istota could not check which of your calendars and address books are shared with it.
      {/if}
      Stop sharing them as well?
    </p>
    <p class="modal-text">
      If you keep sharing, Istota keeps access to them. You can unshare them later in the Nextcloud
      Calendar and Contacts apps.
    </p>
  {/snippet}
</ConfirmDialog>

<style>
  .group h3 {
    margin: 0;
  }
  .note,
  .modal-text {
    margin: 0;
  }
  .modal-text + .modal-text {
    margin-top: var(--space-2);
  }
  .card-actions {
    display: flex;
    gap: var(--space-2);
  }
</style>
