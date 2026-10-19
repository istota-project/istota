import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import FormStack from './FormStack.svelte';
import FormGrid from './FormGrid.svelte';

afterEach(cleanup);

const fields = createRawSnippet(() => ({
  render: () => '<div><input aria-label="Name" /><button type="submit">Save</button></div>',
}));

describe('FormStack', () => {
  it('is a fieldset column at the --space-3 gap by default', () => {
    const { container } = render(FormStack, { children: fields });
    const root = container.firstElementChild as HTMLElement;
    expect(root.tagName).toBe('FIELDSET');
    expect(root.classList.contains('ui-form-stack')).toBe(true);
    expect(root.style.gap).toBe('var(--space-3)');
  });

  it('takes another step of the spacing ramp', () => {
    const { container } = render(FormStack, { gap: 2, children: fields });
    expect((container.firstElementChild as HTMLElement).style.gap).toBe('var(--space-2)');
  });

  it('disables every control in it while busy', () => {
    render(FormStack, { disabled: true, children: fields });
    expect(screen.getByRole('textbox', { name: 'Name' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('names the group with its legend', () => {
    render(FormStack, { legend: 'Initial owners', children: fields });
    expect(screen.getByRole('group', { name: 'Initial owners' })).toBeInTheDocument();
    expect(document.querySelector('legend')?.classList.contains('ui-form-stack-legend')).toBe(true);
  });

  it('as a form, hands the submit to its handler without navigating', async () => {
    const onsubmit = vi.fn((e: SubmitEvent) => e.defaultPrevented);
    const { container } = render(FormStack, { as: 'form', onsubmit, children: fields });
    const form = container.firstElementChild as HTMLFormElement;
    expect(form.tagName).toBe('FORM');
    await fireEvent.submit(form);
    expect(onsubmit).toHaveBeenCalledOnce();
    expect(onsubmit.mock.results[0].value).toBe(true);
  });

  it('as a busy form, disables its controls through an inner fieldset', () => {
    const { container } = render(FormStack, {
      as: 'form',
      disabled: true,
      onsubmit: () => {},
      children: fields,
    });
    expect(container.firstElementChild?.tagName).toBe('FORM');
    expect(screen.getByRole('textbox', { name: 'Name' })).toBeDisabled();
  });

  it('passes data and aria attributes through', () => {
    const { container } = render(FormStack, {
      'aria-label': 'Card details',
      'data-testid': 'card-form',
      children: fields,
    });
    const root = container.firstElementChild as HTMLElement;
    expect(root.getAttribute('aria-label')).toBe('Card details');
    expect(root.getAttribute('data-testid')).toBe('card-form');
  });
});

describe('FormGrid', () => {
  it('fits columns no narrower than 10rem, never wider than its container', () => {
    const { container } = render(FormGrid, { children: fields });
    const root = container.firstElementChild as HTMLElement;
    expect(root.classList.contains('ui-form-grid')).toBe(true);
    expect(root.style.getPropertyValue('--form-grid-min')).toBe('10rem');
    expect(root.style.gap).toBe('var(--space-3)');
  });

  it('takes its own minimum and gap', () => {
    const { container } = render(FormGrid, { min: '12rem', gap: 2, children: fields });
    const root = container.firstElementChild as HTMLElement;
    expect(root.style.getPropertyValue('--form-grid-min')).toBe('12rem');
    expect(root.style.gap).toBe('var(--space-2)');
  });
});
