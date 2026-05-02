import type { Plugin } from 'vite';

interface MockReq {
	url: string;
	method: string;
	body: any;
}
type MockHandler = (req: MockReq) => unknown | undefined;

const user = {
	username: 'stefan',
	display_name: 'Stefan',
	features: {
		feeds: true,
		location: true,
		money: true,
		google_workspace: false,
		google_workspace_enabled: false,
	},
};

const emptyFeeds = { feeds: [], entries: [], total: 0 };

interface MockFeed {
	url: string;
	title?: string;
	category?: string;
	poll_interval_minutes?: number;
}
interface MockCategory {
	slug: string;
	title?: string;
}
const mockFeedsConfig: {
	settings: { default_poll_interval_minutes?: number };
	categories: MockCategory[];
	feeds: MockFeed[];
} = {
	settings: { default_poll_interval_minutes: 30 },
	categories: [
		{ slug: 'blogs', title: 'Blogs' },
		{ slug: 'tumblr', title: 'Tumblr' },
		{ slug: 'arena', title: 'Are.na' },
	],
	feeds: [
		{ url: 'https://example.com/feed.xml', title: 'Example Blog', category: 'blogs' },
		{ url: 'tumblr:nemfrog', title: 'Nemfrog', category: 'tumblr' },
		{ url: 'arena:cats-in-a-channel', category: 'arena', poll_interval_minutes: 60 },
	],
};

function feedsConfigResponse() {
	const now = new Date().toISOString();
	return {
		config: mockFeedsConfig,
		diagnostics: {
			total_feeds: mockFeedsConfig.feeds.length,
			total_entries: 42,
			unread_entries: 7,
			error_feeds: 0,
			last_poll_at: now,
		},
		feed_state: mockFeedsConfig.feeds.map((f) => ({
			url: f.url,
			last_fetched_at: now,
			last_error: null,
			error_count: 0,
		})),
	};
}

interface MockPlace {
	id: number;
	name: string;
	lat: number;
	lon: number;
	radius_meters: number;
	category: string;
	notes: string;
}

const mockPlaces: { places: MockPlace[] } = {
	places: [
		{ id: 1, name: 'Home', lat: 52.5200, lon: 13.4050, radius_meters: 80, category: 'home', notes: '' },
		{ id: 2, name: 'Office', lat: 52.5074, lon: 13.3904, radius_meters: 60, category: 'work', notes: '' },
		{ id: 3, name: 'Berghain Boiler Room (Side Entrance)', lat: 52.5111, lon: 13.4430, radius_meters: 50, category: 'social', notes: '' },
		{ id: 4, name: 'Climbing Gym', lat: 52.5300, lon: 13.4150, radius_meters: 40, category: 'gym', notes: '' },
		{ id: 5, name: 'Sunday Farmers Market on Maybachufer', lat: 52.4920, lon: 13.4280, radius_meters: 75, category: 'shopping', notes: '' },
		{ id: 6, name: 'Pizza Place', lat: 52.5180, lon: 13.4100, radius_meters: 30, category: 'food', notes: '' },
		{ id: 7, name: "Mom's", lat: 52.5400, lon: 13.4500, radius_meters: 100, category: 'family', notes: '' },
		{ id: 8, name: 'Co-working Spot', lat: 52.5050, lon: 13.3850, radius_meters: 45, category: 'work', notes: '' },
		{ id: 9, name: 'Dentist', lat: 52.5260, lon: 13.4020, radius_meters: 35, category: 'medical', notes: '' },
		{ id: 10, name: 'Café around the corner with the wifi password on the wall', lat: 52.5210, lon: 13.4080, radius_meters: 30, category: 'food', notes: '' },
		{ id: 11, name: 'Hotel Adlon', lat: 52.5163, lon: 13.3789, radius_meters: 50, category: 'hotel', notes: '' },
		{ id: 12, name: 'Friend Anna', lat: 52.5350, lon: 13.4200, radius_meters: 80, category: 'friend', notes: '' },
	],
};

interface MockDismissed {
	id: number;
	lat: number;
	lon: number;
	radius_meters: number;
	dismissed_at: string;
}
const mockDismissed: { dismissed: MockDismissed[] } = {
	dismissed: [
		{ id: 1, lat: 52.5000, lon: 13.4500, radius_meters: 120, dismissed_at: '2026-04-10T00:00:00Z' },
	],
};

interface MockCluster {
	lat: number;
	lon: number;
	radius_meters: number;
	total_pings: number;
	first_seen: string;
	last_seen: string;
}
const mockDiscover: { clusters: MockCluster[] } = {
	clusters: [
		{ lat: 52.5235, lon: 13.4115, radius_meters: 60, total_pings: 42, first_seen: '2026-04-15T08:00:00Z', last_seen: '2026-04-25T19:30:00Z' },
		{ lat: 52.4980, lon: 13.4380, radius_meters: 90, total_pings: 18, first_seen: '2026-04-20T12:00:00Z', last_seen: '2026-04-26T11:00:00Z' },
		{ lat: 52.5320, lon: 13.3950, radius_meters: 45, total_pings: 11, first_seen: '2026-04-22T17:00:00Z', last_seen: '2026-04-25T22:00:00Z' },
	],
};

