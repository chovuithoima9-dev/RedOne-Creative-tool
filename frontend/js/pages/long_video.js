// Long video page — N scenes + extend + concat. State survives navigation.
import { el, clear, toast, setLoading, icon, ensureFlowAccountOrWarn, wireDropzone } from '../ui.js';
import { api } from '../api.js';
import { tasksStore } from '../tasks_store.js';
import { makeRetryFailedButton, makeSelectionToolbar, attachCardCheckbox } from '../gallery_actions.js';

const form = {
  prompts: ['', ''],
  quality: 'fast',
  aspect: '16:9',
  duration: 8,
  taskName: '',
  startImagePath: null,
  startImagePreviewUrl: null,
};

// Duration encoded in model_key for Omni Flash; Veo 3.1 keys are fixed at 8s.
const DURATION_BY_MODEL = {
  omni_flash: [4, 6, 8, 10],
  lite:       [8],
  fast:       [8],
  quality:    [8],
  lite_lp:    [8],
};

// Short labels so they fit the narrow config column (see content.js).
const MODEL_LABELS = {
  omni_flash: 'Omni Flash · 7–15 credit/cảnh (theo độ dài)',
  lite_lp:    'Veo 3.1 Lite · Miễn phí (chậm)',
  lite:       'Veo 3.1 Lite · 5 credit/cảnh',
  fast:       'Veo 3.1 Fast · 10 credit/cảnh',
  quality:    'Veo 3.1 Quality · 100 credit/cảnh',
};
// Omni Flash gained I2V on 2026-07-20 → same list for both cases now.
const MODELS_NO_IMAGE = ['omni_flash', 'lite_lp', 'lite', 'fast', 'quality'];
const MODELS_WITH_IMAGE = ['omni_flash', 'lite_lp', 'lite', 'fast', 'quality'];

function defaultTaskName() {
  const d = new Date();
  const ts = `${d.getHours().toString().padStart(2, '0')}h${d.getMinutes().toString().padStart(2, '0')}m${d.getSeconds().toString().padStart(2, '0')}`;
  return `long_video_${ts}`;
}

// Long video supports only 16:9 and 9:16 (same as single-scene video)
const VIDEO_ASPECTS = ['16:9', '9:16'];

function applySavedDefaults() {
  if (form._initialized) return;
  form._initialized = true;
  const s = window.__app?.store?.settings || {};
  if (s.default_quality) form.quality = s.default_quality;
  if (s.default_aspect && VIDEO_ASPECTS.includes(s.default_aspect)) {
    form.aspect = s.default_aspect;
  }
  if (!VIDEO_ASPECTS.includes(form.aspect)) form.aspect = '16:9';
}

