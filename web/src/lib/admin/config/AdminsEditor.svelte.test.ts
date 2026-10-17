import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import AdminsEditor from './AdminsEditor.svelte';

afterEach(cleanup);

function mount(extra: { pending?: string[] | null; readOnlyReason?: string | null } = {}) {
  const onChange = vi.fn();
  render(AdminsEditor, {
    props: {
      users: ['alice', 'bob'],
      candidates: ['alice', 'bob', 'carol'],
      pending: extra.pending ?? null,
      self: 'alice',
      editable: true,
      readOnlyReason: extra.readOnlyReason ?? null,
      onChange,
    },
  });
  return onChange;
}

it('removes another admin, but never the acting one', async () => {
  const onChange = mount();
  expect(screen.getByText('alice (you)')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Remove alice (you)' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Remove bob' }));
  expect(onChange).toHaveBeenLastCalledWith(['alice']);
});

it('says what a pending list removes, and undoes it', async () => {
  const onChange = mount({ pending: ['alice'] });
  expect(screen.getByText('Removed: bob')).toBeInTheDocument();
  await fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
  expect(onChange).toHaveBeenLastCalledWith(null);
});

it('reports a list equal to the current one as unchanged', async () => {
  const onChange = mount({ pending: ['alice', 'bob', 'carol'] });
  await fireEvent.click(screen.getByRole('button', { name: 'Remove carol' }));
  expect(onChange).toHaveBeenLastCalledWith(null);
});

it('is read-only with the reason when a plan manages the list', () => {
  mount({ readOnlyReason: 'Managed by ansible, whose next run would revert a change made here.' });
  expect(screen.getByText(/Managed by ansible/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Remove bob' })).toBeNull();
  expect(screen.queryByRole('combobox')).toBeNull();
});
