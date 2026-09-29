// App entry — router + state + global UI wiring
//
// Cache-busting strategy:
//   The HTML <script> tag has ?b=<BUILD_ID>, which changes every server restart.
//   Combined with the server's `Cache-Control: no-store` header on /static,
//   F5 always pulls fresh JS. All sub-imports use canonical URLs (no query
//   param) so the ES module registry stores a SINGLE instance per file —
//   crucial for tasks_store.js which holds shared in-memory state.

import { api } from './api.js';
import { ws } from './ws.js';
import { $, $$, el, clear, toast, icon } from './ui.js';
// Side-effect import: wires WS → global tasks store BEFORE page modules import it.
import './tasks_store.js';

import { renderContent } from './pages/content.js';
import { renderImage } from './pages/image.js';
import { renderStoryboard } from './pages/storyboard.js';
import { renderShakker } from './pages/shakker.js';
import { renderLongVideo } from './pages/long_video.js';
import { renderYoutube } from './pages/youtube.js';
import { renderScript } from './pages/script.js';
import { renderImagePrompt } from './pages/image_prompt.js';
import { renderVideoWatermark } from './pages/video_watermark.js';
import { renderBatchResize } from './pages/batch_resize.js';
import { renderBatchColor } from './pages/batch_color.js';
import { renderSubtitle } from './pages/subtitle.js';
import { renderAccounts } from './pages/accounts.js';
import { renderSettings } from './pages/settings.js';
import { renderTasksManager } from './pages/tasks_manager.js';
import { renderTeam } from './pages/team.js';
import { renderAdmin } from './pages/admin.js';
import { renderUserTracking } from './pages/user_tracking.js';
import { renderFeatureStore } from './pages/feature_store.js';
import { renderHGStock } from './pages/hgstock.js';
import { loadCatalog, getFeature, getCatalog } from './features/catalog.js';
import * as featureState from './features/state.js';

// Build-id banner so you can verify which build is loaded
try {
  const BUST = new URL(import.meta.url).searchParams.get('b') || '?';
  console.log(`%c[RedOne] build=${BUST} loaded`, 'color:#dc2626;font-weight:600');
} catch {}

const PAGES = {
  'content':       { title: 'Tạo Video',         subtitle: 'Tạo video AI từ prompt (Text-to-Video / Image-to-Video)', render: renderContent },
  'image':         { title: 'Tạo Ảnh',           subtitle: 'Sinh ảnh AI bằng Nano Banana / Imagen qua Google Labs',  render: renderImage },
  'storyboard':    { title: 'Tạo Storyboard',    subtitle: 'Ý tưởng + ảnh tham chiếu → kịch bản phân cảnh (prompt + ảnh) → gửi sang Tạo Video I2V', render: renderStoryboard },
  'shakker':       { title: 'Ảnh Shakker',       subtitle: 'Sinh ảnh hàng loạt qua Shakker.ai — model + LoRA + ảnh tham chiếu', render: renderShakker },
  'long-video':    { title: 'Video Dài',         subtitle: 'Ghép N cảnh thành video dài liên tục bằng Extend API', render: renderLongVideo },
  'youtube':       { title: 'YouTube → Prompt',  subtitle: 'Phân tích YouTube / TikTok thành storyboard',            render: renderYoutube },
  'script':        { title: 'Ý Tưởng → Prompt',  subtitle: 'Từ kịch bản tiếng Việt sang storyboard prompt — gửi sang Tạo Video hoặc Tạo Ảnh', render: renderScript },
  'image-prompt':  { title: 'Ảnh → Prompt',      subtitle: 'Phân tích ảnh và sinh prompt cho video',                 render: renderImagePrompt },
  'batch-resize':  { title: 'Resize Hàng Loạt',  subtitle: 'Đổi kích thước nhiều ảnh theo preset',                   render: renderBatchResize },
  'batch-color':   { title: 'Chỉnh màu hàng loạt', subtitle: 'Áp một thiết lập màu cho nhiều ảnh và video — xem trước đúng màu file xuất', render: renderBatchColor },
  'subtitle':      { title: 'Tạo Phụ Đề',        subtitle: 'Sinh phụ đề SRT từ video bằng Whisper',                  render: renderSubtitle },
  'video-watermark': { title: 'Xóa Watermark Video', subtitle: 'Xóa logo Veo / watermark khỏi nhiều video một lúc', render: renderVideoWatermark },
  'tasks':         { title: 'Quản lý Task',      subtitle: 'Theo dõi tiến độ + hàng đợi tất cả tác vụ',              render: renderTasksManager },
  'accounts':      { title: 'Tài Khoản',         subtitle: 'Quản lý Google account & credits',                       render: renderAccounts },
  'settings':      { title: 'Cài Đặt',           subtitle: 'API keys, thư mục, tùy chọn',                            render: renderSettings },
  'feature-store': { title: 'Kho tính năng',     subtitle: 'Cài thêm tính năng nặng khi cần — máy yếu có thể bỏ qua để nhẹ hơn', render: renderFeatureStore },
  'hgstock':       { title: 'HG Stock',          subtitle: 'Upload ảnh/video/audio lên HG Stock — gắn tag, chọn dự án',       render: renderHGStock },
  'team':          { title: 'Team',              subtitle: 'Theo dõi task & credit của thành viên dưới quyền',       render: renderTeam },
  'admin':         { title: 'Quản trị',          subtitle: 'Người dùng, nhóm, hạn mức credit nội bộ (Hub)',          render: renderAdmin },
  'user-tracking': { title: 'Tracking',           subtitle: 'Theo dõi hành vi sử dụng tool: ảnh, video, kịch bản, thời lượng (Firebase)', render: renderUserTracking },
};

