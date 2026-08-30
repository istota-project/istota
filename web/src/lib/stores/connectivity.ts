/**
 * Connectivity as a fact the app has, not an error each caller reports.
 *
 * ISSUE-202. Offline is a state, so it belongs in one store that the banner,
 * the composer and (from the later stages of the offline work) the transcript
 * cache and the send queue all read — rather than in each surface's own guess
 * from its own failed request.
 *
 * Three inputs, in descending authority:
 *
 * 1. **What a request observed** (`noteTransport`). The only input that is a
 *    fact about the *server* rather than about the interface: a `rejected`,
 *    `auth` or `rate_limit` answer means we reached it, and `unreachable` or
 *    `timeout` means we did not. `apiFetch` and `sendChatMessage` report every
 *    completion, so ordinary use of the app keeps this current for free.
 * 2. **The probe** — the same fact, asked for deliberately, on a backoff while
 *    the store believes it is offline. `getChatConfig` because it is small,
 *    already exists, and is authenticated, so a captive portal answering
 *    everything with its own login page fails it where a `HEAD /favicon` would
 *    not.
 * 3. **`navigator.onLine`**, which is a hint and is trusted in one direction
 *    only. In a WKWebView it is `CPNetworkObserver`'s interface reachability:
 *    `false` means there is no interface, so there is certainly no server, and
 *    is worth acting on at once; `true` routinely means a portal, so it buys a
 *    probe and nothing more.
 *
 * Nothing here reports to the user. The banner reads `online`; a connectivity
 * state is not an event, so it never goes through `notify()`.
 */
import { get, writable, type Readable } from 'svelte/store';
import { getChatConfig, type SendFailure } from '$lib/api';

/**
 * How long a probe may run before it counts as a gap.
 *
 * Far below `SEND_TIMEOUT_MS`: a send is worth waiting 30s for because it is
 * the user's message, while a probe is a question we will ask again in five
 * seconds and whose slow answer is itself the answer.
 */
export const PROBE_TIMEOUT_MS = 5_000;

/**
 * Probe cadence while offline, in ms; the last entry repeats.
 *
 * Front-loaded because the common gap is short (a lift, a tunnel, a handover)
 * and the user is looking at the banner, and flattened at a minute because the
 * long gap is a phone in a pocket with no signal, where each probe is a radio
 * wake for an answer nothing is waiting on.
 */
export const PROBE_BACKOFF_MS = [5_000, 10_000, 20_000, 40_000, 60_000];

const state = writable(true);

/**
 * Whether the app can currently reach its server.
 *
 * Read-only by construction: the value is evidence, and a component setting it
 * by hand would be writing a fact nothing observed. Starts `true` so a page
 * that has seen nothing yet renders as it always did — the first request to
 * fail, or `startConnectivity` finding the interface already down, corrects it
 * within a moment.
 */
export const online: Readable<boolean> = { subscribe: state.subscribe };

let listening = false;
let timer: ReturnType<typeof setTimeout> | null = null;
let backoffStep = 0;
let inFlight: Promise<boolean> | null = null;
let offlineHandler: (() => void) | null = null;
let onlineHandler: (() => void) | null = null;
let visibilityHandler: (() => void) | null = null;

function cancelSchedule(): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
}

/**
 * Arm the next probe.
 *
 * Only while started: the schedule is the layout's to own, and a module that
 * kept a timer alive after teardown would go on waking a torn-down page. Going
 * offline without a running loop is still recorded — the store is a fact, and
 * the next request to succeed clears it.
 */
function armProbe(): void {
  if (!listening) return;
  cancelSchedule();
  const delay = PROBE_BACKOFF_MS[Math.min(backoffStep, PROBE_BACKOFF_MS.length - 1)];
  backoffStep += 1;
  timer = setTimeout(() => {
    timer = null;
    void probeAndRearm();
  }, delay);
}

async function probeAndRearm(): Promise<void> {
  const ok = await probe();
  // A success has already cancelled the schedule and reset the step through
  // `setOnline`, so this only re-arms a loop that is still offline.
  if (!ok) armProbe();
}

/** Probe now rather than at the scheduled time, if there is anything to resolve. */
function probeNow(): void {
  if (get(state)) return;
  cancelSchedule();
  void probeAndRearm();
}

function setOnline(next: boolean): void {
  if (get(state) === next) return;
  state.set(next);
  if (next) {
    cancelSchedule();
    backoffStep = 0;
  } else {
    // From the front of the schedule: the gap that just started is a new one,
    // whatever the last one cost to notice.
    backoffStep = 0;
    armProbe();
  }
}

/**
 * Report what a transport attempt actually observed. The authoritative input.
 *
 * `ok` — or a failure the server itself produced — means we reached it. Only
 * `unreachable` and `timeout` are gaps. A failure the caller could not classify
 * says nothing at all and moves nothing: raising the banner on a guess is worse
 * than raising it a few seconds late, since the probe is already running.
 */
export function noteTransport(ok: boolean, failure?: SendFailure): void {
  if (ok) {
    setOnline(true);
    return;
  }
  if (failure === 'unreachable' || failure === 'timeout') {
    setOnline(false);
    return;
  }
  if (failure !== undefined) setOnline(true);
}

/**
 * Ask the server directly; resolve the store and return what it now believes.
 *
 * One at a time. The triggers overlap by design — the `online` event, a
 * foreground and the backoff can all land together on a phone waking in a
 * signal area — and a probe per trigger would be a burst of identical requests
 * at exactly the moment the connection is weakest.
 *
 * The failure path deliberately reads the store rather than assuming the worst:
 * `apiFetch` throws for any non-2xx, so a rejection here may well be a 500 —
 * a server that answered — and it has already reported that through
 * `noteTransport`.
 */
export function probe(): Promise<boolean> {
  inFlight ??= runProbe().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

async function runProbe(): Promise<boolean> {
  try {
    await getChatConfig(PROBE_TIMEOUT_MS);
    // Reported here as well as inside `apiFetch`, so the probe is right about
    // its own result without depending on how the call it makes is plumbed.
    noteTransport(true);
  } catch {
    // Already reported by `apiFetch`, which is the only thing that can tell a
    // gap from a server saying no.
  }
  return get(state);
}

/**
 * Install the interface listeners. Idempotent; returns the teardown.
 *
 * `visibilitychange` covers the app coming back to the foreground as well as a
 * tab being returned to — a WKWebView fires it on resume — which is the moment
 * a backed-off probe is most worth spending, since a phone waking in the user's
 * hand is usually a phone with signal again.
 */
export function startConnectivity(): () => void {
  if (typeof window === 'undefined') return () => {};
  if (listening) return stopConnectivity;
  listening = true;

  offlineHandler = () => setOnline(false);
  onlineHandler = () => probeNow();
  window.addEventListener('offline', offlineHandler);
  window.addEventListener('online', onlineHandler);

  if (typeof document !== 'undefined') {
    visibilityHandler = () => {
      if (document.visibilityState === 'visible') probeNow();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
  }

  // A page loaded with the interface already down — the case the whole feature
  // exists for — has no event coming to tell it so.
  if (typeof navigator !== 'undefined' && navigator.onLine === false) setOnline(false);

  return stopConnectivity;
}

function stopConnectivity(): void {
  listening = false;
  cancelSchedule();
  backoffStep = 0;
  if (offlineHandler) window.removeEventListener('offline', offlineHandler);
  if (onlineHandler) window.removeEventListener('online', onlineHandler);
  if (visibilityHandler && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', visibilityHandler);
  }
  offlineHandler = null;
  onlineHandler = null;
  visibilityHandler = null;
}
