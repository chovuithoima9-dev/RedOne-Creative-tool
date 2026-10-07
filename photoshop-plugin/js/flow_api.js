/**
 * Flow API Client — Google Flow integration via Chrome extension bridge.
 *
 * Runs inside the CEP panel (Node.js environment). Uses the bridge task
 * queue from embedded_server.js to route all Google API calls through
 * the Chrome extension, which executes them in the user's labs.google tab.
 *
 * No external server or Python process needed.
 */

/* global require */
const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');

// Import bridge from embedded_server
const { bridge } = require('./embedded_server.js');

// ── Configuration ───────────────────────────────────────────────
const AISANDBOX_BASE = 'https://aisandbox-pa.googleapis.com/v1';
const MAX_RETRY = 3;

const IMAGE_MODEL_MAP = {
  nano_banana_pro: 'GEM_PIX_2',
  nano_banana_2: 'HARBOR_SEAL',
  nano_banana_lite: 'HARBOR_SEAL',
  imagen_4: 'IMAGEN_3_5',
};

// ── State ───────────────────────────────────────────────────────
let _token = null;
let _tokenTs = 0;
let _projectId = '';

// ── Output directory ────────────────────────────────────────────
const OUTPUT_DIR = path.join(os.homedir(), 'RedOne_GenFill_Output');
try { fs.mkdirSync(OUTPUT_DIR, { recursive: true }); } catch (e) { /* ok */ }

// ── Auth Token ──────────────────────────────────────────────────

async function ensureToken() {
  if (_token && (Date.now() - _tokenTs) < 1800000) return;
  await fetchToken();
}

async function fetchToken() {
  console.log('[flow-api] Fetching auth session...');
  for (let attempt = 1; attempt <= 3; attempt++) {
    let r;
    try {
      r = await bridge.proxyFetch(
        'https://labs.google/fx/api/auth/session',
        'GET',
        { Accept: 'application/json' },
        null, 'json', 15000
      );
    } catch (e) {
      throw new Error('Extension offline: ' + e.message);
    }

    const status = r.status || 0;
    const body = r.body;

    if (status === 200 && body && body.access_token) {
      _token = body.access_token;
      _tokenTs = Date.now();
      console.log('[flow-api] Got token: ya29...' + _token.slice(-8));
      if (!_projectId) await fetchProjectId();
      return;
    }

    if (status === 0 && attempt < 3) {
      await sleep(1000 * attempt);
      continue;
    }

    throw new Error(
      'Không lấy được session. Kiểm tra Chrome extension + tab labs.google'
    );
  }
}

async function fetchProjectId() {
  try {
    const r = await bridge.proxyFetch(
      'https://labs.google/fx/api/trpc/user.getOrCreateUser',
      'GET',
      { Authorization: `Bearer ${_token}`, 'Content-Type': 'application/json' },
      null, 'json', 15000
    );
    const body = r.body || {};
    const json = ((body.result || {}).data || {}).json || {};
    if (json.projectId) {
      _projectId = json.projectId;
      console.log('[flow-api] Project ID:', _projectId);
    }
  } catch (e) {
    console.warn('[flow-api] fetchProjectId failed:', e.message);
  }
}

// ── Sandbox Request ─────────────────────────────────────────────

