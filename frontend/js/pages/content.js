// Content page — T2V / I2V batch generation
// State persisted via tasksStore + module-level form so navigation doesn't reset.
import { el, clear, toast, setLoading, icon, makeThumbnail, makeLazyVideoObserver, ensureFlowAccountOrWarn, openMediaViewer } from '../ui.js';
import { api } from '../api.js';
import { tasksStore } from '../tasks_store.js';
import { makeSelectionToolbar, attachCardCheckbox, makeRetryFailedButton, makePromptEditButton, makeItemRetryButton } from '../gallery_actions.js';

// Form state survives navigation
const form = {
  mode: 't2v',
  prompts: [''],
  quality: 'fast',      // mặc định: Veo 3.1 Fast · 10 credit
  aspect: '16:9',
  resolution: '720p',
  duration: 8,
  concurrent: 1,
  videosPerPrompt: 1, // 1..4 — số video tạo cho mỗi prompt (biến thể khác seed)
  videoMode: 'start_image', // start_image | start_end_image | components
  loop: false,        // I2V "Loop video": dùng cùng 1 ảnh làm khung đầu + khung cuối
  taskName: '',
  // Per-prompt reference images (I2V): same index as prompts
  //   refs[i] = { path, previewUrl, name } | null
  refs: [],
};

// Last task the user was viewing (module-level → survives SPA navigation).
// Restored on re-mount so the view doesn't auto-jump to the newest task
// (e.g. one being upscaled/regenerated). Only explicit actions change it.
let _lastViewedTaskId = null;

// ⭐ favorite variant per prompt-group, for tasks with videosPerPrompt > 1.
// Keyed `${taskId}:${groupIdx}` → the chosen item's output_path. Purely UI:
// "Chọn tất cả" ticks only these (1 per prompt) so bulk actions (Xóa WM / Tải /
// Upscale) run on the one video the user liked, not every duplicate variant.
// Not persisted — lives for this session; a full reload falls back to defaults.
const _favByGroup = new Map();

// Duration options per model.
const DURATION_BY_MODEL = {
  omni_flash: [4, 6, 8, 10],
  lite_lp:    [4, 6, 8],
  lite:       [4, 6, 8],
  fast:       [4, 6, 8],
  quality:    [8],
};

// Active video models from Google Flow
const MODELS_FOR_MODE = {
  t2v: ['lite_lp', 'lite', 'fast', 'quality', 'omni_flash'],
  i2v: ['lite_lp', 'lite', 'fast', 'quality', 'omni_flash'],
};
// Labels for video models dropdown
const MODEL_LABELS = {
  lite_lp:    'Veo 3.1 Lite (Lower Priority · Miễn phí)',
  lite:       'Veo 3.1 – Lite',
  fast:       'Veo 3.1 – Fast',
  quality:    'Veo 3.1 – Quality',
  omni_flash: 'Omni 1.1 Flash',
};

function defaultTaskName(prefix = 'video') {
  const d = new Date();
  const ts = `${d.getHours().toString().padStart(2, '0')}h${d.getMinutes().toString().padStart(2, '0')}m${d.getSeconds().toString().padStart(2, '0')}`;
  return `${prefix}_${ts}`;
}

// Video supports only 16:9 and 9:16 (Google Veo limitation)
const VIDEO_ASPECTS = ['16:9', '9:16'];

function applySavedDefaults() {
  // Apply defaults from Settings page on first render only (so the user's
  // explicit choices later aren't clobbered).
  if (form._initialized) return;
  form._initialized = true;
  const s = window.__app?.store?.settings || {};
  if (s.default_quality) form.quality = s.default_quality;
  if (s.default_aspect && VIDEO_ASPECTS.includes(s.default_aspect)) {
    form.aspect = s.default_aspect;
  }
  // If form.aspect was set from previous session to an invalid value, clamp
  if (!VIDEO_ASPECTS.includes(form.aspect)) form.aspect = '16:9';
}

