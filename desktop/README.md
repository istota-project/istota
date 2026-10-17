# Istota for the Mac

`Istota.app` is a menu-bar app. It supervises the local VM through the bundled Mac CLI (`host/mac/istota-stack`) and shows the web UI in a window of its own. The design is the Mac app spec; this file says how the directory is laid out and how to build it.

## Layout

- `src-tauri/`: the Tauri 2 shell, in Rust. `stack.rs` is the app's contract with the stack: every Mac CLI verb and option it calls, held to the scripts' usage text by `tests/test_desktop_contract.py`. `vm.rs` is the VM's state machine, `web.rs` the web UI's windows and what confines them, `login.rs` the one-use login code handoff, `firstrun.rs` the first run (requirements, the move and import offers, the release copied out of the app, the progress screen's steps), `settings.rs` the background installs and local model servers.
- `ui/`: the app's own pages (status, errors), SvelteKit with `adapter-static`. It reads the web UI's design tokens from `web/src/lib/styles/tokens.css` rather than a copy.
- `lima/`: `VERSION` pins Lima's and zstd's source tarballs by SHA-256; `build.sh` builds `limactl`, the aarch64 guest agent and zstd from them for macOS 14 and later, into `lima/build/` (gitignored).
- `build-app.sh`: builds and signs `Istota.app`.

## Building

Needs an Apple silicon Mac with Go, Rust (cargo) and Node, plus the Command Line Tools.

```bash
npm --prefix desktop ci
desktop/build-app.sh                      # ad hoc signature
desktop/build-app.sh --identity <SHA-1>   # an Apple Development certificate, this Mac only
```

The app is `desktop/src-tauri/target/release/bundle/macos/Istota.app`. A Developer ID signature, notarization and the release artifacts inside the app are `scripts/release-mac.sh`'s.

## Testing

```bash
cargo test --manifest-path desktop/src-tauri/Cargo.toml   # the state machine against a fake Mac CLI, and the rest
npm --prefix desktop run check                            # svelte-check on the UI
uv run pytest tests/test_desktop_contract.py              # the command table against the scripts
scripts/test-desktop.sh                                   # the desktop tier: builds the app and drives it
scripts/test-desktop.sh --control m9                      # row M9's negative control, which must go red
ISTOTA_DESKTOP_RELEASE=<out>/<tag> scripts/test-desktop.sh --control m6   # row M6's, which needs a release
```

The tier builds the app with the `desktop-test-hooks` feature into `target/desktop-tier`, never `target/release`. With `ISTOTA_DESKTOP_RELEASE` naming a directory from `scripts/build-appliance.sh` (and `ISTOTA_DESKTOP_RELEASE_KEY` the public key it was signed with, when that is not the project's), `tests/desktop/test_first_run.py` runs a first run to the web UI in a scratch state directory, instance (`istota-dt`) and workspace under `~/.istota-dt`, and removes the VM, the folder and its Keychain item afterwards. Without one it skips.

## Where the app keeps things

`~/Library/Application Support/Istota/` holds `state.json` (the profiles, and whether the local VM's first run finished), `release/<tag>/` (the release copied out of the app, which the VM mounts read-only), `lima/` (the app's own `LIMA_HOME`, excluded from Time Machine), `config/` (the Mac CLI's `XDG_CONFIG_HOME`) and `logs/`. `ISTOTA_DESKTOP_STATE_DIR` moves it, and `ISTOTA_DESKTOP_INSTANCE` names the app's Lima instance (`istota` by default), which also names the Mac CLI's Keychain item, `secret-key-<instance>`; the desktop tier sets both, and together they make a second install beside the first. A command-line install's `~/.lima` and `~/.config/istota-stack` are never read or written.
