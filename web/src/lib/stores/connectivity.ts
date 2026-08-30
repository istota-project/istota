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
 *    already exists, and goes to our own https origin, which nothing between
 *    the phone and the server can answer for: an interface that is up behind a
 *    captive portal fails it, where a request something else may answer would
 *    pass.
 * 3. **`navigator.onLine`**, which is a hint and is trusted in one direction
 *    only. In a WKWebView it is `CPNetworkObserver`'s interface reachability:
 *    `false` means there is no interface, so there is certainly no server, and
 *    is worth acting on at once; `true` routinely means a portal, so it buys a
 *    probe and nothing more.
 *
 * The spec this comes from lists the room stream as its second input, on the
 * grounds that `roomStreamLive` already flips on the stream's `onopen` and off
 * on its error path. It is **deliberately not wired here**: the error path is
 * not conclusive (a stream also ends because no room is open, or because the
 * server closed it), so only its `onopen` would be usable, and that is a
 * `chat.ts` change belonging to the stage that touches the drain. The two
 * inputs above already cover everything it would report.
 *
 * A report is about the moment a request ended, and nothing here is stamped:
 * a send bounded at 30s can report a gap that closed while it was stalling,
 * so the banner can come back up for one backoff step after the connection
 * has actually returned. Left alone deliberately — the probe corrects it
 * within 5s, and the alternative is a generation on every call site.
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
 *
 * Only while offline, too. A request can succeed between a probe reading the
 * store and its caller resuming, and a schedule armed against an online store
 * would spend a request finding out what it already knew.
 */
function armProbe(): void {
  if (!listening || get(state)) return;
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

/**
 * Probe now rather than at the scheduled time, if there is anything to resolve.
 *
 * A probe already running is that answer arriving, so the trigger is spent
 * rather than joined. Joining looks harmless — `probe()` hands back the same
 * promise — but every joiner also re-arms when it fails, and each re-arm takes
 * a step off the backoff, so two triggers landing during one probe pushed the
 * schedule to 20s after a single failure. A phone waking in and out of signal
 * produces exactly that overlap.
 */
function probeNow(): void {
  if (get(state) || inFlight !== null) return;
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
  if (inFlight !== null) return inFlight;
  // Clears the slot only if it still holds this probe. A teardown drops the
  // slot without being able to recall the request, so an unconditional clear
  // here would let a probe from the previous session cancel the current one's
  // claim and let a second run in parallel with it.
  const mine: Promise<boolean> = runProbe().finally(() => {
    if (inFlight === mine) inFlight = null;
  });
  inFlight = mine;
  return mine;
}

async function runProbe(): Promise<boolean> {
  try {
    await getChatConfig(PROBE_TIMEOUT_MS);
    // Reported here as well as inside `apiFetch`, so the probe is right about
    // its own result without depending on how the call it makes is plumbed.
    noteTransport(true);
  } catch {
    // Already reported by `apiFetch`, which is the only thing that can tell a
    // gap from a server saying no. Reading the store back rather than assuming
    // the worst is what makes a 500 — or a config endpoint that has since
    // changed shape — a reachable server rather than an outage.
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
  // A second caller gets a teardown that stops nothing: it installed nothing,
  // and handing it the real one would let its unmount switch connectivity off
  // under whoever is still using it.
  if (listening) return () => {};
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

  // Starting while already offline arms nothing above: `setOnline` returns
  // early on an unchanged value, and `armProbe` refuses before `listening`.
  // Without this the invariant "listening and offline implies a schedule"
  // holds only by the order the layout happens to run in, and a restart while
  // offline would sit with the banner up and nothing on its way to clear it.
  if (!get(state)) armProbe();

  return stopConnectivity;
}

function stopConnectivity(): void {
  listening = false;
  cancelSchedule();
  backoffStep = 0;
  // A probe on the wire cannot be recalled, but the next session must not be
  // handed its promise — it would resolve against the state of a torn-down
  // page and stand in for the probe that session needed.
  inFlight = null;
  if (offlineHandler) window.removeEventListener('offline', offlineHandler);
  if (onlineHandler) window.removeEventListener('online', onlineHandler);
  if (visibilityHandler && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', visibilityHandler);
  }
  offlineHandler = null;
  onlineHandler = null;
  visibilityHandler = null;
}
