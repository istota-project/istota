import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { tick } from 'svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor, fireEvent, within } from '@testing-library/svelte';

import type { AdminConnection, AdminConnectionLink, AdminPairingState } from '$lib/api';

/**
 * The WhatsApp card on `/admin/connections`.
 *
 * The card's job is to offer the right control for the state the session is
 * in, and the two it can offer are not interchangeable — one is a click and
 * the other is a typed phrase. So the assertions here are mostly about which
 * of them is reachable, per state, including the states nobody can produce on
 * demand: a working session, a latched permanent fault, a session this process
 * cannot see, and a deployment whose operator switched the whole flow off.
 *
 * Three properties are worth more than the rendering:
 *
 * * **A working session offers no re-pair on the card at all.** Not a disabled
 *   button and not a prominent one behind a dialog — nothing to press. That is
 *   asserted as a negative DOM query for the one-click control's own label,
 *   because the failure it guards against is a control appearing there rather
 *   than one being mislabelled.
 * * **A latched permanent fault is one click and no challenge.** This is the
 *   case the whole flow exists for, so it has to be the shortest route through
 *   the UI; a challenge appearing there would be the fix for the state above
 *   applied to the state it was not about.
 * * **The code is never a string.** The stream frame carries `qr_seq` and not a
 *   payload, and the card refetches the server-rendered SVG on a bump and holds
 *   the image still between them. The control for that is a frame carrying a
 *   `qr` field the server does not send: nothing of it may reach the document.
 */

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

// The two URL builders are the real ones behind a spy rather than canned
// returns: the `seq=` spelling is what the refetch assertion turns on, and a
// stub would move that spelling into this file where nothing checks it.
const actual = await vi.importActual<typeof import('$lib/api')>('$lib/api');
await fillApiDouble(api, {
  whatsAppPairingQrUrl: vi.fn(actual.whatsAppPairingQrUrl),
  whatsAppPairingStreamUrl: vi.fn(actual.whatsAppPairingStreamUrl),
});

import {
  cancelWhatsAppPairing,
  getAdminConnections,
  getWhatsAppPairing,
  startWhatsAppPairing,
  whatsAppPairingStreamUrl,
} from '$lib/api';
import Page from './+page.svelte';

/** The one-click control's label, and the typed one's. Spelled once. */
const REPAIR = 'Re-pair';
const UNLINK = 'Unlink and re-pair';

/** A stand-in for the SSE connection — jsdom has none — that also counts how
 *  many were opened, which is how the switched-off states are asserted. */
interface FakeStream {
  url: string;
  closed: boolean;
  emit: (kind: string, payload: unknown) => void;
  /** An `error` event — a non-200, a wrong content type or an expired session,
   *  which EventSource reports here and never reconnects from. */
  fail: () => void;
}

let streams: FakeStream[] = [];

function installFakeEventSource() {
  streams = [];
  class Fake {
    url: string;
    listeners = new Map<string, ((e: unknown) => void)[]>();
    closed = false;
    constructor(url: string) {
      this.url = url;
      streams.push(this as unknown as FakeStream);
    }
    addEventListener(kind: string, fn: (e: unknown) => void) {
      const cur = this.listeners.get(kind) ?? [];
      cur.push(fn);
      this.listeners.set(kind, cur);
    }
    removeEventListener() {}
    close() {
      this.closed = true;
    }
    emit(kind: string, payload: unknown) {
      for (const fn of this.listeners.get(kind) ?? []) fn({ data: JSON.stringify(payload) });
    }
    fail() {
      for (const fn of this.listeners.get('error') ?? []) fn({});
    }
  }
  (globalThis as unknown as { EventSource: unknown }).EventSource = Fake;
}

function linkState(over: Partial<AdminConnectionLink> = {}): AdminConnectionLink {
  return {
    listening: true,
    connected: true,
    ready: false,
    fatal_reason: null,
    fatal_is_permanent: false,
    restarts: 0,
    ...over,
  };
}

function connection(over: Partial<AdminConnection> = {}): AdminConnection {
  return {
    id: 'whatsapp',
    label: 'WhatsApp',
    enabled: true,
    provider: 'baileys',
    number: '+15551230000',
    pairing_supported: true,
    pairing_enabled: true,
    restart_interval_seconds: 30,
    credential_errors: [],
    link: null,
    pairing: null,
    pairing_blocked_reason: null,
    ...over,
  };
}

