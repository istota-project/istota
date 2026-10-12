import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { ConfigRequest } from '$lib/api';
import { configRequest } from '$lib/test/adminConfig';
import HistoryList from './HistoryList.svelte';

afterEach(cleanup);

function mount(
  requests: ConfigRequest[] | null,
  extra: { blocked?: string; restoring?: number } = {},
) {
  const onRestore = vi.fn();
  render(HistoryList, {
    props: {
      requests,
      error: '',
      restoreBlockedReason: extra.blocked ?? null,
      restoring: extra.restoring ?? null,
      onRestore,
    },
  });
  return { onRestore };
}

const applied = configRequest({
  id: 12,
  status: 'applied',
  changed_sections: ['email', 'models'],
  secret_names: ['istota_email_imap_password'],
  diff_text: '--- config.toml\n+++ config.toml\n+imap_host = "new"',
  finished_at: '2026-10-12T08:06:00Z',
});
const refused = configRequest({
  id: 13,
  status: 'refused',
  admins_changed: true,
  changed_sections: [],
  output_text: 'config.toml changed since this request was made',
});

describe('the history', () => {
  it('lists each change with what it touched', () => {
    mount([refused, applied]);
    expect(screen.getByText('#13')).toBeInTheDocument();
    expect(screen.getByText('refused')).toBeInTheDocument();
    expect(screen.getByText('the admin list')).toBeInTheDocument();
    expect(
      screen.getByText('email, models · credentials: istota_email_imap_password'),
    ).toBeInTheDocument();
    expect(screen.getByText(/imap_host = "new"/)).toBeInTheDocument();
    expect(screen.getByText(/changed since this request was made/)).toBeInTheDocument();
  });

  it('offers restore for an applied change only', async () => {
    const { onRestore } = mount([refused, applied]);
    const buttons = screen.getAllByRole('button', { name: 'Restore' });
    expect(buttons).toHaveLength(1);
    await fireEvent.click(buttons[0]);
    expect(onRestore).toHaveBeenCalledWith(12);
  });

  it('says why restore is not available now', () => {
    mount([applied], { blocked: 'Review or discard your pending changes first.' });
    expect(screen.getByRole('button', { name: 'Restore' })).toBeDisabled();
    expect(screen.getByText(/pending changes first/)).toBeInTheDocument();
  });

  it('says when nothing has been changed from the page', () => {
    mount([]);
    expect(screen.getByText(/No change has been made/)).toBeInTheDocument();
  });
});
