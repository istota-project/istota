import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Field from './Field.svelte';

afterEach(cleanup);

const input = (name = 'value') =>
  createRawSnippet(() => ({ render: () => `<input name="${name}" />` }));

const checkbox = () => createRawSnippet(() => ({ render: () => `<input type="checkbox" />` }));

describe('Field', () => {
  it('labels its control', () => {
    render(Field, { label: 'Display name', children: input() });
    // The implicit <label> association is the whole reason this wraps rather
    // than sitting beside: no `for`/`id` pair to keep in sync.
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
      const order = [...field.children].map((el) => el.tagName.toLowerCase());
      expect(order.indexOf('input')).toBeLessThan(order.indexOf('span'));
    });
  });

  describe('labelled', () => {
    it('renders a div so several controls stay individually labellable', () => {
      // A <label> wrapping a row of checkboxes claims only the first one, and
      // silently leaves the rest with no accessible name. The same wrapper is
      // what stops a <button> in the slot becoming the label's control.
      const { container } = render(Field, {
        label: 'Disabled modules',
        labelled: false,
        children: input(),
      });
      expect(container.querySelector('div.field')).toBeTruthy();
      expect(container.querySelector('label.field')).toBeNull();
    });

    it('renders a label element by default', () => {
      const { container } = render(Field, { label: 'Display name', children: input() });
      expect(container.querySelector('label.field')).toBeTruthy();
    });
  });
});
