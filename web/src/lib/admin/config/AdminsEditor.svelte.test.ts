import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import AdminsEditor from './AdminsEditor.svelte';

afterEach(cleanup);

function mount(extra: { pending?: string[] | null; readOnlyReason?: string | null } = {}) {
  const onChange = vi.fn();
  render(AdminsEditor, {
    props: {
      users: ['alice', 'bob'],
      pending: extra.pending ?? null,
      self: 'alice',
      editable: true,
      readOnlyReason: extra.readOnlyReason ?? null,
      onChange,
    },
  });
  return onChange;
}

it('adds an id and removes another, but never the acting admin', async () => {
  const onChange = mount();
  expect(screen.queryByRole('button', { name: 'Remove alice' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Remove bob' }));
  expect(onChange).toHaveBeenLastCalledWith(['alice']);

  await fireEvent.input(screen.getByPlaceholderText('user id'), { target: { value: 'carol' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Add' }));
  expect(onChange).toHaveBeenLastCalledWith(['alice', 'bob', 'carol']);
});

it('reports a list equal to the current one as unchanged', async () => {
  const onChange = mount({ pending: ['alice'] });
  expect(screen.getByText('Removed: bob')).toBeInTheDocument();
  await fireEvent.input(screen.getByPlaceholderText('user id'), { target: { value: 'bob' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Add' }));
  expect(onChange).toHaveBeenLastCalledWith(null);
});

it('refuses a malformed id and a duplicate without changing the list', async () => {
  const onChange = mount();
  await fireEvent.input(screen.getByPlaceholderText('user id'), { target: { value: 'a b' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Add' }));
  expect(screen.getByText(/letters, digits/)).toBeInTheDocument();
  await fireEvent.input(screen.getByPlaceholderText('user id'), { target: { value: 'bob' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Add' }));
  expect(screen.getByText('bob is already an admin.')).toBeInTheDocument();
  expect(onChange).not.toHaveBeenCalled();
});

it('is read-only with the reason when a plan manages the list', () => {
  mount({ readOnlyReason: 'Managed by ansible, whose next run would revert a change made here.' });
  expect(screen.getByText(/Managed by ansible/)).toBeInTheDocument();
  expect(screen.queryByPlaceholderText('user id')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Remove bob' })).toBeNull();
});
