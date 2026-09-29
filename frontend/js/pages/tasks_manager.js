// Tasks Manager — table of all jobs (running + queued + completed) with
// progress, type, time. Live-updates via WebSocket.
import { el, clear, toast, setLoading, icon, confirm } from '../ui.js';
import { api } from '../api.js';
import { ws } from '../ws.js';
import { tasksStore } from '../tasks_store.js';

const KIND_LABEL = {
  image: 'Tạo Ảnh',
  storyboard: 'Storyboard',
  content: 'Tạo Video',
  t2v: 'Tạo Video',
  i2v: 'Tạo Video (I2V)',
  long_video: 'Video Dài',
  shakker: 'Ảnh Shakker',
  flow_upscale: 'Upscale',
};
const KIND_NAV = {
  image: 'image',
  storyboard: 'storyboard',
  content: 'content',
  t2v: 'content',
  i2v: 'content',
  long_video: 'long-video',
  shakker: 'shakker',
};
// Parent task ids whose nested upscale children are collapsed (hidden).
// Module-level → survives WS reloads + page navigation. Default = expanded.
const tmCollapsed = new Set();

const STATUS_CHIP = {
  PENDING:   { cls: 'chip-yellow', label: 'Đang chờ' },
  RUNNING:   { cls: 'chip-blue',   label: 'Đang chạy' },
  COMPLETED: { cls: 'chip-green',  label: 'Hoàn tất' },
  ERROR:     { cls: 'chip-red',    label: 'Lỗi' },
  CANCELLED: { cls: '',            label: 'Đã hủy' },
  PAUSED:    { cls: 'chip-yellow', label: 'Tạm dừng' },
};

