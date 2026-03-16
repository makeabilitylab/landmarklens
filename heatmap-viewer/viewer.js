// ============================================
// Gaze Heatmap Viewer
// ============================================

const FRAMES_BASE = '/web/frames';

const GOOD_PIDS = Array.from({ length: 10 }, (_, i) => `p${String(i + 1).padStart(2, '0')}`);
const POOR_PIDS = Array.from({ length: 10 }, (_, i) => `p${String(i + 11).padStart(2, '0')}`);

const NEIGHBORHOOD_NAMES = {
  ginza: 'Ginza (銀座)',
  kinshicho: 'Kinshichō (錦糸町)',
  koenji: 'Kōenji (高円寺)',
  nezu: 'Nezu (根津)',
};

const GROUP_COLORS = { good: '#00BFC4', poor: '#F8766D' };
const PARTICIPANT_COLORS = Object.fromEntries([
  ...GOOD_PIDS.map(pid => [pid, GROUP_COLORS.good]),
  ...POOR_PIDS.map(pid => [pid, GROUP_COLORS.poor]),
]);

// ── Heatmap config ────────────────────────────────────

const HM_GRID_W = 360;
const HM_GRID_H = 180;
const HM_SIGMA = 10;
const HM_GLOBAL_OPACITY = 0.70;

// Rainbow colormap matching generate_group_heatmaps.py
// Breakpoints: [threshold, [R, G, B], alpha]  (converted from OpenCV BGR)
const HM_BREAKPOINTS = [
  [0.00, [20,  50, 120], 0.55],
  [0.01, [20,  50, 120], 0.55],
  [0.15, [10,  60, 170], 0.70],
  [0.30, [10, 180, 200], 0.80],
  [0.50, [20, 210,  60], 0.85],
  [0.70, [250, 230, 30], 0.85],
  [0.85, [250, 100, 15], 0.90],
  [1.00, [255,  20, 15], 0.90],
];

const HM_COLOR_LUT = new Uint8Array(256 * 3);
const HM_ALPHA_LUT = new Float32Array(256);

(function buildHeatmapLuts() {
  const bp = HM_BREAKPOINTS;
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    for (let j = 0; j < bp.length - 1; j++) {
      const [t0, c0, a0] = bp[j];
      const [t1, c1, a1] = bp[j + 1];
      if (t <= t1 || j === bp.length - 2) {
        const frac = t1 === t0 ? 0 : Math.min((t - t0) / (t1 - t0), 1);
        HM_COLOR_LUT[i * 3]     = Math.round(c0[0] + frac * (c1[0] - c0[0]));
        HM_COLOR_LUT[i * 3 + 1] = Math.round(c0[1] + frac * (c1[1] - c0[1]));
        HM_COLOR_LUT[i * 3 + 2] = Math.round(c0[2] + frac * (c1[2] - c0[2]));
        HM_ALPHA_LUT[i] = a0 + frac * (a1 - a0);
        break;
      }
    }
  }
})();

// ── State ─────────────────────────────────────────────

let routesMeta = null;
let currentRouteId = null;
let currentRoute = null;
let currentFrameIndex = 0;
let gazeData = null;
let selectedPids = new Set();
let showHeatmapGood = false;
let showHeatmapPoor = false;
let viewer = null;
let currentBlobUrl = null;
let cachedPanorama = null;
let markerScale = 0.70;
let isLoading = false;

// Reusable off-screen canvas for heatmap layers
const hmCanvas = document.createElement('canvas');
hmCanvas.width = HM_GRID_W;
hmCanvas.height = HM_GRID_H;
const hmCtx = hmCanvas.getContext('2d');

// ── DOM refs ──────────────────────────────────────────

const els = {};

function cacheElements() {
  els.routeSelect = document.getElementById('route-select');
  els.prevBtn = document.getElementById('prev-btn');
  els.nextBtn = document.getElementById('next-btn');
  els.frameInput = document.getElementById('frame-input');
  els.frameTotal = document.getElementById('frame-total');
  els.frameId = document.getElementById('frame-id');
  els.panorama = document.getElementById('panorama');
  els.loadingOverlay = document.getElementById('loading-overlay');
  els.toggleGood = document.getElementById('toggle-good');
  els.togglePoor = document.getElementById('toggle-poor');
  els.heatmapGood = document.getElementById('heatmap-good');
  els.heatmapPoor = document.getElementById('heatmap-poor');
  els.goodPids = document.getElementById('good-participants');
  els.poorPids = document.getElementById('poor-participants');
  els.opacitySlider = document.getElementById('opacity-slider');
  els.opacityValue = document.getElementById('opacity-value');
  els.statSelected = document.getElementById('stat-selected');
  els.statPoints = document.getElementById('stat-points');
}

