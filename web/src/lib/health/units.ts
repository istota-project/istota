import type { DisplayUnits } from '$lib/api';

export const METRIC_LABELS: Record<string, string> = {
	weight: 'Weight',
	resting_hr: 'Resting HR',
	blood_pressure_systolic: 'BP Systolic',
	blood_pressure_diastolic: 'BP Diastolic',
	body_fat_pct: 'Body Fat',
	body_temp: 'Body Temp',
	respiratory_rate: 'Respiratory Rate',
	blood_oxygen: 'Blood O₂',
};

export const METRIC_UNITS: Record<string, string> = {
	weight: 'kg',
	resting_hr: 'bpm',
	blood_pressure_systolic: 'mmHg',
	blood_pressure_diastolic: 'mmHg',
	body_fat_pct: '%',
	body_temp: '°C',
	respiratory_rate: 'brpm',
	blood_oxygen: '%',
};

export function metricLabel(key: string): string {
	return METRIC_LABELS[key] || key;
}

export function canonicalUnit(key: string): string {
	return METRIC_UNITS[key] || '';
}

export function kgToLb(kg: number): number {
	return kg * 2.2046226218;
}

export function lbToKg(lb: number): number {
	return lb / 2.2046226218;
}

export function cToF(c: number): number {
	return (c * 9) / 5 + 32;
}

export function cmToFtIn(cm: number): { feet: number; inches: number } {
	const totalInches = cm / 2.54;
	const feet = Math.floor(totalInches / 12);
	const inches = Math.round((totalInches - feet * 12) * 10) / 10;
	return { feet, inches };
}

export function ftInToCm(feet: number, inches: number): number {
	return (feet * 12 + inches) * 2.54;
}

export function fToC(f: number): number {
	return ((f - 32) * 5) / 9;
}

export const LOG_UNIT_CHOICES: Record<string, readonly string[]> = {
	weight: ['kg', 'lb'],
	body_temp: ['°C', '°F'],
};

export function toCanonical(
	metric: string,
	value: number,
	unit: string,
): { value: number; unit: string } {
	const canonical = METRIC_UNITS[metric] || unit;
	if (metric === 'weight' && unit === 'lb') {
		return { value: lbToKg(value), unit: canonical };
	}
	if (metric === 'body_temp' && unit === '°F') {
		return { value: fToC(value), unit: canonical };
	}
	return { value, unit: canonical };
}

/** Display a metric value in the user's preferred units. */
export function formatStat(
	metric: string,
	value: number,
	storedUnit: string,
	display: DisplayUnits,
): { value: number; unit: string } {
	if (metric === 'weight' && display.weight === 'lb' && storedUnit === 'kg') {
		return { value: Math.round(kgToLb(value) * 10) / 10, unit: 'lb' };
	}
	if (metric === 'body_temp' && display.temp === 'F' && storedUnit === '°C') {
		return { value: Math.round(cToF(value) * 10) / 10, unit: '°F' };
	}
	return { value: Math.round(value * 100) / 100, unit: storedUnit };
}
