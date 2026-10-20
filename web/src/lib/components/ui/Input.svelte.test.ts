import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import Input from './Input.svelte';
import TextArea from './TextArea.svelte';
import InputHarness from './Input.harness.svelte';

afterEach(cleanup);

describe('Input', () => {
  it('defaults to a text input', () => {
    render(Input, { 'aria-label': 'Name' });
    expect(screen.getByLabelText('Name')).toHaveAttribute('type', 'text');
  });

  it('takes a type', () => {
    render(Input, { type: 'number', 'aria-label': 'Rate' });
    expect(screen.getByLabelText('Rate')).toHaveAttribute('type', 'number');
  });

  it('passes arbitrary attributes through', () => {
    // The rest spread is what makes this usable at all: min/max/step/pattern
    // and the autocomplete hints are the reason a form reaches for a raw input.
    render(Input, { type: 'number', min: 0, step: 5, placeholder: '0', 'aria-label': 'Qty' });
    const el = screen.getByLabelText('Qty');
    expect(el).toHaveAttribute('min', '0');
    expect(el).toHaveAttribute('step', '5');
    expect(el).toHaveAttribute('placeholder', '0');
  });

  it('reflects typing back through the binding', async () => {
    render(InputHarness);
    await fireEvent.input(screen.getByLabelText('Field'), { target: { value: 'ada' } });
    expect(screen.getByText('ada')).toBeInTheDocument();
  });

  describe('invalid', () => {
    it('sets aria-invalid so a screen reader hears the rejection', () => {
      render(Input, { invalid: true, 'aria-label': 'Rate' });
      expect(screen.getByLabelText('Rate')).toHaveAttribute('aria-invalid', 'true');
    });

    it('omits the attribute entirely when valid', () => {
      // `aria-invalid="false"` is meaningful to a screen reader and would
      // announce every untouched field as explicitly-not-invalid.
      render(Input, { 'aria-label': 'Rate' });
      expect(screen.getByLabelText('Rate')).not.toHaveAttribute('aria-invalid');
    });
  });

  it('marks a monospace control with a class rather than an inline style', () => {
    render(Input, { monospace: true, 'aria-label': 'Path' });
    expect(screen.getByLabelText('Path').className).toContain('mono');
  });

  it('renders a bare input when nothing adorns it', () => {
    // Pages place a plain Input as a flex child by its own element, so the
    // wrapper exists only when there is something to put beside the value.
    const { container } = render(Input, { 'aria-label': 'Name' });
    expect(container.querySelector('.ui-input-box')).toBeNull();
    expect(screen.getByLabelText('Name').parentElement).toBe(container);
  });

  it('marks the size, variant and density on the control', () => {
    render(Input, { size: 'toolbar', variant: 'ghost', dense: true, 'aria-label': 'Filter' });
    const el = screen.getByLabelText('Filter');
    expect(el).toHaveClass('ui-input', 'size-toolbar', 'ghost', 'dense');
  });

  it('binds the element through ref', () => {
    render(InputHarness);
    expect(screen.getByTestId('ref')).toHaveTextContent('INPUT');
  });

  describe('prefix and suffix', () => {
    it('renders string adornments on either side of the value', () => {
      const { container } = render(Input, {
        type: 'number',
        prefix: '$',
        suffix: 'of 12',
        'aria-label': 'Amount',
      });
      const box = container.querySelector('.ui-input-box')!;
      expect(box).not.toBeNull();
      const parts = [...box.children].map((el) => el.textContent?.trim() || el.tagName);
      expect(parts).toEqual(['$', 'INPUT', 'of 12']);
      // The box carries the look; the value inside it is unframed.
      expect(screen.getByLabelText('Amount')).toHaveClass('ui-input-inner');
    });

    it('carries invalid to the box, where the border is', () => {
      const { container } = render(Input, { prefix: '$', invalid: true, 'aria-label': 'Amount' });
      expect(container.querySelector('.ui-input-box')).toHaveClass('invalid');
      expect(screen.getByLabelText('Amount')).toHaveAttribute('aria-invalid', 'true');
    });

    it('hides a decorative adornment from the accessible name', () => {
      const { container } = render(Input, { prefix: '$', 'aria-label': 'Amount' });
      expect(container.querySelector('.ui-input-prefix')).toHaveAttribute('aria-hidden', 'true');
    });
  });

  describe('clearable', () => {
    it('shows Clear only while there is a value', async () => {
      render(InputHarness, { clearable: true });
      expect(screen.queryByRole('button', { name: 'Clear' })).toBeNull();
      await fireEvent.input(screen.getByLabelText('Field'), { target: { value: 'ada' } });
      expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument();
    });

    it('empties the value and puts focus back in the field', async () => {
      render(InputHarness, { clearable: true, initial: 'ada' });
      await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
      expect(screen.getByTestId('value')).toHaveTextContent('');
      expect(document.activeElement).toBe(screen.getByLabelText('Field'));
    });

    it('fires input so a filter bound to oninput sees the clear', async () => {
      const oninput = vi.fn();
      render(Input, { clearable: true, value: 'ada', oninput, 'aria-label': 'Filter' });
      await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
      expect(oninput).toHaveBeenCalled();
    });
  });

  describe('revealable', () => {
    it('toggles a password between hidden and shown', async () => {
      render(Input, { type: 'password', revealable: true, 'aria-label': 'Card number' });
      const el = screen.getByLabelText('Card number');
      expect(el).toHaveAttribute('type', 'password');
      const show = screen.getByRole('button', { name: 'Show Card number' });
      expect(show).toHaveAttribute('aria-pressed', 'false');
      await fireEvent.click(show);
      expect(el).toHaveAttribute('type', 'text');
      const hide = screen.getByRole('button', { name: 'Show Card number' });
      expect(hide).toHaveAttribute('aria-pressed', 'true');
    });

    it('does nothing for a type that is not a password', () => {
      render(Input, { revealable: true, 'aria-label': 'Name' });
      expect(screen.queryByRole('button')).toBeNull();
    });
  });
});

describe('TextArea', () => {
  it('defaults to three rows', () => {
    render(TextArea, { 'aria-label': 'Notes' });
    expect(screen.getByLabelText('Notes')).toHaveAttribute('rows', '3');
  });

  it('takes a row count', () => {
    render(TextArea, { rows: 8, 'aria-label': 'Notes' });
    expect(screen.getByLabelText('Notes')).toHaveAttribute('rows', '8');
  });

  it('reflects typing back through the binding', async () => {
    render(InputHarness, { multiline: true });
    await fireEvent.input(screen.getByLabelText('Field'), { target: { value: 'hello' } });
    expect(screen.getByText('hello')).toBeInTheDocument();
  });

  it('binds the element through ref', () => {
    render(InputHarness, { multiline: true });
    expect(screen.getByTestId('ref')).toHaveTextContent('TEXTAREA');
  });

  it('marks a dense control', () => {
    render(TextArea, { dense: true, 'aria-label': 'Notes' });
    expect(screen.getByLabelText('Notes')).toHaveClass('dense');
  });

  it('sets aria-invalid when invalid', () => {
    render(TextArea, { invalid: true, 'aria-label': 'Notes' });
    expect(screen.getByLabelText('Notes')).toHaveAttribute('aria-invalid', 'true');
  });
});
