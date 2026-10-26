import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

const KIT = join(process.cwd(), 'src', 'apps', 'kit');

const NAMES = [
  'RTCPeerConnection',
  'webkitRTCPeerConnection',
  'mozRTCPeerConnection',
  'RTCDataChannel',
  'RTCIceCandidate',
] as const;

type Globals = Record<string, unknown>;

function runGuard(): void {
  new Function(readFileSync(join(KIT, 'guard.js'), 'utf8'))();
}

describe('guard.js', () => {
  afterEach(() => {
    for (const name of NAMES) {
      const descriptor = Object.getOwnPropertyDescriptor(window, name);
      if (descriptor?.configurable) delete (window as unknown as Globals)[name];
    }
  });

  it('removes the WebRTC constructors before any app code runs', () => {
    class Fake {}
    for (const name of NAMES) {
      Object.defineProperty(window, name, { value: Fake, configurable: true, writable: true });
    }
    runGuard();
    for (const name of NAMES) {
      expect((window as unknown as Globals)[name], name).toBeUndefined();
    }
  });

  it('leaves them unrecoverable by assignment', () => {
    Object.defineProperty(window, 'RTCPeerConnection', {
      value: class {},
      configurable: false,
      writable: true,
    });
    runGuard();
    try {
      (window as unknown as Globals).RTCPeerConnection = class {};
    } catch {
      // A frozen binding throws in strict code; either way it must stay empty.
    }
    expect((window as unknown as Globals).RTCPeerConnection).toBeUndefined();
  });

  it('runs where none of them exist', () => {
    expect(() => runGuard()).not.toThrow();
  });
});
