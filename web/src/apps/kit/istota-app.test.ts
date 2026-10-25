import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

const KIT = join(process.cwd(), 'src', 'apps', 'kit');

type Message = Record<string, unknown>;
type Istota = {
  kv: {
    get(key: string): Promise<unknown>;
    set(key: string, value: unknown): Promise<unknown>;
    delete(key: string): Promise<unknown>;
    list(options?: Record<string, unknown>): Promise<unknown>;
  };
  files: {
    list(path?: string): Promise<unknown>;
    read(path: string, options?: Record<string, unknown>): Promise<unknown>;
    write(path: string, content: string, options?: Record<string, unknown>): Promise<unknown>;
    delete(path: string): Promise<unknown>;
  };
  env: Record<string, unknown>;
  on(event: string, fn: (env: Record<string, unknown>) => void): () => void;
  copy(text: string): Promise<unknown>;
  IstotaError: new (code: string, message: string) => Error & { code: string };
  ERROR_CODES: readonly string[];
};

const SOURCE = readFileSync(join(KIT, 'istota-app.js'), 'utf8');
const listeners: Array<[string, EventListener]> = [];
let posted: MockInstance;
let istota: Istota;

function load(hash = ''): Istota {
  window.history.replaceState(null, '', `/frame${hash}`);
  const add = window.addEventListener.bind(window);
  const spy = vi
    .spyOn(window, 'addEventListener')
    .mockImplementation((type: string, fn: EventListenerOrEventListenerObject, opts?: unknown) => {
      listeners.push([type, fn as EventListener]);
      add(type, fn, opts as AddEventListenerOptions);
    });
  new Function(SOURCE)();
  spy.mockRestore();
  return (globalThis as unknown as { istota: Istota }).istota;
}

function sent(): Message[] {
  return posted.mock.calls.map((call) => call[0] as Message);
}

function last(): Message {
  const all = sent();
  return all[all.length - 1];
}

function reply(data: unknown, source: Window | null = window): void {
  window.dispatchEvent(new MessageEvent('message', { data, source }));
}

beforeEach(() => {
  posted = vi.spyOn(window, 'postMessage').mockImplementation(() => {});
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('data-font-size');
});

