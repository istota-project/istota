<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import {
    getProfile,
    getModules,
    getWallet,
    AuthError,
    updateProfile,
    type UserProfile,
  } from '$lib/api';
  import { changedProfileFields } from '$lib/profilePatch';
  import { AppShell, ShellHeader, Sidebar, SidebarToggle } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { getCurrentUser } from '$lib/userContext';
  import { notifyInfo, notifySuccess, notifyWarning, notifyError } from '$lib/stores/notices';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { USER_SETTINGS_SECTIONS } from '$lib/settings/sections';
  import { setUserSettings } from '$lib/settings/userSettingsContext';

  let { children } = $props();

  let sidebarOpen = $state(false);
  let walletEnabled = $state(false);

  // The profile is held here rather than by a section because three sections
  // edit it (Account, Preferences, Delivery) and one save covers all of them.
  // This layout persists across a section switch, so an unsaved edit survives
  // a visit to another section and the app-bar Save still knows about it.
  let profile: UserProfile | null = $state(null);
  let allModules: string[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let info = $state('');
  let profileSaving = $state(false);
  let initialProfileJson = $state('');
  let profileDirty = $derived(profile ? JSON.stringify(profile) !== initialProfileJson : false);

  /* This page needs the identity *fresh* rather than merely current — a
     Nextcloud connect made elsewhere changes `nextcloud_token` while it is open
     — so it asks the layout to re-resolve rather than fetching a second `/me`
     of its own (ISSUE-355). Done here rather than on Connections, which is
     the section that reads it, so the request is made once per visit to
     settings rather than once per visit to that section. */
  const identity = getCurrentUser();

  async function reload() {
    loading = true;
    try {
      const [profResp, modResp, confirmed] = await Promise.all([
        getProfile(),
        getModules(),
        identity.reload(),
      ]);
      // `reload()` never rejects — the layout owns the 401 redirect and the
      // offline fallback — so the failure the other two would have raised has
      // to be raised here instead. Without it a `/me` that fails on its own
      // would leave the page showing whatever connection state the layout last
      // resolved, silently, where it used to say the settings could not load.
      if (!confirmed) throw new Error('Could not confirm your account details.');
      const next = profResp.profile;
      if (next) {
        // Normalize optional routing fields so the bindings are safe.
        next.routing = next.routing || {};
        next.default_destination = next.default_destination || 'talk';
        next.default_room = next.default_room || '';
        next.relay_delivery = next.relay_delivery || '';
      }
      profile = next;
      initialProfileJson = next ? JSON.stringify(next) : '';
      allModules = modResp.modules;
      error = '';
    } catch (e) {
      error = (e as Error).message || 'Failed to load settings';
    } finally {
      loading = false;
    }
  }

  async function saveProfile() {
    if (!profile) return;
    profileSaving = true;
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
        default_room: profile.default_room || '',
        routing: profile.routing || {},
        timezone_follow_location: profile.timezone_follow_location,
        external_turn_display: profile.external_turn_display || 'collapsed',
        relay_delivery: profile.relay_delivery || '',
      };
      // Send only what changed. The server writes each key it is given, so
      // sending the whole form makes an untouched field overwrite whatever set
      // it since the page loaded — which for `timezone` means an open tab
      // silently undoing a travel update and triggering another one.
      const patch = changedProfileFields(edited, initialProfileJson);
      if (Object.keys(patch).length === 0) {
        info = 'No changes to save.';
        return;
      }
      await updateProfile(patch);
      await reload();
      info = 'Profile saved.';
    } catch (e) {
      error = (e as Error).message || 'Save failed';
    } finally {
      profileSaving = false;
    }
  }

  setUserSettings({
    get profile() {
      return profile;
    },
    get allModules() {
      return allModules;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    set error(v: string) {
      error = v;
    },
    get info() {
      return info;
    },
    set info(v: string) {
      info = v;
    },
    reload,
  });

  // One save in the app bar for the profile, aggregated with whatever the open
  // section registers (a `ServiceCard`'s pending credential edits).
  useSettingsSave(() => ({
    dirty: profileDirty,
    saving: profileSaving,
    save: saveProfile,
  }));

  // The Google connect flow is a full-page round trip that lands back on
  // Connections with its outcome in the query string. Read here rather than on
  // that section so an older redirect to `/settings` is still reported.
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
    void reload();
    void Promise.resolve(getWallet())
      .then((wallet) => {
        walletEnabled = wallet?.enabled ?? false;
      })
      .catch((e) => {
        if (e instanceof AuthError) identity.expireSession();
      });
  });

  const settingsBase = $derived(`${base}/settings`);

  // `/settings` is a prefix of every section, so the index matches exactly
  // rather than by prefix — otherwise Account stays lit everywhere.
  function sectionActive(href: string): boolean {
    const path = page.url.pathname.replace(/\/$/, '');
    return path === `${settingsBase}${href}`.replace(/\/$/, '');
  }
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title="User settings"
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open settings sections"
    >
      {#snippet leading()}
        <SidebarToggle
          open={sidebarOpen}
          label="Settings sections"
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />
      {/snippet}
      {#snippet tools()}
        <HeaderSave />
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    <Sidebar title="Settings" open={sidebarOpen} onClose={() => (sidebarOpen = false)}>
      <nav class="views" aria-label="Settings sections">
        {#each USER_SETTINGS_SECTIONS.filter((section) => section.href !== '/wallet' || walletEnabled) as section (section.href)}
          {@const Icon = section.icon}
          {@const active = sectionActive(section.href)}
          <a
            class="view-btn"
            class:active
            href="{settingsBase}{section.href}"
            aria-current={active ? 'page' : undefined}
            onclick={() => (sidebarOpen = false)}
          >
            <Icon size={14} />
            <span class="view-name">{section.label}</span>
          </a>
        {/each}
      </nav>
    </Sidebar>
  {/snippet}

  {@render children()}
</AppShell>
