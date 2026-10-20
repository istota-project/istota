import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import PlaceForm from './PlaceForm.svelte';
import { pressEnter } from '$lib/test/implicitSubmit';

afterEach(cleanup);

const place = {
  id: 1,
  name: 'Office',
  lat: 1,
  lon: 2,
  radius_meters: 100,
  category: 'work',
  notes: '',
} as never;

function mount() {
  const onSave = vi.fn();
  render(PlaceForm, { place, onSave, onCancel: vi.fn() });
  return onSave;
}

describe('PlaceForm Enter to save', () => {
  it('saves on Enter in a single-line text input', async () => {
    const onSave = mount();
    await pressEnter(screen.getByPlaceholderText('e.g. Office, Gym...'));
    expect(onSave).toHaveBeenCalledOnce();
  });

  // Enter on the bits-ui trigger opens or confirms the dropdown; it used to
  // save the form in the same keystroke.
  it('does not save on Enter from the category Select trigger', async () => {
    const onSave = mount();
    const trigger = screen.getByRole('button', { name: 'Category' });
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(onSave).not.toHaveBeenCalled();
  });

  it('does not save on Enter from the radius slider or the notes', async () => {
    const onSave = mount();
    await pressEnter(screen.getByRole('slider'));
    await fireEvent.keyDown(screen.getByPlaceholderText('Anything to remember about this place'), {
      key: 'Enter',
    });
    expect(onSave).not.toHaveBeenCalled();
  });
});