export const store = {
  accounts: [],
  settings: {},
  app: { name: 'RedOne Creative', version: '1.0.0' },
  totalCredits: 0,
};

export async function navigate(pageId, opts = {}) {
  // Static page first; otherwise an installed Kho feature (lazy-loaded).
  const staticPage = PAGES[pageId];
  const feature = !staticPage ? getFeature(pageId) : null;
  const page = staticPage || (feature ? featureToPage(feature) : null);
  if (!page) return;
  // Optional deep-link to a specific task — pages that show task gallery
  // (content, image, long-video) consume this on mount, then clear it.
  if (opts.taskId != null) {
    window.__app._pendingTaskId = opts.taskId;
  }
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === pageId));
  $('#page-title').textContent = page.title;
  $('#page-subtitle').textContent = page.subtitle;
  const container = $('#page-container');
  clear(container);
  container.classList.add('fade-in');
  setTimeout(() => container.classList.remove('fade-in'), 350);
  history.replaceState(null, '', `#${pageId}`);

  try {
    if (page.loader) {
      // Lazy feature (Kho tính năng): import its module on demand so heavy
      // code/assets (e.g. fabric.js) only load when the user opens it.
      const token = (navigate._t = (navigate._t || 0) + 1);
      container.appendChild(el('div', {
        class: 'card', style: { textAlign: 'center', color: 'var(--text-muted)' },
      }, el('span', { class: 'spinner' }), ' Đang tải tính năng…'));
      let mod;
      try {
        mod = await page.loader();
      } catch (e) {
        if (navigate._t !== token) return;
        clear(container);
        container.appendChild(el('div', { class: 'card' },
          `Không tải được tính năng: ${e.message}. Thử mở lại từ "Kho tính năng".`));
        return;
      }
      if (navigate._t !== token) return;   // user navigated away mid-load
      clear(container);
      const fn = mod[page.renderName] || mod.render || mod.default;
      if (typeof fn !== 'function') throw new Error('Module tính năng không có hàm render');
      fn(container);
    } else {
      page.render(container);
    }
  } catch (e) {
    console.error('Page render error:', e);
    clear(container);
    container.appendChild(el('div', { class: 'card' }, `Lỗi render trang: ${e.message}`));
  }

  // Eye-icon deep-link (Tasks Manager) → scroll to results gallery. Static
  // pages only; features have no task gallery.
  if (opts.taskId != null && staticPage) {
    const RESULTS_SEL = {
      content: '#cnt-results',
      image: '#img-results',
      storyboard: '#sb2-results',
      'long-video': '#lv-progress',
      shakker: '#shk-results',
    };
    const sel = RESULTS_SEL[pageId];
    if (sel) {
      requestAnimationFrame(() => {
        const target = document.querySelector(sel);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
  }
}

// ── Kho tính năng — lazy routing + dynamic sidebar tabs ───────────────
// Map a catalog feature → a page-like object with an async module loader.
function featureToPage(f) {
  if (!featureState.isInstalled(f)) return null;
  if (f.kind === 'builtin' || f.kind === 'asset') {
    // builtin: code ships in the app; asset: binary downloaded, but tab JS is in pages/
    const mod = f.module || f.id;
    return {
      title: f.name, subtitle: f.description || '',
      loader: () => import(`./pages/${mod}.js`),
      renderName: f.renderName || 'render',
    };
  }
  // frontend (downloaded bundle under /addons/<id>/)
  const entry = (f.download && f.download.entry) || 'index.js';
  return {
    title: f.name, subtitle: f.description || '',
    loader: () => import(`/addons/${f.id}/${entry}`),
    renderName: f.renderName || 'render',
  };
}

function _featuresList() {
  try { const c = getCatalog(); return (c && c.features) || []; } catch { return []; }
}

function findNavGroup(label) {
  if (!label) return null;
  for (const g of $$('.sidebar-nav .nav-group')) {
    const lab = $('.nav-label', g);
    if (lab && lab.textContent.trim() === label) return g;
  }
  return null;
}

// Inject sidebar tabs for installed features. Idempotent — clears prior
// feature items first, then re-adds installed ones into their declared group.
function renderFeatureNav() {
  $$('.nav-item[data-feature="1"]').forEach(n => n.remove());
  // The remote catalog still declares the old split groups ("Xử lý ảnh" /
  // "Xử lý video"), which were merged into "Xử lý media". Alias them, and fall
  // back to the media group so an installed feature never silently vanishes
  // from the sidebar when its declared group no longer exists here.
  const GROUP_ALIAS = { 'Xử lý ảnh': 'Xử lý media', 'Xử lý video': 'Xử lý media' };
  for (const f of _featuresList()) {
    if (!featureState.isInstalled(f)) continue;
    const group = findNavGroup(GROUP_ALIAS[f.group] || f.group)
      || findNavGroup('Xử lý media');
    if (!group) continue;
    const svg = icon(f.icon || 'sparkles', 20);
    svg.classList.add('ni');
    const btn = el('button', { class: 'nav-item', 'data-page': f.id, 'data-feature': '1' },
      svg, el('span', null, f.name));
    btn.addEventListener('click', () => navigate(f.id));
    group.appendChild(btn);
  }
  const cur = (location.hash || '').slice(1);
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === cur));
}

