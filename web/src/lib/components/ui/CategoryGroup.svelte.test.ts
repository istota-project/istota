import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import CategoryGroup from './CategoryGroup.svelte';

afterEach(cleanup);
beforeEach(() => localStorage.clear());

const rows = createRawSnippet(() => ({ render: () => '<p>Row one</p>' }));

describe('CategoryGroup', () => {
  it('says whether a collapsible group is open, and toggles', async () => {
    render(CategoryGroup, { label: 'Pinned', collapsible: true, children: rows });
    const toggle = screen.getByRole('button', { name: /Pinned/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Row one')).toBeInTheDocument();
    await fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Row one')).toBeNull();
  });

  it('puts aria-expanded on the caret when the label filters', async () => {
    const onSelect = vi.fn();
    render(CategoryGroup, { label: 'Work', collapsible: true, onSelect, children: rows });
    const caret = screen.getByRole('button', { name: 'Show Work' });
    expect(caret).toHaveAttribute('aria-expanded', 'true');
    await fireEvent.click(caret);
    expect(caret).toHaveAttribute('aria-expanded', 'false');
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("draws Disclosure's chevron marker rather than a text glyph", () => {
    const { container } = render(CategoryGroup, {
      label: 'Pinned',
      collapsible: true,
      children: rows,
    });
    const caret = container.querySelector('.caret')!;
    expect(caret.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    expect(caret.textContent).not.toContain('▶');
  });

  it('carries no expanded state when it cannot collapse', () => {
    render(CategoryGroup, { label: 'Static', children: rows });
    expect(screen.queryByRole('button')).toBeNull();
  });
});
