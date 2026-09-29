// Global tasks store — survives page navigation AND browser refresh.
// • Tasks are kept in memory (Map) for fast lookup + reactive subscriptions.
// • Same tasks are mirrored to localStorage on every change, so an F5 or full
//   browser restart still restores the gallery. (Backend keeps running the
//   job; we just need to find which task to re-attach to.)
// • Subscribes to WebSocket events ONCE at app boot.

import { ws } from './ws.js';

const STORAGE_KEY = 'redone_tasks_v1';
const MAX_STORED_TASKS = 20;    // prune oldest beyond this
const INSTANCE_ID = Math.random().toString(36).slice(2, 8);   // unique per module load
const LOG_PREFIX = `%c[tasks_store#${INSTANCE_ID}]`;
const LOG_STYLE = 'color:#dc2626;font-weight:600';

function log(...args) {
  console.log(LOG_PREFIX, LOG_STYLE, ...args);
}

// Track all instances on window so we can detect duplicates
if (!window.__tasksStoreInstances) window.__tasksStoreInstances = [];
window.__tasksStoreInstances.push(INSTANCE_ID);
log(`module loaded (instance count: ${window.__tasksStoreInstances.length})`);

const tasks = new Map();
const subscribers = new Map();
const kindSubscribers = new Map();

// Progress of the current 2K/4K upscale batch. Upscale is sequential (one
// image at a time, anti-429) and only one batch runs at once, so a single
// module-level record is enough. Read by the gallery toolbar to render
// "Đang upscale 2/5". `running` is the 1-based index currently processing.
// `taskId` scopes the progress to ONE gallery so an upscale started from the
// Storyboard tab doesn't show "Đang upscale" in the Tạo Ảnh tab (both read
// this global).
let upscaleBatch = { active: false, total: 0, done: 0, running: 0, resolution: '', taskId: null };

// ── Persistence ────────────────────────────────────────────
function persistNow() {
  try {
    // Convert Map → array, drop oldest beyond MAX
    const arr = [...tasks.values()]
      .sort((a, b) => b.created_at - a.created_at)
      .slice(0, MAX_STORED_TASKS);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
  } catch (e) {
    console.warn('[tasks_store] persist failed', e);
  }
}

// Trailing-debounced persist. notify() fires on EVERY WS event (hundreds
// during a 100-item task); a synchronous JSON.stringify(all tasks) + localStorage
// write per event saturates the main thread. Coalesce writes to ~once/500ms.
// Subscribers still fire synchronously (see notify) — only the write is delayed.
let _persistTimer = 0;
function persist() {
  if (_persistTimer) return;
  _persistTimer = setTimeout(() => { _persistTimer = 0; persistNow(); }, 500);
}
// Flush pending write immediately (tab close / hide / structural deletes) so
// no state is lost on reload. Backend doesn't replay events on reconnect.
function persistFlush() {
  if (_persistTimer) { clearTimeout(_persistTimer); _persistTimer = 0; }
  persistNow();
}
window.addEventListener('pagehide', persistFlush);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') persistFlush();
});

function restore() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return;
    for (const t of arr) {
      if (t && t.id) tasks.set(t.id, t);
    }
    log(`restored ${arr.length} tasks from localStorage`);
  } catch (e) {
    console.warn('[tasks_store] restore failed', e);
  }
}
restore();

function notify(taskId) {
  const t = tasks.get(taskId);
  if (!t) return;
  persist();
  const subs = subscribers.get(taskId);
  if (subs) for (const fn of subs) {
    try { fn(t); } catch (e) { console.error(e); }
  }
  const kindSubs = kindSubscribers.get(t.kind);
  if (kindSubs) for (const fn of kindSubs) {
    try { fn(t); } catch (e) { console.error(e); }
  }
}