export function renderLongVideo(root) {
  applySavedDefaults();

  root.appendChild(el('div', { class: 'page-hero' },
    el('div', { class: 'hero-icon' }, icon('play', 28)),
    el('div', { class: 'hero-text' },
      el('h2', null, 'Tạo Video Dài (Multi-Scene)'),
      el('p', null, 'Cảnh 1 generate, các cảnh tiếp dùng Extend từ cảnh trước, rồi ghép bằng FFmpeg'),
    ),
  ));

  const layout = el('div', { class: 'gen-layout' });
  root.appendChild(layout);

  // LEFT
  layout.appendChild(el('div', { class: 'gen-config' },
    el('div', { class: 'card' },
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Tên task'),
        el('input', { class: 'input', id: 'lv-taskname',
          placeholder: 'vd: cau_chuyen_5_canh' }),
        el('div', { class: 'field-help' },
          'File sẽ lưu tại outputs/video/<ngày>/<tên_task>/'),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Model'),
        el('select', { class: 'select', id: 'lv-quality' }),
        el('div', { class: 'field-help', id: 'lv-quality-help' }),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Độ dài mỗi cảnh'),
        el('select', { class: 'select', id: 'lv-duration' }),
        el('div', { class: 'field-help' },
          'Chỉ Omni Flash hỗ trợ tùy chỉnh (4/6/8/10s). Veo 3.1 cố định 8s.'),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Tỉ lệ'),
        el('select', { class: 'select', id: 'lv-aspect' },
          el('option', { value: '16:9' }, '16:9 (ngang)'),
          el('option', { value: '9:16' }, '9:16 (dọc)'),
        ),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Ảnh frame đầu (tùy chọn)'),
        el('div', { class: 'dropzone', id: 'lv-dropzone' },
          el('div', { class: 'dropzone-icon' }, icon('image', 24)),
          el('div', null, 'Click để chọn ảnh'),
          el('div', { class: 'field-help' }, 'Cảnh đầu sẽ bắt đầu từ ảnh này'),
          el('input', { type: 'file', accept: 'image/*', id: 'lv-image', style: { display: 'none' } }),
        ),
        el('div', { id: 'lv-image-preview', style: { marginTop: '8px' } }),
      ),
      el('div', { style: { display: 'flex', gap: '8px', marginTop: '20px' } },
        el('button', { class: 'btn btn-primary', style: { flex: 1 }, id: 'lv-start' },
          icon('play'), 'Render video dài',
        ),
        el('button', { class: 'btn btn-danger hidden', id: 'lv-cancel' },
          icon('stop'), 'Hủy',
        ),
      ),
      el('div', { class: 'field-help', style: { marginTop: '12px' } },
        'Tối thiểu 2 prompts, tối đa 20. Mỗi cảnh ~8s. Tiến độ KHÔNG mất khi đổi trang.'),
    ),
  ));

  function refreshModelDropdown() {
    const sel = root.querySelector('#lv-quality');
    const help = root.querySelector('#lv-quality-help');
    if (!sel) return;
    const allowed = form.startImagePath ? MODELS_WITH_IMAGE : MODELS_NO_IMAGE;
    const prev = form.quality;
    clear(sel);
    for (const m of allowed) {
      sel.appendChild(el('option', { value: m }, MODEL_LABELS[m] || m));
    }
    form.quality = allowed.includes(prev) ? prev : 'lite_lp';
    sel.value = form.quality;
    if (help) {
      help.textContent = 'Omni Flash hỗ trợ 4/6/8/10s; các model Veo 3.1 cố định 8s';
    }
  }
  refreshModelDropdown();

  // Restore selects
  root.querySelector('#lv-aspect').value = form.aspect;

  function refreshDurationDropdown() {
    const sel = root.querySelector('#lv-duration');
    if (!sel) return;
    const allowed = DURATION_BY_MODEL[form.quality] || [4, 6, 8];
    const prev = form.duration;
    clear(sel);
    for (const d of allowed) {
      sel.appendChild(el('option', { value: String(d) }, `${d} giây`));
    }
    form.duration = allowed.includes(prev) ? prev : (allowed.includes(8) ? 8 : allowed[allowed.length - 1]);
    sel.value = String(form.duration);
  }
  refreshDurationDropdown();
  root.querySelector('#lv-quality').addEventListener('change', (e) => {
    form.quality = e.target.value;
    refreshDurationDropdown();
  });
  root.querySelector('#lv-duration').addEventListener('change', (e) => {
    form.duration = parseInt(e.target.value, 10) || 8;
  });
  const nameInput = root.querySelector('#lv-taskname');
  nameInput.value = form.taskName || defaultTaskName();
  nameInput.addEventListener('input', (e) => { form.taskName = e.target.value; });
  if (form.startImagePreviewUrl) {
    root.querySelector('#lv-image-preview').appendChild(
      el('img', { src: form.startImagePreviewUrl, class: 'thumb', style: { maxWidth: '200px' } })
    );
  }

  // RIGHT
  const right = el('div', { class: 'gen-results' });
  layout.appendChild(right);

  right.appendChild(el('div', { class: 'card', style: { marginBottom: '16px' } },
    el('div', { class: 'card-header' },
      el('div', null,
        el('h3', { class: 'card-title' }, 'Kịch bản từng cảnh'),
        el('div', { class: 'card-subtitle', id: 'lv-count' }, '2 cảnh'),
      ),
      el('button', { class: 'btn btn-sm btn-primary', id: 'lv-add' },
        icon('plus', 14), 'Thêm cảnh',
      ),
    ),
    el('div', { class: 'prompt-list', id: 'lv-list' }),
  ));

  right.appendChild(el('div', { class: 'card' },
    el('div', { class: 'card-header' },
      el('div', null,
        el('h3', { class: 'card-title' }, 'Tiến độ'),
        el('div', { class: 'card-subtitle', id: 'lv-status' }, 'Chưa render'),
      ),
      el('div', { id: 'lv-header-actions', style: { display: 'flex', gap: '8px' } },
        el('button', {
          class: 'btn btn-sm btn-danger hidden',
          id: 'lv-clear-all',
          title: 'Xóa toàn bộ danh sách (huỷ tác vụ đang chạy)',
          onclick: clearCurrentTask,
        }, icon('trash', 14), 'Xóa danh sách'),
      ),
    ),
    el('div', { id: 'lv-progress' },
      el('div', { class: 'empty' },
        el('div', { class: 'empty-icon' }, icon('play', 32)),
        el('div', null, 'Bắt đầu render để xem tiến độ từng cảnh'),
      ),
    ),
  ));

  const list = root.querySelector('#lv-list');
  const countEl = root.querySelector('#lv-count');
  function refreshList() {
    // Preserve column scroll — see content.js refreshList (clear() collapses
    // height → scrollTop clamps to 0 → page jumps to top on row delete).
    const _scroller = list.closest('.gen-config, .gen-results');
    const _scrollTop = _scroller ? _scroller.scrollTop : 0;
    clear(list);
    form.prompts.forEach((p, i) => {
      const row = el('div', { class: 'prompt-row' },
        el('div', { class: 'row-number' }, String(i + 1)),
        el('textarea', { class: 'textarea', rows: 2, style: { flex: 1 },
          oninput: (e) => { form.prompts[i] = e.target.value; } }, p),
        el('div', { class: 'row-actions' },
          form.prompts.length > 2
            ? el('button', { class: 'btn btn-icon btn-ghost', onclick: () => {
                form.prompts.splice(i, 1); refreshList();
              } }, icon('trash', 14))
            : null,
        ),
      );
      row.querySelector('textarea').value = p;
      list.appendChild(row);
    });
    countEl.textContent = `${form.prompts.length} cảnh`;
    if (_scroller) _scroller.scrollTop = _scrollTop;
  }
  refreshList();

  root.querySelector('#lv-add').addEventListener('click', () => {
    if (form.prompts.length >= 20) return toast('Tối đa 20 cảnh', 'warning');
    form.prompts.push(''); refreshList();
  });

  // Image upload
  const dz = root.querySelector('#lv-dropzone');
  const fi = root.querySelector('#lv-image');
  const preview = root.querySelector('#lv-image-preview');
  dz.addEventListener('click', () => fi.click());
  wireDropzone(dz, fi);
  fi.addEventListener('change', async () => {
    const file = fi.files[0]; if (!file) return;
    try {
      const r = await api.content.uploadImage(file);
      form.startImagePath = r.path;
      form.startImagePreviewUrl = URL.createObjectURL(file);
      preview.innerHTML = '';
      preview.appendChild(el('img', { src: form.startImagePreviewUrl, class: 'thumb', style: { maxWidth: '200px' } }));
      refreshModelDropdown();   // hide Omni Flash since I2V isn't supported
      refreshDurationDropdown();
      toast('Đã tải ảnh', 'success');
    } catch (e) { toast(e.message, 'error'); }
  });

  // Start / cancel
  const startBtn = root.querySelector('#lv-start');
  const cancelBtn = root.querySelector('#lv-cancel');
  let _taskId = null;

  startBtn.addEventListener('click', async () => {
    const prompts = form.prompts.map(p => (p || '').trim()).filter(Boolean);
    if (prompts.length < 2) return toast('Cần ít nhất 2 cảnh', 'warning');
    if (!(await ensureFlowAccountOrWarn())) return;
    form.quality = root.querySelector('#lv-quality').value;
    form.aspect = root.querySelector('#lv-aspect').value;
    form.duration = parseInt(root.querySelector('#lv-duration').value || '8', 10);
    setLoading(startBtn, true);
    try {
      const targetFlowEmail = window.currentFlowAccount?.email || null;
      const res = await api.longVideo.start({
        prompts,
        quality: form.quality,
        aspect_ratio: form.aspect,
        duration: form.duration,
        start_image_path: form.startImagePath,
        task_name: (() => { let n = (form.taskName || '').trim(); if (!n) { n = defaultTaskName(); nameInput.value = n; } return n; })(),
        flow_account_email: targetFlowEmail,
      });
      tasksStore.register(res.task_id, 'long_video', {
        items: prompts,
        aspect: form.aspect,
        model: form.quality,
        flow_account_email: res.flow_account_email || targetFlowEmail,
      });
      attachToTask(res.task_id);
      toast(`Đã tạo task #${res.task_id}`, 'success');
    } catch (e) { toast(e.message, 'error'); }
    finally { setLoading(startBtn, false); }
  });

  cancelBtn.addEventListener('click', async () => {
    if (!_taskId) return;
    try { await api.longVideo.cancel(_taskId); toast('Đã hủy', 'info'); } catch (e) {}
  });

  async function clearCurrentTask() {
    if (!_taskId) return;
    try {
      const t = tasksStore.get(_taskId);
      if (t && t.status === 'running') {
        await api.longVideo.cancel(_taskId).catch(() => {});
      }
    } catch (e) { /* ignore */ }
    tasksStore.remove(_taskId);
    _taskId = null;
    renderProgress(null);
    toast('Đã xóa danh sách (file vẫn còn trên ổ đĩa)', 'info');
  }

  // Bulk-remove watermark from selected scene videos. `kind` picks the mask
  // ('veo_mini' | 'gemini') — the toolbar asks the user before calling.
  // Backend streams WS events → tasks_store sets per-item wm_status; gallery
  // chips update via the normal notify path.
  async function runBatchWatermark(paths, kind = 'veo_mini') {
    const handle = toast(`Đang xóa watermark cho ${paths.length} video… (mỗi video ~5-30s)`, 'info', 0);
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

  function renderProgress(taskState) {
    if (!root.isConnected) return;
    const wrap = root.querySelector('#lv-progress');
    if (!wrap) return;
    clear(wrap);
    const clearBtn = root.querySelector('#lv-clear-all');
    if (clearBtn) clearBtn.classList.toggle('hidden', !taskState);
    retryBtn.refresh(taskState);
    if (!taskState) {
      wrap.appendChild(el('div', { class: 'empty' },
        el('div', { class: 'empty-icon' }, icon('play', 32)),
        el('div', null, 'Bắt đầu render để xem tiến độ từng cảnh'),
      ));
      root.querySelector('#lv-status').textContent = 'Chưa render';
      cancelBtn.classList.add('hidden');
      return;
    }

    const strip = el('div', { class: 'scene-strip' });

    // Selection toolbar (Gen lại / Xóa WM / Upscale Video / Tải) — appears once
    // ≥1 scene is done. Each finished scene clip is treated as a gallery item,
    // mirroring the Tạo Video gallery so long-video gets the same batch actions.
    const hasAnyDone = taskState.items.some(it => it.status === 'done' && it.output_path);
    let toolbar = null;
    if (hasAnyDone) {
      toolbar = makeSelectionToolbar({
        getCards: () => [...strip.querySelectorAll('.scene-card[data-path]')],
        pathOf: (card) => card.dataset.path,
        itemOf: (card) => {
          const id = parseInt(card.dataset.itemId || '0', 10);
          if (!id) return null;
          const t = tasksStore.get(_taskId);
          return (t && t.items.find(x => x.id === id)) || { id };
        },
        onRegen: async () => {
          // Long video is an extend chain — a single scene can't be regenerated
          // in isolation. Re-run the whole task; the backend resumes from the
          // first non-completed scene (skips finished ones) and re-concats.
          tasksStore.resetErrorItems(_taskId);
          await api.tasks.retry(_taskId);
        },
        onRemoveWatermark: async (paths, kind) => { await runBatchWatermark(paths, kind); },
        onUpscaleVideo: async (paths) => {
          const videoExts = ['.mp4', '.mov', '.avi', '.mkv', '.webm'];
          const videoPaths = paths.filter(p => videoExts.some(e => p.toLowerCase().endsWith(e)));
          if (!videoPaths.length) return toast('Không có video nào được chọn', 'warning');
          window.__pendingUpscaleVideos = window.__pendingUpscaleVideos || [];
          videoPaths.forEach(vp => {
            const name = vp.split(/[/\\]/).pop();
            if (!window.__pendingUpscaleVideos.some(p => p.path === vp)) {
              window.__pendingUpscaleVideos.push({ path: vp, name });
            }
          });
          if (window.__app && window.__app.navigate) {
            window.__app.navigate('video-upscale');
            toast(`Đã thêm ${videoPaths.length} video vào danh sách nâng cấp!`, 'success');
          }
        },
      });
      wrap.appendChild(toolbar);
    }

    taskState.items.forEach((it, i) => {
      const card = el('div', { class: 'scene-card', 'data-scene': i + 1 });
      const thumb = el('div', { class: 'scene-thumb' },
        el('div', { class: 'scene-number' }, `Cảnh ${i + 1}`),
      );
      if (it.status === 'done' && it.output_url) {
        thumb.appendChild(el('video', { src: it.output_url, controls: true, style: { width: '100%', height: '100%' } }));
      } else if (it.status === 'error') {
        // Show the FULL friendly message (no 60-char truncation) + raw in tooltip,
        // matching content.js / storyboard.js so long messages aren't cut off.
        thumb.appendChild(el('div', {
          class: 'scene-error',
          title: it.error_detail || it.error || 'Lỗi',
          style: { color: 'var(--red)', fontSize: '11px', padding: '8px', textAlign: 'center', lineHeight: '1.35', overflowY: 'auto', maxHeight: '100%' },
        }, it.error || 'Lỗi'));
      } else {
        thumb.appendChild(el('div', { class: 'spinner' }));
      }
      card.appendChild(thumb);

      const chipClass = it.status === 'done' ? 'chip-green'
                     : it.status === 'error' ? 'chip-red'
                     : it.status === 'generating' ? 'chip-blue'
                     : 'chip-yellow';
      const chipText = it.status === 'done' ? 'Hoàn tất'
                     : it.status === 'error' ? 'Lỗi'
                     : it.status === 'generating' ? 'Đang render'
                     : 'Chờ';

      const meta = el('div', { class: 'scene-meta' },
        el('span', { class: `chip ${chipClass}` }, chipText)
      );

      // Download + watermark-status chip for finished scenes.
      let sceneActions = null;
      if (it.status === 'done' && it.output_url) {
        sceneActions = el('div', { class: 'scene-actions' },
          el('a', { href: it.output_url, download: '', class: 'btn btn-sm btn-ghost' },
            icon('download', 14), 'Tải'),
        );
        if (it.wm_status === 'running') {
          sceneActions.appendChild(el('span', { class: 'chip chip-blue', 'data-wm-chip': '1', style: { marginLeft: 'auto' }, title: it.wm_label || '' }, `Xóa WM ${Math.round(it.wm_progress || 0)}%`));
        } else if (it.wm_status === 'done' && it.wm_url) {
          sceneActions.appendChild(el('a', { href: it.wm_url, download: '', target: '_blank', class: 'chip chip-green', 'data-wm-chip': '1', style: { marginLeft: 'auto', textDecoration: 'none' }, title: 'Click để tải video đã xóa watermark' }, '✓ Đã xóa WM'));
        } else if (it.wm_status === 'error') {
          sceneActions.appendChild(el('span', { class: 'chip chip-red', 'data-wm-chip': '1', style: { marginLeft: 'auto' }, title: it.wm_error || 'Lỗi xóa watermark' }, 'WM lỗi'));
        }
      }

      card.appendChild(el('div', { class: 'scene-info' },
        meta,
        el('div', { class: 'scene-prompt' }, it.prompt),
        sceneActions,
      ));
      if (it.status === 'done' && it.output_path) {
        if (it.id != null) card.dataset.itemId = String(it.id);
        attachCardCheckbox(card, it.output_path, toolbar);
      }
      strip.appendChild(card);
    });
    wrap.appendChild(strip);

    const total = taskState.items.length;
    const doneCount = taskState.items.filter(x => x.status === 'done').length;
    let statusText;
    if (taskState.status === 'running') {
      statusText = `Đang xử lý ${doneCount}/${total} cảnh`;
      cancelBtn.classList.remove('hidden');
    } else if (taskState.status === 'completed') {
      statusText = `Hoàn tất ${doneCount}/${total} cảnh — đang ghép FFmpeg`;
      cancelBtn.classList.add('hidden');
    } else if (taskState.status === 'error') {
      statusText = `Lỗi: ${taskState.error_message || ''}`;
      cancelBtn.classList.add('hidden');
    } else if (taskState.status === 'cancelled') {
      statusText = 'Đã hủy';
      cancelBtn.classList.add('hidden');
    }
    if (taskState.flow_account_email) {
      statusText += ` • Flow: ${taskState.flow_account_email}`;
    }
    root.querySelector('#lv-status').textContent = statusText;
  }

  // "Gen lại N lỗi" button — for long_video, items = scenes. Failed scenes
  // are common (one scene fails → user wants to retry just that scene
  // without re-rendering the whole 5-min video).
  const retryBtn = makeRetryFailedButton({
    getTaskState: () => {
      const t = _taskId && tasksStore.get(_taskId);
      return t ? { taskId: _taskId, errorCount: t.error || 0, status: t.status } : null;
    },
    onResetUI: (id) => tasksStore.resetErrorItems(id),
    // Long video is an extend chain — retry re-runs the whole task, which
    // resumes from the first non-completed scene (skips done ones) and
    // re-concats. Per-item retry-failed (the default) would regenerate a scene
    // as a standalone clip via the content generator and break the chain.
    retryFn: (taskId) => api.tasks.retry(taskId),
  });
  root.querySelector('#lv-header-actions').prepend(retryBtn);

  let unsubscribe = null;
  function attachToTask(taskId) {
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
    _taskId = taskId;
    renderProgress(tasksStore.get(taskId));
    unsubscribe = tasksStore.on(taskId, (s) => renderProgress(s));
  }

  // Deep-link from Tasks Manager (eye icon) takes priority over "latest".
  const pending = window.__app && window.__app._pendingTaskId;
  if (pending != null) {
    window.__app._pendingTaskId = null;
    const existing = tasksStore.get(pending);
    if (existing) {
      attachToTask(existing.id);
    } else {
      tasksStore.getOrFetch(pending).then((t) => {
        if (!root.isConnected) return;
        if (t) {
          attachToTask(t.id);
        } else {
          const latest = tasksStore.latestByKind('long_video');
          if (latest) attachToTask(latest.id);
        }
      });
    }
  } else {
    const latest = tasksStore.latestByKind('long_video');
    if (latest) attachToTask(latest.id);
  }

  // `root` is the PERSISTENT #page-container so document.body.contains(root) is
  // always true — detect unmount via our own marker, else the subscription
  // leaks and the progress view jumps tasks on item events.
  const obs = new MutationObserver(() => {
    if (!root.querySelector('#lv-progress')) {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      obs.disconnect();
    }
  });
  obs.observe(document.body, { childList: true, subtree: true });
}