async function initFeatures() {
  try {
    await loadCatalog();
    featureState.initBuiltinDefaults();
    renderFeatureNav();
    featureState.onChange(renderFeatureNav);
    // If the boot hash points to an installed feature, render it now.
    const h = (location.hash || '').slice(1);
    if (h && getFeature(h) && featureState.isInstalled(h)) {
      navigate(h);
    } else if (!PAGES[h]) {
      // Not a valid static page nor an installed feature -> fallback to content
      navigate('content');
    }
  } catch (e) {
    console.warn('initFeatures failed:', e);
  }
}

// ── RedOne Hub (multi-user) — role + nav gating ──────────────────────
// Fetch the user's Hub role/quota (via the local proxy). Reveals the
// "Quản lý" nav group only for lead/admin. When the Hub is disabled
// (HUB_BASE_URL empty) /status returns {enabled:false} instantly → tabs
// stay hidden and a standalone build looks exactly as before.
async function refreshHubStatus() {
  window.__app = window.__app || {};
  try {
    window.__app.hub = (await api.hub.status()) || { enabled: false };
  } catch {
    window.__app.hub = { enabled: false };
  }
  applyHubNav();
  return window.__app.hub;
}

function applyHubNav() {
  const role = (window.__app && window.__app.hub && window.__app.hub.role) || null;
  const group = document.getElementById('nav-group-hub');
  const isManager = role === 'lead' || role === 'admin';
  if (group) group.style.display = isManager ? '' : 'none';
  $$('#nav-group-hub [data-role]').forEach(item => {
    const allowed = (item.dataset.role || '').split(/\s+/).filter(Boolean);
    item.style.display = (role && allowed.includes(role)) ? '' : 'none';
  });
}

async function refreshAccounts() {
  try {
    const data = await api.accounts.list();
    store.accounts = data.accounts || [];
    const enabledCount = store.accounts.filter(a => a.enabled).length;
    const total = store.accounts.reduce((sum, a) => sum + (a.credit || 0), 0);
    store.totalCredits = total;
    $('#topbar-account-count').textContent = enabledCount;
    $('#topbar-credits').textContent = total.toLocaleString('vi-VN');
  } catch (e) {
    console.warn('Failed to load accounts:', e.message);
  }
}

async function refreshShakkerPower() {
  // Roll up usable power across enabled Shakker accounts → topbar chip.
  try {
    const r = await api.shakkerAccounts.list();
    const accts = r.accounts || [];
    store.shakkerAccounts = accts;
    const total = accts
      .filter(a => a.enabled)
      .reduce((sum, a) => sum + (a.usable_power || 0), 0);
    store.shakkerPower = total;
    const elx = $('#topbar-shakker-power');
    if (elx) elx.textContent = total.toLocaleString('vi-VN');
  } catch (e) {
    console.warn('Failed to load Shakker power:', e && e.message);
  }
}

function setupFlowAccountSync() {
  window.addEventListener('message', (ev) => {
    if (!ev.data || ev.data.source !== 'REDONE_AUTH_HELPER') return;
    if (ev.data.type === 'FLOW_ACCOUNT_DETECTED') {
      const acc = ev.data.data || {};
      window.currentFlowAccount = acc;
      const elAccount = $('#topbar-flow-account');
      const elChip = $('#topbar-flow-account-chip');
      if (elAccount) {
        if (acc.email) {
          elAccount.textContent = acc.email;
          elAccount.title = `Flow: ${acc.email} (${acc.tier || 'FREE'})`;
          if (elChip) elChip.style.display = '';
        } else if (acc.hasTab) {
          elAccount.textContent = 'Chưa đăng nhập Flow';
          elAccount.title = 'Đã mở tab Google Flow nhưng chưa đăng nhập';
        } else {
          elAccount.textContent = 'Chưa mở tab Flow';
          elAccount.title = 'Vui lòng mở tab flow.google.com trên profile Chrome này';
        }
      }
      window.dispatchEvent(new CustomEvent('flow_account_changed', { detail: acc }));
    }
  });

  // Request immediate account update on load
  window.postMessage({ type: 'REDONE_REQUEST_FLOW_ACCOUNT' }, '*');
}

async function loadSettings() {
  try {
    const data = await api.settings.get();
    store.settings = data.settings || {};
    store.app = data.app || store.app;
    $('#app-version').textContent = store.app.version;
  } catch (e) { console.warn(e); }
}

