// ========================================
// Tokyo 360° Street View - Viewer Page
// ========================================

mapboxgl.accessToken = 'pk.eyJ1IjoiY3Jlc2NlbmRvY2h1IiwiYSI6ImNpdGR5MWZ5aDAycjIyc3A5ZHoxZzRwMGsifQ.nEaSxm520v7TpKAy2GG_kA';

const NEIGHBORHOOD_META = {
  ginza:      { name: 'Ginza',      nameJa: '銀座'   },
  kinshicho:  { name: 'Kinshichō',  nameJa: '錦糸町' },
  koenji:     { name: 'Kōenji',     nameJa: '高円寺' },
  nezu:       { name: 'Nezu',       nameJa: '根津'   },
};

// ========================================
// State
// ========================================
let neighborhoodId = null;
let neighborhoodMeta = null;
let frames = [];
let currentIndex = 0;
let viewer = null;
let map = null;
let activeMarkerReady = false;
let frameColors = {};
let frameBearings = {};
let lineVisible = true;
let splitRatio = 0.5;
let isDragging = false;
let debugExpanded = true;

// Edit mode state
let editMode = false;
let headingAdjustments = {};
const STORAGE_KEY = 'tokyo-streetview-heading-adjustments';

// Smooth transition state
let viewers = { a: null, b: null };
let activeViewer = 'a';
let isTransitioning = false;
let preloadCache = new Map();
const PRELOAD_COUNT = 2;
const CROSSFADE_DURATION = 400;

const FRAMES_BASE = 'frames/';

// ========================================
// DOM Elements
// ========================================
const elements = {
  loading: document.getElementById('loading'),
  neighborhoodName: document.getElementById('neighborhood-name'),
  neighborhoodNameJa: document.getElementById('neighborhood-name-ja'),
  photoInfo: document.getElementById('photo-info'),
  panoramaA: document.getElementById('panorama-a'),
  panoramaB: document.getElementById('panorama-b'),
  transitionOverlay: document.getElementById('transition-overlay'),
  minimap: document.getElementById('minimap'),
  minimapContainer: document.getElementById('minimap-container'),
  minimapToggle: document.getElementById('minimap-toggle'),
  lineToggle: document.getElementById('line-toggle'),
  prevBtn: document.getElementById('prev-btn'),
  nextBtn: document.getElementById('next-btn'),
  navCounterInput: document.getElementById('nav-counter-input'),
  navCounterTotal: document.getElementById('nav-counter-total'),
  navControls: document.querySelector('.nav-controls'),
  debugPanel: document.getElementById('debug-panel'),
  debugContent: document.getElementById('debug-content'),
  debugToggle: document.getElementById('debug-toggle'),
  debugSource: document.getElementById('debug-source'),
  debugFile: document.getElementById('debug-file'),
  debugCoords: document.getElementById('debug-coords'),
  debugAlt: document.getElementById('debug-alt'),
  debugColor: document.getElementById('debug-color'),
  splitDivider: document.getElementById('split-divider'),
  editPanel: null,
  editYaw: null,
  editPitch: null,
  editRoll: null,
  editAutoBearing: null,
};

// ========================================
// Initialize
// ========================================
async function init() {
  try {
    const urlParams = new URLSearchParams(window.location.search);
    neighborhoodId = urlParams.get('neighborhood');

    if (!neighborhoodId) {
      window.location.href = 'index.html';
      return;
    }

    neighborhoodMeta = NEIGHBORHOOD_META[neighborhoodId] || { name: neighborhoodId, nameJa: '' };

    loadHeadingAdjustments();

    const resp = await fetch('data.json');
    const allData = await resp.json();

    frames = allData.frames.filter(f => f.neighborhood === neighborhoodId);

    if (frames.length === 0) {
      console.error('No frames found for neighborhood:', neighborhoodId);
      window.location.href = 'index.html';
      return;
    }

    calculateFrameBearings();

    document.title = `${neighborhoodMeta.name} - Tokyo Walks`;
    elements.neighborhoodName.textContent = neighborhoodMeta.name;
    elements.neighborhoodNameJa.textContent = neighborhoodMeta.nameJa;

    initPanorama();
    initMinimap();
    initControls();
    initEditMode();

    loadPhoto(0);
  } catch (error) {
    console.error('Failed to initialize viewer:', error);
  }
}

// ========================================
// Heading Adjustments
// ========================================
function loadHeadingAdjustments() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) headingAdjustments = JSON.parse(saved);
  } catch (e) {
    headingAdjustments = {};
  }
}

