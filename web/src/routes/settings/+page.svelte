<script lang="ts">
  import { uploadAvatar, deleteAvatar, AVATAR_ACCEPT, AuthError } from '$lib/api';
  import { Avatar, AvatarPicker, Select, type SelectOption } from '$lib/components/ui';
  import { getCurrentUser } from '$lib/userContext';
  import {
    SecurityCard,
    SettingsLayout,
    SettingsCard,
    SettingsField,
  } from '$lib/components/settings';
  import { getUserSettings } from '$lib/settings/userSettingsContext';
  import { parseListInput, profileListString } from '$lib/settings/listInput';
  import { isManaged, managedBadge } from '$lib/settings/managed';

  const settings = getUserSettings();
  const identity = getCurrentUser();
  const profile = $derived(settings.profile);

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

  /* The profile picture is deliberately outside `profile` and outside the
     profile's dirty check. It commits on pick through its own multipart call,
     so letting it into the JSON patch would light the header Save button for a
     change that has already landed and then PUT an empty patch. */
  /* What is happening, while it is. A phone photograph takes seconds to go up,
     and with the zone back on its prompt and the preview still on the old
     picture there was nothing on screen saying anything had been picked — which
     reads as a control that did nothing, and invites picking the same file
     again. */
  let avatarBusyLabel = $state('');
  let avatarError = $state('');
  let avatarNote = $state('');
  /* What the *server* last told this page the caller's picture is. `undefined`
     means it has said nothing since the page loaded, in which case the identity
     the layout resolved is the answer. It is only ever set from an upload's own
     response, which is authoritative, and is dropped again as soon as a
     `reload()` puts the same hash on the shared record. */
  let uploadedHash: string | null | undefined = $state(undefined);
  const avatarVersion = $derived(
    uploadedHash === undefined ? (identity.user.avatars?.user ?? null) : uploadedHash,
  );
  /* Read here rather than inside the preview snippet: a snippet body is its own
     scope, so the `{#if profile}` guard around the card does not narrow through
     it and each field would otherwise need a non-null assertion. */
  const avatarIdentity = $derived.by(() =>
    profile ? { userId: profile.user_id, label: profile.display_name || profile.user_id } : null,
  );

  async function uploadPicture(file: File) {
    avatarBusyLabel = 'Saving your picture…';
    avatarError = '';
    avatarNote = '';
    try {
      const stored = await uploadAvatar(file);
      // Adopted before the reload, because the browser holds the old `?v` URL
      // as `immutable` and would keep painting the old face until the new hash
      // reaches the `src`.
      uploadedHash = stored.hash;
      const confirmed = await identity.reload();
      // Dropped only once the shared record actually carries the new hash —
      // never on the strength of `reload()` reporting an answer. A reload
      // superseded by a later one returns `true` without painting (see
      // `loadUser` in `routes/+layout.svelte`), so trusting the boolean can
      // put the preview back on a picture the browser holds as `immutable`
      // and will not re-fetch. Dropping it once the record agrees is also what
      // keeps a change made in another tab from being pinned by this one.
      if (identity.user.avatars?.user === stored.hash) uploadedHash = undefined;
      else if (!confirmed)
        avatarNote = 'Picture saved. Your account details could not be refreshed.';
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else avatarError = (e as Error).message || 'Could not save that picture.';
    } finally {
      avatarBusyLabel = '';
    }
  }

  async function removePicture() {
    avatarBusyLabel = 'Removing your picture…';
    avatarError = '';
    avatarNote = '';
    try {
      const { deleted } = await deleteAvatar();
      // Nothing in the response says what is showing now — removing an upload
      // reveals whatever was imported behind it — so the shared record is the
      // only source for the next version, and this page holds no guess of its
      // own past this point.
      const confirmed = await identity.reload();
      uploadedHash = undefined;
      if (!confirmed)
        avatarNote = 'Removed. Your account details could not be refreshed — reload to see it.';
      else if (!deleted)
        // Phrased on what this page can see. `deleted: false` says only that
        // there was no upload row of the caller's — which is also what a
        // removal from another tab leaves behind, and there the second clause
        // would be false, so it is added only where a picture is in fact still
        // showing.
        avatarNote = avatarVersion
          ? 'There was nothing of yours to remove. The picture showing was imported from Nextcloud.'
          : 'There was nothing to remove.';
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else avatarError = (e as Error).message || 'Could not remove that picture.';
    } finally {
      avatarBusyLabel = '';
    }
  }
