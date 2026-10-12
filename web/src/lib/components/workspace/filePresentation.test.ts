import { expect, it } from 'vitest';
import {
  File,
  FileText,
  FileType,
  FileImage,
  FileVideo,
  FileAudio,
  FileArchive,
} from '@lucide/svelte';
import { fileIcon } from './filePresentation';

it('uses distinct existing icons for common file types and Drive MIME types', () => {
  expect(fileIcon('note.md')).toBe(FileText);
  expect(fileIcon('photo.JPG')).toBe(FileImage);
  expect(fileIcon('invoice.pdf')).toBe(FileType);
  expect(fileIcon('clip.mp4')).toBe(FileVideo);
  expect(fileIcon('recording.mp3')).toBe(FileAudio);
  expect(fileIcon('backup.zip')).toBe(FileArchive);
  expect(fileIcon('unknown.bin')).toBe(File);
  expect(fileIcon('Project', 'application/vnd.google-apps.document')).toBe(FileText);
  expect(fileIcon('Photo', 'image/png')).toBe(FileImage);
});
