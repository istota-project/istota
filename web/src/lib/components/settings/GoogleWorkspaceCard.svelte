<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getGoogleStatus,
    saveGoogleScopes,
    disconnectGoogle,
    type GoogleStatus,
    type GoogleScopeLevel,
  } from '$lib/api';
  import { Button, ConfirmDialog, Select, type SelectOption } from '$lib/components/ui';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import SettingsCard from './SettingsCard.svelte';
  import SettingsField from './SettingsField.svelte';

  interface Props {
    /** Called after a change the services list should re-read. */
    onChanged?: () => void;
  }

  let { onChanged }: Props = $props();

  let loading = $state(true);
  let saving = $state(false);
  let busy = $state(false);
  let error = $state('');
  let info = $state('');
  let confirmingDisconnect = $state(false);

  let status: GoogleStatus | null = $state(null);
  /** The picker's working copy. Empty until the status lands. */
  let pending: Record<string, GoogleScopeLevel> = $state({});

  const LEVEL_LABELS: Record<GoogleScopeLevel, string> = {
    off: 'No access',
    readonly: 'Read-only',
    full: 'Read and write',
  };

  async function refresh() {
    loading = true;
    error = '';
    try {
      status = await getGoogleStatus();
      pending = { ...status.selection };
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load Google status';
    } finally {
      loading = false;
    }
  }

  function resetBanners() {
    error = '';
    info = '';
  }

  /**
   * A service the instance does not offer is fixed at "No access" and says so.
   * The level list is truncated at the ceiling for the same reason: a scope the
   * operator's Google Cloud project has not enabled fails at Google's end with
   * an error the user can do nothing about, so offering it would be a lie.
   */
  function levelOptions(maxLevel: GoogleScopeLevel): SelectOption[] {
    const levels: GoogleScopeLevel[] =
      maxLevel === 'full'
        ? ['off', 'readonly', 'full']
        : maxLevel === 'readonly'
          ? ['off', 'readonly']
          : ['off'];
    return levels.map((l) => ({ value: l, label: LEVEL_LABELS[l] }));
  }

  let dirty = $derived.by(() => {
    if (!status) return false;
    const current = status.selection;
    const keys = new Set([...Object.keys(current), ...Object.keys(pending)]);
    for (const k of keys) {
      if ((current[k] ?? 'off') !== (pending[k] ?? 'off')) return true;
    }
    return false;
  });

  // `$derived.by` rather than `$derived`: at this point in the module body TS
  // has `status` narrowed to its `null` initializer, and a function body is
  // what makes it read the declared type instead.
  let grantedByService = $derived.by(
    () => new Map((status?.granted ?? []).map((g) => [g.service, g])),
  );

  // Connecting with an empty request sends the user to a consent screen that
  // grants nothing; the server declines it, so the button should too. Derived
  // from the *saved* request, since that is what a connect would carry — an
  // unsaved pick has not reached the server.
  let nothingRequested = $derived.by(
    () => !!status?.enabled && status.requested_scopes.length === 0,
  );

  function setLevel(service: string, level: string) {
    pending = { ...pending, [service]: level as GoogleScopeLevel };
  }

  async function save() {
    if (!status) return;
    saving = true;
    resetBanners();
    try {
      const resp = await saveGoogleScopes(pending);
      await refresh();
      info = resp.reconnect_required
        ? 'Saved. Reconnect your Google account to grant the new access.'
        : 'Saved.';
      onChanged?.();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Save failed';
    } finally {
      saving = false;
    }
  }

  // The card contributes to the page's single app-bar Save like every other
  // settings card. It withdraws when the instance has Google switched off —
  // there is nothing to write then.
  useSettingsSave(() => (status?.enabled ? { dirty, saving, save } : null));

  function connect() {
    busy = true;
    // Full-page nav — the OAuth callback redirects back into the app.
    window.location.href = `${base}/google/connect`;
  }

  async function doDisconnect() {
    confirmingDisconnect = false;
    busy = true;
    resetBanners();
    try {
      await disconnectGoogle();
      await refresh();
      info = 'Disconnected from Google.';
      onChanged?.();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Disconnect failed';
    } finally {
      busy = false;
    }
  }

  onMount(refresh);
</script>

<!--
  The connection state is a pill beside the card title, like every other
  service card's. It was a Badge in the body, which put the loudest thing on
  the card two lines below the heading it qualifies. Withheld until the status
  lands, and on an instance with Google switched off: there is no connection to
  report in either case.

  Declared out here and passed by name rather than written inside the card's
  tags, which is the shorter form: that spelling takes the snippet's own name
  as the prop, and `status` is already this component's loaded payload.
