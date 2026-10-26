// Navigate the frame away before its first `load`, so that load belongs to the
// recorder's page, which the shell then hands the bridge's port. The kit posted
// this document's key to the shell before this script ran, and the page never
// had it, so it cannot echo the key: every call it makes must be refused, and
// the bridge's hello deadline is what makes the shell notice and remove the
// frame. The recorder reports what the landing page tried
// (`/mark`), which `escape.spec.ts` reads back.
location.replace('http://127.0.0.1:5198/landed');