export function renderTasksManager(root) {
  root.appendChild(el('div', { class: 'page-hero' },
    el('div', { class: 'hero-icon' }, icon('refresh', 22)),
    el('div', { class: 'hero-text' },
      el('h2', null, 'Quản lý Task'),
      el('p', null, 'Theo dõi tất cả task — hỗ trợ chạy song song 2 task trên 2 tài khoản Google Flow khác nhau'),
    ),
  ));

  // Stat overview
  const stats = el('div', { class: 'stats-grid' });
  root.appendChild(stats);

  // Toolbar
  root.appendChild(el('div', { style: { display: 'flex', gap: '8px', marginBottom: '16px' } },
    el('button', { class: 'btn', id: 'tm-refresh' }, icon('refresh'), 'Làm mới'),
    el('div', { style: { flex: 1 } }),
    el('label', { style: { display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12.5px' } },
      el('input', { type: 'checkbox', id: 'tm-active-only' }),
      'Chỉ hiện đang hoạt động',
    ),
  ));

  // Table card
  const tableCard = el('div', { class: 'card' },
    el('div', { class: 'card-header' },
      el('h3', { class: 'card-title' }, 'Danh sách task'),
      el('div', { class: 'card-subtitle', id: 'tm-count' }, '—'),
    ),
    el('div', { id: 'tm-table-wrap', style: { overflowX: 'auto' } }),
  );
  root.appendChild(tableCard);

  // State
  let allTasks = [];

  async function reload() {
    try {
      const r = await api.tasks.list(200);
      allTasks = r.tasks || [];
      render();
    } catch (e) { toast(e.message, 'error'); }
  }

  function render() {
    // Guard: this page's WS subscription can outlive its DOM. navigate()
    // swaps the contents of the persistent #page-container, so a debounced
    // reload() scheduled before leaving can fire after our elements are gone.
    // If our marker element is missing, the page is no longer mounted — bail
    // instead of throwing "Cannot set properties of null" on #tm-count.
    const countEl = root.querySelector('#tm-count');
    if (!countEl) return;
    const activeOnly = root.querySelector('#tm-active-only')?.checked;
    // Upscale chạy NGOÀI queue → backend vẫn để task = COMPLETED khi upscale.
    // Coi task đang upscale là "đang hoạt động" để nó không bị ẩn khỏi filter.
    const ub = tasksStore.getUpscaleBatch();
    const upscalingId = ub.active ? ub.taskId : null;
    const tasks = activeOnly
      ? allTasks.filter(t => ['PENDING', 'RUNNING', 'PAUSED'].includes(t.status) || t.id === upscalingId)
      : allTasks;
    // Task upscale (con) là task phụ của task gốc → KHÔNG đếm như task riêng.
    const countedN = tasks.filter((t) => t.mode !== 'flow_upscale').length;
    countEl.textContent = `${countedN} task`;
    renderStats(allTasks);
    renderTable(tasks);
  }

  function renderStats(tasks) {
    clear(stats);
    // Task upscale (con) không tính như task riêng trong thống kê.
    tasks = tasks.filter((t) => t.mode !== 'flow_upscale');
    const ub = tasksStore.getUpscaleBatch();
    const upscalingId = ub.active ? ub.taskId : null;
    // Task đang upscale (gen đã COMPLETED) vẫn tính là "đang chạy".
    const running = tasks.filter(t => t.status === 'RUNNING' || t.id === upscalingId).length;
    const queued = tasks.filter(t => t.status === 'PENDING' && t.queue_position > 0).length;
    const done = tasks.filter(t => t.status === 'COMPLETED').length;
    const err = tasks.filter(t => t.status === 'ERROR').length;
    [
      { label: 'Đang chạy', value: running, glow: 'blue' },
      { label: 'Đang chờ', value: queued, glow: 'orange' },
      { label: 'Hoàn tất', value: done, glow: 'green' },
      { label: 'Lỗi', value: err, glow: 'purple' },
    ].forEach(s => {
      stats.appendChild(el('div', { class: `stat-card glow-${s.glow}` },
        el('div', { class: 'stat-label' }, s.label),
        el('div', { class: 'stat-value' }, String(s.value)),
      ));
    });
  }

  function renderTable(tasks) {
    const wrap = root.querySelector('#tm-table-wrap');
    clear(wrap);

    if (tasks.length === 0) {
      wrap.appendChild(el('div', { class: 'empty' },
        el('div', { class: 'empty-icon' }, icon('refresh', 32)),
        el('div', null, 'Chưa có task nào'),
      ));
      return;
    }

    // Nhóm task Flow-upscale (mode 'flow_upscale') xuống DƯỚI task gốc của nó
    // như dòng con thụt vào. parent_task_id nằm trong character_images_json.
    const cfgOf = (t) => { try { return JSON.parse(t.character_images_json || '{}'); } catch (_) { return {}; } };
    const parentOf = (t) => (t.mode === 'flow_upscale' ? (cfgOf(t).parent_task_id || null) : null);
    const idSet = new Set(tasks.map((t) => t.id));
    const childrenByParent = new Map();
    for (const t of tasks) {
      const p = parentOf(t);
      if (p != null && idSet.has(p)) {
        if (!childrenByParent.has(p)) childrenByParent.set(p, []);
        childrenByParent.get(p).push(t);
      }
    }

    const table = el('table', { class: 'table' });
    table.appendChild(el('thead', null, el('tr', null,
      el('th', null, '#'),
      el('th', null, 'Tên'),
      el('th', null, 'Loại'),
      el('th', null, 'Trạng thái'),
      el('th', null, 'Tiến độ'),
      el('th', null, 'Tạo lúc'),
      el('th', { style: { textAlign: 'right' } }, ''),
    )));
    const tbody = el('tbody');
    for (const t of tasks) {
      // Dòng con (cha có trong danh sách) render dưới cha — bỏ qua ở đây.
      // Con mồ côi (cha bị lọc/đã xóa) vẫn render top-level để không bị ẩn.
      const p = parentOf(t);
      if (p != null && idSet.has(p)) continue;
      const kids = childrenByParent.get(t.id);
      const hasKids = !!(kids && kids.length);
      const expanded = hasKids && !tmCollapsed.has(t.id);
      tbody.appendChild(renderRow(t, {
        hasKids,
        expanded,
        onToggle: () => {
          if (tmCollapsed.has(t.id)) tmCollapsed.delete(t.id);
          else tmCollapsed.add(t.id);
          render();
        },
      }));
      if (hasKids && expanded) kids.forEach((k) => tbody.appendChild(renderRow(k, { isChild: true })));
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  function renderRow(t, opts = {}) {
    const { isChild = false, hasKids = false, expanded = false, onToggle = null } = opts;
    const kindLabel = KIND_LABEL[t.mode] || t.mode || '—';
    const navTarget = KIND_NAV[t.mode];
    // Khi ảnh của task đang được upscale (chạy ngoài queue), hiện "Đang upscale"
    // thay cho "Hoàn tất" — đọc trạng thái upscale từ store (frontend-only).
    const ub = tasksStore.getUpscaleBatch();
    const isUpscaling = ub.active && ub.taskId === t.id;
    const stat = isUpscaling
      ? { cls: 'chip-blue', label: `Đang upscale ${(ub.resolution || '').toUpperCase()}${ub.total ? ` ${ub.done}/${ub.total}` : ''}` }
      : (STATUS_CHIP[t.status] || { cls: '', label: t.status });

    // Position badge for queued
    let posBadge = null;
    if (t.queue_position > 0) {
      posBadge = el('span', { class: 'chip chip-yellow', style: { marginLeft: '6px' } },
        `Hàng đợi #${t.queue_position}`);
    }

    // Progress bar
    const pct = t.progress_percent || 0;
    const progressBar = el('div', { style: { minWidth: '140px' } },
      el('div', { class: 'progress' },
        el('div', { class: 'progress-bar', style: { width: `${pct}%` } }),
      ),
      el('div', { style: { fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' } },
        `${t.done_count || 0}/${t.total_count || 0} ${t.error_count ? `· ${t.error_count} lỗi` : ''}`),
    );

    // Name cell. Child (upscale) rows hang off a CSS tree-line (.tm-subname) and
    // lead with a colored status dot — hierarchy read like a sidebar tree.
    const nameInner = el('div', null,
      el('div', { style: { fontWeight: 600 } }, t.name || '(không tên)'),
      // Dòng con (upscale): chỉ tên + chấm trạng thái, bỏ dòng phụ cho gọn.
      isChild ? null : el('div', { style: { fontSize: '11px', color: 'var(--text-muted)' } },
        `${t.quality || '—'} · ${t.aspect_ratio || '—'}`),
    );
    const DOT = {
      RUNNING: '#3b82f6', PENDING: '#3b82f6', COMPLETED: 'var(--green)',
      ERROR: 'var(--red)', PAUSED: 'var(--yellow)', CANCELLED: 'var(--text-muted)',
    };
    // Parent with upscale children: a chevron toggle to expand/collapse them.
    const caret = hasKids
      ? el('button', {
          class: 'btn btn-sm btn-ghost tm-caret',
          title: expanded ? 'Thu gọn task con' : 'Mở rộng task con',
          onclick: () => { if (onToggle) onToggle(); },
          style: { transform: expanded ? 'rotate(90deg)' : 'none' },
        }, icon('chevron', 16))
      : null;
    const nameCell = isChild
      ? el('td', { class: 'tm-subname' },
          el('div', { style: { display: 'flex', alignItems: 'center', gap: '9px' } },
            el('span', { class: 'tm-dot', style: { background: DOT[t.status] || 'var(--text-muted)' } }),
            nameInner))
      : el('td', null,
          hasKids
            ? el('div', { style: { display: 'flex', alignItems: 'center', gap: '4px' } }, caret, nameInner)
            : nameInner);

    return el('tr', isChild ? { class: 'tm-subrow' } : null,
      // Dòng con (upscale) không hiện số task riêng — nó thuộc task gốc phía trên.
      el('td', { class: 'mono', style: { color: 'var(--text-muted)' } }, isChild ? '' : `#${t.id}`),
      nameCell,
      el('td', null, kindLabel),
      el('td', null,
        el('span', { class: `chip ${stat.cls}` }, stat.label),
        posBadge,
        t.flow_account_email
          ? el('span', {
              class: 'chip chip-purple',
              title: `Tài khoản Google Flow: ${t.flow_account_email}`,
              style: { marginLeft: '6px' },
            }, `Flow: ${t.flow_account_email.split('@')[0]}`)
          : null,
      ),
      el('td', null, progressBar),
      el('td', { style: { fontSize: '11.5px', color: 'var(--text-muted)' } },
        fmtTime(t.created_at)),
      el('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } },
        el('button', {
          class: 'btn btn-sm btn-ghost',
          title: 'Mở thư mục chứa file của task',
          onclick: () => openTaskFolder(t.id),
        }, icon('folder', 14)),
        navTarget
          ? el('button', { class: 'btn btn-sm btn-ghost', title: 'Xem task này trên trang gốc',
              onclick: () => window.__app.navigate(navTarget, { taskId: t.id }) },
              icon('eye', 14))
          : null,
        ['ERROR', 'CANCELLED'].includes(t.status)
          ? el('button', { class: 'btn btn-sm btn-primary', title: 'Thử lại task',
              onclick: () => retryTask(t.id) },
              icon('refresh', 14))
          : null,
        t.status === 'PAUSED'
          ? el('button', { class: 'btn btn-sm btn-primary', title: 'Tiếp tục gen',
              onclick: () => resumeTask(t.id) },
              icon('play', 14))
          : null,
        ['PENDING', 'RUNNING'].includes(t.status)
          ? el('button', { class: 'btn btn-sm btn-ghost', title: 'Tạm dừng (gen tiếp sau)',
              onclick: () => pauseTask(t.id) },
              icon('pause', 14))
          : null,
        ['PENDING', 'RUNNING'].includes(t.status)
          ? el('button', { class: 'btn btn-sm btn-danger', title: 'Hủy',
              onclick: () => cancelTask(t.id) },
              icon('stop', 14))
          : null,
      ),
    );
  }

  function fmtTime(s) {
    if (!s) return '—';
    try {
      const d = new Date(typeof s === 'string' && s.includes(' ') ? s.replace(' ', 'T') + 'Z' : s);
      if (isNaN(d.getTime())) return s;
      const now = new Date();
      const diff = (now - d) / 1000;
      if (diff < 60) return 'vừa xong';
      if (diff < 3600) return `${Math.floor(diff / 60)} phút trước`;
      if (diff < 86400) return `${Math.floor(diff / 3600)} giờ trước`;
      return d.toLocaleString('vi-VN');
    } catch { return String(s); }
  }

  async function cancelTask(id) {
    if (!await confirm('Hủy task này?', 'Xác nhận')) return;
    try {
      await api.tasks.cancel(id);
      toast('Đã hủy', 'info');
      await reload();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function openTaskFolder(id) {
    try {
      const r = await api.tasks.openFolder(id);
      toast(`Đã mở: ${r.path}`, 'success', 5000);
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  async function retryTask(id) {
    try {
      const r = await api.tasks.retry(id);
      toast(`Đã enqueue lại task #${id} (vị trí ${r.queue_position})`, 'success');
      await reload();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function pauseTask(id) {
    try {
      await api.tasks.pause(id);
      toast('Đã tạm dừng — bấm Tiếp tục để gen nốt', 'info');
      await reload();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function resumeTask(id) {
    try {
      await api.tasks.resume(id);
      toast(`Đang tiếp tục task #${id}`, 'success');
      await reload();
    } catch (e) { toast(e.message, 'error'); }
  }

  // Wire toolbar
  root.querySelector('#tm-refresh').addEventListener('click', reload);
  root.querySelector('#tm-active-only').addEventListener('change', render);

  // Live updates via WebSocket
  const offs = [];
  const events = [
    'task_started', 'task_completed', 'task_error', 'task_cancelled',
    'task_paused', 'task_resumed',
    'task_progress', 'item_completed', 'item_error', 'queue_updated',
  ];
  for (const ev of events) {
    offs.push(ws.on(ev, () => {
      // Debounce reload calls
      clearTimeout(window._tmReloadTimer);
      window._tmReloadTimer = setTimeout(reload, 250);
    }));
  }
  // Upscale chạy ngoài queue → danh sách task từ backend KHÔNG đổi. Chỉ cần
  // re-render (không reload) để chip "Đang upscale" cập nhật theo store.
  const upscaleEvents = [
    'upscale_batch_started', 'upscale_started', 'upscale_completed',
    'upscale_error', 'upscale_batch_done',
  ];
  for (const ev of upscaleEvents) {
    offs.push(ws.on(ev, () => {
      clearTimeout(window._tmUpscaleTimer);
      window._tmUpscaleTimer = setTimeout(render, 120);
    }));
  }

  const obs = new MutationObserver(() => {
    // #page-container is persistent (navigate() only swaps its children), so
    // document.body.contains(root) stays true forever and never triggers
    // cleanup. Detect unmount by our own marker disappearing — otherwise the
    // WS subscriptions leak and keep firing reload() on every task event from
    // OTHER pages (wasteful + the source of the #tm-count null crash).
    if (!root.querySelector('#tm-count')) {
      offs.forEach(o => o());
      clearTimeout(window._tmReloadTimer);
      clearTimeout(window._tmUpscaleTimer);
      obs.disconnect();
    }
  });
  obs.observe(document.body, { childList: true, subtree: true });

  // Initial load
  reload();
}
