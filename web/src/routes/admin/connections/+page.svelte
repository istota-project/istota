<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import {
    cancelWhatsAppPairing,
    getAdminConnections,
    getWhatsAppPairing,
    startWhatsAppPairing,
    whatsAppPairingQrUrl,
    whatsAppPairingStreamUrl,
    WHATSAPP_PAIRING_TERMINAL_STATES,
    type AdminConnection,
    type AdminPairingState,
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

  /** Typed into the destructive confirmation. Short, and not a word anyone
   *  types by reflex — the point is that the phrase cannot arrive by accident. */
  const UNLINK_CHALLENGE = 'unlink';

  /** The log tail's own ladder, and its cap. A stream that cannot be reopened
   *  says so rather than retrying for the life of the tab. */
  const MAX_STREAM_RETRIES = 5;
  const STREAM_RETRY_CEILING_MS = 15_000;

  /** One retry per code, far enough out to clear a rotation mid-flight. */
  const QR_RETRY_DELAY_MS = 500;

  let connections = $state<AdminConnection[] | null>(null);
  let pairingRow = $state<AdminPairingState | null>(null);
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
  let qrFetchFailed = $state(false);
  let qrNonce = $state(0);
  let stream: EventSource | null = null;
  let streamRetries = 0;
  let streamRetryTimer: ReturnType<typeof setTimeout> | null = null;
  let qrRetried = false;
  let qrRetryTimer: ReturnType<typeof setTimeout> | null = null;
  /** The rendered pairing state the current `notice` was issued against. A
   *  plain `let` so the effect that reads it is not re-run by writing it. */
  let noticeState = '';

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
        message: frame.message,
        qrSeq: frame.qr_seq,
        qrAvailable: frame.qr_available,
        expiresAt: frame.expires_at,
      };
    }
    const row = pairingRow;
    if (!row || row.state === null) return null;
    return {
      state: row.state,
      message: row.message ?? '',
      qrSeq: row.qr_seq,
      qrAvailable: row.qr_available,
      expiresAt: row.expires_at_epoch,
    };
  });

  /**
   * Whether a window is in flight — **the row's answer, never the rendered
   * one**, and that distinction is the whole of this derived.
   *
   * `pairing.state` is the *relay's* state whenever a window is publishing,
   * and the relay reaches `paired` / `expired` / `failed` a poll before the
   * row mirrors it. The server's start guard reads the row, so gating on the
   * rendered state renders both start controls — the destructive one included
   * — beside "Paired. The session is linked again.", against a route that
   * would answer 409. So `terminal` is read off the payload, computed from
   * the same column the guard reads.
   *
   * The frame is the fallback only where no row has been read yet: another
   * admin's window opening against a card whose last index read found none.
   */
  let inProgress = $derived.by(() => {
    const rowOpen = pairingRow !== null && pairingRow.state !== null && !pairingRow.terminal;
    const frameOpen = frame?.state != null && !WHATSAPP_PAIRING_TERMINAL_STATES.has(frame.state);
    // **Either, and not the row first.** An early return on the row shadows a
    // live frame behind a *closed* row — the resting state after any completed
    // pairing — so another admin's window, or the CLI's attach mode writing the
    // same row while this pane sits open, rendered a code mid-scan with no
    // Cancel (that is behind `inProgress`) and Re-pair beside it. Nothing heals
    // it either: only a terminal frame triggers a re-read. A non-terminal frame
    // is itself proof the server's row is open, since the reader consults the
    // relay only past its own terminal veto, so it is newer evidence than this
    // copy of the row — while the row staying authoritative when *it* is the
    // open one is what keeps the relay from reporting a close the row has not
    // reached.
    return rowOpen || frameOpen;
  });

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

  /**
   * The session has said, itself, that it is finished — the only state in
   * which a re-pair destroys nothing.
   *
   * **Everything else takes the confirmation, including a link this process
   * cannot read**, and that asymmetry is the whole of the control's gating.
   * The two are derived from one predicate so they are mutually exclusive by
   * construction: an operator reported seeing both at once, and they were
   * right — the old pair of conditions were independently true wherever
   * `link` was null, which is every page load on the split deployment.
   */
  let knownDead = $derived(link !== null && link.fatal_is_permanent);

  /** One click, no phrase. The fault is latched, so there is nothing to lose
   *  and nothing to warn about — and this is the case the whole flow exists
   *  for, so it must be the shortest route through the pane. */
  let offersDirect = $derived(pairingOffered && !inProgress && knownDead);

  /**
   * The confirmed unlink, for every other state.
   *
   * **It deliberately does not depend on having watched a refusal**, which is
   * what the first attempt at this did: it offered the one-click control where
   * `link` was unreadable and revealed the confirmed one once the bridge
   * answered `session_live`. That dead-ends twice over. The gate lived in
   * client state, so a reload after the refusal lost it and left the pane with
   * a refusal message and no way to act on it — and the refusal reason reaches
   * the client as prose (`_write_pairing_outcome` carries it that way on
   * purpose, since writing `session_live` into the state would have the poll's
   * orphan arm fire on a row that never opened a window), so there was nothing
   * durable to key on either.
   *
   * So an unreadable link takes the phrase. On the split deployment that is
   * every re-pair, which costs one typed word on a rare recovery action and
   * buys a pane that cannot strand the operator. The confirmation is honest
   * there rather than merely cautious: from this process the session may well
   * be working, and nothing here can tell.
   */
  let offersForced = $derived(pairingOffered && !inProgress && !knownDead);

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
      if (link.connection_replaced_latched && !link.ready) {
        return { variant: 'danger', label: 'in use elsewhere' };
      }
      if (link.fatal_is_permanent && link.fatal_reason === 'credential_unreadable') {
        return { variant: 'danger', label: 'credential unreadable' };
      }
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
    if (link.connection_replaced_latched && !link.ready) {
      // ISSUE-553. The device is still linked; somebody else holds the
      // credential. Given up, the only way back is a re-pair, and the phone
      // has to drop the old device first or the copy keeps working.
      if (link.fatal_is_permanent) {
        return (
          'Another client kept replacing this session’s connection and was still there on every ' +
          'retry, so the sidecar has stopped trying. Re-pair below. If you do not know what the ' +
          'other client is, first remove the old device under Linked Devices in WhatsApp on the ' +
          'phone: a re-pair does not revoke the copied credential.'
        );
      }
      return (
        'Another client is using this WhatsApp session, so the sidecar has stopped reconnecting ' +
        'and sends are refused. It retries after 15 minutes and then after an hour twice more; ' +
        'stop the other client and the next retry brings the session back.'
      );
    }
    if (link.fatal_is_permanent && link.fatal_reason === 'credential_unreadable') {
      // ISSUE-552. The device is still linked; the local file cannot be read.
      return (
        'The saved credential cannot be read, so nothing is opened and sends are refused. ' +
        'Check the owner and mode of creds.json and restart the sidecar first. Re-pair below ' +
        'only if the file itself is empty or corrupt and creds.json.bak is not usable.'
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

  /** A primitive `$derived`, so the effect below re-runs on a *rotation* and
   *  not on every frame — `pairing` is a fresh object each time. */
  let qrSeq = $derived(pairing?.qrSeq ?? 0);

  /** The code reaches the page only as this endpoint's bytes. `qr_seq` is what
   *  makes the browser refetch on a rotation and hold the image still between
   *  them; nothing here holds the payload. `qrNonce` is the one retry below. */
  let qrSrc = $derived.by(() => {
    if (pairing === null || pairing.state !== 'awaiting_scan' || !pairing.qrAvailable) return null;
    const url = whatsAppPairingQrUrl(pairing.qrSeq);
    return qrNonce === 0 ? url : `${url}&retry=${qrNonce}`;
  });

  let remaining = $derived.by(() => {
    if (!inProgress || !pairing?.expiresAt) return null;
    // Seconds, not rounded milliseconds — the misplaced paren rounded the
    // product and then divided, so the round did nothing and a 90s window
    // read "1m 29s" the instant it opened. `countdown` floors.
    return Math.max(0, (pairing.expiresAt * 1000 - now) / 1000);
  });

  function countdown(seconds: number): string {
    const whole = Math.floor(seconds);
    if (whole < 60) return `${whole}s`;
    return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, '0')}s`;
  }

  // A clock only while something is counting down, and cleaned up by the effect
  // itself — a leaked interval outlives the page and the test run alike. `now`
  // is re-seeded here rather than trusted from construction: seeded once at
  // mount it is stale by however long the pane has been open, and the first
  // interval tick is a second away, so a window opened on a page left sitting
  // rendered its whole page-open age on top of the real remaining time.
  $effect(() => {
    if (!inProgress) return;
    now = Date.now();
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  // A new code invalidates whatever the last one's fetch did.
  $effect(() => {
    void qrSeq;
    qrFetchFailed = false;
    qrNonce = 0;
    qrRetried = false;
    return clearQrRetry;
  });

  // A destructive dialog may not outlive the state that offered it. Without
  // this, one left open stays confirmable through any later change — another
  // admin's window opening, a link latching a permanent fault — and `onConfirm`
  // sends `force` unconditionally. It is also what stops the dialog reappearing
  // already open if the card unmounts and comes back with `confirmOpen` true.
  $effect(() => {
    if (!offersForced) confirmOpen = false;
  });

  // The notice is a receipt for an action, so it goes the moment the pairing
  // state moves past the one it was issued against — otherwise "Pairing
  // requested…" sits above a `failed` message contradicting it for minutes.
  $effect(() => {
    const state = pairing?.state ?? '';
    if (state !== noticeState) {
      noticeState = state;
      notice = '';
    }
  });

  $effect(() => {
    if (streamAvailable) startStream();
    else stopStream();
  });

  function setNotice(text: string) {
    notice = text;
    noticeState = pairing?.state ?? '';
  }

  function clearQrRetry() {
    if (qrRetryTimer !== null) {
      clearTimeout(qrRetryTimer);
      qrRetryTimer = null;
    }
  }

  /**
   * The image could not be fetched. Retry once, then say so in words.
   *
   * The server documents 404 for a stale window, a terminal row, a missing
   * relay file and a window not yet at `awaiting_scan`, and the gate here is
   * `qr_available` off a relay read up to a poll old — so the window can
   * rotate or close between the frame that announced a sequence and the
   * browser's request for it. `src` is a pure function of that sequence, so
   * nothing re-issues a failed fetch on its own; the nonce is what makes the
   * second request a new URL rather than a cache read. This is the operator's
   * only route to the code, so the fallback is a sentence rather than a broken
   * image with no text.
   */
  function onQrError() {
    if (!qrRetried) {
      qrRetried = true;
      clearQrRetry();
      qrRetryTimer = setTimeout(() => {
        qrRetryTimer = null;
        qrNonce = Date.now();
      }, QR_RETRY_DELAY_MS);
      return;
    }
    qrFetchFailed = true;
  }

  function startStream() {
    if (stream || typeof EventSource === 'undefined') return;
    const es = new EventSource(whatsAppPairingStreamUrl(), { withCredentials: true });
    stream = es;
    es.addEventListener('pairing', (ev) => {
      let raw: unknown;
      try {
        raw = JSON.parse((ev as MessageEvent).data);
      } catch {
        return;
      }
      // Projected to the five declared fields rather than stored whole, so
      // nothing the server sends that this page did not ask for reaches
      // component state. It is also the type guard the template needs: a
      // `JSON.parse` of a scalar survives a `!== null` test and then throws in
      // `pairing.state.replace(...)`.
      const next = projectFrame(raw);
      if (next === null) return;
      const was = frame?.state ?? null;
      frame = next;
      streamError = '';
      streamRetries = 0;
      // A close changes what the *link* half of the card says — green and a
      // number after a scan — and that comes off the index, not the stream.
      // Conditioned on our row copy still being open rather than on the state
      // having changed: the relay reaches a terminal state a poll before the
      // row does, and both of those arrive as frames with the same `state`.
      if (
        next.state !== null &&
        WHATSAPP_PAIRING_TERMINAL_STATES.has(next.state) &&
        !(pairingRow?.terminal ?? false)
      ) {
        void load({ quiet: true });
      }
    });
    es.addEventListener('stream_error', () => {
      // The server could not read the relay and has ended the stream itself.
      // Not retried: nothing about a second connection reads a different file.
      stopStream();
      streamError = 'The live pairing state could not be read. Reload to resume it.';
    });
    es.addEventListener('error', () => {
      // A dropped transport is retried by the browser on its own. A non-200,
      // a wrong content type or an expired session are not: EventSource goes
      // to CLOSED and fires this without ever reconnecting — reachable here as
      // a 404 the moment `pairing_enabled` flips or the provider changes, a
      // 401 on session expiry, and any 5xx or proxy error. With no listener
      // that is silent *and* unrecoverable, since `stream` stays non-null and
      // `startStream` refuses to reopen. So reopen on the log tail's own
      // capped ladder, and say which of the two states the pane is in.
      es.close();
      if (stream !== es) return;
      stream = null;
      frame = null;
      streamRetries += 1;
      if (streamRetries > MAX_STREAM_RETRIES) {
        streamError =
          'The live pairing state is not reachable. The record below is the durable one; ' +
          'reload the page to try the live stream again.';
        return;
      }
      streamError = 'Reconnecting to the live pairing state…';
      streamRetryTimer = setTimeout(
        () => {
          streamRetryTimer = null;
          startStream();
        },
        Math.min(1000 * 2 ** (streamRetries - 1), STREAM_RETRY_CEILING_MS),
      );
    });
  }

  function projectFrame(raw: unknown): PairingFrame | null {
    if (raw === null || typeof raw !== 'object') return null;
    const f = raw as Record<string, unknown>;
    return {
      state: typeof f.state === 'string' ? f.state : null,
      qr_seq: typeof f.qr_seq === 'number' && Number.isFinite(f.qr_seq) ? f.qr_seq : 0,
      qr_available: f.qr_available === true,
      expires_at:
        typeof f.expires_at === 'number' && Number.isFinite(f.expires_at) ? f.expires_at : null,
      message: typeof f.message === 'string' ? f.message : '',
    };
  }

  /** Drop the live stream **and the frame it left behind**. A stale frame is
   *  not merely out of date: `pairing` prefers it whenever it exists, so a
   *  non-terminal one freezes the pane for the life of the page — both start
   *  controls hidden, the countdown pinned at zero, `qrSrc` on a retired
   *  sequence, and every later row read correct and ignored. */
  function stopStream() {
    if (streamRetryTimer !== null) {
      clearTimeout(streamRetryTimer);
      streamRetryTimer = null;
    }
    streamRetries = 0;
    stream?.close();
    stream = null;
    frame = null;
  }

  async function load(opts: { quiet?: boolean } = {}) {
    if (!opts.quiet) loading = true;
    try {
      const payload = await getAdminConnections();
      // Read off the payload rather than through the derived, so the row and
      // the link can never be one render apart.
      const found = payload.connections.find((c) => c.id === 'whatsapp') ?? null;
      connections = payload.connections;
      pairingRow = found?.pairing ?? null;
      error = '';
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Failed to load connections';
      if (connections) actionError = message;
      else error = message;
    } finally {
      loading = false;
    }
  }

  /**
   * Re-read the pairing row alone.
   *
   * Writing or cancelling a request changes the row and nothing about the
   * link, so this asks the endpoint that answers for the row rather than
   * re-reading the whole index. It falls back to the index where that endpoint
   * refuses — a 404 or a 409 there means this browser's copy of the
   * deployment's configuration is stale, which is the one case in which the
   * link may have moved too.
   */
  async function refreshPairing() {
    try {
      pairingRow = (await getWhatsAppPairing()).pairing;
    } catch {
      await load({ quiet: true });
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
    // Re-entrancy is the function's own property rather than something each of
    // its three call sites has to remember.
    if (starting) return;
    starting = true;
    notice = '';
    actionError = '';
    let receipt = '';
    try {
      // Both flags on every call, and neither inferred. `force` is the
      // operator's acceptance of a disconnect; the server refuses it without
      // its companion rather than filling one in.
      await startWhatsAppPairing({ force, confirmDisconnect: force });
      receipt = force
        ? 'Re-pair requested. The sidecar is being asked to stop; a code follows once it restarts.'
        : 'Pairing requested. The scheduler picks it up within one poll interval.';
    } catch (e) {
      actionError = startFailure(e);
    } finally {
      starting = false;
      await refreshPairing();
      // After the read, so the receipt is tagged with the state it describes
      // rather than with the one it superseded.
      if (receipt) setNotice(receipt);
    }
  }

  async function cancel() {
    if (cancelling) return;
    cancelling = true;
    notice = '';
    actionError = '';
    let receipt = '';
    try {
      const outcome = await cancelWhatsAppPairing();
      if (outcome.cancelled) {
        receipt =
          'Pairing cancelled. Any session directory already moved aside is left where it is — ' +
          'the message below names it.';
      } else if (outcome.reason === 'servicing') {
        actionError =
          'The re-pair is already running and cannot be stopped part-way. It will report its ' +
          'outcome here.';
      } else {
        receipt = 'There was no open pairing request to cancel.';
      }
    } catch (e) {
      actionError = e instanceof Error ? e.message : 'Failed to cancel pairing';
    } finally {
      cancelling = false;
      await refreshPairing();
      if (receipt) setNotice(receipt);
    }
  }

  onMount(() => load());
  onDestroy(() => {
    clearQrRetry();
    stopStream();
  });
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
          {#if qrSrc && !qrFetchFailed}
            <img
              class="qr"
              src={qrSrc}
              alt="WhatsApp pairing code"
              data-testid="pairing-qr"
              onerror={onQrError}
            />
          {:else if qrSrc}
            <!-- The image is the operator's only route to the code, so a
                 failed fetch says so in words rather than leaving a broken
                 image with no text. One retry has already been spent. -->
            <p class="caption" data-testid="pairing-qr-error">
              The pairing code could not be fetched. It is redrawn about every twenty seconds, so
              the next rotation should bring it back.
            </p>
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

      {#if offersForced}
        <!-- One control, in the card. The confirmation is what carries the
             weight here rather than the button's placement: an operator
             reported the separate section and its paragraph as redundant, and
             on the shape where `link` is unreadable they were — both controls
             rendered at once. This is not a one-click way out of a working
             session; it opens the typed confirmation. -->
        <div class="form-actions" data-testid="forced-zone">
          <Button variant="primary" onclick={() => (confirmOpen = true)} disabled={starting}
            >Unlink and re-pair</Button
          >
          <ConfirmDialog
            bind:open={confirmOpen}
            title="Unlink and re-pair WhatsApp"
            confirmLabel="Unlink and re-pair"
            challenge={UNLINK_CHALLENGE}
            message={'This disconnects the WhatsApp session, whether or not it is working, and ' +
              'costs a real reconnect. The current credential is moved aside rather than deleted, ' +
              'and a new code has to be scanned from the phone before messages can be sent again.'}
            onConfirm={() => {
              confirmOpen = false;
              void start(true);
            }}
          />
        </div>
      {:else if offersDirect}
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
  {:else}
    <!-- Reached when the payload carries no `whatsapp` entry, which is not the
         same as carrying no connections: the index returns exactly one member
         today, so the card reads it by id rather than iterating. -->
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
</style>
