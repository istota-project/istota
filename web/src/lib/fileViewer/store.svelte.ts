type ViewerState =
  | { mode: 'closed' }
  | { mode: 'file'; path: string }
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
  openFile(path: string) {
    rememberFocus();
    state = { mode: 'file', path };
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