// ── Initialization ────────────────────────────────────

async function init() {
  cacheElements();
  showLoading(true);

  try {
    const resp = await fetch('data/routes.json');
    routesMeta = await resp.json();
  } catch (e) {
    console.error('Failed to load routes.json:', e);
    return;
  }

  buildRouteSelector();
  buildParticipantGroup(els.goodPids, GOOD_PIDS, 'toggle-good');
  buildParticipantGroup(els.poorPids, POOR_PIDS, 'toggle-poor');
  bindEvents();

  const firstRoute = Object.keys(routesMeta)[0];
  await loadRoute(firstRoute);
}

// ── UI building ───────────────────────────────────────

function buildRouteSelector() {
  for (const [routeId, route] of Object.entries(routesMeta)) {
    const opt = document.createElement('option');
    opt.value = routeId;
    opt.textContent = NEIGHBORHOOD_NAMES[route.neighborhood] || route.neighborhood;
    els.routeSelect.appendChild(opt);
  }
}

function buildParticipantGroup(container, pids, groupToggleId) {
  pids.forEach(pid => {
    const label = document.createElement('label');
    label.className = 'pid-checkbox';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.pid = pid;
    cb.addEventListener('change', () => {
      if (cb.checked) selectedPids.add(pid);
      else selectedPids.delete(pid);
      syncGroupToggle(groupToggleId, pids);
      onVisualChange();
    });

    const colorDot = document.createElement('span');
    colorDot.className = 'pid-color';
    colorDot.style.backgroundColor = PARTICIPANT_COLORS[pid];

    label.appendChild(cb);
    label.appendChild(colorDot);
    label.appendChild(document.createTextNode(pid));
    container.appendChild(label);
  });
}

function syncGroupToggle(toggleId, pids) {
  const toggle = document.getElementById(toggleId);
  const allChecked = pids.every(pid => selectedPids.has(pid));
  const someChecked = pids.some(pid => selectedPids.has(pid));
  toggle.checked = allChecked;
  toggle.indeterminate = someChecked && !allChecked;
}

function setGroupCheckboxes(container, pids, checked) {
  const checkboxes = container.querySelectorAll('input[type="checkbox"]');
  checkboxes.forEach(cb => {
    cb.checked = checked;
    if (checked) selectedPids.add(cb.dataset.pid);
    else selectedPids.delete(cb.dataset.pid);
  });
}

// ── Events ────────────────────────────────────────────

function bindEvents() {
  els.routeSelect.addEventListener('change', () => loadRoute(els.routeSelect.value));

  els.prevBtn.addEventListener('click', () => navigateFrame(-1));
  els.nextBtn.addEventListener('click', () => navigateFrame(1));

  els.frameInput.addEventListener('change', () => {
    const num = parseInt(els.frameInput.value, 10);
    if (!isNaN(num) && num >= 1 && num <= currentRoute.frames.length) {
      loadFrame(num - 1);
    } else {
      els.frameInput.value = currentFrameIndex + 1;
    }
  });
  els.frameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); els.frameInput.dispatchEvent(new Event('change')); }
  });

  els.toggleGood.addEventListener('change', () => {
    setGroupCheckboxes(els.goodPids, GOOD_PIDS, els.toggleGood.checked);
    onVisualChange();
  });
  els.togglePoor.addEventListener('change', () => {
    setGroupCheckboxes(els.poorPids, POOR_PIDS, els.togglePoor.checked);
    onVisualChange();
  });

  els.heatmapGood.addEventListener('change', () => {
    showHeatmapGood = els.heatmapGood.checked;
    onVisualChange();
  });
  els.heatmapPoor.addEventListener('change', () => {
    showHeatmapPoor = els.heatmapPoor.checked;
    onVisualChange();
  });

  let scaleTimer = null;
  els.opacitySlider.addEventListener('input', () => {
    markerScale = parseInt(els.opacitySlider.value, 10) / 100;
    els.opacityValue.textContent = `${els.opacitySlider.value}%`;
    if (selectedPids.size > 0 || showHeatmapGood || showHeatmapPoor) {
      clearTimeout(scaleTimer);
      scaleTimer = setTimeout(() => onVisualChange(), 150);
    }
  });

  document.addEventListener('keydown', onKeyDown);
}

