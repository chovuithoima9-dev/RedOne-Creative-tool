// RedOne Auth Helper — Tool UI Content Script
// Injected into http://localhost/* and http://127.0.0.1/*
// Automatically bridges the active Google Flow account of this specific Chrome profile into the web UI.

(function () {
    let _lastReportedEmail = null;

    function queryFlowAccount() {
        if (!chrome.runtime || !chrome.runtime.sendMessage) return;
        try {
            chrome.runtime.sendMessage({ type: "GET_FLOW_ACCOUNT" }, (res) => {
                if (chrome.runtime.lastError || !res || !res.ok) return;

                // Post message into page context (window) so frontend (app.js / pages) can read it
                window.postMessage({
                    source: "REDONE_AUTH_HELPER",
                    type: "FLOW_ACCOUNT_DETECTED",
                    data: {
                        email: res.email || null,
                        tier: res.tier || "FREE",
                        credits: res.credits != null ? res.credits : null,
                        hasTab: !!res.hasTab,
                    }
                }, "*");

                if (res.email && res.email !== _lastReportedEmail) {
                    _lastReportedEmail = res.email;
                    console.log(`[RedOne Extension] Profile Flow account detected: ${res.email} (${res.tier})`);
                }
            });
        } catch (_) {}
    }

    // Query immediately on load
    queryFlowAccount();

    // Query periodically every 4 seconds to sync account switches or login changes
    setInterval(queryFlowAccount, 4000);

    // Listen for manual request from page
    window.addEventListener("message", (ev) => {
        if (ev.data && ev.data.type === "REDONE_REQUEST_FLOW_ACCOUNT") {
            queryFlowAccount();
        }
    });
})();