const today = new Date().toISOString().slice(0, 10);
const mockPings = (() => {
	const pings: any[] = [];
	const startLat = 52.5200;
	const startLon = 13.4050;
	for (let i = 0; i < 50; i++) {
		const t = new Date();
		t.setHours(8 + Math.floor(i / 5), (i % 5) * 12, 0, 0);
		pings.push({
			recorded_at: t.toISOString(),
			lat: startLat + Math.sin(i / 6) * 0.01 + i * 0.0002,
			lon: startLon + Math.cos(i / 6) * 0.01 + i * 0.0003,
			horizontal_accuracy: 15,
			activity_type: i < 10 ? 'stationary' : i < 30 ? 'walking' : 'in_vehicle',
			speed: i < 10 ? 0 : i < 30 ? 1.2 : 8.5,
			place: i < 10 ? 'Home' : null,
			place_id: i < 10 ? 1 : null,
		});
	}
	return { pings, count: pings.length };
})();
const mockTrips = {
	date: today,
	trips: [
		{ start_lat: 52.5200, start_lon: 13.4050, end_lat: 52.5074, end_lon: 13.3904, start_time: `${today}T08:30:00Z`, end_time: `${today}T09:00:00Z`, distance_km: 4.2, duration_min: 30, mode: 'walking' },
	],
};
const mockDay = {
	date: today,
	timezone: 'Europe/Berlin',
	ping_count: 50,
	transit_pings: 20,
	stops: [
		{ lat: 52.5200, lon: 13.4050, name: 'Home', start_time: `${today}T07:00:00Z`, end_time: `${today}T08:30:00Z`, duration_min: 90, ping_count: 10 },
		{ lat: 52.5074, lon: 13.3904, name: 'Office', start_time: `${today}T09:00:00Z`, end_time: `${today}T17:00:00Z`, duration_min: 480, ping_count: 30 },
	],
};
const mockCurrent = {
	last_ping: { recorded_at: new Date().toISOString(), lat: 52.5200, lon: 13.4050, horizontal_accuracy: 12 },
	current_visit: { place: 'Home', place_id: 1, started_at: `${today}T07:00:00Z` },
};

const ledgers = { ledgers: ['main', 'business'] };
const checkResp = { error_count: 0, errors: [] };
const accountsResp = {
	accounts: [
		{ account: 'Assets:Checking', balance: '0.00 USD' },
		{ account: 'Assets:Savings', balance: '0.00 USD' },
		{ account: 'Expenses:Food', balance: '0.00 USD' },
		{ account: 'Income:Salary', balance: '0.00 USD' },
	],
};

let nextPlaceId = mockPlaces.places.length + 1;
let nextDismissedId = mockDismissed.dismissed.length + 1;

// Approximate distance between two coords in meters (sufficient for nearby clustering checks).
function distMeters(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
	const R = 6371000;
	const toRad = (d: number) => (d * Math.PI) / 180;
	const dLat = toRad(b.lat - a.lat);
	const dLon = toRad(b.lon - a.lon);
	const lat1 = toRad(a.lat);
	const lat2 = toRad(b.lat);
	const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
	return 2 * R * Math.asin(Math.sqrt(h));
}

function dropClusterNear(point: { lat: number; lon: number }, radius: number): void {
	mockDiscover.clusters = mockDiscover.clusters.filter(
		(c) => distMeters(c, point) > Math.max(radius, c.radius_meters),
	);
}