function saveHeadingAdjustments() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(headingAdjustments));
  } catch (e) {
    console.error('Failed to save heading adjustments:', e);
  }
}

function getFrameHeading(frame) {
  const localSaved = headingAdjustments[frame.filename];
  if (localSaved) {
    return { yaw: localSaved.yaw, pitch: localSaved.pitch, roll: localSaved.roll || 0, source: 'local' };
  }
  return { yaw: 0, pitch: 0, roll: 0, source: 'default' };
}

// ========================================
// Calculate Bearings
// ========================================
function calculateFrameBearings() {
  const bySource = {};
  frames.forEach((frame, index) => {
    if (!bySource[frame.source]) bySource[frame.source] = [];
    bySource[frame.source].push({ frame, index });
  });

  Object.values(bySource).forEach(sourceFrames => {
    sourceFrames.forEach((item, i) => {
      let bearing = 0;
      if (i < sourceFrames.length - 1) {
        const next = sourceFrames[i + 1].frame;
        bearing = calculateBearing(item.frame.lat, item.frame.lon, next.lat, next.lon);
      } else if (i > 0) {
        const prev = sourceFrames[i - 1].frame;
        bearing = calculateBearing(prev.lat, prev.lon, item.frame.lat, item.frame.lon);
      }
      frameBearings[item.index] = bearing;
    });
  });
}

// ========================================
// Panorama Viewers (Dual for transitions)
// ========================================
function initPanorama() {
  viewers.a = createViewer('panorama-a');
  viewers.b = createViewer('panorama-b');
  viewer = viewers.a;
}

function createViewer(containerId) {
  return pannellum.viewer(containerId, {
    type: 'equirectangular',
    panorama: '',
    autoLoad: false,
    showControls: false,
    mouseZoom: true,
    keyboardZoom: true,
    hfov: 100,
    minHfov: 50,
    maxHfov: 120,
    compass: false,
    showFullscreenCtrl: false,
    showZoomCtrl: false,
    friction: 0.15,
    yaw: 0,
    pitch: 0,
  });
}

// ========================================
// Preloading
// ========================================
function preloadAdjacentPhotos(idx) {
  const toPreload = [];
  for (let i = 1; i <= PRELOAD_COUNT; i++) {
    if (idx + i < frames.length) toPreload.push(idx + i);
    if (idx - i >= 0) toPreload.push(idx - i);
  }

  toPreload.forEach(j => {
    if (!preloadCache.has(j)) {
      const img = new Image();
      const path = FRAMES_BASE + frames[j].filename;
      preloadCache.set(j, { img, loaded: false, path });
      img.onload = () => {
        const cached = preloadCache.get(j);
        if (cached) cached.loaded = true;
      };
      img.src = path;
    }
  });

  const keepRange = PRELOAD_COUNT + 2;
  for (const [k] of preloadCache) {
    if (Math.abs(k - idx) > keepRange) preloadCache.delete(k);
  }
}

function isPhotoPreloaded(idx) {
  const cached = preloadCache.get(idx);
  return cached && cached.loaded;
}

