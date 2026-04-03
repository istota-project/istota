/**
 * Read a value from localStorage, returning the fallback if missing or invalid.
 */
export function loadSetting<T>(key: string, fallback: T): T {
	if (typeof window === 'undefined') return fallback;
	try {
		const raw = localStorage.getItem(key);
		if (raw === null) return fallback;
		return JSON.parse(raw) as T;
	} catch {
		return fallback;
	}
}

/**
 * Write a value to localStorage.
 */
export function saveSetting<T>(key: string, value: T): void {
	if (typeof window === 'undefined') return;
	try {
		localStorage.setItem(key, JSON.stringify(value));
	} catch {
		// quota exceeded or disabled — ignore
	}
}
