import {
  File,
  FileText,
  FileImage,
  FileVideo,
  FileAudio,
  FileArchive,
  FileType,
} from '@lucide/svelte';

export function fileIcon(name: string, mime: string | null = null) {
  const ext = name.includes('.') ? name.split('.').at(-1)!.toLowerCase() : '';
  if (mime === 'application/pdf' || ext === 'pdf') return FileType;
  if (
    mime?.startsWith('image/') ||
    ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'heic', 'bmp', 'tiff'].includes(ext)
  )
    return FileImage;
  if (mime?.startsWith('video/') || ['mp4', 'mov', 'mkv', 'webm', 'avi', 'm4v'].includes(ext))
    return FileVideo;
  if (mime?.startsWith('audio/') || ['mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac'].includes(ext))
    return FileAudio;
  if (['zip', 'gz', 'tar', '7z', 'rar', 'bz2', 'xz'].includes(ext)) return FileArchive;
  if (
    mime?.startsWith('text/') ||
    mime?.startsWith('application/vnd.google-apps.') ||
    [
      'txt',
      'md',
      'markdown',
      'csv',
      'log',
      'json',
      'yaml',
      'yml',
      'toml',
      'xml',
      'html',
      'css',
      'js',
      'ts',
      'py',
      'sh',
      'doc',
      'docx',
      'odt',
      'rtf',
    ].includes(ext)
  )
    return FileText;
  return File;
}
