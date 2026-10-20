import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { X } from '@lucide/svelte';
import IconButton from './IconButton.svelte';
import Button from './Button.svelte';
import { createRawSnippet } from 'svelte';
import { linkOrButton } from './internal/linkOrButton';

afterEach(cleanup);

const text = (t: string) => createRawSnippet(() => ({ render: () => `<span>${t}</span>` }));

describe('linkOrButton', () => {
  it('is a link with its attributes when it has an href and can be used', () => {
    const el = linkOrButton({ href: '/x', download: 'x.csv', reload: true });
    expect(el.tag).toBe('a');
    expect(el.attrs).toMatchObject({ href: '/x', download: 'x.csv', 'data-sveltekit-reload': '' });
  });

  it.each([{ disabled: true }, { loading: true }])(
    'falls back to a disabled button for %o, never a submit',
    (state) => {
      const el = linkOrButton({ href: '/x', type: 'submit', ...state });
      expect(el.tag).toBe('button');
      expect(el.attrs).toMatchObject({ type: 'button', disabled: true });
    },
  );
});

describe('IconButton', () => {
  it('falls back from a link to a disabled button while loading, as Button does', () => {
    render(IconButton, { label: 'Download', href: '/file', icon: X, loading: true });
    const button = screen.getByRole('button', { name: 'Download' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button.querySelector('.spinner')).toBeTruthy();
    expect(button.querySelector('.ui-icon')).toBeNull();
    cleanup();
    render(Button, { href: '/file', loading: true, children: text('Download') });
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('renders a usable href as a link', () => {
    render(IconButton, { label: 'Open', href: '/file', icon: X });
    expect(screen.getByRole('link', { name: 'Open' })).toHaveAttribute('href', '/file');
  });

  it('adds the touch target and the muted tone as classes', () => {
    const { container } = render(IconButton, {
      label: 'Mic',
      icon: X,
      size: 'round',
      tone: 'muted',
      touchTarget: true,
    });
    const el = container.querySelector('.icon-btn')!;
    expect(el.classList.contains('touch-target')).toBe(true);
    expect(el.classList.contains('muted')).toBe(true);
  });

  it('passes other attributes and handlers to the element and binds ref', async () => {
    const onmousedown = vi.fn();
    let ref: HTMLElement | undefined;
    render(IconButton, {
      label: 'Attach',
      icon: X,
      onmousedown,
      'aria-expanded': 'false',
      get ref() {
        return ref;
      },
      set ref(value) {
        ref = value;
      },
    });
    const button = screen.getByRole('button', { name: 'Attach' });
    expect(button).toHaveAttribute('aria-expanded', 'false');
    await fireEvent.mouseDown(button);
    expect(onmousedown).toHaveBeenCalledOnce();
    expect(ref).toBe(button);
  });
});

describe('Button', () => {
  it('passes a placement class through', () => {
    render(Button, { class: 'push-end', children: text('Add') });
    expect(screen.getByRole('button', { name: 'Add' })).toHaveClass('btn', 'push-end');
  });

  it('keeps an href button a link with its download attribute', () => {
    render(Button, { href: '/export', download: 'x.csv', children: text('Export') });
    expect(screen.getByRole('link', { name: 'Export' })).toHaveAttribute('download', 'x.csv');
  });
});
