<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getSettingsServices,
    getModules,
    getProfile,
    updateProfile,
    getMe,
    disconnectNextcloudToken,
    type ServiceCard as ServiceCardData,
    type UserProfile,
    type NextcloudTokenStatus,
  } from '$lib/api';
  import { normalizeExternalTurnDisplay } from '$lib/stores/externalTurns';
  import { changedProfileFields } from '$lib/profilePatch';
  import {
    AppShell,
    ShellHeader,
    Button,
    ConfirmDialog,
    Field,
    Select,
    type SelectOption,
  } from '$lib/components/ui';
  import { shellAtLeast } from '$lib/platform/native';
  import { clearOfflineData } from '$lib/offline/clear';
  import { notifyInfo, notifySuccess, notifyWarning, notifyError } from '$lib/stores/notices';
  import { fontSize, setFontSize, type FontSize } from '$lib/stores/fontSize';
  import { theme, setTheme, type Theme } from '$lib/stores/theme';
  import {
    ServiceCard,
    GarminCard,
    GoogleWorkspaceCard,
    HeaderSave,
    SettingsLayout,
    SettingsCard,
    SettingsField,
  } from '$lib/components/settings';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';

  let services: ServiceCardData[] = $state([]);
  let allModules: string[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let info = $state('');
  // null = operator hasn't enabled encrypted token storage → no card.
  let ncToken: NextcloudTokenStatus | null = $state(null);
  let ncTokenBusy = $state(false);

  // Full IANA timezone list from the browser (no hardcoded list / extra dep).
  // Older engines may not implement supportedValuesOf — fall back to UTC.
  const timezoneOptions: SelectOption[] = (() => {
    let zones: string[];
    try {
      zones = (Intl as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.(
        'timeZone',
      ) ?? ['UTC'];
    } catch {
      zones = ['UTC'];
    }
    if (!zones.includes('UTC')) zones = ['UTC', ...zones];
    return zones.map((z) => ({ value: z, label: z }));
  })();

  // Appearance. Both are client-local (localStorage, per browser), so they
  // apply on change with no Save step and no round-trip. Theme is also on the
  // header toggle — this is the same store, so the two stay in sync.
  const themeOptions: SelectOption[] = [
    { value: 'dark', label: 'Dark (default)' },
    { value: 'light', label: 'Light' },
  ];

  const fontSizeOptions: SelectOption[] = [
    { value: 'small', label: 'Small' },
    { value: 'medium', label: 'Medium (default)' },
    { value: 'large', label: 'Large' },
  ];

  let profile: UserProfile | null = $state(null);
  let profileSaving = $state(false);
  let profileError = $state('');
  let initialProfileJson = $state('');
  let profileDirty = $derived(profile ? JSON.stringify(profile) !== initialProfileJson : false);

  async function refresh() {
    loading = true;
    try {
      const [svcResp, profResp, modResp, meResp] = await Promise.all([
        getSettingsServices(),
        getProfile(),
        getModules(),
        getMe(),
      ]);
      services = svcResp.services;
      ncToken = meResp.nextcloud_token ?? null;
      profile = profResp.profile;
      if (profile) {
        // Normalize optional routing fields so the bindings are safe.
        profile.routing = profile.routing || {};
        profile.default_destination = profile.default_destination || 'talk';
      }
      initialProfileJson = profile ? JSON.stringify(profile) : '';
      allModules = modResp.modules;
      error = '';
    } catch (e) {
      error = (e as Error).message || 'Failed to load settings';
    } finally {
      loading = false;
    }
  }

  async function reloadServices() {
    try {
      services = (await getSettingsServices()).services;
    } catch (e) {
      error = (e as Error).message || 'Failed to reload services';
    }
  }

  function toggleDisabledModule(name: string) {
    if (!profile) return;
    const next = new Set(profile.disabled_modules || []);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    profile.disabled_modules = [...next];
  }

  // User-routable surfaces only — self-routing (istota_file) and the inline
  // repl surface are held back from the UI; the server's delivery_surfaces
  // list is the source of truth, this is the offline fallback. `web` is the
  // web chat surface: routing logs/alerts there posts into the user's room.
  const BUILTIN_SURFACES = ['talk', 'email', 'ntfy', 'web'];

  function deliverySurfaces(): string[] {
    const s = profile?.delivery_surfaces;
    return s && s.length ? s : BUILTIN_SURFACES;
  }

  // Per-purpose route dropdown. `emptyValue`/`emptyLabel` is the leading no-op
  // option; `talkLabel` spells out where the bare `talk` surface resolves for
  // this purpose (the logs room vs the alerts channel) so it isn't ambiguous.
  // A saved descriptor that isn't one of the offered surfaces (e.g. a
  // CLI-set "talk:<token>" or "talk,email") is kept as an extra option so it
  // shows and isn't silently dropped on re-save.
  function routeOptions(
    current: string,
    opts: { emptyValue?: string; emptyLabel?: string; talkLabel?: string } = {},
  ): SelectOption[] {
    const { emptyValue = '', emptyLabel = '(default)', talkLabel = 'talk' } = opts;
    const surfaces = deliverySurfaces();
    const out: SelectOption[] = [{ value: emptyValue, label: emptyLabel }];
    for (const s of surfaces) out.push({ value: s, label: s === 'talk' ? talkLabel : s });
    if (current && current !== emptyValue && !surfaces.includes(current))
      out.push({ value: current, label: current });
    return out;
  }

  // Labelled by what the reader gets, not by the stored token: "collapsed"
  // names the mechanism, and the choice being made is about how much of a
  // stranger's text sits in the transcript.
  const EXTERNAL_TURN_DISPLAY_OPTIONS: SelectOption[] = [
    { value: 'full', label: 'Show the whole message' },
    { value: 'collapsed', label: 'Sender, subject and first line (default)' },
    { value: 'hidden', label: 'Sender and subject only' },
  ];

  // Default destination dropdown: every surface, no no-op option (there is
  // always a default), plus the current value if it's a custom descriptor.
  function destinationOptions(current: string): SelectOption[] {
    const surfaces = deliverySurfaces();
    const out: SelectOption[] = surfaces.map((s) => ({ value: s, label: s }));
    if (current && !surfaces.includes(current)) out.push({ value: current, label: current });
    return out;
  }

  // The execution log is opt-in and (off) must override a provisioned
  // log_channel, so its empty option carries the explicit "none" sentinel. The
  // displayed value reflects the *effective* destination: an explicit
  // routing.log wins, else a provisioned log_channel shows as "talk" (the logs
  // channel), else "(off)".
  function logRouteValue(): string {
    const r = (profile?.routing || {})['log'];
    if (r) return r;
    if (profile?.log_channel) return 'talk';
    return 'none';
  }

  function setRoute(purpose: string, value: string) {
    if (!profile) return;
    const next = { ...(profile.routing || {}) };
    const v = (value || '').trim();
    if (v) next[purpose] = v;
    else delete next[purpose];
    profile.routing = next;
  }

  // A full page navigation, not `goto`: `/reconnect` is a server auth route that
  // answers with a redirect to Nextcloud, so the client router has nothing to
  // resolve. Same shape as GoogleWorkspaceCard's connect.
  //
  // This replaces the card's old instruction to log out and back in, which was
  // the only documented remedy for a credential that had died silently
  // (ISSUE-333).
  function reconnectNextcloud() {
    window.location.href = `${base}/reconnect`;
  }

  async function disconnectNextcloud() {
    ncTokenBusy = true;
    try {
      await disconnectNextcloudToken();
      ncToken = { connected: false, expires_at: null };
      info = 'Nextcloud connection removed.';
    } catch (e) {
      error = (e as Error).message || 'Disconnect failed';
    } finally {
      ncTokenBusy = false;
    }
  }

  // Offline storage (ISSUE-202). Shown only in a shell that installs a service
  // worker, which is what makes this more than a cache-clearing button: a
  // worker can serve a document from a build the server has deleted, and there
  // is no reload out of that. `shellAtLeast('0.10.0')` is the version that
  // declares the app-bound domains WebKit needs before it will run one at all,
  // so an older app is offered nothing it could act on. In a browser there is
  // no worker and the page reload the user already knows is the whole remedy.
  const offlineDataClearable = shellAtLeast('0.10.0');
  let confirmingClearOffline = $state(false);
  let clearingOffline = $state(false);

  // A full reload rather than a refetch, and it is the point of the action
  // rather than a flourish: the running page was served by the worker being
  // unregistered, and its module graph is the one being cleared. Reloading is
  // what makes the next document come from the network.
  //
  // Withheld when the stored data is still there. The row exists to escape a
  // state the user can see, so reloading over a clear that did nothing would
  // present the failure as the fix — and a reload takes the report away with
  // it. The other two steps are counted rather than reported: a worker that
  // was never registered and an origin with no caches are the ordinary case,
  // not a failure.
  async function clearOfflineStorage() {
    confirmingClearOffline = false;
    clearingOffline = true;
    let cleared = false;
    try {
      cleared = (await clearOfflineData()).database;
    } finally {
      clearingOffline = false;
    }
    if (!cleared) {
      error = 'Could not clear the offline data. Close the app’s other tabs and try again.';
      return;
    }
    window.location.reload();
  }

  function profileListString(values: string[]): string {
    return values.join(', ');
  }

  function parseListInput(value: string): string[] {
    return value
      .split(',')
      .map((v) => v.trim())
      .filter((v) => v.length > 0);
  }

  async function saveProfile() {
    if (!profile) return;
    profileSaving = true;
    profileError = '';
    info = '';
    try {
      const edited: Partial<UserProfile> = {
        display_name: profile.display_name,
        timezone: profile.timezone,
        email_addresses: profile.email_addresses,
        trusted_email_senders: profile.trusted_email_senders,
        quiet_email_senders: profile.quiet_email_senders,
        disabled_skills: profile.disabled_skills,
        disabled_modules: profile.disabled_modules,
        default_destination: profile.default_destination || 'talk',
        routing: profile.routing || {},
        timezone_follow_location: profile.timezone_follow_location,
        external_turn_display: profile.external_turn_display || 'collapsed',
      };
      // Send only what changed on this page. The server writes each key it is
      // given, so sending the whole form makes an untouched field overwrite
      // whatever set it since the page loaded — which for `timezone` means an
      // open tab silently undoing a travel update and triggering another one.
      const patch = changedProfileFields(edited, initialProfileJson);
      if (Object.keys(patch).length === 0) {
        info = 'No changes to save.';
        return;
      }
      await updateProfile(patch);
      info = 'Profile saved.';
      await refresh();
    } catch (e) {
      profileError = (e as Error).message || 'Save failed';
    } finally {
      profileSaving = false;
    }
  }

  // The Google connect flow is a full-page round trip that lands back here
  // with its outcome in the query string. Nothing read that parameter before,
  // so a refused connect returned the user to a page that looked exactly as
  // they left it — the failure was reported to no one.
  const GOOGLE_OUTCOMES: Record<string, { message: string; notify: typeof notifyInfo }> = {
    connected: { message: 'Google account connected.', notify: notifySuccess },
    error: {
      message: 'Google did not complete the connection. Try connecting again.',
      notify: notifyError,
    },
    no_scopes: {
      message:
        'Nothing was requested, so there was nothing to connect. Choose at least one Google service first.',
      notify: notifyWarning,
    },
  };

  function reportGoogleOutcome() {
    const outcome = new URLSearchParams(window.location.search).get('google');
    if (!outcome) return;
    const entry = GOOGLE_OUTCOMES[outcome];
    if (entry) entry.notify(entry.message, { key: 'settings:google-connect' });
    // Drop the parameter so a reload does not re-announce a stale outcome.
    const url = new URL(window.location.href);
    url.searchParams.delete('google');
    history.replaceState(history.state, '', url);
  }

  onMount(() => {
    reportGoogleOutcome();
    void refresh();
  });

  // Identity and Preferences edit one record and were saved by two copies of
  // the same button. One save in the app bar covers both — and, by aggregation,
  // the connected-service cards below, which each used to carry a third.
  useSettingsSave(() => ({
    dirty: profileDirty,
    saving: profileSaving,
    save: saveProfile,
  }));

  // /settings/services already filters to connected services (no module-owned
  // monarch/feeds/overland leak through). Skip cards whose status is
  // "unavailable" — historically used to mean "no resource declaration" but
  // now only OAuth services with the global flag off can land there.
  //
  // OAuth cards sort to the top so they sit with the Nextcloud card rendered
  // just above this list, which is a connect flow too — the account
  // connections group together instead of being split by the credential-field
  // cards. (They were all three-line connect/disconnect cards when this was
  // written; the Google one is now the tallest card on the page, which changes
  // how the grouping looks but not what it is for.) The sort is stable, so
  // everything else keeps the API's order, and it runs on filter()'s fresh
  // array rather than mutating `services`.
  let activeServices = $derived(
    services
      .filter((s) => s.status !== 'unavailable')
      .sort((a, b) => Number(b.oauth ?? false) - Number(a.oauth ?? false)),
  );
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader title="User settings">
      {#snippet tools()}
        <HeaderSave />
      {/snippet}
    </ShellHeader>
  {/snippet}

  <SettingsLayout
    description="Profile and per-service credentials. Secrets are encrypted at rest and never sent back to the browser — secret fields are write-only."
    {loading}
    error={error || profileError}
    {info}
  >
    {#if profile}
      <SettingsCard title="Identity">
        <p class="hint">
          How Istota addresses you. User ID: <code>{profile.user_id}</code>
        </p>

        <SettingsField label="Display name">
          <input type="text" bind:value={profile.display_name} />
        </SettingsField>
        <SettingsField label="Email addresses (comma-separated)">
          <input
            type="text"
            value={profileListString(profile.email_addresses)}
            oninput={(e) => {
              if (profile)
                profile.email_addresses = parseListInput(
                  (e.currentTarget as HTMLInputElement).value,
                );
            }}
          />
        </SettingsField>
        <SettingsField
          label="Timezone (IANA)"
          hint="Setting a timezone here overrides your Nextcloud timezone and is kept across restarts."
        >
          <Select
            value={profile.timezone || 'UTC'}
            options={timezoneOptions}
            ariaLabel="Timezone"
            fullWidth
            onValueChange={(v) => {
              if (profile) profile.timezone = v;
            }}
          />
        </SettingsField>
        <SettingsField
          label="Update timezone when I travel"
          checkbox
          hint="Needs the location module. Once you have settled in a new timezone for about an hour, the field above is set to it and you get a message saying so. Off by default, because it overwrites the timezone you chose. A journey in progress does not count — it waits until you have stayed somewhere."
        >
          <input type="checkbox" bind:checked={profile.timezone_follow_location} />
        </SettingsField>
      </SettingsCard>

      <SettingsCard
        title="Appearance"
        description="Stored in this browser and applied immediately — no Save needed."
      >
        <SettingsField label="Theme" hint="Also on the toggle in the header.">
          <Select
            value={$theme}
            options={themeOptions}
            ariaLabel="Theme"
            fullWidth
            onValueChange={(v) => setTheme(v as Theme)}
          />
        </SettingsField>
        <SettingsField
          label="Text size"
          hint="Scales the whole interface. Small is the original, denser size; large steps it up further for easier reading."
        >
          <Select
            value={$fontSize}
            options={fontSizeOptions}
            ariaLabel="Text size"
            fullWidth
            onValueChange={(v) => setFontSize(v as FontSize)}
          />
        </SettingsField>
      </SettingsCard>

      <SettingsCard title="Preferences" description="How Istota behaves for your account.">
        <SettingsField label="Trusted email senders (fnmatch patterns, comma-separated)">
          <input
            type="text"
            value={profileListString(profile.trusted_email_senders)}
            oninput={(e) => {
              if (profile)
                profile.trusted_email_senders = parseListInput(
                  (e.currentTarget as HTMLInputElement).value,
                );
            }}
          />
        </SettingsField>
        <SettingsField
          label="Quiet email senders (filed silently — no task; fnmatch patterns, comma-separated)"
        >
          <input
            type="text"
            value={profileListString(profile.quiet_email_senders)}
            oninput={(e) => {
              if (profile)
                profile.quiet_email_senders = parseListInput(
                  (e.currentTarget as HTMLInputElement).value,
                );
            }}
          />
        </SettingsField>
        <SettingsField label="Disabled skills (comma-separated)">
          <input
            type="text"
            value={profileListString(profile.disabled_skills)}
            oninput={(e) => {
              if (profile)
                profile.disabled_skills = parseListInput(
                  (e.currentTarget as HTMLInputElement).value,
                );
            }}
          />
        </SettingsField>
        {#if allModules.length > 0}
          <!-- labelled={false}: one implicit <label> would claim the first
               checkbox and leave the rest of them unlabelled. -->
          <Field label="Disabled modules" labelled={false}>
            <div class="module-toggles">
              {#each allModules as m (m)}
                <label class="module-chip">
                  <input
                    type="checkbox"
                    checked={(profile.disabled_modules || []).includes(m)}
                    onchange={() => toggleDisabledModule(m)}
                  />
                  <span>{m}</span>
                </label>
              {/each}
            </div>
            <p class="hint">
              Modules are on by default. Tick to opt out — the corresponding UI tab and scheduled
              jobs will be hidden / paused.
            </p>
          </Field>
        {/if}
        <SettingsField
          label="Email from outside, in chat"
          hint="How much of a message that arrived from an external correspondent is shown inline in the chat transcript. The turn itself always appears — this decides how much of its text comes with it."
        >
          <Select
            value={profile.external_turn_display || 'collapsed'}
            options={EXTERNAL_TURN_DISPLAY_OPTIONS}
            ariaLabel="External email display"
            fullWidth
            onValueChange={(v) => {
              if (profile) profile.external_turn_display = normalizeExternalTurnDisplay(v);
            }}
          />
        </SettingsField>
        <SettingsField
          label="Default delivery destination"
          hint="Where your results and notifications go. Alerts can use a separate channel below."
        >
          <Select
            value={profile.default_destination || 'talk'}
            options={destinationOptions(profile.default_destination || 'talk')}
            ariaLabel="Default delivery destination"
            fullWidth
            onValueChange={(v) => {
              if (profile) profile.default_destination = v || 'talk';
            }}
          />
        </SettingsField>
        <SettingsField
          label="Send alerts to"
          hint="Optional. Route alerts (heartbeat failures, security and policy notices) to a louder or separate channel, e.g. ntfy for push. 'talk' uses your alerts channel; leave on (default) to use the default destination."
        >
          <Select
            value={(profile.routing || {})['alert'] || ''}
            options={routeOptions((profile.routing || {})['alert'] || '', {
              talkLabel: 'talk (alerts channel)',
            })}
            ariaLabel="Alert delivery destination"
            fullWidth
            onValueChange={(v) => setRoute('alert', v)}
          />
        </SettingsField>
        <SettingsField
          label="Send execution log to"
          hint="Optional. The verbose per-task execution log — every tool call plus a final summary. 'talk' uses your logs channel; email and ntfy get a single final summary. (off) disables it."
        >
          <Select
            value={logRouteValue()}
            options={routeOptions(logRouteValue(), {
              emptyValue: 'none',
              emptyLabel: '(off)',
              talkLabel: 'talk (logs channel)',
            })}
            ariaLabel="Execution log destination"
            fullWidth
            onValueChange={(v) => setRoute('log', v)}
          />
        </SettingsField>
      </SettingsCard>
    {/if}

    <!-- Outside the `{#if profile}` block above, deliberately: the profile
         fetch is the first thing that fails when the app cannot reach the
         server, and a cache gone wrong is a reason it might not. A row that
         disappears exactly when it is needed is not an escape hatch. -->
    {#if offlineDataClearable}
      <SettingsCard
        title="Offline data"
        description="What this app keeps on the device so it opens and reads without a connection: the app itself, your room list, and the recent messages of the rooms you have opened."
      >
        <p class="hint">
          Clearing it takes the app back to a first install: it is downloaded again the next time
          you open it, and each room's messages are fetched again when you open the room. Messages
          waiting to send are kept, but a file held with one is not.
        </p>
        <div class="oauth-actions">
          <Button
            variant="secondary"
            size="sm"
            onclick={() => (confirmingClearOffline = true)}
            disabled={clearingOffline}
          >
            {clearingOffline ? 'Clearing…' : 'Clear offline data'}
          </Button>
        </div>
      </SettingsCard>
    {/if}

    {#if activeServices.length > 0 || ncToken}
      <div class="subsection-heading">
        <h2>Connected services</h2>
        <p class="hint">
          Per-service credentials for skills that need them. Values are encrypted at rest and never
          sent back to the browser — secret fields are write-only. Module-specific credentials live
          on their own settings pages (<a href="{base}/feeds/settings">feeds</a>,
          <a href="{base}/money/settings">money</a>,
          <a href="{base}/location/settings">location</a>).
        </p>
      </div>
    {/if}

    {#if ncToken}
      {@const nc = ncToken}
      <SettingsCard
        title="Nextcloud"
        description="When connected, messages you send from web chat appear in Nextcloud Talk under your own name, and read state syncs between web and Talk."
      >
        {#snippet status()}
          <span class="status-pill status-{nc.connected ? 'configured' : 'missing'}">
            {nc.connected ? 'Connected' : 'Not connected'}
          </span>
        {/snippet}
        {#if nc.connected}
          <div class="oauth-actions">
            <Button variant="secondary" size="sm" onclick={reconnectNextcloud}>Reconnect</Button>
            <Button
              variant="secondary"
              size="sm"
              onclick={disconnectNextcloud}
              disabled={ncTokenBusy}
            >
              {ncTokenBusy ? 'Disconnecting…' : 'Disconnect'}
            </Button>
          </div>
        {:else}
          <div class="oauth-actions">
            <Button variant="primary" size="sm" onclick={reconnectNextcloud}>Connect</Button>
          </div>
          <p class="empty">
            Connecting signs you in to Nextcloud again and brings you back here. Your session stays
            as it is.
          </p>
        {/if}
      </SettingsCard>
    {/if}

    <ConfirmDialog
      bind:open={confirmingClearOffline}
      title="Clear offline data"
      message="Are you sure? The app and its saved messages are downloaded again the next time you open each room, which needs a connection. Messages waiting to send are kept — a file held with one is not."
      confirmLabel="Clear"
      onConfirm={clearOfflineStorage}
    />

    {#each activeServices as svc (svc.service)}
      {#if svc.custom_ui && svc.service === 'garmin'}
        <GarminCard />
      {:else if svc.custom_ui && svc.service === 'google_workspace'}
        <GoogleWorkspaceCard onChanged={reloadServices} />
      {:else}
        <ServiceCard service={svc} onChanged={reloadServices} />
      {/if}
    {/each}
  </SettingsLayout>
</AppShell>

<style>
  /* Shared .settings/.card/.field/.grid/.banner/.icon-btn primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only page-specific
	   layout (module toggles, connected-service rows) stays here. */

  /* The Nextcloud card's Disconnect row. */
  .oauth-actions {
    display: flex;
    gap: var(--space-2);
  }

  .module-toggles {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .module-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 0.15rem var(--space-2);
    border-radius: var(--radius-pill);
    background: var(--surface-raised);
    font-size: var(--text-xs);
    color: var(--text-muted);
    cursor: pointer;
  }

  .module-chip input[type='checkbox'] {
    margin: 0;
    width: auto;
  }
</style>
