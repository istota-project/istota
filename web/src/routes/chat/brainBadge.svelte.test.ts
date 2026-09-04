/**
 * The room's brain pin in the chat header.
 *
 * It sits beside the model badge and is set in the same modal, so the one
 * thing it cannot afford is to read as another model badge. Two assertions
 * here are about that and not about wording: the badge appears only for a room
 * that actually pins a brain (an unpinned room runs the deployment's own, which
 * is not an override and is the same answer everywhere), and it is drawn in a
 * different color from the model badge's muted gray.
 *
 * The color assertion reads the `<style>` block rather than a computed style:
 * jsdom applies no component CSS, so `getComputedStyle` returns the empty
 * string for both badges and an equality check between them passes no matter
 * what either rule says. Same reason `offlineBanner.svelte.test.ts` reads the
 * source for its padding claim.
 *
 * The label falls back to the raw kind, and that path is not an edge case: the
 * catalogue is the operator's `room_selectable` allowlist and is empty outright
 * for a non-admin, who can sit in a pinned room without being able to write
 * one. Hiding the badge there would answer "which brain is this room on" with
 * silence for exactly the people who cannot find out any other way.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

// `vi.hoisted`, because `vi.mock`'s factory is lifted above every top-level
// declaration in the file and would otherwise read this before it exists.
const { brainCatalogue } = vi.hoisted(() => ({
  brainCatalogue: vi.fn(async () => [
    { kind: 'native', label: 'Native', model_namespace: 'native' },
    { kind: 'claude_code', label: 'Claude Code', model_namespace: 'claude' },
  ]),
}));

vi.mock('$lib/components/chat/autocomplete/providers', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, getSelectableBrains: () => brainCatalogue() };
});

vi.mock('$lib/stores/chat', async () => {
  const { writable } = await import('svelte/store');
  const stores: Record<string, unknown> = {
    rooms: writable([]),
    activeRoomId: writable(1),
    messages: writable([]),
    status: writable('idle'),
    loaded: writable(true),
    hasMore: writable(false),
    loadingOlder: writable(false),
    view: writable('room'),
    scrollTarget: writable(null),
    sendSettled: writable({ n: 0, token: null }),
    sendReturned: writable({ n: 0, token: null, text: '', attachments: [] }),
    outboundDrafts: writable([]),
    externalTurnDisplay: writable('full'),
    offlineTranscript: writable(false),
    queuedCounts: writable({}),
  };
  const session = new Proxy(stores, {
    get: (target, key: string) => (target[key] ??= vi.fn(async () => undefined)),
  });
  return { getChatSession: () => session };
});

import { getChatSession } from '$lib/stores/chat';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { User } from '$lib/api';

const person: User = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: true,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: false,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: true,
  },
};

const room = (over: Record<string, unknown> = {}) => ({
  id: 1,
  token: 'web-1',
  name: 'Testing',
  origin: 'web',
  talk_token: null,
  model: null,
  effort: null,
  brain: null,
  unread: 0,
  last_activity: new Date().toISOString(),
  ...over,
});

/** The header's badges, keyed by the class the markup gives them. */
const badge = (cls: string) => document.querySelector<HTMLElement>(`.${cls}`);

const setRoom = (over: Record<string, unknown> = {}) => {
  const session = getChatSession() as unknown as {
    rooms: { set: (v: unknown) => void };
  };
  session.rooms.set([room(over)]);
};

const renderPage = () => render(Harness, { component: Page, user: person });