function onKeyDown(e) {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft' || e.key === 'a') { e.preventDefault(); navigateFrame(-1); }
  else if (e.key === 'ArrowRight' || e.key === 'd') { e.preventDefault(); navigateFrame(1); }
}

function navigateFrame(delta) {
  const newIndex = currentFrameIndex + delta;
  if (newIndex >= 0 && newIndex < currentRoute.frames.length) {
    loadFrame(newIndex);
  }
}

// ── Route / Frame loading ─────────────────────────────

async function loadRoute(routeId) {
  showLoading(true);
  currentRouteId = routeId;
  currentRoute = routesMeta[routeId];
  currentFrameIndex = 0;
  cachedPanorama = null;

  if (currentBlobUrl) { URL.revokeObjectURL(currentBlobUrl); currentBlobUrl = null; }

  els.routeSelect.value = routeId;

  try {
    const resp = await fetch(`data/gaze_${routeId}.json`);
    gazeData = await resp.json();
  } catch (e) {
    console.error(`Failed to load gaze data for ${routeId}:`, e);
    gazeData = {};
  }

  await loadFrame(0);
}

async function loadFrame(index, preserveView = false) {
  if (isLoading) return;
  isLoading = true;
  showLoading(true);

  currentFrameIndex = index;
  const frame = currentRoute.frames[index];

  try {
    const img = await loadPanoramaImage(frame);
    cachedPanorama = { frameId: frame.id, img };
    const imageUrl = await compositeFrame(img, frame.id);
    updatePannellum(imageUrl, frame, preserveView);
  } catch (e) {
    console.error('Failed to load frame:', e);
  }

  updateNavUI();
  updateStats();
  isLoading = false;
}

function loadPanoramaImage(frame) {
  return new Promise((resolve, reject) => {
    if (cachedPanorama && cachedPanorama.frameId === frame.id) {
      resolve(cachedPanorama.img);
      return;
    }
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`Failed to load: ${frame.source}/${frame.id}.jpg`));
    img.src = `${FRAMES_BASE}/${frame.source}/${frame.id}.jpg`;
  });
}

// ── Helpers ───────────────────────────────────────────

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function computeCentroid(frameGaze) {
  let sumU = 0, sumV = 0;
  const n = frameGaze.length / 2;
  for (let i = 0; i < frameGaze.length; i += 2) {
    sumU += frameGaze[i] / 1000;
    sumV += frameGaze[i + 1] / 1000;
  }
  return { u: sumU / n, v: sumV / n };
}

// ── Heatmap: density grid + blur ──────────────────────

function buildGroupGrid(frameId, pids) {
  const grid = new Float32Array(HM_GRID_W * HM_GRID_H);
  for (const pid of pids) {
    const frameGaze = gazeData[pid]?.[frameId];
    if (!frameGaze) continue;
    for (let i = 0; i < frameGaze.length; i += 2) {
      const u = frameGaze[i] / 1000;
      const v = frameGaze[i + 1] / 1000;
      const x = Math.floor(u * HM_GRID_W) % HM_GRID_W;
      const y = Math.max(0, Math.min(HM_GRID_H - 1, Math.floor(v * HM_GRID_H)));
      grid[y * HM_GRID_W + x] += 1;
    }
  }
  return grid;
}

function makeGaussianKernel(sigma, radius) {
  const size = 2 * radius + 1;
  const kernel = new Float32Array(size);
  let sum = 0;
  for (let i = 0; i < size; i++) {
    const x = i - radius;
    kernel[i] = Math.exp(-0.5 * (x * x) / (sigma * sigma));
    sum += kernel[i];
  }
  for (let i = 0; i < size; i++) kernel[i] /= sum;
  return kernel;
}

function gaussianBlur(grid, w, h, sigma) {
  const radius = Math.ceil(sigma * 3);
  const kernel = makeGaussianKernel(sigma, radius);

  const temp = new Float32Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let sum = 0;
      for (let k = -radius; k <= radius; k++) {
        sum += grid[y * w + (((x + k) % w) + w) % w] * kernel[k + radius];
      }
      temp[y * w + x] = sum;
    }
  }

  const result = new Float32Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let sum = 0;
      for (let k = -radius; k <= radius; k++) {
        sum += temp[Math.max(0, Math.min(h - 1, y + k)) * w + x] * kernel[k + radius];
      }
      result[y * w + x] = sum;
    }
  }
  return result;
}

