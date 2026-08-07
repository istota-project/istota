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
  import { changedProfileFields } from '$lib/profilePatch';
  import {
    AppShell,
    ShellHeader,
    Button,
    Field,
    Select,
    type SelectOption,
  } from '$lib/components/ui';
  import { fontSize, setFontSize, type FontSize } from '$lib/stores/fontSize';
  import { theme, setTheme, type Theme } from '$lib/stores/theme';
  import {
    ServiceCard,
    GarminCard,
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
  let oauthBusy = $state(false);
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

  function connectGoogle() {
    oauthBusy = true;
    // Full-page nav — the OAuth callback redirects back to /istota/.
    window.location.href = `${base}/google/connect`;
  }

  async function disconnectGoogle() {
    oauthBusy = true;
    try {
      await fetch(`${base}/api/google/disconnect`, {
        method: 'DELETE',
        credentials: 'include',
      });
      await reloadServices();
    } catch (e) {
      error = (e as Error).message || 'Disconnect failed';
    } finally {
      oauthBusy = false;
    }
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

  onMount(() => {
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
  // just above this list, which is a connect flow too — the three-line
  // connect/disconnect cards group together instead of being split by the
  // credential-field cards. Today that puts Google Workspace directly under
  // Nextcloud; a future OAuth service joins the group on its own. The sort is
  // stable, so everything else keeps the API's order, and it runs on filter()'s
  // fresh array rather than mutating `services`.
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
          warning="Needs the location module. Once you have settled in a new timezone for about an hour, the field above is set to it and you get a message saying so."
          hint="Off by default, because it overwrites the timezone you chose. A journey in progress does not count — it waits until you have stayed somewhere."
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
          <p class="empty">
            Log out and back in to connect — the connection is established at login.
          </p>
        {/if}
      </SettingsCard>
    {/if}

    {#each activeServices as svc (svc.service)}
      {#if svc.custom_ui && svc.service === 'garmin'}
        <GarminCard />
      {:else}
        <ServiceCard
          service={svc}
          onChanged={reloadServices}
          onConnect={connectGoogle}
          onDisconnect={disconnectGoogle}
          {oauthBusy}
        />
      {/if}
    {/each}
  </SettingsLayout>
</AppShell>

<style>
  /* Shared .settings/.card/.field/.grid/.banner/.icon-btn primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only page-specific
	   layout (module toggles, connected-service rows) stays here. */

  /* Mirrors ServiceCard's Connect/Disconnect row so the Nextcloud card and
	   the OAuth service cards below it line up. */
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
