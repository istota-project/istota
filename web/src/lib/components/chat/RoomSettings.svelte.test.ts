import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';

// The component asks the autocomplete providers for the model dropdown on
// mount. Nothing here is about models, and the real provider reaches the API.
vi.mock('$lib/components/chat/autocomplete/providers', () => ({
  getBaseModelChoices: vi.fn(async () => []),
}));

import RoomSettings from './RoomSettings.svelte';
import type { ChatRoom } from '$lib/api';

function room(overrides: Partial<ChatRoom> = {}): ChatRoom {
  return {
    id: 1,
    token: 'web-alice-abc',
    name: 'general',
    archived: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    origin: 'web',
    ...overrides,
  };
}

function mount(r: ChatRoom) {
  return render(RoomSettings, {
    props: {
      open: true,
      room: r,
      onSave: vi.fn(),
      onDelete: vi.fn(),
      onPromote: vi.fn(),
      onClose: vi.fn(),
    },
  });
}

const TALK_LINE = /also open in Nextcloud Talk/i;
const PROMOTE_LABEL = /^Also open in Talk$/i;
const RECONNECT_LABEL = /^Reconnect to Talk$/i;

afterEach(() => cleanup());

// ISSUE-342. A promoted room keeps `origin: 'web'`, so `talk_token` is the only
// thing that can say it is on Talk. The listing never sent that key, and the
// room-list refresh writes it unconditionally — so a poll erased what the
// promote response had just set, and the room reverted to offering a promote
// the backend then refuses.
describe('RoomSettings — Talk state', () => {
  it('shows the Talk line for a promoted room', () => {
    mount(room({ origin: 'web', talk_token: 'tk4ab9cd' }));
    expect(screen.getByText(TALK_LINE)).toBeTruthy();
    expect(screen.queryByRole('button', { name: PROMOTE_LABEL })).toBeNull();
  });

  it('shows the Talk line for a Talk-origin room', () => {
    mount(room({ origin: 'talk', token: 'cpz', talk_token: 'cpz' }));
    expect(screen.getByText(TALK_LINE)).toBeTruthy();
  });

  // ISSUE-401. A promoted room's binding can go stale — the Talk conversation
  // deleted out from under it — and this button is the only way back. Hiding it
  // once `talk_token` was set is what made that state permanent from the app.
  it('offers a reconnect button for a promoted room, alongside the Talk line', () => {
    mount(room({ origin: 'web', talk_token: 'tk4ab9cd' }));
    expect(screen.getByText(TALK_LINE)).toBeTruthy();
    const btn = screen.getByRole('button', { name: RECONNECT_LABEL }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it('calls onPromote when the reconnect button is pressed', async () => {
    const onPromote = vi.fn();
    render(RoomSettings, {
      props: {
        open: true,
        room: room({ origin: 'web', talk_token: 'tk4ab9cd' }),
        onSave: vi.fn(),
        onDelete: vi.fn(),
        onPromote,
        onClose: vi.fn(),
      },
    });
    await fireEvent.click(screen.getByRole('button', { name: RECONNECT_LABEL }));
    expect(onPromote).toHaveBeenCalledTimes(1);
  });

  // A Talk-origin room's binding names its own canonical token, so there is
  // nothing here to repair and no second conversation to mint.
  it('offers no promote or reconnect button for a Talk-origin room', () => {
    mount(room({ origin: 'talk', token: 'cpz', talk_token: 'cpz' }));
    expect(screen.queryByRole('button', { name: PROMOTE_LABEL })).toBeNull();
    expect(screen.queryByRole('button', { name: RECONNECT_LABEL })).toBeNull();
  });

  it('offers the plain promote button, not reconnect, for an unbound room', () => {
    mount(room({ origin: 'web', talk_token: null }));
    expect(screen.queryByRole('button', { name: RECONNECT_LABEL })).toBeNull();
    expect(screen.getByRole('button', { name: PROMOTE_LABEL })).toBeTruthy();
  });

  it('shows no Talk line and an enabled button for an unbound room', () => {
    mount(room({ origin: 'web', talk_token: null }));
    expect(screen.queryByText(TALK_LINE)).toBeNull();
    const btn = screen.getByRole('button', { name: PROMOTE_LABEL }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it('treats an absent talk_token the same as a null one', () => {
    // An older backend sends neither key; the room is web-only and promotable.
    mount(room({ origin: 'web' }));
    expect(screen.getByRole('button', { name: PROMOTE_LABEL })).toBeTruthy();
  });
});
