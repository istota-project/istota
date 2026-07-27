import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import KebabMenu from './KebabMenu.svelte';
import type { KebabItem } from './KebabMenu.svelte';

// An open bits-ui menu holds a body scroll lock whose reset runs after unmount;
// if the test ends with the menu open that reset can land after jsdom has torn
// down `document` and surfaces as an unhandled error. Close it first.
afterEach(async () => {
  await fireEvent.keyDown(document.body, { key: 'Escape' });
  cleanup();
  // Let the unmount effects flush while `document` is still alive.
  await new Promise((resolve) => setTimeout(resolve, 0));
});

// bits-ui opens its floating content on pointerdown, which jsdom only partly
// implements; the keyboard path is equivalent and fully supported here.
async function openMenu(items: KebabItem[], ariaLabel = 'Actions') {
  render(KebabMenu, { items, ariaLabel });
  const trigger = screen.getByLabelText(ariaLabel);
  await fireEvent.keyDown(trigger, { key: 'Enter' });
  return trigger;
}

describe('KebabMenu', () => {
  it('labels the trigger so each row menu is distinguishable', () => {
    render(KebabMenu, {
      items: [{ label: 'Edit', onSelect: () => {} }],
      ariaLabel: 'Block actions',
    });
    expect(screen.getByLabelText('Block actions')).not.toBeNull();
  });

  it('renders every item once opened', async () => {
    await openMenu([
      { label: 'Edit', onSelect: () => {} },
      { label: 'Duplicate', onSelect: () => {} },
      { label: 'Delete', danger: true, onSelect: () => {} },
    ]);
    expect(await screen.findByText('Edit')).not.toBeNull();
    expect(screen.getByText('Duplicate')).not.toBeNull();
    expect(screen.getByText('Delete')).not.toBeNull();
  });

  it('invokes onSelect when an item is chosen', async () => {
    const onSelect = vi.fn();
    await openMenu([{ label: 'Duplicate', onSelect }]);
    await fireEvent.click(await screen.findByText('Duplicate'));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  // An href item must stay a real anchor: health's "Edit" actions navigate to a
  // detail page, and routing them through onSelect + goto() would silently drop
  // middle-click and open-in-new-tab.
  it('renders an href item as an anchor carrying the url', async () => {
    await openMenu([{ label: 'Edit', href: '/istota/health/immunizations/detail?id=7' }]);
    const link = await screen.findByText('Edit');
    expect(link.tagName).toBe('A');
    expect(link.getAttribute('href')).toBe('/istota/health/immunizations/detail?id=7');
  });

  it('renders a non-href item as something other than an anchor', async () => {
    await openMenu([{ label: 'Delete', danger: true, onSelect: () => {} }]);
    const item = await screen.findByText('Delete');
    expect(item.tagName).not.toBe('A');
  });

  it('marks a disabled item as disabled and does not fire its onSelect', async () => {
    const onSelect = vi.fn();
    await openMenu([{ label: 'Running…', disabled: true, onSelect }]);
    const item = await screen.findByText('Running…');
    expect(item.hasAttribute('data-disabled')).toBe(true);
    await fireEvent.click(item);
    expect(onSelect).not.toHaveBeenCalled();
  });
});
