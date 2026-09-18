<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  // The tree's own date rendering. A local `toLocaleString` here was a second
  // copy of it, and `dateFormat.test.ts`'s drift guard is what said so — by
  // name, in the default suite. `formatRelative` rather than `formatDateTime`:
  // this stamp answers "is it keeping up", which a relative reading says
  // directly, and it falls back to an absolute date past its own threshold for
  // the vault nobody has edited in a month.
  import { formatRelative } from '$lib/dateFormat';
  import {
    getSettingsServices,
    getModules,
    getProfile,
    getVaultStatus,
    selectVaultFile,
    setVaultPassphrase,
    updateProfile,
    disconnectNextcloudToken,
    AuthError,
    uploadAvatar,
    deleteAvatar,
    AVATAR_ACCEPT,
    type ServiceCard as ServiceCardData,
    type UserProfile,
    type NextcloudTokenStatus,
    type VaultStatus,
  } from '$lib/api';
  import { normalizeExternalTurnDisplay } from '$lib/stores/externalTurns';
  import {
    joinDescriptor,
    routeOptions,
    routeRoom,
    routeSurface,
    hasRoom,
    talkRoomOptions,
    webRoomOptions,
    withSurface,
  } from '$lib/deliveryDescriptor';
  import { changedProfileFields } from '$lib/profilePatch';
  import {
    AppShell,
    ShellHeader,
    Avatar,
    AvatarPicker,
    Button,
    ConfirmDialog,
    Field,
    Input,
    Select,
    type SelectOption,
  } from '$lib/components/ui';
  import { shellAtLeast } from '$lib/platform/native';
  import { clearOfflineData } from '$lib/offline/clear';
  import { getCurrentUser } from '$lib/userContext';
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
    SecretField,
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
  // null = this user has no credential vault, which is the default for
  // everyone, and also what an unreachable endpoint resolves to. Both render
  // nothing: a heading that always says something is a heading every user has
  // to read past.
  let vault: VaultStatus | null = $state(null);
  // The whole response, including the form's half — which is present for a user
  // with no vault, where `vault` above is deliberately null.
  let vaultForm: VaultStatus | null = $state(null);

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

  /* This page needs the identity *fresh* rather than merely current — a
     Nextcloud connect made elsewhere changes `nextcloud_token` while it is open
     — so it asks the layout to re-resolve rather than fetching a second `/me`
     of its own (ISSUE-355). The same one request as before, moved rather than
     removed, and now the nav and the offline cache see the new record too. */
  const identity = getCurrentUser();

  /* The profile picture is deliberately outside `profile` and outside
     `profileDirty`. It commits on pick through its own multipart call, so
     letting it into the JSON patch would light the header Save button for a
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
     it and each field would otherwise need a non-null assertion. `$derived.by`
     rather than `$derived` for the same reason in the other direction — inside
     a closure `profile` is its declared type again, where at this point in the
     module body it is still narrowed to the `null` it was initialised with. */
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

  async function refresh() {
    loading = true;
    try {
      const [svcResp, profResp, modResp, confirmed] = await Promise.all([
        getSettingsServices(),
        getProfile(),
        getModules(),
        identity.reload(),
      ]);
      // `reload()` never rejects — the layout owns the 401 redirect and the
      // offline fallback — so the failure the other three would have raised has
      // to be raised here instead. Without it a `/me` that fails on its own
      // (its timeout is its own, and a 500 is not a connectivity failure) would
      // leave this page showing whatever connection state the layout last
      // resolved, silently, where it used to say the settings could not load.
      if (!confirmed) throw new Error('Could not confirm your account details.');
      services = svcResp.services;
      ncToken = identity.user.nextcloud_token ?? null;
      profile = profResp.profile;
      if (profile) {
        // Normalize optional routing fields so the bindings are safe.
        profile.routing = profile.routing || {};
        profile.default_destination = profile.default_destination || 'talk';
        profile.default_room = profile.default_room || '';
      }
      initialProfileJson = profile ? JSON.stringify(profile) : '';
      allModules = modResp.modules;
      error = '';
    } catch (e) {
      error = (e as Error).message || 'Failed to load settings';
    } finally {
      loading = false;
    }
    // Outside the `Promise.all` and outside the try, deliberately. The vault is
    // an optional per-user feature nobody has by default, so a deployment where
    // this endpoint is unreachable, slow or answering an error must not be a
    // settings page that fails to load — the cards it governs are the page's
    // actual content. Failure leaves `vault` null, which renders nothing, which
    // is the same as the ordinary unconfigured case.
    await refreshVault();
  }

  // The failing sentence, or empty when the vault is working. Computed by the
  // server: the precedence between a live finding and a recorded one is a rule,
  // and restating it here was a second copy of it — one that compared against
  // the literal `'ok'`, hardcoding a Python constant in TypeScript with nothing
  // holding the two in step.
  // `.by` rather than the expression form: a bare `$derived(vault?.problem)` is
  // narrowed by control-flow analysis to the `null` the state was initialised
  // with, since every assignment to `vault` is further down the file. The
  // closure defers the read and keeps the declared type.
  let vaultProblem = $derived.by(() => vault?.problem ?? '');

  async function refreshVault() {
    try {
      const status = await getVaultStatus();
      // Two pieces of state off one response, and the split is the same one the
      // server makes. `vault` is the *status line*, which renders only for a
      // configured vault — a heading that always says something is a heading
      // every user reads past for the life of the deployment. `vaultForm` is
      // the *form*, which renders whenever this surface may write, including
      // for the user who has no vault at all: that user is the one the form
      // exists for.
      vault = status && status.configured ? status : null;
      vaultForm = status ?? null;
    } catch {
      vault = null;
      vaultForm = null;
    }
  }

  // Emptied on every successful save and on every navigation away: it is a
  // credential, and leaving it in a reactive variable keeps it in the page for
  // as long as the tab is open.
  let mintedPassphrase = $state('');
  let vaultBusy = $state(false);
  let vaultError = $state('');
  let passphraseInput = $state('');

  let vaultEditable = $derived.by(() => vaultForm?.editable ?? false);
  let vaultConfigured = $derived.by(() => vaultForm?.configured ?? false);
  let vaultHasPassphrase = $derived.by(() => vaultForm?.passphrase_present ?? false);
  let vaultDir = $derived.by(() => vaultForm?.vault_dir ?? '');
  let vaultFiles = $derived.by(() => vaultForm?.files ?? []);
  let vaultFile = $derived.by(() => vaultForm?.vault_file ?? '');

  /**
   * The dropdown's options, plus one for "nothing chosen".
   *
   * The empty entry is what the user picks to undo a choice: with several files
   * and none stored the server asks the question again, which is the state the
   * card is for. It is absent when there is only one file, where there is no
   * question to ask and no choice to undo.
   */
  let vaultFileOptions: SelectOption[] = $derived.by(() => {
    const options = vaultFiles.map((name) => ({ value: name, label: name }));
    return vaultFiles.length > 1 ? [{ value: '', label: 'Choose a file…' }, ...options] : options;
  });

  /**
   * Store the choice and re-read the status.
   *
   * A filename rather than a path, validated by the server against the listing
   * it just produced — so there is no rule for this side to restate and no
   * refusal it could anticipate. What it does anticipate is the *save* failing,
   * which is reported in the card rather than swallowed.
   */
  async function chooseVaultFile(name: string) {
    if (name === vaultFile) return;
    vaultBusy = true;
    vaultError = '';
    try {
      await selectVaultFile(name);
      await refreshVault();
      notifySuccess(name ? `Reading ${name}` : 'Vault file choice cleared');
    } catch (e) {
      vaultError = (e as Error).message || 'Could not save the vault file';
      // The dropdown is bound to the server's answer, so a refused choice has
      // to be put back rather than left showing a selection nothing stored.
      await refreshVault();
    } finally {
      vaultBusy = false;
    }
  }

  let confirmingVaultReplace = $state(false);

  /**
   * Generate, or ask first when there is one to destroy.
   *
   * The server refuses a generate over an existing passphrase without
   * `replace`, on the same reasoning `istota secret ensure --generate` refuses
   * one without `--force`: minting a second value destroys the only copy this
   * deployment has of the one the KDBX is already encrypted under, and the
   * vault then fails to open until the user re-keys the file by hand. So this
   * is the page's one irreversible action, and it takes a `ConfirmDialog` like
   * every other.
   */
  function generateVaultPassphrase() {
    if (vaultHasPassphrase) {
      confirmingVaultReplace = true;
      return;
    }
    return saveVaultPassphrase(true);
  }

  function replaceVaultPassphrase() {
    return saveVaultPassphrase(true, true);
  }

  function saveTypedVaultPassphrase() {
    return saveVaultPassphrase(false);
  }

  async function saveVaultPassphrase(generate: boolean, replace = false) {
    vaultBusy = true;
    vaultError = '';
    mintedPassphrase = '';
    try {
      const resp = await setVaultPassphrase(
        generate ? { generate: true, replace } : { passphrase: passphraseInput },
      );
      // Shown once, here, because nothing reads it back — the user needs it to
      // open their own KDBX. The typed path returns an empty string, since the
      // client already has that value.
      mintedPassphrase = resp.generated ?? '';
      passphraseInput = '';
      await refreshVault();
    } catch (e) {
      vaultError = (e as Error).message || 'Could not store the passphrase';
    } finally {
      vaultBusy = false;
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

  // The option lists themselves are in `$lib/deliveryDescriptor` — plain
  // functions over plain data, so the rules in them (which surface a purpose
  // omits, keeping a value that is no longer offered) are unit-tested rather
  // than reached through a portal-backed dropdown.
  const routeOpts = (current: string, opts?: Parameters<typeof routeOptions>[2]) =>
    routeOptions(deliverySurfaces(), current, opts);

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

  // The room half of a `web` or `talk` route (ISSUE-473, ISSUE-475). Both are
  // surfaces whose destination is one room out of several the user has; email
  // and ntfy have no room at all, so their rows show the surface dropdown alone.
  const webRooms = (current: string) =>
    webRoomOptions(
      profile?.web_rooms || [],
      current,
      undefined,
      profile?.unavailable_web_rooms || [],
    );

  // The default room picker asks a different question of a dead pin than a
  // route row does, so it gets its own builder rather than a flag on the one
  // above (ISSUE-479). Its leading option means "no room pinned" rather than
  // "the room a bare `web` lands in" — which after ISSUE-477 is whatever this
  // setting says, so naming it there would be circular. And `ignored_default_room` marks a pin
  // the resolvers have stopped honouring, which is not the route rows'
  // `(unavailable)`: the delivery still arrives, just not where the user chose.
  //
  // It passes an empty `unavailable`, which this row used to receive and no
  // longer does. That drops one mark, deliberately: the only token in both sets
  // is a room the user *hid* that a route also pins, and a hidden pin is
  // recoverable — the next delivery un-hides it — so `(unavailable)` there was
  // reporting a working setting as broken. Everything the route list would have
  // marked here that is genuinely dead, `ignored_default_room` already names.
  const defaultRoomOpts = () =>
    webRoomOptions(
      profile?.web_rooms || [],
      profile?.default_room || '',
      'Automatic',
      [],
      profile?.ignored_default_room || '',
    );
  const talkRooms = (current: string, emptyLabel: string) =>
    talkRoomOptions(profile?.talk_rooms || [], current, emptyLabel);

  /** The room dropdown's options for whichever roomed surface the route is on.
   * `bareTalkLabel` names where an unpinned `talk` lands for this purpose — the
   * leading option the Talk picker needs and the web picker works out for
   * itself. It is the only place that sentence appears: the surface dropdown
   * beside it labels `talk` with the bare word (ISSUE-475). */
  function roomOptionsFor(descriptor: string, bareTalkLabel: string): SelectOption[] {
    const surface = routeSurface(descriptor);
    const room = routeRoom(descriptor);
    return surface === 'talk' ? talkRooms(room, bareTalkLabel) : webRooms(room);
  }

  function routeDescriptor(purpose: string): string {
    return (profile?.routing || {})[purpose] || '';
  }

  // Both setters take `current` — the descriptor the row is *displaying*, not
  // `routing[purpose]`. The two differ on the log row, where `logRouteValue`
  // reads `talk` off a provisioned `log_channel` with no routing key behind it:
  // deriving from the key there would join onto an empty surface and clear the
  // route instead of pinning a conversation. Only `setRouteRoom` can reach that
  // today, since the displayed fallback carries no room for `withSurface` to
  // drop — but the two rows reading one value is what keeps it that way.
  function setRouteSurface(purpose: string, current: string, value: string) {
    setRoute(purpose, withSurface(current, value));
  }

  // The surface is read rather than hardcoded: the picker beside a `talk` route
  // must write `talk:<token>`, not `web:<token>`.
  function setRouteRoom(purpose: string, current: string, room: string) {
    setRoute(purpose, joinDescriptor(routeSurface(current), room));
  }

  // The default destination names a transport and nothing else (ISSUE-475), so
  // unlike the three routes it is read and written raw: `destinationOptions`
  // keeps an operator-set `web:<token>` as its own option, and splitting it
  // here would rewrite it to a bare surface on the next save.
  //
  // A room pinned here would be half-inert anyway. `default_destination` is the
  // third rung of `resolve_destinations` and nothing else reads it — a task
  // result takes its plan from `task.output_target` or the source-type default
  // — so it governs notifications only, and *those* are what the alert route is
  // for. It differs from the routes in one more way: there is always one, so
  // clearing it falls back to `talk` rather than deleting a key.
  function setDestination(value: string) {
    if (!profile) return;
    profile.default_destination = value || 'talk';
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
        default_room: profile.default_room || '',
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
        <!-- `labelled={false}` on all three: the slot holds a Select, whose
             bits-ui trigger is a <button> and so becomes a <label>'s implicit
             control — and where a room dropdown sits beside it there are two of
             them, so the caption would act on whichever came first. -->
        <SettingsField
          labelled={false}
          label="Default delivery destination"
          hint="Which transport your results and notifications go out on, and — on the two transports that have rooms — which room a delivery that names none of its own lands in. Leave the room automatic and the oldest room you are alone in is used, which changes if you archive that room."
        >
          <div class="route-row">
            <Select
              value={profile.default_destination || 'talk'}
              options={destinationOptions(profile.default_destination || 'talk')}
              ariaLabel="Default delivery destination"
              fullWidth
              onValueChange={setDestination}
            />
            <!-- The same shape as the alert and log rows: the room picker opens
                 beside the transport when the transport has rooms, rather than
                 standing as a row of its own that email and ntfy users have to
                 read past. What it writes is unchanged — `default_room`, not a
                 room on `default_destination`, which still names a transport and
                 nothing else (ISSUE-475), because the pin governs every bare
                 `web` and `talk` destination on both surfaces at once.
                 Hiding it on email and ntfy can leave a pin set and unshown: it
                 still answers a bare `web` or `talk` route from the rows below,
                 and those rows carry their own room picker, so the room stays
                 reachable where it is doing the work. -->
            {#if hasRoom(profile.default_destination || 'talk')}
              <Select
                value={profile.default_room || ''}
                options={defaultRoomOpts()}
                ariaLabel="Default room"
                fullWidth
                onValueChange={(v) => {
                  if (profile) profile.default_room = v || '';
                }}
              />
            {/if}
          </div>
        </SettingsField>
        <SettingsField
          labelled={false}
          label="Send alerts to"
          hint="Optional. Route alerts (heartbeat failures, security and policy notices) to a louder or separate channel, e.g. ntfy for push. 'talk' and 'web' both deliver into a room, which you can pick beside them. Leave on (default) to use the default destination."
        >
          <div class="route-row">
            <Select
              value={routeSurface(routeDescriptor('alert'))}
              options={routeOpts(routeSurface(routeDescriptor('alert')))}
              ariaLabel="Alert delivery destination"
              fullWidth
              onValueChange={(v) => setRouteSurface('alert', routeDescriptor('alert'), v)}
            />
            {#if hasRoom(routeDescriptor('alert'))}
              <Select
                value={routeRoom(routeDescriptor('alert'))}
                options={roomOptionsFor(routeDescriptor('alert'), 'Alerts channel (default)')}
                ariaLabel="Alert delivery room"
                fullWidth
                onValueChange={(v) => setRouteRoom('alert', routeDescriptor('alert'), v)}
              />
            {/if}
          </div>
        </SettingsField>
        <!-- `web` is not offered here. Web chat already shows every task's tool
             calls inline in the turn itself, so a log route to `web` posts a
             second copy of what the room is displaying — and only the final
             summary, since the surface is non-edit and the live stream is
             skipped for it. The other three surfaces have no such view. An
             existing `web` log route still shows and stays editable: it falls
             through `routeOptions`' keep-the-current-value branch. -->
        <SettingsField
          labelled={false}
          label="Send execution log to"
          hint="Optional. The verbose per-task execution log — every tool call plus a final summary. 'talk' delivers into a conversation, which you can pick beside it; email and ntfy get a single final summary. Web chat is not offered: it already shows each task's tool calls in the turn itself. (off) disables it."
        >
          <div class="route-row">
            <Select
              value={routeSurface(logRouteValue())}
              options={routeOpts(routeSurface(logRouteValue()), {
                emptyValue: 'none',
                emptyLabel: '(off)',
                omit: ['web'],
              })}
              ariaLabel="Execution log destination"
              fullWidth
              onValueChange={(v) => setRouteSurface('log', logRouteValue(), v)}
            />
            {#if hasRoom(logRouteValue())}
              <Select
                value={routeRoom(logRouteValue())}
                options={roomOptionsFor(logRouteValue(), 'Logs channel (default)')}
                ariaLabel="Execution log room"
                fullWidth
                onValueChange={(v) => setRouteRoom('log', logRouteValue(), v)}
              />
            {/if}
          </div>
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
        <!--
          The vault goes on the heading rather than in the list of cards below
          it, and nothing on it is writable. It is not a connected service —
          every other entry there is a credential *for* something, and this is
          the source those credentials come from, so a card among them would
          make it a peer of the things whose fields it has just disabled. It is
          also the referent the disabled-field sentence needs, which has to be
          visible from every card that shows one: a heading is above all of
          them where a sibling card is not.

          Rendered only when there is a vault. Most deployments give nobody one.
        -->
        {#if vault}
          <p class="hint vault" data-testid="vault-status">
            <strong>Credential vault:</strong>
            {#if vault.owned && vault.owned.length > 0}
              this file is the authority for
              {#each vault.owned as name, i (name)}{#if i > 0},
                {/if}<code>{name}</code>{/each}.
            {:else}
              no services are assigned to it yet.
            {/if}
            {#if vault.path}
              It is read from <code>{vault.path}</code>, never written.
            {/if}
            {#if vaultProblem}
              <span class="vault-problem">Not working: {vaultProblem}</span>
            {:else if vault.last_success_at}
              <!--
                "applied", not "synced". Istota re-reads the file only when its
                bytes change, so a vault nobody has edited for three weeks
                reports a three-week-old timestamp and is working perfectly —
                calling that "last synced" reads as staleness and sends a user
                looking for a fault that is not there.
              -->
              Istota last applied it {formatRelative(vault.last_success_at)}, and re-reads it
              whenever the file changes.
            {:else}
              Nothing has been applied from it yet.
            {/if}
          </p>
        {/if}
      </div>
    {/if}

    <!--
      A `SettingsCard` with a `SecretField`, so it is the same shape as every
      service card beside it — a peer of the things whose credentials it
      supplies rather than something hanging off the section heading. The vault
      *status line* stays on the heading, because it is the referent the
      disabled-field sentence on each managed card points at and has to be
      visible from all of them; this is the form that sets the thing up, and a
      form is a card.

      It renders for a user with *no* vault, which is the state it exists to
      move them out of — and is exactly the state the heading above is silent
      for.

      The passphrase is a `SecretField` for the reason every other credential
      on this page is one: it is write-only, it is bullet-masked, and
      `configured` says it is set without echoing it. Generate sits beside it
      rather than above it — the security argument for a random value is real
      (see the caption) but it is one way of filling a field, not a mode of its
      own.

      What the card deliberately does not offer is an absolute-path field: an
      absolute path is checked against the trees a sandbox binds rather than
      against this user's own directory, which is the right question for a path
      an operator wrote into config.toml and not a line a user may put
      themselves on the far side of.
    -->
    {#if vaultForm}
      <SettingsCard
        title="Credential vault"
        description="Keep your credentials in a KeePassXC file instead of typing each one in here. Istota reads the file and never writes to it, so it stays yours to edit on any device."
      >
        {#snippet status()}
          <!--
            The server's own verdict, not a proxy for it. `source` says where
            the *selection* came from and is empty for a folder vault, which is
            now the ordinary way to have one — read as the pill it said "Not
            set up" over a vault that was working.
          -->
          <span class="status-pill status-{vaultConfigured ? 'configured' : 'missing'}">
            {vaultConfigured ? 'Configured' : 'Not set up'}
          </span>
        {/snippet}

        <div class="vault-form" data-testid="vault-form">
          {#if !vaultEditable}
            <p class="caption">
              Your credential vault's file is set in this deployment's configuration, so it is not
              selectable here. Ask your administrator to change it.
            </p>
          {:else}
            <SecretField
              label="Master password"
              configured={vaultHasPassphrase}
              value={passphraseInput}
              disabled={vaultBusy}
              onValueChange={(next) => (passphraseInput = next)}
            />
            <!--
              The one sentence that stops Generate reading as a write to a file
              the card has just called read-only. Istota reads the *file*; the
              master password is a credential Istota has to *hold* in order to
              open it, which is a different thing.
            -->
            <p class="caption">
              The password your KeePassXC file is encrypted with. Istota stores it so it can open
              the file — it is never written back to the file, and cannot be shown to you again. If
              you have not made the file yet, generate one here and use it as the master password
              when you create it.
            </p>
            <div class="vault-actions control-row">
              <Button
                variant="secondary"
                size="sm"
                onclick={saveTypedVaultPassphrase}
                loading={vaultBusy}
                disabled={!passphraseInput}
              >
                Save password
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onclick={generateVaultPassphrase}
                disabled={vaultBusy}
              >
                {vaultHasPassphrase ? 'Generate a new one' : 'Generate one for me'}
              </Button>
            </div>
            {#if mintedPassphrase}
              <!--
                The one place this value is ever rendered. There is no route
                that reads it back, so it is here or nowhere — which is why it
                is a bordered block rather than a line of prose, and why it is
                not a `notify()`: a transient banner that expires takes the
                only copy with it.
              -->
              <p class="vault-minted" data-testid="vault-minted">
                <strong>Copy this now — it will not be shown again:</strong>
                <code>{mintedPassphrase}</code>
              </p>
            {/if}

            <!--
              The file half, and it is a folder plus a name rather than a path.
              There is nothing to type: the user drops their KeePassXC file into
              the folder named below and the server offers what it found. With
              one file there is no question to ask, which is why the dropdown is
              absent for it and a line of prose says which file is being read.

              `warning`, not `hint`: a hint renders behind a hover "?" and is
              discoverable rather than seen, and web/AGENTS.md's rule is that
              nothing the user has to act on goes there. Putting the file
              somewhere is the action.
            -->
            {#if vaultFiles.length === 0}
              <p class="caption vault-folder" data-testid="vault-folder">
                {#if vaultDir}
                  Put your KeePassXC file in <code>{vaultDir}</code> and it will show up here.
                {:else}
                  Istota cannot reach your files on this deployment, so the vault file is an
                  administrator setting.
                {/if}
              </p>
            {:else if vaultFiles.length === 1}
              <p class="caption vault-folder" data-testid="vault-folder">
                Reading <code>{vaultFiles[0]}</code> from <code>{vaultDir}</code>.
              </p>
            {:else}
              <Field
                label="Vault file"
                warning="There is more than one file in your vault folder, so Istota needs to know which one to read."
                wide
              >
                <Select
                  value={vaultFile}
                  options={vaultFileOptions}
                  disabled={vaultBusy}
                  fullWidth
                  ariaLabel="Vault file"
                  onValueChange={chooseVaultFile}
                />
              </Field>
              <p class="caption vault-folder" data-testid="vault-folder">
                From <code>{vaultDir}</code>
              </p>
            {/if}
          {/if}
          {#if vaultError}
            <p class="banner error" data-testid="vault-error">{vaultError}</p>
          {/if}
          <ConfirmDialog
            bind:open={confirmingVaultReplace}
            title="Replace the vault password"
            message="Are you sure? Your vault file is encrypted with the password Istota already has, and generating a new one does not re-encrypt it — the vault will stop opening until you set the new password on the file yourself in KeePassXC."
            confirmLabel="Replace it"
            confirmVariant="danger"
            onConfirm={replaceVaultPassphrase}
          />
        </div>
      </SettingsCard>
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

  /* A delivery route: the surface, and for `web` the room beside it. Wraps
	     rather than shrinking, so the room name stays readable on a phone. */
  .route-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .route-row > :global(*) {
    flex: 1 1 12rem;
    min-width: 0;
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

  /* A second paragraph under the same heading, separated from the first rather
     than styled apart from it: it is the same kind of statement about the same
     card list. `.hint` carries the size and colour. */
  .vault {
    margin-top: var(--space-2);
  }

  .vault code {
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    font-size: 0.9em;
    color: var(--text-muted);
    /* A resolved filesystem path has no break opportunities of its own, so on a
       phone it would otherwise push the whole heading block sideways. */
    overflow-wrap: anywhere;
  }

  /* The one part of this paragraph that is not neutral prose. Colour alone
     would not carry it — the sentence says "Not working" in words. */
  .vault-problem {
    color: var(--status-warn-fg);
  }

  .vault-form :global(.micro-label) {
    margin: 0;
  }

  .vault-form {
    margin-top: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  /* A folder path has no break opportunities of its own, so on a phone it
     would push the card sideways. Same rule the heading's `.vault code` uses. */
  .vault-folder code {
    overflow-wrap: anywhere;
  }

  .vault-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  /* The minted passphrase. Deliberately loud: it is shown once and there is no
     route that shows it again, so a user who scrolls past it has lost it. */
  .vault-minted {
    padding: var(--space-2);
    border: 1px solid var(--status-warn-fg);
    border-radius: var(--radius-sm);
    background: var(--surface-raised);
  }

  .vault-minted code {
    display: block;
    margin-top: var(--space-1);
    /* No break opportunities of its own, and it must be selectable whole. */
    overflow-wrap: anywhere;
    user-select: all;
  }
</style>
