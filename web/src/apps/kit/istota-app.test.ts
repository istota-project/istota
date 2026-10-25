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
    read(namespace: string, key: string): Promise<unknown>;
    readList(namespace: string, options?: Record<string, unknown>): Promise<unknown>;
  };
  files: {
    list(path?: string): Promise<unknown>;
    read(path: string, options?: Record<string, unknown>): Promise<unknown>;
    write(path: string, content: string, options?: Record<string, unknown>): Promise<unknown>;
    delete(path: string): Promise<unknown>;
    listShared(folder: string, path?: string): Promise<unknown>;
    readShared(folder: string, path: string, options?: Record<string, unknown>): Promise<unknown>;
  };
  calendar: {
    list(options: Record<string, unknown>): Promise<unknown>;
  };
  skill(operation: string, args?: Record<string, unknown>): Promise<unknown>;
  env: Record<string, unknown>;
  on(event: string, fn: (env: Record<string, unknown>) => void): () => void;
  copy(text: string): Promise<unknown>;
  IstotaError: new (code: string, message: string) => Error & { code: string };
  ERROR_CODES: readonly string[];
};

const SOURCE = readFileSync(join(KIT, 'istota-app.js'), 'utf8');
const listeners: Array<[string, EventListener]> = [];
const channels: MessageChannel[] = [];
/** The kit's end of the channel: what the kit sends on it is recorded here. */
let posted: MockInstance;
let windowPosted: MockInstance;
let kitPort: MessagePort | null;
let istota: Istota;

/** Run the kit; `port: false` leaves it waiting for the shell's port. */
function load(hash = '', { port = true } = {}): Istota {
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
  if (port) handOver();
  return (globalThis as unknown as { istota: Istota }).istota;
}

/** The shell's port handoff, as `bridge.connect` posts it; jsdom's top window is its own parent. */
function handOver(source: Window | null = window, data: unknown = { istota: 1, kind: 'port' }) {
  const channel = new MessageChannel();
  channels.push(channel);
  const spy = vi.spyOn(channel.port2, 'postMessage');
  window.dispatchEvent(new MessageEvent('message', { data, source, ports: [channel.port2] }));
  if (!kitPort && channel.port2.onmessage) {
    kitPort = channel.port2;
    posted = spy;
  }
  return { channel, spy };
}

/** Everything the kit sent over the port, the hello first. */
function raw(): Message[] {
  return posted ? posted.mock.calls.map((call) => call[0] as Message) : [];
}

/** The kit's calls and shell requests, without the one hello. */
function sent(): Message[] {
  return raw().filter((message) => message.kind !== 'hello');
}

/** The one key message the kit sends as soon as it takes the port. */
function hellos(): Message[] {
  return raw().filter((message) => message.kind === 'hello');
}

function last(): Message {
  const all = sent();
  return all[all.length - 1];
}

/** A message from the shell on the port, delivered synchronously. */
function reply(data: unknown): void {
  if (!kitPort?.onmessage) throw new Error('the kit holds no port');
  kitPort.onmessage(new MessageEvent('message', { data }));
}

beforeEach(() => {
  kitPort = null;
  posted = undefined as unknown as MockInstance;
  windowPosted = vi.spyOn(window, 'postMessage').mockImplementation(() => {});
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('data-font-size');
});