async function sandboxRequest(endpoint, payload, isTextPlain) {
  await ensureToken();
  const url = `${AISANDBOX_BASE}/${endpoint.replace(/^\//, '')}`;
  const bodyJson = JSON.stringify(payload);
  const ct = isTextPlain ? 'text/plain;charset=UTF-8' : 'application/json';

  console.log(`[flow-api] sandboxRequest: ${endpoint} (${ct}, body=${bodyJson.length} bytes)`);

  for (let attempt = 1; attempt <= MAX_RETRY; attempt++) {
    let r;
    try {
      r = await bridge.proxyFetch(url, 'POST', {
        Authorization: `Bearer ${_token}`,
        'Content-Type': ct,
      }, bodyJson, 'json', 120000);
    } catch (e) {
      console.error(`[flow-api] proxyFetch error (attempt ${attempt}):`, e.message);
      if (attempt < MAX_RETRY) { await sleep(2000 * attempt); continue; }
      return { error: e.message };
    }

    if (r.error) {
      console.error(`[flow-api] Bridge error (attempt ${attempt}):`, r.error);
      if (attempt < MAX_RETRY) { await sleep(2000 * attempt); continue; }
      return { error: r.error };
    }

    const status = r.status || 0;
    console.log(`[flow-api] Response: HTTP ${status} for ${endpoint}`);

    if (status === 401 || status === 403) {
      const errText = JSON.stringify(r.body || '').slice(0, 200).toLowerCase();
      if (errText.includes('recaptcha') || errText.includes('unusual_activity')) {
        return { error: `HTTP ${status}`, text: errText };
      }
      if (attempt < MAX_RETRY) {
        _token = null;
        await ensureToken();
        await sleep(1000);
        continue;
      }
    }

    if (status >= 200 && status < 300) {
      return (typeof r.body === 'object') ? r.body : { text: String(r.body).slice(0, 2000) };
    }

    const errText = JSON.stringify(r.body || r.body_text || '').slice(0, 500);
    console.error(`[flow-api] HTTP ${status} for ${endpoint}:`, errText);
    if (attempt < MAX_RETRY && status >= 500) { await sleep(2000 * attempt); continue; }
    return { error: `HTTP ${status}`, text: errText };
  }
  return { error: 'Max retries exceeded' };
}

// ── Upload Image ────────────────────────────────────────────────

async function uploadImage(filePath) {
  await ensureToken();

  const fileBytes = fs.readFileSync(filePath);
  const fileName = path.basename(filePath);
  const size = fileBytes.length;
  const mime = fileName.endsWith('.jpg') || fileName.endsWith('.jpeg')
    ? 'image/jpeg' : 'image/png';

  console.log(`[flow-api] Uploading ${fileName} (${size} bytes, ${mime})...`);

  // Encode as base64 — matches the real FlowClient._upload_image_raw
  const b64 = fileBytes.toString('base64');

  // Use the mask magic project for uploads too (matching HAR)
  const MASK_MAGIC_PROJECT = '01097a8c-0909-459b-acf3-4038572d17b1';

  const payload = {
    clientContext: {
      projectId: MASK_MAGIC_PROJECT,
      tool: 'PINHOLE',
    },
    imageBytes: b64,
    mimeType: mime,
  };

  // POST to /flow/uploadImage — Content-Type must be text/plain (from HAR)
  const result = await sandboxRequest('flow/uploadImage', payload, true);

  if (result.error) {
    const errMsg = result.text || result.error || 'Unknown upload error';
    console.error(`[flow-api] Upload failed for ${fileName}:`, errMsg);
    return null;
  }

  // Extract media_id from response — field is 'name'
  let mediaId = result.name || result.mediaId || result.media_id || '';

  // Deep search in nested response
  if (!mediaId) {
    const nested = result.media || result.image || result.result || result.response || {};
    if (typeof nested === 'object') {
      mediaId = nested.name || nested.mediaId || '';
    }
  }

  if (mediaId) {
    console.log(`[flow-api] Upload OK: ${fileName} → ${mediaId}`);
  } else {
    console.error('[flow-api] No media_id in upload response:', JSON.stringify(result).slice(0, 300));
  }

  return mediaId || null;
}

// ── Generate with Mask ──────────────────────────────────────────

