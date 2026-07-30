import { describe, it, expect } from 'vitest';
import {
  PROVISIONING_VERSION,
  decodeProvisioning,
  encodeProvisioning,
  endpointFromWebhookUrl,
  hostOf,
} from './provisioning';

const GOOD = { endpoint: 'https://example.invalid/webhooks/location', token: 'a'.repeat(43) };

describe('encode/decode round trip', () => {
  it('survives the trip', () => {
    expect(decodeProvisioning(encodeProvisioning(GOOD))).toEqual(GOOD);
  });

  it('carries the version, so a stale printed code is recognisable', () => {
    expect(JSON.parse(encodeProvisioning(GOOD)).v).toBe(PROVISIONING_VERSION);
  });

  it('is not a bare URL', () => {
    // A URL payload is offered for opening by every generic scanner, including
    // the iOS Camera app — which would put the token in Safari's history.
    expect(encodeProvisioning(GOOD).startsWith('http')).toBe(false);
  });
});

describe('decodeProvisioning rejects', () => {
  it.each([
    ['not JSON at all', 'https://example.invalid/webhooks/location?token=abc'],
    ['a JSON array', '[]'],
    ['null', 'null'],
    ['a future version', JSON.stringify({ v: 99, ...GOOD })],
    ['a missing token', JSON.stringify({ v: 1, endpoint: GOOD.endpoint })],
    ['an empty token', JSON.stringify({ v: 1, ...GOOD, token: '' })],
    ['a missing endpoint', JSON.stringify({ v: 1, token: GOOD.token })],
    ['a non-string token', JSON.stringify({ v: 1, endpoint: GOOD.endpoint, token: 42 })],
  ])('%s', (_label, payload) => {
    expect(decodeProvisioning(payload)).toBeNull();
  });

  it('a plaintext endpoint', () => {
    // The plugin refuses http too, but a token posted in the clear is a token
    // to rotate — so it never gets as far as being configured.
    const http = JSON.stringify({ v: 1, ...GOOD, endpoint: 'http://example.invalid/webhooks' });
    expect(decodeProvisioning(http)).toBeNull();
  });

  it('an enormous payload without parsing it', () => {
    expect(decodeProvisioning('{'.repeat(50_000))).toBeNull();
  });

  it('an over-long token inside otherwise valid JSON', () => {
    const fat = JSON.stringify({ v: 1, endpoint: GOOD.endpoint, token: 'x'.repeat(600) });
    expect(decodeProvisioning(fat)).toBeNull();
  });
});

describe('endpointFromWebhookUrl', () => {
  it('drops the token query the tracker does not use', () => {
    // The token travels as an Authorization header, so it appears in the
    // payload exactly once rather than twice.
    expect(endpointFromWebhookUrl('https://h/webhooks/location?token=abc')).toBe(
      'https://h/webhooks/location',
    );
  });

  it('leaves a bare URL alone', () => {
    expect(endpointFromWebhookUrl('https://h/webhooks/location')).toBe(
      'https://h/webhooks/location',
    );
  });

  it('drops the placeholder form the settings-info endpoint publishes', () => {
    expect(endpointFromWebhookUrl('https://h/webhooks/location?token=<token>')).toBe(
      'https://h/webhooks/location',
    );
  });
});

describe('hostOf', () => {
  it('names the host', () => {
    expect(hostOf('https://example.invalid/webhooks/location')).toBe('example.invalid');
  });

  it('falls back to the input rather than throwing', () => {
    expect(hostOf('not a url')).toBe('not a url');
  });
});