afterEach(() => {
  for (const [type, fn] of listeners.splice(0)) window.removeEventListener(type, fn);
  for (const channel of channels.splice(0)) {
    channel.port1.close();
    channel.port2.close();
  }
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

describe('the port', () => {
  it('queues a call made before the port arrives and sends it once it does', async () => {
    istota = load('', { port: false });
    const call = istota.kv.get('early');
    expect(windowPosted).not.toHaveBeenCalled();
    handOver();
    // The hello goes first, then the call that waited for the port.
    expect(raw()).toEqual([
      { istota: 1, kind: 'hello', k: '' },
      {
        istota: 1,
        id: expect.any(Number),
        kind: 'call',
        capability: 'kv:own',
        op: 'get',
        args: { key: 'early' },
      },
    ]);
    reply({ istota: 1, id: last().id, ok: true, result: 'E' });
    await expect(call).resolves.toBe('E');
  });

  it('takes the port only from the parent window', () => {
    istota = load('', { port: false });
    void istota.kv.get('k');
    const stranger = handOver(null);
    expect(stranger.spy).not.toHaveBeenCalled();
    expect(kitPort).toBeNull();
    const parent = handOver();
    // The hello, then the call that was waiting for a port.
    expect(parent.spy).toHaveBeenCalledTimes(2);
  });

  it('takes no port from a message that is not the handoff', () => {
    istota = load('', { port: false });
    void istota.kv.get('k');
    handOver(window, { istota: 1, kind: 'env', env: {} });
    expect(kitPort).toBeNull();
  });

  it('ignores a second port', () => {
    istota = load();
    const second = handOver();
    void istota.kv.get('k');
    expect(second.spy).not.toHaveBeenCalled();
    expect(second.channel.port2.onmessage).toBeNull();
    expect(hellos()).toHaveLength(1);
    expect(sent()).toHaveLength(1);
  });

  it('takes no environment posted on the window once it holds the port', () => {
    istota = load('#theme=dark');
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { istota: 1, kind: 'env', env: { theme: 'light' } },
        source: window,
      }),
    );
    expect(istota.env.theme).toBe('dark');
  });
});

describe('the port key', () => {
  it('sends the key from the fragment as its first port message', () => {
    istota = load('#theme=dark&k=s3cret-key');
    expect(raw()[0]).toEqual({ istota: 1, kind: 'hello', k: 's3cret-key' });
  });

  it('strips the fragment so app code cannot read the key back', () => {
    istota = load('#theme=dark&k=s3cret-key');
    expect(window.location.hash).toBe('');
    expect(hellos()).toEqual([{ istota: 1, kind: 'hello', k: 's3cret-key' }]);
  });

  it('sends an empty key when the fragment has none', () => {
    istota = load('#theme=dark');
    expect(hellos()).toEqual([{ istota: 1, kind: 'hello', k: '' }]);
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

  it('names the read capabilities by their scope', () => {
    void istota.kv.read('notes', 'today');
    void istota.kv.readList('notes', { prefix: 'day:' });
    void istota.files.listShared('notes/aviation');
    void istota.files.readShared('notes/aviation', 'weather.md', { encoding: 'base64' });
    void istota.calendar.list({ from: '2026-11-01', to: '2026-11-08' });
    void istota.calendar.list({ from: '2026-11-01', to: '2026-11-08', calendar: 'Home' });
    const messages = sent();
    expect(messages.map((m) => [m.capability, m.op, m.args])).toEqual([
      ['kv:read:notes', 'get', { key: 'today' }],
      ['kv:read:notes', 'list', { prefix: 'day:' }],
      ['files:read:notes/aviation', 'list', { path: '' }],
      ['files:read:notes/aviation', 'read', { path: 'weather.md', encoding: 'base64' }],
      ['calendar:read', 'list', { from: '2026-11-01', to: '2026-11-08' }],
      ['calendar:read:Home', 'list', { from: '2026-11-01', to: '2026-11-08' }],
    ]);
  });

  it('names a skill operation as its capability, with the one op call', () => {
    void istota.skill('markets.quote', { symbols: ['AAPL'] });
    void istota.skill('location.places');
    expect(sent().map((m) => [m.capability, m.op, m.args])).toEqual([
      ['skill:markets.quote', 'call', { symbols: ['AAPL'] }],
      ['skill:location.places', 'call', {}],
    ]);
  });

  it('sends over the port and never posts on a window', () => {
    void istota.kv.get('a');
    expect(sent()).toHaveLength(1);
    expect(windowPosted).not.toHaveBeenCalled();
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

  it('takes no reply posted on the window, even from the parent', async () => {
    vi.useFakeTimers();
    const call = istota.kv.get('k');
    const settled = call.catch((e: unknown) => e);
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { istota: 1, id: last().id, ok: true, result: 'forged' },
        source: window,
      }),
    );
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
