// The client library: `window.istota`, a thin wrapper over postMessage to the
// shell that frames the app. The shell relays data calls to the server, which
// checks each against the installed version's grants; this file enforces
// nothing. It runs after the guard and before any app code.
//
// Frame to shell:  {istota: 1, id, kind: 'call', capability, op, args}
//                  {istota: 1, id, kind: 'shell', op, args}
// Shell to frame:  {istota: 1, id, ok: true, result}
//                  {istota: 1, id, ok: false, error: {code, message}}
//                  {istota: 1, kind: 'env', env}
// Anything else, or anything not from the parent window, is dropped.
(() => {
  const PROTOCOL = 1;
  const TIMEOUT_MS = 15000;
  const REPORT_MAX = 4096;
  const ERROR_CODES = Object.freeze([
    'not_granted',
    'bad_args',
    'quota',
    'rate_limited',
    'stale_version',
    'not_found',
    'offline',
    'timeout',
    'internal',
  ]);
  const THEMES = ['dark', 'light'];
  const SCALES = ['small', 'medium', 'large'];
  /** @type {Record<string, string>} */
  const SCALE_ALIASES = { sm: 'small', md: 'medium', lg: 'large' };
  const SHELLS = ['web', 'ios'];
  const NOTICES = /** @type {Record<string, string>} */ ({
    stale_version: 'This app was updated. Reload.',
    offline: 'You are offline. Changes will not be saved until the connection is back.',
  });

  class IstotaError extends Error {
    /**
     * @param {string} code
     * @param {string} message
     */
    constructor(code, message) {
      super(message);
      this.name = 'IstotaError';
      this.code = ERROR_CODES.includes(code) ? code : 'internal';
    }
  }

  /**
   * @typedef {{theme: string, scale: string, shell: string, visibility: string,
   *   online: boolean, keyboard: boolean}} Env
   */

  /** @param {unknown} value */
  function scaleOf(value) {
    if (typeof value !== 'string') return null;
    const name = SCALE_ALIASES[value] || value;
    return SCALES.includes(name) ? name : null;
  }

  /** @returns {Env} */
  function startingEnv() {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const theme = params.get('theme');
    const shell = params.get('shell');
    return {
      theme: theme && THEMES.includes(theme) ? theme : 'dark',
      scale: scaleOf(params.get('scale')) || 'medium',
      shell: shell && SHELLS.includes(shell) ? shell : 'web',
      visibility: document.visibilityState === 'hidden' ? 'hidden' : 'visible',
      online: true,
      keyboard: false,
    };
  }

  /** @type {Env} */
  let env = startingEnv();

  // The attributes minot's tokens.css keys the theme and text scale on.
  function applyEnv() {
    const root = document.documentElement;
    root.setAttribute('data-theme', env.theme);
    root.setAttribute('data-font-size', env.scale);
  }
  applyEnv();

  /**
   * The known keys of an env message, each checked; the rest are ignored.
   * @param {Record<string, unknown>} update
   */
  function mergeEnv(update) {
    /** @type {Env} */
    const next = { ...env };
    if (typeof update.theme === 'string' && THEMES.includes(update.theme))
      next.theme = update.theme;
    const scale = scaleOf(update.scale);
    if (scale) next.scale = scale;
    if (typeof update.shell === 'string' && SHELLS.includes(update.shell))
      next.shell = update.shell;
    if (update.visibility === 'visible' || update.visibility === 'hidden') {
      next.visibility = update.visibility;
    }
    if (typeof update.online === 'boolean') next.online = update.online;
    if (typeof update.keyboard === 'boolean') next.keyboard = update.keyboard;
    return next;
  }

  /** @type {Set<(env: Env) => void>} */
  const envListeners = new Set();

  /** @type {Map<number, {resolve: (value: unknown) => void, reject: (error: IstotaError) => void, timer: ReturnType<typeof setTimeout>}>} */
  const pending = new Map();
  let nextId = 1;

  /** @param {string} code */
  function showNotice(code) {
    const text = NOTICES[code];
    if (!text || !document.body) return;
    if (document.querySelector(`.istota-notice[data-code="${code}"]`)) return;
    const notice = document.createElement('div');
    notice.className = 'istota-notice';
    notice.dataset.code = code;
    notice.setAttribute('role', 'status');
    notice.textContent = text;
    document.body.prepend(notice);
  }

  /**
   * @param {Record<string, unknown>} fields
   * @returns {Promise<unknown>}
   */
  function request(fields) {
    const id = nextId;
    nextId += 1;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new IstotaError('timeout', 'The app got no answer in time.'));
      }, TIMEOUT_MS);
      pending.set(id, { resolve, reject, timer });
      window.parent.postMessage({ istota: PROTOCOL, id, ...fields }, '*');
    });
  }

  /**
   * @param {string} capability
   * @param {string} op
   * @param {Record<string, unknown>} args
   */
  function call(capability, op, args) {
    return request({ kind: 'call', capability, op, args }).catch((error) => {
      if (error instanceof IstotaError) showNotice(error.code);
      throw error;
    });
  }

  /**
   * @param {string} op
   * @param {Record<string, unknown>} args
   */
  function shell(op, args) {
    return request({ kind: 'shell', op, args });
  }

  /** @param {unknown} data */
  function onMessage(data) {
    if (!data || typeof data !== 'object') return;
    const message = /** @type {Record<string, unknown>} */ (data);
    if (message.istota !== PROTOCOL) return;
    if (message.kind === 'env') {
      if (!message.env || typeof message.env !== 'object') return;
      env = mergeEnv(/** @type {Record<string, unknown>} */ (message.env));
      applyEnv();
      for (const fn of envListeners) fn(env);
      return;
    }
    if (typeof message.id !== 'number' || typeof message.ok !== 'boolean') return;
    const entry = pending.get(message.id);
    if (!entry) return;
    pending.delete(message.id);
    clearTimeout(entry.timer);
    if (message.ok) {
      entry.resolve(message.result === undefined ? null : message.result);
      return;
    }
    const error = /** @type {Record<string, unknown>} */ (
      message.error && typeof message.error === 'object' ? message.error : {}
    );
    entry.reject(
      new IstotaError(
        typeof error.code === 'string' ? error.code : 'internal',
        typeof error.message === 'string' ? error.message : '',
      ),
    );
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window.parent) return;
    onMessage(event.data);
  });

  /** @param {unknown} value */
  function clip(value) {
    return String(value === undefined || value === null ? '' : value).slice(0, REPORT_MAX);
  }

  /**
   * Fire and forget: a failed report is not itself reported.
   * @param {string} kind
   * @param {unknown} error
   * @param {unknown} fallbackMessage
   */
  function report(kind, error, fallbackMessage) {
    const err = /** @type {{message?: unknown, stack?: unknown}} */ (
      error && typeof error === 'object' ? error : {}
    );
    const message = err.message !== undefined ? err.message : fallbackMessage;
    shell('error', { kind, message: clip(message), stack: clip(err.stack) }).catch(() => {});
  }

  window.addEventListener('error', (event) => {
    report('error', event.error, event.message);
  });
  window.addEventListener('unhandledrejection', (event) => {
    report('rejection', event.reason, event.reason);
  });

  // The iOS keyboard shrinks the frame; keep the field being typed in visible.
  window.addEventListener('resize', () => {
    const active = document.activeElement;
    if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
      active.scrollIntoView({ block: 'nearest' });
    }
  });

  const kv = Object.freeze({
    /** @param {string} key */
    get: (key) => call('kv:own', 'get', { key }),
    /**
     * @param {string} key
     * @param {unknown} value any JSON value
     */
    set: (key, value) => call('kv:own', 'set', { key, value }),
    /** @param {string} key */
    delete: (key) => call('kv:own', 'delete', { key }),
    /** @param {{prefix?: string, offset?: number, limit?: number}} [options] */
    list: (options = {}) => call('kv:own', 'list', { ...options }),
  });

  const files = Object.freeze({
    /** @param {string} [path] relative to the app's data/ folder */
    list: (path = '') => call('files:own', 'list', { path }),
    /**
     * @param {string} path
     * @param {{encoding?: 'utf8' | 'base64'}} [options]
     */
    read: (path, { encoding = 'utf8' } = {}) => call('files:own', 'read', { path, encoding }),
    /**
     * @param {string} path
     * @param {string} content
     * @param {{encoding?: 'utf8' | 'base64'}} [options]
     */
    write: (path, content, { encoding = 'utf8' } = {}) =>
      call('files:own', 'write', { path, content, encoding }),
    /** @param {string} path */
    delete: (path) => call('files:own', 'delete', { path }),
  });

  const api = {
    kv,
    files,
    get env() {
      return { ...env };
    },
    /**
     * @param {string} event only 'env'
     * @param {(env: Env) => void} fn
     */
    on(event, fn) {
      if (event !== 'env') throw new TypeError(`istota.on: no event named ${event}`);
      envListeners.add(fn);
      return () => {
        envListeners.delete(fn);
      };
    },
    /** @param {string} text */
    copy: (text) => shell('copy', { text: String(text) }),
    IstotaError,
    ERROR_CODES,
  };

  /** @type {any} */ (globalThis).istota = api;
})();
