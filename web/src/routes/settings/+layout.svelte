<script lang="ts">
  import { invalidateNotificationCounts } from '$lib/stores/notifications';
  import { onDestroy, onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { createDrawerClose } from '$lib/ui/drawerClose';
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { getProfile, getModules, updateProfile, type UserProfile } from '$lib/api';
  import { createDraft } from '$lib/ui/draft.svelte';
  import {
    AppShell,
    Icon,
    ListRow,
    ShellHeader,
    Sidebar,
    SidebarNav,
    SidebarToggle,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { getCurrentUser } from '$lib/userContext';
  import { notifyInfo, notifySuccess, notifyWarning, notifyError } from '$lib/stores/notices';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { USER_SETTINGS_SECTIONS } from '$lib/settings/sections';
  import { appConnectionAvailable, openAppSettings } from '$lib/platform/nativeConnection';
  import { Smartphone } from '@lucide/svelte';
  import { setUserSettings } from '$lib/settings/userSettingsContext';

  let { children } = $props();

  let sidebarOpen = $state(false);
  const drawer = createDrawerClose(
    () => sidebarOpen,
    () => (sidebarOpen = false),
  );
  onDestroy(drawer.cancel);

  // The profile is held here so Account, Preferences, Notifications and
  // Delivery share one draft and one save.
  // This layout persists across a section switch, so an unsaved edit survives
  // a visit to another section and the app-bar Save still knows about it.
  const profileDraft = createDraft<UserProfile | null>(null);
  const profile = $derived(profileDraft.value);
  let allModules: string[] = $state([]);
  let allSkills: string[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let info = $state('');
  let profileSaving = $state(false);

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
        next.show_nav_unread_badges = next.show_nav_unread_badges ?? false;
        next.push_chat_alerts = next.push_chat_alerts ?? 'alerts';
        next.push_preview = next.push_preview ?? true;
        next.push_inbox_categories = next.push_inbox_categories ?? {};
      }
      profileDraft.reset(next);
      allModules = modResp.modules;
      allSkills = modResp.skills ?? [];
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
        show_nav_unread_badges: profile.show_nav_unread_badges,
        push_chat_alerts: profile.push_chat_alerts,
        push_preview: profile.push_preview,
        push_inbox_categories: profile.push_inbox_categories,
        external_turn_display: profile.external_turn_display || 'collapsed',
        relay_delivery: profile.relay_delivery || '',
      };
      // Send only what changed. The server writes each key it is given, so
      // sending the whole form makes an untouched field overwrite whatever set
      // it since the page loaded — which for `timezone` means an open tab
      // silently undoing a travel update and triggering another one.
      const patch = profileDraft.changed(edited);
      if (Object.keys(patch).length === 0) {
        info = 'No changes to save.';
        return;
      }
      await updateProfile(patch);
      if (patch.push_chat_alerts !== undefined || patch.push_inbox_categories !== undefined)
        invalidateNotificationCounts();
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
    get allSkills() {
      return allSkills;
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
    dirty: profileDraft.dirty,
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
  });

  const settingsBase = $derived(`${base}/settings`);

  // `/settings` is a prefix of every section, so the index matches exactly
  // rather than by prefix — otherwise Account stays lit everywhere.
  // In the app the page runs full screen with no native bar above it, so this entry is how a
  // loaded page reaches Device settings (Change server, Disconnect). It opens the native sheet
  // rather than a page. The app icon's quick action is the route that does not need the page.
  const appSettingsOffered = appConnectionAvailable();

  async function showAppSettings() {
    sidebarOpen = false;
    try {
      await openAppSettings();
    } catch {
      notifyError(
        'Could not open Device settings. Long-press the Istota icon and choose Device settings.',
      );
    }
  }

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
      <SidebarNav
        items={USER_SETTINGS_SECTIONS.map((section) => ({
          href: `${settingsBase}${section.href}`,
          label: section.label,
          icon: section.icon,
          active: sectionActive(section.href),
        }))}
        label="Settings sections"
        onnavigate={(item, event) => drawer.link(event, () => void goto(item.href))}
      />
      {#if appSettingsOffered}
        {#snippet deviceIcon()}<Icon icon={Smartphone} size="sm" />{/snippet}
        <ListRow onclick={showAppSettings} icon={deviceIcon}>Device</ListRow>
      {/if}
    </Sidebar>
  {/snippet}

  {@render children()}
</AppShell>
