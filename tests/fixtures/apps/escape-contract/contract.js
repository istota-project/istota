// The escape tier's own check on itself: a fixture that does only what any app
// may, through the kit, and reports each step the way every fixture reports.
//
// Reporting convention (web/tests-apps-escape/README.md): one `kv:own` set of
// `report:<name>` to `{outcome, detail}` per step, then `done` to true. The
// harness's in-memory store records every set it relays.
(async () => {
  const report = (name, outcome, detail) =>
    window.istota.kv.set(`report:${name}`, { outcome, detail: String(detail ?? '') });

  await report('kit', typeof window.istota === 'object' ? 'present' : 'missing');
  await report('env', 'read', JSON.stringify(window.istota.env));

  await window.istota.kv.set('round-trip', 42);
  const back = await window.istota.kv.get('round-trip');
  await report('round-trip', back === 42 ? 'ok' : 'mismatch', back);

  document.getElementById('status').textContent = 'Done';
  await window.istota.kv.set('done', true);
})();