function renderGroupHeatmap(grid) {
  let peak = 0;
  for (let i = 0; i < grid.length; i++) {
    if (grid[i] > peak) peak = grid[i];
  }
  if (peak === 0) return false;

  const imageData = hmCtx.createImageData(HM_GRID_W, HM_GRID_H);
  const pixels = imageData.data;

  for (let i = 0; i < grid.length; i++) {
    const t = grid[i] / peak;
    if (t < 0.01) continue;
    const idx = Math.min(255, Math.round(t * 255));
    const pi = i * 4;
    pixels[pi]     = HM_COLOR_LUT[idx * 3];
    pixels[pi + 1] = HM_COLOR_LUT[idx * 3 + 1];
    pixels[pi + 2] = HM_COLOR_LUT[idx * 3 + 2];
    pixels[pi + 3] = Math.round(HM_ALPHA_LUT[idx] * HM_GLOBAL_OPACITY * 255);
  }

  hmCtx.putImageData(imageData, 0, 0);
  return true;
}

// ── Compositing ───────────────────────────────────────

function compositeFrame(panoramaImg, frameId) {
  return new Promise((resolve) => {
    if (currentBlobUrl) { URL.revokeObjectURL(currentBlobUrl); currentBlobUrl = null; }

    const hasMarkers = selectedPids.size > 0;
    const hasHeatmaps = showHeatmapGood || showHeatmapPoor;

    if (!hasMarkers && !hasHeatmaps) {
      const frame = currentRoute.frames[currentFrameIndex];
      resolve(`${FRAMES_BASE}/${frame.source}/${frameId}.jpg`);
      return;
    }

    const w = panoramaImg.naturalWidth;
    const h = panoramaImg.naturalHeight;
    const outCanvas = document.createElement('canvas');
    outCanvas.width = w;
    outCanvas.height = h;
    const ctx = outCanvas.getContext('2d');

    ctx.drawImage(panoramaImg, 0, 0);

    // ── Layer 1: Group heatmaps ───────────────────────
    if (hasHeatmaps) {
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';

      // Slight darken so heatmap colors are visible
      ctx.fillStyle = 'rgba(0, 0, 0, 0.2)';
      ctx.fillRect(0, 0, w, h);

      if (showHeatmapGood) {
        const grid = buildGroupGrid(frameId, GOOD_PIDS);
        const blurred = gaussianBlur(grid, HM_GRID_W, HM_GRID_H, HM_SIGMA);
        if (renderGroupHeatmap(blurred)) {
          ctx.drawImage(hmCanvas, 0, 0, w, h);
        }
      }

      if (showHeatmapPoor) {
        const grid = buildGroupGrid(frameId, POOR_PIDS);
        const blurred = gaussianBlur(grid, HM_GRID_W, HM_GRID_H, HM_SIGMA);
        if (renderGroupHeatmap(blurred)) {
          ctx.drawImage(hmCanvas, 0, 0, w, h);
        }
      }
    }

    // ── Layer 2: Centroid markers ──────────────────────
    if (hasMarkers) {
      const markers = [];
      for (const pid of selectedPids) {
        const frameGaze = gazeData[pid]?.[frameId];
        if (!frameGaze || frameGaze.length < 2) continue;
        const centroid = computeCentroid(frameGaze);
        markers.push({ pid, x: centroid.u * w, y: centroid.v * h });
      }

      const scale = markerScale;
      const dotRadius = Math.round(h * 0.008 * scale);
      const haloRadius = Math.round(dotRadius * 2.5);
      const fontSize = Math.round(h * 0.014 * scale);
      const labelOffset = dotRadius + Math.round(h * 0.004);

      ctx.textBaseline = 'middle';
      ctx.font = `bold ${fontSize}px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`;

      for (const m of markers) {
        const color = PARTICIPANT_COLORS[m.pid];
        const [r, g, b] = hexToRgb(color);

        // Halo
        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.15)`;
        ctx.beginPath();
        ctx.arc(m.x, m.y, haloRadius, 0, Math.PI * 2);
        ctx.fill();

        // Dot
        ctx.fillStyle = color;
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = Math.max(2, Math.round(h * 0.001));
        ctx.beginPath();
        ctx.arc(m.x, m.y, dotRadius, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        // Label pill
        const tw = ctx.measureText(m.pid).width;
        const pillH = fontSize + Math.round(fontSize * 0.4);
        const pillW = tw + Math.round(fontSize * 0.6);
        const pillX = m.x + labelOffset;
        const pillY = m.y - pillH / 2;
        const pillR = Math.round(pillH * 0.3);

        ctx.fillStyle = 'rgba(0, 0, 0, 0.65)';
        roundRect(ctx, pillX, pillY, pillW, pillH, pillR);
        ctx.fill();

        ctx.fillStyle = color;
        roundRect(ctx, pillX, pillY, Math.round(fontSize * 0.2), pillH, pillR);
        ctx.fill();

        ctx.fillStyle = '#ffffff';
        ctx.fillText(m.pid, pillX + Math.round(fontSize * 0.3), m.y);
      }
    }

    outCanvas.toBlob((blob) => {
      currentBlobUrl = URL.createObjectURL(blob);
      resolve(currentBlobUrl);
    }, 'image/jpeg', 0.92);
  });
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

// ── Pannellum ─────────────────────────────────────────

function updatePannellum(imageUrl, frame, preserveView = false) {
  let yaw = frame.yaw || 0;
  let pitch = frame.pitch || 0;
  let hfov = 100;

  if (preserveView && viewer) {
    try { yaw = viewer.getYaw(); pitch = viewer.getPitch(); hfov = viewer.getHfov(); }
    catch (e) { /* viewer may be destroyed */ }
  }

  if (viewer) {
    try { viewer.destroy(); } catch (e) { /* ignore */ }
    viewer = null;
  }

  viewer = pannellum.viewer(els.panorama, {
    type: 'equirectangular',
    panorama: imageUrl,
    autoLoad: true,
    showControls: false,
    mouseZoom: true,
    keyboardZoom: false,
    hfov,
    minHfov: 50,
    maxHfov: 120,
    compass: false,
    showFullscreenCtrl: false,
    showZoomCtrl: false,
    friction: 0.15,
    yaw,
    pitch,
    horizonRoll: frame.roll || 0,
  });

  viewer.on('load', () => { showLoading(false); preloadAdjacent(); });
  setTimeout(() => showLoading(false), 2000);
}

function preloadAdjacent() {
  if (!currentRoute) return;
  const total = currentRoute.frames.length;
  for (const offset of [1, -1, 2, -2]) {
    const idx = currentFrameIndex + offset;
    if (idx >= 0 && idx < total) {
      const f = currentRoute.frames[idx];
      const img = new Image();
      img.src = `${FRAMES_BASE}/${f.source}/${f.id}.jpg`;
    }
  }
}

// ── Visual change handler ─────────────────────────────

async function onVisualChange() {
  if (!cachedPanorama || !currentRoute) return;
  const frame = currentRoute.frames[currentFrameIndex];
  const imageUrl = await compositeFrame(cachedPanorama.img, frame.id);
  updatePannellum(imageUrl, frame, true);
  updateStats();
}

// ── UI updates ────────────────────────────────────────

function showLoading(show) {
  els.loadingOverlay.classList.toggle('hidden', !show);
}

function updateNavUI() {
  if (!currentRoute) return;
  const total = currentRoute.frames.length;
  const frame = currentRoute.frames[currentFrameIndex];
  els.frameInput.value = currentFrameIndex + 1;
  els.frameInput.max = total;
  els.frameTotal.textContent = `/ ${total}`;
  els.frameId.textContent = frame.id;
  els.prevBtn.disabled = currentFrameIndex === 0;
  els.nextBtn.disabled = currentFrameIndex === total - 1;
}

function updateStats() {
  els.statSelected.textContent = selectedPids.size;
  if (!gazeData || selectedPids.size === 0) { els.statPoints.textContent = '0'; return; }
  const frame = currentRoute.frames[currentFrameIndex];
  let total = 0;
  for (const pid of selectedPids) {
    const fg = gazeData[pid]?.[frame.id];
    if (fg) total += fg.length / 2;
  }
  els.statPoints.textContent = total.toLocaleString();
}

// ── Start ─────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', init);