// ========================================
// Minimap (Mapbox)
// ========================================
function initMinimap() {
  if (map) { map.remove(); map = null; }

  const lats = frames.map(f => f.lat);
  const lons = frames.map(f => f.lon);
  const centerLat = (Math.min(...lats) + Math.max(...lats)) / 2;
  const centerLon = (Math.min(...lons) + Math.max(...lons)) / 2;

  map = new mapboxgl.Map({
    container: 'minimap',
    style: 'mapbox://styles/mapbox/dark-v11',
    center: [centerLon, centerLat],
    zoom: 16,
    attributionControl: false,
  });

  map.once('load', () => {
    const routesBySource = {};
    frames.forEach((frame, index) => {
      if (!frame.lat || !frame.lon) return;
      if (!routesBySource[frame.source]) routesBySource[frame.source] = [];
      routesBySource[frame.source].push({ frame, index });
    });

    const sourceNames = Object.keys(routesBySource);
    const routeColors = generateRouteColors(sourceNames.length);

    const allFeatures = [];

    sourceNames.forEach((source, routeIndex) => {
      const routeFrames = routesBySource[source];
      const color = routeColors[routeIndex];

      const lineCoords = routeFrames.map(r => [r.frame.lon, r.frame.lat]);
      if (lineCoords.length > 1) {
        allFeatures.push({
          type: 'Feature',
          properties: { color, routeIndex },
          geometry: { type: 'LineString', coordinates: lineCoords },
        });
      }

      routeFrames.forEach((r, i) => {
        frameColors[r.index] = color;

        let bearing = 0;
        if (i < routeFrames.length - 1) {
          const next = routeFrames[i + 1].frame;
          bearing = calculateBearing(r.frame.lat, r.frame.lon, next.lat, next.lon);
        } else if (i > 0) {
          const prev = routeFrames[i - 1].frame;
          bearing = calculateBearing(prev.lat, prev.lon, r.frame.lat, r.frame.lon);
        }

        allFeatures.push({
          type: 'Feature',
          properties: { index: r.index, color, routeIndex, bearing },
          geometry: { type: 'Point', coordinates: [r.frame.lon, r.frame.lat] },
        });
      });
    });

    map.addSource('photos', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: allFeatures },
    });

    map.addLayer({
      id: 'photos-path',
      type: 'line',
      source: 'photos',
      filter: ['==', '$type', 'LineString'],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': ['get', 'color'], 'line-width': 2.5, 'line-opacity': 0.8 },
    });

    updateLineVisibility();

    // Plain circle dots for all photo positions
    map.addLayer({
      id: 'photos-points',
      type: 'circle',
      source: 'photos',
      filter: ['==', '$type', 'Point'],
      paint: {
        'circle-radius': 4,
        'circle-color': ['get', 'color'],
        'circle-opacity': 0.85,
      },
    });

    map.on('click', 'photos-points', (e) => {
      if (e.features && e.features.length > 0) {
        loadPhoto(e.features[0].properties.index);
      }
    });

    map.on('mouseenter', 'photos-points', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'photos-points', () => { map.getCanvas().style.cursor = ''; });

    // Active marker source
    if (!map.hasImage('cone-icon')) {
      map.addImage('cone-icon', createConeImageData(), { sdf: true });
    }

    map.addSource('active-photo', {
      type: 'geojson',
      data: { type: 'Feature', properties: { bearing: 0, color: '#ffffff' }, geometry: { type: 'Point', coordinates: [frames[0].lon, frames[0].lat] } },
    });

    // Cone: directional wedge showing look direction
    map.addLayer({
      id: 'active-cone-glow',
      type: 'symbol',
      source: 'active-photo',
      layout: {
        'icon-image': 'cone-icon', 'icon-size': 1.6,
        'icon-rotate': ['get', 'bearing'], 'icon-rotation-alignment': 'map',
        'icon-allow-overlap': true, 'icon-ignore-placement': true,
      },
      paint: { 'icon-color': ['get', 'color'], 'icon-opacity': 0.25 },
    });

    map.addLayer({
      id: 'active-cone',
      type: 'symbol',
      source: 'active-photo',
      layout: {
        'icon-image': 'cone-icon', 'icon-size': 1.1,
        'icon-rotate': ['get', 'bearing'], 'icon-rotation-alignment': 'map',
        'icon-allow-overlap': true, 'icon-ignore-placement': true,
      },
      paint: { 'icon-color': ['get', 'color'], 'icon-opacity': 0.7 },
    });

    // Dot: bigger circle with white border on top
    map.addLayer({
      id: 'active-dot',
      type: 'circle',
      source: 'active-photo',
      paint: {
        'circle-radius': 7,
        'circle-color': ['get', 'color'],
        'circle-opacity': 1,
        'circle-stroke-width': 2.5,
        'circle-stroke-color': '#ffffff',
      },
    });

    const coordinates = frames.map(f => [f.lon, f.lat]);
    if (coordinates.length > 0) {
      const bounds = coordinates.reduce(
        (b, coord) => b.extend(coord),
        new mapboxgl.LngLatBounds(coordinates[0], coordinates[0])
      );
      map.fitBounds(bounds, { padding: 40, duration: 0 });
    }
  });
}

function updateActiveMarker(index) {
  if (!map || !map.getSource('active-photo') || !frames[index]) return;

  const frame = frames[index];
  const color = frameColors[index] || '#ffffff';
  const bearing = frameBearings[index] || 0;

  map.getSource('active-photo').setData({
    type: 'Feature',
    properties: { bearing, color },
    geometry: { type: 'Point', coordinates: [frame.lon, frame.lat] },
  });
}

