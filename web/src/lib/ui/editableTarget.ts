const EDITABLE = 'input, textarea, select, [contenteditable]:not([contenteditable="false"])';

/**
 * Whether a key event's target is somewhere the user types, so a single-key
 * shortcut must leave it alone. Takes the event or its target.
 */
export function isEditableTarget(eventOrTarget: Event | EventTarget | null): boolean {
  const target = eventOrTarget instanceof Event ? eventOrTarget.target : eventOrTarget;
  return target instanceof Element && target.closest(EDITABLE) !== null;
}
