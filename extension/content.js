// Minimal content script — injected into every labs.google and flow.google.com tab.
//
// Signals "page loaded" so background.js can re-poll quickly when a labs/flow tab
// finishes navigating, and marks tab as non-discardable.

try {
    chrome.runtime.sendMessage({ type: "LABS_TAB_READY", url: location.href });
} catch (_) { /* extension may not be fully alive yet */ }

// Keep Flow tab alive via inaudible Web Audio oscillator (G-Labs Studio architecture)
// Prevents Chrome from freezing, throttling, or discarding the tab when running in background.
const keepAlive = {
    ctx: null,
    osc: null,
    want: true,
    wired: false,
    _cleanup: null,

    wire() {
        if (this.wired) return;
        this.wired = true;

        const onGesture = () => {
            // Chrome Autoplay policy requires real user activation (e.g. click/touch/keydown)
            // NEVER trigger on mousemove, which throws:
            // "The AudioContext was not allowed to start. It must be resumed (or created) after a user gesture on the page."
            if (window.navigator?.userActivation && !window.navigator.userActivation.hasBeenActive) {
                return;
            }
            this.unlock();
        };

        const events = ["pointerdown", "keydown", "touchstart", "click"];
        const opts = { capture: true, passive: true };
        for (const ev of events) {
            window.addEventListener(ev, onGesture, opts);
        }

        this._cleanup = () => {
            for (const ev of events) {
                window.removeEventListener(ev, onGesture, opts);
            }
        };

        document.addEventListener("visibilitychange", () => {
            if (this.want && !document.hidden) this.resume();
        });
    },

    unlock() {
        if (this.ctx && this.ctx.state === "running") return;
        if (window.navigator?.userActivation && !window.navigator.userActivation.hasBeenActive) {
            return;
        }

        try {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return;

            if (!this.ctx) {
                this.ctx = new AC();
            }

            if (this.ctx.state === "suspended") {
                this.ctx.resume().then(() => {
                    this._startOscillator();
                }).catch(() => {});
            } else if (this.ctx.state === "running") {
                this._startOscillator();
            }
        } catch (_) {
            this.ctx = null;
        }
    },

    _startOscillator() {
        if (this.osc || !this.ctx || this.ctx.state !== "running") return;
        try {
            const g = this.ctx.createGain();
            g.gain.value = 0.0001; // inaudible
            const o = this.ctx.createOscillator();
            o.frequency.value = 440;
            o.connect(g).connect(this.ctx.destination);
            o.start();
            this.osc = o;
            if (this._cleanup) {
                this._cleanup();
                this._cleanup = null;
            }
        } catch (_) {
            this.osc = null;
        }
    },

    resume() {
        try {
            if (this.ctx && this.ctx.state === "suspended") {
                if (window.navigator?.userActivation && !window.navigator.userActivation.hasBeenActive) return;
                this.ctx.resume().catch(() => {});
            }
        } catch (_) {}
    },

    suspend() {
        try {
            if (this.ctx && this.ctx.state === "running") {
                this.ctx.suspend().catch(() => {});
            }
        } catch (_) {}
    },
};
keepAlive.wire();
