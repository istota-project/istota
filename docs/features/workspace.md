# Workspace

Open Workspace in the main navigation to see the files and connections Istota can reach for you. On a phone, open Places beside the page title to choose a location.

## Places and access

My files is your own workspace folder. Linked folders, room folders and group folders appear when they are available to your account. Room membership is checked again on each request, so leaving a room removes access to its folder.

The folder toolbar shows the selected place's access for a task in a private room: Read-write, Read-only, Via skill or No access. These badges describe task access; they do not mean that the web page can edit the files. Open “How Istota reaches your files” to see the selected place's access in a private room, on your own turn in a shared room, and on a guest's turn or a task nobody asked for. The disclosure also explains where files are stored and whether sandboxing limits task access.

Personal memory is not loaded into shared-room prompts, even when your own turn can read your files. A room folder is available to tasks in that room only. Group folders show “Depends on room” for shared-room turns: every reader must belong to the group, with no guests or other bots present. A room linked to a group can use only that group. Connections such as Google Drive are reached through a skill during a task. On a workspace that is the Nextcloud mount, Talk attachments show their reach but cannot be browsed here. On a local workspace, files posted in Talk are in the room's own folder.

## Browsing

Choose a folder to open it. The breadcrumbs return to its parents, and the address bar keeps the current place and folder so browser Back, Forward and reload work. Files in My files, linked folders, room folders and group folders open in the shared viewer. Access is checked again when you open a file.

Hidden files are off by default. Show hidden remembers your choice in this browser. Symbolic links are labelled and cannot be opened or followed. Other special entries, such as sockets, are also inert.

A large directory shows at most 2,000 entries and a notice. This is a slice of the directory's enumeration order, sorted with folders first; it is not necessarily the alphabetically first 2,000 entries. Entries whose names cannot be represented as text are skipped and counted below the listing. A folder that cannot be read shows an error and a Retry button.

## Adding files

In My files, use Upload or drop files onto the listing. Files upload one at a time, with progress and any error beside each file. Uploads keep existing files: a repeated name gets a number. The default size limit is 100 MB per file. New folder creates a folder in the location shown in the dialog. Room folders, group folders, linked folders and Drive have no upload or new-folder controls.

## Configuration files

The Configuration section in the Files sidebar lists your own config files: `USER.md`, `CRON.md`, `TASKS.md` and `HEARTBEAT.md`, with their sizes. Skill instructions, collapsed below them, has one row per skill you can use. A skill's file is added to that skill's own instructions whenever it loads. Edit opens the file in an editor. You can only reach your own files.

A save checks the file the way Istota reads it, and nothing is written when the check fails:

- `CRON.md` and `HEARTBEAT.md` are parsed first. A TOML error, a job with a bad field or a command job saved by someone who is not an admin is listed under the text, and your text stays in the editor.
- A skill's file is limited to 32 KB, the size above which Istota does not load it. The safety skills take no user instructions and are not listed.
- If the file changed since you opened it, usually because Istota wrote to it, the save is refused and your text stays. Reload to see the other version. If Istota is writing the file at that moment, try again.

Jobs saved in `CRON.md` change at the next CRON.md sync, not at once. A file that is not a plain file, such as a link a task left in its place, cannot be edited.

## Nextcloud and Drive

On a Nextcloud-backed workspace, My files shows a Shared marker when Nextcloud reports a share. Open in Nextcloud appears when the file is shared with your account. If share information is unavailable, browsing still works and a notice explains what is missing. A disconnected Nextcloud folder shows an error instead of an empty listing.

Google Drive has My Drive and Shared with me views. Open folders to browse them and choose Load more when more items are available. Files open in Google Drive in another tab. If Drive access is off or the connection needs attention, use the Connections link to restore it.

## Pins

Open the three-dot menu beside a file or folder and choose Pin to add it to Pinned at the top of Files. The same menu has Download for files and a provider link when available. Pins are references, not copies. Open a pinned file or folder from there, or remove its pin with the adjacent remove button. A file that was deleted or is no longer accessible stays in the list as Unavailable so you can remove it yourself. You can keep up to 50 pins.

Istota sees the references on your next private-room task and can read them when needed. Pinning does not load a file's contents. Pins follow the personal-memory rule: they are absent from shared-room member and guest turns, and present in email thread rooms that run as your correspondence. Unavailable references stay in the prompt until you remove their pins.