export const tasksStore = {
  register(taskId, kind, meta = {}) {
    const t = {
      id: taskId,
      kind,
      name: meta.name || '',
      idea: meta.idea || '',
      status: 'running',
      aspect: meta.aspect || '1:1',
      model: meta.model || '',
      // Sub-type within a kind: shakker tasks are either gen (false) or upscale
      // (true). Lets the Shakker page keep the two galleries independent.
      upscale: !!meta.upscale,
      // Video-gen only: how many videos each prompt produced (1..4). The
      // content gallery groups every N contiguous items as one prompt's
      // variants. Defaults to 1 (flat gallery) for every other page.
      videosPerPrompt: meta.videosPerPrompt || 1,
      items: (meta.items || []).map(p => ({
        id: null,
        prompt: typeof p === 'string' ? p : (p.prompt || ''),
        status: 'pending',
        output_url: null,
        error: null,
        // For upscale tasks: the original (pre-upscale) image url, so the
        // gallery can open a before/after comparison on the result.
        before_url: (p && typeof p === 'object' && p.before_url) ? p.before_url : null,
      })),
      done: 0,
      error: 0,
      total: (meta.items || []).length,
      error_message: null,
      flow_account_email: meta.flow_account_email || null,
      created_at: Date.now(),
    };
    tasks.set(taskId, t);
    log(`register task=${taskId} kind=${kind} items=${t.total}`);
    notify(taskId);
    return t;
  },

  get(taskId) { return tasks.get(taskId) || null; },

  latestByKind(kind) {
    let latest = null;
    for (const t of tasks.values()) {
      if (t.kind === kind && (!latest || t.created_at > latest.created_at)) {
        latest = t;
      }
    }
    return latest;
  },

  list() {
    return [...tasks.values()].sort((a, b) => b.created_at - a.created_at);
  },

  on(taskId, fn) {
    if (!subscribers.has(taskId)) subscribers.set(taskId, new Set());
    subscribers.get(taskId).add(fn);
    return () => {
      const s = subscribers.get(taskId);
      if (s) s.delete(fn);
    };
  },

  onKind(kind, fn) {
    if (!kindSubscribers.has(kind)) kindSubscribers.set(kind, new Set());
    kindSubscribers.get(kind).add(fn);
    return () => {
      const s = kindSubscribers.get(kind);
      if (s) s.delete(fn);
    };
  },

  remove(taskId) {
    tasks.delete(taskId);
    subscribers.delete(taskId);
    persistFlush();   // structural change → write now (don't wait for debounce)
  },

  /**
   * Remove items from a task by their server-side output_path. Files on
   * disk are NOT touched — this only clears them from the in-memory + UI
   * state. If a task becomes empty, it's removed entirely.
   */
  removeItemsByPath(taskId, paths) {
    const t = tasks.get(taskId);
    if (!t) return;
    const drop = new Set(paths);
    t.items = t.items.filter(it => !drop.has(it.output_path));
    t.done = t.items.filter(it => it.status === 'done').length;
    t.error = t.items.filter(it => it.status === 'error').length;
    t.total = t.items.length;
    if (t.items.length === 0) {
      tasks.delete(taskId);
      subscribers.delete(taskId);
      persistFlush();
      return;
    }
    notify(taskId);
  },

  clear() {
    tasks.clear();
    subscribers.clear();
    persistFlush();
  },

  /**
   * Reset all items with status='error' back to 'pending' and flip the task
   * status to 'running'. Called by the gallery's "Gen lại N lỗi" button
   * BEFORE the WS events stream in — gives the UI an instant response so
   * the user sees the spinners come back without waiting for the first
   * item_status broadcast.
   *
   * Items with status='done' are left untouched (their files exist).
   * Items in 'pending' (e.g. cancelled mid-queue) also reset to clear any
   * stale "Đang chờ" → matches backend's retry_task() behaviour.
   */
  resetErrorItems(taskId) {
    const t = tasks.get(taskId);
    if (!t) return 0;
    let n = 0;
    for (const it of t.items) {
      if (it.status === 'error' || it.status === 'pending') {
        it.status = 'pending';
        it.error = null;
        it.error_detail = null;
        n += 1;
      }
    }
    t.error = 0;
    t.done = t.items.filter(x => x.status === 'done').length;
    t.status = 'running';
    t.error_message = null;
    // Clear the 403 cooldown flag — user explicitly asked for a retry,
    // presumably after waiting out Google's cooldown window.
    t.circuit_tripped = false;
    t.circuit_message = null;
    notify(taskId);
    return n;
  },

  /**
   * Optimistically mark selected items as queued for 2K/4K upscale the
   * instant the user clicks — so the gallery shows "Chờ upscale" on them
   * before the backend's first WS event arrives. The WS events
   * (upscale_started / _completed / _error) then flip each card to
   * running / done / error in turn. Also seeds the batch progress shown in
   * the toolbar so it reads "Chờ upscale N ảnh" immediately.
   */
  markUpscaleQueued(itemIds, resolution) {
    const ids = new Set(itemIds);
    let taskId = null;
    upscaleBatch = {
      active: true, total: itemIds.length, done: 0, running: 0,
      resolution: resolution || '', taskId: null,
    };
    for (const t of tasks.values()) {
      let changed = false;
      for (const it of t.items) {
        if (ids.has(it.id)) {
          it.upscale_status = 'queued';
          it.upscale_resolution = resolution;
          it.upscale_error = null;
          changed = true;
          if (taskId == null) taskId = t.id;   // scope progress to this gallery
        }
      }
      if (changed) notify(t.id);
    }
    upscaleBatch.taskId = taskId;
  },

  /** Current 2K/4K batch progress for the gallery toolbar. */
  getUpscaleBatch() { return upscaleBatch; },

  /**
   * Abort the optimistic queue state — used when the upscale request fails
   * before any WS event arrives (e.g. an immediate 401 on a dead session),
   * which would otherwise leave cards stuck on "Chờ upscale".
   */
  clearUpscaleQueue() {
    upscaleBatch = { active: false, total: 0, done: 0, running: 0, resolution: '', taskId: null };
    // Notify every task unconditionally so batch-tied UI (the disabled
    // "Xóa danh sách" button) re-enables even if nothing was queued.
    for (const t of tasks.values()) {
      for (const it of t.items) {
        if (it.upscale_status === 'queued') it.upscale_status = null;
      }
      notify(t.id);
    }
  },

  /**
   * Optimistically flip ONE errored item back to 'generating' — called the
   * instant the user clicks the per-item "Gen lại" button, before the backend
   * WS events (item_status → item_completed/item_error) stream in. Keeps the
   * task in 'running' so the overall status text + cancel button make sense.
   *
   * Returns true if the item was found + flipped.
   */
  /**
   * Point an item at its newly-saved prompt. Needed because only the image
   * path echoes `prompt` back over WS (item_status/item_completed); on the
   * video + Shakker galleries the card renders the STORE's copy, which was
   * seeded once at register() — without this it would keep showing the old
   * text after an edit.
   */
  updateItemPrompt(taskId, itemId, prompt) {
    const t = tasks.get(taskId);
    if (!t) return false;
    const it = t.items.find(x => x.id === itemId);
    if (!it) return false;
    it.prompt = prompt;
    notify(taskId);
    return true;
  },

  retryItemUI(taskId, itemId, asStatus = 'generating') {
    const t = tasks.get(taskId);
    if (!t) return false;
    const it = t.items.find(x => x.id === itemId);
    if (!it) return false;
    // Decrement whichever terminal counter this item was contributing to so
    // the task progress (done/error of total) stays consistent while it
    // regenerates. Regen-from-done overwrites in place → drop the old output.
    if (it.status === 'error') t.error = Math.max(0, t.error - 1);
    else if (it.status === 'done') t.done = Math.max(0, t.done - 1);
    it.status = asStatus;
    it.error = null;
    it.error_detail = null;
    it.output_url = null;
    it.output_path = null;
    // A prior upscale (if any) is invalidated by regenerating the base image —
    // clear it so the card stops offering "Tải 2K/4K" for a stale file.
    it.upscale_status = null;
    it.upscale_path = null;
    it.upscale_url = null;
    it.upscale_resolution = null;
    t.status = 'running';
    t.error_message = null;
    notify(taskId);
    return true;
  },

  /**
   * Ingest a full task record + items (from DB via api.tasks.get).
   * Restores historical tasks into memory so they can be viewed, navigated,
   * and interacted with even if the app was restarted or localStorage cleared.
   */
  ingest(task, items = []) {
    if (!task || !task.id) return null;
    const kind = (task.mode || 'image').toLowerCase();
    const statusMap = {
      COMPLETED: 'completed',
      RUNNING: 'running',
      PENDING: 'running',
      PAUSED: 'paused',
      ERROR: 'error',
      CANCELLED: 'cancelled',
    };
    const t = {
      id: task.id,
      kind: kind,
      name: task.name || '',
      idea: task.idea || '',
      status: statusMap[task.status] || (task.status || '').toLowerCase() || 'completed',
      aspect: task.aspect_ratio || '16:9',
      model: task.image_model || '',
      upscale: false,
      videosPerPrompt: 1,
      items: (items || []).map(it => {
        let extra = {};
        try { extra = it.extra_json ? JSON.parse(it.extra_json) : {}; } catch (_) {}
        const isDone = (it.status || '').toUpperCase() === 'COMPLETED';
        const isErr = (it.status || '').toUpperCase() === 'ERROR';
        const outUrl = isDone && it.output_path ? (pathToUrl(it.output_path) + `?v=${Date.now()}`) : null;
        const upUrl = extra.upscale_path ? pathToUrl(extra.upscale_path) : null;
        return {
          id: it.id,
          prompt: it.prompt || '',
          status: isDone ? 'done' : (isErr ? 'error' : 'pending'),
          output_url: outUrl,
          output_path: it.output_path || null,
          error: isErr ? (it.error_message || 'Lỗi') : null,
          error_detail: it.error_message || null,
          media_id: extra.media_id || null,
          upscale_status: extra.upscale_status || null,
          upscale_path: extra.upscale_path || null,
          upscale_url: upUrl,
          upscale_resolution: extra.upscale_resolution || null,
          extra: extra,
        };
      }),
      done: task.done_count || 0,
      error: task.error_count || 0,
      total: task.total_count || (items ? items.length : 0),
      error_message: null,
      flow_account_email: task.flow_account_email || null,
      created_at: task.created_at ? new Date(task.created_at.includes(' ') ? task.created_at.replace(' ', 'T') + 'Z' : task.created_at).getTime() : Date.now(),
    };
    if (!t.idea) {
      for (const it of t.items) {
        if (it.extra && it.extra.idea) {
          t.idea = it.extra.idea;
          break;
        }
      }
    }
    tasks.set(task.id, t);
    persist();
    notify(task.id);
    return t;
  },
};