function pairingState(over: Partial<AdminPairingState> = {}): AdminPairingState {
  return {
    window_id: 'w1',
    state: 'requested',
    row_state: 'requested',
    terminal: false,
    requested_by: 'alice',
    requested_at: '2026-09-17 12:00:00',
    expires_at: '2026-09-17 12:05:00',
    expires_at_epoch: null,
    message: '',
    force: false,
    qr_seq: 0,
    qr_available: false,
    ...over,
  };
}

/** The five fields the stream sends. `extra` is how a control puts something on
 *  the frame the server never sends. */
function frame(
  state: string,
  over: { qr_seq?: number; qr_available?: boolean; message?: string } & Record<
    string,
    unknown
  > = {},
) {
  return {
    state,
    qr_seq: 0,
    qr_available: false,
    expires_at: null,
    message: '',
    ...over,
  };
}

async function mount(over: Partial<AdminConnection> = {}) {
  api.getAdminConnections.mockResolvedValue({ connections: [connection(over)] });
  render(Page);
  await screen.findByTestId('whatsapp-card');
}

/** The same mount under fake timers, where `findBy*` cannot be awaited: the
 *  library's polling wait needs a clock this test is holding still. One zero
 *  advance flushes the load's microtasks, and `tick()` flushes Svelte. */
async function mountFake(over: Partial<AdminConnection> = {}) {
  api.getAdminConnections.mockResolvedValue({ connections: [connection(over)] });
  render(Page);
  await vi.advanceTimersByTimeAsync(0);
  await tick();
}

beforeEach(() => {
  installFakeEventSource();
  api.getAdminConnections.mockReset();
  api.startWhatsAppPairing.mockReset();
  api.startWhatsAppPairing.mockResolvedValue({
    window_id: 'w1',
    state: 'requested',
    force: false,
  });
  api.cancelWhatsAppPairing.mockReset();
  api.cancelWhatsAppPairing.mockResolvedValue({ cancelled: true, reason: '' });
  api.getWhatsAppPairing.mockReset();
  api.getWhatsAppPairing.mockResolvedValue({ pairing: null });
});

afterEach(cleanup);

