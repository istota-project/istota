import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import NewIncognitoRoomDialog from './NewIncognitoRoomDialog.svelte';

afterEach(cleanup);

function button(name: RegExp) {
  return screen.getByRole('button', { name }) as HTMLButtonElement;
}

describe('NewIncognitoRoomDialog', () => {
  it('says the room is permanent before anything is created', () => {
    render(NewIncognitoRoomDialog, { open: true, onCreate: vi.fn() });
    expect(screen.getByText('This is permanent.')).toBeTruthy();
    expect(button(/create incognito room/i).disabled).toBe(true);
  });

  it('creates the room under the typed name', async () => {
    const onCreate = vi.fn(async () => {});
    render(NewIncognitoRoomDialog, { open: true, onCreate });
    await fireEvent.input(screen.getByPlaceholderText('Room name…'), {
      target: { value: '  Private  ' },
    });
    await fireEvent.click(button(/create incognito room/i));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith('Private'));
  });

  it('keeps the dialog and says so when the create fails', async () => {
    const onCreate = vi.fn(async () => {
      throw new Error('nope');
    });
    render(NewIncognitoRoomDialog, { open: true, onCreate });
    await fireEvent.input(screen.getByPlaceholderText('Room name…'), {
      target: { value: 'Private' },
    });
    await fireEvent.click(button(/create incognito room/i));
    await waitFor(() => expect(screen.getByText('The room could not be created.')).toBeTruthy());
  });
});