// ========================================
// Load Photo (with smooth transitions)
// ========================================
function loadPhoto(index, forceImmediate = false) {
  if (index < 0 || index >= frames.length) return;
  if (isTransitioning && !forceImmediate) return;

  const previousIndex = currentIndex;
  const isForward = index > previousIndex;
  const isAdjacent = Math.abs(index - previousIndex) === 1;
  const shouldAnimate = isAdjacent && !forceImmediate && previousIndex !== index;

  if (shouldAnimate) {
    transitionToPhoto(index, isForward);
  } else {
    loadPhotoImmediate(index);
  }
}

function loadPhotoImmediate(index) {
  const frame = frames[index];
  currentIndex = index;

  if (!isPhotoPreloaded(index)) {
    elements.loading.classList.remove('hidden');
  }

  const heading = getFrameHeading(frame);
  const imagePath = FRAMES_BASE + frame.filename;

  const activeContainer = activeViewer === 'a' ? elements.panoramaA : elements.panoramaB;
  const currentV = viewers[activeViewer];

  currentV.destroy();
  viewers[activeViewer] = pannellum.viewer(activeContainer.id, {
    type: 'equirectangular',
    panorama: imagePath,
    autoLoad: true,
    showControls: false,
    mouseZoom: true,
    keyboardZoom: true,
    hfov: 100,
    minHfov: 50,
    maxHfov: 120,
    compass: false,
    showFullscreenCtrl: false,
    showZoomCtrl: false,
    friction: 0.15,
    yaw: heading.yaw,
    pitch: heading.pitch,
    horizonRoll: heading.roll,
  });

  viewer = viewers[activeViewer];

  viewer.on('load', () => {
    elements.loading.classList.add('hidden');
    preloadAdjacentPhotos(index);
  });

  updatePhotoUI(index, frame);
}

function transitionToPhoto(index, isForward) {
  isTransitioning = true;

  const frame = frames[index];
  currentIndex = index;

  const heading = getFrameHeading(frame);
  const imagePath = FRAMES_BASE + frame.filename;

  const currentViewerKey = activeViewer;
  const nextViewerKey = activeViewer === 'a' ? 'b' : 'a';
  const currentContainer = currentViewerKey === 'a' ? elements.panoramaA : elements.panoramaB;
  const nextContainer = nextViewerKey === 'a' ? elements.panoramaA : elements.panoramaB;

  if (isForward) {
    elements.transitionOverlay.classList.add('active');
  }

  prepareNextPanorama(nextViewerKey, nextContainer, imagePath, heading, () => {
    performCrossfade(currentContainer, nextContainer, nextViewerKey, () => {
      elements.transitionOverlay.classList.remove('active');
      isTransitioning = false;
      preloadAdjacentPhotos(index);
    });
  });

  updatePhotoUI(index, frame);
}

function prepareNextPanorama(viewerKey, container, imagePath, heading, callback) {
  let callbackFired = false;
  const safeCallback = () => {
    if (!callbackFired) { callbackFired = true; callback(); }
  };

  const existing = viewers[viewerKey];
  if (existing) existing.destroy();

  viewers[viewerKey] = pannellum.viewer(container.id, {
    type: 'equirectangular',
    panorama: imagePath,
    autoLoad: true,
    showControls: false,
    mouseZoom: true,
    keyboardZoom: true,
    hfov: 100,
    minHfov: 50,
    maxHfov: 120,
    compass: false,
    showFullscreenCtrl: false,
    showZoomCtrl: false,
    friction: 0.15,
    yaw: heading.yaw,
    pitch: heading.pitch,
    horizonRoll: heading.roll,
  });

  viewers[viewerKey].on('load', safeCallback);
  setTimeout(safeCallback, 2000);
}

function performCrossfade(outgoing, incoming, incomingKey, callback) {
  outgoing.classList.add('panorama-outgoing');
  outgoing.classList.remove('panorama-active');
  incoming.classList.add('panorama-active');

  requestAnimationFrame(() => {
    outgoing.classList.add('panorama-zoom-in');
  });

  setTimeout(() => {
    outgoing.classList.remove('panorama-outgoing', 'panorama-zoom-in');
    activeViewer = incomingKey;
    viewer = viewers[activeViewer];
    callback();
  }, CROSSFADE_DURATION);
}