beforeEach(() => {
  brainCatalogue.mockClear();
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('the room brain badge', () => {
  it('is absent for a room that pins no brain', async () => {
    setRoom({ brain: null });
    renderPage();
    // Waited on rather than read once: the catalogue resolves a tick later, and
    // asserting before it lands would pass against a badge that appears after.
    await waitFor(() => expect(brainCatalogue).toHaveBeenCalled());
    expect(badge('brain-badge')).toBeNull();
  });

  it('names the pinned brain, by its display label', async () => {
    setRoom({ brain: 'native' });
    renderPage();
    await waitFor(() => expect(badge('brain-badge')).not.toBeNull());
    expect(badge('brain-badge')!.textContent!.trim()).toBe('Native');
  });

  it('falls back to the raw kind when the catalogue does not name it', async () => {
    // A non-admin (empty catalogue), or an admin whose operator has since
    // dropped the pinned kind from `room_selectable`.
    brainCatalogue.mockResolvedValueOnce([]);
    setRoom({ brain: 'native' });
    renderPage();
    await waitFor(() => expect(badge('brain-badge')).not.toBeNull());
    expect(badge('brain-badge')!.textContent!.trim()).toBe('native');
  });

  it('offers no click-to-change where the user cannot change it', async () => {
    // The empty catalogue is the server collapsing "the operator listed no
    // kinds" and "you may not write one" into one answer, and `RoomSettings`
    // gates its whole brain control on the same emptiness — so the modal this
    // would open has no brain field in it. A button saying `click to change`
    // there promises an edit that does not exist.
    brainCatalogue.mockResolvedValueOnce([]);
    setRoom({ brain: 'native' });
    renderPage();
    await waitFor(() => expect(badge('brain-badge')).not.toBeNull());
    const el = badge('brain-badge')!;
    expect(el.tagName).toBe('SPAN');
    expect(el.getAttribute('title')).toBe('Room brain');
  });

  it('is a button that opens the settings where the user can change it', async () => {
    setRoom({ brain: 'native' });
    renderPage();
    await waitFor(() => expect(badge('brain-badge')).not.toBeNull());
    const el = badge('brain-badge')!;
    expect(el.tagName).toBe('BUTTON');
    expect(el.getAttribute('title')).toMatch(/click to change/);
  });

  it('never renders the raw kind on the way to rendering the label', async () => {
    // The badge is gated on the catalogue having settled, so there is no first
    // paint showing `claude_code` that flips to `Claude Code` a microtask
    // later. Held by resolving the catalogue late and watching what appears.
    type Brains = Awaited<ReturnType<typeof brainCatalogue>>;
    let release!: (v: Brains) => void;
    brainCatalogue.mockReturnValueOnce(new Promise<Brains>((resolve) => (release = resolve)));
    setRoom({ brain: 'claude_code' });
    renderPage();
    // Nothing at all while the catalogue is outstanding — not the raw kind.
    await Promise.resolve();
    expect(badge('brain-badge')).toBeNull();
    release([{ kind: 'claude_code', label: 'Claude Code', model_namespace: 'claude' }]);
    await waitFor(() => expect(badge('brain-badge')).not.toBeNull());
    expect(badge('brain-badge')!.textContent!.trim()).toBe('Claude Code');
  });

  it('still shows the badge when the catalogue request fails outright', async () => {
    // `loadCatalogue` caches its promise for the life of the session, so one
    // rejection is permanent. The pin is still a fact about the room.
    brainCatalogue.mockRejectedValueOnce(new Error('offline'));
    setRoom({ brain: 'native' });
    renderPage();
    await waitFor(() => expect(badge('brain-badge')).not.toBeNull());
    expect(badge('brain-badge')!.textContent!.trim()).toBe('native');
  });

  it('stands beside the model badge without replacing it', async () => {
    setRoom({ brain: 'native', model: 'claude-opus-4-8' });
    renderPage();
    await waitFor(() => expect(badge('brain-badge')).not.toBeNull());
    const badges = [...document.querySelectorAll('.model-badge')];
    expect(badges).toHaveLength(2);
    // Brain first: the model name is resolved under the brain, so it reads in
    // the order it is decided.
    expect(badges[0].classList.contains('brain-badge')).toBe(true);
    expect(badges[1].textContent!.trim()).toBe('claude-opus-4-8');
  });

  it('is drawn in a different color from the model badge', () => {
    const source = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), '+page.svelte'),
      'utf8',
    );
    const ruleFor = (sel: string) => {
      const m = source.match(new RegExp(`\\n {2}\\${sel} \\{([\\s\\S]*?)\\n {2}\\}`));
      expect(m, `${sel} rule not found in +page.svelte`).not.toBeNull();
      return m![1];
    };
    const modelColor = ruleFor('.model-badge')
      .match(/\n\s*color:\s*([^;]+);/)![1]
      .trim();
    const brainColor = ruleFor('.brain-badge')
      .match(/\n\s*color:\s*([^;]+);/)![1]
      .trim();
    expect(modelColor).toBe('var(--text-muted)');
    expect(brainColor).not.toBe(modelColor);
    // A token, not a literal — the badge has to work in both themes, and both
    // themes define this one.
    expect(brainColor).toMatch(/^var\(--[a-z-]+\)$/);
  });
});