/**
 * Auto-scan all accounts on tool login: refresh Flow credit + session status
 * so the user doesn't have to click "Check" on each account manually.
 *
 * Fire-and-forget — never blocks the UI. The backend broadcasts
 * `account_updated` per account, which the accounts page + topbar consume
 * live; we also re-run refreshAccounts() at the end to roll up the totals.
 *
 * Guards:
 *  - Skip in playwright mode: check-all there spawns a browser per account,
 *    a heavy/surprising side-effect to trigger automatically on every open.
 *  - Skip when there are no accounts (e.g. Vertex / fresh machines) — nothing
 *    to scan, and Vertex bills per-call with no credit counter anyway.
 *
 * Runs after a short delay so that, in extension mode, the Chrome extension
 * has a moment to connect before we ask it to fetch credits.
 */
function autoScanAccounts() {
  try {
    // Flow accounts (Chrome-extension bridge): refresh credit + session
    // status so the user doesn't click "Check" per account. Skip if none.
    const accounts = store.accounts || [];
    if (accounts.length) {
      setTimeout(() => {
        api.accounts.checkAll()
          .then(() => refreshAccounts())
          .catch((e) => console.warn('Auto-scan Flow accounts failed:', e && e.message));
      }, 3500);
    }
    // Shakker accounts: independent pool, refreshed via the stored token
    // (no browser). checkAll() is a harmless no-op when there are none.
    // Fired slightly later so it doesn't contend with the Flow scan.
    setTimeout(() => {
      api.shakkerAccounts.checkAll()
        .catch((e) => console.warn('Auto-scan Shakker accounts failed:', e && e.message));
    }, 4500);
  } catch (e) {
    console.warn('autoScanAccounts error:', e);
  }
}

function setupSidebar() {
  $$('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => navigate(btn.dataset.page));
  });
}

function setupTheme() {
  const KEY = 'redone_theme';
  const label = $('#theme-toggle-label');
  const btn = $('#theme-toggle');
  function apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem(KEY, theme);
    if (label) label.textContent = theme === 'dark' ? 'Dark Mode' : 'Light Mode';
  }
  const current = localStorage.getItem(KEY) || 'light';
  apply(current);
  if (btn) {
    btn.addEventListener('click', () => {
      const t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      apply(t);
    });
  }
}

// Shutdown button (topbar). Confirms before killing the backend so an
// accidental click doesn't trash an in-flight task.
function setupAuthUI() {
  // Inject a tiny "logged in as X — logout" chip into the topbar actions
  // row so users can see who they are + sign out. Skipped silently if
  // there's no user info (offline boot, OAuth not configured, etc).
  const user = window.__app && window.__app.user;
  if (!user || !user.email) return;
  const actions = $('.topbar-actions') || $('.topbar-right');
  if (!actions) return;
  if ($('#auth-chip')) return;   // already injected

  const chip = document.createElement('div');
  chip.id = 'auth-chip';
  chip.style.cssText =
    'display:flex;align-items:center;gap:6px;padding:4px 8px 4px 4px;' +
    'background:rgba(255,255,255,0.04);border:1px solid var(--border);' +
    'border-radius:18px;font-size:11.5px;color:var(--text-muted);' +
    'cursor:pointer;transition:background 0.15s;margin-right:6px;';
  chip.title = `Đăng nhập: ${user.email}\nClick để đăng xuất`;
  chip.onmouseover = () => chip.style.background = 'rgba(255,255,255,0.08)';
  chip.onmouseout = () => chip.style.background = 'rgba(255,255,255,0.04)';

  if (user.picture) {
    const img = document.createElement('img');
    img.src = user.picture;
    img.style.cssText = 'width:22px;height:22px;border-radius:50%;';
    chip.appendChild(img);
  }
  const label = document.createElement('span');
  // Show just the local part of the email to save space, full email in tooltip
  label.textContent = (user.email || '').split('@')[0];
  chip.appendChild(label);

  chip.addEventListener('click', async () => {
    const { confirm } = await import('./ui.js');
    if (!await confirm(
      `Đăng xuất khỏi RedOne Creative?\nTài khoản: ${user.email}`,
      'Xác nhận đăng xuất',
    )) return;
    try {
      await fetch('/auth/logout', { method: 'POST' });
    } catch (e) { /* ignore */ }
    window.location.replace('/login.html');
  });

  // Insert before the theme toggle / shutdown buttons so it appears
  // on the left side of the action cluster.
  actions.insertBefore(chip, actions.firstChild);
}


// ── GitHub help links (guide + feedback) ─────────────────────────────
// The repo is single-sourced in backend config. Seed with the known URL so
// the buttons work even before the fetch resolves, then refresh from
// /api/system/info (a repo rename only needs a backend change).
const _GH_FALLBACK = 'https://github.com/kiennt-bit/RedOne-Creative-tool';
let GH = {
  guide: `${_GH_FALLBACK}/blob/main/docs/HUONG_DAN_SU_DUNG.md`,
  feedback: `${_GH_FALLBACK}/issues`,   // → Google Form khi FEEDBACK_FORM_URL được set
};
async function loadGithubLinks() {
  try {
    const info = await api.system.info();
    const base = (info && info.github_url) || _GH_FALLBACK;
    GH = {
      guide: `${base}/blob/main/docs/HUONG_DAN_SU_DUNG.md`,
      // Feedback ưu tiên Google Form (cấu hình ở backend); fallback GitHub Issues.
      feedback: (info && info.feedback_url) ? info.feedback_url : `${base}/issues`,
    };
  } catch (e) { /* keep fallback */ }
}
function setupHelpLinks() {
  const help = $('#help-btn');
  if (help) help.addEventListener('click', () => window.open(GH.guide, '_blank', 'noopener'));
  const fb = $('#feedback-btn');
  if (fb) fb.addEventListener('click', () => window.open(GH.feedback, '_blank', 'noopener'));
}

