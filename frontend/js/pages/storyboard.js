// Storyboard generator — native port of the Google Flow "Storyboard Creator"
// tool. Idea + reference images → Gemini scene prompts → Nano Banana images,
// reusing the image task pipeline (batch concurrency "số luồng song song" +
// cooldown + WebSocket streaming). Output scenes can be sent in bulk to the
// "Tạo Video" tab as I2V (each scene image becomes the reference frame).
import { el, clear, toast, setLoading, icon, makeThumbnail, ensureFlowAccountOrWarn, geminiKeyNotice, openMediaViewer, openCompareViewer } from '../ui.js';
import { api } from '../api.js';
import { tasksStore } from '../tasks_store.js';
import { makeSelectionToolbar, attachCardCheckbox, makeRetryFailedButton, makePromptEditButton, makeItemRetryButton } from '../gallery_actions.js';

const IMAGE_MODELS = [
  { key: 'nano_banana_pro',  label: '🍌 Nano Banana Pro' },
  { key: 'nano_banana_2',    label: '🍌 Nano Banana 2' },
  { key: 'nano_banana_lite', label: '🍌 Nano Banana Lite' },
];
const ASPECTS = ['1:1', '16:9', '9:16', '4:3', '3:4'];
const DRAFT_IDEA_KEY = 'redone_storyboard_draft_idea';

// Module-level state survives SPA navigation (ref File objects too; cleared on F5).
const form = {
  idea: '', count: 4, model: 'nano_banana_pro', aspect: '16:9', concurrent: 4,
  refs: [],   // [{ file, previewUrl, name }]
};
let _taskId = null;
let _unsub = null;
let _generating = false;        // Gemini is writing scene prompts (before the task exists)
let _liveRenderSB = () => {};   // current mount's renderer (for async resolves)

function ratioToStyle(r) {
  return ({ '1:1': '1/1', '16:9': '16/9', '9:16': '9/16', '4:3': '4/3', '3:4': '3/4' })[r] || '16/9';
}

// Open the before/after comparison for a Flow-upscaled scene (original vs the
// 2K/4K result), with a download button inside the viewer.
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

// Upscale-state pill on a scene thumbnail (same look as the Tạo Ảnh gallery).
function _upscaleBadge(it) {
  const res = (it.upscale_resolution || '').toUpperCase();
  switch (it.upscale_status) {
    case 'queued':
      return el('div', { class: 'upscale-badge is-queued', title: 'Đang chờ tới lượt upscale' }, `⏳ Chờ ${res}`);
    case 'running':
      return el('div', { class: 'upscale-badge is-running' },
        el('span', { class: 'upscale-badge-spin' }), `Đang upscale ${res}…`);
    case 'done':
      return el('a', {
        class: 'upscale-badge is-done', href: '#',
        title: `Ảnh ${res} — click để so sánh trước/sau`,
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); openFlowUpscaleCompare(it); },
      }, `✓ ${res}`);
    case 'error':
      return el('div', { class: 'upscale-badge is-error', title: it.upscale_error || 'Lỗi upscale' }, `⚠ Lỗi ${res}`);
    default:
      return null;
  }
}

// Upscale selected scenes via the shared image upscale-batch endpoint (the
// storyboard items ARE image-task items with a media_id, so it just works).
async function runBatchUpscale(itemIds, resolution) {
  const RES = resolution.toUpperCase();
  // Upscale is now a queued task (pausable/cancellable in "Quản lý task").
  await api.image.upscaleBatch(itemIds, resolution);
  toast(
    `Đã thêm ${itemIds.length} cảnh vào hàng đợi upscale → ${RES}. `
    + `Vào "Quản lý task" để Tạm dừng / Tiếp tục / Hủy.`,
    'success',
  );
}

