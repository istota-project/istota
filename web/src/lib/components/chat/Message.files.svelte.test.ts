import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));
import { chatFileUrl } from '$lib/api';
import { viewer } from '$lib/fileViewer/store.svelte';
import type { ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

const path = '/Users/alice/istota/note.md';
const url = chatFileUrl(path);
const callbacks = { onConfirm: () => {}, onReject: () => {} };
function assistant(body: string): ChatMessage {
  return {
    cid: 1,
    role: 'assistant',
    text: '',
    streaming: false,
    segments: [{ kind: 'text', id: 't0', text: body, settled: true }],
  };
}
afterEach(() => {
  cleanup();
  viewer.close();
  vi.restoreAllMocks();
});

describe('workspace file clicks in messages', () => {
  it.each(['assistant', 'system'] as const)(
    'opens a %s body link and cancels navigation',
    async (role) => {
      const open = vi.spyOn(viewer, 'openFile');
      const body = `[**Read note**](${url})`;
      const message =
        role === 'assistant'
          ? assistant(body)
          : {
              cid: 2,
              role,
              text: body,
              segments: [],
              streaming: false,
            };
      const { getByText } = render(Message, { ...callbacks, message });
      expect(await fireEvent.click(getByText('Read note'))).toBe(false);
      expect(open).toHaveBeenCalledExactlyOnceWith(path);
    },
  );

  it('preserves Ctrl-click and already claimed clicks', async () => {
    const open = vi.spyOn(viewer, 'openFile');
    const { getByRole } = render(Message, { ...callbacks, message: assistant(`[Read](${url})`) });
    const link = getByRole('link', { name: 'Read' });
    expect(await fireEvent.click(link, { ctrlKey: true })).toBe(true);
    const event = new MouseEvent('click', { bubbles: true, cancelable: true });
    event.preventDefault();
    await fireEvent(link, event);
    expect(open).not.toHaveBeenCalled();
  });

  it('opens an upload chip while retaining its download name and inert siblings', async () => {
    const open = vi.spyOn(viewer, 'openFile');
    const { getByRole, getByText } = render(Message, {
      ...callbacks,
      message: {
        cid: 3,
        role: 'user',
        text: '',
        segments: [],
        streaming: false,
        attachments: ['note.md', 'unavailable.txt'],
        attachmentPaths: [path, null],
      },
    });
    const chip = getByRole('link', { name: /note.md/ });
    expect(chip.getAttribute('download')).toBe('note.md');
    expect(await fireEvent.click(chip, { ctrlKey: true })).toBe(true);
    expect(open).not.toHaveBeenCalled();
    expect(await fireEvent.click(chip)).toBe(false);
    expect(open).toHaveBeenCalledExactlyOnceWith(path);
    expect(getByText(/unavailable.txt/).closest('a')).toBeNull();
  });

  it('keeps inline images with onImageOpen and uses a linked image’s anchor target', async () => {
    const open = vi.spyOn(viewer, 'openFile');
    const onImageOpen = vi.fn();
    const src = chatFileUrl('/Users/alice/istota/image.png');
    const { getByAltText } = render(Message, {
      ...callbacks,
      onImageOpen,
      message: assistant(`![Plain](${src})\n\n[![Linked](${src})](${url})`),
    });
    await fireEvent.click(getByAltText('Plain'));
    expect(onImageOpen).toHaveBeenCalledExactlyOnceWith([src], 0);
    expect(open).not.toHaveBeenCalled();
    expect(await fireEvent.click(getByAltText('Linked'))).toBe(false);
    expect(open).toHaveBeenCalledExactlyOnceWith(path);
    expect(onImageOpen).toHaveBeenCalledTimes(1);
  });
});