async function generateImageWithMask(prompt, baseImageId, maskImageId, modelKey, seed, isUpscale = false) {
  await ensureToken();

  if (!seed) seed = Math.floor(Math.random() * 900000) + 100000;
  const modelName = IMAGE_MODEL_MAP[modelKey] || modelKey;

  // Get reCAPTCHA token
  let recaptchaToken = '';
  try { recaptchaToken = await bridge.harvestRecaptcha('IMAGE_GENERATION'); }
  catch (e) { console.warn('[flow-api] reCAPTCHA failed:', e.message); }

  const clientContext = {
    projectId: _projectId,
    tool: 'PINHOLE',
    sessionId: `;${Date.now()}`,
  };
  if (recaptchaToken) {
    clientContext.recaptchaContext = {
      token: recaptchaToken,
      applicationType: 'RECAPTCHA_APPLICATION_TYPE_WEB',
    };
  }

  // Augment prompt matching exactly the "Mask of Magic" app implementation
  const qualitySuffix = isUpscale 
    ? ", 8k resolution, highly detailed, photorealistic, sharp focus, masterpiece, cinematic lighting" 
    : "";
  const fullPrompt = `Nhiệm vụ: ${prompt}. Hòa quyện tự nhiên với nền. Mặt nạ chỉ định vùng cần sửa.. Match the exact aspect ratio and framing of the reference image${qualitySuffix}.`;

  // The mask magic applet lives in a specific project — must use that project
  // for the endpoint + clientContext, otherwise the applet isn't found.
  const MASK_MAGIC_PROJECT = '01097a8c-0909-459b-acf3-4038572d17b1';
  const maskCtx = { ...clientContext, projectId: MASK_MAGIC_PROJECT };

  const payload = {
    clientContext: maskCtx,
    mediaGenerationContext: { batchId: crypto.randomUUID() },
    useNewMedia: true,
    requests: [{
      clientContext: maskCtx,
      imageModelName: modelName,
      imageAspectRatio: 'IMAGE_ASPECT_RATIO_UNSPECIFIED',
      structuredPrompt: { parts: [{ text: fullPrompt }] },
      seed,
      imageInputs: [
        { imageInputType: 'IMAGE_INPUT_TYPE_REFERENCE', name: baseImageId },
        { imageInputType: 'IMAGE_INPUT_TYPE_REFERENCE', name: maskImageId },
      ],
      requestContext: {
        flowSdkInfo: {
          appletId: '54ee85a4-a175-4d14-a699-544f4bf86a21',
          appletVersionId: '4bde581b-6813-4639-9b72-337222808bbc',
        },
      },
    }],
  };

  const endpoint = `projects/${MASK_MAGIC_PROJECT}/flowMedia:batchGenerateImages`;
  console.log(`[flow-api] GenFill: model=${modelName}, seed=${seed}, project=${MASK_MAGIC_PROJECT}`);

  let result = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    if (attempt > 0) {
      await sleep(1500 + Math.random() * 1500);
      try { recaptchaToken = await bridge.harvestRecaptcha('IMAGE_GENERATION'); }
      catch (e) { /* ignore */ }
      if (recaptchaToken) {
        clientContext.recaptchaContext = {
          token: recaptchaToken,
          applicationType: 'RECAPTCHA_APPLICATION_TYPE_WEB',
        };
      }
    }

    result = await sandboxRequest(endpoint, payload, true);

    if (result.error) {
      const err = String(result.text || result.error || '').toLowerCase();
      const is429 = err.includes('429') || err.includes('resource_exhausted');
      const is403 = err.includes('403') || err.includes('recaptcha');

      if (is429 && attempt < 4) {
        const delay = Math.min(60, (5 * (attempt + 1)) + Math.random() * 8);
        console.warn(`[flow-api] Throttled (attempt ${attempt + 1}/5), wait ${delay.toFixed(1)}s`);
        await sleep(delay * 1000);
        continue;
      }
      if (is403 && attempt < 4) {
        console.warn(`[flow-api] reCAPTCHA rejected (attempt ${attempt + 1}/5)`);
        _token = null;
        await ensureToken();
        continue;
      }
      throw new Error(result.text || result.error);
    }
    break;
  }

  if (!result || result.error) {
    throw new Error(String((result || {}).text || (result || {}).error || 'Unknown'));
  }

  return extractImageResult(result);
}

function findFifeUrl(obj) {
  if (!obj || typeof obj !== 'object') return null;
  if (obj.fifeUrl) return obj.fifeUrl;
  if (obj.fife_url) return obj.fife_url;
  for (let key in obj) {
    const res = findFifeUrl(obj[key]);
    if (res) return res;
  }
  return null;
}

