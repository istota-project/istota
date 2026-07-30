<script lang="ts">
  /**
   * The background tracker on *this* device.
   *
   * Everything here reads and writes the native plugin directly — no server
   * round trip, because there is no server-side place to put it. One account
   * can have two phones, so a tracker setting kept in the profile would have
   * the two devices overwriting each other's row.
   *
   * In a browser the card renders one line saying so, rather than nothing at
   * all: a user who set this up on their phone and later opens the page on a
   * laptop would otherwise find the section simply missing and have no way to
   * tell whether that is by design.
   */
  import { onMount } from 'svelte';
  import { SettingsCard, SettingsField } from '$lib/components/settings';
  import { Button, Select, type SelectOption } from '$lib/components/ui';
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
  import { hostOf } from '$lib/location/provisioning';
  import { getLocationPlaces, type Place } from '$lib/api';

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

  function formatSent(iso: string | null): string {
    if (!iso) return 'never';
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return iso;
    const minutes = Math.round((Date.now() - at.getTime()) / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} h ago`;
    return at.toLocaleString();
  }

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
  <SettingsCard title="This device">
    <p class="hint">
      Background tracking is set up per device, in the Istota app. Open the app on the phone you
      want to track and come back to this page there.
    </p>
  </SettingsCard>
{:else}
  <SettingsCard title="This device" description="Background tracking on this phone.">
    {#snippet actions()}
      <Button variant="ghost" size="sm" onclick={refresh} disabled={busy}>Refresh</Button>
    {/snippet}

    {#if error}<p class="tracker-error">{error}</p>{/if}
    {#if note}<p class="tracker-note">{note}</p>{/if}

    {#if !status}
      <p class="hint">Reading the tracker…</p>
    {:else if !status.configured}
      <p class="hint">
        Not provisioned yet. Generate a token above on another screen, then scan the code it shows.
      </p>
      {#if canScan}
        <div class="tracker-actions">
          <Button variant="primary" size="sm" onclick={scan} disabled={busy}>
            Scan provisioning code
          </Button>
        </div>
      {:else}
        <p class="tracker-warning">
          This version of the app cannot scan a code. Update it from TestFlight.
        </p>
      {/if}
    {:else}
      <!-- labelled={false} because this slot holds buttons: inside a <label>
           a <button> becomes the label's implicit control, and clicking the
           caption "Tracking" would stop the tracker. -->
      <SettingsField label="Tracking" warning={trackingWarning} labelled={false}>
        <div class="tracker-row">
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
              <Button variant="ghost" size="sm" onclick={askPermission} disabled={busy}>
                {status.authorization === 'whenInUse' ? 'Allow always' : 'Allow location'}
              </Button>
            {/if}
          {/if}
        </div>
      </SettingsField>

      <SettingsField
        label="Profile"
        hint={PROFILE_HINT[profileChoice]}
        warning={status.tracking ? '' : 'Applies when you start tracking.'}
        labelled={false}
      >
        <Select
          value={profileChoice}
          options={PROFILES}
          fullWidth
          disabled={busy}
          ariaLabel="Tracking profile"
          onValueChange={(v) => chooseProfile(v as TrackingProfile)}
        />
      </SettingsField>

      {#if canPinWifi}
        <!-- labelled={false} for the same reason as Tracking above: the slot
             holds a Select, whose bits-ui trigger is a <button>. -->
        <SettingsField
          label="Wifi zone"
          hint={'While joined to this phone’s current network, report a fixed place instead of ' +
            'a measured position. Indoors a fix wanders tens of metres between readings, which ' +
            'can walk you across a place boundary and back all evening; being on the network is ' +
            'better proof of being there than any one fix.'}
          warning={zoneWarning}
          labelled={false}
        >
          <Select
            value={zoneChoice}
            options={zoneOptions}
            fullWidth
            disabled={busy || !status.currentWifi || places.length === 0}
            ariaLabel="Wifi zone place"
            onValueChange={chooseZone}
          />
        </SettingsField>
      {/if}

      <dl class="kv">
        <dt>Queued points</dt>
        <dd>{status.queuedPoints}</dd>
        <dt>Last sent</dt>
        <dd>{formatSent(status.lastSentAt)}</dd>
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
          <Button variant="ghost" size="sm" onclick={scan} disabled={busy}>Rescan code</Button>
        {/if}
      </div>
    {/if}
  </SettingsCard>
{/if}

<style>
  .tracker-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

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
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: var(--text-xs);
    word-break: break-all;
  }

  .kv {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: var(--space-1) var(--space-3);
    margin: 0;
    font-size: var(--text-sm);
  }

  .kv dt {
    color: var(--text-dim);
  }

  .kv dd {
    margin: 0;
    color: var(--text-secondary);
  }
</style>