-->
{#snippet statusPill()}
  {#if status?.enabled}
    <span class="status-pill status-{status.connected ? 'configured' : 'missing'}">
      {status.connected ? 'Connected' : 'Not connected'}
    </span>
    {#if status.connected && status.missing_scopes.length > 0}
      <span class="status-pill status-partial">Reconnect needed</span>
    {/if}
  {/if}
{/snippet}

<SettingsCard
  title="Google Workspace"
  description="Choose which Google services the bot may use, and at what level. A change takes effect the next time you connect: Google has to ask you again."
  status={statusPill}
>
  {#if loading}
    <p class="muted">Loading…</p>
  {:else if !status}
    <p class="empty">Google status is unavailable.</p>
  {:else if !status.enabled}
    <p class="empty">Google Workspace OAuth is not configured on this Istota instance.</p>
  {:else}
    {#if status.connected}
      {#if status.missing_scopes.length > 0}
        <div class="banner warn">
          Your grant is narrower than what this instance now asks for. Reconnect to apply the
          current selection; until then some Google actions will fail.
        </div>
      {:else if status.extra_scopes.length > 0}
        <div class="banner info">
          Your grant is wider than the current selection. Reconnect to narrow it, or remove access
          from your Google account settings.
        </div>
      {/if}
    {/if}

    <!-- One row per service, and the row is the whole account of that service:
         what to ask for next time, and what Google currently holds. The two
         used to be separate sections, which said each level twice and left the
         reader matching names across them. -->
    {#each status.offered as svc (svc.service)}
      {@const held = grantedByService.get(svc.service)}
      <SettingsField
        label={svc.label}
        labelled={false}
        warning={svc.max_level === 'off' ? 'This instance does not offer this service.' : undefined}
      >
        <div class="level-row">
          <Select
            value={pending[svc.service] ?? 'off'}
            options={levelOptions(svc.max_level)}
            disabled={svc.max_level === 'off' || saving || busy}
            ariaLabel="{svc.label} access level"
            onValueChange={(v) => setLevel(svc.service, v)}
            fullWidth
          />
          {#if held}
            <!-- Flat text rather than nested spans: the partial note qualifies
                 the level it follows, and splitting them reads as two facts. -->
            <span class="caption"
              >granted: {LEVEL_LABELS[held.level]}{#if !held.complete}
                — partial, some boxes were deselected at consent{/if}</span
            >
          {/if}
        </div>
        {#if held && held.also.length > 0}
          <!-- A scope of the same service below the level reported: in the map,
               so never "unrecognised", and not in the reported level's set, so
               it would otherwise show nowhere. -->
          <p class="caption also">
            also granted:
            {#each held.also as s, i (s)}{#if i > 0},
              {/if}<code>{s}</code>{/each}
          </p>
        {/if}
      </SettingsField>
    {/each}

    {#if status.connected && status.unrecognized_scopes.length > 0}
      <!-- Shown rather than dropped: the service map lags a hand-edited
           config, and hiding a granted scope is the thing this display
           exists to stop. -->
      <p class="caption footnote">
        Also granted, not recognised as a service:
        {#each status.unrecognized_scopes as s, i (s)}{#if i > 0},
          {/if}<code>{s}</code>{/each}
      </p>
    {/if}
    {#if status.unoffered_scopes.length > 0}
      <!-- Ceiling scopes with no service row. They are requested regardless —
           no picker row can turn one off — so naming them is the difference
           between an informed consent and a surprise on Google's screen. -->
      <p class="caption footnote">
        This instance also always requests, with no per-service control:
        {#each status.unoffered_scopes as s, i (s)}{#if i > 0},
          {/if}<code>{s}</code>{/each}
      </p>
    {/if}

    <div class="actions">
      {#if status.connected}
        <Button variant="secondary" onclick={connect} disabled={busy || nothingRequested}>
          Reconnect
        </Button>
        <Button variant="ghost" onclick={() => (confirmingDisconnect = true)} disabled={busy}>
          Disconnect
        </Button>
      {:else}
        <Button variant="primary" onclick={connect} disabled={busy || nothingRequested}>
          {busy ? 'Connecting…' : 'Connect'}
        </Button>
      {/if}
    </div>
    {#if nothingRequested}
      <!-- The server refuses this connect too, but it refuses it after a
           full-page round trip; saying so here is the difference between a
           disabled button and a page that appears to have done nothing. -->
      <p class="caption">Choose at least one service before connecting.</p>
    {/if}
  {/if}

  {#if error}
    <div class="banner error">{error}</div>
  {/if}
  {#if info}
    <div class="banner success">{info}</div>
  {/if}
</SettingsCard>

<ConfirmDialog
  open={confirmingDisconnect}
  title="Disconnect Google"
  message={'Are you sure you want to disconnect your Google account? The stored tokens are ' +
    'deleted and the bot loses access immediately. You can connect again at any time.'}
  confirmLabel="Disconnect"
  onConfirm={doDisconnect}
  onCancel={() => (confirmingDisconnect = false)}
/>

<style>
  /* .caption is typography only by design, so the spacing stays here — and
     here it is nothing: the card is a flex column with its own gap, and the
     field the `also` line sits in is another. */
  .also,
  .footnote {
    margin: 0;
  }
  .level-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .actions {
    display: flex;
    gap: var(--space-2);
    margin-top: var(--space-4);
  }
  code {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    word-break: break-all;
  }
</style>
