import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import KebabMenu from './KebabMenu.svelte';
import type { KebabItem } from './KebabMenu.svelte';

// Auto-cleanup is off (vitest runs without `globals`, so testing-library never
// sees a global `afterEach` to register with), hence the explicit unmount.
//
// The deferred body-scroll-lock reset that unmount schedules is drained once,
// before teardown, in vitest-setup.ts — it is not this component's problem, and
// every file that opens a bits-ui overlay has it. This hook used to try to
// handle it here by flushing a 0ms timer, which could never work: the reset is
// scheduled 24ms out, so the flush returned long before it and the flake stayed.
afterEach(() => {
  cleanup();
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
