import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

// The members and group panes fetch on mount. Answered with an empty room so
// they settle quietly; their own behaviour is in their own test files.
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
  getRoomMembers: vi.fn(async () => ({ members: [], can_manage: false, message_count: 0 })),
  getChatUsers: vi.fn(async () => ({ users: [] })),
  getRoomGroup: vi.fn(async () => ({
    group_id: null,
    group_name: null,
    can_set: false,
    refusal: null,
    choices: [],
  })),
});

// The component asks the autocomplete providers for the model and brain
// dropdowns on mount, and the real ones reach the API. Both are `vi.fn`s the
// brain cases below reprogram per test; the default is the shipped deployment,
// where the operator has listed no selectable kinds.
vi.mock('$lib/components/chat/autocomplete/providers', () => ({
  getBaseModelChoices: vi.fn(async () => []),
  getDefaultModel: vi.fn(async () => null),
  getSelectableBrains: vi.fn(async () => []),
  getBrainNamespaces: vi.fn(async () => ({})),
  getInheritedBrain: vi.fn(async () => null),
}));

import RoomSettings from './RoomSettings.svelte';
import {
  getBaseModelChoices,
  getBrainNamespaces,
  getDefaultModel,
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
/** Every kind the server knows, which is what `brain_namespaces` carries —
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
  const CROSSING_NOTE = /listing the models the new brain can run/i;

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

  it('clears the stale pin explicitly when the brain crosses', async () => {
    // The selects are live now (ISSUE-417) and the crossing effect resets them
    // to "Default model", so the modal sends what it is showing rather than
    // leaving the server to infer it. Same end state as the server's own
    // clearing rule, arrived at by saying so.
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    const onSave = vi.fn();
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }), onSave);
    await pick(BRAIN, 'Native');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0] as RoomPatch).toEqual({
      brain: 'native',
      model: null,
    });
  });

  it('sends the brain alone when the room had no pin to clear', async () => {
    // The backend leaves an absent key untouched, so a room with nothing
    // stored must not grow a redundant `model: null`.
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    const onSave = vi.fn();
    await mountSettled(room({ brain: 'claude_code' }), onSave);
    await pick(BRAIN, 'Native');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
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

  it('keeps the model select usable across a namespace-crossing change', async () => {
    // The selects used to be disabled here, because the server applied `model`
    // first and the brain change then cleared it. The server applies the brain
    // first now and validates a model in the same body against it, so both go
    // in one save (ISSUE-417) — the control stays live and says what it is
    // listing.
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }));
    const model = () => screen.getByRole('button', { name: MODEL }) as HTMLButtonElement;
    const effort = () => screen.getByRole('button', { name: EFFORT }) as HTMLButtonElement;
    expect(model().disabled).toBe(false);
    expect(screen.queryByText(CROSSING_NOTE)).toBeNull();

    await pick(BRAIN, 'Native');

    expect(model().disabled).toBe(false);
    expect(effort().disabled).toBe(false);
    expect(screen.getByText(CROSSING_NOTE)).toBeTruthy();
  });

  it("asks for the pending brain's models when the selection crosses", async () => {
    // The room still holds its old brain until the save lands, so `room_id`
    // alone would offer the outgoing brain's list for a change already made.
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ id: 7, brain: 'claude_code' }));
    models.mockClear();
    await pick(BRAIN, 'Native');
    expect(models).toHaveBeenCalledWith(7, 'native');
  });

  it('names the model an unpinned room runs', async () => {
    // #548: a bare "Default model" hid that the default had moved past every
    // alias the list offered.
    const defaultModel = vi.mocked(getDefaultModel);
    defaultModel.mockResolvedValue('claude-opus-5-5');
    try {
      models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
      await mountSettled(room({ brain: 'claude_code' }));
      expect(screen.getByRole('button', { name: MODEL })).toHaveTextContent(
        'Default model (claude-opus-5-5)',
      );
      expect(defaultModel).toHaveBeenCalledWith(1, undefined);
    } finally {
      defaultModel.mockReset();
      defaultModel.mockResolvedValue(null);
    }
  });

  it('keeps listing a pin no alias targets any more', async () => {
    // A room pinned to the previous Opus before the aliases moved on still
    // runs it; the select has to say so rather than show an unlisted value.
    models.mockResolvedValue([{ value: 'claude-opus-5-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }));
    expect(screen.getByRole('button', { name: MODEL })).toHaveTextContent('claude-opus-5');
    expect(screen.getByRole('button', { name: MODEL })).not.toHaveTextContent('opus (');
  });

  it('resets a stale pin to the brain default as the list changes', async () => {
    // The select must not go on showing an id the new list does not hold.
    offer([CLAUDE, NATIVE]);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }));
    expect(screen.getByRole('button', { name: MODEL })).toHaveTextContent('opus (claude-opus-5)');
    await pick(BRAIN, 'Native');
    expect(screen.getByRole('button', { name: MODEL })).toHaveTextContent('Default model');
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
    expect(screen.queryByText(CROSSING_NOTE)).toBeNull();
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
    expect(screen.queryByText(CROSSING_NOTE)).toBeNull();
  });

  it('reads an unpinned room moving out of the inherited namespace as crossing', async () => {
    offer([CLAUDE, NATIVE], CLAUDE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: null, model: 'claude-opus-5' }));
    await pick(BRAIN, 'Native');
    expect(screen.getByText(CROSSING_NOTE)).toBeTruthy();
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

  it('reads a clear whose inherited brain is elsewhere as crossing', async () => {
    offer([CLAUDE, NATIVE], NATIVE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'claude_code', model: 'claude-opus-5' }));
    await pick(BRAIN, 'Default brain');
    expect(screen.getByText(CROSSING_NOTE)).toBeTruthy();
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

  it('still reads an unknown outgoing kind as crossing', async () => {
    // The residual unknown, and the direction that stays safe: a kind this
    // build does not know has no namespace anywhere, and the server clears a
    // pin whose portability it could not establish.
    offer([CLAUDE, NATIVE], CLAUDE);
    models.mockResolvedValue([{ value: 'claude-opus-5', label: 'opus' }]);
    await mountSettled(room({ brain: 'ghost_brain', model: 'claude-opus-5' }));
    await pick(BRAIN, 'Claude Code');
    expect(screen.getByText(CROSSING_NOTE)).toBeTruthy();
  });

  it('drops a model picked for the outgoing brain when the selection crosses', async () => {
    // Order matters: pick a model, then cross a namespace. That pick was made
    // against the previous brain's list and cannot run on the new one, so the
    // crossing effect resets it rather than sending an id the server would
    // refuse.
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
    // `undefined` for the brain: the room's own is what scopes an unedited
    // modal, and the pending-selection override is the crossing case above.
    expect(models).toHaveBeenCalledWith(7, undefined);
  });
});

describe('RoomSettings — colour', () => {
  afterEach(cleanup);

  const swatch = (label: string) => screen.getByRole('radio', { name: label }) as HTMLInputElement;

  it('offers the palette plus a no-colour option', () => {
    mount(room());
    // Every entry is pickable by its accessible name, which is what a radio
    // group buys over a row of styled buttons.
    for (const label of ['Rose', 'Coral', 'Citron', 'Green', 'Teal', 'Sky', 'Indigo', 'Plum']) {
      expect(swatch(label)).toBeTruthy();
    }
    expect(swatch('No colour')).toBeTruthy();
  });

  it('initializes from room.color', () => {
    mount(room({ color: 'teal' }));
    expect(swatch('Teal').checked).toBe(true);
    expect(swatch('No colour').checked).toBe(false);
  });

  it('reads a room with no colour as the no-colour option', () => {
    mount(room({ color: null }));
    expect(swatch('No colour').checked).toBe(true);
  });

  it('sends only the colour when only the colour changed', async () => {
    // The sparse patch is deliberate: a name-only edit must not re-send a model
    // the backend might now reject, and the same holds in reverse.
    const onSave = vi.fn();
    mount(room({ name: 'general' }), onSave);
    await fireEvent.click(swatch('Plum'));
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledWith({ color: 'plum' });
  });

  it('sends null to clear rather than the empty-string sentinel', async () => {
    const onSave = vi.fn();
    mount(room({ color: 'rose' }), onSave);
    await fireEvent.click(swatch('No colour'));
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledWith({ color: null });
  });

  it('does not send the colour when something else changed', async () => {
    const onSave = vi.fn();
    mount(room({ name: 'general', color: 'sky' }), onSave);
    await fireEvent.input(screen.getByPlaceholderText('Room name'), {
      target: { value: 'renamed' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledWith({ name: 'renamed' });
  });

  it('folds a stored name the palette no longer carries onto no-colour', async () => {
    // The sidebar already renders such a room untinted (roomColorVar returns
    // null), so the picker has to agree. Seeded raw it would match no radio —
    // reading as unset while reporting no change — and the first touch of any
    // control would write over a value with no path back to it.
    const onSave = vi.fn();
    mount(room({ name: 'general', color: 'mauve' }), onSave);
    expect(swatch('No colour').checked).toBe(true);
    // And it opens clean, so an unrelated edit sends no colour key.
    await fireEvent.input(screen.getByPlaceholderText('Room name'), {
      target: { value: 'renamed' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledWith({ name: 'renamed' });
  });

  it('re-seeds the picker when the modal is reused for another room', async () => {
    // The page keeps one instance across rooms, so a stale selection here would
    // offer to repaint the new room with the old room's colour.
    const { rerender } = mount(room({ id: 1, color: 'teal' }));
    expect(swatch('Teal').checked).toBe(true);
    await rerender({ room: room({ id: 2, color: 'green' }) });
    expect(swatch('Green').checked).toBe(true);
    expect(swatch('Teal').checked).toBe(false);
  });
});

// Multiplayer Stage 17. In a shared room the room-wide settings are the host's,
// so another member sees them and cannot change them; colour stays theirs.
describe('RoomSettings — a shared room another member hosts', () => {
  afterEach(cleanup);

  const hosted = (refusal: string | null, isHost = false) =>
    room({
      shared: true,
      policy: {
        host: 'alice',
        is_host: isHost,
        guest_reply: 'direct',
        settings_refusal: refusal,
      },
    });
  const REFUSAL = "Only this room's host (alice) can change its settings.";

  it("shows the server's reason and makes the room-wide controls read-only", async () => {
    await mountSettled(hosted(REFUSAL));
    expect(screen.getByText(REFUSAL)).toBeTruthy();
    const name = screen.getByPlaceholderText('Room name') as HTMLInputElement;
    expect(name.readOnly).toBe(true);
    const model = screen.getByRole('button', { name: 'Room model default' }) as HTMLButtonElement;
    expect(model.disabled).toBe(true);
    const guests = screen.getByRole('button', {
      name: 'How guests are answered',
    }) as HTMLButtonElement;
    expect(guests.disabled).toBe(true);
    expect(screen.queryByRole('button', { name: PROMOTE_LABEL })).toBeNull();
  });

  it("still saves the member's own colour, and nothing room-wide", async () => {
    const onSave = vi.fn();
    await mountSettled(hosted(REFUSAL), onSave);
    const name = screen.getByPlaceholderText('Room name') as HTMLInputElement;
    await fireEvent.input(name, { target: { value: 'renamed' } });
    const tint = screen.getAllByRole('radio').find((r) => (r as HTMLInputElement).value !== '');
    await fireEvent.click(tint!);
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledTimes(1);
    const patch = onSave.mock.calls[0][0] as RoomPatch;
    expect(Object.keys(patch)).toEqual(['color']);
  });

  it('lets the host change how guests are answered', async () => {
    const onSave = vi.fn();
    await mountSettled(hosted(null, true), onSave);
    await pick('How guests are answered', 'Propose the answer to the host first');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledWith({ guest_reply: 'held' });
  });

  it('shows a private room no guest policy', async () => {
    await mountSettled(room());
    expect(screen.queryByRole('button', { name: 'How guests are answered' })).toBeNull();
  });

  // Email on rooms: a correspondent's mail runs as the host, so an email
  // thread room has no guest reply setting to show.
  it('hides the guest setting in an email thread room', async () => {
    await mountSettled(
      room({
        shared: true,
        policy: {
          host: 'alice',
          is_host: true,
          guest_reply: 'held',
          email_thread: true,
          settings_refusal: null,
        },
      }),
    );
    expect(screen.queryByRole('button', { name: 'How guests are answered' })).toBeNull();
  });
});

// ISSUE-640: the room's own speech mode, beside the Guests field.
describe('RoomSettings — when the bot replies', () => {
  afterEach(cleanup);

  const SPEAK = 'When to reply to a message not addressed to the bot';
  const shared = (over: Record<string, unknown> = {}) =>
    room({
      shared: true,
      policy: {
        host: 'alice',
        is_host: true,
        guest_reply: 'direct',
        speech_mode: null,
        effective_speech_mode: 'mention',
        deployment_speech_mode: 'mention',
        settings_refusal: null,
        ...over,
      },
    });

  it('names what following the deployment means', async () => {
    await mountSettled(shared());
    expect(screen.getByRole('button', { name: SPEAK })).toBeTruthy();
    expect(screen.getByText('Follow deployment (Only when addressed)')).toBeTruthy();
  });

  it('lets the host opt the room into the classifier', async () => {
    const onSave = vi.fn();
    await mountSettled(shared(), onSave);
    await pick(SPEAK, 'Decide from context');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledWith({ speech_mode: 'classifier' });
  });

  it('sends null to follow the deployment again', async () => {
    const onSave = vi.fn();
    await mountSettled(shared({ speech_mode: 'off', effective_speech_mode: 'off' }), onSave);
    await pick(SPEAK, 'Follow deployment (Only when addressed)');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledWith({ speech_mode: null });
  });

  it('warns when every turn is answered', async () => {
    await mountSettled(shared());
    expect(screen.queryByText(/answer every message/)).toBeNull();
    await pick(SPEAK, 'Every turn');
    expect(screen.getByText(/answer every message anyone sends/)).toBeTruthy();
  });

  it('is read-only for a member who is not the host', async () => {
    await mountSettled(shared({ is_host: false }));
    const control = screen.getByRole('button', { name: SPEAK }) as HTMLButtonElement;
    expect(control.disabled).toBe(true);
    expect(screen.getByText(/host, alice, sets this/)).toBeTruthy();
  });

  it('is hidden in an email thread room', async () => {
    await mountSettled(shared({ email_thread: true }));
    expect(screen.queryByRole('button', { name: SPEAK })).toBeNull();
  });

  it('is hidden in a private room', async () => {
    await mountSettled(room());
    expect(screen.queryByRole('button', { name: SPEAK })).toBeNull();
  });
});

describe('a phone room (room-surface-model Stage 24)', () => {
  it('shows the binding read-only and offers no Talk promote', () => {
    mount(room({ origin: 'sms', phone_surface: 'sms', read_only: true, name: 'SMS' }));
    expect(screen.getByText('Connected to')).toBeTruthy();
    expect(screen.getByText(/transcript of an? SMS conversation/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: PROMOTE_LABEL })).toBeNull();
    expect(screen.queryByText('Nextcloud Talk')).toBeNull();
  });

  it('names the private email room as an email transcript (email on rooms, stage 3)', () => {
    mount(room({ origin: 'email', phone_surface: 'email', read_only: true, name: 'Email' }));
    expect(screen.getByText(/transcript of an email conversation/)).toBeTruthy();
    expect(screen.getByText(/reply by email/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: PROMOTE_LABEL })).toBeNull();
  });

  it('names a WhatsApp group as a read-only group', () => {
    mount(
      room({ origin: 'whatsapp', phone_surface: 'whatsapp', read_only: true, phone_group: true }),
    );
    expect(screen.getByText(/this room is a WhatsApp group\. It is read-only here/)).toBeTruthy();
    expect(screen.queryByText(/transcript of a WhatsApp conversation/)).toBeNull();
  });

  it('leaves an ordinary web room on the Talk line', () => {
    mount(room());
    expect(screen.queryByText('Connected to')).toBeNull();
    expect(screen.getByRole('button', { name: PROMOTE_LABEL })).toBeTruthy();
  });
});

describe('RoomSettings — delete', () => {
  afterEach(() => cleanup());

  it('shows one dialog at a time, and a cancelled delete returns to settings with edits kept', async () => {
    const onClose = vi.fn();
    render(RoomSettings, {
      props: {
        open: true,
        room: room(),
        onSave: vi.fn(),
        onDelete: vi.fn(),
        onPromote: vi.fn(),
        onClose,
      },
    });
    await fireEvent.input(screen.getByDisplayValue('general'), { target: { value: 'renamed' } });

    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'Delete room' })).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'Room settings' })).toBeTruthy();
    expect(screen.getByDisplayValue('renamed')).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('RoomSettings — show in room list', () => {
  afterEach(() => cleanup());

  const thread = (over: Partial<ChatRoom> = {}) =>
    room({ origin: 'email', read_only: true, email_thread: true, listed: false, ...over });
  const box = () => screen.queryByRole('checkbox', { name: 'Show in room list' });

  it('is offered for an email thread only', () => {
    mount(thread());
    expect(box()).toBeTruthy();
    cleanup();
    mount(room());
    expect(box()).toBeNull();
    cleanup();
    mount(room({ origin: 'email', phone_surface: 'email', read_only: true }));
    expect(box()).toBeNull();
  });

  it('starts from the room and saves listed alone', async () => {
    const onSave = vi.fn();
    mount(thread(), onSave);
    expect((box() as HTMLInputElement).checked).toBe(false);
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await fireEvent.click(box()!);
    expect(save.disabled).toBe(false);
    await fireEvent.click(save);
    expect(onSave).toHaveBeenCalledWith({ listed: true } satisfies RoomPatch);
  });

  it('unlists a listed thread', async () => {
    const onSave = vi.fn();
    mount(thread({ listed: true }), onSave);
    expect((box() as HTMLInputElement).checked).toBe(true);
    await fireEvent.click(box()!);
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledWith({ listed: false });
  });
});

describe('RoomSettings — add a member', () => {
  afterEach(() => {
    cleanup();
    api.getRoomMembers.mockResolvedValue({ members: [], can_manage: false, message_count: 0 });
    api.getChatUsers.mockResolvedValue({ users: [] });
  });

  /** bits-ui's select commits on pointerup, not on click. */
  async function pickMember(optionLabel: string) {
    const trigger = await screen.findByRole('button', { name: 'Member to add' });
    await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(trigger);
    const item = (await screen.findByText(optionLabel)).closest('[data-select-item]');
    if (!item) throw new Error(`no option ${optionLabel}`);
    await fireEvent.pointerMove(item, { pointerType: 'mouse' });
    await fireEvent.pointerDown(item, { pointerType: 'mouse', button: 0 });
    await fireEvent.pointerUp(item, { pointerType: 'mouse', button: 0 });
  }

  it('shows one dialog at a time, adds on confirm, and returns to settings with edits kept', async () => {
    const alice = { user_id: 'alice', display_name: 'Alice', is_owner: true };
    const bob = { user_id: 'bob', display_name: 'Bob', is_owner: false };
    api.getRoomMembers.mockResolvedValue({ members: [alice], can_manage: true, message_count: 7 });
    api.getChatUsers.mockResolvedValue({
      users: [
        { user_id: 'alice', display_name: 'Alice' },
        { user_id: 'bob', display_name: 'Bob' },
      ],
    });
    api.addRoomMember.mockResolvedValue({ member: bob });
    const onClose = vi.fn();
    const onMembersChanged = vi.fn();
    render(RoomSettings, {
      props: {
        open: true,
        room: room(),
        userId: 'alice',
        onSave: vi.fn(),
        onDelete: vi.fn(),
        onPromote: vi.fn(),
        onClose,
        onMembersChanged,
      },
    });
    await fireEvent.input(screen.getByDisplayValue('general'), { target: { value: 'renamed' } });
    await pickMember('Bob');
    await fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'Add Bob' })).toBeTruthy();
    expect(screen.getByText(/Bob will see all 7 messages/)).toBeTruthy();
    expect(api.addRoomMember).not.toHaveBeenCalled();

    api.getRoomMembers.mockResolvedValue({
      members: [alice, bob],
      can_manage: true,
      message_count: 7,
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Add and share the history' }));
    await waitFor(() => expect(api.addRoomMember).toHaveBeenCalledWith(1, 'bob'));
    await waitFor(() => expect(onMembersChanged).toHaveBeenCalled());
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'Room settings' })).toBeTruthy();
    expect(await screen.findByText('Bob')).toBeTruthy();
    expect(screen.getByDisplayValue('renamed')).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('a cancelled add returns to settings and sends nothing', async () => {
    api.getRoomMembers.mockResolvedValue({
      members: [{ user_id: 'alice', display_name: 'Alice', is_owner: true }],
      can_manage: true,
      message_count: 1,
    });
    api.getChatUsers.mockResolvedValue({ users: [{ user_id: 'bob', display_name: 'Bob' }] });
    api.addRoomMember.mockClear();
    const onClose = vi.fn();
    render(RoomSettings, {
      props: {
        open: true,
        room: room(),
        userId: 'alice',
        onSave: vi.fn(),
        onDelete: vi.fn(),
        onPromote: vi.fn(),
        onClose,
      },
    });
    await pickMember('Bob');
    await fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'Room settings' })).toBeTruthy();
    expect(api.addRoomMember).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