describe('the WhatsApp card — which control each state offers', () => {
  it('offers one click and no typed phrase on a latched permanent fault', async () => {
    await mount({
      link: linkState({ ready: false, fatal_is_permanent: true, fatal_reason: 'logged_out' }),
    });

    expect(screen.getByRole('button', { name: REPAIR })).toBeInTheDocument();
    // The whole point of this state: nothing else stands between the operator
    // and the re-pair. No second control, and no challenge anywhere.
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();
    expect(screen.queryByTestId('forced-zone')).toBeNull();
    expect(screen.queryByLabelText('Type to confirm')).toBeNull();
    expect(screen.getByText(/will not come back without a re-pair/)).toBeInTheDocument();
  });

  it('offers one control on a working session, and it is the confirmed one', async () => {
    await mount({ link: linkState({ ready: true }) });

    // A green session still has no *one-click* way out of itself — the only
    // control opens the typed confirmation. It now lives in the card rather
    // than in a section below it: the separate placement existed to keep a
    // destructive button away from the status badge, and an operator reported
    // the section and its paragraph as redundant once the two controls were
    // made mutually exclusive. What carries the weight is the challenge
    // phrase, which `ConfirmDialog` enforces, not the position on the page.
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.getByText('The session is linked and working.')).toBeInTheDocument();
    const zone = screen.getByTestId('forced-zone');
    expect(screen.getByTestId('whatsapp-card').contains(zone)).toBe(true);
    // Exactly one control, so the redundancy cannot come back unnoticed.
    expect(screen.getAllByRole('button', { name: UNLINK })).toHaveLength(1);
  });

  it('offers only the typed control on an unsettled session', async () => {
    await mount({ link: linkState({ ready: false, connected: true }) });

    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.getByRole('button', { name: UNLINK })).toBeInTheDocument();
    expect(screen.getByText(/may be mid-reconnect/)).toBeInTheDocument();
  });

  it('offers the one-click start alone on the split deployment, not both', async () => {
    // `link: null` is not an edge case — it is every page load on the canonical
    // Ansible deployment, where the bridge lives in the scheduler unit. Both
    // controls used to render here, with a danger-zone paragraph explaining a
    // trade the operator had not yet made; an operator reported the second one
    // as redundant and was right. The one-click start is safe because the
    // bridge refuses an unforced re-pair of a session with no permanent fault
    // and records that on the row, which is what reveals the forced route.
    await mount({ link: null });

    expect(screen.getByRole('button', { name: REPAIR })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();
    expect(screen.queryByTestId('forced-zone')).toBeNull();
    expect(screen.getByText(/cannot read the live link/)).toBeInTheDocument();
  });

  it('reveals the forced route once its own unforced start is refused', async () => {
    // The progressive disclosure that keeps the forced path reachable where
    // `link` is unreadable. Asserted end to end rather than by poking the
    // derived: press the one-click control, have the row come back terminal
    // without `force` under the id the POST handed us — which is what the
    // bridge's `session_live` refusal leaves, since a refusal opens no window
    // and so never adopts the bridge's own window id.
    await mount({ link: null });

    api.startWhatsAppPairing.mockResolvedValue({
      window_id: 'req-1',
      state: 'requested',
      force: false,
    });
    // `refreshPairing` reads the row through `getWhatsAppPairing`, not the
    // index — mocking the index here leaves the row untouched and the gate
    // false, which is how the first draft of this test failed.
    api.getWhatsAppPairing.mockResolvedValue({
      pairing: {
        window_id: 'req-1',
        state: 'failed',
        row_state: 'failed',
        terminal: true,
        requested_by: 'operator',
        requested_at: null,
        expires_at: null,
        expires_at_epoch: null,
        message: 'the WhatsApp session has reported no permanent fault',
        force: false,
        qr_seq: 0,
        qr_available: false,
      },
    });

    await fireEvent.click(screen.getByRole('button', { name: REPAIR }));

    await waitFor(() => expect(screen.getByTestId('forced-zone')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: UNLINK })).toBeInTheDocument();
    // Mutually exclusive, which is the property the report was about.
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
  });

  it('offers no control and opens no stream on the Cloud adapter', async () => {
    await mount({ provider: 'whatsapp_cloud', pairing_supported: false, pairing_enabled: false });

    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();
    expect(screen.getByText(/Cloud API adapter/)).toBeInTheDocument();
    // Synchronous rather than awaited, and that is settled by measurement
    // rather than by reading: `mount` awaits `findByTestId`, which is the same
    // settle point the positive case reaches, so the effect has already run.
    // Driven — `startStream()` made unconditional turns this and its sibling
    // below red, and nothing else.
    expect(streams).toHaveLength(0);
  });

  it('says so and opens no stream when pairing is switched off', async () => {
    await mount({ pairing_enabled: false, link: linkState({ fatal_is_permanent: true }) });

    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();
    expect(screen.getByTestId('pairing-disabled')).toBeInTheDocument();
    // The five pairing routes 404 there, so a stream would be a retry loop
    // against a deliberate refusal. The index stays 200, which is why the card
    // still renders the link state above.
    expect(streams).toHaveLength(0);
  });

  it('withholds both controls where the relay would land in a sandbox bind', async () => {
    await mount({
      pairing_blocked_reason: 'workspace',
      link: linkState({ fatal_is_permanent: true }),
    });

    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();
    expect(screen.getByTestId('pairing-blocked')).toBeInTheDocument();
  });

  it('opens exactly one stream where the pairing routes are reachable', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });

    await waitFor(() => expect(streams).toHaveLength(1));
    expect(streams[0].url).toBe(whatsAppPairingStreamUrl());
  });

  it('closes the stream when the pane goes away', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));

    cleanup();

    // Nothing server-side ends a polling stream, so one left open on a
    // navigated-away pane keeps a database read per second going for the life
    // of the tab.
    expect(streams[0].closed).toBe(true);
  });
});

