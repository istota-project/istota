import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';

// The component asks the autocomplete providers for the model and brain
// dropdowns on mount, and the real ones reach the API. Both are `vi.fn`s the
// brain cases below reprogram per test; the default is the shipped deployment,
// where the operator has listed no selectable kinds.
vi.mock('$lib/components/chat/autocomplete/providers', () => ({
  getBaseModelChoices: vi.fn(async () => []),
  getSelectableBrains: vi.fn(async () => []),
  getBrainNamespaces: vi.fn(async () => ({})),
  getInheritedBrain: vi.fn(async () => null),
}));

import RoomSettings from './RoomSettings.svelte';
import {
  getBaseModelChoices,
  getBrainNamespaces,
  getInheritedBrain,
  getSelectableBrains,
} from './autocomplete/providers';
import type { ChatRoom, RoomPatch, SelectableBrain } from '$lib/api';

function room(overrides: Partial<ChatRoom> = {}): ChatRoom {
  return {
    id: 1,
    token: 'web-alice-abc',
    name: 'general',
    archived: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    origin: 'web',
    ...overrides,
  };
}

function mount(r: ChatRoom, onSave = vi.fn()) {
  return render(RoomSettings, {
    props: {
      open: true,
      room: r,
      onSave,
      onDelete: vi.fn(),
      onPromote: vi.fn(),
      onClose: vi.fn(),
    },
  });
}

const CLAUDE: SelectableBrain = {
  kind: 'claude_code',
  label: 'Claude Code',
  model_namespace: 'anthropic',
};
const TMUX: SelectableBrain = {
  kind: 'tmux_claude',
  label: 'Tmux Claude',
  model_namespace: 'anthropic',
};
const NATIVE: SelectableBrain = {
  kind: 'native',
  label: 'Native',
  model_namespace: 'openai_compat',
};
/** Every kind the server can build, which is what `brain_namespaces` carries —
 *  deliberately wider than whatever a given test puts on the menu. */
const ALL_BRAINS = [CLAUDE, TMUX, NATIVE];

/** Pick an option out of a `Select`, the way bits-ui actually listens for it.
 *  A plain `click` on the item opens nothing and selects nothing — the item
 *  commits on pointerup — so a test written with `click` passes while asserting
 *  against a value that never changed. */
