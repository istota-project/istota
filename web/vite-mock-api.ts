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
const emptyPlaces = { places: [] };
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
	({ url }) => (url.startsWith('/istota/api/location/places') ? emptyPlaces : undefined),
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