// ========================================
// Update UI
// ========================================
function updatePhotoUI(index, frame) {
  elements.photoInfo.textContent = `${frame.source}`;

  elements.prevBtn.disabled = index === 0;
  elements.nextBtn.disabled = index === frames.length - 1;

  if (elements.navCounterInput) {
    elements.navCounterInput.value = index + 1;
    elements.navCounterInput.max = frames.length;
    elements.navCounterInput.min = 1;
  }
  if (elements.navCounterTotal) {
    elements.navCounterTotal.textContent = `/ ${frames.length}`;
  }

  if (elements.debugSource) {
    elements.debugSource.textContent = frame.source || 'N/A';
    const parts = frame.filename.split('/');
    elements.debugFile.textContent = parts[parts.length - 1] || 'N/A';
    elements.debugCoords.textContent = frame.lat && frame.lon
      ? `${frame.lat.toFixed(6)}, ${frame.lon.toFixed(6)}`
      : 'No coordinates';
    elements.debugAlt.textContent = frame.alt != null ? `${frame.alt.toFixed(1)} m` : 'N/A';

    const color = frameColors[index] || '#999';
    elements.debugColor.innerHTML = `<span class="debug-color-swatch" style="background:${color}"></span>${color}`;
  }

  updateMapPosition(frame);
  updateActiveMarker(index);

  if (editMode) updateEditPanel();
}

function updateMapPosition(frame) {
  if (!map) return;
  map.flyTo({ center: [frame.lon, frame.lat], zoom: 17, duration: 500 });
}

function updateLineVisibility() {
  if (!map || !map.getLayer('photos-path')) return;
  map.setLayoutProperty('photos-path', 'visibility', lineVisible ? 'visible' : 'none');

  if (elements.lineToggle) {
    elements.lineToggle.classList.toggle('is-off', !lineVisible);
    elements.lineToggle.title = lineVisible ? 'Hide route lines' : 'Show route lines';
    elements.lineToggle.setAttribute('aria-pressed', String(lineVisible));
  }
}

// ========================================
// Controls
// ========================================
function initControls() {
  elements.prevBtn.addEventListener('click', () => loadPhoto(currentIndex - 1));
  elements.nextBtn.addEventListener('click', () => loadPhoto(currentIndex + 1));

  if (elements.navCounterInput) {
    const jumpToPhoto = () => {
      const num = parseInt(elements.navCounterInput.value, 10);
      if (!isNaN(num) && num >= 1 && num <= frames.length) {
        loadPhoto(num - 1);
      } else {
        elements.navCounterInput.value = currentIndex + 1;
      }
    };
    elements.navCounterInput.addEventListener('change', jumpToPhoto);
    elements.navCounterInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); jumpToPhoto(); }
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;

    if (e.key === 'ArrowLeft' || e.key === 'a') {
      e.preventDefault();
      loadPhoto(currentIndex - 1);
    } else if (e.key === 'ArrowRight' || e.key === 'd') {
      e.preventDefault();
      loadPhoto(currentIndex + 1);
    } else if (e.key === 'e' || e.key === 'E') {
      toggleEditMode();
    } else if (e.key === 's' && editMode && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      saveCurrentHeading();
    }
  });

  elements.minimapToggle.addEventListener('click', () => {
    const container = document.querySelector('.viewer-container');
    container.classList.toggle('split-screen');

    if (container.classList.contains('split-screen')) {
      elements.splitDivider.style.display = 'block';
      splitRatio = 0.5;
      updateSplitLayout();
    } else {
      elements.splitDivider.style.display = 'none';
      if (elements.navControls) elements.navControls.style.bottom = '';
      if (elements.debugPanel) elements.debugPanel.style.bottom = '';
    }

    setTimeout(() => {
      map.resize();
      if (viewers.a) viewers.a.resize();
      if (viewers.b) viewers.b.resize();
    }, 300);
  });

  if (elements.lineToggle) {
    elements.lineToggle.addEventListener('click', () => {
      lineVisible = !lineVisible;
      updateLineVisibility();
    });
  }

  if (elements.debugToggle) {
    elements.debugToggle.addEventListener('click', () => {
      debugExpanded = !debugExpanded;
      elements.debugPanel.classList.toggle('collapsed', !debugExpanded);
      elements.debugContent.style.display = debugExpanded ? 'block' : 'none';
      const icon = elements.debugToggle.querySelector('svg');
      icon.style.transform = debugExpanded ? 'rotate(0deg)' : 'rotate(180deg)';
    });
  }

  initSplitDivider();
}

