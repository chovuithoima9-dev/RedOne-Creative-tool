/**
 * hijack_bypass.js — Three-level property trap for grecaptcha.enterprise.execute
 *
 * Runs in MAIN world at document_start (via manifest.json) — guaranteed to
 * execute before any page scripts, including the reCAPTCHA loader and Flow's
 * x2a trap.
 *
 * Flow's front-end (build boq_labs-ai-sandbox-frontend_20260922.00_p0+) ships a
 * function x2a that overwrites grecaptcha.enterprise.execute with a wrapper
 * which forces action: "extension_hijack_detected" via Object.assign. The
 * original execute is stashed in page-private state for the page's own
 * use. Any token minted through the public API carries the poison action, and
 * the backend rejects it with PUBLIC_ERROR_UNUSUAL_ACTIVITY.
 *
 * This script intercepts the pristine execute function the moment reCAPTCHA
 * assigns it — before x2a can overwrite it — and exposes it via
 * window.__redone_hijack for our extension to consume.
 *
 * Three capture layers, each a fallback for the one above:
 *   Level 1: defineProperty on window.grecaptcha
 *   Level 2: defineProperty on grecaptcha.enterprise
 *   Level 3: defineProperty on enterprise.execute
 *
 * Plus two polling fallbacks:
 *   - Rapid poll (2ms): catches cases where defineProperty gets clobbered
 *   - ready() callback race (5ms): registers our callback before Angular's
 *
 * After capture, all traps self-clean — Object.getOwnPropertyDescriptor returns
 * {value, writable} as if we were never there.
 */
(function () {
  'use strict';

  let _pristineExecute = null;
  let _capturedTrapped = false;
  let _captureSource = null;

  // ── Core capture ──────────────────────────────────────────────────────────

  function savePristine(fn, enterprise, source) {
    if (_pristineExecute || typeof fn !== 'function') return false;
    // Verify this isn't the trapped version — x2a's wrapper contains the
    // string "extension_hijack_detected" in its source.
    try {
      const src = fn.toString();
      if (src.includes('extension_hijack_detected')) {
        _capturedTrapped = true;
        return false;
      }
    } catch (e) {
      // toString on native code may throw; treat as plausibly pristine
    }
    _pristineExecute = fn.bind(enterprise);
    _captureSource = source;
    console.log('[RedOne] Pristine grecaptcha.enterprise.execute captured via', source);
    // Schedule cleanup on next microtask
    Promise.resolve().then(cleanAllTraps);
    return true;
  }

  // ── Trap cleanup ──────────────────────────────────────────────────────────

  function cleanTrap(obj, prop) {
    try {
      const desc = Object.getOwnPropertyDescriptor(obj, prop);
      if (!desc || (!desc.get && !desc.set)) return; // already clean
      Object.defineProperty(obj, prop, {
        value: desc.get ? desc.get() : undefined,
        writable: true,
        configurable: true,
        enumerable: true,
      });
    } catch (e) { /* frozen or non-configurable */ }
  }

  let _cleanedUp = false;
  function cleanAllTraps() {
    if (_cleanedUp) return;
    _cleanedUp = true;
    try {
      const gc = window.grecaptcha;
      if (gc) {
        const ent = gc.enterprise;
        if (ent) cleanTrap(ent, 'execute');
        cleanTrap(gc, 'enterprise');
      }
      cleanTrap(window, 'grecaptcha');
    } catch (e) { /* best-effort */ }
  }

  // ── Level 3: enterprise.execute ───────────────────────────────────────────

  function watchExecute(enterprise) {
    let _val = enterprise.execute;
    if (typeof _val === 'function' && savePristine(_val, enterprise, 'L3-immediate')) {
      return;
    }
    try {
      Object.defineProperty(enterprise, 'execute', {
        get() { return _val; },
        set(fn) {
          _val = fn;
          savePristine(fn, enterprise, 'L3-setter');
        },
        configurable: true,
        enumerable: true,
      });
    } catch (e) { /* non-configurable */ }
  }

  // ── Level 2: grecaptcha.enterprise ────────────────────────────────────────

  function watchEnterprise(grecaptcha) {
    let _val = grecaptcha.enterprise;
    if (_val && typeof _val === 'object') {
      watchExecute(_val);
    }
    try {
      Object.defineProperty(grecaptcha, 'enterprise', {
        get() { return _val; },
        set(obj) {
          _val = obj;
          if (obj && typeof obj === 'object') {
            watchExecute(obj);
          }
        },
        configurable: true,
        enumerable: true,
      });
    } catch (e) { /* non-configurable */ }
  }

  // ── Level 1: window.grecaptcha ────────────────────────────────────────────

  function installCapture() {
    let _val = window.grecaptcha;
    if (_val && typeof _val === 'object') {
      watchEnterprise(_val);
    }
    try {
      Object.defineProperty(window, 'grecaptcha', {
        get() { return _val; },
        set(obj) {
          _val = obj;
          if (obj && typeof obj === 'object') {
            watchEnterprise(obj);
          }
        },
        configurable: true,
        enumerable: true,
      });
    } catch (e) { /* non-configurable */ }

    // Fallback A: rapid poll (2ms)
    const pollId = setInterval(() => {
      if (_pristineExecute) { clearInterval(pollId); return; }
      try {
        const exec = window.grecaptcha?.enterprise?.execute;
        if (typeof exec === 'function') {
          savePristine(exec, window.grecaptcha.enterprise, 'poll-2ms');
          if (_pristineExecute) clearInterval(pollId);
        }
      } catch (e) { /* ignore */ }
    }, 2);
    setTimeout(() => clearInterval(pollId), 30000);

    // Fallback B: ready() callback race (5ms poll)
    const readyPollId = setInterval(() => {
      if (_pristineExecute) { clearInterval(readyPollId); return; }
      try {
        const ready = window.grecaptcha?.enterprise?.ready;
        if (typeof ready === 'function') {
          clearInterval(readyPollId);
          ready.call(window.grecaptcha.enterprise, () => {
            try {
              const exec = window.grecaptcha?.enterprise?.execute;
              if (typeof exec === 'function') {
                savePristine(exec, window.grecaptcha.enterprise, 'ready-race');
              }
            } catch (e) { /* ignore */ }
          });
        }
      } catch (e) { /* ignore */ }
    }, 5);
    setTimeout(() => clearInterval(readyPollId), 30000);
  }

  installCapture();

  // ── Expose state ─────────────────────────────────────────────────────────

  const state = Object.freeze({
    get pristine() { return _pristineExecute; },
    get trapped() { return _capturedTrapped; },
    get source() { return _captureSource; },
  });

  try {
    Object.defineProperty(window, '__redone_hijack', {
      value: state,
      writable: false,
      configurable: false,
      enumerable: false,
    });
  } catch (e) {
    window.__redone_hijack = state;
  }
  try {
    Object.defineProperty(window, '__fk_hijack', {
      value: state,
      writable: false,
      configurable: false,
      enumerable: false,
    });
  } catch (e) {
    window.__fk_hijack = state;
  }
})();
