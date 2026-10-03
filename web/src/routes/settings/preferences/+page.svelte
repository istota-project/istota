<script lang="ts">
  import { Button, ConfirmDialog, Field, Select, type SelectOption } from '$lib/components/ui';
  import { SettingsLayout, SettingsCard, SettingsField } from '$lib/components/settings';
  import { normalizeExternalTurnDisplay } from '$lib/stores/externalTurns';
  import { shellAtLeast } from '$lib/platform/native';
  import { clearOfflineData } from '$lib/offline/clear';
  import { fontSize, setFontSize, type FontSize } from '$lib/stores/fontSize';
  import { theme, setTheme, type Theme } from '$lib/stores/theme';
  import { getUserSettings } from '$lib/settings/userSettingsContext';
  import { parseListInput, profileListString } from '$lib/settings/listInput';
  import { isManaged, managedBadge } from '$lib/settings/managed';

  const settings = getUserSettings();
  const profile = $derived(settings.profile);

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

  // Labelled by what the reader gets, not by the stored token: "collapsed"
  // names the mechanism, and the choice being made is about how much of a
  // stranger's text sits in the transcript.
  const EXTERNAL_TURN_DISPLAY_OPTIONS: SelectOption[] = [
    { value: 'full', label: 'Show the whole message' },
    { value: 'collapsed', label: 'Sender, subject and first line (default)' },
    { value: 'hidden', label: 'Sender and subject only' },
  ];

  function toggleDisabledModule(name: string) {
    if (!profile) return;
    const next = new Set(profile.disabled_modules || []);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    profile.disabled_modules = [...next];
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
      settings.error =
        'Could not clear the offline data. Close the app’s other tabs and try again.';
      return;
    }
    window.location.reload();
  }
</script>

<SettingsLayout
  description="How Istota looks on this device and behaves for your account."
  loading={settings.loading}
  error={settings.error}
  info={settings.info}
>
  {#if profile}
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
      <SettingsField
        label="Trusted email senders (fnmatch patterns, comma-separated)"
        badge={managedBadge(profile, 'trusted_email_senders')}
      >
        <input
          type="text"
          disabled={isManaged(profile, 'trusted_email_senders')}
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
        badge={managedBadge(profile, 'quiet_email_senders')}
      >
        <input
          type="text"
          disabled={isManaged(profile, 'quiet_email_senders')}
          value={profileListString(profile.quiet_email_senders)}
          oninput={(e) => {
            if (profile)
              profile.quiet_email_senders = parseListInput(
                (e.currentTarget as HTMLInputElement).value,
              );
          }}
        />
      </SettingsField>
      <SettingsField
        label="Disabled skills (comma-separated)"
        badge={managedBadge(profile, 'disabled_skills')}
      >
        <input
          type="text"
          disabled={isManaged(profile, 'disabled_skills')}
          value={profileListString(profile.disabled_skills)}
          oninput={(e) => {
            if (profile)
              profile.disabled_skills = parseListInput((e.currentTarget as HTMLInputElement).value);
          }}
        />
      </SettingsField>
      {#if settings.allModules.length > 0}
        <!-- labelled={false}: one implicit <label> would claim the first
             checkbox and leave the rest of them unlabelled. -->
        <Field
          label="Disabled modules"
          labelled={false}
          badge={managedBadge(profile, 'disabled_modules')}
        >
          <div class="module-toggles">
            {#each settings.allModules as m (m)}
              <label class="module-chip">
                <input
                  type="checkbox"
                  disabled={isManaged(profile, 'disabled_modules')}
                  checked={(profile.disabled_modules || []).includes(m)}
                  onchange={() => toggleDisabledModule(m)}
                />
                <span>{m}</span>
              </label>
            {/each}
          </div>
          <p class="hint">
            Modules are on by default. Tick to opt out — the corresponding UI tab and scheduled jobs
            will be hidden / paused.
          </p>
        </Field>
      {/if}
      <SettingsField
        label="Email from outside, in chat"
        badge={managedBadge(profile, 'external_turn_display')}
        hint="How much of a message that arrived from an external correspondent is shown inline in the chat transcript. The turn itself always appears — this decides how much of its text comes with it."
      >
        <Select
          value={profile.external_turn_display || 'collapsed'}
          options={EXTERNAL_TURN_DISPLAY_OPTIONS}
          ariaLabel="External email display"
          fullWidth
          disabled={isManaged(profile, 'external_turn_display')}
          onValueChange={(v) => {
            if (profile) profile.external_turn_display = normalizeExternalTurnDisplay(v);
          }}
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
        Clearing it takes the app back to a first install: it is downloaded again the next time you
        open it, and each room's messages are fetched again when you open the room. Messages waiting
        to send are kept, but a file held with one is not.
      </p>
      <div class="card-actions">
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

  <ConfirmDialog
    bind:open={confirmingClearOffline}
    title="Clear offline data"
    message="Are you sure? The app and its saved messages are downloaded again the next time you open each room, which needs a connection. Messages waiting to send are kept — a file held with one is not."
    confirmLabel="Clear"
    onConfirm={clearOfflineStorage}
  />
</SettingsLayout>

<style>
  .card-actions {
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
