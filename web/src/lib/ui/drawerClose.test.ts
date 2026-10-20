import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createDrawerClose } from './drawerClose';

let frames: FrameRequestCallback[] = [];

function runFrame() {
  const pending = frames;
  frames = [];
  for (const callback of pending) callback(0);
}

beforeEach(() => {
  frames = [];
  let id = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.push(callback);
    return ++id;
  });
  vi.stubGlobal('cancelAnimationFrame', () => {
    frames = [];
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function drawer(initiallyOpen = true) {
  const state = { open: initiallyOpen };
  const close = createDrawerClose(
    () => state.open,
    () => (state.open = false),
  );
  return { state, close };
}

describe('createDrawerClose', () => {
  it('closes at once and runs the work after the second frame', () => {
    const { state, close } = drawer();
    const work = vi.fn();
    close.run(work);
    expect(state.open).toBe(false);
    expect(work).not.toHaveBeenCalled();
    runFrame();
    expect(work).not.toHaveBeenCalled();
    runFrame();
    expect(work).toHaveBeenCalledOnce();
  });

  it('keeps only the last of several calls made before the frames run', () => {
    const { close } = drawer();
    const first = vi.fn();
    const second = vi.fn();
    close.run(first);
    close.run(second);
    runFrame();
    runFrame();
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledOnce();
  });

  it('runs the work straight away when the drawer is not open', () => {
    const { close } = drawer(false);
    const work = vi.fn();
    close.run(work);
    expect(work).toHaveBeenCalledOnce();
    expect(frames).toHaveLength(0);
  });

  it('is not a thenable, so awaiting it cannot close the drawer', async () => {
    const { state, close } = drawer();
    expect(await Promise.resolve(close)).toBe(close);
    expect(state.open).toBe(true);
  });

  it('holds back a plain link click with the drawer open and follows it after the frames', () => {
    const { state, close } = drawer();
    const event = new MouseEvent('click', { button: 0, cancelable: true });
    const follow = vi.fn();
    close.link(event, follow);
    expect(event.defaultPrevented).toBe(true);
    expect(state.open).toBe(false);
    runFrame();
    runFrame();
    expect(follow).toHaveBeenCalledOnce();
  });

  it('leaves a modified click, or a closed drawer, to the browser', () => {
    const { state, close } = drawer();
    const modified = new MouseEvent('click', { button: 0, metaKey: true, cancelable: true });
    const follow = vi.fn();
    close.link(modified, follow);
    expect(modified.defaultPrevented).toBe(false);
    expect(state.open).toBe(false);

    const plain = new MouseEvent('click', { button: 0, cancelable: true });
    close.link(plain, follow);
    expect(plain.defaultPrevented).toBe(false);
    expect(follow).not.toHaveBeenCalled();
    expect(frames).toHaveLength(0);
  });

  it('drops the waiting work on cancel', () => {
    const { close } = drawer();
    const work = vi.fn();
    close.run(work);
    close.cancel();
    runFrame();
    runFrame();
    expect(work).not.toHaveBeenCalled();
  });
});