// First-run / no-extension reminder. The tool can't generate anything until
// the "RedOne Auth Helper" Chrome extension is installed — its background
// worker polls /sync/* which is what flips extension_live true. If the bridge
// reports no live extension shortly after load, pop a guide link. Self-
// resolving: once the extension connects, it never shows again.
async function maybeShowExtensionReminder() {
  // Give the extension a few seconds to poll after a fresh load before we
  // decide it's missing (avoids a false alarm on a slow start).
  await new Promise(r => setTimeout(r, 4000));
  let live = false;
  try {
    const st = await fetch('/sync/state').then(r => r.json());
    live = !!(st && st.extension_live);
  } catch (e) { /* treat as not live */ }
  if (live) return;
  const { modal } = await import('./ui.js');
  // Inline monospace pill for the literal terms users must type/click.
  const codeSpan = (t) => el('code', {
    style: {
      fontFamily: 'var(--font-mono)', background: 'var(--brand-soft)',
      color: 'var(--brand-2)', padding: '1px 6px', borderRadius: '5px', fontSize: '12px',
    },
  }, t);
  modal({
    title: 'Cần cài Extension để dùng tool',
    body: el('div', null,
      el('p', { style: { margin: '0 0 12px' } },
        'Chưa phát hiện extension "RedOne Auth Helper". RedOne Creative cần extension này '
        + '(chạy trong Chrome của bạn) để tạo ảnh / video.'),
      // Highlighted, step-by-step quick-install box.
      el('div', {
        style: {
          background: 'var(--brand-tint)', borderLeft: '3px solid var(--brand)',
          borderRadius: 'var(--r-sm)', padding: '12px 16px',
        },
      },
        el('div', {
          style: { fontWeight: '700', color: 'var(--brand)', marginBottom: '10px', fontSize: '13.5px' },
        }, '⚡ Cài nhanh extension'),
        el('ol', { style: { margin: '0', paddingLeft: '20px', lineHeight: '1.9', fontSize: '13px' } },
          el('li', null, 'Mở ', codeSpan('chrome://extensions')),
          el('li', null, 'Bật ', el('b', null, 'Chế độ dành cho nhà phát triển'), ' (góc trên bên phải)'),
          el('li', null, 'Bấm ', el('b', null, 'Tải tiện ích đã giải nén'), ' (Load unpacked)'),
          el('li', null, 'Chọn thư mục ', codeSpan('extension/'), ' trong thư mục cài tool'),
          el('li', null, 'Mở một tab ', codeSpan('flow.google.com'), ' và đăng nhập Google'),
        ),
      ),
    ),
    actions: [
      { label: 'Mở hướng dẫn sử dụng', class: 'btn-primary',
        onclick: () => { window.open(GH.guide, '_blank', 'noopener'); } },
      { label: 'Đóng' },
    ],
  });
}

function setupShutdown() {
  const btn = $('#shutdown-btn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const { confirm } = await import('./ui.js');
    if (!await confirm(
      'Tắt tool sẽ dừng backend Python + giải phóng port 8000. '
      + 'Các task đang chạy sẽ bị huỷ. Tiếp tục?',
      'Xác nhận tắt tool',
    )) return;
    try {
      await api.system.shutdown();
      toast('Đang tắt tool — trang sẽ trở thành "không kết nối được"', 'info');
      // Backend dies in ~0.5s. Replace the entire page with a clear
      // "tool is gone" message so the user doesn't sit waiting for the
      // usual loading spinner.
      setTimeout(() => {
        document.body.innerHTML =
          '<div style="display:flex;align-items:center;justify-content:center;'
          + 'height:100vh;font-family:sans-serif;color:#666;text-align:center;'
          + 'padding:20px;background:#fff">'
          + '<div><h2 style="color:#dc2626;margin-bottom:12px">'
          + 'RedOne Creative đã tắt</h2>'
          + '<p>Có thể đóng tab này. Để mở lại tool, double-click '
          + '<code>RedOne Creative.exe</code> hoặc chạy <code>python launch.py</code>.</p>'
          + '</div></div>';
      }, 1500);
    } catch (e) {
      toast(e.message || 'Tắt tool lỗi', 'error');
    }
  });
}

function showSessionDeadBanner(d) {
  const banner = $('#session-banner');
  if (!banner) return;
  banner.classList.remove('hidden');
  banner.innerHTML = '';
  banner.appendChild(el('div', { style: { flex: 1 } },
    el('strong', null, '⚠ Session hết hạn: '),
    el('span', { class: 'mono', style: { background: 'rgba(255,255,255,0.18)', padding: '2px 8px', borderRadius: '99px', margin: '0 4px' } }, d.email || `account #${d.account_id}`),
    ' — ',
    (d.reason || 'cần login lại để tiếp tục'),
  ));
  banner.appendChild(el('button', {
    class: 'btn',
    onclick: async () => {
      window.__app.navigate('accounts');
      banner.classList.add('hidden');
    },
  }, 'Mở tab Tài Khoản'));
  banner.appendChild(el('button', {
    class: 'btn btn-close',
    onclick: () => banner.classList.add('hidden'),
  }, '✕'));
}