// ── WebSocket → store reducer ──────────────────────────────
//
// Backend events carry `item_id` (the DB primary key). The store's items
// were created by the frontend without ids — so the FIRST event we see for
// a given item_id claims the next empty slot. Subsequent events for the
// same id reuse that slot. This works for any ordering, including parallel
// generation where items finish out of order.

function pathToUrl(p) {
  if (!p) return null;
  const s = String(p).replace(/\\/g, '/');
  const i = s.indexOf('outputs/');
  return i >= 0 ? `/files/${s.slice(i + 'outputs/'.length)}` : `/files/${s.split('/').pop()}`;
}

function findOrClaimSlot(t, itemId) {
  // Already claimed?
  for (const it of t.items) {
    if (it.id === itemId) return it;
  }
  // Claim first slot without an id
  for (const it of t.items) {
    if (it.id == null) {
      it.id = itemId;
      return it;
    }
  }
  return null;
}

ws.on('task_started', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  log('task_started', d.task_id, 'in store?', !!t);
  if (t) {
    t.status = 'running';
    notify(d.task_id);
  }
});

ws.on('task_paused', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (t) {
    t.status = 'paused';
    notify(d.task_id);
  }
});

ws.on('item_status', (d) => {
  if (!d || !d.task_id || d.item_id == null) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  const it = findOrClaimSlot(t, d.item_id);
  if (!it) return;
  // Pair prompt↔item by item_id (storyboard: concurrent gen can claim slots
  // out of order; the event carries the correct prompt for this id).
  if (d.prompt != null && d.prompt !== '') it.prompt = d.prompt;
  // Map backend ItemStatus → UI status. Anything that's not done/error is
  // treated as "generating" while the item is moving through stages.
  const s = (d.status || '').toUpperCase();
  if (s === 'COMPLETED') it.status = 'done';
  else if (s === 'ERROR') it.status = 'error';
  else it.status = 'generating';
  notify(d.task_id);
});

