// RedOne Auth Helper — background service worker.
//
// Bridge protocol (talks to backend FastAPI on port 8000):
//   GET  /sync/status            heartbeat
//   GET  /sync/next-task         pull next task to perform (recaptcha / proxy-fetch)
//   POST /sync/task-result       submit task result
//   GET  /sync/accounts-probe    backend asks ext to scan logged-in labs tabs
//
// Payload envelope is `{ d: <xor_hex> }` where the inner is JSON XOR'd with
// 0x5A. This is just lightweight obfuscation, NOT security — keeps the
// protocol from being trivially fingerprinted on the wire.
//
// Task kinds:
//   - "recaptcha": harvest grecaptcha.enterprise.execute(site_key, action)
//                  from a labs.google tab. Returns token string.
//   - "proxy_fetch": run `fetch(url, {credentials: "include", ...})` from
//                    inside a labs.google tab so the request inherits the
//                    user's real Chrome session cookies. Returns
//                    {status, headers, body|base64}.
//
// Design choices for this bridge:
//   - Single-account first iteration: picks the FIRST labs.google tab.
//     Multi-tab routing comes later.
//   - Same XOR key (0x5A) for protocol parity with the backend.

const BRIDGE_HOSTS = ["http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://127.0.0.1:8099", "http://127.0.0.1:8098"];
const POLL_INTERVAL_MS = 1500;
// When we just claimed a task AND still have spare capacity, poll again
// almost immediately so a batch of N parallel items fans out in a few
// hundred ms instead of N × POLL_INTERVAL_MS. Only used during fan-out.
const FAST_POLL_MS = 150;
// Max tasks the extension runs CONCURRENTLY inside the labs.google tab.
// Previously every task ran strictly one-at-a-time (the poll loop awaited
// each ~27s generation before claiming the next), so the backend's
// "luồng song song" setting had no effect. We now dispatch up to this many
// at once. Kept modest: too many simultaneous generate calls from one
// session looks bot-like to Google → more reCAPTCHA / 429. The backend's
// own batch size (the user's concurrency setting) is the real throttle;
// this is just a safety ceiling so it can actually run in parallel.
const MAX_CONCURRENT = 4;
const XOR_KEY = 0x5A;

let _polling = false;
let _inFlight = 0;          // tasks currently executing (not yet result-posted)
let _connected = false;
let _tokenCount = 0;
let _lastSuccessAt = null;

// Anti-idle mouse jiggle — mimics human cursor activity on labs.google tab
// when tasks are running. Reduces reCAPTCHA risk score accumulation during
// long batch runs. Inspired by G-Labs Automation's grokKeepAlive pattern.
let _lastJiggleAt = 0;

// Active detected account from Google Flow tab (email, tier, credits)
let _lastDetectedAccount = { email: null, tier: "FREE", credits: null };
let _lastAccountDetectAt = 0;
// Map tabId -> { email, tier, credits, url, lastDetectAt }
const _tabAccountMap = new Map();

// Shakker bridge state — set when content_shakker.js sends SHAKKER_SYNC.
// Restored from chrome.storage at SW wake so the popup shows correct
// "last seen" info even right after a service worker restart.
let _shakkerEmail = null;
// Short-lived shared Google login request: set when the member clicks
// "Đăng nhập tài khoản chung", consumed by content_accounts.js, then cleared.
// Held only in SW memory (never persisted to storage).
let _pendingGoogleLogin = null;
let _shakkerLastSync = null;
let _targetGoogleEmail = null;

// Restore counters
chrome.storage.local.get(
    ["tokenCount", "lastSuccessAt", "shakkerEmail", "shakkerLastSync", "targetGoogleEmail"],
    (data) => {
        _tokenCount = data.tokenCount || 0;
        _lastSuccessAt = data.lastSuccessAt || null;
        _shakkerEmail = data.shakkerEmail || null;
        _shakkerLastSync = data.shakkerLastSync || null;
        _targetGoogleEmail = data.targetGoogleEmail || null;
    }
);

// Keep the service worker alive via alarms (MV3 service workers auto-suspend
// otherwise). Three different alarm intervals to cover edge cases.
chrome.alarms.create("poll", { periodInMinutes: 0.5 });
chrome.alarms.create("heartbeat", { periodInMinutes: 0.25 });
// Anti-idle: inject mouse/scroll activity every ~2.5min to keep reCAPTCHA
// risk score low during long batch runs.
chrome.alarms.create("antiIdle", { periodInMinutes: 2.5 });

chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === "poll" && !_polling) {
        _pollLoop();
    }
    if (alarm.name === "heartbeat") {
        for (const h of BRIDGE_HOSTS) {
            try {
                await fetch(`${h}/sync/status`, {
                    signal: AbortSignal.timeout(3000),
                });
                break; // One successful heartbeat is enough for keep-alive
            } catch (_) { /* ignore */ }
        }
    }
    // Anti-idle is handled passively via Web Audio keep-alive in content.js.
    // NEVER inject synthetic MouseEvents (event.isTrusted === false) as BotGuard explicitly flags them.
});

chrome.runtime.onInstalled.addListener(() => _pollLoop());
chrome.runtime.onStartup.addListener(() => _pollLoop());

// Invalidate per-tab cache on navigation or closure, and restart poll loop
// when any labs.google or flow.google.com tab completes load.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.url) {
        _tabAccountMap.delete(tabId);
        _lastAccountDetectAt = 0;
        _lastDetectedAccount = { email: null, tier: "FREE", credits: null };
    }
    if (
        changeInfo.status === "complete" &&
        tab.url &&
        (tab.url.includes("labs.google") || tab.url.includes("flow.google.com"))
    ) {
        try {
            chrome.tabs.update(tabId, { autoDiscardable: false }).catch(() => {});
        } catch (_) {}
        if (!_polling) {
            _pollLoop();
        }
    }
});

chrome.tabs.onRemoved.addListener((tabId) => {
    _tabAccountMap.delete(tabId);
});


// ── XOR codec ────────────────────────────────────────────────────────

