import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import Checkbox from './Checkbox.svelte';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Checkbox', () => {
  it('is a native checkbox named by its label', () => {
    render(Checkbox, { label: 'Folders first' });
    const box = screen.getByRole('checkbox', { name: 'Folders first' });
    expect(box.tagName).toBe('INPUT');
    expect(box).not.toBeChecked();
  });

  it('reports a change through onchange', async () => {
    const onchange = vi.fn();
    render(Checkbox, { label: 'Show hidden', onchange });
    await fireEvent.click(screen.getByText('Show hidden'));
    expect(onchange).toHaveBeenCalledWith(true);
    expect(screen.getByRole('checkbox')).toBeChecked();
  });

  it('reflects indeterminate on the element, where it is a property only', () => {
    render(Checkbox, { ariaLabel: 'Select all', indeterminate: true });
    const box = screen.getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement;
    expect(box.indeterminate).toBe(true);
  });

  it('draws a description under the label and describes the box with it', () => {
    render(Checkbox, { label: 'Listed', description: 'Shown in the room directory' });
    expect(screen.getByRole('checkbox', { name: 'Listed' })).toHaveAccessibleDescription(
      'Shown in the room directory',
    );
  });

  it('takes ariaLabel in a table cell, with no visible label', () => {
    const { container } = render(Checkbox, { ariaLabel: 'Email' });
    expect(screen.getByRole('checkbox', { name: 'Email' })).toBeInTheDocument();
    expect(container.querySelector('label')).toBeNull();
  });

  it('warns in development when it has no name at all', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    render(Checkbox, {});
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('ariaLabel'));
  });

  it('does not warn when named', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    render(Checkbox, { ariaLabel: 'Email' });
    expect(warn).not.toHaveBeenCalled();
  });

  it('marks the muted tone with a class', () => {
    render(Checkbox, { label: 'Show hidden', tone: 'muted' });
    expect(screen.getByText('Show hidden').closest('.ui-checkbox')).toHaveClass('muted');
  });

  it('carries the shared touch target, so a bare box in a table cell is easy to hit', () => {
    render(Checkbox, { ariaLabel: 'Select row' });
    expect(screen.getByRole('checkbox')).toHaveClass('ui-checkbox-input', 'touch-target');
  });

  it('is disabled when asked', () => {
    render(Checkbox, { label: 'Enabled', disabled: true });
    expect(screen.getByRole('checkbox')).toBeDisabled();
  });
});