ws.on('item_completed', (d) => {
  if (!d || !d.task_id || d.item_id == null) return;
  const t = tasks.get(d.task_id);
  log('item_completed', d.task_id, 'in store?', !!t);
  if (!t) return;
  const it = findOrClaimSlot(t, d.item_id);
  if (!it) return;
  if (d.prompt != null && d.prompt !== '') it.prompt = d.prompt;
  // Only bump counter when transitioning to done
  if (it.status !== 'done') t.done += 1;
  it.status = 'done';
  // Cache-bust the URL: a regen-from-done overwrites the file at the SAME
  // path, so without a unique query the browser would show the cached old
  // image. /files/ StaticFiles ignores the query string.
  it.output_url = pathToUrl(d.output_path) + (d.output_path ? `?v=${Date.now()}` : '');
  it.output_path = d.output_path;
  // Carry media_id through so the gallery's upscale button (2K/4K) can
  // pass it to the backend without an extra DB lookup.
  if (d.media_id) it.media_id = d.media_id;
  notify(d.task_id);
});

// ── Upscale (2K / 4K) events ──────────────────────────────
// Per-item flags drive the card badges (queued → running → done/error);
// `upscaleBatch` drives the toolbar "Đang upscale 2/5". Upscale carries no
// task_id, so we look the item up across all tasks.
function _findItemById(itemId) {
  for (const t of tasks.values()) {
    const it = t.items.find(x => x.id === itemId);
    if (it) return { task: t, item: it };
  }
  return null;
}

