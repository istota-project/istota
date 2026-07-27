import 'dotenv/config';
import type { CapacitorConfig } from '@capacitor/cli';

// The shell does not bundle the web assets — it points a WKWebView at the live
// site, so the WebView origin *is* the real origin and the existing cookie
// session, the OAuth redirect chain and both SSE streams work unmodified.
// See Specs/Active/native-ios-app-capacitor-shell-background-location.md.
//
// The URL is read from an untracked mobile/.env so a private hostname never
// lands in the repo, and staging can be swapped without editing tracked files.
const siteUrl = process.env.ISTOTA_SITE_URL?.trim();

if (!siteUrl) {
  throw new Error(
    'ISTOTA_SITE_URL is not set. Copy mobile/.env.example to mobile/.env and ' +
      'set it to your deployment, e.g. https://your-host.tld/istota/',
  );
}

if (!siteUrl.startsWith('https://')) {
  // A plaintext origin would make the session cookie (Secure) unusable and
  // would need an ATS exception. Fail loudly rather than debug it on device.
  throw new Error(`ISTOTA_SITE_URL must be https://, got: ${siteUrl}`);
}

// Only needed when Nextcloud is served from a different hostname than the web
// UI. In the standard single-host deployment (nginx routes / -> Nextcloud and
// /istota/ -> web) the whole OAuth redirect chain stays on one host and this
// stays empty.
const allowNavigation = (process.env.ISTOTA_ALLOW_NAVIGATION ?? '')
  .split(',')
  .map((h) => h.trim())
  .filter(Boolean);

const config: CapacitorConfig = {
  appId: 'com.cynium.istota',
  appName: 'Istota',
  // Unused at runtime because server.url wins, but Capacitor requires the
  // directory to exist for `cap sync`. Holds the offline fallback page.
  webDir: 'public',
  server: {
    url: siteUrl,
    cleartext: false,
    ...(allowNavigation.length ? { allowNavigation } : {}),
  },
  ios: {
    scheme: 'Istota',
  },
};

export default config;
