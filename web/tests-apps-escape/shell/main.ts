// The shell page: the real bridge and frame watch, around one app frame, with an
// in-memory `kv:own` standing in for the server. Every relayed call and the
// frame watch's verdict are published on `window.__escape` for the spec to read.
import { createBridge } from '../../src/lib/apps/bridge';
import { watchFrame } from '../../src/lib/apps/frameWatch';

interface EscapeState {
  app: string;
  connected: boolean;
  navigated: boolean;
  frameRemoved: boolean;
  calls: { capability: string; op: string }[];
  kv: Record<string, unknown>;
  errors: { kind: string; message: string }[];
}

const app = new URLSearchParams(location.search).get('app') ?? '';
const state: EscapeState = {
  app,
  connected: false,
  navigated: false,
  frameRemoved: false,
  calls: [],
  kv: {},
  errors: [],
};
(window as unknown as { __escape: EscapeState }).__escape = state;

const env = {
  theme: 'light',
  scale: 'medium',
  shell: 'web',
  visibility: 'visible',
  online: true,
  keyboard: false,
} as const;

const frame = document.createElement('iframe');
frame.setAttribute('sandbox', 'allow-scripts');
frame.setAttribute('data-istota-app-frame', '');
frame.title = app;

function kvOwn(op: string, args: Record<string, unknown>) {
  const key = typeof args.key === 'string' ? args.key : '';
  if (op === 'get') return { ok: true as const, result: key in state.kv ? state.kv[key] : null };
  if (op === 'set') {
    state.kv[key] = args.value;
    return { ok: true as const, result: null };
  }
  if (op === 'delete') {
    delete state.kv[key];
    return { ok: true as const, result: null };
  }
  if (op === 'list') {
    const items = Object.entries(state.kv).map(([k, value]) => ({ key: k, value }));
    return { ok: true as const, result: { items, total: items.length } };
  }
  return { ok: false as const, error: { code: 'bad_args' as const, message: 'unknown op' } };
}

const bridge = createBridge({
  onUnresponsive: leave,
  frameWindow: () => frame.contentWindow,
  navigations: () => watch.navigations,
  online: () => true,
  env: () => ({ ...env }),
  call: async ({ capability, op, args }) => {
    state.calls.push({ capability, op });
    if (capability === 'kv:own') return kvOwn(op, args);
    return { ok: false, error: { code: 'not_granted', message: 'not granted' } };
  },
  copy: async () => true,
  report: async ({ kind, message }) => {
    state.errors.push({ kind, message });
  },
});

function leave() {
  state.navigated = true;
  bridge.dispose();
  frame.remove();
  state.frameRemoved = !frame.isConnected;
}

const watch = watchFrame(frame, leave, () => {
  bridge.connect();
  state.connected = true;
});

// The bridge is listening for the kit's key before the frame starts loading.
// The fragment is what env.frameUrl writes.
frame.src = `/frame/${encodeURIComponent(app)}#theme=light&scale=medium&shell=web&v=1`;
document.getElementById('host')?.append(frame);
