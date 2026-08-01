import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import AutocompleteInput from './AutocompleteInput.svelte';

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
});
