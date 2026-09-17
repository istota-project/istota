<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import {
    cancelWhatsAppPairing,
    getAdminConnections,
    startWhatsAppPairing,
    whatsAppPairingQrUrl,
    whatsAppPairingStreamUrl,
    type AdminConnection,
  } from '$lib/api';
  import { Badge, Button, ConfirmDialog, NoticeBanner } from '$lib/components/ui';

  /** The five small fields the stream carries. Never the pairing code. */
  interface PairingFrame {
    state: string | null;
    qr_seq: number;
    qr_available: boolean;
    expires_at: number | null;
    message: string;
  }

  /** The row's own closed states. A window is in flight in every other one. */
  const TERMINAL = new Set(['paired', 'expired', 'failed']);

  /** Typed into the destructive confirmation. Short, and not a word anyone
   *  types by reflex — the point is that the phrase cannot arrive by accident. */
  const UNLINK_CHALLENGE = 'unlink';

  let connections = $state<AdminConnection[] | null>(null);
  let loading = $state(true);
  let error = $state('');
  let notice = $state('');
  let actionError = $state('');
  let streamError = $state('');
  let aboutCollapsed = $state(true);
  let starting = $state(false);
  let cancelling = $state(false);
  let confirmOpen = $state(false);
  let frame = $state<PairingFrame | null>(null);
  let now = $state(Date.now());
  let stream: EventSource | null = null;

  let whatsapp = $derived(connections?.find((c) => c.id === 'whatsapp') ?? null);
  let link = $derived(whatsapp?.link ?? null);

  /** The pairing state to render: the live stream frame where one has arrived,
   *  the durable row otherwise. The row is what survives a browser reload; the
   *  frame is what moves within a second of the bridge writing it. */
  let pairing = $derived.by(() => {
    if (frame) {
      if (frame.state === null) return null;
      return {
        state: frame.state,
        message: frame.message ?? '',
        qrSeq: frame.qr_seq,
        qrAvailable: frame.qr_available,
        expiresAt: frame.expires_at,
        requestedBy: whatsapp?.pairing?.requested_by ?? null,
      };
    }
    const row = whatsapp?.pairing ?? null;
    if (!row || row.state === null) return null;
    return {
      state: row.state,
      message: row.message ?? '',
      qrSeq: row.qr_seq,
      qrAvailable: row.qr_available,
      expiresAt: row.expires_at_epoch,
      requestedBy: row.requested_by,
    };
  });

  let inProgress = $derived(pairing !== null && !TERMINAL.has(pairing.state));

  /** Whether a start would be accepted at all. `pairing_blocked_reason` is the
   *  server's own pre-refusal — today, a relay path that would land inside a
   *  directory the task sandbox binds — so a control offered past it is a
   *  button whose only outcome is a 409. */
  let pairingOffered = $derived(
    whatsapp !== null &&
      whatsapp.enabled &&
      whatsapp.pairing_supported &&
      whatsapp.pairing_enabled &&
      whatsapp.pairing_blocked_reason === null,
  );

  /**
   * The one-click, unforced start.
   *
   * Offered where the session has a latched permanent fault — the case this
   * whole flow exists for, which must be the shortest route through the UI —
   * **and where this process cannot see the link at all**, which on the split
   * Ansible deployment is every page load. Safe there because the unforced
   * start carries no confirmation for the bridge to honour: `repair_session`
   * refuses anything with no permanent fault and writes `session_live` onto the
   * row, which is what the card then renders.
   */
  let offersDirect = $derived(
    pairingOffered && !inProgress && (link === null || link.fatal_is_permanent),
  );

  /**
   * The destructive, typed-confirmation unlink.
   *
   * Offered wherever the session may be working — live, unsettled, or
   * unreadable from here — and deliberately **not** where the fault is already
   * latched, so the dead-session case has exactly one control. It renders below
   * the card rather than in it: a working link is the state this subsystem
   * exists to produce, and a one-click way out of it must not sit beside a
   * status badge.
   */
  let offersForced = $derived(
    pairingOffered && !inProgress && !(link !== null && link.fatal_is_permanent),
  );

  /** The stream sits behind the same gate as the other four pairing routes, so
   *  opening it on a Cloud deployment or with `pairing_enabled = false` is a
   *  retry loop against a deliberate 404. Not gated on the blocked reason: the
   *  row can still move there, and the pane should say so. */
  let streamAvailable = $derived(
    whatsapp !== null && whatsapp.enabled && whatsapp.pairing_supported && whatsapp.pairing_enabled,
  );

  let badge = $derived.by(
    (): { variant: 'success' | 'warn' | 'danger' | 'neutral' | 'info'; label: string } => {
      if (!whatsapp) return { variant: 'neutral', label: 'unknown' };
      if (!whatsapp.enabled) return { variant: 'neutral', label: 'disabled' };
      if (!whatsapp.pairing_supported) return { variant: 'info', label: 'cloud api' };
      if (link === null) return { variant: 'neutral', label: 'not readable here' };
      if (link.fatal_is_permanent) return { variant: 'danger', label: 'unlinked' };
      if (link.ready) return { variant: 'success', label: 'linked' };
      if (link.connected) return { variant: 'warn', label: 'connecting' };
      return { variant: 'warn', label: 'no sidecar' };
    },
  );

  let linkSummary = $derived.by(() => {
    if (!whatsapp) return '';
    if (!whatsapp.enabled) return 'The WhatsApp surface is switched off on this deployment.';
    if (!whatsapp.pairing_supported) {
      return (
        "This deployment runs Meta's Cloud API adapter, which is configured through Meta's " +
        'business setup and has no pairing code.'
      );
    }
    if (link === null) {
      return (
        'The bridge runs in the scheduler process, so this page cannot read the live link. ' +
        'The pairing record below crosses processes and is the part that is authoritative here.'
      );
    }
    if (link.fatal_is_permanent) {
      const reason = link.fatal_reason ? ` (${link.fatal_reason})` : '';
      return `The session is unlinked${reason} and will not come back without a re-pair.`;
    }
    if (link.ready) return 'The session is linked and working.';
    return (
      'The session has reported neither ready nor a permanent fault, so it may be mid-reconnect. ' +
      'From here that is indistinguishable from a link about to come back on its own.'
    );
  });

  /** What the card says while a window is in flight. The two states that carry
   *  a remedy — and every closed one — render the server's own message instead:
   *  it names the unit, the update log and the archived session directory, and
   *  that wording is the server's to write. */
  let pairingSummary = $derived.by(() => {
    if (!pairing) return '';
    switch (pairing.state) {
      case 'requested':
        return 'Waiting for the scheduler to pick this up, which happens within one poll interval.';
      case 'servicing':
        return 'Stopping the sidecar and moving the old session aside.';
      case 'awaiting_sidecar': {
        const declared = whatsapp?.restart_interval_seconds ?? 0;
        const interval =
          declared > 0 ? ` This deployment declares a ${declared}s restart interval.` : '';
        return `Waiting for the sidecar to come back with no credential, so it draws a code.${interval}`;
      }
      case 'awaiting_scan':
        return 'Scan this from your phone: WhatsApp, Settings, Linked devices, Link a device.';
      case 'paired':
        return 'Paired. The session is linked again.';
      default:
        return '';
    }
  });

  /** The code reaches the page only as this endpoint's bytes. `qr_seq` is what
   *  makes the browser refetch on a rotation and hold the image still between
   *  them; nothing here holds the payload. */
  let qrSrc = $derived(
    pairing !== null && pairing.state === 'awaiting_scan' && pairing.qrAvailable
      ? whatsAppPairingQrUrl(pairing.qrSeq)
      : null,
  );

  let remaining = $derived.by(() => {
    if (!inProgress || !pairing?.expiresAt) return null;
    return Math.max(0, Math.round(pairing.expiresAt * 1000 - now) / 1000);
  });

  function countdown(seconds: number): string {
    const whole = Math.floor(seconds);
    if (whole < 60) return `${whole}s`;
    return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, '0')}s`;
  }

  // A clock only while something is counting down, and cleaned up by the effect
  // itself — a leaked interval outlives the page and the test run alike.
  $effect(() => {
    if (!inProgress) return;
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  $effect(() => {
    if (streamAvailable) startStream();
    else stopStream();
  });

  function startStream() {
    if (stream || typeof EventSource === 'undefined') return;
    const es = new EventSource(whatsAppPairingStreamUrl(), { withCredentials: true });
    stream = es;
    es.addEventListener('pairing', (ev) => {
      let next: PairingFrame;
      try {
        next = JSON.parse((ev as MessageEvent).data);
      } catch {
        return;
      }
      const was = frame?.state ?? null;
      frame = next;
      streamError = '';
      // A close changes what the *link* half of the card says — green and a
      // number after a scan — and that comes off the index, not the stream.
      if (next.state !== null && next.state !== was && TERMINAL.has(next.state)) {
        void load({ quiet: true });
      }
    });
    es.addEventListener('stream_error', () => {
      stopStream();
      streamError = 'The live pairing state stopped updating. Reload to resume it.';
    });
    // No `error` listener: unlike the log tail this URL carries no cursor, so
    // the browser's own reconnect re-requests exactly the right thing and a
    // hand-rolled retry would only duplicate it.
  }

  function stopStream() {
    stream?.close();
    stream = null;
  }

  async function load(opts: { quiet?: boolean } = {}) {
    if (!opts.quiet) loading = true;
    try {
      const payload = await getAdminConnections();
      connections = payload.connections;
      error = '';
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Failed to load connections';
      if (connections) actionError = message;
      else error = message;
    } finally {
      loading = false;
    }
  }

  /** `apiFetch` keeps the status and discards the body, so the refusals are
   *  named here. The 409s the card can predict are pre-empted by the controls
   *  above; what is left is a request that raced another admin, and the
   *  best-effort live-session gate the route applies where it can see the
   *  link. */
  function startFailure(e: unknown): string {
    const message = e instanceof Error ? e.message : 'Failed to start pairing';
    if (message.includes('409')) {
      return (
        'Refused: either a pairing request is already in progress, or the session has reported ' +
        'no permanent fault. Use “Unlink and re-pair” to confirm disconnecting it.'
      );
    }
    if (message.includes('400'))
      return 'The confirmation did not reach the server. Nothing was started.';
    if (message.includes('404'))
      return 'Pairing from the browser is switched off on this deployment.';
    if (message.includes('503')) return 'The WhatsApp surface is disabled on this deployment.';
    return message;
  }

  async function start(force: boolean) {
    starting = true;
    notice = '';
    actionError = '';
    try {
      // Both flags on every call, and neither inferred. `force` is the
      // operator's acceptance of a disconnect; the server refuses it without
      // its companion rather than filling one in.
      await startWhatsAppPairing({ force, confirmDisconnect: force });
      notice = force
        ? 'Re-pair requested. The sidecar is being asked to stop; a code follows once it restarts.'
        : 'Pairing requested. The scheduler picks it up within one poll interval.';
    } catch (e) {
      actionError = startFailure(e);
    } finally {
      starting = false;
      await load({ quiet: true });
    }
  }

  async function cancel() {
    cancelling = true;
    notice = '';
    actionError = '';
    try {
      const outcome = await cancelWhatsAppPairing();
      if (outcome.cancelled) {
        notice =
          'Pairing cancelled. Any session directory already moved aside is left where it is — ' +
          'the message below names it.';
      } else if (outcome.reason === 'servicing') {
        actionError =
          'The re-pair is already running and cannot be stopped part-way. It will report its ' +
          'outcome here.';
      } else {
        notice = 'There was no open pairing request to cancel.';
      }
    } catch (e) {
      actionError = e instanceof Error ? e.message : 'Failed to cancel pairing';
    } finally {
      cancelling = false;
      await load({ quiet: true });
    }
  }

  onMount(() => load());
  onDestroy(stopStream);
</script>

<div class="settings connections-page">
  {#if loading && !connections}
    <div class="center-msg">Loading connections…</div>
  {:else if error && !connections}
    <div class="center-msg error">{error}</div>
  {:else if whatsapp}
    <NoticeBanner
      title="Deployment-level connections"
      variant="info"
      bind:collapsed={aboutCollapsed}
    >
      <p>
        These are the links the deployment holds as a whole, not the per-user services on your
        settings page. There is one for now: the WhatsApp session, which is a single paired device
        shared by everybody on this install.
      </p>
      <p>
        Re-pairing asks the running sidecar to stop, moves the old credential aside, and shows the
        code the restarted sidecar draws. It does not delete anything — the previous session
        directory is kept beside the new one and named in the outcome below.
      </p>
      <p>
        <strong>A pairing code is a full-account credential.</strong> It is rendered by the server and
        never travels as text, and the window closes on its own; even so, keep the page to yourself while
        one is open.
      </p>
    </NoticeBanner>

    {#if actionError}
      <div class="banner error">{actionError}</div>
    {/if}
    {#if streamError}
      <div class="banner warn">{streamError}</div>
    {/if}
    {#if notice}
      <div class="banner info">{notice}</div>
    {/if}

    <section class="card" data-testid="whatsapp-card">
      <div class="card-head">
        <h2>{whatsapp.label}</h2>
        <Badge variant={badge.variant}>{badge.label}</Badge>
      </div>

      <dl class="kv">
        <dt>Adapter</dt>
        <dd>{whatsapp.provider}</dd>
        {#if whatsapp.number}
          <dt>Number</dt>
          <dd>{whatsapp.number}</dd>
        {/if}
        {#if link}
          <dt>Restarts</dt>
          <dd>{link.restarts ?? '—'}</dd>
        {/if}
      </dl>

      <p class="summary">{linkSummary}</p>

      {#if whatsapp.credential_errors.length}
        <div class="banner warn">
          The active adapter reports missing configuration: {whatsapp.credential_errors.join('; ')}.
        </div>
      {/if}

      {#if whatsapp.pairing_supported && whatsapp.enabled && !whatsapp.pairing_enabled}
        <p class="caption" data-testid="pairing-disabled">
          Re-pairing from the browser is switched off here (<code
            >[whatsapp.baileys] pairing_enabled</code
          >). Pair from a terminal with
          <code>istota whatsapp pair</code>.
        </p>
      {/if}

      {#if whatsapp.pairing_blocked_reason}
        <div class="banner warn" data-testid="pairing-blocked">
          A pairing code cannot be written on this deployment: the relay would land inside a
          directory the task sandbox binds ({whatsapp.pairing_blocked_reason}). Point
          <code>[whatsapp.baileys] pairing_relay_path</code> outside it, or pair from a terminal.
        </div>
      {/if}

      {#if pairing}
        <div class="pairing" data-testid="pairing-state">
          <div class="pairing-head">
            <span class="micro-label">{pairing.state.replace(/_/g, ' ')}</span>
            {#if remaining !== null}
              <span class="caption" data-testid="pairing-remaining"
                >expires in {countdown(remaining)}</span
              >
            {/if}
          </div>
          {#if pairingSummary}
            <p class="summary">{pairingSummary}</p>
          {/if}
          {#if pairing.message}
            <p class="caption pairing-message" data-testid="pairing-message">{pairing.message}</p>
          {/if}
          {#if qrSrc}
            <img class="qr" src={qrSrc} alt="WhatsApp pairing code" data-testid="pairing-qr" />
          {/if}
          {#if inProgress}
            <div class="form-actions">
              <Button
                variant="secondary"
                onclick={cancel}
                loading={cancelling}
                loadingLabel="Cancelling…">Cancel pairing</Button
              >
            </div>
          {/if}
        </div>
      {/if}

      {#if offersDirect}
        <div class="form-actions">
          <Button
            variant="primary"
            onclick={() => start(false)}
            loading={starting}
            loadingLabel="Requesting…">Re-pair</Button
          >
        </div>
      {/if}
    </section>

    {#if offersForced}
      <!-- Deliberately outside the card and deliberately quiet. On a working
           session this is the only route to a re-pair, and it is a route with a
           typed confirmation on it rather than a button beside a green badge. -->
      <section class="danger-zone" data-testid="forced-zone">
        <p class="caption">
          Re-pairing a session that is working disconnects it and costs a reconnect. The sidecar is
          asked to stop either way, so an abort part-way has already spent that restart.
        </p>
        <Button variant="ghost" size="sm" onclick={() => (confirmOpen = true)} disabled={starting}
          >Unlink and re-pair</Button
        >
      </section>
    {/if}

    <ConfirmDialog
      bind:open={confirmOpen}
      title="Unlink and re-pair WhatsApp"
      confirmLabel="Unlink and re-pair"
      challenge={UNLINK_CHALLENGE}
      message={'This disconnects the WhatsApp session, whether or not it is working, and costs a ' +
        'real reconnect. The current credential is moved aside rather than deleted, and a new ' +
        'code has to be scanned from the phone before messages can be sent again.'}
      onConfirm={() => {
        confirmOpen = false;
        void start(true);
      }}
    />
  {:else}
    <div class="center-msg">No deployment-level connections are configured.</div>
  {/if}
</div>

<style>
  .summary {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  .pairing {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--surface-raised);
  }

  .pairing-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .pairing-message {
    margin: 0;
  }

  /* The code is drawn at whatever size the pane gives it, capped so it stays
     scannable on a phone held up to a laptop and does not fill a wide pane. */
  .qr {
    width: 100%;
    max-width: 260px;
    height: auto;
    align-self: center;
    background: var(--surface-card);
    border-radius: var(--radius-sm);
    padding: var(--space-2);
  }

  /* No border, no tint, no card. The destructive control is reachable and
     unremarkable, which is the whole point of it not being on the card. */
  .danger-zone {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-2);
    padding: 0 var(--space-1);
  }
</style>
