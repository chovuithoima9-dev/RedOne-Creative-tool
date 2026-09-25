// Image generation page (Imagen / Nano Banana via Google Labs)
// State is persisted in tasksStore so the gallery survives page navigation.
import { el, clear, toast, setLoading, icon, makeThumbnail, ensureFlowAccountOrWarn, openMediaViewer, openCompareViewer } from '../ui.js';
import { api } from '../api.js';
import { tasksStore } from '../tasks_store.js';
import { makeSelectionToolbar, attachCardCheckbox, makeRetryFailedButton, makePromptEditButton, makeItemRetryButton } from '../gallery_actions.js';

// ── Per-form state that survives navigation (singleton, module-level) ──
const form = {
  prompts: [''],
  model: 'nano_banana_pro',
  aspect: '1:1',
  countPerPrompt: 1,
  concurrent: 1,
  taskName: '',
  refImagePaths: [],
  refImagePreviews: [],   // [{ name, url }]  — preserved across re-renders
};

// Last task the user was actually viewing on this page (survives SPA
// navigation since it's module-level). On re-mount we restore THIS task
// instead of jumping to the newest one — prevents the view auto-switching to
// a different task (e.g. one being upscaled/regenerated) when you leave and
// come back. Only explicit actions (new task / eye-icon) change it.
let _lastViewedTaskId = null;

function defaultTaskName() {
  const d = new Date();
  const ts = `${d.getHours().toString().padStart(2, '0')}h${d.getMinutes().toString().padStart(2, '0')}m${d.getSeconds().toString().padStart(2, '0')}`;
  return `image_${ts}`;
}

function applySavedDefaults() {
  if (form._initialized) return;
  form._initialized = true;
  const s = window.__app?.store?.settings || {};
  // Image page only uses aspect from settings (its model dropdown is independent
  // of video quality presets)
  if (s.default_aspect) form.aspect = s.default_aspect;
}

// Open the before/after comparison for a Flow-upscaled item (original gen vs
// the 2K/4K result), with a download button inside the viewer.
function openFlowUpscaleCompare(it) {
  if (!it.upscale_url || !it.output_url) return;
  const res = (it.upscale_resolution || '').toUpperCase();
  openCompareViewer({
    beforeUrl: it.output_url,
    afterUrl: it.upscale_url,
    beforeLabel: 'Gốc',
    afterLabel: res || 'Upscale',
    downloadUrl: it.upscale_url,
    title: `${res ? res + ' · ' : ''}${it.prompt || ''}`.trim(),
  });
}

// Build the upscale-state pill overlaid on a card's thumbnail. Returns an
// element for the current `upscale_status` (queued | running | done | error).
// Done opens a before/after comparison viewer; the rest are plain pills.
function _upscaleBadge(it) {
  const res = (it.upscale_resolution || '').toUpperCase();
  switch (it.upscale_status) {
    case 'queued':
      return el('div', { class: 'upscale-badge is-queued', title: 'Đang chờ tới lượt upscale' },
        `⏳ Chờ ${res}`);
    case 'running':
      return el('div', { class: 'upscale-badge is-running' },
        el('span', { class: 'upscale-badge-spin' }),
        `Đang upscale ${res}…`);
    case 'done':
      return el('a', {
        class: 'upscale-badge is-done',
        href: '#',
        title: `Ảnh ${res} — click để so sánh trước/sau`,
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); openFlowUpscaleCompare(it); },
      }, `✓ ${res}`);
    case 'error':
      return el('div', { class: 'upscale-badge is-error', title: it.upscale_error || 'Lỗi upscale' },
        `⚠ Lỗi ${res}`);
    default:
      return el('span');
  }
}

// Send selected generated images to the Tạo Video tab as I2V (each image =
// a video's first frame, its gen prompt becomes the video prompt). Prefers
// the upscaled file when a 2K/4K upscale finished.
function sendImagesToI2V(taskState, idList) {
  const byId = new Map(taskState.items.map(it => [it.id, it]));
  const chosen = idList.map(id => byId.get(id)).filter(it => it && it.status === 'done' && it.output_path);
  const scenes = chosen.map(it => {
    const up = it.upscale_status === 'done' && it.upscale_path;
    return {
      prompt: it.prompt || '',
      path: up ? it.upscale_path : it.output_path,
      url: up ? it.upscale_url : it.output_url,
      name: 'image',
    };
  });
  if (!scenes.length) return toast('Chọn ảnh đã xong để gửi', 'warning');
  const upN = chosen.filter(it => it.upscale_status === 'done' && it.upscale_path).length;
  sessionStorage.setItem('inject_i2v', JSON.stringify(scenes));
  window.__app.navigate('content');
  toast(`Đã gửi ${scenes.length} ảnh sang Tạo Video (I2V)${upN ? ` (${upN} ảnh đã upscale)` : ''}`, 'success');
}

