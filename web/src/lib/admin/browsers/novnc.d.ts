// noVNC ships no types. This declares the part of `RFB` (core/rfb.js, 1.7.0)
// the browser console uses; widen it as the console uses more.
declare module '@novnc/novnc' {
  export interface RFBCredentials {
    username?: string;
    password?: string;
    target?: string;
  }

  export interface RFBOptions {
    shared?: boolean;
    credentials?: RFBCredentials;
    repeaterID?: string;
    wsProtocols?: string[];
  }

  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, urlOrChannel: string | WebSocket, options?: RFBOptions);
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    clipViewport: boolean;
    focusOnClick: boolean;
    disconnect(): void;
    sendCredentials(credentials: RFBCredentials): void;
  }
}
