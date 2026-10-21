<script lang="ts">
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { invalidateNavUnreadCounts } from '$lib/stores/navUnread';
  import { untrack } from 'svelte';
  import { addRoomMember } from '$lib/api';
  import type {
    ChatRoom,
    GuestReply,
    RoomPatch,
    RoomNotifications,
    SelectableBrain,
    SpeechDisposition,
    SpeechMode,
  } from '$lib/api';
  import {
    Modal,
    Button,
    ConfirmDialog,
    Field,
    Input,
    Select,
    type SelectOption,
    Checkbox,
    CopyField,
  } from '@minot/ui';
  import RoomMembers, { type AddRequest } from './RoomMembers.svelte';
  import RoomGroupLink from './RoomGroupLink.svelte';
  import { ROOM_COLORS, ROOM_COLOR_LABELS, roomColorVar } from '$lib/roomColors';
  import {
    getBaseModelChoices,
    getDefaultModel,
    getBrainNamespaces,
    getInheritedBrain,
    getSelectableBrains,
  } from '$lib/components/chat/autocomplete/providers';

  interface Props {
    open?: boolean;
    room: ChatRoom;
    onSave: (patch: RoomPatch) => void;
    onDelete: () => void;
    onPromote?: () => void;
    onClose: () => void;
    /** The viewer's user id, for the members pane's Leave. */
    userId?: string;
    /** Membership changed, so the room's sharing state may have. */
    onMembersChanged?: () => void;
    /** The viewer left the room from the members pane. */
    onLeft?: () => void;
  }

  let {
    open = $bindable(true),
    room,
    onSave,
    onDelete,
    onPromote,
    onClose,
    userId,
    onMembersChanged,
    onLeft,
  }: Props = $props();

  // In a shared room the name, model, effort, brain and the Talk conversation
  // apply to every member's turn, so they are the host's (multiplayer Stage
  // 16). The server's own refusal says why, and the controls show the values
  // without offering to change them; personal settings stay editable.
  // A group-owned room's settings are its group owners', not its host's, and
  // its membership is the group's: no per-room delete, guest policy or Talk.
  // One read by a single member has no policy, so `is_owner` locks it there.
  const managedBy = $derived(room.managed_by ?? null);
  const lockedReason = $derived(
    room.policy?.settings_refusal ??
      (managedBy && !managedBy.is_owner
        ? `Only an owner of ${managedBy.display_name} can change this room's settings.`
        : null),
  );
  const locked = $derived(!!lockedReason);
  const policyEditable = $derived(managedBy ? !locked : !!room.policy?.is_host);

  // How a guest's turn is answered (D11). Only a shared room has a policy to
  // show, and only its host may change it.
  const GUEST_REPLY_OPTIONS: SelectOption[] = [
    { value: 'direct', label: 'Answer in the room' },
    { value: 'held', label: 'Propose the answer to the host first' },
    { value: 'off', label: 'Record only, never answer' },
  ];
  let guestReplyValue = $state<GuestReply>(untrack(() => room.policy?.guest_reply ?? 'direct'));
  const guestReplyChanged = $derived(!!room.policy && guestReplyValue !== room.policy.guest_reply);

  // When the bot answers an unaddressed turn here (ISSUE-640). "" follows the
  // deployment's mode, which the server names so the option can say what it is.
  const SPEECH_MODE_LABELS: Record<string, string> = {
    mention: 'Only when addressed',
    classifier: 'Decide from context',
    off: 'Every turn',
  };
  const speechModeOptions = $derived<SelectOption[]>([
    {
      value: '',
      label: `Follow deployment (${SPEECH_MODE_LABELS[room.policy?.deployment_speech_mode ?? ''] ?? room.policy?.deployment_speech_mode ?? 'unknown'})`,
    },
    { value: 'mention', label: SPEECH_MODE_LABELS.mention },
    { value: 'classifier', label: SPEECH_MODE_LABELS.classifier },
    { value: 'off', label: SPEECH_MODE_LABELS.off },
  ]);
  let speechModeValue = $state<string>(untrack(() => room.policy?.speech_mode ?? ''));
  const speechModeChanged = $derived(
    !!room.policy && speechModeValue !== (room.policy.speech_mode ?? ''),
  );
  const showSpeechMode = $derived(
    !!room.policy && !room.policy.email_thread && room.policy.speech_mode !== undefined,
  );

  // How wide "for the bot" is when the classifier decides (ISSUE-654). Read
  // only on the classifier, so the field says so when Replies is anything else.
  const DISPOSITION_LABELS: Record<string, string> = {
    reserved: 'Reserved',
    friendly: 'Friendly',
  };
  const dispositionOptions = $derived<SelectOption[]>([
    {
      value: '',
      label: `Follow deployment (${DISPOSITION_LABELS[room.policy?.deployment_disposition ?? ''] ?? room.policy?.deployment_disposition ?? 'unknown'})`,
    },
    { value: 'reserved', label: DISPOSITION_LABELS.reserved },
    { value: 'friendly', label: DISPOSITION_LABELS.friendly },
  ]);
  let dispositionValue = $state<string>(untrack(() => room.policy?.disposition ?? ''));
  const dispositionChanged = $derived(
    !!room.policy && dispositionValue !== (room.policy.disposition ?? ''),
  );
  const showDisposition = $derived(showSpeechMode && room.policy?.disposition !== undefined);
  const classifierSelected = $derived(
    (speechModeValue || room.policy?.deployment_speech_mode) === 'classifier',
  );

  // Model + effort defaults for this room (canonical values, shared Talk+web).
  // "" is the "instance default" sentinel (cleared on the backend as null).
  const EFFORT_OPTIONS: SelectOption[] = [
    { value: '', label: 'Default effort' },
    { value: 'low', label: 'low' },
    { value: 'medium', label: 'medium' },
    { value: 'high', label: 'high' },
    { value: 'xhigh', label: 'xhigh' },
    { value: 'max', label: 'max' },
  ];
  let modelOptions = $state<SelectOption[]>([{ value: '', label: 'Default model' }]);
  let modelValue = $state(untrack(() => room.model ?? ''));
  let effortValue = $state(untrack(() => room.effort ?? ''));

  // Base model choices (dedup + provider-alias-preferred labels) shared with
  // the room header badge, so the dropdown and the badge name a model the same.
  // Scoped to the brain that would have to run the model, so a room on another
  // model namespace is not offered ids it cannot run — which the PATCH would
  // reject anyway.
  //
  // That brain is the one **selected in this modal**, not the one the room
  // still holds (ISSUE-417). The room keeps its old brain until the save lands,
  // so keying on it offered the outgoing brain's models for a change the user
  // had already made — which is why the select used to be disabled on a
  // crossing change with "pick a new one after saving". Reading `brainValue`
  // inside the effect is what subscribes it, so switching the brain refetches.
  $effect(() => {
    // Both captured, and the result dropped if either has moved on: the fetch
    // is per room *and* per pending brain now, so a stale resolution would
    // otherwise paint one selection's aliases under another.
    const forRoom = room.id;
    const forBrain = brainValue;
    Promise.all([
      getBaseModelChoices(forRoom, forBrain || undefined),
      getDefaultModel(forRoom, forBrain || undefined),
    ]).then(([choices, defaultModel]) => {
      if (room.id !== forRoom || brainValue !== forBrain) return;
      // Show the canonical model id in parens next to the alias, so the pick
      // is unambiguous (e.g. `opus (claude-opus-4-8)`). The default names its
      // id too: without it the picker could not show the default had moved
      // past every alias (#548).
      // A pin no alias targets any more (the previous release, or an id set
      // with `!room model`) stays listed under its own id, so the select shows
      // what the room runs rather than a value it does not offer. Not for a
      // crossing brain, which clears the pin below.
      const pin = room.model;
      const keepPin =
        pin && !choices.some((c) => c.value === pin) && !untrack(() => crossesNamespace);
      modelOptions = [
        { value: '', label: defaultModel ? `Default model (${defaultModel})` : 'Default model' },
        ...choices.map((c) => ({ value: c.value, label: `${c.label} (${c.value})` })),
        ...(keepPin ? [{ value: pin, label: pin }] : []),
      ];
    });
  });

  // A model chosen for the previous brain cannot carry to a crossing one, and
  // the server would refuse it. Clearing it back to "Default model" as the list
  // changes is what keeps the select showing something it actually offers,
  // rather than a stale id rendered against a list that no longer holds it.
  $effect(() => {
    if (!crossesNamespace) return;
    untrack(() => {
      if (modelValue) modelValue = '';
      if (effortValue) effortValue = '';
    });
  });

  // The room's brain. The control exists only where the server offered kinds:
  // writing the pin is admin-only and the kinds are an operator allowlist, and
  // the endpoint collapses both conditions into an empty list, so emptiness is
  // the whole test rather than two the client could get out of step.
  let selectableBrains = $state<SelectableBrain[]>([]);
  let brainNamespaces = $state<Record<string, string>>({});
  let inheritedBrain = $state<SelectableBrain | null>(null);
  let brainValue = $state(untrack(() => room.brain ?? ''));
  $effect(() => {
    getSelectableBrains().then((brains) => (selectableBrains = brains));
    getBrainNamespaces().then((map) => (brainNamespaces = map));
    getInheritedBrain().then((b) => (inheritedBrain = b));
  });
  const showBrain = $derived(selectableBrains.length > 0);
  const brainOptions = $derived<SelectOption[]>([
    { value: '', label: 'Default brain' },
    ...selectableBrains.map((b) => ({ value: b.kind, label: b.label })),
  ]);
  const brainChanged = $derived(brainValue !== (room.brain ?? ''));

  // Whether saving this brain change would drop the room's model pin (D5
  // Rule 1), answered the way `commands._clear_pin_across_namespaces` answers
  // it rather than approximately.
  //
  // An empty kind is the *inherited* brain — the room pinning none on the way
  // in, or being cleared back to it on the way out — and the server resolves
  // that through `resolve_brain_kind` to a real namespace, so reading it as
  // unknown here would warn and lock on the two commonest changes there are.
  // `brain_namespaces` rather than `selectable_brains` for the same reason in
  // the other direction: a room can be pinned to a kind the operator has since
  // dropped from the allowlist, which the server still resolves and the picker
  // no longer lists. Unknown remains "not established", and never compares
  // equal, which is the direction the server takes for a kind it cannot build.
  function namespaceOf(kind: string): string | undefined {
    if (!kind) return inheritedBrain?.model_namespace;
    return brainNamespaces[kind];
  }
  const crossesNamespace = $derived.by(() => {
    if (!brainChanged) return false;
    const before = namespaceOf(room.brain ?? '');
    const after = namespaceOf(brainValue);
    return before === undefined || after === undefined || before !== after;
  });
  // There is deliberately no lock any more (ISSUE-417). The model and effort
  // selects used to be disabled while a namespace-crossing brain change was
  // pending, because the server applied `model` first and the brain change then
  // cleared it — so a model sent in the same body was written and dropped in
  // one request, and the caption told the user to come back after saving. The
  // server applies the brain first now and validates a model in the same body
  // against the brain being switched to, so both go in one save; the selects
  // stay live and the effect above repopulates them from the pending brain.

  // A room is on Talk when it originated there or has been promoted.
  const onTalk = $derived(room.origin === 'talk' || !!room.talk_token);
  // A *promoted* room keeps the control, relabelled (ISSUE-401). Its binding
  // can go stale — the Talk conversation deleted out from under it — and the
  // button is the only way back; hiding it once `talk_token` was set is what
  // made that state permanent from the app. The server decides whether
  // anything actually happens: it probes the bound conversation and refuses
  // unless Nextcloud says it is gone, so pressing this on a healthy room is
  // answered with "already connected" rather than a second Talk room.
  // Both keyed off one predicate: `origin` is optional in the type, and an
  // origin-less room with a talk_token otherwise passed canPromote while
  // failing isPromoted, mislabelling the button.
  // A phone room is not offered either: the server refuses to promote anything
  // but a web-origin room, and its binding is the user's phone identity.
  const canPromote = $derived(room.origin !== 'talk' && !room.phone_surface && !managedBy);
  const phoneLabel = $derived(
    room.phone_surface === 'sms'
      ? 'SMS'
      : room.phone_surface === 'whatsapp'
        ? 'WhatsApp'
        : room.phone_surface === 'email'
          ? 'email'
          : null,
  );
  const isPromoted = $derived(canPromote && !!room.talk_token);
  // An imported (Talk-origin) room is hidden per-user, not destroyed — this
  // must match the backend's hide condition (`reg.origin == 'talk'`), NOT
  // `onTalk`: a promoted web room (origin='web' + talk_token) is still hard-
  // deleted, so it must read as a delete, not a hide.
  const isImported = $derived(room.origin === 'talk');
  let promoting = $state(false);
  async function handlePromote() {
    if (!onPromote || promoting) return;
    promoting = true;
    try {
      await onPromote();
    } finally {
      promoting = false;
    }
  }

  // Local edit state. Re-seeded whenever the modal is opened for a different
  // room so reusing one component instance across rooms never leaks state.
  let name = $state(untrack(() => room.name));
  // A stored name the palette no longer carries folds to "no colour", which is
  // what `roomColorVar` already does for the sidebar row. Seeded raw it would
  // match no radio, so the picker would read as unset while `colorChanged`
  // stayed false — a modal whose display disagrees with its own state, one
  // touch away from writing over a value with no path back to it.
  const paletteValue = (c: string | null | undefined) => (roomColorVar(c) ? c! : '');
  let colorValue = $state(untrack(() => paletteValue(room.color)));
  // An email thread sits in the sidebar's "Email threads" group unless this
  // is set. Per member, like the colour, so the host lock does not apply.
  let listedValue = $state(untrack(() => !!room.listed));
  const NOTIFICATION_OPTIONS: SelectOption[] = [
    { value: 'default', label: 'Use account default' },
    { value: 'alerts', label: 'Alerts and badge' },
    { value: 'badge', label: 'Badge only' },
    { value: 'off', label: 'Off' },
  ];
  let notificationsValue = $state<RoomNotifications>(
    untrack(() => room.push_notifications ?? 'default'),
  );
  let showDeleteConfirm = $state(false);
  // A member add waiting on its confirmation, and why the last one failed.
  const pendingAdd = createConfirm<AddRequest>();
  let addError = $state('');
  // Bumped after a confirmed add so the members pane lists the new member.
  let membersEpoch = $state(0);
  let lastRoomId = $state(untrack(() => room.id));

  $effect(() => {
    if (room.id !== lastRoomId) {
      lastRoomId = room.id;
      name = room.name;
      colorValue = paletteValue(room.color);
      listedValue = !!room.listed;
      notificationsValue = room.push_notifications ?? 'default';
      modelValue = room.model ?? '';
      effortValue = room.effort ?? '';
      brainValue = room.brain ?? '';
      guestReplyValue = room.policy?.guest_reply ?? 'direct';
      speechModeValue = room.policy?.speech_mode ?? '';
      dispositionValue = room.policy?.disposition ?? '';
      showDeleteConfirm = false;
      pendingAdd.cancel();
      addError = '';
    }
  });

  const trimmed = $derived(name.trim());
  const nameChanged = $derived(trimmed.length > 0 && trimmed !== room.name);
  // Both are gated on the lock, so a model picked before the brain select was
  // touched is neither counted as a change nor sent: the server would write it
  // and clear it in the same request, which reads as the pick not having taken.
  const modelChanged = $derived(modelValue !== (room.model ?? ''));
  const effortChanged = $derived(effortValue !== (room.effort ?? ''));
  // Compared against the *folded* stored value, so a room holding a retired
  // name opens clean and its unrelated edits send no `color` key. Compared
  // against the raw one it would open dirty and a rename would silently clear
  // the stored value as a side effect. The row already renders untinted, so
  // the picker showing "no colour" is honest; picking one writes over it.
  const colorChanged = $derived(colorValue !== paletteValue(room.color));
  // Gated on `email_thread`: the server refuses `listed` on any other room.
  const listedChanged = $derived(!!room.email_thread && listedValue !== !!room.listed);
  const notificationsChanged = $derived(
    notificationsValue !== (room.push_notifications ?? 'default'),
  );
  // Saveable when anything changed, and the name is never blanked.
  const canSave = $derived(
    trimmed.length > 0 &&
      (notificationsChanged ||
        colorChanged ||
        listedChanged ||
        guestReplyChanged ||
        speechModeChanged ||
        dispositionChanged ||
        (!locked && (nameChanged || modelChanged || effortChanged || brainChanged))),
  );

  function handleSave() {
    if (!canSave) return;
    // Send only what changed. A name-only rename must not re-send a model
    // the backend might now reject (e.g. one retired from the alias table),
    // which would 400 the whole PATCH; the backend leaves absent fields
    // untouched.
    const patch: RoomPatch = {};
    if (!locked) {
      if (nameChanged) patch.name = trimmed;
      if (modelChanged) patch.model = modelValue || null;
      if (effortChanged) patch.effort = effortValue || null;
      if (brainChanged) patch.brain = brainValue || null;
    }
    if (colorChanged) patch.color = colorValue || null;
    if (listedChanged) patch.listed = listedValue;
    if (notificationsChanged) patch.push_notifications = notificationsValue;
    if (guestReplyChanged) patch.guest_reply = guestReplyValue;
    if (speechModeChanged) patch.speech_mode = (speechModeValue || null) as SpeechMode | null;
    if (dispositionChanged)
      patch.disposition = (dispositionValue || null) as SpeechDisposition | null;
    onSave(patch);
  }

  // Only one dialog is up at a time: settings steps aside while the delete or
  // add confirm is shown, and its unsaved edits are still here afterwards.
  // Hiding it for a confirm is not a close, so it must not reach `onClose`.
  // The add confirm lives here rather than in the members pane because that
  // pane is unmounted while settings is hidden.
  const confirming = $derived(showDeleteConfirm || pendingAdd.open);

  function handleOpenChange(next: boolean) {
    if (!next && !confirming) onClose();
  }

  // The add discloses the whole transcript, so the dialog states how much.
  function addDisclosure(request: AddRequest): string {
    const count = request.messageCount;
    return (
      `${request.displayName} will see all ${count} message${count === 1 ? '' : 's'} in this ` +
      'room, everything said here before they joined included.'
    );
  }

  function requestAdd(request: AddRequest) {
    addError = '';
    pendingAdd.ask(request);
  }

  async function confirmAdd() {
    const request = pendingAdd.target;
    pendingAdd.cancel();
    if (!request) return;
    const forRoom = room.id;
    try {
      await addRoomMember(forRoom, request.userId);
      invalidateNavUnreadCounts();
      if (forRoom !== room.id) return;
      membersEpoch += 1;
      onMembersChanged?.();
    } catch (e) {
      if (forRoom === room.id) addError = e instanceof Error ? e.message : 'That didn’t work.';
    }
  }