// ========================================
// Split Divider
// ========================================
function initSplitDivider() {
  const divider = elements.splitDivider;
  if (!divider) return;

  divider.addEventListener('mousedown', startDrag);
  document.addEventListener('mousemove', drag);
  document.addEventListener('mouseup', stopDrag);

  divider.addEventListener('touchstart', startDrag, { passive: false });
  document.addEventListener('touchmove', drag, { passive: false });
  document.addEventListener('touchend', stopDrag);
}

function startDrag(e) {
  const container = document.querySelector('.viewer-container');
  if (!container.classList.contains('split-screen')) return;

  isDragging = true;
  e.preventDefault();
  document.body.style.cursor = 'row-resize';
  document.body.style.userSelect = 'none';

  const panoramaContainer = document.querySelector('.panorama-container');
  const minimapContainer = document.querySelector('.minimap-container');
  [panoramaContainer, minimapContainer, elements.splitDivider, elements.navControls, elements.debugPanel]
    .filter(Boolean).forEach(el => el.style.transition = 'none');
}

function drag(e) {
  if (!isDragging) return;
  e.preventDefault();

  const container = document.querySelector('.viewer-container');
  if (!container.classList.contains('split-screen')) return;

  const clientY = e.touches ? e.touches[0].clientY : e.clientY;
  const rect = container.getBoundingClientRect();
  splitRatio = Math.max(0.1, Math.min(0.9, (clientY - rect.top) / rect.height));
  updateSplitLayout();
}

function stopDrag() {
  if (!isDragging) return;
  isDragging = false;
  document.body.style.cursor = '';
  document.body.style.userSelect = '';

  const panoramaContainer = document.querySelector('.panorama-container');
  const minimapContainer = document.querySelector('.minimap-container');
  [panoramaContainer, minimapContainer, elements.splitDivider, elements.navControls, elements.debugPanel]
    .filter(Boolean).forEach(el => el.style.transition = '');
}

function updateSplitLayout() {
  const container = document.querySelector('.viewer-container');
  if (!container.classList.contains('split-screen')) return;

  const panoH = splitRatio * 100;
  const mapH = (1 - splitRatio) * 100;

  document.querySelector('.panorama-container').style.height = `${panoH}%`;
  const mc = document.querySelector('.minimap-container');
  mc.style.top = `${panoH}%`;
  mc.style.height = `${mapH}%`;
  elements.splitDivider.style.top = `${panoH}%`;

  const navBottom = mapH;
  elements.navControls.style.bottom = `calc(${navBottom}% + 2rem)`;
  if (elements.debugPanel) {
    elements.debugPanel.style.bottom = `calc(${navBottom}% + 6rem)`;
  }

  setTimeout(() => {
    if (map) map.resize();
    if (viewers.a) viewers.a.resize();
    if (viewers.b) viewers.b.resize();
  }, 0);
}

