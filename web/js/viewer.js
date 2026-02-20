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

// Source-frame lookup for keyframe propagation
let sourceFrameIndices = {};  // source -> [globalIndex, ...]
let frameSourcePosition = {}; // globalIndex -> position within source array

// Smooth transition state
let viewers = { a: null, b: null };
let activeViewer = 'a';
let isTransitioning = false;
let preloadCache = new Map();
const PRELOAD_COUNT = 2;
const CROSSFADE_DURATION = 250;

// Route Builder state
let routeBuilderMode = false;
let routeFrameIndices = new Set();
let routeReversed = false;
let routeFlippedFrames = new Set(); // frames needing 180° yaw offset during playback
let savedRoutes = [];
let routePlayback = null; // { indices: [...], position: 0 }
let routeTurning = null; // active cross-source yaw animation { animId }
const ROUTE_STORAGE_KEY = 'tokyo-streetview-routes';

// Lasso state
let lassoActive = false;
let lassoStartPx = null;
let lassoIsDeselect = false;
let lassoMoved = false;
let lassoJustFinished = false;

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
  rbPanel: null,
  rbCount: null,
  rbPlayBtn: null,
  rbClearBtn: null,
  rbSaveBtn: null,
  rbSavedList: null,
  rbExportBtn: null,
  rbImportInput: null,
  lassoRect: null,
  rbPlaybackBar: null,
  rbPlaybackCounter: null,
  rbExitPlayback: null,
  rbToggle: null,
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

    frames = allData.frames
      .filter(f => f.neighborhood === neighborhoodId)
      .map(f => {
        if (f.lat_snapped != null && f.lon_snapped != null) {
          return { ...f, lat_original: f.lat, lon_original: f.lon, lat: f.lat_snapped, lon: f.lon_snapped };
        }
        return f;
      });

    if (frames.length === 0) {
      console.error('No frames found for neighborhood:', neighborhoodId);
      window.location.href = 'index.html';
      return;
    }

    buildSourceFrameIndices();
    calculateFrameBearings();

    document.title = `${neighborhoodMeta.name} - Tokyo Walks`;
    elements.neighborhoodName.textContent = neighborhoodMeta.name;
    elements.neighborhoodNameJa.textContent = neighborhoodMeta.nameJa;

    initPanorama();
    initMinimap();
    initControls();
    initEditMode();
    initRouteBuilder();

    loadPhoto(0);
    startConeSync();
  } catch (error) {
    console.error('Failed to initialize viewer:', error);
  }
}

// ========================================
// Heading Adjustments (Keyframe Propagation)
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

function buildSourceFrameIndices() {
  sourceFrameIndices = {};
  frameSourcePosition = {};
  frames.forEach((frame, index) => {
    if (!sourceFrameIndices[frame.source]) sourceFrameIndices[frame.source] = [];
    frameSourcePosition[index] = sourceFrameIndices[frame.source].length;
    sourceFrameIndices[frame.source].push(index);
  });
}

function getFrameHeading(frame, index) {
  // 1. This frame is an explicit keyframe in localStorage
  if (headingAdjustments[frame.filename]) {
    const adj = headingAdjustments[frame.filename];
    return { yaw: adj.yaw, pitch: adj.pitch, roll: adj.roll || 0, source: 'keyframe', keyframeIndex: index };
  }

  // 2. Walk backwards in same source to find nearest keyframe
  const indices = sourceFrameIndices[frame.source];
  if (indices) {
    const pos = frameSourcePosition[index];
    for (let i = pos - 1; i >= 0; i--) {
      const ki = indices[i];
      const kf = frames[ki];
      if (headingAdjustments[kf.filename]) {
        const adj = headingAdjustments[kf.filename];
        return { yaw: adj.yaw, pitch: adj.pitch, roll: adj.roll || 0, source: 'inherited', keyframeIndex: ki };
      }
    }
  }

  // 3. Baked values from data.json
  if (frame.yaw != null || frame.pitch != null || frame.roll != null) {
    return { yaw: frame.yaw || 0, pitch: frame.pitch || 0, roll: frame.roll || 0, source: 'data' };
  }

  // 4. Default
  return { yaw: 0, pitch: 0, roll: 0, source: 'default' };
}

