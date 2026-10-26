/**
 * Counting an app frame's `load` events, to notice it navigating itself.
 *
 * A sandboxed frame without `allow-top-navigation` may still set its own
 * `location` to any URL, with data in the query string, and no CSP directive
 * stops it. The first `load` is the installed document; any later one means
 * the frame went somewhere else. The data has left by then, so the defence is
 * the record and the frame's removal, not prevention.
 *
 * The first `load` is also when the bridge hands the installed document its
 * port (`onFirstLoad`), and the bridge answers nothing once `navigations`
 * has moved.
 *
 * No Svelte imports: the escape tier's shell page (stage 11) imports this file
 * and `bridge.ts` as they are.
 */
export interface FrameWatch {
  /** Loads after the first: 0 while the installed document is the one in the frame. */
  readonly navigations: number;
  /** True once the frame has loaded anything after its first document. */
  readonly navigated: boolean;
  dispose(): void;
}

export function watchFrame(
  frame: HTMLIFrameElement,
  onNavigated: () => void,
  onFirstLoad?: () => void,
): FrameWatch {
  let loads = 0;
  let fired = false;

  function onLoad() {
    loads += 1;
    if (loads === 1) {
      onFirstLoad?.();
      return;
    }
    if (!fired) {
      fired = true;
      onNavigated();
    }
  }

  frame.addEventListener('load', onLoad);
  return {
    get navigations() {
      return Math.max(0, loads - 1);
    },
    get navigated() {
      return loads > 1;
    },
    dispose() {
      frame.removeEventListener('load', onLoad);
    },
  };
}
