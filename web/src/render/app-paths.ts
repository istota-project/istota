// `$app/paths` for a render bundle, which is not a SvelteKit build. A render
// document never calls istota, so `$lib/api`, pulled in by a component's
// fallback path, gets a base that names nothing on the render origin.
export const base = '';
export const assets = '';
