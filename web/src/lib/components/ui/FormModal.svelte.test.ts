import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import Harness from './FormModal.harness.svelte';

afterEach(cleanup);

/**
 * The button a browser clicks on Enter in a single-line field: the form's
 * first submit button in tree order. jsdom does not implement implicit
 * submission, so the tests check the two facts it rests on (the field and the
 * button share one form, and the button is that form's default) and then press
 * the button the way the browser would. The real-browser pass is in the
 * stage log.
 */
function defaultButton(form: HTMLFormElement): HTMLButtonElement | undefined {
  return [...form.elements].find(
    (el): el is HTMLButtonElement => el instanceof HTMLButtonElement && el.type === 'submit',
  );
}

async function pressEnterIn(field: HTMLInputElement) {
  const form = field.form;
  expect(form).not.toBeNull();
  const button = defaultButton(form!);
  expect(button).toBeDefined();
  await fireEvent.keyDown(field, { key: 'Enter' });
  if (!button!.disabled) await fireEvent.click(button!);
}

async function pickOption(trigger: HTMLElement, name: string) {
  await fireEvent.keyDown(trigger, { key: 'Enter' });
  const option = await screen.findByRole('option', { name });
  await fireEvent.pointerDown(option, { pointerType: 'mouse' });
  await fireEvent.pointerUp(option, { pointerType: 'mouse' });
  await fireEvent.click(option);
}

