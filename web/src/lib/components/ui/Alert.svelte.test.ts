import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import Harness from './Alert.harness.svelte';

afterEach(cleanup);

describe('Alert', () => {
  it('is a bordered note by default, its body always shown', () => {
    const { container } = render(Harness, { title: 'Read-only' });
    const alert = container.querySelector('.ui-alert')!;
    expect(alert).toHaveClass('ui-alert-bordered', 'alert-warn');
    expect(alert).not.toHaveClass('banner');
    expect(alert).toHaveAttribute('role', 'note');
    expect(screen.getByText('Read-only')).toHaveClass('title-section');
    expect(screen.getByText('Alert body')).toBeInTheDocument();
    // Nothing to toggle when `collapsed` is not passed.
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('draws the success variant the merge brought to the bordered look', () => {
    const { container } = render(Harness, { title: 'Saved', variant: 'success' });
    expect(container.querySelector('.ui-alert')).toHaveClass('alert-success');
  });

  it('collapses behind its title when `collapsed` is passed, and reports the toggle back', async () => {
    render(Harness, { title: 'About', collapsed: true, bindCollapsed: true });
    const toggle = screen.getByRole('button', { name: 'About' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Alert body')).toBeNull();

    await fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Alert body')).toBeInTheDocument();
    expect(screen.getByTestId('collapsed')).toHaveTextContent('open');
  });

  it('starts open with `collapsed={false}` and still collapses', async () => {
    render(Harness, { title: 'Paused', collapsed: false });
    const toggle = screen.getByRole('button', { name: 'Paused' });
    expect(screen.getByText('Alert body')).toBeInTheDocument();
    await fireEvent.click(toggle);
    expect(screen.queryByText('Alert body')).toBeNull();
  });

  it('has nothing to collapse without a body', () => {
    const { container } = render(Harness, {
      title: 'Large notebook',
      collapsed: true,
      withBody: false,
    });
    expect(screen.queryByRole('button')).toBeNull();
    expect(container.querySelector('.ui-alert-title')).toHaveTextContent('Large notebook');
  });

  it('puts actions in the title row, outside the toggle', () => {
    const { container } = render(Harness, {
      title: 'Changed elsewhere',
      collapsed: false,
      withActions: true,
    });
    const action = screen.getByRole('button', { name: 'Fix it' });
    expect(action.closest('.ui-alert-head')).toBe(container.querySelector('.ui-alert-head'));
    expect(action.closest('.ui-alert-toggle')).toBeNull();
  });

  it('renders inline as the .banner box with no role of its own', () => {
    const { container } = render(Harness, { appearance: 'inline', variant: 'danger' });
    const alert = container.querySelector('.ui-alert')!;
    expect(alert).toHaveClass('banner', 'danger', 'ui-alert-inline');
    expect(alert).not.toHaveClass('ui-alert-bordered');
    expect(alert).not.toHaveAttribute('role');
    expect(alert.querySelector('.title-section')).toBeNull();
  });

  it('passes a role through, for an alert raised by an action', () => {
    render(Harness, { appearance: 'inline', variant: 'danger', role: 'alert' });
    expect(screen.getByRole('alert')).toHaveTextContent('Alert body');
  });

  it('puts inline actions after the body', () => {
    const { container } = render(Harness, { appearance: 'inline', withActions: true });
    const alert = container.querySelector('.ui-alert')!;
    expect(alert.lastElementChild).toHaveClass('ui-alert-actions');
    expect(alert.lastElementChild).toContainElement(screen.getByRole('button', { name: 'Fix it' }));
  });
});