function _encode(plaintext) {
    // Force ASCII via \uXXXX escapes for any non-ASCII chars, then XOR
    // each byte and hex-encode.
    const ascii = plaintext.replace(/[-￿]/g,
        (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
    let result = "";
    for (let i = 0; i < ascii.length; i++) {
        result += (ascii.charCodeAt(i) ^ XOR_KEY).toString(16).padStart(2, "0");
    }
    return result;
}

function _decode(hexString) {
    let result = "";
    for (let i = 0; i < hexString.length; i += 2) {
        result += String.fromCharCode(parseInt(hexString.substr(i, 2), 16) ^ XOR_KEY);
    }
    return result;
}


// ── Bridge HTTP helpers ──────────────────────────────────────────────

async function _bridgeGet(host, path) {
    const res = await fetch(`${host}${path}`, {
        signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) throw new Error(`bridge ${path} → HTTP ${res.status}`);
    const raw = await res.json();
    // Server can either send envelope `{d: xor}` or plaintext JSON. Accept both.
    if (raw && typeof raw.d === "string") {
        return JSON.parse(_decode(raw.d));
    }
    return raw;
}

async function _bridgePost(host, path, payload) {
    const body = JSON.stringify({ d: _encode(JSON.stringify(payload)) });
    const res = await fetch(`${host}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        signal: AbortSignal.timeout(15000),
    });
    if (!res.ok) throw new Error(`bridge ${path} → HTTP ${res.status}`);
    const raw = await res.json();
    if (raw && typeof raw.d === "string") {
        return JSON.parse(_decode(raw.d));
    }
    return raw;
}


// ── Tab discovery ────────────────────────────────────────────────────

/**
 * Find the first shakker.ai tab open in this Chrome instance.
 * Returns the tab object or null. Used by the popup to render a
 * "shakker tab open ✓" indicator and decide whether to show the
 * "Mở Shakker.ai" button.
 */
async function _findShakkerTab() {
    try {
        const tabs = await chrome.tabs.query({});
        const matches = tabs.filter(t => t.url && t.url.includes("shakker.ai"));
        return matches[0] || null;
    } catch (_) {
        return null;
    }
}


/**
 * Find the most relevant labs.google / flow.google.com tab that's signed in.
 * Returns the tab object or null.
 *
 * Scoring criteria:
 *  1. Penalize discarded tabs (+100) or tabs with error titles (+200).
 *  2. Strongly prioritize tabs with tier === 'ULTRA' (-60) or 'PRO' (-30).
 *  3. Strongly prioritize the user's active/focused tab (-40).
 *  4. Favor more recently accessed tabs (t.lastAccessed recency).
 *  5. Prefer flow.google.com (-10) over legacy labs.google.
 */
async function _findLabsTab() {
    for (let attempt = 0; attempt < 3; attempt++) {
        try {
            const tabs = await chrome.tabs.query({});
            const labsTabs = tabs.filter(t => {
                const u = t.url || t.pendingUrl || "";
                return (u.includes("labs.google") || u.includes("flow.google.com")) &&
                    !u.includes("accounts.google.com");
            });
            if (labsTabs.length > 0) {
                const now = Date.now();
                const score = (t) => {
                    const u = t.url || t.pendingUrl || "";
                    const title = (t.title || "").toLowerCase();
                    let s = 0;

                    // 1. Heavy penalty for discarded or error tabs
                    if (t.discarded) s += 100;
                    if (title.includes("không tìm thấy") || title.includes("not found") || title.includes("404")) {
                        s += 200;
                    }

                    // 2. High priority for ULTRA / PRO tier accounts
                    const cachedAcc = _tabAccountMap.get(t.id);
                    const tier = cachedAcc ? (cachedAcc.tier || "").toUpperCase() : "";
                    if (tier === "ULTRA") s -= 60;
                    else if (tier === "PRO") s -= 30;

                    // 3. User focus & recency
                    if (t.active) s -= 40; // Active tab in window
                    const ageMs = Math.max(0, now - (t.lastAccessed || 0));
                    // Up to +30 penalty for older inactive tabs (1 pt per 10s age, cap at 30)
                    s += Math.min(30, Math.floor(ageMs / 10000));

                    // 4. Domain: flow.google.com is current, labs.google is legacy
                    if (u.includes("flow.google.com")) {
                        s -= 10;
                    }

                    // 5. Heavy preference for active project tab (reCAPTCHA and tokens loaded here)
                    if (u.includes("/project/")) {
                        s -= 80;
                    }

                    return s;
                };

                const chosen = labsTabs.sort((a, b) => score(a) - score(b))[0];
                if (chosen && chosen.id) {
                    try {
                        chrome.tabs.update(chosen.id, { autoDiscardable: false }).catch(() => {});
                    } catch (_) {}
                }
                return chosen;
            }
        } catch (_) { /* fall through to retry */ }
        await new Promise(r => setTimeout(r, 300));
    }
    // No labs/flow tab found: return null. Never auto-create tabs.
    return null;
}

/**
 * Accurately detect which Google account is currently signed in on the Flow tab,
 * along with subscription tier (ULTRA vs FREE vs PRO) and remaining credits.
 */
async function _detectFlowAccountDetails(tab) {
    if (!tab || !tab.id) return { email: null, tier: "FREE", credits: null };
    const tabUrl = tab.url || tab.pendingUrl || "";
    if (!tabUrl.includes("flow.google.com") && !tabUrl.includes("labs.google")) {
        return { email: null, tier: "FREE", credits: null };
    }

    // Check per-tab cache (valid for 5s)
    const cached = _tabAccountMap.get(tab.id);
    if (cached && cached.email && (Date.now() - (cached.lastDetectAt || 0) < 5000)) {
        return cached;
    }

    let detectedEmail = null;
    let detectedTier = "FREE";
    let detectedCredits = null;

    try {
        // Method 1: Execute in-tab DOM & globals inspection
        const domResults = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: "MAIN",
            func: () => {
                const emailRegex = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/;
                const isRealUserEmail = (em) => {
                    if (!em || typeof em !== "string") return false;
                    const low = em.trim().toLowerCase();
                    if (low.includes("w3.org") || low.includes("schema.org") || low.includes("sentry") || low.includes("github")) return false;
                    if (low.endsWith("@google.com") && (low.includes("service") || low.includes("noreply") || low.includes("support"))) return false;
                    return emailRegex.test(low);
                };

                let email = null;
                let tier = "FREE";
                let credits = null;

                // 1. Email detection from profile elements
                const selectors = [
                    'a[href*="SignOutOptions"]',
                    'a[href*="accounts.google.com"]',
                    'a[href*="myaccount.google.com"]',
                    '[aria-label*="@"]',
                    'img[alt*="@"]',
                    '[data-email]',
                    '[data-identifier]',
                    '.gb_d[aria-label]',
                    '.gb_A[aria-label]',
                    'header [aria-label]',
                    'button[aria-label*="Google"]',
                    'a[aria-label*="Google"]',
                    'div[aria-label*="Google"]',
                    'div[aria-label*="Tài khoản"]',
                    'button[aria-label*="Tài khoản"]'
                ];

                for (const sel of selectors) {
                    if (email) break;
                    try {
                        const elements = document.querySelectorAll(sel);
                        for (const el of elements) {
                            const attrs = [
                                el.getAttribute("data-email"),
                                el.getAttribute("data-identifier"),
                                el.getAttribute("aria-label"),
                                el.getAttribute("alt"),
                                el.getAttribute("title"),
                                el.getAttribute("href"),
                                el.textContent
                            ];
                            for (const val of attrs) {
                                if (val) {
                                    const match = val.match(emailRegex);
                                    if (match && isRealUserEmail(match[0])) {
                                        email = match[0].toLowerCase();
                                        break;
                                    }
                                }
                            }
                            if (email) break;
                        }
                    } catch (_) {}
                }

                // Fallback email from WIZ_global_data
                try {
                    if (!email && window.WIZ_global_data) {
                        if (typeof window.WIZ_global_data.oBeKc === "string" && isRealUserEmail(window.WIZ_global_data.oBeKc)) {
                            email = window.WIZ_global_data.oBeKc.toLowerCase();
                        }
                        if (!email) {
                            const jsonStr = JSON.stringify(window.WIZ_global_data);
                            const matches = jsonStr.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g) || [];
                            for (const m of matches) {
                                if (isRealUserEmail(m)) {
                                    email = m.toLowerCase();
                                    break;
                                }
                            }
                        }
                    }
                } catch (_) {}

                // Fallback email from URL query authuser
                if (!email) {
                    try {
                        const m = window.location.search.match(/[?&]authuser=([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/);
                        if (m && isRealUserEmail(m[1])) {
                            email = decodeURIComponent(m[1]).toLowerCase();
                        }
                    } catch (_) {}
                }

                // 2. Credits detection from page text
                try {
                    const text = document.body ? document.body.innerText : "";
                    const creditPatterns = [
                        /(\d[\d,.\s]*)\s*T\u00edn\s*d\u1ee5ng\s*Flow/i,
                        /(\d[\d,.\s]*)\s*Flow\s*credits?/i,
                        /(\d[\d,.\s]*)\s*T\u00edn\s*d\u1ee5ng\s*AI/i,
                        /(\d[\d,.\s]*)\s*AI\s*credits?/i,
                        /credits?\s*:?\s*(\d[\d,.\s]*)/i,
                        /(\d[\d,.\s]*)\s*credits?\s*remaining/i,
                    ];
                    for (const p of creditPatterns) {
                        const m = text.match(p);
                        if (m) {
                            const val = parseInt(m[1].replace(/[,.\s]/g, ""), 10);
                            if (!isNaN(val)) {
                                credits = val;
                                break;
                            }
                        }
                    }
                } catch (_) {}

                // 3. Tier detection:
                // 3a. Check WIZ_global_data
                try {
                    if (window.WIZ_global_data) {
                        const str = JSON.stringify(window.WIZ_global_data);
                        if (str.includes("PAYGATE_TIER_TWO") || str.includes("SERVICE_TIER_ADVANCED") || str.includes("G1_TIER2") || str.includes("AI_PREMIUM")) {
                            tier = "ULTRA";
                        } else if (str.includes("PAYGATE_TIER_ONE") || str.includes("SERVICE_TIER_STANDARD") || str.includes("G1_TIER1")) {
                            tier = "PRO";
                        }
                    }
                } catch (_) {}

                // 3b. Check DOM elements (badges, buttons, chips)
                if (tier === "FREE") {
                    try {
                        const candidates = Array.from(document.querySelectorAll(
                            'header, nav, [role="banner"], [class*="badge"], [class*="tier"], [class*="chip"], [class*="pill"], button, a, div[role="button"], span'
                        ));
                        for (const el of candidates) {
                            const txt = (el.innerText || el.textContent || "").trim();
                            const aria = (el.getAttribute("aria-label") || "").trim();
                            const combined = `${txt} ${aria}`;

                            // Disregard marketing upsells like "Upgrade to Ultra" / "Nâng cấp lên Ultra" / "Try Ultra"
                            const isUpsell = /nâng cấp|upgrade|try\s+ultra|thử\s+ultra|get\s+ultra/i.test(combined);
                            if (!isUpsell) {
                                if (/\bultra\b/i.test(txt) && txt.length <= 25) {
                                    tier = "ULTRA";
                                    break;
                                }
                                if (/google one ai premium|gói ultra|gói ai cao cấp|ai premium/i.test(combined)) {
                                    tier = "ULTRA";
                                    break;
                                }
                                if (/\bpro\b/i.test(txt) && txt.length <= 20) {
                                    tier = "PRO";
                                }
                            }
                        }
                    } catch (_) {}
                }

                // 3c. Credits deduction: Free tier has <= 100 credits. 500+ credits indicates Ultra/Pro subscription.
                if (tier === "FREE" && credits !== null && credits >= 500) {
                    tier = "ULTRA";
                }

                return { email, tier, credits };
            }
        });

        const r = domResults && domResults[0] && domResults[0].result;
        if (r && typeof r === "object") {
            if (r.email) detectedEmail = r.email.trim().toLowerCase();
            if (r.tier) detectedTier = r.tier.toUpperCase();
            if (r.credits != null) detectedCredits = r.credits;
        }

        // Method 2: Check NextAuth session ONLY if on legacy labs.google/fx page (NOT flow.google.com)
        if (!detectedEmail && tabUrl.includes("labs.google/fx")) {
            try {
                const cookies = await chrome.cookies.getAll({ domain: "labs.google" });
                const hasSession = cookies.some(c => c.name.includes("session-token"));
                if (hasSession) {
                    const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join("; ");
                    const res = await fetch("https://labs.google/fx/api/auth/session", {
                        headers: { "Accept": "application/json", "Cookie": cookieStr }
                    });
                    if (res.ok) {
                        const data = await res.json();
                        if (data && data.user && data.user.email) {
                            detectedEmail = data.user.email.toLowerCase();
                        }
                    }
                }
            } catch (_) {}
        }
    } catch (e) {
        console.warn("[RedOne] _detectFlowAccountDetails error:", e);
    }

    const resultObj = {
        email: detectedEmail,
        tier: detectedTier || "FREE",
        credits: detectedCredits,
        lastDetectAt: Date.now(),
    };

    if (detectedEmail) {
        _tabAccountMap.set(tab.id, resultObj);
        _lastDetectedAccount = resultObj;
        _lastAccountDetectAt = Date.now();
    }

    return _lastDetectedAccount;
}

async function _detectFlowUserEmail(tab) {
    const d = await _detectFlowAccountDetails(tab);
    return d.email || "";
}

/**
 * Verify this Chrome profile is actually signed into Google Labs FLOW —
 * NOT merely logged into some Google service. We require the labs.google
 * NextAuth session cookie (the SAME "final proof of login" the backend
 * checks in routers/accounts.py: `__Secure-next-auth.session-token`).
 *
 * Why this must be Flow-specific, not generic google.com cookies:
 * the extension can be force-installed in MULTIPLE Chrome profiles, all
 * polling the SAME localhost backend. Task dispatch is "first `ready`
 * poller wins" with no profile/account targeting. If a profile that has
 * generic google.com auth cookies but is NOT signed into Flow reported
 * "ready", it would claim Flow tasks and run them under a tab with no
 * valid Flow session — the download would 401 "No session found" and a
 * login-check would raise SessionDead (false "chưa đăng nhập" banner).
 * Requiring the Flow session cookie makes such a profile report
 * "no_login" so the backend never hands it a Flow task.
 *
 * NextAuth may split a large session token into chunked cookies
 * (`…session-token.0`, `.1`, …) — startsWith() catches those too.
 */
async function _isSignedIn() {
    try {
        // 1) Check cookies for flow.google.com (returns all cookies sent to flow.google.com,
        // including .google.com cookies like SID, HSID, SSID, __Secure-1PSID, etc.)
        const flowCookies = await chrome.cookies.getAll({ url: "https://flow.google.com" });
        const hasFlowSession = flowCookies.some(c =>
            c.name === "SID" || c.name === "HSID" || c.name === "SSID" ||
            c.name.startsWith("__Secure-1PSID") || c.name.startsWith("__Secure-3PSID") ||
            c.name.startsWith("__Secure-next-auth.session-token") ||
            c.name.startsWith("next-auth.session-token")
        );
        if (hasFlowSession) return true;

        // 2) Fallback: check labs.google cookies
        const labsCookies = await chrome.cookies.getAll({ url: "https://labs.google" });
        const hasLabsSession = labsCookies.some(c =>
            c.name.startsWith("__Secure-next-auth.session-token") ||
            c.name.startsWith("next-auth.session-token") ||
            c.name === "SID"
        );
        return hasLabsSession;
    } catch (_) {
        return false;
    }
}

// ── Task: read browser cookies for given domains ────────────────────
// Used by the backend to feed yt-dlp YouTube auth without exporting a
// cookies.txt by hand. chrome.cookies returns ALREADY-DECRYPTED cookies, so
// this sidesteps yt-dlp's "could not copy Chrome cookie database" problem
// (which fails while Chrome is running). Requires host_permissions for the
// domains + the "cookies" permission (manifest).
async function _doGetCookiesTask(task) {
    const domains = (task.payload && task.payload.domains) || [".youtube.com", ".google.com"];
    const out = [];
    for (const d of domains) {
        try {
            const cks = await chrome.cookies.getAll({ domain: d });
            for (const c of cks) {
                out.push({
                    domain: c.domain,
                    name: c.name,
                    value: c.value,
                    path: c.path || "/",
                    secure: !!c.secure,
                    hostOnly: !!c.hostOnly,
                    expirationDate: c.expirationDate || 0,
                });
            }
        } catch (_) { /* skip this domain */ }
    }
    return { cookies: out, count: out.length };
}


async function _ensureFlowMintPage(tab) {
    if (!tab || !tab.id) return tab;
    const url = tab.url || tab.pendingUrl || "";
    // If already on flow.google.com (dashboard or project workspace), keep it there!
    // reCAPTCHA Enterprise is loaded natively on flow.google.com with sitekey 6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV.
    if (url.includes("flow.google.com")) {
        return tab;
    }
    // If on labs.google, keep it
    if (url.includes("labs.google")) {
        return tab;
    }
    // If on an unrelated tab, navigate to flow.google.com
    try {
        console.log("[RedOne] Navigating tab to flow.google.com for native reCAPTCHA");
        await chrome.tabs.update(tab.id, { url: "https://flow.google.com/" });
        await new Promise((resolve) => {
            const l = (id, info) => {
                if (id === tab.id && info.status === "complete") {
                    chrome.tabs.onUpdated.removeListener(l);
                    resolve();
                }
            };
            chrome.tabs.onUpdated.addListener(l);
            setTimeout(() => {
                chrome.tabs.onUpdated.removeListener(l);
                resolve();
            }, 12000);
        });
        await new Promise(r => setTimeout(r, 2000));
        return await chrome.tabs.get(tab.id);
    } catch (e) {
        console.warn("[RedOne] _ensureFlowMintPage nav error:", e);
    }
    return tab;
}

// ── Task: reCAPTCHA harvest ──────────────────────────────────────────

async function _doRecaptchaTask(task) {
    const { site_key = "", action = "" } = task.payload || {};
    let tab = await _findLabsTab();
    if (!tab) {
        return { token: null, error: "no labs.google tab open" };
    }
    if (!(await _isSignedIn())) {
        return { token: null, error: "user not signed into Google" };
    }
    tab = await _ensureFlowMintPage(tab);
    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: "MAIN",
            func: async (siteKeyArg, actionArg) => {
                try {
                    let key = (siteKeyArg && !siteKeyArg.includes("@") && siteKeyArg.startsWith("6")) ? siteKeyArg : "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
                    // G-Labs Studio architecture: wait for native grecaptcha.enterprise to be loaded by Google
                    const _dl = Date.now() + 10000;
                    while (Date.now() < _dl && (typeof grecaptcha === "undefined" || !grecaptcha.enterprise || !grecaptcha.enterprise.execute)) {
                        await new Promise(r => setTimeout(r, 250));
                    }
                    if (typeof grecaptcha === "undefined" || !grecaptcha.enterprise || !grecaptcha.enterprise.execute) {
                        return { token: null, error: "grecaptcha.enterprise not loaded" };
                    }
                    if (!key) {
                        // Try the internal grecaptcha config — most reliable source
                        try {
                            // eslint-disable-next-line no-undef
                            if (typeof ___grecaptcha_cfg !== "undefined" && ___grecaptcha_cfg.clients) {
                                // eslint-disable-next-line no-undef
                                const clients = ___grecaptcha_cfg.clients;
                                const keys = Object.keys(clients);
                                if (keys.length > 0) {
                                    const client = clients[keys[0]];
                                    for (const p of Object.keys(client)) {
                                        const v = client[p];
                                        if (v && typeof v === "object") {
                                            for (const p2 of Object.keys(v)) {
                                                const v2 = v[p2];
                                                if (v2 && typeof v2 === "object" && v2.sitekey) {
                                                    key = v2.sitekey;
                                                    break;
                                                }
                                            }
                                        }
                                        if (key) break;
                                    }
                                }
                            }
                        } catch (_) { /* fall through */ }
                        // Fallback: scrape `?render=<sitekey>` from script tag
                        if (!key) {
                            const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                            for (const el of scripts) {
                                const m = el.src.match(/[?&]render=([^&]+)/);
                                if (m && m[1] !== "explicit") {
                                    key = m[1];
                                    break;
                                }
                            }
                        }
                    }
                    if (!key) key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
                    const targetAction = actionArg || "IMAGE_GENERATION";

                    // ── Strategy 1: Pristine execute (captured by hijack_bypass.js) ──
                    const hijack = window.__redone_hijack || window.__fk_hijack;
                    if (hijack && typeof hijack.pristine === "function") {
                        try {
                            const token = await Promise.race([
                                hijack.pristine(key, { action: targetAction }),
                                new Promise((_, reject) => setTimeout(
                                    () => reject(new Error("pristine execute timeout (15s)")),
                                    15000,
                                )),
                            ]);
                            if (token) {
                                console.log("[RedOne] Token minted via pristine execute (" + (hijack.source || "pristine") + "):", String(token).substring(0, 25) + "...");
                                return { token: String(token), error: null, sitekey: key, bypass: "pristine" };
                            }
                        } catch (pErr) {
                            console.warn("[RedOne] Pristine execute failed, falling back:", pErr);
                        }
                    }

                    // ── Strategy 2: Object.assign neuter ──
                    // Google Flow x2a trap overrides execute to force action: "extension_hijack_detected" via Object.assign.
                    // We temporarily intercept Object.assign so that any action set to "extension_hijack_detected" is replaced with targetAction!
                    const _realObjectAssign = Object.assign;
                    try {
                        Object.assign = function (target, ...sources) {
                            for (const src of sources) {
                                if (src && typeof src === "object" && src.action === "extension_hijack_detected") {
                                    try { src.action = targetAction; } catch (_) {}
                                }
                            }
                            const res = _realObjectAssign.apply(this, [target, ...sources]);
                            if (res && typeof res === "object" && res.action === "extension_hijack_detected") {
                                try { res.action = targetAction; } catch (_) {}
                            }
                            return res;
                        };

                        await new Promise(resolve => grecaptcha.enterprise.ready(resolve));
                        const token = await Promise.race([
                            grecaptcha.enterprise.execute(key, { action: targetAction }),
                            new Promise((_, reject) => setTimeout(
                                () => reject(new Error("grecaptcha execute timeout (15s)")),
                                15000,
                            )),
                        ]);
                        if (token) {
                            console.log("[RedOne] Token minted via Object.assign neuter:", String(token).substring(0, 25) + "...");
                            return { token: String(token), error: null, sitekey: key, bypass: "assign_neuter" };
                        }
                    } finally {
                        Object.assign = _realObjectAssign;
                    }

                    // ── Strategy 3: Invisible hidden widget fallback ──
                    try {
                        let host = document.getElementById("redone-recaptcha-host");
                        if (!host) {
                            host = document.createElement("div");
                            host.id = "redone-recaptcha-host";
                            host.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;";
                            document.documentElement.appendChild(host);
                        }
                        const widgetId = await new Promise((resolve, reject) => {
                            try {
                                const id = grecaptcha.enterprise.render(host, {
                                    sitekey: key,
                                    size: "invisible",
                                    callback: () => {},
                                    "error-callback": (m) => reject(new Error("render_error: " + m)),
                                });
                                resolve(id);
                            } catch (e) {
                                reject(e);
                            }
                        });
                        const token = await Promise.race([
                            grecaptcha.enterprise.execute(widgetId, { action: targetAction }),
                            new Promise((_, reject) => setTimeout(
                                () => reject(new Error("widget execute timeout (15s)")),
                                15000,
                            )),
                        ]);
                        if (token) {
                            return { token: String(token), error: null, sitekey: key, bypass: "widget" };
                        }
                    } catch (wErr) {
                        console.warn("[RedOne] Widget fallback failed:", wErr);
                    }

                    return { token: null, error: "all minting strategies failed" };
                } catch (err) {
                    return { token: null, error: err.message || String(err) };
                }
            },
            args: [site_key, action],
        });
        const r = results && results[0] && results[0].result;
        if (r && r.token) {
            _tokenCount++;
            _lastSuccessAt = Date.now();
            chrome.storage.local.set({
                tokenCount: _tokenCount,
                lastSuccessAt: _lastSuccessAt,
            });
        }
        return r || { token: null, error: "no script result" };
    } catch (e) {
        return { token: null, error: "executeScript: " + String(e) };
    }
}


// ── Task: proxy fetch (run fetch from inside labs.google tab) ────────

async function _doProxyFetchTask(task) {
    const p = task.payload || {};
    const url = String(p.url || "");
    const method = String(p.method || "GET").toUpperCase();
    const headers = (p.headers && typeof p.headers === "object") ? p.headers : {};
    const body = (p.body === null || p.body === undefined) ? null : String(p.body);
    const responseMode = String(p.response_mode || "json"); // "json" | "text" | "arraybuffer"
    const timeoutMs = Math.max(1000, Math.min(600000, Number(p.timeout_ms) || 60000));

    if (!url) return { status: 0, error: "missing url" };

    // ── Special path: labs.google session endpoint ──────────────────
    // When the user's tab is on flow.google.com, fetching
    // labs.google/fx/api/auth/session from inside the tab is CROSS-ORIGIN
    // → same-origin credentials won't attach labs.google cookies → the
    // endpoint returns {} instead of the real session.
    //
    // Fix: for this specific auth URL, fetch from the background service
    // worker and manually build the Cookie header from chrome.cookies.
    // The extension has "cookies" permission + host_permissions for
    // labs.google, so this is fully authorised.
    if (url.includes("labs.google/fx/api/auth/session")) {
        try {
            const cookies = await chrome.cookies.getAll({ domain: "labs.google" });
            const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join("; ");
            const fetchHeaders = { ...headers, "Cookie": cookieStr };
            const ac = new AbortController();
            const timer = setTimeout(() => ac.abort(), timeoutMs);
            const res = await fetch(url, {
                method,
                headers: fetchHeaders,
                signal: ac.signal,
            });
            clearTimeout(timer);
            const status = res.status;
            let body = null;
            try {
                const txt = await res.text();
                try { body = JSON.parse(txt); } catch (_) { body = txt; }
            } catch (_) { /* empty */ }
            return { status, body };
        } catch (e) {
            return { status: 0, error: "bg-fetch auth/session: " + String(e) };
        }
    }

    const tab = await _findLabsTab();
    if (!tab) return { status: 0, error: "no labs.google tab" };

    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: "MAIN",
            func: async (specJson, bodyStr) => {
                const s = JSON.parse(specJson);
                const ac = new AbortController();
                const timer = setTimeout(() => ac.abort(), s.timeoutMs);
                try {
                    const opts = {
                        method: s.method,
                        headers: s.headers,
                        // Use default credentials mode = "same-origin".
                        // Critical: forcing "include" makes CORS preflight require
                        // `Access-Control-Allow-Credentials: true` from the target
                        // server. aisandbox-pa.googleapis.com (cross-origin from
                        // labs.google) does NOT return that header for our custom
                        // headers → preflight fails → "Failed to fetch".
                        //
                        // With "same-origin":
                        //   - labs.google → labs.google calls (e.g. /fx/api/auth/session,
                        //     trpc media redirect) still send cookies (same-origin)
                        //   - labs.google → aisandbox-pa calls omit cookies, which
                        //     is what we want — Bearer token in Authorization header
                        //     is the actual auth mechanism.
                        // Matches the original Playwright FlowClient behavior exactly.
                        signal: ac.signal,
                    };
                    if (bodyStr) opts.body = bodyStr;
                    const res = await fetch(s.url, opts);
                    const status = res.status;
                    const respHeaders = {};
                    try {
                        for (const [k, v] of res.headers.entries()) respHeaders[k] = v;
                    } catch (_) { /* not iterable in some envs */ }

                    if (s.responseMode === "arraybuffer") {
                        const buf = await res.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let bin = "";
                        // Build base64 in chunks to avoid stack overflow on large bodies
                        const CHUNK = 0x8000;
                        for (let i = 0; i < bytes.length; i += CHUNK) {
                            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
                        }
                        clearTimeout(timer);
                        return {
                            status,
                            headers: respHeaders,
                            body_b64: btoa(bin),
                            content_type: respHeaders["content-type"] || "",
                        };
                    }
                    if (s.responseMode === "text") {
                        const txt = await res.text();
                        clearTimeout(timer);
                        return { status, headers: respHeaders, body: txt };
                    }
                    // Default: json (with text fallback)
                    let txt = "";
                    try { txt = await res.text(); } catch (_) { /* empty body */ }
                    let data = null;
                    try { data = txt ? JSON.parse(txt) : null; } catch (_) { /* not json, return as text */ }
                    clearTimeout(timer);
                    return {
                        status,
                        headers: respHeaders,
                        body: data,
                        body_text: data === null ? txt : null,
                    };
                } catch (err) {
                    clearTimeout(timer);
                    return { status: 0, error: "fetch: " + (err.message || String(err)) };
                }
            },
            args: [
                JSON.stringify({ url, method, headers, responseMode, timeoutMs }),
                body || "",
            ],
        });
        return (results && results[0] && results[0].result) || { status: 0, error: "no script result" };
    } catch (e) {
        return { status: 0, error: "executeScript: " + String(e) };
    }
}


// ── Task: batch_execute (BOQ/WIZ batchexecute RPC on flow.google.com) ─
//
// Google migrated Flow's API from aisandbox-pa REST (Bearer token) to
// BOQ/WIZ batchexecute RPC (cookie auth + CSRF token). This function
// executes a batchexecute call from INSIDE the flow.google.com tab,
// so the browser automatically attaches all Google auth cookies.

async function _doBatchExecuteTask(task) {
    const p = task.payload || {};
    const rpcId = String(p.rpc_id || "");
    const innerPayload = p.inner_payload; // JS value — will be JSON.stringify'd
    const sourcePath = String(p.source_path || "/");
    const timeoutMs = Math.max(5000, Math.min(300000, Number(p.timeout_ms) || 120000));
    // Inline reCAPTCHA: when set, token is minted INSIDE the same executeScript
    // that sends the batchexecute — keeping token + request in the same execution
    // context and TLS session. This prevents Google's UNUSUAL_ACTIVITY detection
    // which fires when a token minted in one context is used in another.
    const recaptchaAction = String(p.recaptcha_action || "");

    if (!rpcId) return { status: 0, error: "missing rpc_id" };

    let tab = await _findLabsTab();
    if (!tab) return { status: 0, error: "no flow.google.com tab" };
    if (recaptchaAction) {
        tab = await _ensureFlowMintPage(tab);
    }

    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: "MAIN",
            func: async (rpcIdArg, innerPayloadJson, sourcePathArg, timeoutMsArg, recaptchaActionArg) => {
                try {
                    if (rpcIdArg === "DEBUG_DOM") {
                        const w = window.WIZ_global_data || {};
                        return {
                            status: 200,
                            rpc_result: {
                                title: document.title,
                                url: window.location.href,
                                at: w.SNlM0e || null,
                                fSid: w.FdrFJe || null,
                                bl: w.cfb2h || null,
                                cookiesLen: document.cookie ? document.cookie.length : 0,
                                cookiesHasOSID: document.cookie.includes("OSID"),
                                cookiesHasSID: document.cookie.includes("SID="),
                                hasGrecaptcha: typeof grecaptcha !== "undefined" && !!grecaptcha.enterprise,
                                innerText: (document.body ? document.body.innerText : "").substring(0, 1000),
                            }
                        };
                    }

                    // ── Inline reCAPTCHA minting ──────────────────────────────
                    // When recaptchaActionArg is set, mint the token RIGHT HERE
                    // in the same execution context that will send the request.
                    // This matches what Google Flow's Angular SPA does: mint →
                    // build payload → fetch() all in one synchronous-like flow.
                    // Previously we minted in a SEPARATE executeScript call and
                    // passed the token string back through Python, which caused
                    // Google to flag as UNUSUAL_ACTIVITY (context mismatch).
                    let finalPayloadJson = innerPayloadJson;
                    if (recaptchaActionArg) {
                        try {
                            const SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
                            const _dl = Date.now() + 10000;
                            while (Date.now() < _dl && (typeof grecaptcha === "undefined" || !grecaptcha.enterprise || !grecaptcha.enterprise.execute)) {
                                await new Promise(r => setTimeout(r, 250));
                            }
                            if (typeof grecaptcha === "undefined" || !grecaptcha.enterprise || !grecaptcha.enterprise.execute) {
                                return { status: 0, error: "grecaptcha.enterprise not loaded — cannot mint inline token" };
                            }
                            const targetAction = recaptchaActionArg || "IMAGE_GENERATION";
                            let rcToken = null;

                            // Strategy 1: Pristine
                            const hijack = window.__redone_hijack || window.__fk_hijack;
                            if (hijack && typeof hijack.pristine === "function") {
                                try {
                                    rcToken = await Promise.race([
                                        hijack.pristine(SITE_KEY, { action: targetAction }),
                                        new Promise((_, reject) => setTimeout(
                                            () => reject(new Error("pristine timeout (15s)")),
                                            15000,
                                        )),
                                    ]);
                                } catch (_) {}
                            }

                            // Strategy 2: Assign neuter
                            if (!rcToken) {
                                const _realObjectAssign = Object.assign;
                                try {
                                    Object.assign = function (target, ...sources) {
                                        for (const s of sources) {
                                            if (s && typeof s === "object" && s.action === "extension_hijack_detected") {
                                                try { s.action = targetAction; } catch (_) {}
                                            }
                                        }
                                        const res = _realObjectAssign.apply(this, [target, ...sources]);
                                        if (res && typeof res === "object" && res.action === "extension_hijack_detected") {
                                            try { res.action = targetAction; } catch (_) {}
                                        }
                                        return res;
                                    };
                                    await new Promise(r => grecaptcha.enterprise.ready(r));
                                    rcToken = await Promise.race([
                                        grecaptcha.enterprise.execute(SITE_KEY, { action: targetAction }),
                                        new Promise((_, reject) => setTimeout(
                                            () => reject(new Error("reCAPTCHA inline mint timeout (15s)")),
                                            15000,
                                        )),
                                    ]);
                                } finally {
                                    Object.assign = _realObjectAssign;
                                }
                            }
                            console.log("[RedOne BOQ] Inline reCAPTCHA minted:", (rcToken || "").substring(0, 30) + "...");
                            // Replace placeholder with real token (keeps JSON valid)
                            finalPayloadJson = innerPayloadJson.replaceAll(
                                '"__MINT_RECAPTCHA__"',
                                JSON.stringify(rcToken),
                            );
                            if (finalPayloadJson.includes("__MINT_RECAPTCHA__")) {
                                finalPayloadJson = finalPayloadJson.replaceAll("__MINT_RECAPTCHA__", rcToken);
                            }
                            console.log("[RedOne BOQ] Placeholder replaced successfully, token len:", rcToken.length);
                        } catch (rcErr) {
                            return { status: 0, error: "inline reCAPTCHA failed: " + (rcErr.message || String(rcErr)) };
                        }
                    }

                    // 1) Read WIZ_global_data for CSRF token + build label
                    // SNlM0e = CSRF/XSRF token (at= param), FdrFJe = session ID (f.sid param)
                    const wgd = window.WIZ_global_data || {};
                    const atToken = wgd.SNlM0e || "";
                    const buildLabel = wgd.cfb2h || "";
                    const fSid = wgd.FdrFJe || "-1";
                    if (!atToken) {
                        return { status: 0, error: "WIZ_global_data.SNlM0e (CSRF token) not found — page not fully loaded?" };
                    }

                    // 2) Build f.req in BOQ format (uses finalPayloadJson which has real token if minted inline)
                    const fReq = JSON.stringify([[[rpcIdArg, finalPayloadJson, null, "generic"]]]);
                    const body = new URLSearchParams();
                    body.set("f.req", fReq);
                    body.set("at", atToken);

                    // DEBUG: log what we're sending
                    console.log("[RedOne BOQ] rpcId:", rpcIdArg, recaptchaActionArg ? "(inline reCAPTCHA)" : "(no reCAPTCHA)");
                    console.log("[RedOne BOQ] payload (first 300):", finalPayloadJson.substring(0, 300));
                    console.log("[RedOne BOQ] at:", atToken.substring(0, 30) + "...");
                    console.log("[RedOne BOQ] buildLabel:", buildLabel);

                    // 3) Build URL (standard BOQ batchexecute path — cookies automatically handled by browser in MAIN world)
                    // Support multi-login /u/N/ prefix (same as HAR capture)
                    const authUserMatch = (window.location.pathname || "").match(/^\/u\/(\d+)/);
                    const basePath = authUserMatch ? `/u/${authUserMatch[1]}/_/AiSandboxAngularFrontend/data/batchexecute` : `/_/AiSandboxAngularFrontend/data/batchexecute`;
                    const pageHl = document.documentElement.lang || (navigator.language && navigator.language.startsWith("vi") ? "vi" : "en") || "vi";
                    const url = `${basePath}?rpcids=${encodeURIComponent(rpcIdArg)}&source-path=${encodeURIComponent(sourcePathArg)}&bl=${encodeURIComponent(buildLabel)}&f.sid=${encodeURIComponent(fSid)}&hl=${encodeURIComponent(pageHl)}&_reqid=${Math.floor(Math.random() * 900000) + 100000}&rt=c`;

                    // 4) POST (same-origin, browser attaches cookies automatically)
                    const ac = new AbortController();
                    const timer = setTimeout(() => ac.abort(), timeoutMsArg);
                    const res = await fetch(url, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                            "X-Same-Domain": "1",
                            "x-browser-channel": "stable",
                            "x-browser-copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
                            "x-browser-validation": "gatHVmB+6BMZgeBsxyVmI0c15LA=",
                            "x-browser-year": "2026",
                        },
                        body: body.toString(),
                        signal: ac.signal,
                    });
                    clearTimeout(timer);
                    const status = res.status;
                    const rawText = await res.text();

                    // 5) Parse BOQ response format:
                    //    )]}'\n\n<length>\n<JSON array>\n<length>\n<JSON array>\n...
                    if (status !== 200) {
                        return { status, error: `batchexecute HTTP ${status}`, body_text: rawText.substring(0, 500) };
                    }
                    // 5) Parse chunks: Google batchexecute responses consist of length lines
                    // followed by single-line JSON arrays. Parsing by line avoids character/byte
                    // offset desynchronization issues with CRLF vs LF.
                    const chunks = [];
                    const lines = rawText.split('\n');
                    for (let line of lines) {
                        line = line.trim();
                        if (!line || line.startsWith(")]}'") || /^\d+$/.test(line)) {
                            continue;
                        }
                        try {
                            chunks.push(JSON.parse(line));
                        } catch (_) { }
                    }

                    // 6) Extract the RPC result
                    //    Format: [["wrb.fr", rpcId, "<inner JSON string>", ...], ...]
                    //    When error: entry[2] = null, entry[5] = [errorCode]
                    let rpcResult = null;
                    let rpcError = null;
                    for (const chunk of chunks) {
                        if (!Array.isArray(chunk)) continue;
                        for (const entry of chunk) {
                            if (Array.isArray(entry) && entry[0] === "wrb.fr" && entry[1] === rpcIdArg) {
                                if (entry[2] != null) {
                                    // Success — entry[2] is a JSON string with the actual result
                                    try {
                                        rpcResult = JSON.parse(entry[2]);
                                    } catch (_) {
                                        rpcResult = entry[2];
                                    }
                                } else {
                                    // Error — entry[5] may contain error code array (e.g. [13])
                                    rpcError = entry[5] || "unknown RPC error (entry[2]=null)";
                                }
                                break;
                            }
                        }
                        if (rpcResult !== null || rpcError !== null) break;
                    }

                    console.log(`[RedOne BOQ] Parsed ${chunks.length} chunks. rpcResult:`, rpcResult ? "OK" : "NULL", "rpcError:", rpcError);
                const projectMatch = (window.location.pathname || "").match(/\/project\/([a-zA-Z0-9_-]{36})/);
                    const activeProjectId = projectMatch ? projectMatch[1] : null;

                    if (rpcError) {
                        return { status: 200, error: "RPC error: " + JSON.stringify(rpcError), rpc_result: null, chunks, active_project_id: activeProjectId, final_payload_preview: finalPayloadJson.substring(0, 300) };
                    }
                    return { status: 200, rpc_result: rpcResult, chunks, active_project_id: activeProjectId };
                } catch (err) {
                    return { status: 0, error: "batchexecute: " + (err.message || String(err)) };
                }
            },
            args: [rpcId, JSON.stringify(innerPayload), sourcePath, timeoutMs, recaptchaAction],
        });
        return (results && results[0] && results[0].result) || { status: 0, error: "no script result" };
    } catch (e) {
        return { status: 0, error: "executeScript batch_execute: " + String(e) };
    }
}


// Helper: check if tab is on a Google Flow "Project not found" / error screen
async function _checkIfTabHasProjectNotFound(tabId) {
    try {
        const res = await chrome.scripting.executeScript({
            target: { tabId },
            world: "MAIN",
            func: () => {
                const text = (document.body ? document.body.innerText : "").toLowerCase();
                const title = (document.title || "").toLowerCase();
                const errSignals = [
                    "không tìm thấy dự án",
                    "project not found",
                    "dự án này không tồn tại",
                    "this project does not exist",
                    "bạn không có quyền truy cập",
                    "you don't have access",
                    "you do not have access",
                    "you don't have permission"
                ];
                return errSignals.some(s => text.includes(s) || title.includes(s));
            }
        });
        return Boolean(res && res[0] && res[0].result);
    } catch (_) {
        return false;
    }
}

// ── Task: init_flow_project (ensure a project is active in Flow) ──────
async function _doInitFlowProjectTask(task) {
    const tab = await _findLabsTab();
    if (!tab) return { error: "no flow.google.com tab" };

    const currentUrl = tab.url || tab.pendingUrl || "";

    // A) If tab is ALREADY on ANY project page (e.g. /project/<uuid>):
    // NEVER navigate, reload, or change the URL! Matches G-Labs Studio architecture.
    const match = currentUrl.match(/\/project\/([a-zA-Z0-9_-]{36})/);
    if (match && match[1]) {
        console.log(`[RedOne] Preserving user's active Flow project: ${match[1]}`);
        return { project_id: match[1], status: "already_open" };
    }

    // B) If tab is on Flow but not on a specific project page, inspect DOM for an existing project link
    try {
        const domResult = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: "MAIN",
            func: () => {
                const projLinks = Array.from(document.querySelectorAll('a[href*="/project/"]'));
                for (const a of projLinks) {
                    const m = (a.getAttribute("href") || "").match(/\/project\/([a-zA-Z0-9_-]{36})/);
                    if (m && m[1]) return m[1];
                }
                return null;
            },
        });
        const foundId = (domResult && domResult[0] && domResult[0].result) || null;
        if (foundId) {
            console.log(`[RedOne] Detected existing project from page: ${foundId}`);
            return { project_id: foundId, status: "found_existing" };
        }
    } catch (err) {
        console.warn("[RedOne] DOM inspect project error:", err);
    }

    return { error: "Không tìm thấy dự án trong tab flow.google.com. Vui lòng mở hoặc chọn một dự án trong tab Chrome." };
}


// ── Poll loop ────────────────────────────────────────────────────────

// ── Server-driven session commands ──────────────────────────────────
// Backend pushes these to reset session state when 403s cascade.
// Mirrors G-Labs' _applyThemeUpdates command system.
async function _executeSessionCommand(cmd) {
    const command = cmd.cmd || cmd.command;
    const params = cmd.params || {};
    try {
        if (command === "reload_extension") {
            console.log("[Extension] Reloading extension on command...");
            chrome.runtime.reload();
            return;
        } else if (command === "reload_tab") {
            const tab = await _findLabsTab();
            if (tab && tab.id) {
                console.log("[Extension] Reloading labs/flow tab on command...");
                chrome.tabs.reload(tab.id);
            }
            return;
        } else if (command === "set_target_email") {
            _targetGoogleEmail = params.email || null;
            chrome.storage.local.set({ targetGoogleEmail: _targetGoogleEmail });
            console.log(`[Extension] Set target Google login email: ${_targetGoogleEmail}`);
        } else if (command === "clear_cookies") {
            // Clear ALL labs.google + flow.google.com cookies → force session re-login
            for (const domain of ["labs.google", "flow.google.com"]) {
                const cookies = await chrome.cookies.getAll({ domain });
                for (const c of cookies) {
                    const url = `https://${c.domain.replace(/^\./, "")}${c.path}`;
                    await chrome.cookies.remove({ url, name: c.name });
                }
            }
        } else if (command === "delay") {
            const ms = params.ms || 1000;
            await new Promise(r => setTimeout(r, ms));
        }
    } catch (_) { /* best effort — commands are advisory, not critical */ }
}

// Execute ONE task to completion and post its result. Runs detached from
// the poll loop (we don't await it there) so multiple tasks can be in
// flight at once. Always decrements _inFlight via the caller's .finally().
async function _runTask(task) {
    let result = null;
    try {
        if (task.kind === "recaptcha") {
            result = await _doRecaptchaTask(task);
        } else if (task.kind === "proxy_fetch") {
            result = await _doProxyFetchTask(task);
        } else if (task.kind === "get_cookies") {
            result = await _doGetCookiesTask(task);
        } else if (task.kind === "batch_execute") {
            result = await _doBatchExecuteTask(task);
        } else if (task.kind === "init_flow_project") {
            result = await _doInitFlowProjectTask(task);
        } else {
            result = { error: `unknown task kind: ${task.kind}` };
        }
    } catch (e) {
        // Never let a thrown task kill the loop — report it as a result so
        // the awaiting backend future resolves instead of timing out.
        result = { error: String((e && e.message) || e) };
    }
    try {
        await _bridgePost(task.sourceHost || BRIDGE_HOSTS[0], "/sync/task-result", {
            task_id: task.id,
            kind: task.kind,
            result,
        });
    } catch (_) { /* best effort */ }
}

async function _pollLoop() {
    if (_polling) return;
    _polling = true;
    while (_polling) {
        let claimed = false;
        try {
            // Tell the backend what we currently can/can't do so it can
            // surface meaningful errors instead of hanging.
            const tab = await _findLabsTab();
            const signedIn = await _isSignedIn();
            const status = !tab ? "no_tab" : !signedIn ? "no_login" : "ready";
            const tabEmail = (tab && status === "ready") ? await _detectFlowUserEmail(tab) : "";
            const accInfo = (tab && status === "ready") ? await _detectFlowAccountDetails(tab) : {};

            // How many more tasks we can take right now. When 0 we still
            // poll (to keep tab_status fresh) but the backend hands nothing.
            const capacity = Math.max(0, MAX_CONCURRENT - _inFlight);

            const params = new URLSearchParams({
                tab_status: status,
                tab_url: tab && tab.url ? tab.url : "",
                tab_email: accInfo.email || tabEmail || "",
                tab_tier: accInfo.tier || "FREE",
                tab_credits: accInfo.credits != null ? String(accInfo.credits) : "",
                capacity: String(capacity),
            }).toString();

            let data = null;
            let sourceHost = null;
            let backendReachable = false;
            for (const host of BRIDGE_HOSTS) {
                try {
                    const resData = await _bridgeGet(host, `/sync/next-task?${params}`);
                    backendReachable = true;  // got a response → backend is alive
                    if (!data) data = resData; // keep first valid response (for session_commands)
                    if (resData && resData.task) {
                        data = resData;
                        sourceHost = host;
                        break;
                    }
                    break;  // got valid response, no need to try other hosts
                } catch (_) { }
            }
            _connected = backendReachable;

            // ── Server-driven session commands ──────────────────
            // Execute commands piggybacked on the poll response BEFORE
            // processing any task. This mirrors G-Labs' _applyThemeUpdates.
            if (data && data.session_commands && Array.isArray(data.session_commands)) {
                for (const cmd of data.session_commands) {
                    await _executeSessionCommand(cmd);
                }
            }

            const task = data && data.task;
            if (task && task.id && task.kind) {
                task.sourceHost = sourceHost;
                claimed = true;
                _inFlight++;
                // Fire WITHOUT awaiting → the loop is free to claim more
                // tasks (up to MAX_CONCURRENT). This is what makes the
                // "luồng song song" setting actually run in parallel.
                _runTask(task).finally(() => {
                    _inFlight = Math.max(0, _inFlight - 1);
                });
            }
        } catch (_) {
            _connected = false;
        }
        // Just grabbed one and still have room → poll again right away to
        // fan out the rest of the batch; otherwise idle the normal interval.
        const moreRoom = _inFlight < MAX_CONCURRENT;
        await _delay(claimed && moreRoom ? FAST_POLL_MS : POLL_INTERVAL_MS);
    }
}

function _delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}


