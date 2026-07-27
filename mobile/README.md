# Istota iOS shell

A Capacitor shell whose WebView loads the live Istota web UI, plus (from Stage 2)
an in-house background location tracker that replaces Overland.

The web assets are **not** bundled — `server.url` points the WebView at the real
deployment, so the WebView origin *is* the site origin and the existing cookie
session, the Nextcloud OAuth redirect chain and both SSE streams work with no
server-side auth changes. See
`Specs/Active/native-ios-app-capacitor-shell-background-location.md`.

## Prerequisites

- Xcode (full install, not just Command Line Tools) with an iOS SDK.
- Node 20+.
- **No CocoaPods.** Capacitor 8 scaffolds with Swift Package Manager; there is
  no `Podfile`. Dependencies resolve on first build and need network access.

If `xcode-select -p` still points at `/Library/Developer/CommandLineTools`,
either switch it (`sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`)
or prefix build commands with `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`.

## Setup

```bash
cd mobile
npm install
cp .env.example .env      # then set ISTOTA_SITE_URL to your deployment
npx cap sync ios
```

`ISTOTA_SITE_URL` is read at config-evaluation time by `capacitor.config.ts` and
must be `https://`. It lives in an untracked `.env` so a private hostname never
lands in the repo. **Re-run `npx cap sync ios` after changing it** — the value is
baked into `ios/App/App/capacitor.config.json` at sync time.

## Build and run

```bash
npm run open                 # open in Xcode (device builds, signing, TestFlight)
npm run run:sim              # build and run on a simulator
```

Or headless:

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
cd ios/App
xcodebuild -project App.xcodeproj -scheme App \
  -sdk iphonesimulator -configuration Debug \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath ../DerivedData CODE_SIGNING_ALLOWED=NO build
```

## Native plugin

The location plugin is compiled **into the app target** rather than shipped as a
separate Capacitor package — it is only ever consumed by this app, and a package
would mean maintaining a Swift package plus a `file:` dependency purely so the
CLI regenerates a `Package.swift` entry.

It is pure Objective-C, which is a first-class shape on Capacitor 8: `CAPPlugin`
is a plain ObjC class and the `CAP_PLUGIN` macros are intact. Three constraints
follow from that and are easy to trip over:

- `resolve:`/`reject:` come from `<Capacitor/Capacitor-Swift.h>`; the typed
  argument accessors come from `<Capacitor/CAPBridgedJSTypes.h>` (excluded from
  the Swift module map on purpose — it is the ObjC entry point). Import both.
- `CAP_PLUGIN` expands to `@interface <name> : NSObject`, so it cannot share a
  translation unit with the real `@interface <name> : CAPPlugin`. It lives in
  `IstotaLocationPluginBridge.m`, which never imports the class header.
- Plugins compiled into the app have no package to be discovered from, so they
  are registered explicitly in `ViewController.capacitorDidLoad()`.
