// Minimal content script — injected into every labs.google and flow.google.com tab.
//
// Signals "page loaded" so background.js can re-poll quickly when a labs/flow tab
// finishes navigating, and marks the tab as autoDiscardable: false via Chrome Tabs API.

try {
    chrome.runtime.sendMessage({ type: "LABS_TAB_READY", url: location.href });
} catch (_) {
    // Extension may not be fully initialized or background waking up
}
