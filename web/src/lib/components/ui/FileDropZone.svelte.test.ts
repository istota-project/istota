import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import FileDropZone from './FileDropZone.svelte';

afterEach(cleanup);

const prompt = createRawSnippet(() => ({ render: () => '<span>Drop a CSV here.</span>' }));

const fileInput = (): HTMLInputElement =>
  document.querySelector('input[type="file"]') as HTMLInputElement;

describe('FileDropZone', () => {
  it('offers picking as a button, with the native control kept out of the layout', () => {
    // The bare `<input type="file">` renders the platform's own control —
    // button plus "no file selected" — whose intrinsic width exceeds a phone's
    // content column. Centred in a flex column it then overflowed both sides,
    // so the Choose File button sat outside the dashed border.
    render(FileDropZone, { file: null, children: prompt });

    expect(screen.getByRole('button', { name: /choose file/i })).toBeInTheDocument();
    expect(fileInput()).not.toBeVisible();
  });

  it('opens the native picker from the button', async () => {
    render(FileDropZone, { file: null, children: prompt });
    const click = vi.spyOn(fileInput(), 'click');

    await fireEvent.click(screen.getByRole('button', { name: /choose file/i }));

    expect(click).toHaveBeenCalledOnce();
  });

  it('replaces the picker with the picked file and a clear button', () => {
    // A second "choose file" control beside the picked row reads as a
    // different action, so the picker is withheld while a file is held.
    render(FileDropZone, { file: new File(['a,b'], 'positions.csv'), children: prompt });

    expect(screen.getByText('positions.csv')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /choose file/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /clear selected file/i })).toBeInTheDocument();
  });

  it('brings the picker back on clear, and resets the input so the same file can be re-picked', async () => {
    const onClear = vi.fn();
    const { rerender } = render(FileDropZone, {
      file: new File(['a,b'], 'positions.csv'),
      children: prompt,
      onClear,
    });
    fileInput().defaultValue = '';

    await fireEvent.click(screen.getByRole('button', { name: /clear selected file/i }));
    // The prop is bindable, so the parent's value is what re-renders it.
    await rerender({ file: null, children: prompt, onClear });

    expect(onClear).toHaveBeenCalledOnce();
    expect(fileInput().value).toBe('');
    expect(screen.getByRole('button', { name: /choose file/i })).toBeInTheDocument();
  });
});

it('accepts a multi-file drop over its content and refuses drops when disabled', async () => {
  const onFiles = vi.fn();
  const props = { overlay: true, onFiles, children: prompt };
  const { container, rerender } = render(FileDropZone, props);
  const files = [new File(['one'], 'one.txt'), new File(['two'], 'two.txt')];
  await fireEvent.dragEnter(container.querySelector('.dropzone')!, {
    dataTransfer: { types: ['Files'] },
  });
  expect(screen.getByText('Drop files to upload')).toBeTruthy();
  await fireEvent.drop(container.querySelector('.dropzone')!, { dataTransfer: { files } });
  expect(onFiles).toHaveBeenCalledWith(files);
  expect(screen.queryByText('Drop files to upload')).toBeNull();
  await rerender({ ...props, disabled: true });
  await fireEvent.drop(container.querySelector('.dropzone')!, { dataTransfer: { files } });
  expect(onFiles).toHaveBeenCalledTimes(1);
});