describe('Modal onsubmit', () => {
  it('wraps the body and the footer in one form, so the footer button submits it', async () => {
    const onsubmit = vi.fn();
    render(Harness, { mode: 'modal', onsubmit });
    const dialog = screen.getByRole('dialog', { name: 'Plain form' });
    const name = screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement;
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    const form = dialog.querySelector('form')!;
    expect(form).not.toBeNull();
    expect(form.noValidate).toBe(true);
    expect(name.form).toBe(form);
    expect(save.form).toBe(form);
    await fireEvent.click(save);
    expect(onsubmit).toHaveBeenCalledOnce();
    expect(onsubmit.mock.calls[0][0].defaultPrevented).toBe(true);
  });

  it('makes the footer submit the default button, ahead of every other button', () => {
    render(Harness, { mode: 'modal', onsubmit: vi.fn() });
    const form = screen.getByRole('dialog').querySelector('form')!;
    expect(defaultButton(form)).toBe(screen.getByRole('button', { name: 'Save' }));
    // Cancel, the Select trigger and the like must not be submit buttons, or
    // the first of them would be what Enter presses.
    const submits = [...form.querySelectorAll('button')].filter((b) => b.type === 'submit');
    expect(submits).toHaveLength(1);
  });

  it('renders no form without onsubmit', () => {
    render(Harness, { mode: 'modal', onsubmit: undefined });
    expect(screen.getByRole('dialog').querySelector('form')).toBeNull();
  });

  it('ignores Escape while busy and marks the dialog busy', async () => {
    render(Harness, { mode: 'modal', onsubmit: vi.fn(), busy: true });
    const dialog = screen.getByRole('dialog', { name: 'Plain form' });
    expect(dialog).toHaveAttribute('aria-busy', 'true');
    await fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.getByTestId('open')).toHaveTextContent('true');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});

describe('FormModal', () => {
  it('submits on Enter in a text input', async () => {
    const onSubmit = vi.fn();
    render(Harness, { onSubmit });
    await pressEnterIn(screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement);
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it('does not submit on Enter from the Select trigger, the textarea or the checkbox', async () => {
    const onSubmit = vi.fn();
    render(Harness, { onSubmit });
    const trigger = screen.getByRole('button', { name: 'Kind' });
    expect(trigger).toHaveAttribute('type', 'button');
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    await fireEvent.keyDown(screen.getByRole('textbox', { name: 'Notes' }), { key: 'Enter' });
    await fireEvent.keyDown(screen.getByRole('checkbox', { name: 'Urgent' }), { key: 'Enter' });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('does not submit when an option is chosen from the portalled Select list', async () => {
    const onSubmit = vi.fn();
    render(Harness, { onSubmit });
    await pickOption(screen.getByRole('button', { name: 'Kind' }), 'Beta');
    await waitFor(() => expect(screen.getByTestId('kind')).toHaveTextContent('b'));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByTestId('open')).toHaveTextContent('true');
  });

  it('closes once onSubmit resolves', async () => {
    const onSubmit = vi.fn(async () => {});
    render(Harness, { onSubmit });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByTestId('open')).toHaveTextContent('false'));
  });

  it('keeps the dialog and the draft open with the message when onSubmit rejects', async () => {
    const onSubmit = vi.fn(async () => {
      throw new Error('That name is taken.');
    });
    render(Harness, { onSubmit });
    const name = screen.getByRole('textbox', { name: 'Name' });
    await fireEvent.input(name, { target: { value: 'Draft' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('That name is taken.');
    expect(alert).toHaveClass('form-error');
    expect(screen.getByTestId('open')).toHaveTextContent('true');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Draft');
  });

  it('clears the last refusal when the dialog is reopened', async () => {
    const onSubmit = vi.fn(async () => {
      throw new Error('That name is taken.');
    });
    render(Harness, { onSubmit });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByRole('alert');
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.getByTestId('open')).toHaveTextContent('false'));
    await fireEvent.click(screen.getByRole('button', { name: 'Reopen' }));
    await waitFor(() => expect(screen.getByTestId('open')).toHaveTextContent('true'));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('stays open without a message when onSubmit resolves false', async () => {
    render(Harness, { onSubmit: async () => false });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await Promise.resolve();
    expect(screen.getByTestId('open')).toHaveTextContent('true');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows progress on the submit button and ignores Escape while the submit is pending', async () => {
    let finish!: () => void;
    const onSubmit = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    const onCancel = vi.fn();
    render(Harness, { onSubmit, onCancel });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    const busyButton = await screen.findByRole('button', { name: 'Saving…' });
    expect(busyButton).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-busy', 'true');
    await fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByTestId('open')).toHaveTextContent('true');
    // A second press while pending is not a second submit.
    await fireEvent.submit(dialog.querySelector('form')!);
    expect(onSubmit).toHaveBeenCalledOnce();
    finish();
    await waitFor(() => expect(screen.getByTestId('open')).toHaveTextContent('false'));
  });

  it('ignores Escape while the caller says it is busy', async () => {
    const onCancel = vi.fn();
    render(Harness, { busy: true, onCancel });
    await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByTestId('open')).toHaveTextContent('true');
  });

  it('cancels from the Cancel button', async () => {
    const onCancel = vi.fn();
    render(Harness, { onCancel });
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(screen.getByTestId('open')).toHaveTextContent('false');
  });

  it('cancels on Escape when nothing is in flight', async () => {
    const onCancel = vi.fn();
    render(Harness, { onCancel });
    await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    await waitFor(() => expect(onCancel).toHaveBeenCalledOnce());
    expect(screen.getByTestId('open')).toHaveTextContent('false');
  });

  it('does not submit while canSubmit is false', async () => {
    const onSubmit = vi.fn();
    render(Harness, { onSubmit, canSubmit: false });
    const save = screen.getByRole('button', { name: 'Save' });
    expect(save).toBeDisabled();
    await pressEnterIn(screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement);
    await fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("renders the caller's error as the form error", () => {
    render(Harness, { error: 'Could not load the owners.' });
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the owners.');
  });

  it('puts Cancel before the primary action', () => {
    render(Harness);
    const footer = screen.getByRole('dialog').querySelector('.ui-modal-footer')!;
    const labels = [...footer.querySelectorAll('button')].map((b) => b.textContent?.trim());
    expect(labels).toEqual(['Cancel', 'Save']);
  });
});