</script>

{#snippet modelNote()}
  {#if crossesNamespace}
    Now listing the models the new brain can run — the one this room had reads model names
    differently and has been cleared. Pick a new one here, or leave it on
    <em>Default model</em> to use that brain's own.
  {:else}
    Applies to every message in this room, on both web and Nextcloud Talk. A
    <code>!model</code> prefix still overrides it for a single message.
  {/if}
{/snippet}

{#snippet guestsNote()}
  {#if room.policy?.is_host}
    A guest's turn runs on your behalf, as this room's host.
  {:else if room.policy?.host}
    A guest's turn runs on behalf of this room's host, {room.policy.host}, who sets this.
  {:else}
    This room has no host, so guests are not answered until a member claims it.
  {/if}
{/snippet}

{#snippet repliesNote()}
  {#if managedBy}
    When the bot answers a message nobody addressed to it. Owners of {managedBy.display_name} set this.
  {:else if room.policy?.is_host}
    When the bot answers a message nobody addressed to it. You set this as the room's host.
  {:else if room.policy?.host}
    When the bot answers a message nobody addressed to it. This room's host, {room.policy.host},
    sets this.
  {:else}
    This room has no host, so nobody can change this until a member claims it.
  {/if}
{/snippet}

{#snippet dispositionNote()}
  Friendly also answers thanks or a remark about what the bot just said; reserved answers only a
  message addressed to it or asking it something.
  {#if managedBy}
    Owners of {managedBy.display_name} set this.
  {:else if room.policy?.is_host}
    You set this as the room's host.
  {:else if room.policy?.host}
    This room's host, {room.policy.host}, sets this.
  {:else}
    This room has no host, so nobody can change this until a member claims it.
  {/if}
  {#if !classifierSelected}
    This applies only when Replies is set to decide from context.
  {/if}
{/snippet}

<Modal
  open={open && !confirming}
  title="Room settings"
  onOpenChange={handleOpenChange}
  width="380px"
>
  {#if lockedReason}
    <p class="caption locked-note" role="note">{lockedReason}</p>
  {/if}
  <div class="vstack">
    <Field label="Name">
      <Input
        bind:value={name}
        maxlength={80}
        readonly={locked}
        placeholder="Room name"
        onkeydown={(e: KeyboardEvent) => {
          if (e.key === 'Enter') handleSave();
        }}
      />
    </Field>

    <!-- A fixed palette rather than a colour input: one user-picked value cannot
         read on both themes' --surface-raised, so the choice is among ours. Radios
         rather than buttons, because that is what a single-choice set is — it
         gives arrow-key movement and one tab stop for free (ISSUE-433). The
         field names the radios as a group. -->
    <Field label="Colour" description="Tints this room's row in the sidebar. Only you see it.">
      <div class="swatches">
        <label class="swatch none" class:selected={colorValue === ''} title="No colour">
          <input
            type="radio"
            name="room-color"
            value=""
            aria-label="No colour"
            bind:group={colorValue}
          />
          <span class="dot" aria-hidden="true"></span>
        </label>
        {#each ROOM_COLORS as c (c)}
          <label
            class="swatch"
            class:selected={colorValue === c}
            style:--swatch={roomColorVar(c)}
            title={ROOM_COLOR_LABELS[c]}
          >
            <input
              type="radio"
              name="room-color"
              value={c}
              aria-label={ROOM_COLOR_LABELS[c]}
              bind:group={colorValue}
            />
            <span class="dot" aria-hidden="true"></span>
          </label>
        {/each}
      </div>
    </Field>

    {#if room.email_thread}
      <Checkbox
        label="Show in room list"
        description="Email threads sit in the collapsed Email threads group, and their unread messages are not counted. Only you see this."
        bind:checked={listedValue}
      />
    {/if}

    <Field
      label="Notifications"
      description="Applies only to you. The account default is in Settings → Notifications. Off excludes this room from alerts and the app badge; its unread count stays visible in chat."
    >
      <Select
        value={notificationsValue}
        options={NOTIFICATION_OPTIONS}
        onValueChange={(v) => (notificationsValue = v as RoomNotifications)}
        ariaLabel="Room notifications"
        fullWidth
      />
    </Field>

    {#if showBrain}
      <Field
        label="Brain"
        description="Every turn in this room runs on this brain, and a room that names one does not fall back to another if it is unavailable."
      >
        <Select
          value={brainValue}
          options={brainOptions}
          onValueChange={(v) => (brainValue = v)}
          ariaLabel="Room brain"
          disabled={locked}
          fullWidth
        />
      </Field>
    {/if}

    <Field label="Model" description={modelNote}>
      <Select
        value={modelValue}
        options={modelOptions}
        onValueChange={(v) => (modelValue = v)}
        ariaLabel="Room model default"
        disabled={locked}
        fullWidth
      />
    </Field>

    <Field label="Effort">
      <Select
        value={effortValue}
        options={EFFORT_OPTIONS}
        onValueChange={(v) => (effortValue = v)}
        ariaLabel="Room effort default"
        disabled={locked}
        fullWidth
      />
    </Field>

    {#if room.policy && !room.policy.email_thread && !managedBy}
      <Field label="Guests" description={guestsNote}>
        <Select
          value={guestReplyValue}
          options={GUEST_REPLY_OPTIONS}
          onValueChange={(v) => (guestReplyValue = v as GuestReply)}
          ariaLabel="How guests are answered"
          disabled={!policyEditable}
          fullWidth
        />
      </Field>
    {/if}

    {#if room.policy && showSpeechMode}
      <Field
        label="Replies"
        description={repliesNote}
        warning={speechModeValue === 'off'
          ? 'The bot will answer every message anyone sends in this room.'
          : undefined}
      >
        <Select
          value={speechModeValue}
          options={speechModeOptions}
          onValueChange={(v) => (speechModeValue = v)}
          ariaLabel="When to reply to a message not addressed to the bot"
          disabled={!policyEditable}
          fullWidth
        />
      </Field>
    {/if}

    {#if room.policy && showDisposition}
      <Field label="Disposition" description={dispositionNote}>
        <Select
          value={dispositionValue}
          options={dispositionOptions}
          onValueChange={(v) => (dispositionValue = v)}
          ariaLabel="How readily the bot replies when it decides from context"
          disabled={!policyEditable}
          fullWidth
        />
      </Field>
    {/if}

    <Field label="Members">
      {#key membersEpoch}
        <RoomMembers
          roomId={room.id}
          {userId}
          talkBound={onTalk}
          phoneLabel={room.read_only && !room.phone_group ? phoneLabel : null}
          onChanged={onMembersChanged}
          {onLeft}
          onRequestAdd={requestAdd}
          {addError}
        />
      {/key}
    </Field>

    <Field label="Group">
      <RoomGroupLink roomId={room.id} />
    </Field>

    <CopyField
      label="Room token"
      value={room.token}
      monospace
      description="Use this to link to or route output to this room."
    />

    {#if phoneLabel}
      <!-- Read-only by design: the binding is the user's phone identity, which
           belongs to the connected-services flow, so there is nothing to unbind
           here. -->
      <Field label="Connected to">
        <p class="caption phone-binding">
          {#if room.phone_group}
            {phoneLabel} — this room is a {phoneLabel} group. It is read-only here; write in the group
            on {phoneLabel}.
          {:else}
            {phoneLabel} — this room is the transcript of {phoneLabel === 'email' ? 'an' : 'a'}
            {phoneLabel} conversation. It is read-only here; reply by {phoneLabel}.
          {/if}
        </p>
      </Field>
    {:else}
      <Field label="Nextcloud Talk">
        {#if onTalk}
          <p class="caption talk-on">
            This room is also open in Nextcloud Talk — replies sync to your phone.
          </p>
        {/if}
        {#if canPromote && onPromote && !locked}
          <button class="talk-btn" type="button" disabled={promoting} onclick={handlePromote}>
            {#if promoting}
              {isPromoted ? 'Checking…' : 'Opening…'}
            {:else}
              {isPromoted ? 'Reconnect to Talk' : 'Also open in Talk'}
            {/if}
          </button>
          <p class="caption">
            {#if isPromoted}
              If the Talk conversation for this room was deleted, this creates a new one and points
              the room at it. Nothing changes while the existing conversation is still there.
            {:else}
              Creates a Nextcloud Talk conversation so this chat is reachable from the Talk apps.
            {/if}
          </p>
        {/if}
      </Field>
    {/if}

    {#if managedBy}
      <p class="caption hide-hint">
        Leaving this room means leaving {managedBy.display_name}.
        <a href={managedBy.manage_url}>Manage membership in Groups</a>
      </p>
    {:else if isImported}
      <p class="caption hide-hint">
        Hiding only removes this room from your web chat list. The Nextcloud Talk conversation and
        its messages aren't deleted, and it reappears here if you post in it again.
      </p>
    {/if}
  </div>

  {#snippet footer()}
    {#if managedBy}
      <!-- Nothing to delete or hide here: the room goes with its group. -->
    {:else if isImported}
      <!-- A hide is reversible (re-engagement un-hides), so it's a one-click
			     action with no type-the-name confirm — unlike a real delete. -->
      <span class="footer-start">
        <Button variant="danger" size="sm" onclick={onDelete}>Hide</Button>
      </span>
    {:else}
      <span class="footer-start">
        <Button variant="danger" size="sm" onclick={() => (showDeleteConfirm = true)}>Delete</Button
        >
      </span>
    {/if}
    <Button variant="secondary" onclick={onClose}>Cancel</Button>
    <Button variant="primary" onclick={handleSave} disabled={!canSave}>Save</Button>
  {/snippet}
</Modal>

<ConfirmDialog
  bind:open={showDeleteConfirm}
  title="Delete room"
  message={`Permanently deletes "${room.name}" and all its messages. This cannot be undone.`}
  challenge={room.name}
  confirmLabel="Delete this room"
  onConfirm={onDelete}
/>

<ConfirmDialog
  bind:open={pendingAdd.open}
  title={pendingAdd.target ? `Add ${pendingAdd.target.displayName}` : 'Add member'}
  message={pendingAdd.target ? addDisclosure(pendingAdd.target) : ''}
  confirmLabel="Add and share the history"
  confirmVariant="primary"
  onConfirm={confirmAdd}
/>

<style>
  .swatches {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    padding-block: var(--space-1);
  }

  .swatch {
    position: relative;
    display: inline-flex;
    cursor: pointer;
    border-radius: var(--radius-pill);
  }

  /* The input carries the accessible name and the keyboard behaviour, so it
	   stays in the tree and is only made invisible — `display: none` would take
	   it out of the radio group's arrow-key navigation. */
  .swatch input {
    position: absolute;
    opacity: 0;
    inset: 0;
    margin: 0;
    cursor: pointer;
  }

  .swatch .dot {
    width: 1.15rem;
    height: 1.15rem;
    border-radius: var(--radius-pill);
    background: var(--swatch);
    /* Same reason as the sidebar dot: forced-colours substitutes a background,
		   which here would flatten all eight swatches to one colour and leave a
		   picker whose options are indistinguishable. The colour is the content. */
    forced-color-adjust: none;
    /* The ring is drawn outside the dot so selecting one does not resize it. */
    box-shadow: 0 0 0 2px var(--surface-card);
    transition: box-shadow var(--transition-fast);
  }

  .swatch.none .dot {
    background: transparent;
    border: 1px dashed var(--border-hover);
  }

  .swatch.selected .dot,
  .swatch input:focus-visible + .dot {
    box-shadow:
      0 0 0 2px var(--surface-card),
      0 0 0 3px var(--accent);
  }

  /* Typography is the global .caption; only the <p> reset stays here. */
  p.caption {
    margin: 0.1rem 0 0;
  }

  .talk-on {
    color: var(--text-muted);
  }

  .locked-note {
    margin: 0 0 var(--space-3);
  }

  .hide-hint {
    margin: 0 0 var(--space-2);
  }

  .talk-btn {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition:
      background var(--transition-fast),
      color var(--transition-fast);
  }
  .talk-btn:hover:not(:disabled) {
    background: var(--hover-bg);
  }
  .talk-btn:disabled {
    opacity: var(--disabled-opacity);
    cursor: not-allowed;
  }

  /* The destructive action sits apart from Cancel and Save, at the far start. */
  .footer-start {
    margin-right: auto;
  }
</style>
