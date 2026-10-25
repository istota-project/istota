# Apps

Apps are small single-page tools that run in the web interface, get an entry in the Apps section, and reach Istota's data through a narrow, declared bridge. An app is a folder in your workspace with a `manifest.toml` and an `index.html`, written by you or by Istota. Unlike a module or a skill, an app is code Istota did not write and you did not review line by line, so it runs inside a sandbox and reaches nothing it did not ask for.

Apps is an experimental module. Enable it by adding `module_apps` to `[experimental] features` in `config.toml`; until then the Apps section is not shown. A user can also disable it for themselves in Settings.

## The Apps section

Apps sits in the top nav after Files. Its sidebar lists your installed apps; the main pane shows installed apps, any install waiting for your approval, and the app folders in your workspace that are not installed or whose files have changed. Open an app from the sidebar or its card.

On a phone the sidebar becomes a drawer. An app is judged against the pane it gets, not the device, so a desktop app in a narrow split view is treated as narrow.

## Installing an app

A draft is an app folder at `{bot_dir}/apps/<id>/` in your workspace. Istota can write it, and you can write it, but a draft never runs.

Select **Review install** on a draft in "All apps". Istota builds the folder into one document, records it as a pending install, and opens the approval screen. The screen shows, in order:

- the capabilities the app requests, with the ones that are new against the current version marked;
- the narrowest pane it says it needs, and why, when that is not a phone;
- the size of the built document;
- the validator's warnings, such as `alert()` in a phone-range app or a fixed width too wide for a phone;
- a diff of the source files against the current version, or the full source for a first install.

**Approve** makes it the next version and current. A version that requests nothing beyond the current version's grants is approved with one button. A version that requests a new capability shows the button after the capability list, and asks you to confirm that list, so an approval made on a screen that has since gone stale is refused.

While a pending install waits, later edits to the folder change nothing: the pending install is a copy Istota built and holds.

## Permissions

Every app is granted two capabilities, and the approval screen lists them without asking:

| Capability | What it gives an app |
| --- | --- |
| `kv:own` | Its own key-value namespace, `app.<id>` |
| `files:own` | Its own folder, `{bot_dir}/apps/<id>/data/` |

An app may request more, and you are asked for each:

| Capability | What it gives an app |
| --- | --- |
| `kv:read:<namespace>` | Read one of your other key-value namespaces |
| `files:read:<folder>` | Read a folder of your files |
| `calendar:read`, `calendar:read:<name>` | Read your calendars, or one by name |
| `skill:<skill>.<operation>` | One read-only operation from a fixed list |

There are no write capabilities on anything outside an app's own storage: an app cannot post to a room, send email, or write to your calendar, and it cannot ask Istota to do any of those things on its behalf.

Data an app writes into its namespace is treated as untrusted when Istota reads it back, the same as anything else written by something other than you.

## Versions and rollback

Every approved install is a version, kept until you delete it. **Versions** in an app's menu lists them with their dates and capabilities. **Make current** makes an older version current again, with the permissions it was approved for; an app's frame that is still open on the version you replaced is told to reload on its next call.

Each app's capabilities, its storage use, and an uninstall control are in Settings → Apps.

## Removing an app

**Uninstall** removes the app. Its key-value data and its `data/` folder are kept unless you check "also delete data"; its folder in your workspace is never touched. An app you uninstall stays in your workspace as a draft, so you can install it again.

## What an app cannot do

An app cannot reach the network: its document has no subresources, and the browser policy it runs under allows no request. It cannot open a page or download a file; to hand you a file it writes it to its own folder, which you open from Files. Copying to the clipboard, showing a dialog, and telling Istota about an error go through the shell, not the browser. It cannot use the camera, the microphone, your location, or your session.

An app runs only while its page is open. There are no background jobs, no scheduled apps, and no offline apps: without the server the frame does not load. In the iOS app an app gets the phone layout, and no native capability reaches it.

## The house kit

Every installed app gets Istota's tokens, a small client library and a set of components (list, table, form, button, dialog, chart). The kit applies your theme and text size before the first paint, keeps forms usable with the on-screen keyboard, and follows the same touch-target, type-size and layout rules as the rest of the interface. An app that does not use the kit still gets its sandbox and its permissions; the kit is what makes it look and feel like the rest of Istota.