describe('the WhatsApp card — starting a pairing', () => {
  it('sends both flags false on the one-click start', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });

    await fireEvent.click(screen.getByRole('button', { name: REPAIR }));

    await waitFor(() => expect(startWhatsAppPairing).toHaveBeenCalled());
    // Never `{}` and never a single flag: the server refuses `force` without
    // its companion, and neither is ever defaulted on either side.
    expect(startWhatsAppPairing).toHaveBeenCalledWith({ force: false, confirmDisconnect: false });
  });

  it('requires the typed phrase before the forced start can be confirmed', async () => {
    await mount({ link: linkState({ ready: true }) });

    await fireEvent.click(screen.getByRole('button', { name: UNLINK }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/disconnects the WhatsApp session/);

    // Scoped to the dialog: the trigger outside it restates the same action, so
    // an unscoped query would find two buttons and could find the wrong one.
    const confirmButton = () => within(dialog).getByRole('button', { name: UNLINK });
    expect(confirmButton()).toBeDisabled();
    expect(startWhatsAppPairing).not.toHaveBeenCalled();

    const challenge = within(dialog).getByLabelText('Type to confirm');
    await fireEvent.input(challenge, { target: { value: 'unlink' } });
    expect(confirmButton()).not.toBeDisabled();

    await fireEvent.click(confirmButton());
    await waitFor(() => expect(startWhatsAppPairing).toHaveBeenCalled());
    expect(startWhatsAppPairing).toHaveBeenCalledWith({ force: true, confirmDisconnect: true });
  });

  it('closes the confirmation when the state stops offering it', async () => {
    await mount({ link: linkState({ ready: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));
    await fireEvent.click(screen.getByRole('button', { name: UNLINK }));
    await screen.findByRole('dialog');

    // Another admin's window opening, or a link latching a fault, withdraws
    // the control — and a dialog left mounted past that stays confirmable,
    // with `onConfirm` sending `force` whatever the card is now showing.
    streams[0].emit('pairing', frame('awaiting_sidecar'));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();
    expect(startWhatsAppPairing).not.toHaveBeenCalled();

    // **And it does not come back open**, which is the half the `{#if}` alone
    // does not give: `confirmOpen` is page state, so a dialog that unmounted
    // while it was true reappears already confirmable the moment the control
    // returns. Measured — with only the `{#if}`, the assertion above stays
    // green and this one goes red.
    streams[0].emit('pairing', frame('expired', { message: 'the window expired.' }));

    expect(await screen.findByRole('button', { name: UNLINK })).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('re-reads the pairing row after a refusal and names it', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    api.startWhatsAppPairing.mockRejectedValue(new Error('API error: 409'));
    api.getWhatsAppPairing.mockClear();

    await fireEvent.click(screen.getByRole('button', { name: REPAIR }));

    await waitFor(() => expect(screen.getByText(/Refused:/)).toBeInTheDocument());
    // The row is the authority on what happened and the link cannot have moved
    // as a result of writing a request, so the card re-reads the row alone
    // rather than the whole index.
    expect(getWhatsAppPairing).toHaveBeenCalled();
  });

  it('ignores a second click while the first start is in flight', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    let release = () => {};
    api.startWhatsAppPairing.mockImplementation(
      () => new Promise((resolve) => (release = () => resolve({ window_id: 'w1' }))),
    );

    const button = screen.getByRole('button', { name: REPAIR });
    await fireEvent.click(button);
    await fireEvent.click(button);
    release();

    await waitFor(() => expect(startWhatsAppPairing).toHaveBeenCalledTimes(1));
  });
});

describe('the WhatsApp card — a window in flight', () => {
  it('names the declared restart interval while waiting for the sidecar', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }), restart_interval_seconds: 30 });
    await waitFor(() => expect(streams).toHaveLength(1));

    streams[0].emit('pairing', frame('awaiting_sidecar'));

    expect(await screen.findByText(/declares a 30s restart interval/)).toBeInTheDocument();
    // No start control while one is running, and a way to stop waiting.
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.getByRole('button', { name: 'Cancel pairing' })).toBeInTheDocument();
  });

  it("renders the server's own message verbatim when the sidecar never returns", async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));

    // The wording names the unit and the update log and is the server's to
    // write; the card must not compose its own version of it.
    const message = 'no sidecar connected. Check istota-whatsapp-baileys and the update log.';
    streams[0].emit('pairing', frame('sidecar_absent', { message }));

    expect(await screen.findByTestId('pairing-message')).toHaveTextContent(message);
  });

  it('cancels an open window and reports what the server did', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));
    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 1, qr_available: true }));
    await screen.findByTestId('pairing-qr');

    await fireEvent.click(screen.getByRole('button', { name: 'Cancel pairing' }));

    await waitFor(() => expect(cancelWhatsAppPairing).toHaveBeenCalled());
    expect(await screen.findByText(/Pairing cancelled/)).toBeInTheDocument();
  });

  it('offers the controls again once the window closes', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));
    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 1, qr_available: true }));
    await screen.findByTestId('pairing-qr');
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();

    streams[0].emit('pairing', frame('expired', { message: 'the pairing window expired.' }));

    // A terminal frame also changes what the *link* half of the card says, so
    // the index is re-read rather than inferred from the stream.
    await waitFor(() => expect(screen.queryByTestId('pairing-qr')).toBeNull());
    expect(await screen.findByRole('button', { name: REPAIR })).toBeInTheDocument();
    expect(getAdminConnections).toHaveBeenCalledTimes(2);
  });
});

