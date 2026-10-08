import { get, writable } from 'svelte/store';
import { search, type SearchGroup } from '$lib/api';
import { loadSetting, saveSetting } from './persisted';

const RECENTS_KEY = 'istota.search.recent';
type Source = { source: string; label: string };
interface SearchState {
  query: string;
  results: SearchGroup[];
  sources: Source[];
  onDemand: Source[];
  loading: boolean;
  error: string;
  activeIndex: number;
  selectedSource: string | null;
  recentQueries: string[];
}

export function createSearch() {
  const saved = loadSetting<unknown>(RECENTS_KEY, []);
  const state = writable<SearchState>({
    query: '',
    results: [],
    sources: [],
    onDemand: [],
    loading: false,
    error: '',
    activeIndex: 0,
    selectedSource: null,
    recentQueries: Array.isArray(saved)
      ? saved.filter((q): q is string => typeof q === 'string').slice(0, 8)
      : [],
  });
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let generation = 0;
  let offset = 0;

  function cancel() {
    clearTimeout(timer);
    controller?.abort();
    generation++;
  }

  async function runQuery(append = false) {
    cancel();
    const current = get(state);
    const query = current.query.trim();
    if (query.length < 2) return;
    const version = generation;
    controller = new AbortController();
    const nextOffset = append ? offset + 20 : 0;
    state.update((s) => ({ ...s, loading: true, error: '' }));
    try {
      const response = await search(query, {
        sources: current.selectedSource ? [current.selectedSource] : undefined,
        limit: current.selectedSource ? 20 : 5,
        offset: nextOffset,
        signal: controller.signal,
      });
      if (version !== generation || get(state).query.trim() !== query) return;
      if (response.query !== undefined && response.query !== query) return;
      const groups = Array.isArray(response.groups) ? response.groups : [];
      offset = nextOffset;
      state.update((s) => {
        const results = append
          ? s.results.map((previous) => {
              const next = groups.find((g) => g.source === previous.source);
              if (!next) return previous;
              const seen = new Set(previous.results.map((hit) => hit.id));
              return {
                ...next,
                results: [...previous.results, ...next.results.filter((hit) => !seen.has(hit.id))],
              };
            })
          : groups;
        const onDemand = response.on_demand ?? s.onDemand;
        const sources = [...s.sources];
        for (const source of [...groups, ...onDemand]) {
          if (!sources.some((item) => item.source === source.source))
            sources.push({ source: source.source, label: source.label });
        }
        return { ...s, results, sources, onDemand, activeIndex: append ? s.activeIndex : 0 };
      });
    } catch (error) {
      if (version !== generation || (error instanceof Error && error.name === 'AbortError')) return;
      state.update((s) => ({ ...s, error: 'Search is unavailable right now.' }));
    } finally {
      if (version === generation) state.update((s) => ({ ...s, loading: false }));
    }
  }

  return {
    subscribe: state.subscribe,
    setQuery(query: string) {
      cancel();
      state.update((s) => ({
        ...s,
        query,
        results: [],
        loading: false,
        error: '',
        activeIndex: 0,
      }));
      if (query.trim().length >= 2) {
        state.update((s) => ({ ...s, loading: true }));
        timer = setTimeout(() => void runQuery(), 200);
      }
    },
    selectSource(source: string | null) {
      cancel();
      state.update((s) => ({
        ...s,
        selectedSource: source,
        results: [],
        activeIndex: 0,
        error: '',
        loading: false,
      }));
      void runQuery();
    },
    showMore() {
      if (!get(state).loading && get(state).selectedSource && offset < 500) void runQuery(true);
    },
    setActive(activeIndex: number) {
      state.update((s) => ({ ...s, activeIndex }));
    },
    rememberQuery() {
      const query = get(state).query.trim();
      if (!query) return;
      const recentQueries = [query, ...get(state).recentQueries.filter((q) => q !== query)].slice(
        0,
        8,
      );
      state.update((s) => ({ ...s, recentQueries }));
      saveSetting(RECENTS_KEY, recentQueries);
    },
    close() {
      cancel();
      offset = 0;
      state.update((s) => ({
        ...s,
        query: '',
        results: [],
        sources: [],
        onDemand: [],
        selectedSource: null,
        loading: false,
        error: '',
        activeIndex: 0,
      }));
    },
  };
}