const handlers: MockHandler[] = [
	({ url }) => (url === '/istota/api/me' ? user : undefined),

	// Feeds settings: config GET/PUT
	({ url, method, body }) => {
		if (url !== '/istota/api/feeds/config') return undefined;
		if (method === 'GET') return feedsConfigResponse();
		if (method === 'PUT') {
			const cfg = body?.config;
			if (cfg && typeof cfg === 'object') {
				mockFeedsConfig.settings = cfg.settings ?? {};
				mockFeedsConfig.categories = cfg.categories ?? [];
				mockFeedsConfig.feeds = cfg.feeds ?? [];
			}
			return {
				status: 'ok',
				sync: {
					categories_added: 0,
					feeds_added: 0,
					feeds_updated: mockFeedsConfig.feeds.length,
				},
			};
		}
		return undefined;
	},

	({ url, method }) => {
		if (url !== '/istota/api/feeds/import-opml' || method !== 'POST') return undefined;
		return {
			status: 'ok',
			feeds_added: 1,
			feeds_updated: 0,
			categories_added: 1,
			rewritten_bridger_urls: 0,
		};
	},

	({ url }) => (url.startsWith('/istota/api/feeds') ? emptyFeeds : undefined),
	({ url }) => (url.startsWith('/istota/api/location/current') ? mockCurrent : undefined),

	// Place stats
	({ url }) => {
		const m = url.match(/\/istota\/api\/location\/places\/(\d+)\/stats/);
		if (!m) return undefined;
		return {
			place_id: Number(m[1]),
			total_visits: 0,
			first_visit: null,
			last_visit: null,
			avg_duration_min: null,
			total_duration_min: null,
			longest_visit_min: null,
		};
	},

	// Place CRUD
	({ url, method, body }) => {
		if (!url.startsWith('/istota/api/location/places')) return undefined;

		const idMatch = url.match(/\/istota\/api\/location\/places\/(\d+)$/);
		if (idMatch && method === 'PUT') {
			const id = Number(idMatch[1]);
			const idx = mockPlaces.places.findIndex((p) => p.id === id);
			if (idx >= 0) {
				mockPlaces.places[idx] = { ...mockPlaces.places[idx], ...body };
			}
			return mockPlaces.places[idx] ?? {};
		}
		if (idMatch && method === 'DELETE') {
			const id = Number(idMatch[1]);
			mockPlaces.places = mockPlaces.places.filter((p) => p.id !== id);
			return {};
		}
		if (method === 'POST') {
			const created: MockPlace = {
				id: nextPlaceId++,
				name: body?.name ?? 'Untitled',
				lat: body?.lat ?? 0,
				lon: body?.lon ?? 0,
				radius_meters: body?.radius_meters ?? 100,
				category: body?.category ?? 'other',
				notes: body?.notes ?? '',
			};
			mockPlaces.places.push(created);
			dropClusterNear(created, created.radius_meters);
			return created;
		}
		return mockPlaces;
	},

	// Dismissed clusters
	({ url, method, body }) => {
		if (!url.startsWith('/istota/api/location/dismissed-clusters')) return undefined;

		const idMatch = url.match(/\/istota\/api\/location\/dismissed-clusters\/(\d+)$/);
		if (idMatch && method === 'DELETE') {
			const id = Number(idMatch[1]);
			mockDismissed.dismissed = mockDismissed.dismissed.filter((d) => d.id !== id);
			return {};
		}
		if (method === 'POST') {
			const created: MockDismissed = {
				id: nextDismissedId++,
				lat: body?.lat ?? 0,
				lon: body?.lon ?? 0,
				radius_meters: body?.radius_meters ?? 100,
				dismissed_at: new Date().toISOString(),
			};
			mockDismissed.dismissed.push(created);
			dropClusterNear(created, created.radius_meters);
			return created;
		}
		return mockDismissed;
	},

	({ url }) => (url.startsWith('/istota/api/location/discover-places') ? mockDiscover : undefined),
	({ url }) => (url.startsWith('/istota/api/location/pings') ? mockPings : undefined),
	({ url }) => (url.startsWith('/istota/api/location/trips') ? mockTrips : undefined),
	({ url }) => (url.startsWith('/istota/api/location/day-summary') ? mockDay : undefined),
	({ url }) => (url.startsWith('/istota/money/api/ledgers') ? ledgers : undefined),
	({ url }) => (url.startsWith('/istota/money/api/check') ? checkResp : undefined),
	({ url }) => (url.startsWith('/istota/money/api/accounts') ? accountsResp : undefined),
];

function readBody(req: any): Promise<any> {
	return new Promise((resolve) => {
		const chunks: Buffer[] = [];
		req.on('data', (c: Buffer) => chunks.push(c));
		req.on('end', () => {
			if (chunks.length === 0) return resolve(undefined);
			const raw = Buffer.concat(chunks).toString('utf8');
			try {
				resolve(JSON.parse(raw));
			} catch {
				resolve(raw);
			}
		});
		req.on('error', () => resolve(undefined));
	});
}

export function mockApi(): Plugin {
	return {
		name: 'istota-mock-api',
		configureServer(server) {
			server.middlewares.use((req, res, next) => {
				if (!req.url?.startsWith('/istota/api/') && !req.url?.startsWith('/istota/money/api/')) return next();

				const method = req.method ?? 'GET';
				const respond = (body: unknown) => {
					res.setHeader('Content-Type', 'application/json');
					res.statusCode = 200;
					res.end(JSON.stringify(body));
				};

				const dispatch = (parsedBody: any) => {
					const ctx: MockReq = { url: req.url!, method, body: parsedBody };
					for (const h of handlers) {
						const body = h(ctx);
						if (body !== undefined) {
							respond(body);
							return;
						}
					}
					if (method !== 'GET') {
						respond({});
						return;
					}
					res.statusCode = 404;
					res.end('mock not implemented');
				};

				if (method === 'GET' || method === 'HEAD') {
					dispatch(undefined);
				} else {
					readBody(req).then(dispatch);
				}
			});
		},
	};
}