ws.on('upscale_batch_started', (d) => {
  upscaleBatch = {
    active: true,
    total: (d && d.total) || 0,
    done: 0,
    running: 0,
    resolution: (d && d.resolution) || '',
    taskId: upscaleBatch.taskId,   // keep the gallery scope set by markUpscaleQueued
  };
  for (const t of tasks.values()) notify(t.id);
});

ws.on('upscale_started', (d) => {
  if (!d || d.item_id == null) return;
  upscaleBatch.active = true;
  if (typeof d.index === 'number') upscaleBatch.running = d.index;
  if (typeof d.total === 'number') upscaleBatch.total = d.total;
  if (d.resolution) upscaleBatch.resolution = d.resolution;
  const hit = _findItemById(d.item_id);
  if (!hit) return;
  upscaleBatch.taskId = hit.task.id;   // authoritative gallery scope
  hit.item.upscale_status = 'running';
  hit.item.upscale_resolution = d.resolution;
  hit.item.upscale_error = null;
  notify(hit.task.id);
});

ws.on('upscale_completed', (d) => {
  if (!d || d.item_id == null) return;
  if (typeof d.index === 'number') upscaleBatch.done = d.index;
  const hit = _findItemById(d.item_id);
  if (!hit) return;
  upscaleBatch.taskId = hit.task.id;
  hit.item.upscale_status = 'done';
  hit.item.upscale_url = d.url;
  hit.item.upscale_path = d.path;
  notify(hit.task.id);
});

