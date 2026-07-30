import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Button from './Button.svelte';

afterEach(cleanup);

/** A static text child, which is all any of these cases needs. */
const label = (text: string) => createRawSnippet(() => ({ render: () => `<span>${text}</span>` }));

describe('Button', () => {
  it('renders its children and fires onclick', async () => {
    const onclick = vi.fn();
    render(Button, { children: label('Save'), onclick });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onclick).toHaveBeenCalledOnce();
  });

  describe('loading', () => {
    it('swaps the label and disables the button', () => {
      render(Button, { children: label('Save'), loading: true });
      const btn = screen.getByRole('button');
      expect(btn).toHaveTextContent('Saving…');
      expect(btn).toBeDisabled();
      expect(btn).toHaveAttribute('aria-busy', 'true');
    });

    it('takes a custom loading label', () => {
      render(Button, { children: label('Import'), loading: true, loadingLabel: 'Importing…' });
      expect(screen.getByRole('button')).toHaveTextContent('Importing…');
    });

    it('gates a second submit on the disabled attribute, not the label', () => {
      // The point of the prop: several of the hand-written ternaries swapped
      // the label without disabling, so a second click re-submitted the form.
      // Asserted as an attribute rather than a suppressed click, because
      // jsdom's fireEvent dispatches onto a disabled element regardless — that
      // suppression is the browser's, so a click test here would only be
      // testing jsdom.
      render(Button, { children: label('Save'), loading: true, onclick: vi.fn() });
      expect(screen.getByRole('button')).toBeDisabled();
    });

    it('is absent from the DOM when not loading', () => {
      render(Button, { children: label('Save'), loading: false });
      const btn = screen.getByRole('button');
      expect(btn).toHaveTextContent('Save');
      expect(btn).not.toBeDisabled();
      expect(btn).not.toHaveAttribute('aria-busy');
    });
  });

  it('stays disabled when disabled and not loading', () => {
    render(Button, { children: label('Save'), disabled: true });
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('carries an accessible name from ariaLabel for icon-only content', () => {
    render(Button, { children: label('×'), ariaLabel: 'Remove row' });
    expect(screen.getByRole('button', { name: 'Remove row' })).toBeInTheDocument();
  });
});
