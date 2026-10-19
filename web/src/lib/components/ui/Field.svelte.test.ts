import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Field from './Field.svelte';
import Input from './Input.svelte';
import FieldContextHarness from './FieldContext.harness.svelte';

afterEach(cleanup);

const input = (name = 'value') =>
  createRawSnippet(() => ({ render: () => `<input name="${name}" />` }));

const checkbox = () => createRawSnippet(() => ({ render: () => `<input type="checkbox" />` }));

describe('Field', () => {
  it('labels its control', () => {
    render(Field, { label: 'Display name', children: input() });
    expect(screen.getByLabelText('Display name')).toBeInTheDocument();
  });

  describe('supplementary text', () => {
    // AGENTS.md's three-slot rule: a hint is optional reading and hides behind
    // a "?", a warning and an error are inline because a hover popover is
    // discoverable, not seen.
    it('renders a warning inline', () => {
      render(Field, { label: 'Rate', warning: 'This client bills as hours', children: input() });
      expect(screen.getByText('This client bills as hours')).toBeInTheDocument();
    });

    it('renders an error inline', () => {
      render(Field, { label: 'Rate', error: 'Must be a number', children: input() });
      expect(screen.getByText('Must be a number')).toBeInTheDocument();
    });

    it('puts a hint behind a trigger rather than inline', async () => {
      render(Field, { label: 'Rate', hint: 'Per hour, before tax', children: input() });
      expect(screen.queryByText('Per hour, before tax')).not.toBeInTheDocument();
      const trigger = screen.getByLabelText('About Rate');
      await fireEvent.click(trigger);
      expect(await screen.findByText('Per hour, before tax')).toBeInTheDocument();
    });

    it('renders a badge beside the label, visible rather than behind a trigger', () => {
      render(Field, { label: 'Email', badge: 'Set by your administrator', children: input() });
      expect(screen.getByText('Set by your administrator')).toBeInTheDocument();
    });

    it('shows no hint trigger when there is no hint', () => {
      render(Field, { label: 'Rate', children: input() });
      expect(screen.queryByLabelText('About Rate')).not.toBeInTheDocument();
    });
  });

  describe('checkbox', () => {
    it('puts the control before the label', () => {
      const { container } = render(Field, {
        label: 'Enabled',
        checkbox: true,
        children: checkbox(),
      });
      const field = container.querySelector('.field')!;
      const children = [...field.children];
      const box = children.findIndex((el) => el.tagName === 'INPUT');
      const caption = children.findIndex((el) => el.classList.contains('field-label'));
      expect(box).toBeGreaterThanOrEqual(0);
      expect(box).toBeLessThan(caption);
    });
  });

  it('renders a div, never a label around its control', () => {
    // A <label> makes its first labelable descendant its control, so a button
    // in the slot became a second way to press it from the caption.
    const { container } = render(Field, { label: 'Display name', children: input() });
    expect(container.querySelector('div.field')).toBeTruthy();
    expect(container.querySelector('label.field')).toBeNull();
  });
});

describe('Field context', () => {
  it('names a text input through the caption, which is its <label for>', () => {
    // `for` is what makes a caption click focus the input in a browser.
    render(FieldContextHarness, { control: 'input' });
    const control = screen.getByLabelText('Caption');
    const caption = screen.getByText('Caption');
    expect(control.tagName).toBe('INPUT');
    expect(caption.tagName).toBe('LABEL');
    expect((caption as HTMLLabelElement).htmlFor).toBe(control.id);
  });

  it('names a textarea the same way', () => {
    render(FieldContextHarness, { control: 'textarea' });
    expect(screen.getByLabelText('Caption').tagName).toBe('TEXTAREA');
  });

  it('names a Select trigger by aria-labelledby, and a caption click does not open it', async () => {
    render(FieldContextHarness, { control: 'select' });
    const trigger = screen.getByRole('button', { name: 'Caption' });
    const caption = screen.getByText('Caption');
    expect(caption.tagName).toBe('SPAN');
    expect(trigger).toHaveAttribute('aria-labelledby', caption.id);
    await fireEvent.click(caption);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('option', { name: 'Beta' })).toBeNull();
  });

  it('names a MultiSelect group by its caption', () => {
    render(FieldContextHarness, { control: 'multiselect' });
    expect(screen.getByRole('group', { name: 'Caption' })).toBeInTheDocument();
  });

  it('does not press a button in the slot from the caption', async () => {
    // The sender-chip defect: a "Sender filters" caption removed a sender.
    render(FieldContextHarness, { control: 'buttons' });
    await fireEvent.click(screen.getByText('Caption'));
    expect(screen.getByTestId('removed')).toHaveTextContent('0');
    // With no single control, the field is a group its caption names.
    expect(screen.getByRole('group', { name: 'Caption' })).toBeInTheDocument();
  });

  it('labels a raw input written into the slot', () => {
    render(FieldContextHarness, { control: 'raw' });
    expect(screen.getByLabelText('Caption').tagName).toBe('INPUT');
  });

  it('points aria-describedby at the description and the error', () => {
    render(FieldContextHarness, {
      control: 'input',
      description: 'Shown on your profile',
      error: 'Too long',
    });
    const control = screen.getByLabelText('Caption');
    expect(control).toHaveAccessibleDescription('Shown on your profile Too long');
    expect(control).toHaveAttribute('aria-invalid', 'true');
  });

  it('describes a raw input too', () => {
    render(FieldContextHarness, { control: 'raw', description: 'Shown on your profile' });
    expect(screen.getByLabelText('Caption')).toHaveAccessibleDescription('Shown on your profile');
  });

  it('sets aria-required without drawing a marker', () => {
    render(FieldContextHarness, { control: 'input', required: true });
    expect(screen.getByLabelText('Caption')).toHaveAttribute('aria-required', 'true');
    expect(screen.queryByText('*')).toBeNull();
  });

  it('appends "(optional)" to the label', () => {
    render(FieldContextHarness, { control: 'input', optional: true });
    expect(screen.getByLabelText('Caption (optional)').tagName).toBe('INPUT');
  });

  it('draws the compact caption as a micro-label', () => {
    render(FieldContextHarness, { control: 'input', layout: 'compact' });
    expect(screen.getByText('Caption')).toHaveClass('micro-label');
  });

  it('labels and toggles a Checkbox from its caption', async () => {
    render(FieldContextHarness, { control: 'checkbox' });
    const box = screen.getByRole('checkbox', { name: 'Caption' });
    await fireEvent.click(screen.getByText('Caption'));
    expect(box).toBeChecked();
    expect(screen.getByTestId('checked')).toHaveTextContent('true');
  });

  it('leaves a control outside any field alone', () => {
    render(Input, { 'aria-label': 'Bare' });
    const el = screen.getByLabelText('Bare');
    expect(el).not.toHaveAttribute('id');
    expect(el).not.toHaveAttribute('aria-describedby');
  });
});
