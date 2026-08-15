import { describe, expect, it } from 'vitest';

import { isOnboarded, markOnboarded, pushSupported, urlBase64ToUint8Array } from './push';

/**
 * The real shape: a P-256 uncompressed point, 65 bytes, base64url, unpadded.
 * Throwaway test key, not any deployment's -- valid on purpose, which is why
 * a secret scanner flags it.
 */
const VAPID_KEY =
  'BCIlbEhlcRv6J3z4oq9ZpjK0pS2o7ts1_TBttZFVtkdS1_quoZ1J9s-fOS7JJfPWxEwiNcsNY1jQKN0OuiwLuio'; // gitleaks:allow

describe('urlBase64ToUint8Array', () => {
  it('decodes a real VAPID application server key to 65 bytes starting 0x04', () => {
    const bytes = urlBase64ToUint8Array(VAPID_KEY);
    // 65 bytes and a 0x04 lead byte is exactly what PushManager rejects the
    // subscription over if the decode is wrong, and the error it gives is
    // opaque — so assert the shape here instead.
    expect(bytes).toBeInstanceOf(Uint8Array);
    expect(bytes.length).toBe(65);
    expect(bytes[0]).toBe(0x04);
  });

  it('restores the padding base64url strips', () => {
    // "aGk" is "hi" with one '=' removed; atob would throw on it as-is.
    expect(Array.from(urlBase64ToUint8Array('aGk'))).toEqual([104, 105]);
    expect(Array.from(urlBase64ToUint8Array('aGk='))).toEqual([104, 105]);
  });

  it('maps the url-safe alphabet back to standard base64', () => {
    // 0xFB 0xEF encodes as "++8" in base64 and "--8" in base64url; 0xFF 0xEF
    // as "/+8" and "_-8". A decoder that skipped the swap would throw here.
    expect(Array.from(urlBase64ToUint8Array('--8'))).toEqual([251, 239]);
    expect(Array.from(urlBase64ToUint8Array('_-8'))).toEqual([255, 239]);
  });

  it('handles an empty key without throwing', () => {
    expect(urlBase64ToUint8Array('').length).toBe(0);
  });
});

describe('pushSupported', () => {
  it('is false in a environment without a PushManager', () => {
    // jsdom/node have no PushManager, which is the case the UI must survive:
    // an unsupported browser gets no bell rather than a switch that no-ops.
    expect(pushSupported()).toBe(false);
  });
});

describe('onboarding flag', () => {
  it('reads as not-onboarded where localStorage does not exist', () => {
    // The test runner has no localStorage, which stands in for SSR and for
    // Safari's private mode: the helper must answer, not throw.
    expect(isOnboarded()).toBe(false);
  });

  it('swallows a failing write rather than taking the shell down', () => {
    expect(() => markOnboarded()).not.toThrow();
  });
});