export function renderContent(root) {
  applySavedDefaults();

  const ui = el('div', null,
    el('div', { class: 'page-hero' },
      el('div', { class: 'hero-icon' }, icon('play', 28)),
      el('div', { class: 'hero-text' },
        el('h2', null, 'Tạo Video AI'),
        el('p', null, 'Sinh video từ prompt text hoặc ảnh tham chiếu, hàng loạt qua Google Veo API'),
      ),
    ),
    el('div', { class: 'gen-layout' },
      // LEFT
      el('div', { class: 'gen-config' },
        el('div', { class: 'card' },
          el('div', { class: 'tabs', style: { marginBottom: '16px' } },
            tabBtn('t2v', 'Text → Video'),
            tabBtn('i2v', 'Image → Video'),
          ),
          el('div', { class: 'field-group' },
            el('label', { class: 'field-label' }, 'Tên task'),
            el('input', { class: 'input', id: 'cnt-taskname',
              placeholder: 'vd: thien_nhien_4k' }),
            el('div', { class: 'field-help' },
              'File sẽ lưu tại outputs/video/<ngày>/<tên_task>/'),
          ),
          el('div', { class: 'field-group' },
            el('label', { class: 'field-label' }, 'Model'),
            // Options populated dynamically — Omni Flash hidden in I2V mode
            el('select', { class: 'select', id: 'cnt-quality' }),
            el('div', { class: 'field-help', id: 'cnt-quality-help' }),
          ),
          el('div', { class: 'form-row' },
            el('div', { class: 'field-group' },
              el('label', { class: 'field-label' }, 'Tỉ lệ'),
              el('select', { class: 'select', id: 'cnt-aspect' },
                option('16:9', '16:9 (ngang)'),
                option('9:16', '9:16 (dọc)'),
              ),
            ),
            el('div', { class: 'field-group' },
              el('label', { class: 'field-label' }, 'Độ phân giải'),
              el('select', { class: 'select', id: 'cnt-resolution' },
                option('720p', '720p HD'),
                option('1080p', '1080p Full HD'),
              ),
            ),
          ),
          el('div', { class: 'field-group' },
            el('label', { class: 'field-label' }, 'Độ dài video'),
            el('select', { class: 'select', id: 'cnt-duration' }),
            el('div', { class: 'field-help', id: 'cnt-duration-help' },
              'Chỉ Omni Flash hỗ trợ tùy chỉnh (4/6/8/10s). Veo 3.1 cố định 8s.'),
          ),
          el('div', { class: 'field-group', id: 'i2v-image-block' },
            el('label', { class: 'field-label' }, 'Ảnh tham chiếu (Image-to-Video)'),
            el('div', { class: 'dropzone', id: 'cnt-dropzone' },
              el('div', { class: 'dropzone-icon' }, icon('image', 24)),
              el('div', null, 'Kéo thả NHIỀU ảnh hoặc bấm để chọn'),
              el('div', { class: 'field-help' },
                'Ảnh 1 ↔ Prompt 1, Ảnh 2 ↔ Prompt 2... theo thứ tự upload'),
              el('input', { type: 'file', accept: 'image/*', multiple: 'true',
                id: 'cnt-image-file', style: { display: 'none' } }),
            ),
            el('div', { class: 'field-group', style: { marginTop: '12px' } },
              el('label', { class: 'field-label' }, 'Vị trí gán ảnh trong Flow'),
              el('select', { class: 'select', id: 'cnt-video-mode',
                onchange: (e) => {
                  form.videoMode = e.target.value;
                  const loopWrap = root.querySelector('#cnt-loop-wrap');
                  if (loopWrap) {
                    loopWrap.style.display = (form.videoMode === 'components') ? 'none' : 'flex';
                  }
                }
              },
                el('option', { value: 'start_image', ...(form.videoMode === 'start_image' ? { selected: 'true' } : {}) }, 'Khung hình đầu (Start Frame) — [Chuẩn I2V]'),
                el('option', { value: 'start_end_image', ...(form.videoMode === 'start_end_image' ? { selected: 'true' } : {}) }, 'Khung hình đầu & cuối (First - Last Frame)'),
                el('option', { value: 'components', ...(form.videoMode === 'components' ? { selected: 'true' } : {}) }, 'Thành phần tham chiếu (Ingredients / Reference)'),
              ),
              el('div', { class: 'field-help' },
                'G-Labs Flow: Chọn Khung hình đầu để tạo chuyển động từ ảnh; chọn Thành phần để giữ phong cách/nhân vật.'),
            ),
            // Loop video (I2V only) — reuse the SAME image as first + last
            // frame. Lives inside #i2v-image-block → auto-hidden in T2V mode.
            el('label', {
              id: 'cnt-loop-wrap',
              style: {
                display: form.videoMode === 'components' ? 'none' : 'flex',
                alignItems: 'center', justifyContent: 'space-between',
                gap: '12px', marginTop: '12px', padding: '10px 12px',
                border: '1px solid var(--border)', borderRadius: 'var(--r-md)',
                background: 'var(--bg-2)', cursor: 'pointer',
              },
            },
              el('div', { style: { lineHeight: '1.35' } },
                el('div', { style: { fontWeight: 600, fontSize: '13px' } }, 'Loop video'),
                el('div', { class: 'field-help', style: { margin: 0 } },
                  'Frame đầu và frame cuối giống nhau.'),
              ),
              el('span', { class: 'toggle' },
                el('input', { type: 'checkbox', id: 'cnt-loop',
                  ...(form.loop ? { checked: 'true' } : {}),
                  onchange: (e) => { form.loop = e.target.checked; } }),
                el('span', { class: 'toggle-track' }),
              ),
            ),
          ),
          el('div', { class: 'field-group' },
            el('label', { class: 'field-label' }, 'Số luồng song song'),
            el('input', { type: 'number', class: 'input', id: 'cnt-concurrent', value: form.concurrent, min: 1, max: 5 }),
          ),
          el('div', { class: 'field-group' },
            el('label', { class: 'field-label' }, 'Số video / prompt'),
            el('input', { type: 'number', class: 'input', id: 'cnt-count', value: form.videosPerPrompt, min: 1, max: 4 }),
            el('div', { class: 'field-help' }, '1 prompt tạo ra N video khác nhau (tối đa 4). Xếp thành nhóm để chọn 1 video ưng ý.'),
          ),
          el('div', { style: { display: 'flex', gap: '8px', marginTop: '24px' } },
            el('button', { class: 'btn btn-primary', style: { flex: 1 }, id: 'cnt-start' },
              icon('play'), el('span', null, 'Bắt đầu render'),
            ),
            el('button', { class: 'btn btn-danger hidden', id: 'cnt-cancel' },
              icon('stop'), el('span', null, 'Hủy'),
            ),
          ),
        ),
      ),
      // RIGHT
      el('div', { class: 'gen-results' },
        el('div', { class: 'card', style: { marginBottom: '16px' } },
          el('div', { class: 'card-header' },
            el('div', null,
              el('h3', { class: 'card-title' }, 'Danh sách prompts'),
              el('div', { class: 'card-subtitle', id: 'cnt-count' }, '1 prompt'),
            ),
            el('div', { style: { display: 'flex', gap: '8px' } },
              el('button', { class: 'btn btn-sm btn-ghost', id: 'cnt-import' },
                icon('upload', 14), 'Import .txt',
              ),
              el('button', { class: 'btn btn-sm btn-danger', id: 'cnt-clear-prompts' },
                icon('trash', 14), 'Xóa tất cả',
              ),
              el('button', { class: 'btn btn-sm btn-primary', id: 'cnt-add' },
                icon('plus', 14), 'Thêm dòng',
              ),
            ),
          ),
          // Bulk prompt — paste 1 đoạn = áp cho tất cả (I2V cho mọi ảnh, T2V làm 1 prompt),
          // hoặc nhiều đoạn cách nhau dòng trắng = mỗi đoạn 1 prompt riêng.
          // Hiển thị cho cả T2V và I2V.
          el('div', { id: 'cnt-bulk-prompt-block', style: {
            background: 'var(--bg-2)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--r-md)',
            padding: '12px',
            marginBottom: '12px',
          } },
            el('label', { class: 'field-label' },
              '⚡ Bulk prompt — paste nhanh nhiều prompts'),
            el('textarea', { class: 'textarea', id: 'cnt-bulk-prompt', rows: 4,
              placeholder: '• 1 đoạn → 1 prompt (T2V) HOẶC áp cho TẤT CẢ ảnh (I2V)\n• Nhiều đoạn cách nhau bằng dòng trắng → mỗi đoạn = 1 prompt riêng\n\nVí dụ:\n\nstatic shot, mây trôi\n\nzoom in slowly\n\npan right, gió thổi' }),
            el('div', { style: { display: 'flex', gap: '8px', marginTop: '8px', alignItems: 'center' } },
              el('button', { class: 'btn btn-sm btn-primary', id: 'cnt-bulk-apply' },
                icon('check', 14), 'Áp dụng'),
              el('div', { class: 'field-help', id: 'cnt-bulk-help', style: { margin: 0 } }),
            ),
          ),
          el('div', { class: 'prompt-list', id: 'cnt-list' }),
        ),
        el('div', { class: 'card' },
          el('div', { class: 'card-header' },
            el('div', null,
              el('h3', { class: 'card-title', id: 'cnt-results-title' }, 'Kết quả'),
              el('div', { class: 'card-subtitle', id: 'cnt-status' }, 'Chưa có job'),
            ),
            el('div', { id: 'cnt-header-actions', style: { display: 'flex', gap: '8px' } },
              el('button', {
                class: 'btn btn-sm btn-danger hidden',
                id: 'cnt-clear-all',
                title: 'Xóa toàn bộ danh sách (huỷ tác vụ đang chạy, file vẫn còn trên ổ đĩa)',
                onclick: clearCurrentTask,
              }, icon('trash', 14), 'Xóa danh sách'),
            ),
          ),
          el('div', { id: 'cnt-results' },
            el('div', { class: 'empty' },
              el('div', { class: 'empty-icon' }, icon('image', 32)),
              el('div', null, 'Khởi chạy render để xem kết quả ở đây'),
            ),
          ),
        ),
      ),
    ),
  );
  root.appendChild(ui);

  function tabBtn(mode, label) {
    return el('button', {
      class: `tab ${form.mode === mode ? 'active' : ''}`,
      'data-mode': mode,
      onclick: (e) => {
        form.mode = mode;
        root.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        e.currentTarget.classList.add('active');
        root.querySelector('#i2v-image-block').style.display = mode === 'i2v' ? 'block' : 'none';
        refreshModelDropdown();  // hide/show Omni Flash based on T2V/I2V
        refreshDurationDropdown();
        refreshBulkPromptBlock();
        refreshList();    // re-render rows (show/hide image thumbs)
      },
    }, label);
  }

  function splitBulkPrompts(text) {
    // Split by blank lines (1+ empty lines between content), keep paragraphs intact
    return (text || '')
      .split(/\n\s*\n+/)
      .map(s => s.trim())
      .filter(Boolean);
  }

  function refreshBulkPromptBlock() {
    const help = root.querySelector('#cnt-bulk-help');
    const ta = root.querySelector('#cnt-bulk-prompt');
    if (!help) return;
    const refCount = form.refs.filter(Boolean).length;
    const blocks = ta ? splitBulkPrompts(ta.value) : [];
    if (blocks.length >= 2) {
      const fillTarget = form.mode === 'i2v'
        ? `${blocks.length} ảnh đầu tiên`
        : `${blocks.length} prompts`;
      help.textContent = `→ phát hiện ${blocks.length} đoạn — sẽ thay thế danh sách bằng ${fillTarget}`;
      help.style.color = 'var(--brand)';
    } else if (blocks.length === 1) {
      if (form.mode === 'i2v') {
        help.textContent = refCount > 0
          ? `→ áp dụng cùng prompt cho ${refCount} ảnh đã upload`
          : 'Hãy upload ảnh trước (hoặc chuyển sang T2V)';
      } else {
        help.textContent = '→ ghi đè danh sách bằng 1 prompt này';
      }
      help.style.color = 'var(--text-muted)';
    } else {
      help.textContent = form.mode === 'i2v'
        ? (refCount > 0 ? 'Nhập prompt rồi bấm Áp dụng' : 'Upload ảnh + nhập prompt')
        : 'Nhập 1 hoặc nhiều prompts';
      help.style.color = 'var(--text-muted)';
    }
  }
  function option(value, label) {
    return el('option', { value }, label);
  }

  // Populate Model dropdown based on current mode (T2V vs I2V).
  function refreshModelDropdown() {
    const sel = root.querySelector('#cnt-quality');
    const help = root.querySelector('#cnt-quality-help');
    if (!sel) return;
    const allowed = MODELS_FOR_MODE[form.mode] || MODELS_FOR_MODE.t2v;
    const prev = form.quality;
    clear(sel);
    for (const m of allowed) {
      sel.appendChild(el('option', { value: m }, MODEL_LABELS[m] || m));
    }
    // If previous choice no longer allowed (vd Omni Flash khi vào I2V),
    // fallback sang lite_lp (miễn phí, an toàn nhất)
    form.quality = allowed.includes(prev) ? prev : 'lite_lp';
    sel.value = form.quality;
    updateQualityHelp();
  }
  function updateQualityHelp() {
    const help = root.querySelector('#cnt-quality-help');
    if (!help) return;
    if (form.quality === 'lite_lp') {
      help.textContent = 'Veo 3.1 Lite LP: Hàng đợi ưu tiên thấp, miễn phí hoàn toàn (không trừ credit Google Flow).';
    } else if (form.quality === 'omni_flash') {
      help.textContent = 'Omni Flash hỗ trợ 4/6/8/10s, tốc độ nhanh.';
    } else {
      help.textContent = 'Veo 3.1 (Lite / Fast / Quality) tạo video chất lượng cao.';
    }
  }
  refreshModelDropdown();

  // Restore selects from form
  root.querySelector('#cnt-aspect').value = form.aspect;
  root.querySelector('#cnt-resolution').value = form.resolution;

  // Populate duration dropdown based on currently-selected model, and
  // keep it in sync when the user picks a different model.
  function refreshDurationDropdown() {
    const sel = root.querySelector('#cnt-duration');
    if (!sel) return;
    const allowed = DURATION_BY_MODEL[form.quality] || [4, 6, 8];
    const prev = form.duration;
    clear(sel);
    for (const d of allowed) {
      sel.appendChild(el('option', { value: String(d) }, `${d} giây`));
    }
    // Try to keep the previous choice if still allowed, else fall back to 8s
    form.duration = allowed.includes(prev) ? prev : (allowed.includes(8) ? 8 : allowed[allowed.length - 1]);
    sel.value = String(form.duration);
  }
  refreshDurationDropdown();
  root.querySelector('#cnt-quality').addEventListener('change', (e) => {
    form.quality = e.target.value;
    updateQualityHelp();
    refreshDurationDropdown();
  });
  root.querySelector('#cnt-duration').addEventListener('change', (e) => {
    form.duration = parseInt(e.target.value, 10) || 8;
  });
  root.querySelector('#i2v-image-block').style.display = form.mode === 'i2v' ? 'block' : 'none';
  refreshBulkPromptBlock();

  // Live update of help text as user types
  root.querySelector('#cnt-bulk-prompt').addEventListener('input', refreshBulkPromptBlock);

  // Bulk-prompt apply: smart mode
  //   - Multi paragraphs  → REPLACE prompts array with blocks
  //   - 1 paragraph + I2V → fill every ref slot with the same text
  //   - 1 paragraph + T2V → REPLACE with single-row [text]
  root.querySelector('#cnt-bulk-apply').addEventListener('click', () => {
    const raw = root.querySelector('#cnt-bulk-prompt').value;
    const blocks = splitBulkPrompts(raw);
    if (blocks.length === 0) return toast('Nhập prompt trước', 'warning');

    if (blocks.length >= 2) {
      // Multi-paragraph: replace entire prompts array
      form.prompts = blocks.slice();
      // For I2V, pad to match refs.length so unmatched refs still show
      if (form.mode === 'i2v') {
        while (form.prompts.length < form.refs.length) form.prompts.push('');
      }
      refreshList();
      refreshRefPairing();
      toast(`Đã thay thế danh sách bằng ${blocks.length} prompts`, 'success');
      return;
    }

    // Single paragraph
    const text = blocks[0];
    if (form.mode === 'i2v') {
      const refCount = form.refs.filter(Boolean).length;
      if (refCount === 0) return toast('Chưa có ảnh nào — upload ảnh trước', 'warning');
      while (form.prompts.length < form.refs.length) form.prompts.push('');
      for (let i = 0; i < form.refs.length; i++) {
        if (form.refs[i]) form.prompts[i] = text;
      }
      refreshList();
      refreshRefPairing();
      toast(`Đã áp dụng prompt cho ${refCount} ảnh`, 'success');
    } else {
      // T2V: just replace with single row
      form.prompts = [text];
      refreshList();
      toast('Đã ghi đè danh sách bằng 1 prompt', 'success');
    }
  });

  // Clear-all prompts button
  root.querySelector('#cnt-clear-prompts').addEventListener('click', async () => {
    const { confirm } = await import('../ui.js');
    if (!await confirm('Xóa tất cả prompts hiện có?', 'Xác nhận')) return;
    // Reset to 1 empty row. Keep refs intact (user may want to re-enter prompts).
    form.prompts = [''];
    refreshList();
    refreshRefPairing();
    toast('Đã xóa toàn bộ prompts (ảnh vẫn còn)', 'info');
  });
  const nameInput = root.querySelector('#cnt-taskname');
  nameInput.value = form.taskName || defaultTaskName('video');
  nameInput.addEventListener('input', (e) => { form.taskName = e.target.value; });

  // ─────────── I2V reference image pairing (with drag-to-reorder) ───────────
  let dragSrcIdx = null;

  function refreshRefPairing() {
    const wrap = root.querySelector('#cnt-ref-pairing');
    if (!wrap) return;
    clear(wrap);
    if (form.refs.length === 0) return;

    // Header — image count + "Clear all" button. Only useful when there
    // are images to clear; hidden when zero (the dropzone above conveys
    // the empty state).
    //
    // Note: variable is named `headerCount` (not `refCount`) because
    // `refCount` is re-declared further down in the mismatch-warning
    // block — JS would throw SyntaxError if we used the same name twice
    // in this function scope.
    const headerCount = form.refs.filter(Boolean).length;
    if (headerCount === 0) return;   // all slots null → treat as empty
    const header = el('div', {
      style: {
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: '8px', padding: '0 2px',
      },
    },
      el('span', { class: 'field-help', style: { fontWeight: 600, color: 'var(--text)' } },
        `${headerCount} ảnh đã upload`),
      el('button', {
        class: 'btn btn-sm btn-ghost',
        style: { color: 'var(--accent-red, #ef4444)' },
        title: 'Xóa tất cả ảnh tham chiếu — prompts vẫn giữ nguyên',
        onclick: async () => {
          const { confirm } = await import('../ui.js');
          if (!await confirm(
            `Xóa tất cả ${headerCount} ảnh đã upload?\n\nPrompts sẽ giữ nguyên — chỉ ảnh tham chiếu bị xóa.`,
            'Xác nhận xóa ảnh',
          )) return;
          form.refs = [];
          refreshRefPairing();
          refreshList();
          toast(`Đã xóa ${headerCount} ảnh`, 'info');
        },
      }, icon('trash', 12), 'Xóa tất cả'),
    );
    wrap.appendChild(header);

    const grid = el('div', { style: {
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(96px, 1fr))',
      gap: '8px',
    } });
    form.refs.forEach((ref, i) => {
      if (!ref) return;
      const promptText = (form.prompts[i] || '').trim() || `(prompt #${i + 1} trống)`;
      const card = el('div', {
        class: 'ref-card',
        draggable: 'true',
        'data-idx': i,
        style: {
          position: 'relative', borderRadius: '10px', overflow: 'hidden',
          border: '1px solid var(--border)', background: 'var(--bg-2)',
          cursor: 'grab', transition: 'transform 0.15s, opacity 0.15s',
        },
        title: `Kéo để đổi vị trí • → Prompt ${i + 1}: ${promptText.slice(0, 80)}`,
        ondragstart: (e) => {
          dragSrcIdx = i;
          card.style.opacity = '0.4';
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('text/plain', String(i));
        },
        ondragend: () => {
          card.style.opacity = '';
          dragSrcIdx = null;
          grid.querySelectorAll('.ref-card').forEach(c => c.classList.remove('drag-over'));
        },
        ondragover: (e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          if (dragSrcIdx !== null && dragSrcIdx !== i) {
            card.classList.add('drag-over');
          }
        },
        ondragleave: () => card.classList.remove('drag-over'),
        ondrop: (e) => {
          e.preventDefault();
          card.classList.remove('drag-over');
          const src = dragSrcIdx;
          const dst = i;
          if (src === null || src === dst) return;
          const [moved] = form.refs.splice(src, 1);
          form.refs.splice(dst, 0, moved);
          form.refs = trimTrailingNulls(form.refs);
          refreshRefPairing();
          refreshList();
        },
      },
        el('img', { src: ref.previewUrl, style: {
          width: '100%', height: '90px', objectFit: 'cover', display: 'block',
          pointerEvents: 'none',  // let drag events bubble to parent
        } }),
        el('div', { style: {
          position: 'absolute', top: '4px', left: '4px',
          background: 'var(--brand)', color: 'white',
          padding: '2px 7px', borderRadius: '10px',
          fontSize: '10.5px', fontWeight: 700, fontFamily: 'var(--font-mono)',
          pointerEvents: 'none',
        } }, `#${i + 1}`),
        el('button', { class: 'btn btn-icon btn-ghost', draggable: 'false', style: {
          position: 'absolute', top: '4px', right: '4px',
          background: 'rgba(0,0,0,0.6)', color: 'white',
          width: '20px', height: '20px', padding: 0, borderRadius: '50%',
        }, onclick: (e) => {
          e.stopPropagation();
          form.refs[i] = null;
          form.refs = trimTrailingNulls(form.refs);
          refreshRefPairing();
          refreshList();
        } }, icon('x', 11)),
      );
      grid.appendChild(card);
    });
    wrap.appendChild(grid);

    // Mismatch warning
    const usedPrompts = form.prompts.filter(p => p.trim()).length;
    const refCount = form.refs.filter(Boolean).length;
    if (refCount !== usedPrompts) {
      wrap.appendChild(el('div', { class: 'field-help',
        style: { color: 'var(--accent-amber)', marginTop: '6px' } },
        `⚠ ${refCount} ảnh / ${usedPrompts} prompt — số ảnh nên bằng số prompt`));
    }
    wrap.appendChild(el('div', { class: 'field-help', style: { marginTop: '4px' } },
      '💡 Kéo thả ảnh để đổi vị trí — số thứ tự sẽ tự cập nhật theo prompt mới'));

    // Keep bulk-apply help text in sync
    if (typeof refreshBulkPromptBlock === 'function') refreshBulkPromptBlock();
  }

  function trimTrailingNulls(arr) {
    const out = [...arr];
    while (out.length && !out[out.length - 1]) out.pop();
    return out;
  }

  // Restore on mount
  refreshRefPairing();

  // Prompt list — in I2V mode each row shows its paired reference image
  const list = root.querySelector('#cnt-list');
  const countEl = root.querySelector('#cnt-count');
  function refreshList() {
    // Preserve the column's scroll position. clear()ing the list collapses
    // its height, so the browser clamps the scroll container's scrollTop to 0
    // — without restoring it, deleting a row jumps the page to the top.
    const _scroller = list.closest('.gen-config, .gen-results');
    const _scrollTop = _scroller ? _scroller.scrollTop : 0;
    clear(list);
    const isI2V = form.mode === 'i2v';
    form.prompts.forEach((p, i) => {
      const ref = form.refs[i];
      const thumbOrSlot = isI2V
        ? (ref
            ? el('img', {
                src: ref.previewUrl,
                title: `Ảnh ${i + 1}: ${ref.name}`,
                style: {
                  width: '56px', height: '56px', objectFit: 'cover',
                  borderRadius: '8px', border: '1px solid var(--border)',
                  flexShrink: 0, cursor: 'pointer',
                },
                onclick: () => pickImageForRow(i),
              })
            : el('button', {
                class: 'btn btn-ghost',
                style: {
                  width: '56px', height: '56px',
                  border: '1.5px dashed var(--border-strong)',
                  borderRadius: '8px', flexShrink: 0,
                  display: 'grid', placeItems: 'center',
                  background: 'var(--bg-2)',
                },
                title: `Chọn ảnh cho prompt ${i + 1}`,
                onclick: () => pickImageForRow(i),
              }, icon('plus', 16))
          )
        : null;
      const row = el('div', { class: 'prompt-row' },
        el('div', { class: 'row-number' }, String(i + 1)),
        thumbOrSlot,
        el('textarea', {
          class: 'textarea',
          rows: 2,
          placeholder: 'Mô tả cảnh quay...',
          oninput: (e) => {
            form.prompts[i] = e.target.value;
            refreshRefPairing();
          },
          style: { flex: 1, minHeight: '40px' },
        }, p),
        el('div', { class: 'row-actions' },
          form.prompts.length > 1
            ? el('button', { class: 'btn btn-icon btn-ghost', title: 'Xóa', onclick: () => {
                form.prompts.splice(i, 1);
                form.refs.splice(i, 1);
                refreshList(); refreshRefPairing();
              } }, icon('trash', 14))
            : null,
        ),
      );
      row.querySelector('textarea').value = p;
      list.appendChild(row);
    });
    countEl.textContent = `${form.prompts.length} prompt${form.prompts.length > 1 ? 's' : ''}`;
    if (_scroller) _scroller.scrollTop = _scrollTop;
  }
  refreshList();

  // Pick / replace a single image for a specific prompt row
  function pickImageForRow(idx) {
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = 'image/*';
    inp.onchange = async () => {
      const file = inp.files[0];
      if (!file) return;
      try {
        const r = await api.content.uploadImage(file);
        form.refs[idx] = {
          path: r.path,
          previewUrl: await makeThumbnail(file),
          name: file.name,
        };
        refreshList();
        refreshRefPairing();
        toast(`Đã gán ảnh cho prompt ${idx + 1}`, 'success');
      } catch (e) { toast(e.message, 'error'); }
    };
    inp.click();
  }

  root.querySelector('#cnt-add').addEventListener('click', () => {
    form.prompts.push(''); refreshList();
  });
  root.querySelector('#cnt-import').addEventListener('click', () => {
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

  // Multi-image dropzone — each upload appends to the next empty slot in form.refs
  const dz = root.querySelector('#cnt-dropzone');
  const fi = root.querySelector('#cnt-image-file');
  dz.addEventListener('click', () => fi.click());
  ['dragover', 'dragleave', 'drop'].forEach(ev => {
    dz.addEventListener(ev, e => {
      e.preventDefault();
      dz.classList.toggle('dragover', ev === 'dragover');
      if (ev === 'drop' && e.dataTransfer.files.length) {
        handleFiles(e.dataTransfer.files);
      }
    });
  });
  fi.addEventListener('change', () => {
    if (fi.files.length) handleFiles(fi.files);
    fi.value = '';
  });

  async function handleFiles(fileList) {
    const files = [...fileList];
    if (!files.length) return;
    // Ensure we have prompts to pair with
    while (form.prompts.length < form.refs.length + files.length) {
      form.prompts.push('');
    }
    for (const file of files) {
      try {
        const r = await api.content.uploadImage(file);
        // Find next null slot in refs, or append
        let idx = form.refs.findIndex(x => !x);
        if (idx < 0) idx = form.refs.length;
        form.refs[idx] = {
          path: r.path,
          previewUrl: await makeThumbnail(file),
          name: file.name,
        };
      } catch (e) {
        toast(`Upload "${file.name}" lỗi: ${e.message}`, 'error');
      }
    }
    refreshList();
    refreshRefPairing();
    refreshBulkPromptBlock();
    toast(`Đã tải ${files.length} ảnh`, 'success');
  }

  // ── Start / cancel ──────────────────────────────────────
  const startBtn = root.querySelector('#cnt-start');
  const cancelBtn = root.querySelector('#cnt-cancel');

  startBtn.addEventListener('click', async () => {
    // Build matched prompt + ref-image pairs, keeping their original index aligned
    const pairs = form.prompts
      .map((p, i) => ({ prompt: (p || '').trim(), ref: form.refs[i] || null }))
      .filter(x => x.prompt);
    if (pairs.length === 0) return toast('Cần ít nhất 1 prompt', 'warning');

    form.quality = root.querySelector('#cnt-quality').value;
    form.aspect = root.querySelector('#cnt-aspect').value;
    form.resolution = root.querySelector('#cnt-resolution').value;
    form.duration = parseInt(root.querySelector('#cnt-duration').value || '8', 10);
    form.concurrent = parseInt(root.querySelector('#cnt-concurrent').value || '1', 10);
    form.videosPerPrompt = Math.max(1, Math.min(4, parseInt(root.querySelector('#cnt-count').value || '1', 10)));

    let ref_images = null;
    if (form.mode === 'i2v') {
      const withImage = pairs.filter(p => p.ref).length;
      if (withImage === 0) {
        return toast('Chế độ I2V cần ít nhất 1 ảnh tham chiếu', 'warning');
      }
      if (withImage !== pairs.length) {
        const ok = confirm(
          `${withImage}/${pairs.length} prompt có ảnh. Prompt thiếu ảnh sẽ chạy T2V. Tiếp tục?`,
        );
        if (!ok) return;
      }
      ref_images = pairs.map(p => p.ref ? p.ref.path : null);
    }

    const promptsToSend = pairs.map(p => p.prompt);

    if (!(await ensureFlowAccountOrWarn())) return;

    let taskName = (form.taskName || '').trim();
    if (!taskName) { taskName = defaultTaskName('video'); nameInput.value = taskName; }
    setLoading(startBtn, true);
    try {
      const res = await api.content.start({
        mode: form.mode,
        prompts: promptsToSend,
        quality: form.quality,
        aspect_ratio: form.aspect,
        resolution: form.resolution,
        duration: form.duration,
        concurrent: form.concurrent,
        videos_per_prompt: form.videosPerPrompt,
        reference_images: ref_images,
        video_mode: form.mode === 'i2v' ? (root.querySelector('#cnt-video-mode')?.value || form.videoMode || 'start_image') : 'start_image',
        loop: form.mode === 'i2v' && (form.loop || form.videoMode === 'start_end_image'),
        task_name: taskName,
      });
      // Nở seed gallery: N video mỗi prompt, giữ thứ tự prompt-major (khớp backend)
      // → item thứ k thuộc nhóm k // videosPerPrompt.
      const expandedItems = [];
      for (const pr of promptsToSend) {
        for (let k = 0; k < form.videosPerPrompt; k++) expandedItems.push(pr);
      }
      tasksStore.register(res.task_id, 'content', {
        items: expandedItems,
        videosPerPrompt: form.videosPerPrompt,
        aspect: form.aspect,
        model: form.quality,
        name: taskName,
      });
      attachToTask(res.task_id);
      toast(`Đã tạo task #${res.task_id} (${res.items} items)`, 'success');
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
      await api.content.cancel(t);
      toast('Đã hủy task', 'info');
    } catch (e) { toast(e.message, 'error'); }
  });

  // Bulk-remove watermark from selected video files. `kind` picks the mask
  // ('veo_mini' | 'gemini') — the toolbar asks the user before calling.
  // Backend processes sequentially and streams WS events
  // (watermark_progress / _item_completed / _item_error) which tasks_store
  // translates into per-item wm_status flags — gallery chips update via the
  // normal notify path.
  async function runBatchWatermark(paths, kind = 'veo_mini') {
    const handle = toast(
      `Đang xóa watermark cho ${paths.length} video… (mỗi video ~5-30s)`,
      'info', 0,
    );
    try {
      const r = await api.media.videoWatermarkBatch(paths, {
        method: 'auto', device: 'auto', watermarkKind: kind,
      });
      const okN = (r.completed || []).length;
      const errN = (r.errors || []).length;
      if (errN === 0) {
        toast(`Đã xóa watermark ${okN} video — file mới có suffix [RedOne].mp4`, 'success');
      } else {
        toast(`Watermark: ${okN} OK, ${errN} lỗi (xem chip trên từng video)`, 'warning');
      }
    } catch (e) {
      toast(`Watermark batch lỗi: ${e.message}`, 'error');
    } finally {
      if (handle && typeof handle.remove === 'function') handle.remove();
    }
  }

  async function clearCurrentTask() {
    const tid = currentTaskId();
    if (!tid) return;
    try {
      const t = tasksStore.get(tid);
      if (t && t.status === 'running') {
        await api.content.cancel(tid).catch(() => {});
      }
    } catch (e) { /* ignore */ }
    tasksStore.remove(tid);
    _currentTaskId = null;
    renderTaskGallery(null);
    toast('Đã xóa danh sách (file vẫn còn trên ổ đĩa)', 'info');
  }

  // ── Gallery rendering ─────────────────────────────────
  function aspectThumbClass(aspect) {
    if (aspect === '9:16' || aspect === '3:4') return 'thumb-portrait';
    if (aspect === '1:1') return 'thumb-square';
    return '';
  }

  // Snapshot of the last render — used to detect wm-only changes so we can
  // patch chips in-place without rebuilding the entire grid (which destroys
  // <video> elements and causes flicker during watermark removal).
  let _lastRenderedSnap = null;

  function _patchWmChipsInPlace(taskState) {
    const wrap = root.querySelector('#cnt-results');
    if (!wrap) return false;
    const cards = wrap.querySelectorAll('.scene-card');
    if (!cards.length || cards.length !== taskState.items.length) return false;

    taskState.items.forEach((it, i) => {
      const card = cards[i];
      if (!card) return;
      // Find or create the wm-chip container
      const actions = card.querySelector('.scene-actions');
      if (!actions) return;
      let chip = actions.querySelector('[data-wm-chip]');
      if (it.wm_status === 'running') {
        const text = `Xóa WM ${Math.round(it.wm_progress || 0)}%`;
        if (chip) {
          chip.className = 'chip chip-blue';
          chip.textContent = text;
          chip.title = it.wm_label || '';
        } else {
          chip = el('span', {
            class: 'chip chip-blue', 'data-wm-chip': '1',
            style: { marginLeft: 'auto' }, title: it.wm_label || '',
          }, text);
          actions.appendChild(chip);
        }
      } else if (it.wm_status === 'done' && it.wm_url) {
        if (chip && chip.tagName === 'A') {
          chip.href = it.wm_url;
        } else {
          if (chip) chip.remove();
          chip = el('a', {
            href: it.wm_url, download: '', target: '_blank',
            class: 'chip chip-green', 'data-wm-chip': '1',
            style: { marginLeft: 'auto', textDecoration: 'none' },
            title: 'Click để tải video đã xóa watermark',
          }, '✓ Đã xóa WM');
          actions.appendChild(chip);
        }
      } else if (it.wm_status === 'error') {
        if (chip) {
          chip.className = 'chip chip-red';
          chip.textContent = 'WM lỗi';
          chip.title = it.wm_error || 'Lỗi xóa watermark';
        } else {
          chip = el('span', {
            class: 'chip chip-red', 'data-wm-chip': '1',
            style: { marginLeft: 'auto' }, title: it.wm_error || 'Lỗi xóa watermark',
          }, 'WM lỗi');
          actions.appendChild(chip);
        }
      }
    });

    // Also update status text
    _updateStatusText(taskState);
    return true;
  }

  function _updateStatusText(taskState) {
    const total = taskState.total || taskState.items.length;
    let statusText;
    if (taskState.status === 'running') {
      if (taskState.batch_cooldown) {
        const cd = taskState.batch_cooldown;
        const elapsed = (Date.now() - cd.started_at) / 1000;
        const remaining = Math.max(0, Math.ceil((cd.seconds || 0) - elapsed));
        statusText = `Đang nghỉ ${remaining}s giữa đợt gen (${cd.batch_done}/${cd.batch_total}) • ${taskState.done}/${total} xong`;
      } else {
        statusText = `Đang render • ${taskState.done + taskState.error}/${total} (OK: ${taskState.done}, lỗi: ${taskState.error})`;
      }
      cancelBtn.classList.remove('hidden');
    } else if (taskState.status === 'completed') {
      statusText = `Hoàn tất: ${taskState.done} OK / ${taskState.error} lỗi`;
      cancelBtn.classList.add('hidden');
    } else if (taskState.status === 'error') {
      statusText = `Lỗi: ${taskState.error_message || ''}`;
      cancelBtn.classList.add('hidden');
    } else if (taskState.status === 'cancelled') {
      statusText = 'Đã hủy';
      cancelBtn.classList.add('hidden');
    }
    const el2 = root.querySelector('#cnt-status');
    if (el2) el2.textContent = statusText;
  }

  // Detect if only wm_ fields changed (no item status/url/count changes).
  function _isWmOnlyChange(taskState) {
    if (!_lastRenderedSnap) return false;
    const snap = _lastRenderedSnap;
    if (snap.id !== taskState.id) return false;
    if (snap.itemCount !== taskState.items.length) return false;
    if (snap.status !== taskState.status) return false;
    if (snap.done !== taskState.done || snap.error !== taskState.error) return false;
    // Check that no item changed status or output_url (only wm_ fields differ)
    for (let i = 0; i < taskState.items.length; i++) {
      const cur = taskState.items[i];
      const prev = snap.itemStatuses[i];
      if (!prev) return false;
      if (cur.status !== prev.status || cur.output_url !== prev.output_url) return false;
    }
    return true;
  }

  function _saveSnap(taskState) {
    _lastRenderedSnap = {
      id: taskState.id,
      itemCount: taskState.items.length,
      status: taskState.status,
      done: taskState.done,
      error: taskState.error,
      itemStatuses: taskState.items.map(it => ({ status: it.status, output_url: it.output_url })),
    };
  }

  function renderTaskGallery(taskState) {
    if (!root.isConnected) return;
    const wrap = root.querySelector('#cnt-results');
    if (!wrap) return;

    // Fast-path: if only wm_ fields changed, patch chips in-place (no flicker).
    if (taskState && _isWmOnlyChange(taskState)) {
      _patchWmChipsInPlace(taskState);
      _saveSnap(taskState);
      return;
    }

    clear(wrap);
    if (taskState) _saveSnap(taskState);

    // Show WHICH task these cards belong to (task name).
    const titleEl = root.querySelector('#cnt-results-title');
    if (titleEl) {
      titleEl.textContent = taskState
        ? `Kết quả — ${taskState.name || ('Task #' + taskState.id)}`
        : 'Kết quả';
    }

    const clearBtn = root.querySelector('#cnt-clear-all');
    if (clearBtn) clearBtn.classList.toggle('hidden', !taskState);
    retryBtn.refresh(taskState);

    if (!taskState) {
      wrap.appendChild(el('div', { class: 'empty' },
        el('div', { class: 'empty-icon' }, icon('image', 32)),
        el('div', null, 'Khởi chạy render để xem kết quả ở đây'),
      ));
      root.querySelector('#cnt-status').textContent = 'Chưa có job';
      cancelBtn.classList.add('hidden');
      return;
    }

    const grid = el('div', { class: 'scene-grid' });
    const aspectCls = aspectThumbClass(taskState.aspect);

    // Số video / prompt. N>1 → gallery hiển thị THEO NHÓM: mỗi N item liền nhau
    // là các biến thể của 1 prompt (backend thêm item prompt-major). N=1 → phẳng.
    const N = Math.max(1, taskState.videosPerPrompt || 1);
    const groupOf = (i) => Math.floor(i / N);
    if (N > 1) {
      // Mỗi nhóm cần 1 ⭐ hợp lệ; mặc định = biến thể done đầu tiên của nhóm.
      const doneByGroup = new Map();  // g -> [output_path,...] done, đúng thứ tự
      taskState.items.forEach((it, i) => {
        if (it.status === 'done' && it.output_path) {
          const g = groupOf(i);
          if (!doneByGroup.has(g)) doneByGroup.set(g, []);
          doneByGroup.get(g).push(it.output_path);
        }
      });
      for (const [g, paths] of doneByGroup) {
        const key = taskState.id + ':' + g;
        const cur = _favByGroup.get(key);
        if (!cur || !paths.includes(cur)) _favByGroup.set(key, paths[0]);
      }
    }

    const hasAnyDone = taskState.items.some(it => it.status === 'done' && it.output_path);
    let toolbar = null;
    if (hasAnyDone) {
      toolbar = makeSelectionToolbar({
        getCards: () => [...grid.querySelectorAll('.scene-card[data-path]')],
        pathOf: (card) => card.dataset.path,
        itemOf: (card) => {
          const id = parseInt(card.dataset.itemId || '0', 10);
          if (!id) return null;
          const t = tasksStore.get(taskState.id);
          return (t && t.items.find(x => x.id === id)) || { id };
        },
        onRegen: async (ids) => {
          await api.tasks.retryItems(taskState.id, ids);
          ids.forEach(iid => tasksStore.retryItemUI(taskState.id, iid, 'pending'));
        },
        // N>1: "Chọn tất cả" chỉ tick ⭐ mỗi nhóm (1 video/prompt), không quét
        // hết bản trùng — để Xóa WM / Tải / Upscale chạy đúng video ưng ý.
        onSelectAll: N > 1 ? (cards, mark) => {
          const prefix = taskState.id + ':';
          const favSet = new Set();
          for (const [k, p] of _favByGroup) if (k.startsWith(prefix)) favSet.add(p);
          cards.forEach(c => mark(c, favSet.has(c.dataset.path)));
        } : undefined,
        onChange: () => renderTaskGallery(tasksStore.get(taskState.id)),
        onClearSelected: (paths) => {
          tasksStore.removeItemsByPath(taskState.id, paths);
          renderTaskGallery(tasksStore.get(taskState.id));
        },
        onRemoveWatermark: async (paths, kind) => {
          await runBatchWatermark(paths, kind);
        },
        onUpscaleVideo: async (paths) => {
          const videoExts = ['.mp4', '.mov', '.avi', '.mkv', '.webm'];
          const videoPaths = paths.filter(p => videoExts.some(e => p.toLowerCase().endsWith(e)));
          if (!videoPaths.length) {
            return toast('Không có video nào được chọn', 'warning');
          }
          window.__pendingUpscaleVideos = window.__pendingUpscaleVideos || [];
          videoPaths.forEach(vp => {
            const name = vp.split(/[/\\]/).pop();
            if (!window.__pendingUpscaleVideos.some(p => p.path === vp)) {
              window.__pendingUpscaleVideos.push({ path: vp, name: name });
            }
          });
          if (window.__app && window.__app.navigate) {
            window.__app.navigate('video-upscale');
            toast(`Đã thêm ${videoPaths.length} video vào danh sách chọn nâng cấp!`, 'success');
          }
        },
      });
      wrap.appendChild(toolbar);
    }

    taskState.items.forEach((it, i) => {
      // Header nhóm (full-width) ở đầu mỗi nhóm khi N>1 — ngăn cách các biến thể
      // của prompt này với prompt khác. gridColumn 1/-1 → chiếm trọn 1 hàng.
      if (N > 1 && i % N === 0) {
        grid.appendChild(el('div', {
          class: 'scene-group-header',
          style: {
            gridColumn: '1 / -1', fontWeight: '600', fontSize: '13px',
            color: 'var(--text-muted)', margin: '6px 0 -2px', padding: '0 2px',
          },
        }, `Prompt #${groupOf(i) + 1} · ${N} video`));
      }
      const card = el('div', { class: 'scene-card' });
      const thumb = el('div', { class: `scene-thumb ${aspectCls}` },
        el('div', { class: 'scene-number' }, `#${i + 1}`),
      );
      if (it.status === 'done' && it.output_url) {
        // Lazy: no `src` yet — `data-src` is wired by _mediaObs (below) so only
        // videos near the viewport fetch metadata/first-frame. Prevents 100+
        // simultaneous metadata fetches that lag the page.
        // Poster-only thumbnail (no inline controls); click → lightbox để xem.
        const vid = el('video', { 'data-src': it.output_url, preload: 'none', muted: true, playsinline: true, style: { width: '100%', height: '100%', objectFit: 'cover', cursor: 'zoom-in' } });
        vid.addEventListener('click', () => {
          const galleryItems = taskState.items
            .filter(x => x.status === 'done' && x.output_url)
            .map((x, j) => ({ url: x.output_url, type: 'video', label: `#${j + 1} ${x.prompt || ''}`.trim() }));
          const doneIndex = taskState.items.slice(0, i + 1).filter(x => x.status === 'done' && x.output_url).length - 1;
          openMediaViewer({ items: galleryItems, currentIndex: doneIndex });
        });
        thumb.appendChild(vid);
        // Icon play ở giữa để user biết đây là video (bấm để xem trong lightbox).
        // pointer-events:none → click xuyên qua xuống <video> ở trên.
        thumb.appendChild(el('div', {
          style: { position: 'absolute', inset: '0', display: 'flex', alignItems: 'center',
                   justifyContent: 'center', pointerEvents: 'none', zIndex: '2' },
        }, el('div', {
          style: { width: '52px', height: '52px', borderRadius: '50%', color: '#fff',
                   background: 'rgba(0,0,0,0.5)', border: '2px solid rgba(255,255,255,0.9)',
                   display: 'flex', alignItems: 'center', justifyContent: 'center', paddingLeft: '3px' },
        }, icon('play', 26))));
      } else if (it.status === 'error') {
        // Show the FULL friendly message (no truncation) so the user knows
        // exactly what happened. Tooltip carries the raw detail for debugging.
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
      card.appendChild(thumb);

      const chipClass = it.status === 'done' ? 'chip-green'
                     : it.status === 'error' ? 'chip-red'
                     : it.status === 'generating' ? 'chip-blue'
                     : 'chip-yellow';
      const chipText = it.status === 'done' ? 'Hoàn thành'
                     : it.status === 'error' ? 'Lỗi'
                     : it.status === 'generating' ? 'Đang render'
                     : 'Đang chờ';

      // Build actions row with download + copy-prompt + upscale button
      const sceneActions = (it.status === 'done' && it.output_url)
        ? el('div', { class: 'scene-actions' },
            el('a', { href: it.output_url, download: '', class: 'btn btn-sm btn-ghost' },
              icon('download', 14), 'Tải'),
            el('button', { class: 'btn btn-sm btn-ghost', title: 'Copy prompt', onclick: () => {
              const p = (it.prompt || '').trim();
              if (!p) return toast('Không có prompt để copy', 'warning');
              navigator.clipboard.writeText(p);
              toast('Đã copy prompt', 'success');
            } }, icon('copy', 14)),
            it.id != null
              ? makePromptEditButton({ taskId: taskState.id, item: it })
              : null,
          )
        : (it.status === 'error' && it.id != null)
        ? el('div', { class: 'scene-actions' },
            makeItemRetryButton(taskState.id, it.id),
            el('button', { class: 'btn btn-sm btn-ghost btn-icon', title: 'Copy prompt', style: { marginLeft: 'auto' }, onclick: () => {
              const p = (it.prompt || '').trim();
              if (!p) return toast('Không có prompt để copy', 'warning');
              navigator.clipboard.writeText(p);
              toast('Đã copy prompt', 'success');
            } }, icon('copy', 14)),
            makePromptEditButton({ taskId: taskState.id, item: it }),
          )
        : null;
      // ⭐ đánh dấu video ưng ý của nhóm (chỉ khi N>1). Bấm sao → đặt biến thể
      // này làm ⭐ của nhóm; các sao cùng nhóm cập nhật tại chỗ (KHÔNG re-render:
      // taskState không đổi nên renderTaskGallery sẽ rơi vào fast-path wm-only).
      if (N > 1 && sceneActions && it.status === 'done' && it.output_path) {
        const g = groupOf(i);
        const key = taskState.id + ':' + g;
        const isFav = _favByGroup.get(key) === it.output_path;
        const favBtn = el('button', {
          class: 'btn btn-sm ' + (isFav ? 'btn-warm' : 'btn-ghost'),
          'data-fav-group': String(g),
          'data-fav-path': it.output_path,
          title: isFav ? 'Video ưng ý của prompt này (được chọn khi bấm "Chọn tất cả")'
                       : 'Đánh dấu là video ưng ý của prompt này',
          onclick: () => {
            _favByGroup.set(key, it.output_path);
            grid.querySelectorAll('[data-fav-group="' + g + '"]').forEach(b => {
              const on = b.dataset.favPath === it.output_path;
              b.classList.toggle('btn-warm', on);
              b.classList.toggle('btn-ghost', !on);
              b.textContent = on ? '★' : '☆';
              b.title = on ? 'Video ưng ý của prompt này (được chọn khi bấm "Chọn tất cả")'
                           : 'Đánh dấu là video ưng ý của prompt này';
            });
          },
        }, isFav ? '★' : '☆');
        sceneActions.appendChild(favBtn);
      }
      if (sceneActions) {
        if (it.wm_status === 'running') {
          sceneActions.appendChild(el('span', {
            class: 'chip chip-blue', 'data-wm-chip': '1',
            style: { marginLeft: 'auto' },
            title: it.wm_label || '',
          }, `Xóa WM ${Math.round(it.wm_progress || 0)}%`));
        } else if (it.wm_status === 'done' && it.wm_url) {
          sceneActions.appendChild(el('a', {
            href: it.wm_url, download: '', target: '_blank',
            class: 'chip chip-green', 'data-wm-chip': '1',
            style: { marginLeft: 'auto', textDecoration: 'none' },
            title: 'Click để tải video đã xóa watermark',
          }, '✓ Đã xóa WM'));
        } else if (it.wm_status === 'error') {
          sceneActions.appendChild(el('span', {
            class: 'chip chip-red', 'data-wm-chip': '1',
            style: { marginLeft: 'auto' },
            title: it.wm_error || 'Lỗi xóa watermark',
          }, 'WM lỗi'));
        }
      }

      // (Per-card "Gen lại" removed — regen is now on the selection toolbar:
      //  tick cards → "Gen lại".)
      card.appendChild(el('div', { class: 'scene-info' },
        el('div', { class: 'scene-meta' },
          el('span', { class: `chip ${chipClass}` }, chipText),
        ),
        el('div', { class: 'scene-prompt' }, it.prompt),
        sceneActions,
      ));
      if (it.status === 'done' && it.output_path) {
        if (it.id != null) card.dataset.itemId = String(it.id);
        attachCardCheckbox(card, it.output_path, toolbar);
      }
      grid.appendChild(card);
    });

    wrap.appendChild(grid);

    // Lazy-load videos: only those near the viewport fetch metadata/first-frame
    // (preview preserved); the rest stay cheap placeholders. Fresh observer per
    // render; disconnect the previous one to avoid leaking across rebuilds.
    if (_mediaObs) { _mediaObs.disconnect(); _mediaObs = null; }
    const lazyVids = grid.querySelectorAll('video[data-src]:not([src])');
    if (lazyVids.length) {
      _mediaObs = makeLazyVideoObserver(null);
      lazyVids.forEach(v => _mediaObs.observe(v));
    }

    _updateStatusText(taskState);
  }

  // "Gen lại N lỗi" button — created once, refreshed on every render.
  // Prepended to the header actions so it sits LEFT of "Xóa danh sách"
  // (most-relevant action when the task has failures).
  const retryBtn = makeRetryFailedButton({
    getTaskState: () => {
      const id = currentTaskId();
      const t = id && tasksStore.get(id);
      return t ? { taskId: id, errorCount: t.error || 0, status: t.status } : null;
    },
    onResetUI: (id) => tasksStore.resetErrorItems(id),
  });
  root.querySelector('#cnt-header-actions').prepend(retryBtn);

  // Live subscription
  let unsubscribe = null;
  let _currentTaskId = null;
  let _mediaObs = null;     // IntersectionObserver for lazy videos (Change B2)
  let _rafId = 0;           // coalesces re-renders to ~1 per frame (Change A)
  function currentTaskId() { return _currentTaskId; }
  // Coalesce the WS-event render storm: many notifies in one frame → ONE
  // rebuild reading the LATEST store state (not a stale captured `s`).
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
    if (_mediaObs) { _mediaObs.disconnect(); _mediaObs = null; }
    _currentTaskId = taskId;
    _lastViewedTaskId = taskId;   // remember across re-mounts (sticky view)
    renderTaskGallery(tasksStore.get(taskId));   // immediate first paint
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
    const latest = tasksStore.latestByKind('content');
    if (latest) attachToTask(latest.id);
  }

  // `root` is the PERSISTENT #page-container, so document.body.contains(root) is
  // always true and never tore the subscription down — each visit leaked a live
  // tasksStore.on() that repainted #cnt-results with its own task on any item
  // event (the "view jumps to another task" bug). Detect unmount via our marker.
  const obs = new MutationObserver(() => {
    if (!root.querySelector('#cnt-results')) {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      if (_rafId) { cancelAnimationFrame(_rafId); _rafId = 0; }
      if (_mediaObs) { _mediaObs.disconnect(); _mediaObs = null; }
      obs.disconnect();
    }
  });
  obs.observe(document.body, { childList: true, subtree: true });

  // Inject prompts from other pages (T2V — text only)
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

  // Inject I2V scenes from Storyboard ("Gửi tất cả sang Tạo Video"):
  // each scene = { prompt, path (server image), url, name }. Switches to I2V,
  // sets each prompt + its reference image (the scene's generated image).
  const injI2V = sessionStorage.getItem('inject_i2v');
  if (injI2V) {
    try {
      const arr = JSON.parse(injI2V);
      if (Array.isArray(arr) && arr.length) {
        form.mode = 'i2v';
        form.prompts = arr.map(x => x.prompt || '');
        form.refs = arr.map(x => (x.path ? { path: x.path, previewUrl: x.url || '', name: x.name || 'scene' } : null));
        root.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.mode === 'i2v'));
        const i2vBlock = root.querySelector('#i2v-image-block');
        if (i2vBlock) i2vBlock.style.display = 'block';
        refreshModelDropdown();
        refreshDurationDropdown();
        refreshBulkPromptBlock();
        refreshList();
        refreshRefPairing();
        toast(`Đã nạp ${arr.length} cảnh sang I2V`, 'success');
      }
    } catch (e) { /* ignore */ }
    sessionStorage.removeItem('inject_i2v');
  }
}
