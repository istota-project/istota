<script lang="ts">
  /**
   * The background tracker on *this* device.
   *
   * Everything here reads and writes the native plugin directly — no server
   * round trip, because there is no server-side place to put it. One account
   * can have two phones, so a tracker setting kept in the profile would have
   * the two devices overwriting each other's row. The one server call is the
   * mint of this phone's own ingest token, on setup.
   *
   * In a browser the card renders one line saying so, rather than nothing at
   * all: a user who set this up on their phone and later opens the page on a
   * laptop would otherwise find the section simply missing and have no way to
   * tell whether that is by design.
   */
  import { onDestroy, onMount } from 'svelte';
  import { Button, Select, type SelectOption, Card, Field, Timestamp } from '$lib/components/ui';
  import {
    trackerAvailable,
    scannerAvailable,
    trackerStatus,
    startTracking,
    stopTracking,
    sendNow,
    requestPermissions,
    openAppSettings,
    configureTracker,
    scanAndDecode,
    wifiZoneAvailable,
    configureWifiZone,
    type TrackerStatus,
    type TrackingProfile,
  } from '$lib/platform/nativeLocation';
  import { deviceModel } from '$lib/platform/native';
  import { hostOf } from '$lib/location/provisioning';
  import { createLocationDevice, getLocationPlaces, type Place } from '$lib/api';

  /** Called once this phone has a new row on the server, so the device list can show it. */
  let { onDeviceAdded }: { onDeviceAdded?: () => void | Promise<void> } = $props();

  let status: TrackerStatus | null = $state(null);
  let busy = $state(false);
  let error = $state('');
  let note = $state('');

  /**
   * The profile the picker is showing.
   *
   * Held here rather than read straight off `status` for two reasons. While
   * tracking is *off* the choice has nowhere to live on the device — the
   * plugin's only way to set a profile is `start`, which also arms the
   * tracker, so applying it on selection would turn tracking on for someone
   * who had deliberately stopped it. And when a switch is refused (the plugin
   * rejects unless location is authorized) the picker must fall back to what
   * the device actually has, which it cannot do while bound to the child.
   */
  let profileChoice: TrackingProfile = $state('detailed');

  const available = trackerAvailable();
  const canScan = scannerAvailable();
  const canPinWifi = wifiZoneAvailable();

  /**
   * The user's places, for the zone picker.
   *
   * A zone is a coordinate, but nobody wants to type one — and the coordinate
   * they mean is almost always a place the server already knows, which is also
   * the place the override exists to keep them inside. Loaded lazily and
   * failing to nothing: a places request that errors leaves the picker empty
   * with an explanation, and the rest of the card unaffected.
   */
  let places: Place[] = $state([]);
  let placesError = $state('');

  const PROFILES: SelectOption[] = [
    { value: 'detailed', label: 'Detailed' },
    { value: 'places', label: 'Places' },
  ];

  const PROFILE_HINT: Record<TrackingProfile, string> = {
    detailed: 'A continuous line on the map. Sends every minute, at a battery cost.',
    places: 'Arrivals and departures only — no trace between them. Sends every five minutes.',
  };

  /**
   * Every action funnels through here so the readout can never drift from the
   * device: each one ends by adopting whatever the plugin last reported, and a
   * refusal (authorization missing, camera denied) lands in one place.
   */
  async function run(action: () => Promise<TrackerStatus | null | void>) {
    busy = true;
    error = '';
    // Cleared here rather than at the call sites, so a "Sent 12 points."
    // cannot outlive the queue it was counting and go on making a claim that
    // a Refresh or a Stop has since made false.
    note = '';
    try {
      const result = await action();
      adopt(result ?? (await trackerStatus()));
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      // A refused action leaves the device as it was, so the card has to go
      // back to the device rather than keep showing what was asked for — a
      // rejected profile switch would otherwise leave the picker displaying a
      // profile the tracker is not running.
      try {
        adopt(await trackerStatus());
      } catch {
        // The readout is already carrying an error; a failure to re-read it
        // is not a second thing to tell the user about.
      }
    } finally {
      busy = false;
    }
  }

  function adopt(next: TrackerStatus | null) {
    status = next;
    if (next) profileChoice = next.profile;
  }

  async function refresh() {
    await run(() => trackerStatus());
  }

  async function scan() {
    await run(async () => {
      const result = await scanAndDecode();
      if (!result.ok) {
        // A cancel is a decision, not a failure — say nothing about it. The
        // other two are worth reporting, and differently: one is the wrong
        // code, the other is an app that cannot scan at all.
        if (result.reason === 'unrecognised') {
          error = 'That code is not an Istota provisioning code.';
        } else if (result.reason === 'unavailable') {
          error = 'This app cannot scan a code. Update it from TestFlight.';
        }
        return null;
      }
      await configureTracker(result.provisioning);
      note = `Provisioned against ${hostOf(result.provisioning.endpoint)}.`;
      return null;
    });
  }

  async function flush() {
    await run(async () => {
      const sent = await sendNow();
      note = sent === 0 ? 'Nothing queued to send.' : `Sent ${sent} point${sent === 1 ? '' : 's'}.`;
      return null;
    });
  }

  /**
   * Apply a profile choice.
   *
   * While tracking, `start` re-arms in place so the switch costs no gap in
   * coverage. While stopped it would *begin* tracking, so the choice is only
   * remembered and travels with the next Start.
   */
  async function chooseProfile(next: TrackingProfile) {
    profileChoice = next;
    if (!status?.tracking) return;
    await run(() => startTracking(next));
  }

  /**
   * iOS refuses the Always prompt as a first ask, so this is normally two
   * taps: When In Use, then Always. The button label follows the state rather
   * than pretending one tap will finish it.
   */
  async function askPermission() {
    await run(async () => {
      await requestPermissions();
      return null;
    });
  }

  /**
   * Mint a token for this phone and hand it to the tracker.
   *
   * The server keys an `app` row on the tracker's device id, so doing this
   * twice replaces the phone's own row rather than adding a second one, and no
   * other device's token is touched. The row exists from the moment the mint
   * answers, whatever `configure` says next, so the list is told then.
   */
  async function provisionThisDevice(deviceId: string) {
    const minted = await createLocationDevice({
      kind: 'app',
      device_id: deviceId,
      label: deviceModel(),
    });
    void onDeviceAdded?.();
    try {
      await configureTracker({ endpoint: minted.endpoint, token: minted.token });
    } catch (e) {
      // WRONG_SERVER and INVALID_CONFIGURATION carry a message written for a
      // person; this one carries a keychain status code.
      if ((e as { code?: unknown } | null)?.code === 'STORE_FAILED') {
        throw new Error('The tracker could not save its credentials. Try again.');
      }
      throw e;
    }
  }

  /**
   * The permission poll.
   *
   * `requestPermissions` resolves as soon as the prompt is up, not when it is
   * answered, and the plugin sends no event for the answer, so the card asks
   * once a second for up to a minute. `onDestroy` wakes a pending wait with
   * `false`, which ends the poll without another read.
   */
  const PERMISSION_POLL_MS = 1000;
  const PERMISSION_POLL_TRIES = 60;
  let destroyed = false;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let cancelPollWait: (() => void) | null = null;

  function waitForPoll(): Promise<boolean> {
    return new Promise((resolve) => {
      if (destroyed) {
        resolve(false);
        return;
      }
      cancelPollWait = () => resolve(false);
      pollTimer = setTimeout(() => {
        pollTimer = null;
        cancelPollWait = null;
        resolve(true);
      }, PERMISSION_POLL_MS);
    });
  }

  /** The status once authorization leaves `notDetermined`, the last one read on timeout, or null if the card went away. */
  async function awaitPermissionAnswer(): Promise<TrackerStatus | null> {
    let latest: TrackerStatus | null = null;
    for (let i = 0; i < PERMISSION_POLL_TRIES; i++) {
      if (!(await waitForPoll())) return null;
      latest = await trackerStatus();
      if (!latest || latest.authorization !== 'notDetermined') return latest;
    }
    return latest;
  }

  onDestroy(() => {
    destroyed = true;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    cancelPollWait?.();
    cancelPollWait = null;
  });

  /**
   * One tap from "off" to tracking: mint, configure, get permission, start.
   *
   * Anything short of a grant leaves the tracker configured and stopped, and
   * the configured branch's own warning and buttons say what is missing.
   */
  async function enable() {
    await run(async () => {
      if (!status) return null;
      await provisionThisDevice(status.deviceId);
      let current = await trackerStatus();
      if (!current) return null;
      if (current.authorization === 'notDetermined') {
        // Shown while iOS's prompt is up, so the card reads as configured and
        // waiting rather than as nothing having happened.
        adopt(current);
        note = 'Waiting for your answer to the location prompt.';
        await requestPermissions();
        const answered = await awaitPermissionAnswer();
        note = '';
        if (!answered) return null;
        current = answered;
      }
      if (current.authorization !== 'always' && current.authorization !== 'whenInUse') {
        return current;
      }
      const started = await startTracking(profileChoice);
      const granted = started?.authorization ?? current.authorization;
      note =
        granted === 'always'
          ? 'Tracking is on.'
          : 'Tracking is on. iOS will ask again later to allow it in the background.';
      return started;
    });
  }

  /**
   * A new token for a phone whose old one was removed, and nothing else.
   *
   * Tracking state and profile stay as they are: the tracker has been logging
   * all along and holding the points the server refused, and they go out on
   * the next send.
   */
  async function setUpAgain() {
    await run(async () => {
      if (!status) return null;
      await provisionThisDevice(status.deviceId);
      note =
        'This phone has a new token. Points held since it was refused go out on the next send.';
      return null;
    });
  }

  /** GLManager's text for a 401 or 403 from the server. */
  const tokenRejected = $derived.by(() => {
    if (!status?.lastError) return false;
    return status.lastError.includes('token rejected');
  });

  /**
   * One line under the Tracking row, and only one.
   *
   * A permission problem outranks a pause: the pause is working as intended
   * and resolves itself, while the permission is why nothing will be logged at
   * all. Showing both would put the reassuring sentence next to the alarming
   * one and make neither land.
   */
  const trackingWarning = $derived.by(() => {
    if (!status) return '';
    if (status.authorization === 'denied')
      return 'Location access is denied. Only iOS Settings can restore it.';
    if (status.authorization === 'whenInUse')
      return 'Only granted while the app is open, so nothing is logged in the background.';
    if (status.authorization === 'notDetermined')
      return 'Location access has not been granted yet.';
    if (status.tracking && status.paused)
      // Says "nothing is wrong" out loud, because the readout beside it — an
      // empty queue and a last-sent time hours old — is what a broken tracker
      // looks like, and that is the reading someone opens this card to check.
      return (
        'This phone has not moved for a while, so iOS has paused updates to save ' +
        'battery. Nothing is queued or sent until it moves again, which resumes ' +
        'tracking on its own.'
      );
    return '';
  });

  /**
   * The zone picker's options: off, plus one per place.
   *
   * Keyed by place id as a string because `Select` is string-valued. A zone
   * whose coordinates match no current place still shows as configured in the
   * summary line below — the picker just cannot name it, which is honest: the
   * place it referred to has been renamed or deleted.
   */
  const ZONE_OFF = '';
  const zoneOptions: SelectOption[] = $derived([
    { value: ZONE_OFF, label: 'Off' },
    ...places.map((p) => ({ value: String(p.id), label: p.name })),
  ]);

  /** Which place the stored zone points at, or '' for none. */
  const zoneChoice = $derived.by(() => {
    if (!status?.wifiZoneSsid) return ZONE_OFF;
    const lat = status.wifiZoneLatitude;
    const lon = status.wifiZoneLongitude;
    if (lat == null || lon == null) return ZONE_OFF;
    // Compared rather than stored by id, because the device holds coordinates
    // and nothing else — it has no idea places exist. A tenth of a metre of
    // slack absorbs the float round-trip through JSON and NSUserDefaults.
    const match = places.find((p) => Math.abs(p.lat - lat) < 1e-6 && Math.abs(p.lon - lon) < 1e-6);
    return match ? String(match.id) : ZONE_OFF;
  });

  /**
   * Set or clear the zone.
   *
   * The SSID is always the network the device is on right now, never typed.
   * That is a real restriction — a zone cannot be set up for the office from
   * the sofa — and it is worth it: an SSID typed from memory that differs by a
   * character produces a zone that silently never matches, which is precisely
   * the failure mode this whole feature is prone to.
   */
  async function chooseZone(next: string) {
    if (next === ZONE_OFF) {
      await run(() => configureWifiZone(null));
      return;
    }
    const place = places.find((p) => String(p.id) === next);
    if (!place) return;
    const ssid = status?.currentWifi;
    if (!ssid) {
      error = 'Join the network you want to pin before setting a zone.';
      return;
    }
    await run(async () => {
      const result = await configureWifiZone({ ssid, latitude: place.lat, longitude: place.lon });
      note = `While on ${ssid}, this phone reports ${place.name}.`;
      return result;
    });
  }

  /**
   * Why the zone controls cannot be used, or ''.
   *
   * The capability case comes first and is the one that matters: without
   * Access WiFi Information the device reads no network name at all, so a zone
   * would store fine and then never match anything. Saying "not on wifi" is
   * the same observation from the device's side — it genuinely cannot tell the
   * two apart — so the sentence names both.
   */
  const zoneWarning = $derived.by(() => {
    if (!status) return '';
    if (placesError) return placesError;
    if (!status.currentWifi) {
      return status.wifiZoneSsid
        ? `Set for ${status.wifiZoneSsid}, but this phone cannot read a network name — ` +
            'either it is not on wifi, or this build lacks the Access WiFi Information ' +
            'capability. The zone does nothing until it can.'
        : 'No network name is readable, so there is nothing to pin. Join a wifi network, ' +
            'or update the app if this persists on one.';
    }
    if (status.wifiZoneSsid && !status.wifiZoneActive)
      return `Set for ${status.wifiZoneSsid}; this phone is on ${status.currentWifi}.`;
    return '';
  });

  onMount(() => {
    if (!available) return;
    void refresh();
    if (canPinWifi) {
      getLocationPlaces()
        .then((r) => (places = r.places))
        .catch(() => (placesError = 'Could not load your places, so there is nothing to pin to.'));
    }
  });
