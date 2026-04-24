export const ACTIVITY_COLORS: Record<string, string> = {
	driving: '#4a9eff',
	walking: '#4aff7f',
	running: '#ff9f4a',
	cycling: '#b44aff',
	stationary: '#666666',
};

export const ACTIVITY_LABELS: Record<string, string> = {
	driving: 'Driving',
	walking: 'Walking',
	running: 'Running',
	cycling: 'Cycling',
	stationary: 'Stationary',
};

export const ALL_ACTIVITY_TYPES = Object.keys(ACTIVITY_COLORS);

export const DEFAULT_PATH_COLOR = '#4a9eff';

// Speed-gradient stops (km/h → color). Log-ish spacing so walking/cycling/driving
// each land in a visually distinct band despite the wide speed range.
export const SPEED_GRADIENT_STOPS: Array<[number, string]> = [
	[0, '#2a4a8a'],     // standstill — deep blue
	[4, '#3f7dc8'],     // walking
	[10, '#4aff7f'],    // fast walk / slow bike — green
	[20, '#b6ff3a'],    // cycling
	[40, '#ffd84a'],    // urban driving — yellow
	[80, '#ff8a3a'],    // highway — orange
	[120, '#ff3a3a'],   // fast highway — red
	[200, '#ff3aff'],   // HSR / intercity rail — magenta
	[300, '#ffffff'],   // Shinkansen — white-hot
];
