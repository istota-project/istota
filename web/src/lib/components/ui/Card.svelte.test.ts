import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Card from './Card.svelte';

afterEach(cleanup);

const text = (s: string) => createRawSnippet(() => ({ render: () => `<span>${s}</span>` }));

describe('Card', () => {
  it('renders its title, body, actions and footer', () => {
    const { container } = render(Card, {
      title: 'Identity',
      description: 'How Istota addresses you.',
      actions: text('Refresh'),
      footer: text('Save'),
      children: text('Body'),
    });
    expect(screen.getByRole('heading', { level: 2, name: 'Identity' })).toBeInTheDocument();
    expect(container.querySelector('.ui-card-head .title-section')?.textContent).toBe('Identity');
    expect(container.querySelector('.description')?.textContent).toBe('How Istota addresses you.');
    expect(container.querySelector('.ui-card-head')?.textContent).toContain('Refresh');
    expect(container.querySelector('.form-actions')?.textContent).toBe('Save');
    expect(screen.getByText('Body')).toBeInTheDocument();
  });

  it('draws the card look with no settings ancestor', () => {
    // The look used to exist only as `.settings .card`, so /admin, /groups and
    // setup wrapped themselves in `class="settings"` to borrow it.
    const { container } = render(Card, { title: 'Bare', children: text('x') });
    const root = container.firstElementChild as HTMLElement;
    expect(root.tagName).toBe('SECTION');
    expect(root.classList.contains('ui-card')).toBe(true);
    expect(root.classList.contains('ui-card-default')).toBe(true);
    expect(root.classList.contains('ui-card-pad-md')).toBe(true);
    expect(container.querySelector('.settings')).toBeNull();
  });

  it('passes placement classes and data/aria attributes through', () => {
    const { container } = render(Card, {
      title: 'Task activity',
      class: 'installation',
      'data-fill': '',
      'data-width': 'full',
      'aria-live': 'polite',
      'data-testid': 'conn',
      children: text('x'),
    });
    const root = container.firstElementChild as HTMLElement;
    expect(root.classList.contains('installation')).toBe(true);
    expect(root.hasAttribute('data-fill')).toBe(true);
    expect(root.getAttribute('data-width')).toBe('full');
    expect(root.getAttribute('aria-live')).toBe('polite');
    expect(root.getAttribute('data-testid')).toBe('conn');
  });

  it.each(['flat', 'inset', 'raised', 'outline'] as const)(
    'carries the %s variant as a class',
    (variant) => {
      const { container } = render(Card, { variant, children: text('x') });
      expect(container.querySelector(`.ui-card-${variant}`)).toBeTruthy();
      expect(container.querySelector('.ui-card-default')).toBeNull();
    },
  );

  it('omits the header when there is nothing to put in it', () => {
    const { container } = render(Card, { children: text('x') });
    expect(container.querySelector('.ui-card-head')).toBeNull();
  });

  it('takes a level-3 heading and a small padding', () => {
    const { container } = render(Card, {
      title: 'Coverage',
      level: 3,
      padding: 'sm',
      children: text('x'),
    });
    expect(screen.getByRole('heading', { level: 3, name: 'Coverage' })).toBeInTheDocument();
    expect(container.querySelector('.ui-card-pad-sm')).toBeTruthy();
  });

  it('puts the icon before the title and the status beside it', () => {
    const { container } = render(Card, {
      title: 'Nextcloud',
      icon: text('ICON'),
      status: text('Connected'),
      children: text('x'),
    });
    const head = container.querySelector('.ui-card-head') as HTMLElement;
    expect(head.textContent?.indexOf('ICON')).toBeLessThan(head.textContent!.indexOf('Nextcloud'));
    expect(head.textContent?.indexOf('Nextcloud')).toBeLessThan(
      head.textContent!.indexOf('Connected'),
    );
  });

  it('renders a link when given an href, with the hover treatment', () => {
    const { container } = render(Card, {
      href: '/istota/chat',
      title: 'Chat',
      children: text('x'),
    });
    const root = container.firstElementChild as HTMLAnchorElement;
    expect(root.tagName).toBe('A');
    expect(root.getAttribute('href')).toBe('/istota/chat');
    expect(root.classList.contains('ui-card-interactive')).toBe(true);
  });

  it('renders a button for an interactive card with onclick', async () => {
    const onclick = vi.fn();
    render(Card, { interactive: true, onclick, ariaLabel: 'Open weight', children: text('x') });
    const button = screen.getByRole('button', { name: 'Open weight' });
    expect(button).toHaveAttribute('type', 'button');
    await fireEvent.click(button);
    expect(onclick).toHaveBeenCalledOnce();
  });

  it('renders as another element on request', () => {
    const { container } = render(Card, { as: 'article', children: text('x') });
    expect(container.firstElementChild?.tagName).toBe('ARTICLE');
  });

  it('takes a heading snippet in place of a plain title', () => {
    render(Card, {
      heading: createRawSnippet(() => ({ render: () => '<span>Change <b>#4</b></span>' })),
      children: text('x'),
    });
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('Change #4');
  });
});