function getEffectiveHeading(frame, index) {
  const heading = getFrameHeading(frame, index);
  if (routePlayback && routeFlippedFrames.has(index)) {
    return { ...heading, yaw: (heading.yaw + 180) % 360 };
  }
  return heading;
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
      if (lassoJustFinished) return;
      if (e.features && e.features.length > 0) {
        const idx = e.features[0].properties.index;
        if (routePlayback) {
          navigateRoutePlaybackTo(idx);
        } else if (routeBuilderMode) {
          toggleFrameInRoute(idx);
        } else {
          loadPhoto(idx);
        }
      }
    });

    map.on('mouseenter', 'photos-points', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'photos-points', () => { map.getCanvas().style.cursor = ''; });

    map.on('click', 'route-selection-dots', (e) => {
      if (!routePlayback) return;
      if (e.features && e.features.length > 0) {
        const idx = e.features[0].properties.index;
        navigateRoutePlaybackTo(idx);
      }
    });

    map.on('mouseenter', 'route-selection-dots', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'route-selection-dots', () => { map.getCanvas().style.cursor = ''; });

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

    // Route selection highlight layers (above everything)
    map.addSource('route-selection', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    });

    map.addLayer({
      id: 'route-selection-glow',
      type: 'circle',
      source: 'route-selection',
      filter: ['==', '$type', 'Point'],
      paint: {
        'circle-radius': 14,
        'circle-color': '#facc15',
        'circle-opacity': 0.35,
        'circle-blur': 0.5,
      },
    });

    map.addLayer({
      id: 'route-selection-line',
      type: 'line',
      source: 'route-selection',
      filter: ['==', '$type', 'LineString'],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': '#facc15', 'line-width': 4, 'line-opacity': 0.95 },
    });

    map.addLayer({
      id: 'route-selection-dots',
      type: 'circle',
      source: 'route-selection',
      filter: ['==', '$type', 'Point'],
      paint: {
        'circle-radius': 7,
        'circle-color': '#ffffff',
        'circle-opacity': 1,
        'circle-stroke-width': 3,
        'circle-stroke-color': '#facc15',
      },
    });

    // Direction arrows along route lines
    map.addLayer({
      id: 'route-selection-arrows',
      type: 'symbol',
      source: 'route-selection',
      filter: ['==', '$type', 'LineString'],
      layout: {
        'symbol-placement': 'line',
        'symbol-spacing': 70,
        'text-field': '▶',
        'text-size': 14,
        'text-allow-overlap': true,
        'text-rotation-alignment': 'map',
        'text-keep-upright': false,
      },
      paint: { 'text-color': '#facc15', 'text-opacity': 0.9 },
    });

    // Start/End endpoint markers
    map.addSource('route-endpoints', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    });

    map.addLayer({
      id: 'route-endpoint-start',
      type: 'circle',
      source: 'route-endpoints',
      filter: ['==', ['get', 'type'], 'start'],
      paint: {
        'circle-radius': 11,
        'circle-color': '#22c55e',
        'circle-opacity': 1,
        'circle-stroke-width': 3,
        'circle-stroke-color': '#ffffff',
      },
    });

    map.addLayer({
      id: 'route-endpoint-end',
      type: 'circle',
      source: 'route-endpoints',
      filter: ['==', ['get', 'type'], 'end'],
      paint: {
        'circle-radius': 11,
        'circle-color': '#ef4444',
        'circle-opacity': 1,
        'circle-stroke-width': 3,
        'circle-stroke-color': '#ffffff',
      },
    });

    // Ensure active marker renders above everything else
    map.moveLayer('active-cone-glow');
    map.moveLayer('active-cone');
    map.moveLayer('active-dot');

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
  const color = routePlayback ? '#facc15' : (frameColors[index] || '#ffffff');
  const baseBearing = frameBearings[index] || 0;
  const flipOffset = (routePlayback && routeFlippedFrames.has(index)) ? 180 : 0;
  const bearing = baseBearing + flipOffset;

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
  lastSyncedBearing = null;

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

  const heading = getEffectiveHeading(frame, index);
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

  const heading = getEffectiveHeading(frame, index);
  const imagePath = FRAMES_BASE + frame.filename;

  const nextViewerKey = activeViewer === 'a' ? 'b' : 'a';
  const currentContainer = activeViewer === 'a' ? elements.panoramaA : elements.panoramaB;
  const nextContainer = nextViewerKey === 'a' ? elements.panoramaA : elements.panoramaB;

  prepareNextPanorama(nextViewerKey, nextContainer, imagePath, heading, () => {
    performCrossfade(currentContainer, nextContainer, nextViewerKey, () => {
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
  setTimeout(safeCallback, 500);
}

function performCrossfade(outgoing, incoming, incomingKey, callback) {
  incoming.classList.add('panorama-active');
  outgoing.classList.add('panorama-fading');
  outgoing.classList.remove('panorama-active');

  setTimeout(() => {
    outgoing.classList.remove('panorama-fading');
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
    if (frame.lat && frame.lon) {
      let coordText = `${frame.lat.toFixed(6)}, ${frame.lon.toFixed(6)}`;
      if (frame.lat_original != null) coordText += ' (snapped)';
      elements.debugCoords.textContent = coordText;
    } else {
      elements.debugCoords.textContent = 'No coordinates';
    }
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
  map.flyTo({ center: [frame.lon, frame.lat], duration: 500 });
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
  elements.prevBtn.addEventListener('click', () => {
    if (routePlayback) navigateRoutePlayback(-1);
    else loadPhoto(currentIndex - 1);
  });
  elements.nextBtn.addEventListener('click', () => {
    if (routePlayback) navigateRoutePlayback(1);
    else loadPhoto(currentIndex + 1);
  });

  if (elements.navCounterInput) {
    const jumpToPhoto = () => {
      const num = parseInt(elements.navCounterInput.value, 10);
      if (routePlayback) {
        if (!isNaN(num) && num >= 1 && num <= routePlayback.indices.length) {
          routePlayback.position = num - 1;
          loadPhoto(routePlayback.indices[routePlayback.position]);
          updatePlaybackUI();
        } else {
          elements.navCounterInput.value = routePlayback.position + 1;
        }
      } else if (!isNaN(num) && num >= 1 && num <= frames.length) {
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
      if (routePlayback) navigateRoutePlayback(-1);
      else loadPhoto(currentIndex - 1);
    } else if (e.key === 'ArrowRight' || e.key === 'd') {
      e.preventDefault();
      if (routePlayback) navigateRoutePlayback(1);
      else loadPhoto(currentIndex + 1);
    } else if (e.key === 'Escape' && routePlayback) {
      exitRoutePlayback();
    } else if ((e.key === 'r' || e.key === 'R') && !editMode) {
      toggleRouteBuilder();
    } else if ((e.key === 'e' || e.key === 'E') && !routeBuilderMode) {
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
        <div class="edit-hint">Press E to toggle &middot; Keyframe mode</div>
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
          <span class="status-text">Default (no adjustment)</span>
        </div>
        <div class="edit-propagation-info" id="edit-propagation-info"></div>
        <div class="edit-buttons">
          <button class="edit-btn edit-btn-primary" id="edit-save-btn">Save as Keyframe (⌘S)</button>
          <button class="edit-btn edit-btn-secondary" id="edit-use-current-btn">Use Current View</button>
          <button class="edit-btn edit-btn-secondary" id="edit-remove-keyframe-btn" style="display:none">Remove Keyframe</button>
        </div>
        <div class="edit-export">
          <button class="edit-btn edit-btn-export" id="edit-export-btn">Export Keyframes</button>
          <button class="edit-btn edit-btn-clear" id="edit-clear-btn">Clear All Keyframes</button>
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
  elements.editRemoveKeyframeBtn = document.getElementById('edit-remove-keyframe-btn');
  elements.editUseCurrentBtn = document.getElementById('edit-use-current-btn');
  elements.editExportBtn = document.getElementById('edit-export-btn');
  elements.editClearBtn = document.getElementById('edit-clear-btn');
  elements.editStatus = document.getElementById('edit-status');
  elements.editPropagationInfo = document.getElementById('edit-propagation-info');
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
  elements.editRemoveKeyframeBtn.addEventListener('click', removeKeyframe);
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
  const heading = getFrameHeading(frame, currentIndex);

  elements.editAutoBearing.textContent = `${autoBearing.toFixed(1)}°`;
  elements.editYaw.value = heading.yaw.toFixed(1);
  elements.editPitch.value = heading.pitch.toFixed(1);
  elements.editRoll.value = heading.roll.toFixed(1);

  const statusText = elements.editStatus.querySelector('.status-text');
  elements.editStatus.className = 'edit-row edit-status';

  if (heading.source === 'keyframe') {
    elements.editStatus.classList.add('has-keyframe');
    statusText.textContent = 'Keyframe (set on this frame)';
    elements.editSaveBtn.textContent = 'Update Keyframe (⌘S)';
    elements.editRemoveKeyframeBtn.style.display = '';
  } else if (heading.source === 'inherited') {
    elements.editStatus.classList.add('has-inherited');
    statusText.textContent = `Inherited from frame #${heading.keyframeIndex + 1}`;
    elements.editSaveBtn.textContent = 'Save as Keyframe (⌘S)';
    elements.editRemoveKeyframeBtn.style.display = 'none';
  } else if (heading.source === 'data') {
    elements.editStatus.classList.add('has-data');
    statusText.textContent = 'From data.json (baked)';
    elements.editSaveBtn.textContent = 'Save as Keyframe (⌘S)';
    elements.editRemoveKeyframeBtn.style.display = 'none';
  } else {
    statusText.textContent = 'Default (no adjustment)';
    elements.editSaveBtn.textContent = 'Save as Keyframe (⌘S)';
    elements.editRemoveKeyframeBtn.style.display = 'none';
  }

  updatePropagationInfo(heading);
  updateExportCount();
}

function updatePropagationInfo(heading) {
  if (!elements.editPropagationInfo) return;

  if (heading.source === 'keyframe') {
    const count = countPropagatedFrames(currentIndex);
    elements.editPropagationInfo.innerHTML = count > 0
      ? `<span class="propagation-count">Applies to ${count} following frame${count !== 1 ? 's' : ''}</span>`
      : `<span class="propagation-count">No following frames affected</span>`;
    elements.editPropagationInfo.style.display = '';
  } else if (heading.source === 'inherited') {
    const kfFrame = frames[heading.keyframeIndex];
    const kfParts = kfFrame.filename.split('/');
    elements.editPropagationInfo.innerHTML =
      `<span class="propagation-source">From: ${kfParts[kfParts.length - 1]}</span>` +
      `<a class="propagation-jump" href="#" id="edit-jump-keyframe">Jump to keyframe</a>`;
    elements.editPropagationInfo.style.display = '';
    document.getElementById('edit-jump-keyframe').onclick = (e) => {
      e.preventDefault();
      loadPhoto(heading.keyframeIndex);
    };
  } else {
    elements.editPropagationInfo.style.display = 'none';
    elements.editPropagationInfo.innerHTML = '';
  }
}

function countPropagatedFrames(keyframeIndex) {
  const frame = frames[keyframeIndex];
  const indices = sourceFrameIndices[frame.source];
  if (!indices) return 0;

  const pos = frameSourcePosition[keyframeIndex];
  let count = 0;
  for (let i = pos + 1; i < indices.length; i++) {
    const fi = indices[i];
    if (headingAdjustments[frames[fi].filename]) break;
    count++;
  }
  return count;
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
  showEditFeedback('Keyframe saved!');
}

function removeKeyframe() {
  const frame = frames[currentIndex];
  if (!headingAdjustments[frame.filename]) return;

  delete headingAdjustments[frame.filename];
  saveHeadingAdjustments();
  loadPhoto(currentIndex, true);
  updateEditPanel();
  showEditFeedback('Keyframe removed');
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
    elements.editExportCount.textContent = count > 0 ? `(${count} keyframes)` : '(no keyframes)';
  }
}

function exportAdjustments() {
  const count = Object.keys(headingAdjustments).length;
  if (count === 0) { alert('No keyframe adjustments to export.'); return; }

  const exportData = {
    exported: new Date().toISOString(),
    mode: 'keyframe',
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

  showEditFeedback(`Exported ${count} keyframes`);
}

function clearLocalEdits() {
  const count = Object.keys(headingAdjustments).length;
  if (count === 0) { showEditFeedback('No keyframes to clear'); return; }

  if (confirm(`Clear ${count} keyframe(s)? This will remove all local heading adjustments.`)) {
    headingAdjustments = {};
    saveHeadingAdjustments();
    loadPhoto(currentIndex, true);
    updateEditPanel();
    showEditFeedback(`Cleared ${count} keyframes`);
  }
}

// ========================================
// Route Builder
// ========================================
function initRouteBuilder() {
  const panelHTML = `
    <div class="rb-panel" id="rb-panel">
      <div class="rb-header">
        <div class="rb-title">Route Builder</div>
        <div class="rb-hint">R to toggle · Drag to lasso · Alt+drag to deselect</div>
      </div>
      <div class="rb-content">
        <div class="rb-selection-info">
          <span class="rb-count" id="rb-count">0</span> frames selected
        </div>
        <div class="rb-direction" id="rb-direction">
          <div class="rb-direction-row">
            <span class="rb-endpoint-dot rb-dot-start"></span>
            <span class="rb-endpoint-label">Start</span>
            <span class="rb-endpoint-dot rb-dot-end"></span>
            <span class="rb-endpoint-label">End</span>
            <button class="rb-reverse-btn" id="rb-reverse-btn" title="Reverse direction">⇄ Reverse</button>
          </div>
        </div>
        <div class="rb-actions">
          <button class="edit-btn edit-btn-primary" id="rb-play-btn" disabled>Play Route</button>
          <button class="edit-btn edit-btn-secondary" id="rb-clear-btn">Clear Selection</button>
        </div>
        <div class="rb-divider"></div>
        <div class="rb-actions">
          <button class="edit-btn edit-btn-secondary" id="rb-save-btn" disabled>Save Route</button>
        </div>
        <div class="rb-saved-list" id="rb-saved-list"></div>
        <div class="rb-divider"></div>
        <div class="rb-io">
          <button class="edit-btn edit-btn-export" id="rb-export-btn">Export</button>
          <label class="edit-btn edit-btn-export rb-import-label" id="rb-import-label">
            Import
            <input type="file" id="rb-import-input" accept=".json" style="display:none">
          </label>
        </div>
      </div>
    </div>
  `;
  document.querySelector('.viewer-container').insertAdjacentHTML('beforeend', panelHTML);

  const playbackHTML = `
    <div class="rb-playback-bar" id="rb-playback-bar">
      <span class="rb-playback-label">Route Playback</span>
      <span class="rb-playback-counter" id="rb-playback-counter">1 / 1</span>
      <button class="edit-btn edit-btn-secondary rb-exit-btn" id="rb-exit-playback">Exit</button>
    </div>
  `;
  document.querySelector('.viewer-container').insertAdjacentHTML('beforeend', playbackHTML);

  elements.rbPanel = document.getElementById('rb-panel');
  elements.rbCount = document.getElementById('rb-count');
  elements.rbPlayBtn = document.getElementById('rb-play-btn');
  elements.rbClearBtn = document.getElementById('rb-clear-btn');
  elements.rbSaveBtn = document.getElementById('rb-save-btn');
  elements.rbSavedList = document.getElementById('rb-saved-list');
  elements.rbExportBtn = document.getElementById('rb-export-btn');
  elements.rbImportInput = document.getElementById('rb-import-input');
  elements.lassoRect = document.getElementById('lasso-rect');
  elements.rbPlaybackBar = document.getElementById('rb-playback-bar');
  elements.rbPlaybackCounter = document.getElementById('rb-playback-counter');
  elements.rbExitPlayback = document.getElementById('rb-exit-playback');
  elements.rbToggle = document.getElementById('route-builder-toggle');

  elements.rbReverseBtn = document.getElementById('rb-reverse-btn');
  elements.rbDirection = document.getElementById('rb-direction');

  elements.rbPlayBtn.addEventListener('click', startRoutePlayback);
  elements.rbClearBtn.addEventListener('click', clearRouteSelection);
  elements.rbReverseBtn.addEventListener('click', reverseRouteDirection);
  elements.rbSaveBtn.addEventListener('click', saveRoute);
  elements.rbExportBtn.addEventListener('click', exportRouteJSON);
  elements.rbImportInput.addEventListener('change', importRouteJSON);
  elements.rbExitPlayback.addEventListener('click', exitRoutePlayback);
  elements.rbToggle.addEventListener('click', toggleRouteBuilder);

  loadSavedRoutes();
  renderSavedRoutesList();
}

function toggleRouteBuilder() {
  if (routePlayback) return;

  routeBuilderMode = !routeBuilderMode;
  elements.rbPanel.classList.toggle('visible', routeBuilderMode);
  elements.rbToggle.classList.toggle('active', routeBuilderMode);

  if (routeBuilderMode) {
    const container = document.querySelector('.viewer-container');
    if (!container.classList.contains('split-screen')) {
      elements.minimapToggle.click();
    }
    attachLassoEvents();
    dimBaseRouteLayers(true);
    updateRouteBuilderPanel();
  } else {
    detachLassoEvents();
    dimBaseRouteLayers(false);
    if (map) map.dragPan.enable();
  }
}

function dimBaseRouteLayers(dim) {
  if (!map) return;
  if (map.getLayer('photos-points')) {
    map.setPaintProperty('photos-points', 'circle-opacity', dim ? 0.3 : 0.85);
  }
  if (map.getLayer('photos-path')) {
    map.setPaintProperty('photos-path', 'line-opacity', dim ? 0.2 : 0.8);
  }
}

// ── Lasso Selection ──────────────────────────────────────
function attachLassoEvents() {
  if (!map) return;
  const container = map.getCanvasContainer();
  container.addEventListener('mousedown', onLassoStart);
  document.addEventListener('mousemove', onLassoMove);
  document.addEventListener('mouseup', onLassoEnd);
  map.dragPan.disable();
}

function detachLassoEvents() {
  if (!map) return;
  const container = map.getCanvasContainer();
  container.removeEventListener('mousedown', onLassoStart);
  document.removeEventListener('mousemove', onLassoMove);
  document.removeEventListener('mouseup', onLassoEnd);
  map.dragPan.enable();
}

function onLassoStart(e) {
  if (!routeBuilderMode || e.button !== 0) return;

  const rect = map.getCanvasContainer().getBoundingClientRect();
  lassoStartPx = { x: e.clientX - rect.left, y: e.clientY - rect.top };
  lassoIsDeselect = e.altKey;
  lassoMoved = false;
  lassoActive = true;
}

function onLassoMove(e) {
  if (!lassoActive) return;

  const rect = map.getCanvasContainer().getBoundingClientRect();
  const currentPx = { x: e.clientX - rect.left, y: e.clientY - rect.top };
  const dx = Math.abs(currentPx.x - lassoStartPx.x);
  const dy = Math.abs(currentPx.y - lassoStartPx.y);

  if (dx > 4 || dy > 4) {
    lassoMoved = true;
    showLassoRect(lassoStartPx, currentPx);
  }
}

function onLassoEnd(e) {
  if (!lassoActive) return;
  lassoActive = false;
  hideLassoRect();

  if (!lassoMoved) return;

  const rect = map.getCanvasContainer().getBoundingClientRect();
  const endPx = { x: e.clientX - rect.left, y: e.clientY - rect.top };

  const minX = Math.min(lassoStartPx.x, endPx.x);
  const minY = Math.min(lassoStartPx.y, endPx.y);
  const maxX = Math.max(lassoStartPx.x, endPx.x);
  const maxY = Math.max(lassoStartPx.y, endPx.y);

  // Convert pixel corners to geographic coordinates
  const sw = map.unproject([minX, maxY]);
  const ne = map.unproject([maxX, minY]);

  const selected = [];
  frames.forEach((frame, idx) => {
    if (frame.lat >= sw.lat && frame.lat <= ne.lat &&
        frame.lon >= sw.lng && frame.lon <= ne.lng) {
      selected.push(idx);
    }
  });

  if (lassoIsDeselect) {
    selected.forEach(idx => routeFrameIndices.delete(idx));
  } else {
    selected.forEach(idx => routeFrameIndices.add(idx));
  }

  lassoJustFinished = true;
  setTimeout(() => { lassoJustFinished = false; }, 150);

  updateRouteHighlight();
  updateRouteBuilderPanel();
}

function showLassoRect(start, current) {
  const el = elements.lassoRect;
  const mapRect = map.getCanvasContainer().getBoundingClientRect();
  const containerRect = elements.minimapContainer.getBoundingClientRect();
  const offsetX = mapRect.left - containerRect.left;
  const offsetY = mapRect.top - containerRect.top;

  const x = Math.min(start.x, current.x) + offsetX;
  const y = Math.min(start.y, current.y) + offsetY;
  const w = Math.abs(current.x - start.x);
  const h = Math.abs(current.y - start.y);

  el.style.left = x + 'px';
  el.style.top = y + 'px';
  el.style.width = w + 'px';
  el.style.height = h + 'px';
  el.style.display = 'block';
  el.classList.toggle('deselect', lassoIsDeselect);
}

function hideLassoRect() {
  elements.lassoRect.style.display = 'none';
}

function toggleFrameInRoute(idx) {
  if (routeFrameIndices.has(idx)) {
    routeFrameIndices.delete(idx);
  } else {
    routeFrameIndices.add(idx);
  }
  updateRouteHighlight();
  updateRouteBuilderPanel();
}

// ── Route Ordering (graph-based endpoint detection) ──────
function distBetweenFrames(i, j) {
  const f1 = frames[i], f2 = frames[j];
  const dlat = (f2.lat - f1.lat) * Math.PI / 180;
  const dlon = (f2.lon - f1.lon) * Math.PI / 180;
  const a = Math.sin(dlat / 2) ** 2 +
    Math.cos(f1.lat * Math.PI / 180) * Math.cos(f2.lat * Math.PI / 180) *
    Math.sin(dlon / 2) ** 2;
  return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function getSortedRouteIndices() {
  const all = Array.from(routeFrameIndices).sort((a, b) => a - b);
  if (all.length < 2) {
    routeFlippedFrames = routeReversed ? new Set(all) : new Set();
    return routeReversed ? [...all].reverse() : all;
  }

  const sourceGroups = {};
  all.forEach(idx => {
    const src = frames[idx].source;
    if (!sourceGroups[src]) sourceGroups[src] = [];
    sourceGroups[src].push(idx);
  });

  const sources = Object.keys(sourceGroups);
  if (sources.length === 1) {
    routeFlippedFrames = routeReversed ? new Set(all) : new Set();
    return routeReversed ? [...all].reverse() : all;
  }

  const { ordered, segmentReversed } = chainSourceSegments(sourceGroups);

  if (routeReversed) {
    // Flip toggles: forward segments become reversed, reversed become forward
    routeFlippedFrames = new Set(ordered.filter(idx => !segmentReversed.has(idx)));
    return ordered.reverse();
  } else {
    routeFlippedFrames = segmentReversed;
    return ordered;
  }
}

function chainSourceSegments(sourceGroups) {
  const CONNECT_THRESHOLD = 20; // meters

  const terminals = [];
  Object.entries(sourceGroups).forEach(([src, group]) => {
    terminals.push({ idx: group[0], src, pos: 'first' });
    if (group.length > 1) {
      terminals.push({ idx: group[group.length - 1], src, pos: 'last' });
    }
  });

  const deadEnds = terminals.filter(t =>
    !terminals.some(o =>
      o.src !== t.src && distBetweenFrames(t.idx, o.idx) < CONNECT_THRESHOLD
    )
  );

  let startT;
  if (deadEnds.length >= 1) {
    startT = deadEnds[0];
  } else {
    let maxDist = 0;
    let best = terminals[0];
    for (let i = 0; i < terminals.length; i++) {
      for (let j = i + 1; j < terminals.length; j++) {
        const d = distBetweenFrames(terminals[i].idx, terminals[j].idx);
        if (d > maxDist) { maxDist = d; best = terminals[i]; }
      }
    }
    startT = best;
  }

  const ordered = [];
  const segmentReversed = new Set();
  const visited = new Set();
  let curT = startT;

  while (curT && !visited.has(curT.src)) {
    visited.add(curT.src);
    const group = sourceGroups[curT.src];

    if (curT.pos === 'first') {
      ordered.push(...group);
    } else {
      // This segment is traversed backwards relative to recording
      const rev = [...group].reverse();
      rev.forEach(idx => segmentReversed.add(idx));
      ordered.push(...rev);
    }

    const lastIdx = ordered[ordered.length - 1];
    let nextT = null;
    let minDist = Infinity;
    for (const t of terminals) {
      if (visited.has(t.src)) continue;
      const d = distBetweenFrames(lastIdx, t.idx);
      if (d < minDist) { minDist = d; nextT = t; }
    }
    curT = nextT;
  }

  return { ordered, segmentReversed };
}

// ── Map Highlight ────────────────────────────────────────

function updateRouteHighlight() {
  if (!map || !map.getSource('route-selection')) return;

  const sortedIndices = getSortedRouteIndices();
  const features = [];

  let currentSegment = [];
  let currentSource = null;

  sortedIndices.forEach(idx => {
    const frame = frames[idx];
    if (!frame) return;

    if (frame.source !== currentSource) {
      if (currentSegment.length > 1) {
        features.push({
          type: 'Feature',
          properties: {},
          geometry: { type: 'LineString', coordinates: currentSegment },
        });
      }
      currentSegment = [];
      currentSource = frame.source;
    }

    currentSegment.push([frame.lon, frame.lat]);

    features.push({
      type: 'Feature',
      properties: { index: idx },
      geometry: { type: 'Point', coordinates: [frame.lon, frame.lat] },
    });
  });

  if (currentSegment.length > 1) {
    features.push({
      type: 'Feature',
      properties: {},
      geometry: { type: 'LineString', coordinates: currentSegment },
    });
  }

  map.getSource('route-selection').setData({
    type: 'FeatureCollection',
    features,
  });

  updateRouteEndpoints(sortedIndices);
}

function updateRouteEndpoints(sortedIndices) {
  if (!map || !map.getSource('route-endpoints')) return;

  if (!sortedIndices || sortedIndices.length === 0) {
    map.getSource('route-endpoints').setData({ type: 'FeatureCollection', features: [] });
    return;
  }

  const startFrame = frames[sortedIndices[0]];
  const endFrame = frames[sortedIndices[sortedIndices.length - 1]];

  map.getSource('route-endpoints').setData({
    type: 'FeatureCollection',
    features: [
      {
        type: 'Feature',
        properties: { type: 'start' },
        geometry: { type: 'Point', coordinates: [startFrame.lon, startFrame.lat] },
      },
      {
        type: 'Feature',
        properties: { type: 'end' },
        geometry: { type: 'Point', coordinates: [endFrame.lon, endFrame.lat] },
      },
    ],
  });
}

// ── Panel UI ─────────────────────────────────────────────
function updateRouteBuilderPanel() {
  const count = routeFrameIndices.size;
  elements.rbCount.textContent = count;
  elements.rbPlayBtn.disabled = count === 0;
  elements.rbSaveBtn.disabled = count === 0;
  if (elements.rbDirection) {
    elements.rbDirection.style.display = count > 0 ? '' : 'none';
  }
}

function reverseRouteDirection() {
  routeReversed = !routeReversed;
  updateRouteHighlight();
}

function clearRouteSelection() {
  routeFrameIndices.clear();
  routeReversed = false;
  routeFlippedFrames.clear();
  updateRouteHighlight();
  updateRouteBuilderPanel();
}

// ── Save / Load / Delete ─────────────────────────────────
function loadSavedRoutes() {
  try {
    const saved = localStorage.getItem(ROUTE_STORAGE_KEY);
    savedRoutes = saved ? JSON.parse(saved) : [];
  } catch (e) {
    savedRoutes = [];
  }
}

function persistSavedRoutes() {
  localStorage.setItem(ROUTE_STORAGE_KEY, JSON.stringify(savedRoutes));
}

function saveRoute() {
  if (routeFrameIndices.size === 0) return;

  const route = {
    id: 'route_' + Date.now(),
    created: new Date().toISOString(),
    neighborhoodId,
    frameIndices: getSortedRouteIndices(),
    reversed: routeReversed,
  };

  savedRoutes.push(route);
  persistSavedRoutes();
  renderSavedRoutesList();
  updateRouteBuilderPanel();
}

function deleteRoute(routeId) {
  savedRoutes = savedRoutes.filter(r => r.id !== routeId);
  persistSavedRoutes();
  renderSavedRoutesList();
}

function loadRouteIntoSelection(routeId) {
  const route = savedRoutes.find(r => r.id === routeId);
  if (!route) return;

  routeFrameIndices = new Set(route.frameIndices);
  routeReversed = route.reversed || false;
  updateRouteHighlight();
  updateRouteBuilderPanel();
}

function renderSavedRoutesList() {
  const myRoutes = savedRoutes.filter(r => r.neighborhoodId === neighborhoodId);

  if (myRoutes.length === 0) {
    elements.rbSavedList.innerHTML = '<div class="rb-no-routes">No saved routes</div>';
    return;
  }

  elements.rbSavedList.innerHTML = myRoutes.map((route, i) => {
    const date = new Date(route.created);
    const dateStr = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const timeStr = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    return `
      <div class="rb-saved-route" data-route-id="${route.id}">
        <div class="rb-route-info">
          <span class="rb-route-name">Route ${i + 1}</span>
          <span class="rb-route-meta">${route.frameIndices.length} frames · ${dateStr} ${timeStr}</span>
        </div>
        <div class="rb-route-actions">
          <button class="rb-route-btn rb-route-load" data-route-id="${route.id}" title="Load">▶</button>
          <button class="rb-route-btn rb-route-delete" data-route-id="${route.id}" title="Delete">✕</button>
        </div>
      </div>
    `;
  }).join('');

  elements.rbSavedList.querySelectorAll('.rb-route-load').forEach(btn => {
    btn.addEventListener('click', () => loadRouteIntoSelection(btn.dataset.routeId));
  });
  elements.rbSavedList.querySelectorAll('.rb-route-delete').forEach(btn => {
    btn.addEventListener('click', () => deleteRoute(btn.dataset.routeId));
  });
}

// ── Export / Import ──────────────────────────────────────
function exportRouteJSON() {
  const myRoutes = savedRoutes.filter(r => r.neighborhoodId === neighborhoodId);
  if (myRoutes.length === 0) {
    alert('No saved routes to export.');
    return;
  }

  const exportData = {
    exported: new Date().toISOString(),
    neighborhoodId,
    routes: myRoutes,
  };

  const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `routes-${neighborhoodId}-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function importRouteJSON(e) {
  const file = e.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = (event) => {
    try {
      const data = JSON.parse(event.target.result);
      const routes = data.routes || [];

      let imported = 0;
      routes.forEach(route => {
        if (!savedRoutes.some(r => r.id === route.id)) {
          savedRoutes.push(route);
          imported++;
        }
      });

      persistSavedRoutes();
      renderSavedRoutesList();
      alert(`Imported ${imported} route(s).`);
    } catch (err) {
      alert('Failed to import: ' + err.message);
    }
  };
  reader.readAsText(file);
  e.target.value = '';
}

// ── Route Playback ───────────────────────────────────────
function startRoutePlayback() {
  if (routeFrameIndices.size === 0) return;

  routePlayback = { indices: getSortedRouteIndices(), position: 0 };

  routeBuilderMode = false;
  elements.rbPanel.classList.remove('visible');
  elements.rbToggle.classList.remove('active');
  elements.rbPlaybackBar.classList.add('visible');

  if (map) map.dragPan.enable();
  detachLassoEvents();

  loadPhoto(routePlayback.indices[0], true);
  updatePlaybackUI();
}

function exitRoutePlayback() {
  cancelRouteTurn();
  routePlayback = null;
  elements.rbPlaybackBar.classList.remove('visible');

  updatePhotoUI(currentIndex, frames[currentIndex]);
}

function cancelRouteTurn() {
  if (routeTurning) {
    cancelAnimationFrame(routeTurning.animId);
    routeTurning = null;
  }
}

function animateYawTo(targetYaw, callback) {
  cancelRouteTurn();

  const startYaw = viewer.getYaw();
  let diff = targetYaw - startYaw;
  while (diff > 180) diff -= 360;
  while (diff < -180) diff += 360;

  if (Math.abs(diff) < 3) { callback(); return; }

  const duration = Math.min(Math.max(Math.abs(diff) * 14, 600), 2500);
  const startTime = performance.now();
  const state = { animId: 0 };
  routeTurning = state;

  function step(time) {
    if (routeTurning !== state) return;
    const t = Math.min((time - startTime) / duration, 1);
    const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    // Normalize to [-180, 180] so pannellum doesn't clamp the value
    const yaw = ((startYaw + diff * ease) % 360 + 540) % 360 - 180;
    viewer.setYaw(yaw);
    if (t < 1) {
      state.animId = requestAnimationFrame(step);
    } else {
      routeTurning = null;
      callback();
    }
  }

  state.animId = requestAnimationFrame(step);
}

function navigateRoutePlayback(delta) {
  if (!routePlayback) return;
  if (routeTurning) return;

  const newPos = routePlayback.position + delta;
  if (newPos < 0 || newPos >= routePlayback.indices.length) return;

  const curGlobal = routePlayback.indices[routePlayback.position];
  const nextGlobal = routePlayback.indices[newPos];
  const crossSource = frames[curGlobal].source !== frames[nextGlobal].source;

  if (crossSource) {
    const curHeading = getEffectiveHeading(frames[curGlobal], curGlobal);
    const SPAN = 8;

    // Compute current route direction from GPS positions several frames apart
    const backPos = Math.max(0, routePlayback.position - SPAN);
    const backIdx = routePlayback.indices[backPos];
    const curDir = (backPos < routePlayback.position)
      ? calculateBearing(frames[backIdx].lat, frames[backIdx].lon,
                         frames[curGlobal].lat, frames[curGlobal].lon)
      : (frameBearings[curGlobal] || 0) + (routeFlippedFrames.has(curGlobal) ? 180 : 0);

    // Compute next route direction from GPS positions several frames ahead
    const aheadPos = Math.min(routePlayback.indices.length - 1, newPos + SPAN);
    const aheadIdx = routePlayback.indices[aheadPos];
    const nextDir = (aheadPos > newPos)
      ? calculateBearing(frames[nextGlobal].lat, frames[nextGlobal].lon,
                         frames[aheadIdx].lat, frames[aheadIdx].lon)
      : (frameBearings[nextGlobal] || 0) + (routeFlippedFrames.has(nextGlobal) ? 180 : 0);

    let turn = nextDir - curDir;
    while (turn > 180) turn -= 360;
    while (turn < -180) turn += 360;

    const targetYaw = curHeading.yaw + turn;

    animateYawTo(targetYaw, () => {
      routePlayback.position = newPos;
      loadPhoto(nextGlobal, true);
      updatePlaybackUI();
    });
    return;
  }

  routePlayback.position = newPos;
  const isAdjacent = Math.abs(nextGlobal - curGlobal) === 1;
  if (isAdjacent) {
    loadPhoto(nextGlobal);
  } else {
    loadPhoto(nextGlobal, true);
  }
  updatePlaybackUI();
}

function navigateRoutePlaybackTo(frameIndex) {
  if (!routePlayback) return;
  const routePos = routePlayback.indices.indexOf(frameIndex);
  if (routePos === -1) return;
  routePlayback.position = routePos;
  loadPhoto(frameIndex, true);
  updatePlaybackUI();
}

function updatePlaybackUI() {
  if (!routePlayback) return;
  elements.rbPlaybackCounter.textContent =
    `${routePlayback.position + 1} / ${routePlayback.indices.length}`;

  elements.navCounterInput.value = routePlayback.position + 1;
  elements.navCounterTotal.textContent = `/ ${routePlayback.indices.length}`;

  elements.prevBtn.disabled = routePlayback.position === 0;
  elements.nextBtn.disabled = routePlayback.position === routePlayback.indices.length - 1;
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

// ========================================
// Cone Sync (tracks panorama view direction)
// ========================================
let lastSyncedBearing = null;

function startConeSync() {
  function sync() {
    requestAnimationFrame(sync);
    if (!map || !map.getSource('active-photo') || !viewer) return;

    const frame = frames[currentIndex];
    if (!frame) return;

    let viewerYaw;
    try { viewerYaw = viewer.getYaw(); } catch (_) { return; }

    const heading = getEffectiveHeading(frame, currentIndex);
    const baseBearing = frameBearings[currentIndex] || 0;
    const flipOffset = (routePlayback && routeFlippedFrames.has(currentIndex)) ? 180 : 0;
    const coneBearing = baseBearing + flipOffset + (viewerYaw - heading.yaw);

    // Only push an update when the bearing actually changes
    if (lastSyncedBearing !== null && Math.abs(coneBearing - lastSyncedBearing) < 0.5) return;
    lastSyncedBearing = coneBearing;

    const color = routePlayback ? '#facc15' : (frameColors[currentIndex] || '#ffffff');
    map.getSource('active-photo').setData({
      type: 'Feature',
      properties: { bearing: coneBearing, color },
      geometry: { type: 'Point', coordinates: [frame.lon, frame.lat] },
    });
  }
  requestAnimationFrame(sync);
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