async function pick(ariaLabel: string, optionLabel: string) {
  const trigger = screen.getByRole('button', { name: ariaLabel });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const item = screen.getByText(optionLabel).closest('[data-select-item]');
  if (!item) throw new Error(`no option ${optionLabel} under ${ariaLabel}`);
  await fireEvent.pointerMove(item, { pointerType: 'mouse' });
  await fireEvent.pointerDown(item, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(item, { pointerType: 'mouse', button: 0 });
}

/** Mount, then let the two provider promises resolve — both dropdowns are
 *  filled from an `$effect`, so nothing is on screen until they settle. */
async function mountSettled(r: ChatRoom, onSave = vi.fn()) {
  const out = mount(r, onSave);
  await Promise.resolve();
  await Promise.resolve();
  return out;
}

const TALK_LINE = /also open in Nextcloud Talk/i;
const PROMOTE_LABEL = /^Also open in Talk$/i;
const RECONNECT_LABEL = /^Reconnect to Talk$/i;

afterEach(() => cleanup());

// ISSUE-342. A promoted room keeps `origin: 'web'`, so `talk_token` is the only
// thing that can say it is on Talk. The listing never sent that key, and the
// room-list refresh writes it unconditionally — so a poll erased what the
// promote response had just set, and the room reverted to offering a promote
// the backend then refuses.
describe('RoomSettings — Talk state', () => {
  it('shows the Talk line for a promoted room', () => {
    mount(room({ origin: 'web', talk_token: 'tk4ab9cd' }));
    expect(screen.getByText(TALK_LINE)).toBeTruthy();
    expect(screen.queryByRole('button', { name: PROMOTE_LABEL })).toBeNull();
  });

  it('shows the Talk line for a Talk-origin room', () => {
    mount(room({ origin: 'talk', token: 'cpz', talk_token: 'cpz' }));
    expect(screen.getByText(TALK_LINE)).toBeTruthy();
  });

  // ISSUE-401. A promoted room's binding can go stale — the Talk conversation
  // deleted out from under it — and this button is the only way back. Hiding it
  // once `talk_token` was set is what made that state permanent from the app.
  it('offers a reconnect button for a promoted room, alongside the Talk line', () => {
    mount(room({ origin: 'web', talk_token: 'tk4ab9cd' }));
    expect(screen.getByText(TALK_LINE)).toBeTruthy();
    const btn = screen.getByRole('button', { name: RECONNECT_LABEL }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it('calls onPromote when the reconnect button is pressed', async () => {
    const onPromote = vi.fn();
    render(RoomSettings, {
      props: {
        open: true,
        room: room({ origin: 'web', talk_token: 'tk4ab9cd' }),
        onSave: vi.fn(),
        onDelete: vi.fn(),
        onPromote,
        onClose: vi.fn(),
      },
    });
    await fireEvent.click(screen.getByRole('button', { name: RECONNECT_LABEL }));
    expect(onPromote).toHaveBeenCalledTimes(1);
  });

  // A Talk-origin room's binding names its own canonical token, so there is
  // nothing here to repair and no second conversation to mint.
  it('offers no promote or reconnect button for a Talk-origin room', () => {
    mount(room({ origin: 'talk', token: 'cpz', talk_token: 'cpz' }));
    expect(screen.queryByRole('button', { name: PROMOTE_LABEL })).toBeNull();
    expect(screen.queryByRole('button', { name: RECONNECT_LABEL })).toBeNull();
  });

  it('offers the plain promote button, not reconnect, for an unbound room', () => {
    mount(room({ origin: 'web', talk_token: null }));
    expect(screen.queryByRole('button', { name: RECONNECT_LABEL })).toBeNull();
    expect(screen.getByRole('button', { name: PROMOTE_LABEL })).toBeTruthy();
  });

  it('shows no Talk line and an enabled button for an unbound room', () => {
    mount(room({ origin: 'web', talk_token: null }));
    expect(screen.queryByText(TALK_LINE)).toBeNull();
    const btn = screen.getByRole('button', { name: PROMOTE_LABEL }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it('treats an absent talk_token the same as a null one', () => {
    // An older backend sends neither key; the room is web-only and promotable.
    mount(room({ origin: 'web' }));
    expect(screen.getByRole('button', { name: PROMOTE_LABEL })).toBeTruthy();
  });
});

// The room's brain. Three questions the modal answers on its own — whether to
// show the control at all, what it starts on, and whether the pending change
// will cost the room its model pin — and one it does not: the server is still
// the authority on all three, and every assertion here is about the modal
// agreeing with it rather than deciding anything.
describe('RoomSettings — brain', () => {
  const brains = vi.mocked(getSelectableBrains);
  const models = vi.mocked(getBaseModelChoices);
  const namespaces = vi.mocked(getBrainNamespaces);
  const inherited = vi.mocked(getInheritedBrain);

  /** Offer these kinds, and answer the namespace questions consistently with
   *  them — the server builds all three fields from one pass, so a test whose
   *  menu and namespace map disagree is testing a payload that cannot exist. */
  function offer(list: SelectableBrain[], inheritedBrain: SelectableBrain | null = CLAUDE) {
    brains.mockResolvedValue(list);
    namespaces.mockResolvedValue(
      Object.fromEntries(ALL_BRAINS.map((b) => [b.kind, b.model_namespace])),
    );
    inherited.mockResolvedValue(inheritedBrain);
  }

  afterEach(() => {
    for (const m of [brains, models, namespaces, inherited]) m.mockReset();
    brains.mockResolvedValue([]);
    models.mockResolvedValue([]);
    namespaces.mockResolvedValue({});
    inherited.mockResolvedValue(null);
  });

  const BRAIN = 'Room brain';
  const MODEL = 'Room model default';
  const EFFORT = 'Room effort default';
  const CLEAR_WARNING = /clears the room's model and effort defaults/i;

  it('renders no control where the server offered no kinds', async () => {
    // The shipped default, and also every non-admin: the endpoint publishes an
    // empty list in both cases, so emptiness is the whole test.
    offer([]);
    await mountSettled(room());
    expect(screen.queryByRole('button', { name: BRAIN })).toBeNull();
    // The control: the model select, which is not gated, is still there.
    expect(screen.getByRole('button', { name: MODEL })).toBeTruthy();
  });

  it('renders the control once kinds are offered', async () => {
    offer([CLAUDE, NATIVE]);
    await mountSettled(room());
    expect(screen.getByRole('button', { name: BRAIN })).toBeTruthy();
  });

  it('initializes from room.brain', async () => {
    offer([CLAUDE, NATIVE]);
    await mountSettled(room({ brain: 'native' }));
    expect(screen.getByRole('button', { name: BRAIN })).toHaveTextContent('Native');
  });

  it('reads an unpinned room as the inherited default', async () => {
    offer([CLAUDE, NATIVE]);
    await mountSettled(room({ brain: null }));
    expect(screen.getByRole('button', { name: BRAIN })).toHaveTextContent('Default brain');
  });

  it('sends the brain alone when only the brain changed', async () => {
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    const onSave = vi.fn();
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }), onSave);
    await pick(BRAIN, 'Native');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledTimes(1);
    // Only what changed: the backend leaves an absent key untouched, and
    // re-sending the model here is exactly what the lock below prevents.
    expect(onSave.mock.calls[0][0] as RoomPatch).toEqual({ brain: 'native' });
  });

  it('sends null to clear rather than the empty-string sentinel', async () => {
    offer([CLAUDE, NATIVE]);
    const onSave = vi.fn();
    await mountSettled(room({ brain: 'native' }), onSave);
    await pick(BRAIN, 'Default brain');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave.mock.calls[0][0] as RoomPatch).toEqual({ brain: null });
  });

  it('disables the model select while a namespace-crossing change is pending', async () => {
    // The server applies `model` first and then clears it, so a model sent
    // alongside this change would be written and dropped in one request. The
    // user sees the consequence here rather than in the response.
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }));
    const model = () => screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    const effort = () => screen.getByRole('button', { name: EFFORT }) as HTMLButtonElement;
    expect(model().disabled).toBe(false);
    expect(screen.queryByText(CLEAR_WARNING)).toBeNull();

    await pick(BRAIN, 'Native');

    expect(model().disabled).toBe(true);
    // Effort goes with the model: the server clears the pair, because the two
    // were set as one.
    expect(effort().disabled).toBe(true);
    expect(screen.getByText(CLEAR_WARNING)).toBeTruthy();
  });

  it('leaves the model select alone for a move inside one namespace', async () => {
    // The converse, and what stops the assertion above passing against a modal
    // that disables on any brain change at all. `claude_code` and `tmux_claude`
    // share the anthropic namespace, so the pin survives the move.
    offer([CLAUDE, TMUX]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }));
    await pick(BRAIN, 'Tmux Claude');
    const model = screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    expect(model.disabled).toBe(false);
    expect(screen.queryByText(CLEAR_WARNING)).toBeNull();
  });

  // The outgoing brain is the one the modal is most likely not to have on its
  // menu, and reading it as unknown is what made the first cut of this control
  // over-lock: it warned and dropped a model edit the server would have kept.
  // `brain_namespaces` and `inherited_brain` are what close that, and each of
  // the three shapes below is a case only one of them answers.
  it('does not lock an unpinned room moving inside the inherited namespace', async () => {
    // The commonest change there is, and the one the first cut got wrong: the
    // server resolves an absent pin through `resolve_brain_kind` to the
    // deployment's real namespace, so anthropic to anthropic clears nothing.
    offer([CLAUDE, TMUX], CLAUDE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: null, model: 'claude-opus-5' }));
    await pick(BRAIN, 'Claude Code');
    const model = screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    expect(model.disabled).toBe(false);
    expect(screen.queryByText(CLEAR_WARNING)).toBeNull();
  });

  it('locks an unpinned room moving out of the inherited namespace', async () => {
    offer([CLAUDE, NATIVE], CLAUDE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: null, model: 'claude-opus-5' }));
    await pick(BRAIN, 'Native');
    const model = screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    expect(model.disabled).toBe(true);
  });

  it('does not lock a clear back to a same-namespace inherited brain', async () => {
    // The same question asked in the other direction: `brainValue` is now the
    // empty sentinel, and reading *that* as unknown locked every clear.
    offer([CLAUDE, NATIVE], CLAUDE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }));
    await pick(BRAIN, 'Default brain');
    const model = screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    expect(model.disabled).toBe(false);
  });

  it('locks a clear whose inherited brain is in another namespace', async () => {
    offer([CLAUDE, NATIVE], NATIVE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }));
    await pick(BRAIN, 'Default brain');
    const model = screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    expect(model.disabled).toBe(true);
  });

  it('reads a pin the operator dropped from the allowlist off the wider map', async () => {
    // `tmux_claude` is not on the menu, and the server still resolves it — so
    // moving to `claude_code` stays inside anthropic and keeps the pin.
    offer([CLAUDE, NATIVE], CLAUDE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'tmux_claude', model: 'claude-opus-5' }));
    await pick(BRAIN, 'Claude Code');
    const model = screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    expect(model.disabled).toBe(false);
  });

  it('still locks when the outgoing kind is in neither the menu nor the map', async () => {
    // The residual unknown, and the direction that stays safe: a kind this
    // build cannot construct has no namespace anywhere, and the server clears
    // a pin whose portability it could not establish.
    offer([CLAUDE, NATIVE], CLAUDE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'ghost_brain', model: 'claude-opus-5' }));
    await pick(BRAIN, 'Claude Code');
    const model = screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    expect(model.disabled).toBe(true);
  });

  it('drops a model already picked when the brain change locks it', async () => {
    // Order matters: pick a model, then cross a namespace. The lock is read by
    // `modelChanged` as well as by the control, so the abandoned pick is not
    // sent — the alternative is the server writing it and clearing it, which
    // reads as the pick not having taken.
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    const onSave = vi.fn();
    await mountSettled(room({ brain: 'claude_code' }), onSave);
    await pick(MODEL, 'opus (claude-opus-5)');
    await pick(BRAIN, 'Native');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave.mock.calls[0][0] as RoomPatch).toEqual({ brain: 'native' });
  });

  it('still saves a model change on its own', async () => {
    // The control for the two above: nothing about the lock may cost an
    // ordinary model edit in a room whose brain is not being touched.
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    const onSave = vi.fn();
    await mountSettled(room({ brain: 'claude_code' }), onSave);
    await pick(MODEL, 'opus (claude-opus-5)');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave.mock.calls[0][0] as RoomPatch).toEqual({ model: 'claude-opus-5' });
  });

  it('re-seeds the control when the modal is reused for another room', async () => {
    // One instance is reused across rooms, so leaked state here would offer one
    // room's brain as another's — the same hazard the name and model fields
    // already re-seed against.
    offer([CLAUDE, NATIVE]);
    const { rerender } = await mountSettled(room({ id: 1, brain: 'native' }));
    expect(screen.getByRole('button', { name: BRAIN })).toHaveTextContent('Native');
    await rerender({ room: room({ id: 2, brain: 'claude_code' }) });
    expect(screen.getByRole('button', { name: BRAIN })).toHaveTextContent('Claude Code');
  });

  it('drops a model list that resolved after the modal moved on', async () => {
    // The fetch is per room now, so a stale resolution would paint one room's
    // aliases into another's dropdown — which before the scoping was harmless
    // by construction, since every room got the same list.
    offer([CLAUDE, NATIVE]);
    let releaseFirst!: (v: { value: string; label: string }[]) => void;
    models.mockImplementation((roomId?: number) => {
      if (roomId === 1) return new Promise((res) => (releaseFirst = res));
      return Promise.resolve([{ value: 'endpoint/m', label: 'fast' }]);
    });
    const { rerender } = mount(room({ id: 1 }));
    await rerender({ room: room({ id: 2 }) });
    await Promise.resolve();
    await Promise.resolve();
    // Room 1's fetch lands late, naming a model room 2 cannot run.
    releaseFirst([{ value: 'claude-opus-5', label: 'opus' }]);
    await Promise.resolve();
    await Promise.resolve();
    await pick(MODEL, 'fast (endpoint/m)');
    expect(screen.queryByText('opus (claude-opus-5)')).toBeNull();
  });

  it("asks for this room's own model choices", async () => {
    // D5 Rule 2: a surface that offers a model name lists the aliases of the
    // brain that would have to run it, and the catalogue has no room of its own
    // — the id is what scopes it.
    offer([CLAUDE, NATIVE]);
    await mountSettled(room({ id: 7 }));
    expect(models).toHaveBeenCalledWith(7);
  });
});