ws.on('upscale_error', (d) => {
  if (!d || d.item_id == null) return;
  if (typeof d.index === 'number') upscaleBatch.done = d.index;
  const hit = _findItemById(d.item_id);
  if (!hit) return;
  hit.item.upscale_status = 'error';
  hit.item.upscale_error = d.error || 'Lỗi upscale';
  notify(hit.task.id);
});

ws.on('upscale_batch_done', () => {
  upscaleBatch.active = false;
  upscaleBatch.taskId = null;
  // Clear any item still 'queued' (batch may have aborted early, e.g. on a
  // dead session) so no card is stuck showing "Chờ upscale". Notify EVERY
  // task unconditionally so UI tied to the batch state — e.g. the disabled
  // "Xóa danh sách" button — refreshes even when nothing was left queued.
  for (const t of tasks.values()) {
    for (const it of t.items) {
      if (it.upscale_status === 'queued') it.upscale_status = null;
    }
    notify(t.id);
  }
});

// ── Watermark removal events ──────────────────────────────
// Backend doesn't carry item_id (path-based). Look up by matching output_path:
// the toolbar passes the gallery's selected file paths through, and the
// backend echoes them in `src` so we can find the right item to update.
function _findItemByPath(srcPath) {
  if (!srcPath) return null;
  // Match by EXACT basename. The backend sends a bare filename for progress
  // (src.name) but a full absolute path for completion (str(src)); the old
  // loose endsWith matched the WRONG item for numeric names — "16.mp4" ends
  // with "6.mp4" — so completion mis-routed and finished videos never flipped
  // to "done", leaving the whole processed prefix stuck showing "Xóa WM …%".
  const base = String(srcPath).replace(/\\/g, '/').split('/').pop();
  if (!base) return null;
  for (const t of tasks.values()) {
    for (const it of t.items) {
      const op = (it.output_path || '').replace(/\\/g, '/').split('/').pop();
      if (op && op === base) {
        return { task: t, item: it };
      }
    }
  }
  return null;
}

ws.on('watermark_progress', (d) => {
  if (!d || !d.source) return;
  const hit = _findItemByPath(d.source);
  if (!hit) return;
  hit.item.wm_status = 'running';
  hit.item.wm_label = d.status || '…';
  hit.item.wm_progress = d.progress || 0;
  notify(hit.task.id);
});

ws.on('watermark_item_completed', (d) => {
  if (!d || !d.src) return;
  const hit = _findItemByPath(d.src);
  if (!hit) return;
  hit.item.wm_status = 'done';
  hit.item.wm_url = d.url;
  hit.item.wm_path = d.path;
  hit.item.wm_label = '✓ Đã xóa watermark';
  hit.item.wm_progress = 100;
  notify(hit.task.id);
});

ws.on('watermark_item_error', (d) => {
  if (!d || !d.src) return;
  const hit = _findItemByPath(d.src);
  if (!hit) return;
  hit.item.wm_status = 'error';
  hit.item.wm_error = d.error || 'Lỗi xóa watermark';
  notify(hit.task.id);
});

ws.on('item_error', (d) => {
  if (!d || !d.task_id || d.item_id == null) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  const it = findOrClaimSlot(t, d.item_id);
  if (!it) return;
  if (it.status !== 'error') t.error += 1;
  it.status = 'error';
  it.error = d.error || 'Lỗi không xác định';
  // Raw error string (Google JSON / stack) for the tooltip — friendly
  // message is in `it.error`, full detail rides along here.
  it.error_detail = d.error_detail || null;
  notify(d.task_id);
});

ws.on('task_progress', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  if (typeof d.done === 'number') t.done = d.done;
  if (typeof d.error === 'number') t.error = d.error;
  if (typeof d.total === 'number') t.total = d.total;
  notify(d.task_id);
});