describe('the WhatsApp card — the row is what gates the controls', () => {
  it('withholds both starts while the row is open, though the relay says paired', async () => {
    const open = pairingState({ state: 'awaiting_scan', row_state: 'awaiting_scan' });
    await mount({ link: linkState({ fatal_is_permanent: true }), pairing: open });
    await waitFor(() => expect(streams).toHaveLength(1));

    // The happy path, one poll wide: the relay reaches `paired` before the row
    // mirrors it, so the rendered state is terminal while the server's own
    // start guard — which reads the row — would still answer 409. A card
    // gating on the rendered state puts both controls, the destructive one
    // included, directly beside "Paired".
    api.getAdminConnections.mockResolvedValue({
      connections: [connection({ link: linkState({ ready: true }), pairing: open })],
    });
    streams[0].emit('pairing', frame('paired'));

    expect(await screen.findByText('Paired. The session is linked again.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();

    // And they come back once the row itself is closed, which is the read the
    // terminal frame triggered.
    api.getAdminConnections.mockResolvedValue({
      connections: [
        connection({
          link: linkState({ ready: true }),
          pairing: pairingState({ state: 'paired', row_state: 'paired', terminal: true }),
        }),
      ],
    });
    streams[0].emit('pairing', frame('paired', { expires_at: 1 }));

    expect(await screen.findByRole('button', { name: UNLINK })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
  });

  it('treats a live frame as open even behind a closed row', async () => {
    // A terminal row is the resting state after any completed pairing, and a
    // window can open without this pane having asked for it: another admin, or
    // the CLI's attach mode writing the same row. Only a *terminal* frame
    // triggers a re-read, so a stale closed row is not self-healing — and with
    // the row consulted first it shadowed the frame entirely, rendering a code
    // mid-scan with no Cancel and both start controls beside it.
    await mount({
      link: linkState({ fatal_is_permanent: true }),
      pairing: pairingState({ state: 'paired', row_state: 'paired', terminal: true }),
    });
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(screen.getByRole('button', { name: REPAIR })).toBeInTheDocument();

    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 4, qr_available: true }));

    await screen.findByTestId('pairing-qr');
    expect(screen.getByRole('button', { name: 'Cancel pairing' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();
  });

  it('renders the durable row when no stream frame has arrived', async () => {
    const message = 'no sidecar connected. Check the unit and the update log.';
    await mount({
      link: linkState({ fatal_is_permanent: true }),
      pairing: pairingState({
        state: 'sidecar_absent',
        row_state: 'sidecar_absent',
        message,
        expires_at_epoch: Date.now() / 1000 + 120,
      }),
    });

    // The row is what survives a browser reload, and on a `pairing_enabled`
    // deployment it is also what a CLI-initiated pairing leaves behind.
    expect(screen.getByTestId('pairing-state')).toHaveTextContent('sidecar absent');
    expect(screen.getByTestId('pairing-message')).toHaveTextContent(message);
    expect(screen.getByTestId('pairing-remaining')).toHaveTextContent(/expires in \dm/);
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
  });

  it('drops a stale frame when the stream stops, rather than freezing the pane', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));
    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 1, qr_available: true }));
    await screen.findByTestId('pairing-qr');
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();

    streams[0].emit('stream_error', { error: 'pairing read failed' });

    // A non-terminal frame left in place outlives the stream that delivered
    // it: `pairing` prefers a frame whenever one exists, so both controls stay
    // hidden, the countdown pins at zero and every later row read is ignored.
    expect(await screen.findByRole('button', { name: REPAIR })).toBeInTheDocument();
    expect(screen.queryByTestId('pairing-qr')).toBeNull();
    expect(screen.getByText(/could not be read/)).toBeInTheDocument();
  });

  it('reopens the stream on a close the browser will not retry, then gives up', async () => {
    vi.useFakeTimers();
    try {
      await mountFake({ link: linkState({ fatal_is_permanent: true }) });
      expect(streams).toHaveLength(1);

      // Not a dropped transport, which the browser retries itself: a 404 the
      // moment `pairing_enabled` flips, a 401 on session expiry, any 5xx.
      // EventSource goes to CLOSED and fires this without reconnecting.
      streams[0].fail();
      await tick();
      expect(screen.getByText(/Reconnecting/)).toBeInTheDocument();

      let opened = 1;
      for (let attempt = 1; attempt <= 5; attempt += 1) {
        await vi.advanceTimersByTimeAsync(20_000);
        opened += 1;
        expect(streams).toHaveLength(opened);
        streams[streams.length - 1].fail();
        await tick();
      }

      // Past the cap it says so rather than retrying for the life of the tab.
      await vi.advanceTimersByTimeAsync(20_000);
      expect(streams).toHaveLength(opened);
      expect(screen.getByText(/not reachable/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('the WhatsApp card — the countdown', () => {
  it('counts down from the deadline and leaves no timer behind', async () => {
    vi.useFakeTimers();
    try {
      // **The pane is opened before the window is, and the clock is moved on
      // in between.** That gap is the whole test: `now` seeded once at
      // construction is stale by however long the page has been sitting, and
      // the interval's first assignment is a second away — so the first render
      // of a window added a minute of page-open age to the real remaining
      // time. Mounted with the window already present, the construction
      // instant and the effect's own reading are the same and the assertion
      // cannot fail; measured, that version stayed green under the control.
      await mountFake({ link: linkState({ fatal_is_permanent: true }) });
      await vi.advanceTimersByTimeAsync(60_000);
      expect(streams).toHaveLength(1);

      streams[0].emit('pairing', frame('awaiting_scan', { expires_at: Date.now() / 1000 + 95 }));
      await tick();

      expect(screen.getByTestId('pairing-remaining')).toHaveTextContent('expires in 1m 35s');

      await vi.advanceTimersByTimeAsync(2000);
      await tick();
      expect(screen.getByTestId('pairing-remaining')).toHaveTextContent('expires in 1m 33s');

      cleanup();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('floors the remaining seconds rather than rounding the milliseconds', async () => {
    vi.useFakeTimers();
    try {
      await mountFake({ link: linkState({ fatal_is_permanent: true }) });
      expect(streams).toHaveLength(1);

      // The misplaced paren rounded the *millisecond* difference and then
      // divided, which is a no-op except in the sub-millisecond band below a
      // whole second — where it rounds up and reports one second more than is
      // left. 89999.6ms is inside that band: floored it is 89s, rounded first
      // it is 90s.
      streams[0].emit(
        'pairing',
        frame('awaiting_scan', { expires_at: (Date.now() + 89_999.6) / 1000 }),
      );
      await tick();

      expect(screen.getByTestId('pairing-remaining')).toHaveTextContent('expires in 1m 29s');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('the WhatsApp card — the code', () => {
  it('refetches the SVG when qr_seq moves and holds it still when it does not', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));

    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 3, qr_available: true }));
    const img = await screen.findByTestId('pairing-qr');
    const first = img.getAttribute('src');
    expect(first).toContain('seq=3');
    expect(first).toContain('/pairing/qr.svg');

    // A frame that repeats the sequence must not move the src: the browser
    // refetches on the URL changing, so a src rebuilt per frame would pull a
    // fresh image every second for the whole window.
    //
    // `tick()`, deliberately, and **not** `waitFor` — Svelte batches the DOM
    // write, so an equality assertion runs first against the *pre-emit* DOM
    // and `waitFor` returns on that pass before the update it was meant to
    // catch has landed. Measured: the control that rebuilds the src on every
    // frame leaves the `waitFor` form green and turns this one red.
    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 3, qr_available: true }));
    await tick();
    expect(screen.getByTestId('pairing-qr').getAttribute('src')).toBe(first);

    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 4, qr_available: true }));
    await waitFor(() =>
      expect(screen.getByTestId('pairing-qr').getAttribute('src')).toContain('seq=4'),
    );
  });

  it('draws nothing while the window carries no code yet', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));

    // `qr_available` is why the frame carries it: a `qr_seq` of 0 means "no
    // code yet" rather than "a code the fetch would miss".
    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 0, qr_available: false }));

    await screen.findByTestId('pairing-state');
    expect(screen.queryByTestId('pairing-qr')).toBeNull();
  });

  it('retries a failed fetch once, then says the code could not be fetched', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));
    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 7, qr_available: true }));
    const img = await screen.findByTestId('pairing-qr');
    const first = img.getAttribute('src');

    // The server 404s a stale window, a terminal row, a missing relay file and
    // a window not yet at `awaiting_scan`, and `qr_available` comes off a relay
    // read up to a poll old — so the window can close between the frame and the
    // browser's request. `src` is a pure function of the sequence, so nothing
    // re-issues a failed fetch on its own.
    await fireEvent.error(img);
    await waitFor(() =>
      expect(screen.getByTestId('pairing-qr').getAttribute('src')).not.toBe(first),
    );
    expect(screen.getByTestId('pairing-qr').getAttribute('src')).toContain('seq=7');

    await fireEvent.error(screen.getByTestId('pairing-qr'));

    // One retry, then words: this is the operator's only route to the code, so
    // the failure must not be a broken image with no text.
    expect(await screen.findByTestId('pairing-qr-error')).toBeInTheDocument();
    expect(screen.queryByTestId('pairing-qr')).toBeNull();
  });

  it('refetches after a rotation even once a fetch has failed', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));
    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 7, qr_available: true }));
    const img = await screen.findByTestId('pairing-qr');
    await fireEvent.error(img);
    await fireEvent.error(screen.getByTestId('pairing-qr'));
    await screen.findByTestId('pairing-qr-error');

    streams[0].emit('pairing', frame('awaiting_scan', { qr_seq: 8, qr_available: true }));

    // A new code is a new fetch, so the fallback is not a one-way door for the
    // rest of the window.
    const next = await screen.findByTestId('pairing-qr');
    expect(next.getAttribute('src')).toContain('seq=8');
    expect(screen.queryByTestId('pairing-qr-error')).toBeNull();
  });

  it('ignores a frame that is not an object rather than crashing on it', async () => {
    // **The throw is recorded, not merely absent from the DOM**, and that is
    // the whole shape of this test. `JSON.parse('7')` survives a `!== null`
    // check and then throws in the template at `pairing.state.replace(...)` —
    // during Svelte's flush, in a microtask nothing here awaits, so it arrives
    // as an unhandled error attributed to whichever file was running while the
    // suite still reports every test green. Measured: with the projection
    // replaced by a bare cast, the DOM assertions below all pass and only the
    // recorder goes red.
    const unhandled: unknown[] = [];
    const record = (e: unknown) => unhandled.push(e);
    process.on('unhandledRejection', record);
    process.on('uncaughtException', record);
    try {
      await mount({ link: linkState({ fatal_is_permanent: true }) });
      await waitFor(() => expect(streams).toHaveLength(1));

      streams[0].emit('pairing', 7);
      streams[0].emit('pairing', 'paired');
      await tick();
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(unhandled).toEqual([]);
      expect(screen.queryByTestId('pairing-state')).toBeNull();

      // And the pane is still live afterwards.
      streams[0].emit('pairing', frame('awaiting_sidecar'));
      expect(await screen.findByTestId('pairing-state')).toHaveTextContent('awaiting sidecar');
    } finally {
      process.off('unhandledRejection', record);
      process.off('uncaughtException', record);
    }
  });

  it('puts no payload in the document even if a frame carries one', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    await waitFor(() => expect(streams).toHaveLength(1));

    // The stream sends five fields and none of them is the code. This is the
    // control for that: a frame carrying one must leave no trace of it in the
    // DOM, so the property does not rest on the server alone.
    streams[0].emit(
      'pairing',
      frame('awaiting_scan', { qr_seq: 2, qr_available: true, qr: 'PAYLOAD-2@G.WHATSAPP.NET' }),
    );

    await screen.findByTestId('pairing-qr');
    // The DOM, which is what a reader of the page sees. It says nothing about
    // the heap — that property is structural instead: `projectFrame` copies the
    // five declared fields and the parsed object is not retained, so an
    // undeclared field never reaches component state at all.
    expect(document.body.innerHTML).not.toContain('PAYLOAD-2');
  });
});
