import { describe, it, expect, afterEach, vi } from 'vitest';

// bits-ui reads the user agent once, at import, to decide whether a touch
// selection waits for the click (everywhere else) or happens on pointerup
// (iOS). It has to be an iPhone before the import below runs.
vi.hoisted(() => {
  Object.defineProperty(window.navigator, 'userAgent', {
    value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15',
    configurable: true,
  });
});

import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import Select from './Select.svelte';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const options = [
  { value: 'accounts', label: 'Accounts' },
  { value: 'portfolio', label: 'Portfolio' },
];

async function openSelect() {
  const trigger = screen.getByRole('button', { name: 'Section' });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
}

function elementBehind() {
  const behind = document.createElement('button');
  const onClick = vi.fn();
  behind.addEventListener('click', onClick);
  document.body.appendChild(behind);
  return { behind, onClick };
}

describe('Select on iOS (ISSUE-677)', () => {
  it('does not leak the tap that picked an item to the element behind it', async () => {
    const onValueChange = vi.fn();
    render(Select, { value: 'accounts', options, ariaLabel: 'Section', onValueChange });
    const { behind, onClick } = elementBehind();
    await openSelect();

    const option = await screen.findByRole('option', { name: 'Portfolio' });
    await fireEvent.pointerUp(option, { pointerType: 'touch' });
    // WebKit's synthesized click lands on whatever is under the finger once the
    // popover has gone, which is the account row on /money/accounts.
    await fireEvent.click(behind);

    expect(onValueChange).toHaveBeenCalledTimes(1);
    expect(onValueChange).toHaveBeenCalledWith('portfolio');
    expect(onClick).not.toHaveBeenCalled();
    behind.remove();
  });

  it('lets the next real tap through', async () => {
    render(Select, { value: 'accounts', options, ariaLabel: 'Section' });
    const { behind, onClick } = elementBehind();
    await openSelect();

    const option = await screen.findByRole('option', { name: 'Portfolio' });
    await fireEvent.pointerUp(option, { pointerType: 'touch' });
    // A new tap starts with its own pointerdown; the ghost click never does.
    await fireEvent.pointerDown(behind, { pointerType: 'touch' });
    await fireEvent.click(behind);

    expect(onClick).toHaveBeenCalledTimes(1);
    behind.remove();
  });

  it('stops swallowing once the ghost click could no longer arrive', async () => {
    render(Select, { value: 'accounts', options, ariaLabel: 'Section' });
    const { behind, onClick } = elementBehind();
    await openSelect();

    const option = await screen.findByRole('option', { name: 'Portfolio' });
    vi.useFakeTimers();
    await fireEvent.pointerUp(option, { pointerType: 'touch' });
    vi.advanceTimersByTime(1000);
    await fireEvent.click(behind);

    expect(onClick).toHaveBeenCalledTimes(1);
    behind.remove();
  });
});
