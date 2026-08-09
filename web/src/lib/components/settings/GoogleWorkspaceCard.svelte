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
  import { Badge, Button, ConfirmDialog, Select, type SelectOption } from '$lib/components/ui';
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

<SettingsCard
  title="Google Workspace"
  description="Choose which Google services the bot may use, and at what level. A change takes effect the next time you connect: Google has to ask you again."
>
  {#if loading}
    <p class="muted">Loading…</p>
  {:else if !status}
    <p class="empty">Google status is unavailable.</p>
  {:else if !status.enabled}
    <p class="empty">Google Workspace OAuth is not configured on this Istota instance.</p>
  {:else}
    <div class="state-row">
      <Badge variant={status.connected ? 'success' : 'neutral'}>
        {status.connected ? 'Connected' : 'Not connected'}
      </Badge>
      {#if status.connected && status.missing_scopes.length > 0}
        <Badge variant="warn">Reconnect needed</Badge>
      {/if}
    </div>

    {#if status.connected}
      <h3 class="micro-label">What you granted</h3>
      {#if status.granted.length === 0}
        <p class="empty small">Google granted no recognised service scopes.</p>
      {:else}
        <ul class="grants">
          {#each status.granted as g (g.service)}
            <li>
              <span class="grant-name">{g.label}</span>
              <Badge variant={g.level === 'full' ? 'warn' : 'info'}>
                {LEVEL_LABELS[g.level]}
              </Badge>
              {#if !g.complete}
                <span class="caption">partial — some boxes were deselected at consent</span>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}

      {#if status.unrecognized_scopes.length > 0}
        <!-- Shown rather than dropped: the service map lags a hand-edited
             config, and hiding a granted scope is the thing this display
             exists to stop. -->
        <p class="caption">
          Also granted, not recognised as a service:
          {#each status.unrecognized_scopes as s, i (s)}{#if i > 0},
            {/if}<code>{s}</code>{/each}
        </p>
      {/if}

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

    <h3 class="micro-label">What to ask for</h3>
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
            <span class="caption">granted: {LEVEL_LABELS[held.level]}</span>
          {/if}
        </div>
      </SettingsField>
    {/each}

    <div class="actions">
      {#if status.connected}
        <Button variant="secondary" onclick={connect} disabled={busy}>Reconnect</Button>
        <Button variant="ghost" onclick={() => (confirmingDisconnect = true)} disabled={busy}>
          Disconnect
        </Button>
      {:else}
        <Button variant="primary" onclick={connect} disabled={busy || !status.enabled}>
          {busy ? 'Connecting…' : 'Connect'}
        </Button>
      {/if}
    </div>
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
  .state-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }
  /* .micro-label is typography only by design, so the spacing stays here. */
  h3.micro-label {
    margin: var(--space-4) 0 var(--space-2);
  }
  .grants {
    list-style: none;
    margin: 0 0 var(--space-3);
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .grants li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .grant-name {
    font-size: var(--text-sm);
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
