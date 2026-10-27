import type { SharedFolderPeople } from '$lib/api';

/** A share's name, or a stand-in when the sender gave none. */
export function folderName(item: { name: string | null }): string {
  return item.name || 'A folder';
}

function person(name: string | null | undefined, id: string | null | undefined): string {
  if (name && id) return `${name} (${id})`;
  return name || id || '';
}

/** Who offered a share: the owner, and the sharer first when it is someone else. */
export function sharedFrom(item: SharedFolderPeople): string {
  const owner = person(item.owner_display_name, item.owner);
  const sharer = item.shared_by ? person(item.shared_by_display_name, item.shared_by) : '';
  if (sharer && owner) return `${sharer}, for ${owner}`;
  return sharer || owner;
}

export function accessLabel(access: 'read' | 'write'): string {
  return access === 'write' ? 'Read and write' : 'Read only';
}

/** An accepted share's line: file or folder once known, who it is from, the sending server. */
export function shareCaption(
  share: SharedFolderPeople & { kind?: 'file' | 'folder' | null; host: string },
): string {
  const from = sharedFrom(share);
  return [
    share.kind === 'file' ? 'File' : share.kind === 'folder' ? 'Folder' : '',
    from ? `from ${from}` : '',
    share.host,
  ]
    .filter(Boolean)
    .join(' · ');
}
