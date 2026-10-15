/**
 * Where a viewed file's bytes come from. A workspace path goes through
 * `/chat/files`; a Nextcloud path is relative to the caller's own account and
 * is read with their personal connection. A `url` source is an app endpoint
 * that serves one known type; it is trusted, not sniffed through a preview call.
 */
export type FileSource =
  | { kind: 'workspace' }
  | { kind: 'nextcloud'; webUrl: string | null }
  | { kind: 'url'; url: string; name: string; mime: 'application/pdf' };

type ViewerState =
  | { mode: 'closed' }
  | { mode: 'file'; path: string; source: FileSource }
  | { mode: 'images'; images: string[]; index: number };

let state = $state<ViewerState>({ mode: 'closed' });
let returnFocus: HTMLElement | null = null;

function rememberFocus() {
  if (state.mode === 'closed' && typeof document !== 'undefined') {
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }
}

export const viewer = {
  get state() {
    return state;
  },
  openFile(path: string, source: FileSource = { kind: 'workspace' }) {
    rememberFocus();
    state = { mode: 'file', path, source };
  },
  openImages(images: string[], index: number) {
    if (!images.length) return;
    rememberFocus();
    state = { mode: 'images', images, index: Math.max(0, Math.min(index, images.length - 1)) };
  },
  close() {
    state = { mode: 'closed' };
    const target = returnFocus;
    returnFocus = null;
    // Modal's teardown first removes the focus trap and background inertness.
    setTimeout(() => {
      if (state.mode === 'closed' && target?.isConnected) target.focus();
    }, 0);
  },
};