// "Gửi sang Upscale Shakker" (green button): hand the selected images to the
// Shakker tab as local urls and switch IMMEDIATELY — the upload to Shakker runs
// in the Upscale panel (each item shows "Đang tải lên…"), so we never block the
// tab switch on uploads.
function sendImagesToUpscale(taskState, idList) {
  const byId = new Map(taskState.items.map(it => [it.id, it]));
  const chosen = idList.map(id => byId.get(id)).filter(it => it && it.status === 'done' && it.output_path);
  if (!chosen.length) return toast('Chọn ảnh đã xong để gửi', 'warning');
  // Always send the ORIGINAL (output_url) to Shakker upscale — never the Flow
  // 2K/4K, so the user re-upscales the source image, not an already-upscaled one.
  const out = chosen.map(it => ({
    url: it.output_url, label: (it.prompt || 'ảnh').slice(0, 40), needsUpload: true,
  })).filter(x => x.url);
  if (!out.length) return toast('Ảnh chưa sẵn sàng', 'warning');
  window.__app._pendingUpscale = out;
  window.__app.navigate('shakker');
  toast(`Đã gửi ${out.length} ảnh sang Upscale Shakker (đang tải lên…)`, 'success');
}

export function renderImage(root) {
  applySavedDefaults();

  root.appendChild(el('div', { class: 'page-hero' },
    el('div', { class: 'hero-icon' }, icon('image', 22)),
    el('div', { class: 'hero-text' },
      el('h2', null, 'Tạo Ảnh AI'),
      el('p', null, 'Sinh ảnh bằng Nano Banana / Imagen qua Google Labs, hàng loạt + reference'),
    ),
  ));

  const layout = el('div', { class: 'gen-layout' });
  root.appendChild(layout);

  // ── LEFT: config ──────────────────────────────────────
  const left = el('div', { class: 'gen-config' },
    el('div', { class: 'card' },
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Tên task'),
        el('input', { class: 'input', id: 'img-taskname',
          placeholder: 'vd: chan_dung_studio' }),
        el('div', { class: 'field-help' },
          'File sẽ lưu tại outputs/image/<ngày>/<tên_task>/'),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Model'),
        el('select', { class: 'select', id: 'img-model' },
          el('option', { value: 'nano_banana_pro' }, 'Nano Banana Pro (mới nhất)'),
          el('option', { value: 'nano_banana_2' }, 'Nano Banana 2'),
          el('option', { value: 'nano_banana_lite' }, 'Nano Banana Lite (nhanh)'),
        ),
      ),
      el('div', { class: 'form-row' },
        el('div', { class: 'field-group' },
          el('label', { class: 'field-label' }, 'Tỉ lệ'),
          el('select', { class: 'select', id: 'img-aspect' },
            el('option', { value: '1:1' }, '1:1 (vuông)'),
            el('option', { value: '16:9' }, '16:9 (ngang)'),
            el('option', { value: '9:16' }, '9:16 (dọc)'),
            el('option', { value: '4:3' }, '4:3'),
            el('option', { value: '3:4' }, '3:4'),
          ),
        ),
        el('div', { class: 'field-group' },
          el('label', { class: 'field-label' }, 'Số ảnh / prompt'),
          el('input', { type: 'number', class: 'input', id: 'img-count', value: form.countPerPrompt, min: 1, max: 8 }),
        ),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Số luồng song song'),
        el('input', { type: 'number', class: 'input', id: 'img-concurrent', value: form.concurrent, min: 1, max: 8 }),
        el('div', { class: 'field-help' },
          'Số ảnh tạo đồng thời. 2-3 là tốt nhất, cao hơn dễ bị reCAPTCHA 403.'),
      ),
      el('div', { class: 'field-group' },
        el('label', {
          class: 'field-label',
          style: { display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer' },
        },
          el('label', { class: 'toggle' },
            el('input', { type: 'checkbox', id: 'img-wm-remove' }),
            el('span', { class: 'toggle-track' }),
          ),
          el('span', null, 'Tự xóa watermark Gemini trên ảnh'),
        ),
        el('div', { class: 'field-help' },
          'Xóa logo ✦ Gemini ở góc ảnh ngay khi tạo xong (Nano Banana). Mặc định bật.'),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Ảnh tham chiếu (nhân vật / phong cách)'),
        el('div', { class: 'dropzone', id: 'img-dropzone' },
          el('div', { class: 'dropzone-icon' }, icon('image', 22)),
          el('div', null, 'Kéo thả nhiều ảnh hoặc click'),
          el('div', { class: 'field-help' }, 'Tùy chọn — model sẽ tham chiếu phong cách / nhân vật. Tối đa 10 ảnh.'),
          el('input', { type: 'file', accept: 'image/*', multiple: 'true', id: 'img-ref-file', style: { display: 'none' } }),
        ),
        // Header strip: count + clear-all button. Visible only when ref
        // list is non-empty (handled in refreshRefHeader()).
        el('div', {
          id: 'img-ref-header',
          style: {
            display: 'none',
            justifyContent: 'space-between', alignItems: 'center',
            marginTop: '8px', padding: '0 2px',
          },
        }),
        el('div', { id: 'img-ref-list', style: { marginTop: '6px', display: 'flex', flexWrap: 'wrap', gap: '6px' } }),
      ),
      el('div', { style: { display: 'flex', gap: '8px', marginTop: '20px' } },
        el('button', { class: 'btn btn-primary', style: { flex: 1 }, id: 'img-start' },
          icon('sparkles'), el('span', null, 'Tạo ảnh'),
        ),
        el('button', { class: 'btn btn-danger hidden', id: 'img-cancel' },
          icon('stop'), el('span', null, 'Hủy'),
        ),
      ),
    ),
  );
  layout.appendChild(left);

  // Restore form select values
  left.querySelector('#img-model').value = form.model;
  left.querySelector('#img-aspect').value = form.aspect;
  left.querySelector('#img-concurrent').value = form.concurrent;
  const nameInput = left.querySelector('#img-taskname');
  nameInput.value = form.taskName || defaultTaskName();
  nameInput.addEventListener('input', (e) => { form.taskName = e.target.value; });

  // Watermark-removal toggle (default ON): reflect the saved value + persist on
  // change. Lives here (Tạo ảnh tab), not in Settings, per product decision.
  const wmCb = root.querySelector('#img-wm-remove');
  if (wmCb) {
    api.settings.get().then(r => {
      const v = r?.settings?.auto_remove_image_watermark;
      wmCb.checked = (v === undefined ? true : !!v);
    }).catch(() => { wmCb.checked = true; });
    wmCb.addEventListener('change', async () => {
      try {
        await api.settings.update({ auto_remove_image_watermark: wmCb.checked });
        toast(wmCb.checked ? 'Đã bật tự xóa watermark Gemini' : 'Đã tắt tự xóa watermark Gemini', 'success');
      } catch (err) { toast(err.message || 'Lỗi lưu cài đặt', 'error'); }
    });
  }

  // ── RIGHT: prompts + gallery ──────────────────────────
  const right = el('div', { class: 'gen-results' });
  layout.appendChild(right);

  const promptsCard = el('div', { class: 'card', style: { marginBottom: '16px' } },
    el('div', { class: 'card-header' },
      el('div', null,
        el('h3', { class: 'card-title' }, 'Prompts'),
        el('div', { class: 'card-subtitle', id: 'img-count-label' }, '1 prompt'),
      ),
      el('div', { style: { display: 'flex', gap: '8px' } },
        el('button', { class: 'btn btn-sm btn-ghost', id: 'img-import' },
          icon('upload', 14), 'Import .txt',
        ),
        el('button', { class: 'btn btn-sm btn-danger', id: 'img-clear-prompts' },
          icon('trash', 14), 'Xóa tất cả',
        ),
        el('button', { class: 'btn btn-sm btn-primary', id: 'img-add' },
          icon('plus', 14), 'Thêm prompt',
        ),
      ),
    ),
    // Bulk paste — split by blank lines into separate prompts
    el('div', { style: {
      background: 'var(--bg-2)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--r-md)',
      padding: '12px',
      marginBottom: '12px',
    } },
      el('label', { class: 'field-label' },
        '⚡ Bulk prompt — paste nhanh nhiều prompts'),
      el('textarea', { class: 'textarea', id: 'img-bulk-prompt', rows: 4,
        placeholder: '• 1 đoạn → ghi đè danh sách bằng 1 prompt\n• Nhiều đoạn cách nhau bằng dòng trắng → mỗi đoạn = 1 prompt riêng\n\nVí dụ:\n\nchân dung phụ nữ, ánh sáng vàng\n\ntoàn cảnh núi, sương mù\n\ncận cảnh hoa hồng đỏ' }),
      el('div', { style: { display: 'flex', gap: '8px', marginTop: '8px', alignItems: 'center' } },
        el('button', { class: 'btn btn-sm btn-primary', id: 'img-bulk-apply' },
          icon('check', 14), 'Áp dụng'),
        el('div', { class: 'field-help', id: 'img-bulk-help', style: { margin: 0 } },
          'Nhập 1 hoặc nhiều prompts'),
      ),
    ),
    el('div', { class: 'prompt-list', id: 'img-list' }),
  );
  right.appendChild(promptsCard);

  // Helpers for bulk-paste
  function splitBulkPrompts(text) {
    return (text || '')
      .split(/\n\s*\n+/)
      .map(s => s.trim())
      .filter(Boolean);
  }
  function refreshImgBulkHelp() {
    const help = root.querySelector('#img-bulk-help');
    const ta = root.querySelector('#img-bulk-prompt');
    if (!help || !ta) return;
    const blocks = splitBulkPrompts(ta.value);
    if (blocks.length >= 2) {
      help.textContent = `→ phát hiện ${blocks.length} đoạn — sẽ thay thế danh sách bằng ${blocks.length} prompts`;
      help.style.color = 'var(--brand)';
    } else if (blocks.length === 1) {
      help.textContent = '→ ghi đè danh sách bằng 1 prompt này';
      help.style.color = 'var(--text-muted)';
    } else {
      help.textContent = 'Nhập 1 hoặc nhiều prompts';
      help.style.color = 'var(--text-muted)';
    }
  }

  const resultsCard = el('div', { class: 'card' },
    el('div', { class: 'card-header' },
      el('div', null,
        el('h3', { class: 'card-title', id: 'img-results-title' }, 'Ảnh kết quả'),
        el('div', { class: 'card-subtitle', id: 'img-status' }, 'Chưa có job'),
      ),
      el('div', { id: 'img-header-actions', style: { display: 'flex', gap: '8px' } },
        el('button', {
          class: 'btn btn-sm btn-danger hidden',
          id: 'img-clear-all',
          title: 'Xóa toàn bộ danh sách (huỷ tác vụ đang chạy, file vẫn còn trên ổ đĩa)',
          onclick: clearCurrentTask,
        }, icon('trash', 14), 'Xóa danh sách'),
      ),
    ),
    el('div', { id: 'img-results' },
      el('div', { class: 'empty' },
        el('div', { class: 'empty-icon' }, icon('image', 28)),
        el('div', null, 'Sinh ảnh để xem ở đây'),
      ),
    ),
  );
  right.appendChild(resultsCard);

  // Run upscale on selected items. Backend processes sequentially and
  // streams WS events (`upscale_started` / `_completed` / `_error`) which
  // tasks_store translates into per-item `upscale_status` flags — the
  // gallery chips re-render automatically via the normal notify path.
  // Upscale now runs as a QUEUED TASK (pausable/cancellable in "Quản lý task").
  // The call returns immediately with a task_id; per-card badges + the toolbar
  // "Đang upscale N/M" update live via WS as the task runs.
  async function runBatchUpscale(itemIds, resolution) {
    const RES = resolution.toUpperCase();
    await api.image.upscaleBatch(itemIds, resolution);
    toast(
      `Đã thêm ${itemIds.length} ảnh vào hàng đợi upscale → ${RES}. `
      + `Vào "Quản lý task" để Tạm dừng / Tiếp tục / Hủy.`,
      'success',
    );
  }

  async function clearCurrentTask() {
    const tid = currentTaskId();
    if (!tid) return;
    // Block while a 2K/4K upscale batch is running — clearing now would cancel
    // the task and tear down the gallery the upscale is still writing into.
    if (tasksStore.getUpscaleBatch().active) {
      toast('Đang upscale 2K/4K — không thể xóa danh sách. Đợi upscale xong đã.', 'warning');
      return;
    }
    // Best-effort cancel if still running
    try {
      const t = tasksStore.get(tid);
      if (t && t.status === 'running') {
        await api.image.cancel(tid).catch(() => {});
      }
    } catch (e) { /* ignore */ }
    tasksStore.remove(tid);
    _currentTaskId = null;
    renderTaskGallery(null);
    toast('Đã xóa danh sách (file vẫn còn trên ổ đĩa)', 'info');
  }

  // ── Prompts list rendering ────────────────────────────
  const list = root.querySelector('#img-list');
  const countLabel = root.querySelector('#img-count-label');
  function refreshList() {
    // Preserve column scroll — see content.js refreshList for the rationale
    // (clear() collapses height → scrollTop clamps to 0 → page jumps to top).
    const _scroller = list.closest('.gen-config, .gen-results');
    const _scrollTop = _scroller ? _scroller.scrollTop : 0;
    clear(list);
    form.prompts.forEach((p, i) => {
      const row = el('div', { class: 'prompt-row' },
        el('div', { class: 'row-number' }, String(i + 1)),
        el('textarea', {
          class: 'textarea',
          rows: 2,
          placeholder: 'Mô tả ảnh muốn tạo... (English work best)',
          oninput: (e) => { form.prompts[i] = e.target.value; },
          style: { flex: 1, minHeight: '44px' },
        }, p),
        el('div', { class: 'row-actions' },
          form.prompts.length > 1
            ? el('button', { class: 'btn btn-icon btn-ghost', title: 'Xóa', onclick: () => {
                form.prompts.splice(i, 1); refreshList();
              } }, icon('trash', 14))
            : null,
        ),
      );
      row.querySelector('textarea').value = p;
      list.appendChild(row);
    });
    countLabel.textContent = `${form.prompts.length} prompt${form.prompts.length > 1 ? 's' : ''}`;
    if (_scroller) _scroller.scrollTop = _scrollTop;
  }
  refreshList();

  root.querySelector('#img-add').addEventListener('click', () => {
    form.prompts.push(''); refreshList();
  });
  root.querySelector('#img-import').addEventListener('click', () => {
    const fi = document.createElement('input');
    fi.type = 'file'; fi.accept = '.txt';
    fi.onchange = async () => {
      if (!fi.files[0]) return;
      const text = await fi.files[0].text();
      const lines = text.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      if (lines.length === 0) return toast('File rỗng', 'warning');
      form.prompts = lines;
      refreshList();
      toast(`Đã import ${lines.length} prompts`, 'success');
    };
    fi.click();
  });

  // Bulk-paste: smart split by blank lines, replace prompts list
  root.querySelector('#img-bulk-prompt').addEventListener('input', refreshImgBulkHelp);
  root.querySelector('#img-bulk-apply').addEventListener('click', () => {
    const blocks = splitBulkPrompts(root.querySelector('#img-bulk-prompt').value);
    if (blocks.length === 0) return toast('Nhập prompt trước', 'warning');
    form.prompts = blocks.slice();
    refreshList();
    toast(`Đã thay thế danh sách bằng ${blocks.length} prompt${blocks.length > 1 ? 's' : ''}`, 'success');
  });

  // Clear all prompts (keep reference images)
  root.querySelector('#img-clear-prompts').addEventListener('click', async () => {
    const { confirm } = await import('../ui.js');
    if (!await confirm('Xóa tất cả prompts hiện có?', 'Xác nhận')) return;
    form.prompts = [''];
    refreshList();
    toast('Đã xóa toàn bộ prompts (ảnh tham chiếu vẫn còn)', 'info');
  });

  // Initial help text
  refreshImgBulkHelp();

  // ── Reference images ──────────────────────────────────
  const refDz = root.querySelector('#img-dropzone');
  const refFi = root.querySelector('#img-ref-file');
  const refListEl = root.querySelector('#img-ref-list');
  refDz.addEventListener('click', () => refFi.click());
  ['dragover', 'dragleave', 'drop'].forEach(ev => {
    refDz.addEventListener(ev, e => {
      e.preventDefault();
      refDz.classList.toggle('dragover', ev === 'dragover');
      if (ev === 'drop' && e.dataTransfer.files.length) {
        refFi.files = e.dataTransfer.files;
        handleRefFiles();
      }
    });
  });
  refFi.addEventListener('change', handleRefFiles);

  function appendRefThumb(thumb) {
    const node = el('div', { style: { position: 'relative' } },
      el('img', { src: thumb.url, style: {
        width: '64px', height: '64px', objectFit: 'cover',
        borderRadius: '8px', border: '1px solid var(--border)',
      } }),
      el('button', { class: 'btn btn-icon btn-ghost', style: {
        position: 'absolute', top: '-6px', right: '-6px',
        background: 'var(--red)', color: 'white', width: '20px', height: '20px',
        padding: 0, borderRadius: '50%',
      }, onclick: () => {
        const idx = form.refImagePaths.indexOf(thumb.path);
        if (idx >= 0) {
          form.refImagePaths.splice(idx, 1);
          form.refImagePreviews.splice(idx, 1);
        }
        node.remove();
        refreshRefHeader();
      } }, icon('x', 12)),
    );
    refListEl.appendChild(node);
    refreshRefHeader();
  }

  // Show/hide the "N ảnh — Xóa tất cả" header depending on whether the
  // list is empty. Rebuilt on each call so the count stays accurate.
  function refreshRefHeader() {
    const header = root.querySelector('#img-ref-header');
    if (!header) return;
    const count = form.refImagePreviews.length;
    if (count === 0) {
      header.style.display = 'none';
      header.innerHTML = '';
      return;
    }
    header.style.display = 'flex';
    clear(header);
    header.appendChild(el('span', { class: 'field-help', style: { fontWeight: 600, color: 'var(--text)' } },
      `${count} ảnh đã upload`));
    header.appendChild(el('button', {
      class: 'btn btn-sm btn-ghost',
      style: { color: 'var(--accent-red, #ef4444)' },
      title: 'Xóa tất cả ảnh tham chiếu',
      onclick: async () => {
        const { confirm } = await import('../ui.js');
        if (!await confirm(
          `Xóa tất cả ${count} ảnh tham chiếu?\n\nPrompts giữ nguyên — chỉ ảnh bị xóa.`,
          'Xác nhận xóa ảnh',
        )) return;
        form.refImagePaths = [];
        form.refImagePreviews = [];
        clear(refListEl);
        refreshRefHeader();
        toast(`Đã xóa ${count} ảnh`, 'info');
      },
    }, icon('trash', 12), 'Xóa tất cả'));
  }

  // Restore previously uploaded ref thumbs
  form.refImagePreviews.forEach(appendRefThumb);
  refreshRefHeader();

  // Google Labs ImageFX caps at ~10 reference images per request.
  // Beyond that the API rejects with cryptic errors; better to stop
  // the user up front with a clear toast.
  const REF_IMAGE_MAX = 10;

  async function handleRefFiles() {
    const incoming = Array.from(refFi.files);
    const room = Math.max(0, REF_IMAGE_MAX - form.refImagePreviews.length);

    if (room === 0) {
      toast(`Đã đủ ${REF_IMAGE_MAX} ảnh tham chiếu — xóa bớt trước khi thêm`, 'warning', 6000);
      refFi.value = '';
      return;
    }

    let accepted = incoming;
    let dropped = 0;
    if (incoming.length > room) {
      accepted = incoming.slice(0, room);
      dropped = incoming.length - room;
    }

    for (const file of accepted) {
      try {
        const r = await api.content.uploadImage(file);
        const url = await makeThumbnail(file);
        const thumb = { path: r.path, url, name: file.name };
        form.refImagePaths.push(r.path);
        form.refImagePreviews.push(thumb);
        appendRefThumb(thumb);
      } catch (e) {
        toast(`Upload lỗi: ${e.message}`, 'error');
      }
    }

    if (dropped > 0) {
      toast(
        `Chỉ nhận tối đa ${REF_IMAGE_MAX} ảnh — đã bỏ ${dropped} ảnh thừa`,
        'warning',
        6000,
      );
    }
    refFi.value = '';
  }

  // ── Start / cancel ────────────────────────────────────
  const startBtn = root.querySelector('#img-start');
  const cancelBtn = root.querySelector('#img-cancel');

  startBtn.addEventListener('click', async () => {
    const prompts = form.prompts.map(p => (p || '').trim()).filter(Boolean);
    if (prompts.length === 0) return toast('Cần ít nhất 1 prompt', 'warning');
    if (!(await ensureFlowAccountOrWarn())) return;
    form.model = root.querySelector('#img-model').value;
    form.aspect = root.querySelector('#img-aspect').value;
    form.countPerPrompt = parseInt(root.querySelector('#img-count').value || '1', 10);
    form.concurrent = parseInt(root.querySelector('#img-concurrent').value || '1', 10);

    // Expand prompts × count_per_prompt (matches backend)
    const expanded = [];
    for (const p of prompts) {
      for (let k = 0; k < form.countPerPrompt; k++) expanded.push(p);
    }

    let taskName = (form.taskName || '').trim();
    if (!taskName) { taskName = defaultTaskName(); nameInput.value = taskName; }
    setLoading(startBtn, true);
    try {
      const res = await api.image.start({
        prompts,
        model: form.model,
        aspect_ratio: form.aspect,
        count_per_prompt: form.countPerPrompt,
        concurrent: form.concurrent,
        reference_image_paths: form.refImagePaths.length ? form.refImagePaths : null,
        task_name: taskName,
      });
      // Register the task in the global store so it survives navigation
      tasksStore.register(res.task_id, 'image', {
        items: expanded,
        aspect: form.aspect,
        model: form.model,
        name: taskName,
      });
      attachToTask(res.task_id);
      toast(`Task #${res.task_id} (${res.items} ảnh)`, 'success');
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      setLoading(startBtn, false);
    }
  });

  cancelBtn.addEventListener('click', async () => {
    const t = currentTaskId();
    if (!t) return;
    try {
      await api.image.cancel(t);
      toast('Đã hủy', 'info');
    } catch (e) { toast(e.message, 'error'); }
  });

  // ── Gallery rendering (driven by tasksStore) ──────────
  function aspectThumbClass(aspect) {
    if (aspect === '9:16' || aspect === '3:4') return 'thumb-portrait';
    if (aspect === '1:1') return 'thumb-square';
    return '';
  }

  function renderTaskGallery(taskState) {
    // Skip if our DOM has been replaced by navigation — the WS callback may
    // fire after the user has navigated to another page.
    if (!root.isConnected) return;
    const wrap = root.querySelector('#img-results');
    if (!wrap) return;
    clear(wrap);

    // Show WHICH task these cards belong to (task name) so the user always
    // knows the current task — and notices if it ever changes.
    const titleEl = root.querySelector('#img-results-title');
    if (titleEl) {
      titleEl.textContent = taskState
        ? `Ảnh kết quả — ${taskState.name || ('Task #' + taskState.id)}`
        : 'Ảnh kết quả';
    }

    const clearBtn = root.querySelector('#img-clear-all');
    if (clearBtn) {
      clearBtn.classList.toggle('hidden', !taskState);
      // Disable while an upscale batch runs — re-enabled when it finishes
      // (upscale_batch_done re-renders the gallery).
      const upscaling = tasksStore.getUpscaleBatch().active;
      clearBtn.disabled = upscaling;
      clearBtn.title = upscaling
        ? 'Đang upscale 2K/4K — không thể xóa danh sách. Đợi upscale xong.'
        : 'Xóa toàn bộ danh sách (huỷ tác vụ đang chạy, file vẫn còn trên ổ đĩa)';
    }
    retryBtn.refresh(taskState);

    if (!taskState) {
      wrap.appendChild(el('div', { class: 'empty' },
        el('div', { class: 'empty-icon' }, icon('image', 28)),
        el('div', null, 'Sinh ảnh để xem ở đây'),
      ));
      root.querySelector('#img-status').textContent = 'Chưa có job';
      cancelBtn.classList.add('hidden');
      return;
    }

    const grid = el('div', { style: {
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
      gap: '14px',
    } });
    const aspectCls = aspectThumbClass(taskState.aspect);

    // Selection toolbar — shown once any item is done
    const hasAnyDone = taskState.items.some(it => it.status === 'done' && it.output_path);
    let toolbar = null;
    if (hasAnyDone) {
      toolbar = makeSelectionToolbar({
        getCards: () => [...grid.querySelectorAll('.scene-card[data-path]')],
        pathOf: (card) => card.dataset.path,
        // Return the LIVE store item so the toolbar sees media_id + upscale_status
        // (regen must skip cards mid-upscale). Falls back to a dataset shape.
        itemOf: (card) => {
          const id = parseInt(card.dataset.itemId || '0', 10);
          if (!id) return null;
          const t = tasksStore.get(taskState.id);
          return (t && t.items.find(x => x.id === id))
            || { id, mediaId: card.dataset.mediaId || null };
        },
        onUpscale: async (itemIds, resolution) => {
          await runBatchUpscale(itemIds, resolution);
        },
        onSendToI2V: async (ids) => sendImagesToI2V(taskState, ids),
        onSendToUpscale: async (ids) => sendImagesToUpscale(taskState, ids),
        onRegen: async (ids) => {
          await api.tasks.retryItems(taskState.id, ids);
          ids.forEach(iid => tasksStore.retryItemUI(taskState.id, iid, 'pending'));
        },
        onChange: () => renderTaskGallery(tasksStore.get(taskState.id)),
        onClearSelected: (paths) => {
          tasksStore.removeItemsByPath(taskState.id, paths);
          renderTaskGallery(tasksStore.get(taskState.id));
        },
      });
      wrap.appendChild(toolbar);
      // Reflect any in-progress 2K/4K batch ("Đang upscale 2/5"). The toolbar
      // is rebuilt on every store notify, so reading the batch here keeps it
      // live as upscale_started/_completed events arrive.
      if (toolbar._setUpscaleProgress) {
        // Only show progress for an upscale started from THIS gallery's task —
        // otherwise a Storyboard upscale would also show here (shared global).
        const ub = tasksStore.getUpscaleBatch();
        toolbar._setUpscaleProgress(ub.active && ub.taskId === taskState.id ? ub : null);
      }
    }

    taskState.items.forEach((it, i) => {
      const card = el('div', { class: 'scene-card' });
      const thumb = el('div', { class: `scene-thumb ${aspectCls}` },
        el('div', { class: 'scene-number' }, `#${i + 1}`),
      );

      if (it.status === 'done' && it.output_url) {
        const img = el('img', { src: it.output_url, loading: 'lazy', decoding: 'async', style: { width: '100%', height: '100%', objectFit: 'cover', cursor: 'zoom-in' } });
        img.addEventListener('click', () => {
          const galleryItems = taskState.items
            .filter(x => x.status === 'done' && x.output_url)
            .map((x, j) => ({ url: x.output_url, type: 'image', label: `#${j + 1} ${x.prompt || ''}`.trim() }));
          const doneIndex = taskState.items.slice(0, i + 1).filter(x => x.status === 'done' && x.output_url).length - 1;
          openMediaViewer({ items: galleryItems, currentIndex: doneIndex });
        });
        thumb.appendChild(img);
      } else if (it.status === 'error') {
        // Full friendly message + tooltip with raw detail (see content.js).
        thumb.appendChild(el('div', {
          class: 'scene-error',
          title: it.error_detail || it.error || 'Lỗi',
          style: {
            color: 'var(--red)', fontSize: '11px', padding: '8px',
            textAlign: 'center', lineHeight: '1.35',
            overflowY: 'auto', maxHeight: '100%',
          },
        }, it.error || 'Lỗi'));
      } else {
        thumb.appendChild(el('div', { class: 'spinner' }));
      }
      // Upscale (2K/4K) state overlaid on the thumbnail so the actions row
      // stays a clean single line: queued → running → done/error.
      if (it.upscale_status) {
        thumb.appendChild(_upscaleBadge(it));
        if (it.upscale_status === 'queued') card.classList.add('is-upscale-queued');
      }
      card.appendChild(thumb);
      if (it.upscale_status === 'running') {
        card.appendChild(el('div', { class: 'upscale-progress-bar' }));
      }

      const chipClass = it.status === 'done' ? 'chip-green'
                     : it.status === 'error' ? 'chip-red'
                     : it.status === 'generating' ? 'chip-blue'
                     : 'chip-yellow';
      const chipText = it.status === 'done' ? 'Hoàn thành'
                     : it.status === 'error' ? 'Lỗi'
                     : it.status === 'generating' ? 'Đang tạo'
                     : 'Đang chờ';

      const info = el('div', { class: 'scene-info' },
        el('div', { class: 'scene-meta' },
          el('span', { class: `chip ${chipClass}` }, chipText),
        ),
        el('div', { class: 'scene-prompt' }, it.prompt),
      );

      if (it.status === 'done' && it.output_url) {
        // Downloads grouped on the left; copy-prompt as a square icon on the
        // right. The upscaled (2K/4K) download is brand-tinted to stand out.
        const actions = el('div', { class: 'scene-actions' },
          el('a', { href: it.output_url, download: '', class: 'btn btn-sm btn-ghost', title: 'Tải ảnh gốc' },
            icon('download', 14), 'Tải'),
        );
        if (it.upscale_status === 'done' && it.upscale_url) {
          actions.appendChild(el('a', {
            href: it.upscale_url, download: '', target: '_blank',
            class: 'btn btn-sm btn-ghost sa-accent',
            title: 'Tải ảnh đã upscale (bấm chip trên ảnh để so sánh)',
          }, icon('download', 14), `Tải ${(it.upscale_resolution || '').toUpperCase()}`));
        }
        actions.appendChild(el('button', {
          class: 'btn btn-sm btn-ghost btn-icon', title: 'Copy prompt',
          style: { marginLeft: 'auto' },
          onclick: () => {
            const p = (it.prompt || '').trim();
            if (!p) return toast('Không có prompt để copy', 'warning');
            navigator.clipboard.writeText(p);
            toast('Đã copy prompt', 'success');
          },
        }, icon('copy', 14)));
        if (it.id != null) {
          actions.appendChild(makePromptEditButton({ taskId: taskState.id, item: it }));
        }
        info.appendChild(actions);
      } else if (it.status === 'error' && it.id != null) {
        const errorActions = el('div', { class: 'scene-actions' },
          makeItemRetryButton(taskState.id, it.id),
          el('button', {
            class: 'btn btn-sm btn-ghost btn-icon', title: 'Copy prompt',
            style: { marginLeft: 'auto' },
            onclick: () => {
              const p = (it.prompt || '').trim();
              if (!p) return toast('Không có prompt để copy', 'warning');
              navigator.clipboard.writeText(p);
              toast('Đã copy prompt', 'success');
            },
          }, icon('copy', 14)),
          makePromptEditButton({ taskId: taskState.id, item: it }),
        );
        info.appendChild(errorActions);
      }
      card.appendChild(info);
      // Attach checkbox only if file exists server-side
      if (it.status === 'done' && it.output_path) {
        // Stamp the item_id + media_id onto the card so the toolbar's upscale
        // handler can grab them without re-querying the store.
        if (it.id != null) card.dataset.itemId = String(it.id);
        if (it.media_id) card.dataset.mediaId = it.media_id;
        attachCardCheckbox(card, it.output_path, toolbar);
      }
      grid.appendChild(card);
    });

    wrap.appendChild(grid);

    const total = taskState.total || taskState.items.length;
    const done = taskState.done;
    const err = taskState.error;
    let statusText;
    if (taskState.status === 'running') {
      if (taskState.batch_cooldown) {
        const cd = taskState.batch_cooldown;
        const elapsed = (Date.now() - cd.started_at) / 1000;
        const remaining = Math.max(0, Math.ceil((cd.seconds || 0) - elapsed));
        statusText = `Đang nghỉ ${remaining}s giữa đợt gen (${cd.batch_done}/${cd.batch_total}) • ${done}/${total} xong`;
      } else {
        statusText = `Đang tạo ${done + err}/${total} • OK: ${done}, lỗi: ${err}`;
      }
      cancelBtn.classList.remove('hidden');
    } else if (taskState.status === 'completed') {
      statusText = `Hoàn tất: ${done} OK / ${err} lỗi`;
      cancelBtn.classList.add('hidden');
    } else if (taskState.status === 'error') {
      statusText = `Lỗi: ${taskState.error_message || ''}`;
      cancelBtn.classList.add('hidden');
    } else if (taskState.status === 'cancelled') {
      statusText = 'Đã hủy';
      cancelBtn.classList.add('hidden');
    } else {
      statusText = '—';
    }
    root.querySelector('#img-status').textContent = statusText;
  }

  // ── "Gen lại N lỗi" button ───────────────────────────
  // Created once; refresh() is called from renderTaskGallery() on every
  // state update. Prepended into the header actions container so it lands
  // left of "Xóa danh sách" — the most relevant action when items failed.
  const retryBtn = makeRetryFailedButton({
    getTaskState: () => {
      const id = currentTaskId();
      const t = id && tasksStore.get(id);
      return t ? { taskId: id, errorCount: t.error || 0, status: t.status } : null;
    },
    onResetUI: (id) => tasksStore.resetErrorItems(id),
  });
  root.querySelector('#img-header-actions').prepend(retryBtn);

  // ── Live subscription ────────────────────────────────
  let unsubscribe = null;
  let _currentTaskId = null;
  let _rafId = 0;           // coalesce re-renders to ~1 per frame (perf)
  function currentTaskId() { return _currentTaskId; }
  // Collapse the WS-event render storm (100+ items → hundreds of notifies)
  // into ONE rebuild per frame, reading the LATEST store state.
  function scheduleRender() {
    if (_rafId) return;
    _rafId = requestAnimationFrame(() => {
      _rafId = 0;
      if (!root.isConnected) return;
      renderTaskGallery(tasksStore.get(currentTaskId()));
    });
  }

  function attachToTask(taskId) {
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
    if (_rafId) { cancelAnimationFrame(_rafId); _rafId = 0; }
    _currentTaskId = taskId;
    _lastViewedTaskId = taskId;   // remember across re-mounts (sticky view)
    const t = tasksStore.get(taskId);
    renderTaskGallery(t);                          // immediate first paint
    unsubscribe = tasksStore.on(taskId, scheduleRender);
  }

  // Which task to show on mount, in priority order:
  //   1. eye-icon deep-link from Tasks Manager (explicit)
  //   2. the task the user was last viewing (sticky — don't auto-jump)
  //   3. the newest task of this kind (first ever open)
  const pending = window.__app && window.__app._pendingTaskId;
  if (pending != null && tasksStore.get(pending)) {
    attachToTask(pending);
    window.__app._pendingTaskId = null;
  } else if (_lastViewedTaskId != null && tasksStore.get(_lastViewedTaskId)) {
    attachToTask(_lastViewedTaskId);
  } else {
    const latest = tasksStore.latestByKind('image');
    if (latest) attachToTask(latest.id);
  }

  // Cleanup when navigated away. NOTE: `root` is the PERSISTENT #page-container
  // (navigate() only swaps its children), so `document.body.contains(root)` is
  // ALWAYS true — that guard never fired, so each visit leaked a live
  // tasksStore.on() subscription. A leaked mount's scheduleRender then kept
  // repainting #img-results with ITS task whenever that task got an item event
  // → the gallery "jumped" to another task. Detect unmount via our own marker.
  const obs = new MutationObserver(() => {
    if (!root.querySelector('#img-results')) {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      if (_rafId) { cancelAnimationFrame(_rafId); _rafId = 0; }
      obs.disconnect();
    }
  });
  obs.observe(document.body, { childList: true, subtree: true });

  // Inject any prompts sent from other pages
  const inject = sessionStorage.getItem('inject_prompts');
  if (inject) {
    try {
      const arr = JSON.parse(inject);
      if (Array.isArray(arr) && arr.length) {
        form.prompts = arr.filter(Boolean);
        refreshList();
        toast(`Đã import ${arr.length} prompt`, 'success');
      }
    } catch (e) { /* ignore */ }
    sessionStorage.removeItem('inject_prompts');
  }
}
