import type { Plugin } from 'vite';

interface MockHandler {
	(req: { url: string; method: string }): unknown | undefined;
}

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
const mockPlaces = {
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
const emptyDismissed = { dismissed: [] };
const emptyDiscover = { clusters: [] };
const emptyPings = { pings: [], count: 0 };
const emptyTrips = { date: new Date().toISOString().slice(0, 10), trips: [] };
const emptyDay = {
	date: new Date().toISOString().slice(0, 10),
	timezone: 'UTC',
	ping_count: 0,
	transit_pings: 0,
	stops: [],
};
const emptyCurrent = { last_ping: null, current_visit: null };

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

const handlers: MockHandler[] = [
	({ url }) => (url === '/istota/api/me' ? user : undefined),
	({ url }) => (url.startsWith('/istota/api/feeds') ? emptyFeeds : undefined),
	({ url }) => (url.startsWith('/istota/api/location/current') ? emptyCurrent : undefined),
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
	({ url }) => (url.startsWith('/istota/api/location/places') ? mockPlaces : undefined),
	({ url }) => (url.startsWith('/istota/api/location/dismissed-clusters') ? emptyDismissed : undefined),
	({ url }) => (url.startsWith('/istota/api/location/discover-places') ? emptyDiscover : undefined),
	({ url }) => (url.startsWith('/istota/api/location/pings') ? emptyPings : undefined),
	({ url }) => (url.startsWith('/istota/api/location/trips') ? emptyTrips : undefined),
	({ url }) => (url.startsWith('/istota/api/location/day-summary') ? emptyDay : undefined),
	({ url }) => (url.startsWith('/istota/money/api/ledgers') ? ledgers : undefined),
	({ url }) => (url.startsWith('/istota/money/api/check') ? checkResp : undefined),
	({ url }) => (url.startsWith('/istota/money/api/accounts') ? accountsResp : undefined),
];

export function mockApi(): Plugin {
	return {
		name: 'istota-mock-api',
		configureServer(server) {
			server.middlewares.use((req, res, next) => {
				if (!req.url?.startsWith('/istota/api/') && !req.url?.startsWith('/istota/money/api/')) return next();
				const ctx = { url: req.url, method: req.method ?? 'GET' };
				for (const h of handlers) {
					const body = h(ctx);
					if (body !== undefined) {
						res.setHeader('Content-Type', 'application/json');
						res.statusCode = 200;
						res.end(JSON.stringify(body));
						return;
					}
				}
				if (ctx.method !== 'GET') {
					res.statusCode = 200;
					res.setHeader('Content-Type', 'application/json');
					res.end('{}');
					return;
				}
				res.statusCode = 404;
				res.end('mock not implemented');
			});
		},
	};
}
