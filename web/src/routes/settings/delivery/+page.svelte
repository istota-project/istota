<script lang="ts">
  import type { RelayDelivery, UserProfile } from '$lib/api';
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
  import { Select, type SelectOption } from '$lib/components/ui';
  import { SettingsLayout, SettingsCard, SettingsField } from '$lib/components/settings';
  import { getUserSettings } from '$lib/settings/userSettingsContext';
  import { isManaged, managedBadge } from '$lib/settings/managed';

  const settings = getUserSettings();
  const profile = $derived(settings.profile);

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

  // Relay questions from other users. The server says which destinations would
  // reach this user now, from the relay resolver's own checks; an unavailable
  // one is shown disabled rather than hidden, so the reader can see it exists
  // and why they cannot pick it. The current value is never disabled, so a
  // preference whose binding has since gone stays visible and changeable.
  const RELAY_DELIVERY_LABELS: Record<RelayDelivery, string> = {
    '': "Asker's choice (default room)",
    room: 'My default room',
    whatsapp: 'WhatsApp',
    sms: 'SMS',
  };

  function relayDeliveryOptions(p: UserProfile): SelectOption[] {
    const current = p.relay_delivery || '';
    const offered = p.relay_delivery_options ?? [];
    return (Object.keys(RELAY_DELIVERY_LABELS) as RelayDelivery[]).map((value) => {
      const available = value === '' || offered.some((o) => o.value === value && o.available);
      return {
        value,
        label: available
          ? RELAY_DELIVERY_LABELS[value]
          : `${RELAY_DELIVERY_LABELS[value]} (not set up)`,
        disabled: !available && value !== current,
      };
    });
  }

  function relayDeliveryUnavailable(p: UserProfile): string[] {
    const offered = p.relay_delivery_options ?? [];
    return offered
      .filter((o) => o.value !== '' && !o.available)
      .map((o) => RELAY_DELIVERY_LABELS[o.value]);
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
</script>

<SettingsLayout
  description="Where results, notifications, alerts and the execution log reach you."
  loading={settings.loading}
  error={settings.error}
  info={settings.info}
>
  {#if profile}
    <SettingsCard title="Delivery">
      <!-- `labelled={false}` on all four: the slot holds a Select, whose
           bits-ui trigger is a <button> and so becomes a <label>'s implicit
           control — and where a room dropdown sits beside it there are two of
           them, so the caption would act on whichever came first. -->
      <SettingsField
        labelled={false}
        label="Default delivery destination"
        badge={managedBadge(profile, 'default_destination') ??
          managedBadge(profile, 'default_room')}
        hint="Which transport your results and notifications go out on, and — on the two transports that have rooms — which room a delivery that names none of its own lands in. Leave the room automatic and the oldest room you are alone in is used, which changes if you archive that room."
      >
        <div class="route-row">
          <Select
            value={profile.default_destination || 'talk'}
            options={destinationOptions(profile.default_destination || 'talk')}
            ariaLabel="Default delivery destination"
            fullWidth
            disabled={isManaged(profile, 'default_destination')}
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
              disabled={isManaged(profile, 'default_room')}
              onValueChange={(v) => {
                if (profile) profile.default_room = v || '';
              }}
            />
          {/if}
        </div>
      </SettingsField>
      <SettingsField
        labelled={false}
        label="Questions from other users"
        hint="Where a question another user asks you through Istota reaches you. Your choice here overrides theirs. If the one you pick stops working, questions go to your default room."
      >
        <Select
          value={profile.relay_delivery || ''}
          options={relayDeliveryOptions(profile)}
          ariaLabel="Questions from other users"
          fullWidth
          onValueChange={(v) => {
            if (profile) profile.relay_delivery = (v || '') as RelayDelivery;
          }}
        />
        {#if relayDeliveryUnavailable(profile).length > 0}
          <p class="hint">
            Not set up for your account: {relayDeliveryUnavailable(profile).join(', ')}.
          </p>
        {/if}
      </SettingsField>
      <SettingsField
        labelled={false}
        label="Send alerts to"
        badge={managedBadge(profile, 'routing')}
        hint="Optional. Route alerts (heartbeat failures, security and policy notices) to a louder or separate channel, e.g. ntfy for push. 'talk' and 'web' both deliver into a room, which you can pick beside them. Leave on (default) to use the default destination."
      >
        <div class="route-row">
          <Select
            value={routeSurface(routeDescriptor('alert'))}
            options={routeOpts(routeSurface(routeDescriptor('alert')))}
            ariaLabel="Alert delivery destination"
            fullWidth
            disabled={isManaged(profile, 'routing')}
            onValueChange={(v) => setRouteSurface('alert', routeDescriptor('alert'), v)}
          />
          {#if hasRoom(routeDescriptor('alert'))}
            <Select
              value={routeRoom(routeDescriptor('alert'))}
              options={roomOptionsFor(routeDescriptor('alert'), 'Alerts channel (default)')}
              ariaLabel="Alert delivery room"
              fullWidth
              disabled={isManaged(profile, 'routing')}
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
        badge={managedBadge(profile, 'routing')}
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
            disabled={isManaged(profile, 'routing')}
            onValueChange={(v) => setRouteSurface('log', logRouteValue(), v)}
          />
          {#if hasRoom(logRouteValue())}
            <Select
              value={routeRoom(logRouteValue())}
              options={roomOptionsFor(logRouteValue(), 'Logs channel (default)')}
              ariaLabel="Execution log room"
              fullWidth
              disabled={isManaged(profile, 'routing')}
              onValueChange={(v) => setRouteRoom('log', logRouteValue(), v)}
            />
          {/if}
        </div>
      </SettingsField>
    </SettingsCard>
  {/if}
</SettingsLayout>

<style>
  /* A delivery route: the surface, and for a roomed one the room beside it.
     Wraps rather than shrinking, so the room name stays readable on a phone. */
  .route-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .route-row > :global(*) {
    flex: 1 1 12rem;
    min-width: 0;
  }
</style>
