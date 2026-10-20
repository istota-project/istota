import { isPlainClick } from './plainClick';

/**
 * Close a sidebar drawer, then do the work that goes with closing it (switch a
 * room, follow a section link) once the slide has started.
 *
 * Closing and swapping the content in one tick hands the browser a large
 * render in the same frame as the style change, so the drawer jumps shut and
 * the page changes all at once (ISSUE-805). Waiting two frames lets the first
 * moving frame be committed first; the swap then renders under a drawer
 * already on its way out. It does not wait for `transitionend`, which would
 * make every tap feel late.
 *
 * Calls made while the work waits replace it: the last selection wins. With
 * the drawer not open (a desktop sidebar) the work runs at once.
 */
export interface DrawerClose {
  run(work: () => void): void;
  /** A `SidebarNav` link click: a plain one with the drawer open is held back
   *  and `follow`ed after the slide starts; any other closes the drawer and
   *  leaves the link to the browser. */
  link(event: MouseEvent, follow: () => void): void;
  /** Drop waiting work. Call it on unmount. */
  cancel(): void;
}

export function createDrawerClose(isOpen: () => boolean, close: () => void): DrawerClose {
  let pending: (() => void) | null = null;
  let frame = 0;

  function cancel() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    pending = null;
  }

  function run(work: () => void) {
    if (!frame && !isOpen()) {
      work();
      return;
    }
    close();
    pending = work;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        frame = 0;
        const next = pending;
        pending = null;
        next?.();
      });
    });
  }

  function link(event: MouseEvent, follow: () => void) {
    if (!isPlainClick(event) || (!frame && !isOpen())) {
      close();
      return;
    }
    event.preventDefault();
    run(follow);
  }

  return { run, link, cancel };
}
