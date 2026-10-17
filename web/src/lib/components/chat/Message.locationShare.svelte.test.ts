/**
 * An answer carrying location shares renders one map card per share under its
 * body, in the order the server sent them. An answer without any renders as
 * before.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import type { ChatMessage, LocationShareCardData } from '$lib/stores/segments';
import Message from './Message.svelte';

// MapLibre never finishes loading here: the card is drawn with its map frame
// and nothing reaches for WebGL.
vi.mock('maplibre-gl', () => new Promise(() => {}));

afterEach(cleanup);

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

function share(cardId: number, ownerLabel: string, lat: number): LocationShareCardData {
  return {
    cardId,
    ownerLabel,
    mapUrl: `https://www.openstreetmap.org/?mlat=${lat}&mlon=13.4`,
    observation: {
      precision: 'exact',
      lat,
      lon: 13.4,
      accuracy_m: 25,
      observed_at: '2026-10-16T14:02:31+00:00',
      freshness: 'fresh',
      owner_user_id: ownerLabel.toLowerCase(),
    },
  };
}

function answer(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 1,
    role: 'assistant',
    text: 'Bob is here.',
    segments: [{ kind: 'text', text: 'Bob is here.' }],
    streaming: false,
    msgId: 9,
    ...over,
  } as ChatMessage;
}

const cards = (c: HTMLElement) =>
  Array.from(c.querySelectorAll<HTMLElement>('[data-testid="location-share-card"]'));

describe('an answer with location shares', () => {
  it('renders the cards under the body, in order', () => {
    const { container } = render(Message, {
      ...base,
      message: answer({ locationShares: [share(1, 'Bob', 52.5), share(2, 'Carol', 48.1)] }),
    });
    const found = cards(container);
    expect(found.map((c) => c.querySelector('.location-share-owner')?.textContent)).toEqual([
      'Bob',
      'Carol',
    ]);
    const body = container.querySelector('.body.markdown');
    expect(body?.textContent).toContain('Bob is here.');
    expect(body!.compareDocumentPosition(found[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders no card on an ordinary answer', () => {
    const { container } = render(Message, { ...base, message: answer() });
    expect(cards(container)).toHaveLength(0);
  });

  it('renders no card on a user row, whatever it carries', () => {
    const { container } = render(Message, {
      ...base,
      message: answer({ role: 'user', locationShares: [share(1, 'Bob', 52.5)] }),
    });
    expect(cards(container)).toHaveLength(0);
  });
});
