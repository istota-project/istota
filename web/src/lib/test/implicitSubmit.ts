import { fireEvent } from '@testing-library/svelte';

/**
 * Enter in a single-line field, as a browser handles it. jsdom does not
 * implement implicit submission, so this does the browser's step: the form's
 * default button (its first submit button in tree order) is clicked, unless it
 * is disabled. A field outside any form, or a form with no submit button,
 * submits nothing, which is what a browser does for a dialog with more than
 * one field.
 *
 * Enter on a textarea, a button, a Select trigger, or an input outside the
 * spec's list below never submits, so for those only the keydown is fired.
 */
const SUBMITS_ON_ENTER = new Set([
  'text',
  'search',
  'url',
  'tel',
  'email',
  'password',
  'date',
  'month',
  'week',
  'time',
  'datetime-local',
  'number',
]);

export async function pressEnter(field: HTMLElement): Promise<void> {
  await fireEvent.keyDown(field, { key: 'Enter' });
  if (!(field instanceof HTMLInputElement) || !field.form) return;
  // The HTML spec's list: a range, checkbox, radio, file or colour input does not.
  if (!SUBMITS_ON_ENTER.has(field.type)) return;
  const button = [...field.form.elements].find(
    (el): el is HTMLButtonElement => el instanceof HTMLButtonElement && el.type === 'submit',
  );
  if (button && !button.disabled) await fireEvent.click(button);
}