ws.on('task_completed', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  log('task_completed', d.task_id, 'in store?', !!t);
  if (!t) return;
  t.status = 'completed';
  if (typeof d.done === 'number') t.done = d.done;
  if (typeof d.error === 'number') t.error = d.error;
  notify(d.task_id);
});

ws.on('task_error', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  t.status = 'error';
  t.error_message = d.error || null;
  notify(d.task_id);
});

ws.on('task_cancelled', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  t.status = 'cancelled';
  notify(d.task_id);
});

// Circuit breaker tripped — Google flagged the session and the backend
// stopped processing further items. Surface a sticky warning on the task
// so the gallery can render a clear banner, and pop a toast once.
ws.on('task_circuit_tripped', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  t.circuit_tripped = true;
  t.circuit_message = d.message
    || `${d.threshold || 3} items liên tiếp bị 403 — đã pause task. Bấm 'Gen lại N lỗi' để retry.`;
  notify(d.task_id);
  // One-shot toast — only on the transition. Backend broadcasts this
  // event exactly once per task lifecycle.
  try {
    // Use a lazy import to avoid a circular toast/tasks_store loop at
    // module load. window.toast() exists if ui.js has been loaded.
    import('./ui.js').then(({ toast }) => {
      toast(t.circuit_message, 'warning', 12000);
    });
  } catch (_) {}
});

// Task is sleeping between batches — surface a temporary status flag so
// the gallery can render a "Đang nghỉ Xs" chip. Cleared on next
// task_progress or task_completed event.
ws.on('task_batch_cooldown', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  t.batch_cooldown = {
    seconds: d.seconds,
    min: d.min,
    max: d.max,
    batch_done: d.batch_done,
    batch_total: d.batch_total,
    started_at: Date.now(),
  };
  notify(d.task_id);
  // Auto-clear after the cooldown window. Backend will send another
  // task_progress soon afterward anyway (next batch starts), but the
  // timeout guards against edge cases (network drop, etc.) leaving the
  // chip stuck.
  const ms = Math.ceil((d.seconds || 0) * 1000) + 500;
  setTimeout(() => {
    const tt = tasks.get(d.task_id);
    if (tt && tt.batch_cooldown && tt.batch_cooldown.started_at === t.batch_cooldown.started_at) {
      tt.batch_cooldown = null;
      notify(d.task_id);
    }
  }, ms);
});

// Long-video specific
// Long-video scene events. Each handler recomputes the task-level `error`
// count from item statuses — the "Gen lại lỗi" button keys off `t.error`, so
// without this a failed scene never surfaces the retry button (and a resolved
// one never hides it).
function _recountErrors(t) {
  t.error = t.items.filter(it => it.status === 'error').length;
}

ws.on('scene_started', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  const idx = (d.scene || 1) - 1;
  if (t.items[idx]) {
    t.items[idx].status = 'generating';
    t.items[idx].error = null;
    _recountErrors(t);
    notify(d.task_id);
  }
});

ws.on('scene_done', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  const idx = (d.scene || 1) - 1;
  if (t.items[idx]) {
    t.items[idx].status = 'done';
    t.items[idx].output_url = pathToUrl(d.output_path);
    t.items[idx].output_path = d.output_path;
    _recountErrors(t);
    notify(d.task_id);
  }
});

ws.on('scene_failed', (d) => {
  if (!d || !d.task_id) return;
  const t = tasks.get(d.task_id);
  if (!t) return;
  const idx = (d.scene || 1) - 1;
  if (t.items[idx]) {
    t.items[idx].status = 'error';
    t.items[idx].error = d.error || 'Lỗi';
    _recountErrors(t);
    notify(d.task_id);
  }
});

// Expose for manual debugging from devtools console
window.__tasksStore = tasksStore;
log('initialized — call window.__tasksStore.list() to inspect');
