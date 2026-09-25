// Minimal content script — injected into every labs.google tab.
//
// For v1 we don't put a floating button on the page. The extension popup
// (click extension icon) is enough UI. This file exists mainly so future
// versions can add an in-page status FAB without re-shipping the manifest.
//
// Currently we just signal "page loaded" so background.js can re-poll
// quickly when a labs.google tab finishes navigating.

try {
    chrome.runtime.sendMessage({ type: "LABS_TAB_READY", url: location.href });
} catch (_) { /* extension may not be fully alive yet */ }

// Keep Flow tab alive via inaudible Web Audio oscillator (G-Labs Studio architecture)
// Prevents Chrome from freezing, throttling, or discarding the tab when running in background.
const keepAlive = {
    ctx: null,
    osc: null,
    want: true,
    wire() {
        const onGesture = () => { this.unlock(); };
        for (const ev of ["pointerdown", "keydown", "touchstart", "mousemove"]) {
            window.addEventListener(ev, onGesture, { capture: true, passive: true });
        }
        document.addEventListener("visibilitychange", () => {
            if (this.want && !document.hidden) this.resume();
        });
    },
    unlock() {
        if (this.ctx) return;
        try {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return;
            this.ctx = new AC();
            const g = this.ctx.createGain();
            g.gain.value = 0.0001; // inaudible
            const o = this.ctx.createOscillator();
            o.frequency.value = 440;
            o.connect(g).connect(this.ctx.destination);
            o.start();
            this.osc = o;
        } catch (_) { this.ctx = null; }
    },
    resume() { try { if (this.ctx && this.ctx.state === "suspended") this.ctx.resume().catch(() => {}); } catch (_) {} },
    suspend() { try { if (this.ctx && this.ctx.state === "running") this.ctx.suspend().catch(() => {}); } catch (_) {} },
};
keepAlive.wire();
