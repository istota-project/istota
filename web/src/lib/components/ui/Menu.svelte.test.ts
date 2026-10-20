import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { Pencil, Trash2 } from '@lucide/svelte';
import Harness from './Menu.harness.svelte';
import type { MenuItem } from './Menu.svelte';

afterEach(cleanup);

const trigger = () => screen.getByRole('button', { name: 'Open' });
// bits-ui keeps the content `visibility: hidden` until floating-ui places it,
// which never happens in jsdom, and a hidden node has no accessible name; so a
// row is found by its label text.
const item = (label: string) => screen.getByText(label).closest('[role="menuitem"]') as HTMLElement;

async function openByKeyboard() {
  await fireEvent.keyDown(trigger(), { key: 'Enter' });
  return screen.findByRole('menu', { name: 'Row actions' });
}

// jsdom has no PointerEvent; bits-ui reads only `pointerType` and `button`.
function pointer(type: string, pointerType: string) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0 });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  return event;
}

async function tapTrigger() {
  trigger().dispatchEvent(pointer('pointerdown', 'touch'));
  trigger().dispatchEvent(pointer('pointerup', 'touch'));
  await tick();
  return screen.findByRole('menu', { name: 'Row actions' });
}

const items: MenuItem[] = [
  { label: 'Edit', icon: Pencil, onSelect: () => {} },
  { label: 'Delete', icon: Trash2, danger: true, onSelect: () => {} },
];

describe('Menu', () => {
  it('renders each item with its icon, in a menu named by label', async () => {
    render(Harness, { items });
    const menu = await openByKeyboard();
    const edit = item('Edit');
    expect(menu).toContainElement(edit);
    expect(edit.querySelector('svg')).not.toBeNull();
    expect(edit).toHaveClass('ui-menu-item');
  });

  it('marks a danger item with the danger class', async () => {
    render(Harness, { items });
    await openByKeyboard();
    expect(item('Delete')).toHaveClass('ui-menu-item--danger');
    expect(item('Edit')).not.toHaveClass('ui-menu-item--danger');
  });

  it('renders an href item as a link, and marks the current page', async () => {
    render(Harness, {
      items: [
        { label: 'Chat', href: '/istota/chat', active: true },
        { label: 'Files', href: '/istota/files' },
      ],
    });
    await openByKeyboard();
    const chat = item('Chat');
    expect(chat.tagName).toBe('A');
    expect(chat).toHaveAttribute('href', '/istota/chat');
    expect(chat).toHaveAttribute('aria-current', 'page');
    expect(item('Files')).not.toHaveAttribute('aria-current');
  });

  it('runs onSelect for an item and not for a disabled one', async () => {
    const run = vi.fn();
    const skipped = vi.fn();
    render(Harness, {
      items: [
        { label: 'Run', onSelect: run },
        { label: 'Running…', disabled: true, onSelect: skipped },
      ],
    });
    await openByKeyboard();
    await fireEvent.click(item('Running…'));
    expect(skipped).not.toHaveBeenCalled();
    await fireEvent.click(item('Run'));
    expect(run).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
  });

  it('moves focus through the items with the arrow keys', async () => {
    render(Harness, { items });
    await openByKeyboard();
    const edit = item('Edit');
    await waitFor(() => expect(document.activeElement).toBe(edit));
    await fireEvent.keyDown(edit, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(item('Delete'));
  });

  it('renders a trailing snippet inside its item', async () => {
    render(Harness, { items, trailing: true });
    await openByKeyboard();
    const edit = item('Edit');
    expect(edit.querySelector('.test-count')).toHaveTextContent('3');
  });
});

describe('Menu retainFocus', () => {
  it('leaves focus in the field when a tap opens it, picks a row and closes', async () => {
    const pick = vi.fn();
    render(Harness, { items: [{ label: 'Photo Library', onSelect: pick }], retainFocus: true });
    const field = screen.getByTestId('field');
    field.focus();

    await tapTrigger();
    await tick();
    expect(document.activeElement).toBe(field);

    const row = item('Photo Library');
    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    row.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);

    await fireEvent.click(row);
    expect(pick).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    expect(document.activeElement).toBe(field);
  });

  it('still hands focus to the first row when the keyboard opens it', async () => {
    render(Harness, { items, retainFocus: true });
    trigger().focus();
    await openByKeyboard();
    await waitFor(() => expect(document.activeElement).toBe(item('Edit')));
  });
});