async function upscaleImageTo4K(mediaId) {
  await ensureToken();
  console.log(`[flow-api] Upscaling mediaId=${mediaId} to 4K`);

  let recaptchaToken = '';
  try { recaptchaToken = await bridge.harvestRecaptcha('IMAGE_GENERATION'); }
  catch (e) { console.warn('[flow-api] reCAPTCHA failed:', e.message); }

  const clientContext = {
    projectId: _projectId,
    tool: 'PINHOLE',
    userPaygateTier: 'PAYGATE_TIER_TWO',
    sessionId: `;${Date.now()}`,
  };
  if (recaptchaToken) {
    clientContext.recaptchaContext = {
      token: recaptchaToken,
      applicationType: 'RECAPTCHA_APPLICATION_TYPE_WEB',
    };
  }

  const payload = {
    mediaId: mediaId,
    targetResolution: 'UPSAMPLE_IMAGE_RESOLUTION_4K',
    clientContext: clientContext,
  };

  let result = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt > 0) await sleep(1500 + Math.random() * 2000);
    result = await sandboxRequest('flow/upsampleImage', payload);
    if (result.error) {
      if (String(result.text || result.error).includes('reCAPTCHA')) {
        _token = null; await ensureToken(); continue;
      }
      throw new Error(result.text || result.error);
    }
    break;
  }

  if (!result || result.error) {
    throw new Error(String((result || {}).text || (result || {}).error || 'Upscale failed'));
  }

  if (result.encodedImage) {
    return { encodedImage: Buffer.from(result.encodedImage, 'base64') };
  }
  const fifeUrl = findFifeUrl(result);
  if (fifeUrl) {
    return { fifeUrl };
  }
  throw new Error('Unknown upscale response format: ' + JSON.stringify(result).slice(0, 200));
}

function extractImageResult(result) {
  // Format 1: imagePanels (standard generate_image response)
  const panels = result.imagePanels || result.image_panels || [];
  let images;
  if (panels.length > 0) {
    images = panels[0].generatedImages || panels[0].generated_images || [];
  } else {
    images = result.generatedImages || result.generated_images || [];
  }

  if (images && images.length > 0) {
    const img = images[0];
    const mediaId = img.mediaGenerationId || img.media_generation_id || '';
    const encoded = img.encodedImage || img.encoded_image || '';
    let fifeUrl = '';
    if (typeof encoded === 'object') {
      fifeUrl = encoded.fifeUrl || encoded.fife_url || '';
    } else if (typeof encoded === 'string' && encoded.startsWith('http')) {
      fifeUrl = encoded;
    }

    let width = img.width || 0;
    let height = img.height || 0;
    if (typeof encoded === 'object') {
      width = width || encoded.imageWidth || encoded.width || 0;
      height = height || encoded.imageHeight || encoded.height || 0;
    }

    return {
      media_id: mediaId,
      download_url: fifeUrl,
      seed: img.seed || img.randomSeed || 0,
      width,
      height,
    };
  }

  // Format 2: media[].image.generatedImage (mask magic applet response)
  const mediaArr = result.media || [];
  if (mediaArr.length > 0) {
    const mediaItem = mediaArr[0];
    const imageData = mediaItem.image || {};
    const genImage = imageData.generatedImage || {};
    const dims = imageData.dimensions || {};

    const fifeUrl = genImage.fifeUrl || genImage.fife_url || '';
    const mediaId = genImage.mediaId || genImage.media_id ||
                    genImage.mediaGenerationId || '';

    if (fifeUrl) {
      return {
        media_id: mediaId,
        download_url: fifeUrl,
        seed: genImage.seed || 0,
        width: dims.width || 0,
        height: dims.height || 0,
      };
    }
  }

  throw new Error('No images in response: ' + JSON.stringify(result).slice(0, 500));
}

// ── Download Result ─────────────────────────────────────────────

