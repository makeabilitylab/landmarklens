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

// ── State ─────────────────────────────────────────────

let routesMeta = null;
let currentRouteId = null;
let currentRoute = null;
let currentFrameIndex = 0;
let gazeData = null;
let selectedPids = new Set();
let viewer = null;
let currentBlobUrl = null;
let cachedPanorama = null;
let markerScale = 0.70;
let isLoading = false;

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
      onMarkersChange();
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
    onMarkersChange();
  });
  els.togglePoor.addEventListener('change', () => {
    setGroupCheckboxes(els.poorPids, POOR_PIDS, els.togglePoor.checked);
    onMarkersChange();
  });

  let scaleTimer = null;
  els.opacitySlider.addEventListener('input', () => {
    markerScale = parseInt(els.opacitySlider.value, 10) / 100;
    els.opacityValue.textContent = `${els.opacitySlider.value}%`;
    if (selectedPids.size > 0) {
      clearTimeout(scaleTimer);
      scaleTimer = setTimeout(() => onMarkersChange(), 150);
    }
  });

  document.addEventListener('keydown', onKeyDown);
}

function onKeyDown(e) {
  if (e.target.tagName === 'INPUT') return;

  if (e.key === 'ArrowLeft' || e.key === 'a') {
    e.preventDefault();
    navigateFrame(-1);
  } else if (e.key === 'ArrowRight' || e.key === 'd') {
    e.preventDefault();
    navigateFrame(1);
  }
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

  if (currentBlobUrl) {
    URL.revokeObjectURL(currentBlobUrl);
    currentBlobUrl = null;
  }

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

// ── Centroid computation ──────────────────────────────

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

// ── Compositing: draw centroid markers ────────────────

function compositeFrame(panoramaImg, frameId) {
  return new Promise((resolve) => {
    if (currentBlobUrl) {
      URL.revokeObjectURL(currentBlobUrl);
      currentBlobUrl = null;
    }

    if (selectedPids.size === 0) {
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

    // Collect centroids for all selected participants
    const markers = [];
    for (const pid of selectedPids) {
      const frameGaze = gazeData[pid]?.[frameId];
      if (!frameGaze || frameGaze.length < 2) continue;
      const centroid = computeCentroid(frameGaze);
      markers.push({ pid, x: centroid.u * w, y: centroid.v * h, n: frameGaze.length / 2 });
    }

    // Sizing proportional to image height
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
      const label = m.pid;

      // Soft halo
      ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.15)`;
      ctx.beginPath();
      ctx.arc(m.x, m.y, haloRadius, 0, Math.PI * 2);
      ctx.fill();

      // Filled dot with white border
      ctx.fillStyle = color;
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = Math.max(2, Math.round(h * 0.001));
      ctx.beginPath();
      ctx.arc(m.x, m.y, dotRadius, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      // Label background pill
      const textMetrics = ctx.measureText(label);
      const tw = textMetrics.width;
      const pillH = fontSize + Math.round(fontSize * 0.4);
      const pillW = tw + Math.round(fontSize * 0.6);
      const pillX = m.x + labelOffset;
      const pillY = m.y - pillH / 2;
      const pillR = Math.round(pillH * 0.3);

      ctx.fillStyle = `rgba(0, 0, 0, 0.65)`;
      roundRect(ctx, pillX, pillY, pillW, pillH, pillR);
      ctx.fill();

      // Colored left edge on pill
      ctx.fillStyle = color;
      roundRect(ctx, pillX, pillY, Math.round(fontSize * 0.2), pillH, pillR);
      ctx.fill();

      // Label text
      ctx.fillStyle = '#ffffff';
      ctx.fillText(label, pillX + Math.round(fontSize * 0.3), m.y);
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
    try {
      yaw = viewer.getYaw();
      pitch = viewer.getPitch();
      hfov = viewer.getHfov();
    } catch (e) { /* viewer may be destroyed */ }
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

  viewer.on('load', () => {
    showLoading(false);
    preloadAdjacent();
  });
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

// ── Marker toggle handler ─────────────────────────────

async function onMarkersChange() {
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

  if (!gazeData || selectedPids.size === 0) {
    els.statPoints.textContent = '0';
    return;
  }

  const frame = currentRoute.frames[currentFrameIndex];
  let total = 0;
  for (const pid of selectedPids) {
    const frameGaze = gazeData[pid]?.[frame.id];
    if (frameGaze) total += frameGaze.length / 2;
  }
  els.statPoints.textContent = total.toLocaleString();
}

// ── Start ─────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', init);
