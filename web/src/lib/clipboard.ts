import { notifyError, notifySuccess } from './stores/notices';

/**
 * Write text to the clipboard and say so.
 *
 * A copy is the textbook out-of-band notice: it succeeds silently and leaves
 * nothing on screen to show it worked, so without the confirmation the only
 * way to tell a copy from a misfire is to paste somewhere and look. The
 * failure path matters more than it looks — `navigator.clipboard` is absent
 * outside a secure context, so on a plain-http deployment every copy would
 * otherwise do nothing at all, indistinguishably from working.
 *
 * All copies share one notice key, so mashing the button counts rather than
 * queueing five identical confirmations behind each other.
 */
export const COPY_NOTICE_KEY = 'clipboard:copy';

export async function copyText(text: string, options: { label?: string } = {}): Promise<boolean> {
  const label = options.label ?? 'Copied';

  // Nothing to copy. Claiming success here would be a lie, and the realistic
  // way to reach it is a block whose text hasn't arrived yet.
  if (!text.trim()) {
    notifyError('Nothing to copy.', { key: COPY_NOTICE_KEY });
    return false;
  }

  const clipboard = navigator.clipboard;
  if (!clipboard?.writeText) {
    notifyError("This browser won't allow copying here.", { key: COPY_NOTICE_KEY });
    return false;
  }

  try {
    await clipboard.writeText(text);
  } catch {
    // A rejection is a permission denial or a document that wasn't focused.
    // Neither is worth its own wording — the user's move is the same.
    notifyError("Couldn't copy that.", { key: COPY_NOTICE_KEY });
    return false;
  }

  notifySuccess(label, { key: COPY_NOTICE_KEY });
  return true;
}