// ========================================
// Edit Mode
// ========================================
function initEditMode() {
  const editPanelHTML = `
    <div class="edit-panel" id="edit-panel">
      <div class="edit-header">
        <div class="edit-title">Adjust Heading</div>
        <div class="edit-hint">Press E to toggle</div>
      </div>
      <div class="edit-content">
        <div class="edit-row">
          <label>GPS Bearing:</label>
          <span id="edit-auto-bearing">0°</span>
          <span class="edit-hint-inline">(reference)</span>
        </div>
        <div class="edit-row">
          <label>Yaw (horizontal):</label>
          <input type="number" id="edit-yaw" step="0.1" min="-180" max="360">
          <span class="edit-unit">°</span>
        </div>
        <div class="edit-row">
          <label>Pitch (vertical):</label>
          <input type="number" id="edit-pitch" step="0.1" min="-90" max="90">
          <span class="edit-unit">°</span>
        </div>
        <div class="edit-row">
          <label>Roll (level):</label>
          <input type="number" id="edit-roll" step="0.5" min="-45" max="45">
          <span class="edit-unit">°</span>
        </div>
        <div class="edit-row edit-grid-toggle">
          <label for="edit-grid-checkbox">Show grid:</label>
          <input type="checkbox" id="edit-grid-checkbox">
          <span class="edit-hint-inline">Helps with alignment</span>
        </div>
        <div class="edit-row edit-status" id="edit-status">
          <span class="status-dot"></span>
          <span class="status-text">Using default (center)</span>
        </div>
        <div class="edit-buttons">
          <button class="edit-btn edit-btn-primary" id="edit-save-btn">Save (Cmd+S)</button>
          <button class="edit-btn edit-btn-secondary" id="edit-use-current-btn">Use Current View</button>
          <button class="edit-btn edit-btn-secondary" id="edit-reset-btn">Reset to Default</button>
        </div>
        <div class="edit-export">
          <button class="edit-btn edit-btn-export" id="edit-export-btn">Export Local Edits</button>
          <button class="edit-btn edit-btn-clear" id="edit-clear-btn">Clear Local Edits</button>
        </div>
        <div class="edit-export-count" id="edit-export-count"></div>
      </div>
    </div>
  `;

  document.querySelector('.viewer-container').insertAdjacentHTML('beforeend', editPanelHTML);

  elements.editPanel = document.getElementById('edit-panel');
  elements.editYaw = document.getElementById('edit-yaw');
  elements.editPitch = document.getElementById('edit-pitch');
  elements.editRoll = document.getElementById('edit-roll');
  elements.editAutoBearing = document.getElementById('edit-auto-bearing');
  elements.editSaveBtn = document.getElementById('edit-save-btn');
  elements.editResetBtn = document.getElementById('edit-reset-btn');
  elements.editUseCurrentBtn = document.getElementById('edit-use-current-btn');
  elements.editExportBtn = document.getElementById('edit-export-btn');
  elements.editClearBtn = document.getElementById('edit-clear-btn');
  elements.editStatus = document.getElementById('edit-status');
  elements.editExportCount = document.getElementById('edit-export-count');

  const gridOverlay = document.createElement('div');
  gridOverlay.className = 'grid-overlay';
  gridOverlay.id = 'grid-overlay';
  gridOverlay.innerHTML = `
    <div class="grid-pattern"></div>
    <div class="grid-center-h"></div>
    <div class="grid-center-v"></div>
    <div class="grid-crosshair"></div>
  `;
  document.querySelector('.panorama-container').appendChild(gridOverlay);
  elements.gridOverlay = gridOverlay;
  elements.editGridCheckbox = document.getElementById('edit-grid-checkbox');

  elements.editSaveBtn.addEventListener('click', saveCurrentHeading);
  elements.editResetBtn.addEventListener('click', resetCurrentHeading);
  elements.editUseCurrentBtn.addEventListener('click', useCurrentView);
  elements.editExportBtn.addEventListener('click', exportAdjustments);
  elements.editClearBtn.addEventListener('click', clearLocalEdits);
  elements.editGridCheckbox.addEventListener('change', toggleGrid);

  elements.editYaw.addEventListener('input', applyEditedHeading);
  elements.editPitch.addEventListener('input', applyEditedHeading);
  elements.editRoll.addEventListener('input', applyEditedRoll);

  updateExportCount();
}

function toggleEditMode() {
  editMode = !editMode;
  elements.editPanel.classList.toggle('visible', editMode);

  if (editMode) {
    updateEditPanel();
  } else {
    if (elements.gridOverlay) elements.gridOverlay.classList.remove('visible');
    if (elements.editGridCheckbox) elements.editGridCheckbox.checked = false;
  }
}

function toggleGrid() {
  elements.gridOverlay.classList.toggle('visible', elements.editGridCheckbox.checked);
}

function updateEditPanel() {
  if (!elements.editPanel) return;

  const frame = frames[currentIndex];
  const autoBearing = frameBearings[currentIndex] || 0;
  const heading = getFrameHeading(frame);

  elements.editAutoBearing.textContent = `${autoBearing.toFixed(1)}°`;
  elements.editYaw.value = heading.yaw.toFixed(1);
  elements.editPitch.value = heading.pitch.toFixed(1);
  elements.editRoll.value = heading.roll.toFixed(1);

  const statusText = elements.editStatus.querySelector('.status-text');
  if (heading.source === 'local') {
    elements.editStatus.classList.add('has-custom');
    statusText.textContent = 'Local edit (not exported)';
  } else {
    elements.editStatus.classList.remove('has-custom');
    statusText.textContent = 'Using default (center)';
  }

  updateExportCount();
}

function applyEditedHeading() {
  const yaw = parseFloat(elements.editYaw.value) || 0;
  const pitch = parseFloat(elements.editPitch.value) || 0;
  viewer.setYaw(yaw);
  viewer.setPitch(pitch);
}