/**
 * After an update (auto-update or manual zip-replace), the on-disk files are
 * new but Chrome keeps running the OLD unpacked extension until the user
 * reloads it. A version bump that changes the manifest (e.g. v1.2.0 added the
 * shakker.ai content script) WON'T take effect until that reload.
 *
 * Detect "version changed since last boot" via localStorage and show a
 * one-time dismissible reminder. First-ever run records the version silently
 * (no reminder). Uses its own banner element so it never fights the
 * session-dead banner.
 */
function maybeShowUpdateNotice() {
  try {
    const KEY = 'redone_last_version';
    const cur = (store.app && store.app.version) || '';
    if (!cur) return;
    const prev = localStorage.getItem(KEY);
    localStorage.setItem(KEY, cur);
    if (!prev || prev === cur) return;   // first run, or unchanged → no notice

    const sb = $('#session-banner');
    let banner = $('#update-notice-banner');
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'update-notice-banner';
      banner.className = 'session-banner';   // reuse existing styling
      if (sb && sb.parentNode) sb.parentNode.insertBefore(banner, sb.nextSibling);
      else document.body.insertBefore(banner, document.body.firstChild);
    }
    banner.classList.remove('hidden');
    banner.innerHTML = '';
    banner.appendChild(el('div', { style: { flex: 1 } },
      el('strong', null, `✓ Đã cập nhật lên v${cur}. `),
      'Bản mới có thể kèm extension mới — mở ',
      el('span', {
        class: 'mono',
        style: { background: 'rgba(255,255,255,0.18)', padding: '2px 8px', borderRadius: '99px', margin: '0 4px' },
      }, 'chrome://extensions'),
      ' → bấm ↻ Reload extension "RedOne Auth Helper", rồi refresh lại tab flow.google.com + shakker.ai.',
    ));
    banner.appendChild(el('button', {
      class: 'btn btn-close',
      onclick: () => banner.classList.add('hidden'),
    }, '✕'));
  } catch (e) {
    console.warn('maybeShowUpdateNotice error:', e);
  }
}

function setupWS() {
  ws.start();
  ws.on('_connected', () => {
    $('#server-dot').className = 'dot dot-green';
    $('#server-status').textContent = 'Đã kết nối';
  });
  ws.on('_disconnected', () => {
    $('#server-dot').className = 'dot dot-red';
    $('#server-status').textContent = 'Mất kết nối';
  });
  ws.on('account_updated', refreshAccounts);
  ws.on('account_added', refreshAccounts);
  ws.on('account_deleted', refreshAccounts);
  ws.on('account_session_dead', (d) => {
    refreshAccounts();
    showSessionDeadBanner(d || {});
    toast(`Session ${d?.email || ''} hết hạn — login lại trong tab Tài Khoản`, 'error', 10000);
  });
  // Shakker account pool → keep the topbar power chip live.
  ws.on('shakker_account_synced', refreshShakkerPower);
  ws.on('shakker_account_updated', refreshShakkerPower);
  ws.on('shakker_account_deleted', refreshShakkerPower);
}

async function checkForUpdate() {
  try {
    const r = await api.system.checkUpdate();
    if (!r || !r.update_available) return;

    // Suppress same banner the user already dismissed for this exact version
    const dismissed = localStorage.getItem('redone_dismissed_update');
    if (dismissed === r.latest) return;

    const banner = $('#update-banner');
    if (!banner) return;
    banner.classList.remove('hidden');
    banner.innerHTML = '';

    const text = el('div', { class: 'update-text' },
      '🎉 Có bản cập nhật mới ',
      el('span', { class: 'update-version' }, `v${r.latest}`),
      ` — bạn đang dùng v${r.current}`,
    );

    // EXE bundle: in-app installer. Dev mode: just point to GitHub.
    const canAutoInstall = !!(r.can_auto_install && r.download_url);
    const actionBtn = canAutoInstall
      ? el('button', {
          class: 'btn',
          onclick: () => openUpdateModal(r),
        }, 'Tải xuống & cài đặt')
      : el('a', {
          class: 'btn',
          href: r.download_url || r.release_url
            || `https://github.com/${r.github_repo || ''}/releases`,
          target: '_blank', rel: 'noopener',
        }, 'Mở GitHub Release');

    const closeBtn = el('button', {
      class: 'btn btn-close',
      title: 'Bỏ qua phiên bản này',
      onclick: () => {
        localStorage.setItem('redone_dismissed_update', r.latest);
        banner.classList.add('hidden');
      },
    }, '✕');

    banner.appendChild(text);
    banner.appendChild(actionBtn);
    banner.appendChild(closeBtn);
  } catch (e) {
    // Silent — update check is non-critical
    console.warn('Update check failed:', e.message);
  }
}


