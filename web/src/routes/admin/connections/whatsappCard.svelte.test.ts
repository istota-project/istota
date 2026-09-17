import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { tick } from 'svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor, fireEvent, within } from '@testing-library/svelte';

import type { AdminConnection, AdminConnectionLink } from '$lib/api';

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

  it('offers nothing to press on a working session, and no re-pair on the card', async () => {
    await mount({ link: linkState({ ready: true }) });

    // The assertion the user's constraint is about: a green session has no
    // one-click way out of itself.
    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.getByText('The session is linked and working.')).toBeInTheDocument();
    // The destructive route exists, outside the card and behind a phrase.
    const zone = screen.getByTestId('forced-zone');
    expect(zone).toBeInTheDocument();
    expect(screen.getByTestId('whatsapp-card').contains(zone)).toBe(false);
  });

  it('offers only the typed control on an unsettled session', async () => {
    await mount({ link: linkState({ ready: false, connected: true }) });

    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.getByRole('button', { name: UNLINK })).toBeInTheDocument();
    expect(screen.getByText(/may be mid-reconnect/)).toBeInTheDocument();
  });

  it('offers both on the split deployment, where the link cannot be read here', async () => {
    // `link: null` is not an edge case — it is every page load on the canonical
    // Ansible deployment, where the bridge lives in the scheduler unit. The
    // one-click start is safe there because the bridge refuses an unforced
    // re-pair of a session with no permanent fault and records that on the row.
    await mount({ link: null });

    expect(screen.getByRole('button', { name: REPAIR })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: UNLINK })).toBeInTheDocument();
    expect(screen.getByText(/cannot read the live link/)).toBeInTheDocument();
  });

  it('offers no control and opens no stream on the Cloud adapter', async () => {
    await mount({ provider: 'whatsapp_cloud', pairing_supported: false, pairing_enabled: false });

    expect(screen.queryByRole('button', { name: REPAIR })).toBeNull();
    expect(screen.queryByRole('button', { name: UNLINK })).toBeNull();
    expect(screen.getByText(/Cloud API adapter/)).toBeInTheDocument();
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

  it('re-reads the index after a refusal and names it', async () => {
    await mount({ link: linkState({ fatal_is_permanent: true }) });
    api.startWhatsAppPairing.mockRejectedValue(new Error('API error: 409'));
    api.getAdminConnections.mockClear();

    await fireEvent.click(screen.getByRole('button', { name: REPAIR }));

    await waitFor(() => expect(screen.getByText(/Refused:/)).toBeInTheDocument());
    // The server's view is the authority on what happened, so the card asks
    // again rather than reasoning from its own stale copy.
    expect(getAdminConnections).toHaveBeenCalled();
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
    expect(document.body.innerHTML).not.toContain('PAYLOAD-2');
  });
});