afterEach(() => {
  for (const [type, fn] of listeners.splice(0)) window.removeEventListener(type, fn);
  delete (globalThis as unknown as { istota?: Istota }).istota;
  document.querySelectorAll('.istota-notice').forEach((node) => node.remove());
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('the starting environment', () => {
  it('reads the fragment and applies theme and text scale before first paint', () => {
    istota = load('#theme=light&scale=large&shell=ios&v=1');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(document.documentElement.getAttribute('data-font-size')).toBe('large');
    expect(istota.env).toMatchObject({ theme: 'light', scale: 'large', shell: 'ios' });
  });

  it('accepts the short scale names and folds unknown values onto the defaults', () => {
    istota = load('#theme=purple&scale=lg&shell=tv');
    expect(istota.env).toMatchObject({ theme: 'dark', scale: 'large', shell: 'web' });
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
  });

  it('starts visible, online and with no keyboard', () => {
    istota = load();
    expect(istota.env).toMatchObject({ visibility: 'visible', online: true, keyboard: false });
  });
});

describe('calls', () => {
  beforeEach(() => {
    istota = load('#theme=dark');
  });

  it('sends each call with its own request id, the capability and the op', () => {
    void istota.kv.get('a');
    void istota.kv.set('b', { n: 1 });
    void istota.kv.delete('c');
    void istota.kv.list({ prefix: 'card:', limit: 10 });
    void istota.files.list('notes');
    void istota.files.read('x.txt', { encoding: 'base64' });
    void istota.files.write('y.txt', 'hi');
    void istota.files.delete('z.txt');
    const messages = sent();
    expect(messages.map((m) => [m.capability, m.op])).toEqual([
      ['kv:own', 'get'],
      ['kv:own', 'set'],
      ['kv:own', 'delete'],
      ['kv:own', 'list'],
      ['files:own', 'list'],
      ['files:own', 'read'],
      ['files:own', 'write'],
      ['files:own', 'delete'],
    ]);
    expect(new Set(messages.map((m) => m.id)).size).toBe(messages.length);
    expect(messages.every((m) => m.istota === 1 && m.kind === 'call')).toBe(true);
    expect(messages[1].args).toEqual({ key: 'b', value: { n: 1 } });
    expect(messages[3].args).toEqual({ prefix: 'card:', limit: 10 });
    expect(messages[5].args).toEqual({ path: 'x.txt', encoding: 'base64' });
    expect(messages[6].args).toEqual({ path: 'y.txt', content: 'hi', encoding: 'utf8' });
  });

  it('posts to the parent with the wildcard target an opaque origin needs', () => {
    void istota.kv.get('a');
    expect(posted.mock.calls[0][1]).toBe('*');
  });

  it('resolves with the result of the matching reply', async () => {
    const first = istota.kv.get('a');
    const firstId = last().id;
    const second = istota.kv.get('b');
    reply({ istota: 1, id: last().id, ok: true, result: 'B' });
    reply({ istota: 1, id: firstId, ok: true, result: 'A' });
    await expect(first).resolves.toBe('A');
    await expect(second).resolves.toBe('B');
  });

  it('rejects with a typed error', async () => {
    const call = istota.kv.set('k', 'v');
    reply({ istota: 1, id: last().id, ok: false, error: { code: 'quota', message: 'Too big.' } });
    const error = await call.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(istota.IstotaError);
    expect(error).toMatchObject({ code: 'quota', message: 'Too big.' });
  });

  it('maps an unknown error code to internal', async () => {
    const call = istota.kv.get('k');
    reply({ istota: 1, id: last().id, ok: false, error: { code: 'nope', message: 'x' } });
    await expect(call).rejects.toMatchObject({ code: 'internal' });
  });

  it('lists the error codes the spec names', () => {
    expect([...istota.ERROR_CODES].sort()).toEqual(
      [
        'not_granted',
        'bad_args',
        'quota',
        'rate_limited',
        'stale_version',
        'not_found',
        'offline',
        'timeout',
        'internal',
      ].sort(),
    );
  });

  it('times out after 15 seconds with no reply', async () => {
    vi.useFakeTimers();
    const call = istota.kv.get('k');
    const settled = call.catch((e: unknown) => e);
    vi.advanceTimersByTime(14_999);
    reply({ istota: 1, id: 'other', ok: true, result: 1 });
    vi.advanceTimersByTime(1);
    await expect(settled).resolves.toMatchObject({ code: 'timeout' });
  });

  it('ignores a late reply once timed out', async () => {
    vi.useFakeTimers();
    const call = istota.kv.get('k');
    const id = last().id;
    const settled = call.catch((e: unknown) => e);
    vi.advanceTimersByTime(15_000);
    expect(() => reply({ istota: 1, id, ok: true, result: 1 })).not.toThrow();
    await expect(settled).resolves.toMatchObject({ code: 'timeout' });
  });

  it('drops a reply from any window other than the parent', async () => {
    vi.useFakeTimers();
    const call = istota.kv.get('k');
    const settled = call.catch((e: unknown) => e);
    reply({ istota: 1, id: last().id, ok: true, result: 'forged' }, null);
    vi.advanceTimersByTime(15_000);
    await expect(settled).resolves.toMatchObject({ code: 'timeout' });
  });

  it('drops a malformed reply', async () => {
    vi.useFakeTimers();
    const call = istota.kv.get('k');
    const settled = call.catch((e: unknown) => e);
    const id = last().id;
    reply({ istota: 2, id, ok: true, result: 1 });
    reply({ id, ok: true, result: 1 });
    reply('{"istota":1}');
    reply({ istota: 1, id, ok: 'yes', result: 1 });
    vi.advanceTimersByTime(15_000);
    await expect(settled).resolves.toMatchObject({ code: 'timeout' });
  });

  it('says in the frame that the app was updated on stale_version', async () => {
    const call = istota.kv.get('k');
    reply({ istota: 1, id: last().id, ok: false, error: { code: 'stale_version', message: '' } });
    await call.catch(() => {});
    const notice = document.querySelector('.istota-notice');
    expect(notice?.textContent).toBe('This app was updated. Reload.');
    expect(notice?.getAttribute('role')).toBe('status');
  });

  it('says it is offline on offline, once', async () => {
    for (let i = 0; i < 2; i += 1) {
      const call = istota.kv.get('k');
      reply({ istota: 1, id: last().id, ok: false, error: { code: 'offline', message: '' } });
      await call.catch(() => {});
    }
    expect(document.querySelectorAll('.istota-notice')).toHaveLength(1);
  });
});

describe('shell calls', () => {
  beforeEach(() => {
    istota = load();
  });

  it('copy is a shell call', async () => {
    const call = istota.copy('hello');
    expect(last()).toMatchObject({ istota: 1, kind: 'shell', op: 'copy', args: { text: 'hello' } });
    reply({ istota: 1, id: last().id, ok: true, result: null });
    await expect(call).resolves.toBeNull();
  });

  it('forwards an uncaught error as an error shell call, capped at 4 KiB', () => {
    const error = new Error('x'.repeat(5000));
    window.dispatchEvent(new ErrorEvent('error', { message: error.message, error }));
    const message = last();
    expect(message).toMatchObject({ kind: 'shell', op: 'error' });
    const args = message.args as { kind: string; message: string; stack: string };
    expect(args.kind).toBe('error');
    expect(args.message.length).toBe(4096);
    expect(args.stack.length).toBeLessThanOrEqual(4096);
  });

  it('forwards an unhandled rejection', () => {
    const event = new Event('unhandledrejection') as Event & { reason: unknown };
    event.reason = new Error('nope');
    window.dispatchEvent(event);
    expect(last()).toMatchObject({ op: 'error', args: { kind: 'rejection', message: 'nope' } });
  });

  it('does not report the report failing', async () => {
    window.dispatchEvent(new ErrorEvent('error', { message: 'boom' }));
    const count = sent().length;
    reply({ istota: 1, id: last().id, ok: false, error: { code: 'internal', message: '' } });
    await Promise.resolve();
    await Promise.resolve();
    expect(sent()).toHaveLength(count);
  });
});

describe('env messages', () => {
  beforeEach(() => {
    istota = load('#theme=dark&scale=medium');
  });

  it('updates the environment, applies it and tells listeners', () => {
    const seen: Array<Record<string, unknown>> = [];
    const off = istota.on('env', (env) => seen.push(env));
    reply({
      istota: 1,
      kind: 'env',
      env: { theme: 'light', keyboard: true, visibility: 'hidden' },
    });
    expect(istota.env).toMatchObject({ theme: 'light', keyboard: true, visibility: 'hidden' });
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(seen).toHaveLength(1);
    off();
    reply({ istota: 1, kind: 'env', env: { theme: 'dark' } });
    expect(seen).toHaveLength(1);
  });

  it('ignores keys it does not know and values of the wrong type', () => {
    reply({ istota: 1, kind: 'env', env: { online: 'no', extra: 1, scale: 'small' } });
    expect(istota.env.online).toBe(true);
    expect(istota.env).not.toHaveProperty('extra');
    expect(istota.env.scale).toBe('small');
  });

  it('refuses an event name it does not have', () => {
    expect(() => istota.on('click', () => {})).toThrow();
  });
});