export function renderStoryboard(root) {
  // Nhắc nhập Gemini API key nếu chưa có (tab này cần Gemini).
  const _gkn = geminiKeyNotice();
  if (_gkn) root.appendChild(_gkn);
  root.appendChild(el('div', { class: 'page-hero' },
    el('div', { class: 'hero-icon' }, icon('image', 28)),
    el('div', { class: 'hero-text' },
      el('h2', null, 'Tạo Storyboard'),
      el('p', null, 'Ý tưởng + ảnh tham chiếu → kịch bản phân cảnh (prompt + ảnh) bằng Gemini + Nano Banana'),
    ),
  ));

  const layout = el('div', { class: 'gen-layout' });
  root.appendChild(layout);

  // ── LEFT: config ──
  const refCountLabel = el('span', null, `Ảnh tham chiếu (${form.refs.length}/10)`);
  const refGrid = el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '8px' } });

  function pickRefs() {
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = 'image/*'; inp.multiple = true;
    inp.onchange = async () => {
      for (const f of inp.files) {
        if (form.refs.length >= 10) break;
        form.refs.push({ file: f, previewUrl: await makeThumbnail(f), name: f.name });
      }
      renderRefs();
    };
    inp.click();
  }

  function renderRefs() {
    clear(refGrid);
    refCountLabel.textContent = `Ảnh tham chiếu (${form.refs.length}/10)`;
    form.refs.forEach((r, i) => {
      refGrid.appendChild(el('div', { style: { position: 'relative', aspectRatio: '1/1', borderRadius: '8px', overflow: 'hidden', border: '1px solid var(--border)' } },
        el('img', { src: r.previewUrl, style: { width: '100%', height: '100%', objectFit: 'cover' } }),
        el('div', {
          title: 'Bỏ ảnh',
          style: { position: 'absolute', top: '3px', right: '3px', width: '20px', height: '20px', borderRadius: '50%', background: 'rgba(0,0,0,0.6)', color: '#fff', display: 'grid', placeItems: 'center', fontSize: '14px', lineHeight: 1, cursor: 'pointer' },
          onclick: () => { form.refs.splice(i, 1); renderRefs(); },
        }, '×'),
      ));
    });
    if (form.refs.length < 10) {
      refGrid.appendChild(el('div', {
        onclick: pickRefs,
        style: { aspectRatio: '1/1', border: '1px dashed var(--border-strong)', borderRadius: '8px', display: 'grid', placeItems: 'center', cursor: 'pointer', color: 'var(--text-faint)' },
      }, icon('plus', 20)));
    }
  }

  const left = el('div', { class: 'gen-config' },
    el('div', { class: 'card' },
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Ý tưởng'),
        el('textarea', {
          class: 'textarea',
          id: 'sb2-idea',
          rows: 4,
          placeholder: 'Mô tả ý tưởng kịch bản của bạn...',
          oninput: (e) => {
            form.idea = e.target.value;
            try { localStorage.setItem(DRAFT_IDEA_KEY, e.target.value); } catch (_) {}
          },
        }),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, refCountLabel),
        refGrid,
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Model gen ảnh'),
        el('select', { class: 'select', id: 'sb2-model', onchange: (e) => { form.model = e.target.value; } },
          ...IMAGE_MODELS.map(m => el('option', { value: m.key }, m.label)),
        ),
      ),
      el('div', { class: 'form-row' },
        el('div', { class: 'field-group' },
          el('label', { class: 'field-label' }, 'Tỉ lệ'),
          el('select', { class: 'select', id: 'sb2-aspect', onchange: (e) => { form.aspect = e.target.value; } },
            ...ASPECTS.map(a => el('option', { value: a }, a)),
          ),
        ),
        el('div', { class: 'field-group' },
          el('label', { class: 'field-label' }, 'Số cảnh'),
          el('input', { type: 'number', class: 'input', id: 'sb2-count', min: 1, value: form.count, oninput: (e) => { form.count = Math.max(1, parseInt(e.target.value, 10) || 1); } }),
        ),
      ),
      el('div', { class: 'field-group' },
        el('label', { class: 'field-label' }, 'Số luồng song song'),
        el('input', { type: 'number', class: 'input', id: 'sb2-concurrent', min: 1, max: 8, value: form.concurrent, oninput: (e) => { form.concurrent = Math.max(1, Math.min(8, parseInt(e.target.value, 10) || 1)); } }),
      ),
      el('button', { class: 'btn btn-primary', style: { width: '100%', marginTop: '12px' }, id: 'sb2-go', onclick: generate },
        icon('sparkles'), 'Tạo Storyboard',
      ),
    ),
  );
  layout.appendChild(left);
  renderRefs();

  // ── RIGHT: results ──
  const right = el('div', { class: 'gen-results' },
    el('div', { class: 'card' },
      el('div', { class: 'card-header' },
        el('h3', { class: 'card-title', id: 'sb2-results-title' }, 'Storyboard'),
        el('div', { class: 'card-subtitle', id: 'sb2-status' }, 'Chưa tạo'),
      ),
      el('div', { id: 'sb2-actions', style: { marginBottom: '12px' } }),
      el('div', { id: 'sb2-results' },
        el('div', { class: 'empty' },
          el('div', { class: 'empty-icon' }, icon('image', 32)),
          el('div', null, 'Nhập ý tưởng + ảnh tham chiếu rồi bấm Tạo Storyboard'),
        ),
      ),
    ),
  );
  layout.appendChild(right);

  // Restore draft idea from localStorage if empty
  if (!form.idea) {
    try {
      const savedDraft = localStorage.getItem(DRAFT_IDEA_KEY);
      if (savedDraft) form.idea = savedDraft;
    } catch (_) {}
  }

  // Restore field values (survive navigation)
  root.querySelector('#sb2-idea').value = form.idea;
  root.querySelector('#sb2-model').value = form.model;
  root.querySelector('#sb2-aspect').value = form.aspect;
  root.querySelector('#sb2-count').value = form.count;
  root.querySelector('#sb2-concurrent').value = form.concurrent;

  async function generate() {
    const idea = (form.idea || '').trim();
    if (!idea && form.refs.length === 0) return toast('Cần nhập ý tưởng hoặc thêm ảnh tham chiếu', 'warning');
    if (!(await ensureFlowAccountOrWarn())) return;
    // Build the request synchronously (capture inputs) before awaiting.
    const fd = new FormData();
    fd.append('idea', idea);
    fd.append('count', String(form.count));
    fd.append('model', form.model);
    fd.append('aspect_ratio', form.aspect);
    fd.append('concurrent', String(form.concurrent));
    const targetFlowEmail = window.currentFlowAccount?.email || null;
    if (targetFlowEmail) {
      fd.append('flow_account_email', targetFlowEmail);
    }
    form.refs.forEach(r => fd.append('refs', r.file));
    _generating = true;
    _liveRenderSB();   // show "đang viết kịch bản" spinner on the current page
    try {
      const res = await api.storyboard.start(fd);
      _generating = false;
      // Register so WS image events (carrying the prompt) drive the scene cards.
      tasksStore.register(res.task_id, 'storyboard', {
        items: res.prompts,
        aspect: form.aspect,
        name: res.name || '',
        idea: idea,
        flow_account_email: res.flow_account_email || targetFlowEmail,
      });
      _taskId = res.task_id;
      _liveRenderSB();   // attach + render on whichever page is shown now
      toast(`Đã tạo ${res.prompts.length} phân cảnh — đang gen ảnh (${res.model_used})`, 'success');
    } catch (e) {
      _generating = false;
      _liveRenderSB();
      toast(e.message, 'error');
    }
  }

  // Coalesce the WS-event render storm to ~1 rebuild per frame (perf for
  // many-scene tasks). Per-mount closure so it captures THIS mount's
  // renderGallery + root; the isConnected guard makes a post-navigation flush
  // a harmless no-op.
  let _rafId = 0;
  function scheduleRender() {
    if (_rafId) return;
    _rafId = requestAnimationFrame(() => {
      _rafId = 0;
      if (!root.isConnected) return;
      renderGallery(tasksStore.get(_taskId));
    });
  }
  function attachToTask(taskId) {
    if (_unsub) { _unsub(); _unsub = null; }
    if (_rafId) { cancelAnimationFrame(_rafId); _rafId = 0; }
    _taskId = taskId;
    renderGallery(tasksStore.get(taskId));        // immediate first paint
    _unsub = tasksStore.on(taskId, scheduleRender);
  }

  // Render the current gen state into THIS mount: the Gemini "đang viết kịch
  // bản" spinner, or the task gallery, or empty. Wired to `_liveRenderSB` so an
  // in-flight /storyboard/start resolve lands on the page now shown (fixes
  // switch-tab-mid-generation → blank).
  function liveRender() {
    if (!root.isConnected) return;
    const btn = root.querySelector('#sb2-go');
    if (_generating) {
      if (btn) btn.disabled = true;
      const wrap = root.querySelector('#sb2-results');
      const actions = root.querySelector('#sb2-actions');
      const statusEl = root.querySelector('#sb2-status');
      if (actions) clear(actions);
      clear(wrap);
      wrap.appendChild(el('div', { class: 'empty' },
        el('div', { class: 'spinner' }),
        el('div', { style: { marginTop: '10px' } }, 'Đang viết kịch bản (Gemini)…'),
      ));
      if (statusEl) statusEl.textContent = 'Đang tạo kịch bản…';
      return;
    }
    if (btn) btn.disabled = false;
    if (_taskId && tasksStore.get(_taskId)) attachToTask(_taskId);
    else renderGallery(null);
  }
  _liveRenderSB = liveRender;

  // Send the SELECTED scenes (by item id) to the Tạo Video tab as I2V.
  function sendSelectedToI2V(idList) {
    const t = _taskId ? tasksStore.get(_taskId) : null;
    if (!t) return;
    const byId = new Map(t.items.map(it => [it.id, it]));
    const chosen = idList.map(id => byId.get(id)).filter(it => it && it.status === 'done' && it.output_path);
    const scenes = chosen.map(it => {
      // Prefer the upscaled image when a 2K/4K upscale finished for this scene.
      const up = it.upscale_status === 'done' && it.upscale_path;
      return {
        prompt: it.prompt || '',
        path: up ? it.upscale_path : it.output_path,
        url: up ? it.upscale_url : it.output_url,
        name: 'scene',
      };
    });
    if (!scenes.length) return toast('Chọn cảnh đã hoàn thành để gửi', 'warning');
    const upN = chosen.filter(it => it.upscale_status === 'done' && it.upscale_path).length;
    sessionStorage.setItem('inject_i2v', JSON.stringify(scenes));
    window.__app.navigate('content');
    toast(`Đã gửi ${scenes.length} cảnh sang Tạo Video (I2V)${upN ? ` (${upN} ảnh đã upscale)` : ''}`, 'success');
  }

  // Send SELECTED scenes to the Shakker Upscale tab (green button). Hands local
  // urls over and switches IMMEDIATELY — the upload to Shakker runs in the
  // Upscale panel (each shows "Đang tải lên…"), so we don't block the switch.
  function sendSelectedToUpscale(idList) {
    const t = _taskId ? tasksStore.get(_taskId) : null;
    if (!t) return;
    const byId = new Map(t.items.map(it => [it.id, it]));
    const chosen = idList.map(id => byId.get(id)).filter(it => it && it.status === 'done' && it.output_path);
    if (!chosen.length) return toast('Chọn cảnh đã hoàn thành để gửi', 'warning');
    // Always send the ORIGINAL (output_url) to Shakker upscale — never the Flow
    // 2K/4K, so the user re-upscales the source scene, not an upscaled one.
    const out = chosen.map(it => ({
      url: it.output_url, label: (it.prompt || 'cảnh').slice(0, 40), needsUpload: true,
    })).filter(x => x.url);
    if (!out.length) return toast('Ảnh chưa sẵn sàng', 'warning');
    window.__app._pendingUpscale = out;
    window.__app.navigate('shakker');
    toast(`Đã gửi ${out.length} ảnh sang Upscale Shakker (đang tải lên…)`, 'success');
  }

  function renderGallery(state) {
    if (!root.isConnected) return;
    const wrap = root.querySelector('#sb2-results');
    const actions = root.querySelector('#sb2-actions');
    const statusEl = root.querySelector('#sb2-status');
    if (!wrap) return;
    clear(wrap); clear(actions);

    // Show WHICH storyboard task these scenes belong to (task name).
    const titleEl = root.querySelector('#sb2-results-title');
    if (titleEl) {
      const acctTag = state?.flow_account_email ? ` [${state.flow_account_email.split('@')[0]}]` : '';
      titleEl.textContent = state
        ? `Storyboard — ${state.name || ('Task #' + state.id)}${acctTag}`
        : 'Storyboard';
    }

    if (!state) {
      wrap.appendChild(el('div', { class: 'empty' },
        el('div', { class: 'empty-icon' }, icon('image', 32)),
        el('div', null, 'Nhập ý tưởng + ảnh tham chiếu rồi bấm Tạo Storyboard'),
      ));
      statusEl.textContent = 'Chưa tạo';
      return;
    }

    // Hiển thị ý tưởng kịch bản gốc nếu có (để xem lại bất cứ khi nào mở task)
    const taskIdea = (state.idea || (state.items && state.items.find(x => x.extra && x.extra.idea)?.extra?.idea) || '').trim();
    if (taskIdea) {
      const ideaCard = el('div', {
        class: 'storyboard-idea-card',
        style: {
          background: 'var(--card-bg, #1a1b20)',
          border: '1px solid var(--border)',
          borderLeft: '4px solid var(--brand)',
          borderRadius: '8px',
          padding: '12px 14px',
          marginBottom: '14px',
          display: 'flex',
          flexDirection: 'column',
          gap: '8px',
        },
      },
        el('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px', flexWrap: 'wrap' } },
          el('div', { style: { display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600, fontSize: '13px', color: 'var(--brand)' } },
            icon('sparkles', 14),
            'Ý tưởng kịch bản gốc',
          ),
          el('div', { style: { display: 'flex', gap: '6px' } },
            el('button', {
              class: 'btn btn-sm btn-ghost',
              title: 'Sao chép ý tưởng',
              onclick: () => {
                navigator.clipboard.writeText(taskIdea);
                toast('Đã copy ý tưởng kịch bản', 'success');
              },
            }, icon('copy', 13), ' Copy'),
            el('button', {
              class: 'btn btn-sm btn-ghost',
              title: 'Nạp ý tưởng này vào ô nhập kịch bản bên trái',
              onclick: () => {
                form.idea = taskIdea;
                try { localStorage.setItem(DRAFT_IDEA_KEY, taskIdea); } catch (_) {}
                const txt = root.querySelector('#sb2-idea');
                if (txt) { txt.value = taskIdea; txt.focus(); }
                toast('Đã nạp lại ý tưởng vào ô nhập', 'success');
              },
            }, icon('edit', 13), ' Dùng lại ý tưởng này'),
          ),
        ),
        el('div', {
          style: {
            fontSize: '13px',
            lineHeight: '1.5',
            color: 'var(--text-main, #e2e8f0)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            background: 'rgba(0,0,0,0.18)',
            padding: '8px 10px',
            borderRadius: '6px',
            maxHeight: '160px',
            overflowY: 'auto',
          },
        }, taskIdea),
      );
      wrap.appendChild(ideaCard);
    }

    // Scene order = DB item id ascending (creation order); unclaimed slots tail.
    const items = [...state.items].sort((a, b) => {
      if (a.id == null && b.id == null) return 0;
      if (a.id == null) return 1;
      if (b.id == null) return -1;
      return a.id - b.id;
    });

    const grid = el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '14px' } });

    // Bulk "send to I2V" + selection toolbar (upscale 2K/4K) — once ≥1 done.
    const doneScenes = items.filter(it => it.status === 'done' && it.output_path);
    let toolbar = null;
    if (doneScenes.length) {
      toolbar = makeSelectionToolbar({
        getCards: () => [...grid.querySelectorAll('.scene-card[data-path]')],
        pathOf: (card) => card.dataset.path,
        // Live store item → toolbar sees media_id + upscale_status (regen skips
        // scenes mid-upscale). Falls back to a dataset shape.
        itemOf: (card) => {
          const id = parseInt(card.dataset.itemId || '0', 10);
          if (!id) return null;
          const t = tasksStore.get(state.id);
          return (t && t.items.find(x => x.id === id))
            || { id, mediaId: card.dataset.mediaId || null };
        },
        onUpscale: async (ids, res) => { await runBatchUpscale(ids, res); },
        onSendToI2V: async (ids) => sendSelectedToI2V(ids),
        onSendToUpscale: async (ids) => sendSelectedToUpscale(ids),
        onRegen: async (ids) => {
          await api.tasks.retryItems(state.id, ids);
          ids.forEach(iid => tasksStore.retryItemUI(state.id, iid, 'pending'));
        },
      });
      wrap.appendChild(toolbar);
    }

    // "Gen lại N lỗi" — regen ALL failed scenes at once. Error scenes have no
    // file → can't be ticked in the toolbar, so this covers them (done scenes
    // use the toolbar's "Gen lại"). Rebuilt each render alongside the toolbar.
    if (items.some(it => it.status === 'error')) {
      const retryBtn = makeRetryFailedButton({
        getTaskState: () => {
          const t = state && state.id ? tasksStore.get(state.id) : null;
          return t ? { taskId: state.id, errorCount: t.error || 0, status: t.status } : null;
        },
        onResetUI: (id) => tasksStore.resetErrorItems(id),
      });
      retryBtn.refresh(state);
      wrap.appendChild(retryBtn);
    }

    const aspectStyle = ratioToStyle(state.aspect || form.aspect);
    items.forEach((it, i) => {
      const card = el('div', { class: 'scene-card' });
      const thumb = el('div', { class: 'scene-thumb', style: { aspectRatio: aspectStyle } },
        el('div', { class: 'scene-number' }, `SCENE ${i + 1}`),
      );
      if (it.status === 'done' && it.output_url) {
        const img = el('img', { src: it.output_url, loading: 'lazy', decoding: 'async', style: { width: '100%', height: '100%', objectFit: 'cover', cursor: 'zoom-in' } });
        img.addEventListener('click', () => {
          const galleryItems = items
            .filter(x => x.status === 'done' && x.output_url)
            .map((x, j) => ({ url: x.output_url, type: 'image', label: `Scene ${j + 1} ${x.prompt || ''}`.trim() }));
          const doneIndex = items.slice(0, i + 1).filter(x => x.status === 'done' && x.output_url).length - 1;
          openMediaViewer({ items: galleryItems, currentIndex: doneIndex });
        });
        thumb.appendChild(img);
      } else if (it.status === 'error') {
        thumb.appendChild(el('div', {
          class: 'scene-error',
          title: it.error_detail || it.error || 'Lỗi',
          style: { color: 'var(--red)', fontSize: '11px', padding: '8px', textAlign: 'center', lineHeight: '1.35', overflowY: 'auto', maxHeight: '100%' },
        }, it.error || 'Lỗi'));
      } else {
        thumb.appendChild(el('div', { class: 'spinner' }));
      }
      // Upscale state overlay (queued / running / done / error)
      if (it.upscale_status) {
        const b = _upscaleBadge(it);
        if (b) thumb.appendChild(b);
        if (it.upscale_status === 'queued') card.classList.add('is-upscale-queued');
      }
      card.appendChild(thumb);
      if (it.upscale_status === 'running') card.appendChild(el('div', { class: 'upscale-progress-bar' }));

      const actionsRow = el('div', { class: 'scene-actions' });
      if (it.status === 'error' && it.id != null) {
        actionsRow.appendChild(makeItemRetryButton(state.id, it.id));
      }
      if (it.output_url) {
        actionsRow.appendChild(el('a', { href: it.output_url, download: '', class: 'btn btn-sm btn-ghost', title: 'Tải ảnh gốc' }, icon('download', 14), 'Tải'));
      }
      if (it.upscale_status === 'done' && it.upscale_url) {
        actionsRow.appendChild(el('a', {
          href: it.upscale_url, download: '', target: '_blank',
          class: 'btn btn-sm btn-ghost sa-accent', title: 'Tải ảnh đã upscale (bấm chip trên ảnh để so sánh)',
        }, icon('download', 14), `Tải ${(it.upscale_resolution || '').toUpperCase()}`));
      }
      actionsRow.appendChild(el('button', {
        class: 'btn btn-sm btn-ghost btn-icon', title: 'Copy prompt', style: { marginLeft: 'auto' },
        onclick: () => { navigator.clipboard.writeText(it.prompt || ''); toast('Đã copy prompt', 'success'); },
      }, icon('copy', 14)));
      if (it.id != null) {
        actionsRow.appendChild(makePromptEditButton({ taskId: state.id, item: it }));
      }
      // (Per-scene "Gen lại" removed — regen is on the selection toolbar:
      //  tick scenes → "Gen lại". Keeps the card from overflowing.)

      card.appendChild(el('div', { class: 'scene-info' },
        el('div', { class: 'scene-prompt', style: { WebkitLineClamp: 4 } }, it.prompt || 'Đang viết kịch bản...'),
        actionsRow,
      ));

      // Make done scenes selectable (checkbox) so they can be upscaled.
      if (it.status === 'done' && it.output_path) {
        if (it.id != null) card.dataset.itemId = String(it.id);
        if (it.media_id) card.dataset.mediaId = it.media_id;
        attachCardCheckbox(card, it.output_path, toolbar);
      }
      grid.appendChild(card);
    });
    wrap.appendChild(grid);
    if (toolbar && toolbar._setUpscaleProgress) {
      // Scope progress to THIS task so it doesn't bleed into the Tạo Ảnh tab.
      const ub = tasksStore.getUpscaleBatch();
      toolbar._setUpscaleProgress(ub.active && ub.taskId === state.id ? ub : null);
    }

    const total = state.total || items.length;
    let stText = state.status === 'completed'
      ? `Xong ${state.done || 0}/${total}${state.error ? ` • lỗi ${state.error}` : ''}`
      : `Đang gen ${(state.done || 0) + (state.error || 0)}/${total}`;
    if (state.flow_account_email) {
      stText += ` • Flow: ${state.flow_account_email}`;
    }
    statusEl.textContent = stText;
  }

  // On mount: Tasks Manager eye deep-link takes priority; otherwise liveRender
  // shows the in-flight Gemini spinner (if a generation is still running) or
  // re-attaches to the last task.
  const pending = window.__app && window.__app._pendingTaskId;
  if (pending != null) {
    window.__app._pendingTaskId = null;
    const existing = tasksStore.get(pending);
    if (existing) {
      attachToTask(pending);
      if (existing.idea && !form.idea) {
        form.idea = existing.idea;
        const txt = root.querySelector('#sb2-idea');
        if (txt) txt.value = existing.idea;
      }
    } else {
      // Not in memory (e.g. app restart or older task) -> fetch from backend!
      api.tasks.get(pending).then((res) => {
        if (!root.isConnected) return;
        if (res && res.task) {
          const t = tasksStore.ingest(res.task, res.items || []);
          if (t) {
            attachToTask(t.id);
            if (t.idea && !form.idea) {
              form.idea = t.idea;
              const txt = root.querySelector('#sb2-idea');
              if (txt) txt.value = t.idea;
            }
          }
        }
      }).catch((e) => {
        console.warn('Failed to load pending storyboard task', e);
        liveRender();
      });
    }
  } else if (_taskId && tasksStore.get(_taskId)) {
    attachToTask(_taskId);
  } else {
    const latest = tasksStore.latestByKind('storyboard');
    if (latest) attachToTask(latest.id);
    else liveRender();
  }

  // Cleanup when navigated away. `root` is the PERSISTENT #page-container
  // (navigate() only swaps its children), so we detect unmount via our own
  // marker (#sb2-results). Without this the tasksStore.on() subscription leaks
  // and keeps repainting the gallery with its task on item events (the gallery
  // "jumps to another task" bug). `_taskId` stays module-level for sticky view.
  const _sbObs = new MutationObserver(() => {
    if (!root.querySelector('#sb2-results')) {
      if (_unsub) { _unsub(); _unsub = null; }
      if (_rafId) { cancelAnimationFrame(_rafId); _rafId = 0; }
      _sbObs.disconnect();
    }
  });
  _sbObs.observe(document.body, { childList: true, subtree: true });
}