// ── Popup / content script message handlers ──────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "LABS_TAB_READY") {
        if (_sender && _sender.tab && _sender.tab.id) {
            try {
                chrome.tabs.update(_sender.tab.id, { autoDiscardable: false }).catch(() => {});
            } catch (_) {}
        }
        if (!_polling) _pollLoop();
        sendResponse({ ok: true });
        return true;
    }
    if (msg && msg.type === "GET_FLOW_ACCOUNT") {
        (async () => {
            try {
                const tab = await _findLabsTab();
                let acc = { email: null, tier: "FREE", credits: null };
                if (tab) {
                    acc = await _detectFlowAccountDetails(tab);
                }
                const finalEmail = acc.email || _lastDetectedAccount.email || null;
                const finalTier = acc.tier || _lastDetectedAccount.tier || "FREE";
                const finalCredits = acc.credits != null ? acc.credits : _lastDetectedAccount.credits;
                sendResponse({
                    ok: true,
                    hasTab: !!tab,
                    email: finalEmail,
                    tier: finalTier,
                    credits: finalCredits,
                });
            } catch (err) {
                sendResponse({ ok: false, error: String(err) });
            }
        })();
        return true;
    }
    if (msg && msg.type === "GET_METRICS") {
        (async () => {
            // Active probe — re-verify backend reachability before
            // reporting status. Without this, a freshly-woken service
            // worker would still have `_connected = false` (its initial
            // value) and the popup would falsely show "Mất kết nối" for
            // up to one poll interval (~1.5s) after each wake.
            try {
                _connected = false;
                for (const h of BRIDGE_HOSTS) {
                    try {
                        const r = await fetch(`${h}/sync/status`, {
                            signal: AbortSignal.timeout(3000),
                        });
                        if (r.ok) {
                            _connected = true;
                            break;
                        }
                    } catch (_) { }
                }
            } catch (_) {
                _connected = false;
            }
            // While we have the SW awake, also ensure the poll loop is
            // running. After a SW restart, the poll alarm doesn't fire
            // for up to 30s — kicking off pollLoop here closes that gap.
            if (!_polling) _pollLoop();

            const tab = await _findLabsTab();
            const signedIn = await _isSignedIn();
            const shakkerTab = await _findShakkerTab();
            sendResponse({
                connected: _connected,
                tokenCount: _tokenCount,
                lastSuccessAt: _lastSuccessAt,
                hasTab: !!tab,
                signedIn,
                tabUrl: tab ? tab.url : null,
                // Shakker bridge status — separate channel, independent of Flow.
                shakker: {
                    hasTab: !!shakkerTab,
                    tabUrl: shakkerTab ? shakkerTab.url : null,
                    email: _shakkerEmail,
                    lastSync: _shakkerLastSync,
                },
            });
        })();
        return true;
    }
    if (msg && msg.type === "RESET_METRICS") {
        _tokenCount = 0;
        _lastSuccessAt = null;
        chrome.storage.local.set({ tokenCount: 0, lastSuccessAt: null });
        sendResponse({ ok: true });
        return true;
    }
    if (msg && msg.type === "SHAKKER_SYNC") {
        // Forwarded from content_shakker.js whenever the user has shakker.ai
        // open. Payload is the slim {user_uuid, token, email, user_id,
        // account_id, webid} object — we forward straight to the bridge.
        //
        // Posted to /sync/shakker-account (NOT /api/shakker-accounts/sync)
        // so the request bypasses the OAuth auth gate — same trust model
        // as the Flow bridge protocol (local-only origin, no auth).
        (async () => {
            try {
                let r = null;
                for (const host of BRIDGE_HOSTS) {
                    try {
                        r = await _bridgePost(host, "/sync/shakker-account", msg.state || {});
                        if (r && r.ok) break;
                    } catch (_) { }
                }
                if (r && r.ok) {
                    _shakkerEmail = (msg.state && msg.state.email) || _shakkerEmail;
                    _shakkerLastSync = Date.now();
                    chrome.storage.local.set({
                        shakkerEmail: _shakkerEmail,
                        shakkerLastSync: _shakkerLastSync,
                    });
                }
                sendResponse({ ok: !!(r && r.ok), result: r });
            } catch (e) {
                sendResponse({ ok: false, error: String(e) });
            }
        })();
        return true;
    }
    // ── Shared Google account auto-login ─────────────────────────────
    if (msg && msg.type === "LOGIN_SHARED_GOOGLE") {
        // Member clicked "Đăng nhập tài khoản chung": fetch the shared account
        // from the backend (which got it from the Hub), stash it briefly, and
        // open the Google login page — content_accounts.js fills it in.
        (async () => {
            try {
                const r = await _bridgeGet("/sync/shared-google");
                if (!r || !r.ok || !r.email || !r.password) {
                    sendResponse({ ok: false, error: (r && r.reason) || "Chưa có tài khoản chung" });
                    return;
                }
                _pendingGoogleLogin = { email: r.email, password: r.password, ts: Date.now() };
                await chrome.tabs.create({
                    url: "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fflow.google.com",
                });
                sendResponse({ ok: true });
            } catch (e) {
                sendResponse({ ok: false, error: String(e) });
            }
        })();
        return true;
    }
    if (msg && msg.type === "GET_GOOGLE_AUTOFILL") {
        const p = _pendingGoogleLogin;
        if (p && (Date.now() - p.ts) < 180000) {
            sendResponse({ ok: true, email: p.email, password: p.password });
        } else {
            chrome.storage.local.get(["targetGoogleEmail"], (data) => {
                const email = data.targetGoogleEmail || _targetGoogleEmail;
                if (email) {
                    sendResponse({ ok: true, email: email, password: null });
                } else {
                    sendResponse({ ok: false });
                }
            });
        }
        return true; // Keep channel open for async response
    }
    if (msg && msg.type === "GOOGLE_AUTOFILL_DONE") {
        _pendingGoogleLogin = null;
        _targetGoogleEmail = null;
        chrome.storage.local.remove("targetGoogleEmail");
        sendResponse({ ok: true });
        return true;
    }
    return false;
});

// ── Initial poll on every service worker wake ─────────────────────────
// MV3 service workers go idle when not processing events. Each wake-up
// re-evaluates this file from the top — kick off the poll loop here so
// the connection is restored immediately, rather than waiting up to 30s
// for the first alarm tick. The `if (_polling) return` guard inside
// _pollLoop prevents duplicate loops.
_pollLoop();