// ── In-app updater modal ─────────────────────────────────────────
// Opens when user clicks the banner's "Tải xuống & cài đặt" button.
// One modal handles: start → progress → ready-to-install → restart.
// Listens to WS "update_progress" events broadcast by the backend updater.

let _updateModalState = null;   // {root, bar, label, stage, version, action}
let _updateUnsub = null;

function openUpdateModal(info) {
  // If already open, just show it
  if (_updateModalState) {
    _updateModalState.root.classList.remove('hidden');
    return;
  }

  // No inline display override — let .modal-backdrop's `display:grid; place-items:center`
  // center the card. (An inline `display:flex` here only centered vertically and
  // pinned the modal to the left, since flex ignores place-items' justify-items.)
  const root = el('div', { class: 'modal-backdrop' });
  const card = el('div', { class: 'modal', style: { maxWidth: '560px' } });
  root.appendChild(card);
  document.body.appendChild(root);

  card.appendChild(el('h3', { class: 'modal-title' },
    `Cập nhật RedOne Creative v${info.latest}`));

  // Release notes preview
  if (info.release_notes) {
    card.appendChild(el('div', {
      class: 'field-help',
      style: {
        maxHeight: '160px', overflowY: 'auto',
        background: 'var(--bg-2)', padding: '10px',
        borderRadius: 'var(--r-md)', whiteSpace: 'pre-wrap',
        marginBottom: '14px', fontSize: '12px',
      },
    }, info.release_notes));
  }

  // Status line + progress bar
  const label = el('div', { class: 'field-label' }, 'Sẵn sàng tải xuống');
  const sizeMB = info.asset_size
    ? `(${(info.asset_size / 1024 / 1024).toFixed(1)} MB)`
    : '';
  const sub = el('div', { class: 'field-help' },
    `Sẽ tải ${info.asset_name || 'release.zip'} ${sizeMB} từ GitHub. `
    + `Dữ liệu của bạn (data/, outputs/) sẽ KHÔNG bị xóa.`);
  const barWrap = el('div', {
    style: {
      marginTop: '12px', height: '10px', background: 'var(--bg-2)',
      borderRadius: '5px', overflow: 'hidden',
    },
  });
  const bar = el('div', {
    style: {
      height: '100%', width: '0%', background: 'var(--brand)',
      transition: 'width 0.3s',
    },
  });
  barWrap.appendChild(bar);

  card.appendChild(label);
  card.appendChild(sub);
  card.appendChild(barWrap);

  // Action button — starts as "Tải xuống", morphs to "Cài đặt & restart"
  // or "Đóng" depending on stage.
  const actionBtn = el('button', { class: 'btn btn-primary' }, 'Tải xuống');
  const closeBtn = el('button', { class: 'btn btn-ghost' }, 'Đóng');
  const actionsRow = el('div', {
    style: { display: 'flex', gap: '8px', marginTop: '16px', justifyContent: 'flex-end' },
  }, closeBtn, actionBtn);
  card.appendChild(actionsRow);

  _updateModalState = { root, bar, label, sub, stage: 'idle',
    version: info.latest, action: actionBtn, info };

  // Wire WS subscription (lazy import avoids circular dep)
  import('./ws.js').then(({ ws }) => {
    if (_updateUnsub) { _updateUnsub(); _updateUnsub = null; }
    _updateUnsub = ws.on('update_progress', (state) => {
      if (!_updateModalState) return;
      applyUpdateState(state);
    });
  });

  // Fetch latest state in case download was already running (e.g. user
  // reloaded the page mid-download)
  api.system.updateState().then(s => {
    if (s && s.stage !== 'idle') applyUpdateState(s);
  }).catch(() => {});

  actionBtn.addEventListener('click', async () => {
    const st = _updateModalState;
    if (!st) return;
    if (st.stage === 'idle' || st.stage === 'error') {
      // Start download
      actionBtn.disabled = true;
      st.label.textContent = 'Đang gửi yêu cầu…';
      try {
        await api.system.startUpdate();
        // WS events will drive the bar from here
      } catch (e) {
        st.label.textContent = `Lỗi: ${e.message}`;
        actionBtn.disabled = false;
      }
    } else if (st.stage === 'ready') {
      // Install + restart
      actionBtn.disabled = true;
      actionBtn.textContent = 'Đang khởi động lại…';
      st.label.textContent = 'Đang cài đặt — app sẽ tự mở lại trong vài giây';
      try {
        await api.system.applyUpdate();
        // Backend will die. Show a "lost connection" hint after a beat.
        setTimeout(() => {
          st.sub.innerHTML = '';
          st.sub.appendChild(el('div', {
            style: { color: 'var(--brand)' },
          }, '✓ Đã chạy installer. Tool đang khởi động lại — đợi 5-10s rồi mở lại trình duyệt nếu cần.'));
        }, 1500);
      } catch (e) {
        st.label.textContent = `Lỗi: ${e.message}`;
        actionBtn.disabled = false;
        actionBtn.textContent = 'Thử lại';
      }
    }
  });

  closeBtn.addEventListener('click', () => {
    // Allow closing only when not actively downloading — avoids accidental
    // mid-download dismissals. The job continues in backend regardless,
    // so closing the modal isn't destructive.
    closeUpdateModal();
  });
}