function downloadImage(url) {
  return new Promise((resolve, reject) => {
    const client = url.startsWith('https') ? https : require('http');
    client.get(url, { timeout: 60000 }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        // Follow redirect
        downloadImage(res.headers.location).then(resolve).catch(reject);
        return;
      }
      if (res.statusCode !== 200) {
        reject(new Error(`Download failed: HTTP ${res.statusCode}`));
        return;
      }
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => resolve(Buffer.concat(chunks)));
      res.on('error', reject);
    }).on('error', reject);
  });
}

// ── Main Generate Function (called by panel UI) ─────────────────

/**
 * Full generative fill pipeline:
 *   1. Upload source image + mask via bridge
 *   2. Call batchGenerateImages with mask inputs
 *   3. Download result
 *   4. Save to disk + return base64
 *
 * @param {string} imagePath - Path to source image PNG
 * @param {string} maskPath  - Path to mask PNG (white = fill area)
 * @param {string} prompt    - Text prompt
 * @param {string} modelKey  - Model key
 * @returns {object} { ok, image_base64, media_id, width, height, seed, output_path }
 */
async function generateFill(imagePath, maskPath, prompt, modelKey, isUpscale = false) {
  await ensureToken();

  console.log(`[flow-api] generateFill: prompt="${prompt.slice(0, 60)}", model=${modelKey}, upscale=${isUpscale}`);

  // Upload image + mask
  const baseMediaId = await uploadImage(imagePath);
  if (!baseMediaId) throw new Error('Upload ảnh gốc thất bại');

  const maskMediaId = await uploadImage(maskPath);
  if (!maskMediaId) throw new Error('Upload mask thất bại');

  console.log(`[flow-api] Uploaded: base=${baseMediaId}, mask=${maskMediaId}`);

  // Generate
  const result = await generateImageWithMask(
    prompt, baseMediaId, maskMediaId, modelKey, null, isUpscale
  );

  let resultBytes;
  if (isUpscale && result.media_id) {
    try {
      console.log(`[flow-api] Initiating Upscale 4K for media_id: ${result.media_id}`);
      const upscaleRes = await upscaleImageTo4K(result.media_id);
      if (upscaleRes.encodedImage) {
        resultBytes = upscaleRes.encodedImage;
      } else if (upscaleRes.fifeUrl) {
        console.log('[flow-api] Downloading upscaled result...');
        resultBytes = await downloadImage(upscaleRes.fifeUrl);
      }
    } catch (upscaleErr) {
      console.error('[flow-api] Upscale failed, falling back to original image:', upscaleErr);
    }
  }

  // Fallback to original download URL if upscale wasn't requested or failed
  if (!resultBytes) {
    const downloadUrl = result.download_url;
    if (!downloadUrl) throw new Error('Không có URL kết quả từ Flow API');

    // Download result
    console.log('[flow-api] Downloading result...');
    resultBytes = await downloadImage(downloadUrl);
  }

  if (!resultBytes || resultBytes.length < 500) {
    throw new Error('Download kết quả thất bại');
  }

  // Save to disk
  const outName = `genfill_${Date.now()}.png`;
  const outPath = path.join(OUTPUT_DIR, outName);
  fs.writeFileSync(outPath, resultBytes);

  // Return base64
  const b64 = resultBytes.toString('base64');
  console.log(`[flow-api] Done: ${result.width}x${result.height}, saved to ${outPath}`);

  return {
    ok: true,
    image_base64: b64,
    media_id: result.media_id,
    width: result.width,
    height: result.height,
    seed: result.seed,
    output_path: outPath,
  };
}

// ── Helpers ─────────────────────────────────────────────────────

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// Node.js ≥19 has crypto.randomUUID, older doesn't — polyfill
if (!crypto.randomUUID) {
  crypto.randomUUID = function () {
    return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, c =>
      (c ^ crypto.randomBytes(1)[0] & 15 >> c / 4).toString(16)
    );
  };
}

// ── Exports ─────────────────────────────────────────────────────
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    generateFill,
    ensureToken,
    bridge,
    OUTPUT_DIR,
  };
}
