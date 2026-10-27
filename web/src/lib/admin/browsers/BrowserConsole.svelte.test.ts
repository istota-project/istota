import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
const notices = vi.hoisted(() => ({
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('@minot/ui/stores/notices', () => notices);

// A stand-in for noVNC's RFB. Like the real one, it takes over the channel's
// `onmessage` when constructed, so what it receives is what noVNC would read.
const rfb = vi.hoisted(() => {
  class FakeRFB extends EventTarget {
    static instances: FakeRFB[] = [];
    received: unknown[] = [];
    viewOnly = false;
    scaleViewport = false;
    resizeSession = true;
    clipViewport = true;
    focusOnClick = false;
    sendCredentials = vi.fn();
    disconnect = vi.fn();
    constructor(
      readonly target: HTMLElement,
      readonly channel: WebSocket,
    ) {
      super();
      channel.onmessage = (event: MessageEvent) => this.received.push(event.data);
      FakeRFB.instances.push(this);
    }
  }
  return { FakeRFB };
});
vi.mock('@novnc/novnc', () => ({ default: rfb.FakeRFB }));

class FakeSocket extends EventTarget {
  static instances: FakeSocket[] = [];
  sent: unknown[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  binaryType = 'blob';
  constructor(readonly url: string) {
    super();
    FakeSocket.instances.push(this);
  }
  send(data: unknown) {
    this.sent.push(data);
  }
  close = vi.fn();
  open() {
    this.dispatchEvent(new Event('open'));
  }
  deliver(data: unknown) {
    const event = new MessageEvent('message', { data });
    this.onmessage?.(event);
    this.dispatchEvent(event);
  }
  serverClose(code: number) {
    this.dispatchEvent(Object.assign(new Event('close'), { code }));
  }
}

import BrowserConsole from './BrowserConsole.svelte';
import { GroupApiError } from '$lib/api';

const PROOF = { request_id: 'req-1', password: 'hunter2' };

beforeEach(() => {
  vi.clearAllMocks();
  FakeSocket.instances = [];
  rfb.FakeRFB.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
  api.startStepUp.mockResolvedValue({
    request_id: 'req-1',
    method: 'password',
    methods: ['password'],
  });
  api.getAdminBrowserConsoleTicket.mockResolvedValue({ ticket: 'tkt-1', expires_in: 60 });
  api.adminBrowserConsoleUrl.mockReturnValue('ws://test/istota/api/admin/browsers/console');
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function open(onExit = vi.fn()) {
  render(BrowserConsole, { user: 'alice', backHref: '/istota/admin/browsers', onExit });
  return onExit;
}

async function confirmStepUp() {
  const dialog = await screen.findByRole('dialog', { name: 'Confirm sensitive action' });
  await waitFor(() =>
    expect(api.startStepUp).toHaveBeenCalledWith('admin_browser_console', undefined),
  );
  await fireEvent.input(await within(dialog).findByLabelText('Account password'), {
    target: { value: 'hunter2' },
  });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }));
}

async function relayOpen() {
  await confirmStepUp();
  await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
  const socket = FakeSocket.instances[0];
  socket.open();
  return socket;
}

it('asks for the step-up before the ticket, and sends the ticket as the first frame', async () => {
  open();
  expect(screen.getByText('Connecting')).toBeTruthy();
  expect(api.getAdminBrowserConsoleTicket).not.toHaveBeenCalled();
  const socket = await relayOpen();
  expect(api.getAdminBrowserConsoleTicket).toHaveBeenCalledWith('alice', PROOF);
  expect(socket.url).toBe('ws://test/istota/api/admin/browsers/console');
  expect(socket.sent).toEqual(['tkt-1']);
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('hands the socket to RFB inside the ok delivery, before the banner', async () => {
  open();
  const socket = await relayOpen();
  const banner = new Uint8Array([82, 70, 66]).buffer;
  socket.deliver('ok');
  socket.deliver(banner);
  expect(rfb.FakeRFB.instances).toHaveLength(1);
  const view = rfb.FakeRFB.instances[0];
  expect(view.channel).toBe(socket);
  expect(view.received).toEqual([banner]);
  expect(view.scaleViewport).toBe(true);
  expect(view.resizeSession).toBe(false);
  expect(view.clipViewport).toBe(false);
  expect(view.focusOnClick).toBe(true);
  view.dispatchEvent(new Event('connect'));
  expect(await screen.findByText('Connected')).toBeTruthy();
});

it('says the browser is not running on 4404 and offers the way back', async () => {
  open();
  const socket = await relayOpen();
  socket.deliver('ok');
  socket.serverClose(4404);
  expect(await screen.findByText('This browser is not running.')).toBeTruthy();
  const back = screen.getByRole('link', { name: 'Back to browsers' });
  expect(back.getAttribute('href')).toBe('/istota/admin/browsers');
  expect(screen.queryByRole('button', { name: 'Reconnect' })).toBeNull();
  expect(screen.getByText('Closed')).toBeTruthy();
});

it('names the proxy on a 1006 before ok, and Reconnect asks again', async () => {
  open();
  const socket = await relayOpen();
  socket.serverClose(1006);
  expect(await screen.findByText(/must forward websocket connections/)).toBeTruthy();
  expect(rfb.FakeRFB.instances).toHaveLength(0);
  await fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
  expect(await screen.findByRole('dialog', { name: 'Confirm sensitive action' })).toBeTruthy();
  await waitFor(() => expect(api.startStepUp).toHaveBeenCalledTimes(2));
});

it('asks for the VNC password and sends it', async () => {
  open();
  const socket = await relayOpen();
  socket.deliver('ok');
  const view = rfb.FakeRFB.instances[0];
  view.dispatchEvent(new Event('credentialsrequired'));
  const dialog = await screen.findByRole('dialog', { name: 'VNC password' });
  await fireEvent.input(within(dialog).getByLabelText('Password'), {
    target: { value: 'vnc-secret' },
  });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Send' }));
  expect(view.sendCredentials).toHaveBeenCalledWith({ password: 'vnc-secret' });
  view.dispatchEvent(
    new CustomEvent('securityfailure', { detail: { status: 1, reason: 'Authentication failed' } }),
  );
  expect(await within(dialog).findByText('Authentication failed')).toBeTruthy();
});

it('sets view only on the RFB from the checkbox', async () => {
  open();
  const socket = await relayOpen();
  socket.deliver('ok');
  const view = rfb.FakeRFB.instances[0];
  view.dispatchEvent(new Event('connect'));
  const box = await screen.findByLabelText('View only');
  await waitFor(() => expect(box).not.toBeDisabled());
  await fireEvent.click(box);
  expect(view.viewOnly).toBe(true);
});

it('disconnects through RFB and leaves', async () => {
  const onExit = open();
  const socket = await relayOpen();
  socket.deliver('ok');
  const view = rfb.FakeRFB.instances[0];
  await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
  expect(view.disconnect).toHaveBeenCalled();
  expect(onExit).toHaveBeenCalled();
});

it('returns to the list when the step-up is cancelled', async () => {
  const onExit = open();
  const dialog = await screen.findByRole('dialog', { name: 'Confirm sensitive action' });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  expect(onExit).toHaveBeenCalled();
  expect(api.getAdminBrowserConsoleTicket).not.toHaveBeenCalled();
});

it('shows a missing identity as an alert, opening no socket', async () => {
  api.getAdminBrowserConsoleTicket.mockRejectedValue(
    new GroupApiError(
      403,
      'admin_identity_required',
      'Opening a browser console needs an admin with a live email sign-in identity.',
    ),
  );
  open();
  await confirmStepUp();
  expect(await screen.findByText(/needs an admin with a live email sign-in/)).toBeTruthy();
  expect(FakeSocket.instances).toHaveLength(0);
  expect(notices.notifyError).not.toHaveBeenCalled();
});

it('keeps a refused password in the step-up dialog', async () => {
  api.getAdminBrowserConsoleTicket.mockRejectedValue(
    new GroupApiError(403, '', 'That password is not right.', 'bad'),
  );
  open();
  await confirmStepUp();
  const dialog = screen.getByRole('dialog', { name: 'Confirm sensitive action' });
  expect(await within(dialog).findByText('That password is not right.')).toBeTruthy();
  expect(FakeSocket.instances).toHaveLength(0);
});