</script>

{#if !available}
  <Card title="This device">
    <p class="description">
      Background tracking is set up per device, in the Istota app. Open the app on the phone you
      want to track and come back to this page there.
    </p>
  </Card>
{:else}
  <Card title="This device" description="Background tracking on this phone.">
    {#snippet actions()}
      <Button variant="secondary" size="sm" onclick={refresh} disabled={busy}>Refresh</Button>
    {/snippet}

    {#if error}<p class="tracker-error">{error}</p>{/if}
    {#if note}<p class="tracker-note">{note}</p>{/if}

    {#if !status}
      <p class="description">Reading the tracker…</p>
    {:else if !status.configured}
      <p class="description">Location tracking is off on this phone.</p>
      <div class="tracker-actions">
        <Button variant="primary" size="sm" onclick={enable} disabled={busy}>
          Turn on location tracking
        </Button>
        {#if canScan}
          <Button variant="secondary" size="sm" onclick={scan} disabled={busy}>
            Scan a code instead
          </Button>
        {/if}
      </div>
    {:else}
      {#if tokenRejected}
        <p class="tracker-warning">
          The server no longer accepts this phone's token. It was probably removed from the device
          list.
        </p>
        <div class="tracker-actions">
          <Button variant="primary" size="sm" onclick={setUpAgain} disabled={busy}>
            Set up again
          </Button>
        </div>
      {/if}

      <Field label="Tracking" warning={trackingWarning}>
        <div class="hstack">
          <!-- "Paused" rather than "On", because On beside an hours-old
               last-sent time is the reading that sends someone looking for a
               fault. It is deliberately not styled as a problem: nothing is
               wrong, and Stop stays available throughout. -->
          <span
            class="tracker-state"
            class:on={status.tracking && !status.paused}
            class:paused={status.tracking && status.paused}
          >
            {status.tracking ? (status.paused ? 'Paused' : 'On') : 'Off'}
          </span>
          <!-- Start/Stop is present in every state a user can act on. Only a
               denied authorization replaces it, because there the tracker
               cannot run at all until Settings says otherwise; a downgrade to
               While Using still has to be stoppable from here. -->
          {#if status.authorization === 'denied'}
            <Button
              variant="secondary"
              size="sm"
              onclick={() => run(openAppSettings)}
              disabled={busy}
            >
              Open iOS Settings
            </Button>
          {:else}
            {#if status.tracking}
              <Button
                variant="secondary"
                size="sm"
                onclick={() => run(stopTracking)}
                disabled={busy}
              >
                Stop
              </Button>
            {:else}
              <Button
                variant="primary"
                size="sm"
                onclick={() => run(() => startTracking(profileChoice))}
                disabled={busy}
              >
                Start
              </Button>
            {/if}
            {#if status.authorization !== 'always'}
              <Button variant="secondary" size="sm" onclick={askPermission} disabled={busy}>
                {status.authorization === 'whenInUse' ? 'Allow always' : 'Allow location'}
              </Button>
            {/if}
          {/if}
        </div>
      </Field>

      <Field
        label="Profile"
        hint={PROFILE_HINT[profileChoice]}
        warning={status.tracking ? '' : 'Applies when you start tracking.'}
      >
        <Select
          value={profileChoice}
          options={PROFILES}
          fullWidth
          disabled={busy}
          ariaLabel="Tracking profile"
          onValueChange={(v) => chooseProfile(v as TrackingProfile)}
        />
      </Field>

      {#if canPinWifi}
        <Field
          label="Wifi zone"
          hint={'While joined to this phone’s current network, report a fixed place instead of ' +
            'a measured position. Indoors a fix wanders tens of metres between readings, which ' +
            'can walk you across a place boundary and back all evening; being on the network is ' +
            'better proof of being there than any one fix.'}
          warning={zoneWarning}
        >
          <Select
            value={zoneChoice}
            options={zoneOptions}
            fullWidth
            disabled={busy || !status.currentWifi || places.length === 0}
            ariaLabel="Wifi zone place"
            onValueChange={chooseZone}
          />
        </Field>
      {/if}

      <dl class="kv">
        <dt>Queued points</dt>
        <dd>{status.queuedPoints}</dd>
        <dt>Last sent</dt>
        <!-- This row used to fall back to an absolute timestamp past a day, and
             it is the one place here where the exact instant is worth having:
             the question a stalled queue raises is when it stopped. The ladder
             is the same as everywhere else now, so the instant is rendered
             beside it rather than instead of it.

             A second line rather than a `title`: this card only mounts inside
             the iOS shell, where a hover tooltip cannot be reached at all.
             `web/AGENTS.md` — "do not put anything the user needs to act on
             behind hover". -->
        <dd>
          <Timestamp value={status.lastSentAt} format="relative" empty="never" title={false} />
          {#if status.lastSentAt}
            <Timestamp
              class="caption sent-exact"
              value={status.lastSentAt}
              format="datetime"
              title={false}
            />
          {/if}
        </dd>
        {#if status.droppedPoints > 0}
          <dt>Dropped</dt>
          <dd class="tracker-bad">{status.droppedPoints}</dd>
        {/if}
        {#if status.lastError}
          <dt>Last error</dt>
          <dd class="tracker-bad">{status.lastError}</dd>
        {/if}
        <dt>Sending to</dt>
        <dd>{status.endpointHost ?? 'not set'}</dd>
        <dt>Device</dt>
        <dd class="tracker-id">{status.deviceId}</dd>
      </dl>

      <div class="tracker-actions">
        <Button variant="secondary" size="sm" onclick={flush} disabled={busy}>Send now</Button>
        {#if canScan}
          <Button variant="secondary" size="sm" onclick={scan} disabled={busy}>Rescan code</Button>
        {/if}
      </div>
    {/if}
  </Card>
{/if}

<style>
  .tracker-state {
    font-size: var(--text-sm);
    color: var(--text-dim);
  }

  .tracker-state.on {
    color: var(--status-success-fg);
  }

  /* Info, not warning: a pause is the battery saving working. */
  .tracker-state.paused {
    color: var(--status-info-fg);
  }

  .tracker-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .tracker-error {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--status-danger-fg);
  }

  .tracker-warning {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--status-warn-fg);
  }

  .tracker-note {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  .tracker-bad {
    color: var(--status-warn-fg);
  }

  .tracker-id {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    word-break: break-all;
  }

  .kv dd {
    color: var(--text-secondary);
  }

  /* Placement only. The colour and the size are `.caption`'s, per
	   `web/AGENTS.md` — restating either here is what `captions.test.ts` fails. */
  dd :global(.sent-exact) {
    display: block;
  }
</style>