function applyUpdateState(state) {
  if (!_updateModalState) return;
  const st = _updateModalState;
  st.stage = state.stage;
  const pct = state.percent || 0;
  st.bar.style.width = `${pct}%`;
  st.label.textContent = state.message || state.stage;

  if (state.stage === 'downloading') {
    const mbDone = (state.downloaded / 1024 / 1024).toFixed(1);
    const mbTotal = state.total ? (state.total / 1024 / 1024).toFixed(1) : '?';
    st.label.textContent = `Đang tải ${mbDone} / ${mbTotal} MB (${pct.toFixed(0)}%)`;
    st.action.disabled = true;
    st.action.textContent = 'Đang tải…';
  } else if (state.stage === 'extracting') {
    st.bar.style.background = 'var(--accent-orange)';
    st.action.disabled = true;
    st.action.textContent = 'Đang giải nén…';
  } else if (state.stage === 'ready') {
    st.bar.style.background = 'var(--green)';
    st.action.disabled = false;
    st.action.textContent = 'Cài đặt & khởi động lại';
  } else if (state.stage === 'installing') {
    st.bar.style.background = 'var(--brand)';
    st.action.disabled = true;
    st.action.textContent = 'Đang cài…';
  } else if (state.stage === 'error') {
    st.bar.style.background = 'var(--red)';
    st.action.disabled = false;
    st.action.textContent = 'Thử lại';
  }
}

function closeUpdateModal() {
  if (!_updateModalState) return;
  _updateModalState.root.remove();
  _updateModalState = null;
  if (_updateUnsub) { _updateUnsub(); _updateUnsub = null; }
}

async function init() {
  // Auth gate — before anything else. If not logged in, bounce to
  // /login.html instead of rendering the empty app shell. The backend
  // also enforces this server-side, but doing it client-side too means
  // the UI never flashes "loading" then "redirect".
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data && data.oauth_configured !== false && !data.logged_in) {
      window.location.replace('/login.html');
      return;
    }
    if (data && data.email) {
      // Stash the logged-in user on the global app object so other
      // pages (e.g. Settings) can read it without re-fetching.
      window.__app = window.__app || {};
      window.__app.user = data;
      window.__redone_user_email = data.email;
    }
  } catch (e) {
    // Backend offline — let init continue; downstream API calls will
    // surface the connection error themselves.
    console.warn('Auth probe failed:', e);
  }

  setupTheme();
  setupShutdown();
  setupHelpLinks();
  loadGithubLinks();   // fire-and-forget; buttons have a fallback URL meanwhile
  setupAuthUI();
  setupSidebar();
  // Kho tính năng: load catalog + render dynamic feature tabs. Non-blocking so
  // a slow/offline catalog never delays boot (backend falls back instantly).
  initFeatures();
  setupWS();
  setupFlowAccountSync();

  // First-run setup wizard. Blocks the rest of init() until the user
  // completes (or the backend reports everything is already in place).
  // Skipping this would let the watermark video page render with missing
  // deps, leading to confusing error toasts later.
  try {
    const { maybeRunSetupWizard } = await import('./setup_wizard.js');
    await maybeRunSetupWizard();
  } catch (e) {
    console.warn('Setup wizard failed:', e);
    // Non-blocking: if the wizard itself errors out, let the app boot
    // and the user can still try to use OpenCV features.
  }

  await Promise.all([refreshAccounts(), loadSettings(), refreshShakkerPower()]);

  // Reveal Team/Quản trị tabs if this user is a Hub lead/admin (no-op when
  // the Hub is disabled). Fire-and-forget so a slow Hub never blocks boot.
  refreshHubStatus();

  // Firebase tracking: show the Tracking nav group only if tracking is
  // enabled AND the current user is in TRACKING_ADMIN_EMAILS.
  api.tracking.status()
    .then(r => {
      const g = document.getElementById('nav-group-tracking');
      if (g && r && r.enabled && r.is_admin) g.style.display = '';
    })
    .catch(() => {});

  // HG Stock: show sidebar tab if user has upload permission (fire-and-forget).
  import('./pages/hgstock.js').then(m => m.checkHGStockNav && m.checkHGStockNav()).catch(() => {});

  // After an update, remind the user to reload the Chrome extension (Chrome
  // doesn't hot-reload unpacked extensions — new manifest needs a manual ↻).
  maybeShowUpdateNotice();

  // Auto-scan accounts (Flow credit + session) once per tool open. Runs after
  // settings + accounts are loaded so it can honour the auth_mode guard.
  autoScanAccounts();

  const initialPage = (location.hash || '#content').slice(1);
  if (PAGES[initialPage]) {
    navigate(initialPage);
  }

  // Non-blocking update check
  checkForUpdate();

  // First-run nudge: if no Chrome extension is connected, point the user to
  // the GitHub guide. Fire-and-forget (waits a few seconds internally).
  maybeShowExtensionReminder();
}

if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

window.__app = {
  store,
  navigate,
  refreshAccounts,
  refreshShakkerPower,
  refreshHubStatus,
  refreshFeatureNav: renderFeatureNav,
  get currentFlowAccount() { return window.currentFlowAccount || null; },
};