</script>

<SettingsLayout
  description="Who you are to Istota, and how you sign in."
  loading={settings.loading}
  error={settings.error}
  info={settings.info}
>
  {#if profile}
    <SettingsCard title="Identity">
      <p class="hint">
        How Istota addresses you. User ID: <code>{profile.user_id}</code>
      </p>

      <SettingsField
        label="Profile picture"
        labelled={false}
        wide
        error={avatarError}
        warning={avatarNote}
      >
        <AvatarPicker
          pickLabel="Choose a profile picture"
          prompt="Click the picture to choose a file, or drop or paste one here."
          accept={AVATAR_ACCEPT}
          busyLabel={avatarBusyLabel}
          removable={!!avatarVersion}
          onPick={(file) => void uploadPicture(file)}
          onRemove={removePicture}
        >
          {#snippet preview()}
            <!-- Named rather than decorative, unlike the chat gutter: there
                 the author's name is on the row beside it, while here the
                 picture *is* the state being edited and the only other signal
                 is whether Remove exists. -->
            <Avatar
              kind="user"
              userId={avatarIdentity?.userId}
              version={avatarVersion}
              label={avatarIdentity?.label ?? ''}
              alt="Your profile picture"
            />
          {/snippet}
        </AvatarPicker>
        <p class="hint">
          Removing an uploaded picture falls back to the one imported from Nextcloud, if there is
          one. Changing it here does not change the picture Nextcloud shows.
        </p>
      </SettingsField>
      <SettingsField label="Display name" badge={managedBadge(profile, 'display_name')}>
        <input
          type="text"
          bind:value={profile.display_name}
          disabled={isManaged(profile, 'display_name')}
        />
      </SettingsField>
      <SettingsField
        label="Email addresses (comma-separated)"
        badge={managedBadge(profile, 'email_addresses')}
      >
        <input
          type="text"
          disabled={isManaged(profile, 'email_addresses')}
          value={profileListString(profile.email_addresses)}
          oninput={(e) => {
            if (profile)
              profile.email_addresses = parseListInput((e.currentTarget as HTMLInputElement).value);
          }}
        />
      </SettingsField>
      <SettingsField
        label="Timezone (IANA)"
        badge={managedBadge(profile, 'timezone')}
        hint="Setting a timezone here overrides your Nextcloud timezone and is kept across restarts."
      >
        <Select
          value={profile.timezone || 'UTC'}
          options={timezoneOptions}
          ariaLabel="Timezone"
          fullWidth
          disabled={isManaged(profile, 'timezone')}
          onValueChange={(v) => {
            if (profile) profile.timezone = v;
          }}
        />
      </SettingsField>
      <SettingsField
        label="Update timezone when I travel"
        checkbox
        badge={managedBadge(profile, 'timezone_follow_location')}
        hint="Needs the location module. Once you have settled in a new timezone for about an hour, the field above is set to it and you get a message saying so. Off by default, because it overwrites the timezone you chose. A journey in progress does not count — it waits until you have stayed somewhere."
      >
        <input
          type="checkbox"
          bind:checked={profile.timezone_follow_location}
          disabled={isManaged(profile, 'timezone_follow_location')}
        />
      </SettingsField>
    </SettingsCard>
  {/if}

  <SecurityCard auth={identity.user.auth} onSignedOut={identity.expireSession} />
</SettingsLayout>
