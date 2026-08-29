import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';

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
const PROMOTE_LABEL = /Also open in Talk/i;

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

  it('offers the promote button only for a room with no Talk binding', () => {
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
