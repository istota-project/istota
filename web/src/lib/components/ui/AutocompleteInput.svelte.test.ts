import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import AutocompleteInput from './AutocompleteInput.svelte';
import FieldContextHarness from './FieldContext.harness.svelte';

afterEach(cleanup);

const options = ['shared/notes.md', 'istota/TODO.md'];

describe('AutocompleteInput', () => {
  it('offers matching suggestions on focus and commits the chosen one', async () => {
    render(AutocompleteInput, { value: '', options, ariaLabel: 'File path' });
    const input = screen.getByRole('combobox', { name: 'File path' });
    await fireEvent.focus(input);
    await fireEvent.mouseDown(screen.getByRole('option', { name: 'shared/notes.md' }));
    expect(input).toHaveValue('shared/notes.md');
  });

  it('reports a chosen suggestion through onSelect, and typing not at all', async () => {
    const onSelect = vi.fn();
    render(AutocompleteInput, { value: '', options, ariaLabel: 'File path', onSelect });
    const input = screen.getByRole('combobox', { name: 'File path' });
    await fireEvent.input(input, { target: { value: 'notes' } });
    expect(onSelect).not.toHaveBeenCalled();
    await fireEvent.mouseDown(screen.getByRole('option', { name: 'shared/notes.md' }));
    expect(onSelect).toHaveBeenCalledExactlyOnceWith('shared/notes.md');
  });

  it('renders the field in the same face as its suggestions', async () => {
    // The prop used to reach the menu only, so a path field showed monospace
    // suggestions under a sans input — the two-typefaces-in-one-control
    // inconsistency ISSUE-221 reports one component over. Asserted as the class
    // rather than a computed font, since the rule lives in a scoped stylesheet
    // jsdom does not apply.
    render(AutocompleteInput, { value: '', options, ariaLabel: 'File path', monospace: true });
    const input = screen.getByRole('combobox', { name: 'File path' });
    expect(input).toHaveClass('mono');
    await fireEvent.focus(input);
    expect(screen.getByRole('option', { name: 'shared/notes.md' })).toHaveClass('mono');
  });

  it('leaves both unstyled when the prop is absent', () => {
    render(AutocompleteInput, { value: '', options, ariaLabel: 'File path' });
    expect(screen.getByRole('combobox', { name: 'File path' })).not.toHaveClass('mono');
  });

  it('draws its field as the Input primitive, so it has a box outside a Field', () => {
    // Ported from a sibling app's copy: the field was a bare <input>, which no
    // Field rule styles, so it arrived with the browser's own box.
    render(AutocompleteInput, { value: '', options, ariaLabel: 'File path', invalid: true });
    const input = screen.getByRole('combobox', { name: 'File path' });
    expect(input).toHaveClass('ui-input');
    expect(input).toHaveClass('invalid');
    expect(input).toHaveAttribute('aria-invalid', 'true');
  });

  it('takes a Field caption, description and error through the Input it draws', () => {
    render(FieldContextHarness, {
      control: 'autocomplete',
      description: 'Pick a place',
      error: 'Not a place',
      required: true,
    });
    const input = screen.getByRole('combobox', { name: 'Caption' });
    expect(input).toHaveAccessibleDescription(/Pick a place/);
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAttribute('aria-required', 'true');
  });
});
