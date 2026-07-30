import { describe, it, expect, afterEach } from 'vitest';
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

  it('sets aria-invalid when invalid', () => {
    render(TextArea, { invalid: true, 'aria-label': 'Notes' });
    expect(screen.getByLabelText('Notes')).toHaveAttribute('aria-invalid', 'true');
  });
});
