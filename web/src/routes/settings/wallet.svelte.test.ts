import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { ViteDevServer } from 'vite';
import { mockApi } from '../../../vite-mock-api';
import { USER_SETTINGS_SECTIONS } from '$lib/settings/sections';
import Harness from './walletHarness.test.svelte';

function installMock(walletOverride: Record<string, unknown> = {}) {
  let middleware: (req: IncomingMessage, res: ServerResponse, next: () => void) => void;
  const configure = mockApi().configureServer as (server: ViteDevServer) => void;
  configure({
    middlewares: {
      use: (handler: typeof middleware) => {
        middleware = handler;
      },
    },
  } as ViteDevServer);
  vi.stubGlobal(
    'fetch',
    async (url: string, init?: RequestInit) =>
      new Promise<Response>((resolve) => {
        const req = Readable.from(
          init?.body ? [Buffer.from(init.body as string)] : [],
        ) as IncomingMessage;
        req.url = url.startsWith('/api/') ? `/istota${url}` : url;
        req.method = init?.method ?? 'GET';
        const res = {
          statusCode: 200,
          setHeader() {},
          end(raw: string) {
            if (req.url === '/istota/api/settings/wallet' && req.method === 'GET')
              raw = JSON.stringify({ ...JSON.parse(raw), ...walletOverride });
            resolve(
              new Response(raw, {
                status: res.statusCode,
                headers: { 'Content-Type': 'application/json' },
              }),
            );
          },
        } as unknown as ServerResponse;
        middleware(req, res, () => resolve(new Response('{}', { status: 404 })));
      }),
  );
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('adds a card, saves policy through the app bar and cancels a purchase through the stateful mock', async () => {
  installMock();
  render(Harness);
  await screen.findByRole('heading', { name: 'Spending policy' });
  for (const section of USER_SETTINGS_SECTIONS) {
    expect(
      screen.getByRole('link', { name: section.label, exact: true }).getAttribute('href'),
    ).toBe(`/settings${section.href}`);
  }
  await fireEvent.click(screen.getByRole('button', { name: 'Add card', exact: true }));
  const dialog = within(screen.getByRole('dialog'));
  await fireEvent.input(dialog.getByLabelText('Label'), { target: { value: 'Travel card' } });
  await fireEvent.input(dialog.getByLabelText('Card number'), {
    target: { value: '4242'.repeat(4) },
  });
  await fireEvent.input(dialog.getByLabelText('CVC'), { target: { value: '123' } });
  await fireEvent.input(dialog.getByLabelText('Expiry month'), { target: { value: '12' } });
  await fireEvent.input(dialog.getByLabelText('Expiry year'), { target: { value: '2099' } });
  await fireEvent.click(dialog.getByRole('button', { name: 'Add card' }));
  await screen.findByText('Travel card');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(document.body.textContent).not.toContain('4242'.repeat(4));
  await fireEvent.input(screen.getByLabelText('Auto limit per purchase'), {
    target: { value: '25.15' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(
      (screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement).disabled,
    ).toBe(true),
  );
  const data = await (await fetch('/istota/api/settings/wallet')).json();
  expect(data.policy.auto_limit_cents).toBe(2515);
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel purchase 1' }));
  await screen.findByText('Cancelled');
  expect((await (await fetch('/istota/api/settings/wallet')).json()).purchases[0].state).toBe(
    'cancelled',
  );
});

it.each([
  [{ enabled: false }, 'The wallet is off on this deployment.'],
  [{ refusal: 'Isolation refused' }, 'Isolation refused'],
])('keeps saved cards read-only when unavailable (%j)', async (override, message) => {
  installMock(override);
  render(Harness);
  await screen.findByText(message);
  expect((screen.getByRole('button', { name: 'Add card' }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  expect(
    (screen.getByLabelText('Auto limit per purchase') as HTMLInputElement).disabled ||
      screen.getByLabelText('Auto limit per purchase').closest('fieldset')?.disabled,
  ).toBe(true);
  if ('enabled' in override)
    expect(screen.queryByRole('link', { name: 'Wallet', exact: true })).toBeNull();
});

it.each([
  ['JPY', '125', 125],
  ['KWD', '1.125', 1125],
])('saves %s with its currency exponent', async (currency, value, expected) => {
  installMock({
    policy: {
      currency,
      auto_limit_cents: 0,
      auto_budget_cents: 0,
      ceiling_cents: null,
      allow_scheduled: false,
    },
  });
  render(Harness);
  await screen.findByRole('heading', { name: 'Spending policy' });
  await fireEvent.input(screen.getByLabelText('Auto limit per purchase'), { target: { value } });
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(
      (screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement).disabled,
    ).toBe(true),
  );
  vi.unstubAllGlobals();
  installMock();
  expect((await (await fetch('/istota/api/settings/wallet')).json()).policy.auto_limit_cents).toBe(
    expected,
  );
});