function applyEditedRoll() {
  const frame = frames[currentIndex];
  const imagePath = FRAMES_BASE + frame.filename;
  const yaw = parseFloat(elements.editYaw.value) || 0;
  const pitch = parseFloat(elements.editPitch.value) || 0;
  const roll = parseFloat(elements.editRoll.value) || 0;

  const activeContainer = activeViewer === 'a' ? elements.panoramaA : elements.panoramaB;
  viewer.destroy();
  viewers[activeViewer] = pannellum.viewer(activeContainer.id, {
    type: 'equirectangular',
    panorama: imagePath,
    autoLoad: true,
    showControls: false,
    mouseZoom: true,
    keyboardZoom: true,
    hfov: 100,
    minHfov: 50,
    maxHfov: 120,
    compass: false,
    showFullscreenCtrl: false,
    showZoomCtrl: false,
    friction: 0.15,
    yaw: yaw,
    pitch: pitch,
    horizonRoll: roll,
  });
  viewer = viewers[activeViewer];
}

function useCurrentView() {
  elements.editYaw.value = viewer.getYaw().toFixed(1);
  elements.editPitch.value = viewer.getPitch().toFixed(1);
}

function saveCurrentHeading() {
  const frame = frames[currentIndex];
  const yaw = parseFloat(elements.editYaw.value) || 0;
  const pitch = parseFloat(elements.editPitch.value) || 0;
  const roll = parseFloat(elements.editRoll.value) || 0;

  headingAdjustments[frame.filename] = { yaw, pitch, roll };
  saveHeadingAdjustments();
  updateEditPanel();
  showEditFeedback('Saved!');
}

function resetCurrentHeading() {
  const frame = frames[currentIndex];
  delete headingAdjustments[frame.filename];
  saveHeadingAdjustments();
  loadPhoto(currentIndex);
  updateEditPanel();
  showEditFeedback('Reset to default');
}

function showEditFeedback(message) {
  const statusText = elements.editStatus.querySelector('.status-text');
  statusText.textContent = message;
  statusText.classList.add('feedback');
  setTimeout(() => {
    statusText.classList.remove('feedback');
    updateEditPanel();
  }, 1500);
}

function updateExportCount() {
  const count = Object.keys(headingAdjustments).length;
  if (elements.editExportCount) {
    elements.editExportCount.textContent = count > 0 ? `(${count} local edits)` : '(no local edits)';
  }
}

function exportAdjustments() {
  const count = Object.keys(headingAdjustments).length;
  if (count === 0) { alert('No heading adjustments to export.'); return; }

  const exportData = {
    exported: new Date().toISOString(),
    count,
    adjustments: headingAdjustments,
  };

  const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `heading-adjustments-${timestamp}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  showEditFeedback(`Exported ${count} adjustments`);
}

function clearLocalEdits() {
  const count = Object.keys(headingAdjustments).length;
  if (count === 0) { showEditFeedback('No local edits to clear'); return; }

  if (confirm(`Clear ${count} local edit(s)?`)) {
    headingAdjustments = {};
    saveHeadingAdjustments();
    loadPhoto(currentIndex);
    updateEditPanel();
    showEditFeedback(`Cleared ${count} local edits`);
  }
}

// ========================================
// Utilities
// ========================================
function createConeImageData() {
  const size = 32;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');

  const cx = size / 2;
  const cy = size / 2;

  const coneLength = 14;
  const halfAngle = 30 * Math.PI / 180;
  const dir = -Math.PI / 2; // points up (north)

  ctx.fillStyle = 'white';
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, coneLength, dir - halfAngle, dir + halfAngle);
  ctx.closePath();
  ctx.fill();

  return ctx.getImageData(0, 0, size, size);
}

function calculateBearing(lat1, lon1, lat2, lon2) {
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const lat1Rad = lat1 * Math.PI / 180;
  const lat2Rad = lat2 * Math.PI / 180;

  const y = Math.sin(dLon) * Math.cos(lat2Rad);
  const x = Math.cos(lat1Rad) * Math.sin(lat2Rad) -
            Math.sin(lat1Rad) * Math.cos(lat2Rad) * Math.cos(dLon);

  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

function generateRouteColors(count) {
  const baseColors = [
    '#22d3ee', '#f97316', '#a855f7', '#10b981', '#f43f5e',
    '#3b82f6', '#eab308', '#ec4899', '#14b8a6', '#8b5cf6',
    '#84cc16', '#06b6d4', '#f59e0b', '#6366f1', '#ef4444',
    '#0ea5e9', '#d946ef', '#22c55e',
  ];
  return Array.from({ length: count }, (_, i) => baseColors[i % baseColors.length]);
}

// ========================================
// Start
// ========================================
document.addEventListener('DOMContentLoaded', init);
