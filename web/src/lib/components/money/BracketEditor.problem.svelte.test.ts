import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import Harness from './BracketEditor.problem.harness.svelte';

// The taxes settings page reports an unsaveable bracket table up to the page,
// which holds it in one `$state` record and writes a fresh object per report.
// Reporting from a *tracked* effect made the child a dependent of that record,
// so the first report re-invalidated the effect that produced it and the page
// died on mount with effect_update_depth_exceeded.

afterEach(cleanup);

/** Fails the test on the Svelte error the loop raises, wherever it surfaces. */
function captureEffectLoop() {
  const errors: unknown[] = [];
  const onError = (e: ErrorEvent) => errors.push(e.error ?? e.message);
  const onRejection = (e: PromiseRejectionEvent) => errors.push(e.reason);
  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);
  const spy = vi.spyOn(console, 'error').mockImplementation((...args) => errors.push(args[0]));
  return {
    stop() {
      window.removeEventListener('error', onError);
      window.removeEventListener('unhandledrejection', onRejection);
      spy.mockRestore();
      return errors.map((e) => (e instanceof Error ? e.message : String(e))).join('\n');
    },
  };
}

describe('BracketEditor problem reporting', () => {
  it('settles on mount when the parent writes a fresh object per report', async () => {
    const captured = captureEffectLoop();
    let thrown = '';
    try {
      render(Harness, { value: [[0, 0.1]] });
      await tick();
      await tick();
    } catch (e) {
      thrown = e instanceof Error ? e.message : String(e);
    }
    const logged = captured.stop();

    expect(`${thrown}\n${logged}`).not.toMatch(/effect_update_depth_exceeded/);
    expect(screen.getByTestId('problems')).toHaveTextContent('{"federal":"","state":""}');
  });

  it('reports a problem, and clears it once the table is valid again', async () => {
    render(Harness, { value: [[0, 0.1]] });
    await tick();

    // Blank the rate, leaving the threshold typed — a half-typed bracket.
    const rate = screen.getAllByLabelText('Bracket 1 rate, percent')[0] as HTMLInputElement;
    await fireEvent.input(rate, { target: { value: '' } });
    await tick();

    expect(screen.getByTestId('problems')).toHaveTextContent(
      'Every bracket needs both a threshold and a rate.',
    );

    await fireEvent.input(rate, { target: { value: '5' } });
    await tick();

    expect(screen.getByTestId('problems')).toHaveTextContent('{"federal":"","state":""}');
  });

  // `bind:value` on <input type="number"> coerces to a number and writes null
  // when emptied, which is not the string the row buffer is declared to hold —
  // so clearing a field threw inside the very handler that would have recorded
  // the clear, and the field could not be emptied at all.
  it('lets a field be cleared without throwing', async () => {
    const captured = captureEffectLoop();
    render(Harness, { value: [[0, 0.1]] });
    await tick();

    const threshold = screen.getAllByLabelText('Bracket 1 income threshold')[0] as HTMLInputElement;
    await fireEvent.input(threshold, { target: { value: '' } });
    await tick();

    expect(captured.stop()).not.toMatch(/TypeError|trim/);
    expect(threshold.value).toBe('');
  });

  // The consumer stashes the patch until the app-bar Save rather than echoing
  // it back as `value`, so a resync that re-runs on its own bookkeeping rebuilt
  // the rows from the pre-edit prop and reverted what had just been typed.
  it('keeps an edit the consumer has not echoed back', async () => {
    render(Harness, { value: [[0, 0.1]] });
    await tick();

    const rate = screen.getAllByLabelText('Bracket 1 rate, percent')[0] as HTMLInputElement;
    await fireEvent.input(rate, { target: { value: '5' } });
    await tick();

    expect(rate.value).toBe('5');
  });
});
