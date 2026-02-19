// ========================================
// Tokyo 360° Street View - Viewer Page
// ========================================

// Mapbox access token - replace with your own
mapboxgl.accessToken = 'pk.eyJ1IjoiY3Jlc2NlbmRvY2h1IiwiYSI6ImNpdGR5MWZ5aDAycjIyc3A5ZHoxZzRwMGsifQ.nEaSxm520v7TpKAy2GG_kA';

// State
let neighborhood = null;
let photos = [];
let folders = {}; // Map folder name to folder data (including gpx_file)
let currentIndex = 0;
let viewer = null;
let map = null;
let activeMarker = null;
let photoColors = {}; // Map photo index to route color
let photoBearings = {}; // Map photo index to auto-calculated bearing
let lineVisible = true;
let splitRatio = 0.5; // 0 = all panorama, 1 = all map (default 50/50)
let isDragging = false;
let debugExpanded = true;

// Edit mode state
let editMode = false;
let headingAdjustments = {}; // Map photo path to { yaw, pitch } adjustments
const STORAGE_KEY = 'geophotos-heading-adjustments';

// Smooth transition state
let viewers = { a: null, b: null };  // Dual viewers for crossfade
let activeViewer = 'a';              // Which viewer is currently active
let isTransitioning = false;         // Prevent overlapping transitions
let preloadCache = new Map();        // Preloaded images: index -> { img, loaded }
const PRELOAD_COUNT = 2;             // Preload next/prev N photos
const CROSSFADE_DURATION = 400;      // Crossfade duration

// DOM Elements
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
  navCounter: document.getElementById('nav-counter'),
  navCounterInput: document.getElementById('nav-counter-input'),
  navCounterTotal: document.getElementById('nav-counter-total'),
  navControls: document.querySelector('.nav-controls'),
  // Debug elements
  debugPanel: document.getElementById('debug-panel'),
  debugContent: document.getElementById('debug-content'),
  debugToggle: document.getElementById('debug-toggle'),
  debugFolder: document.getElementById('debug-folder'),
  debugFile: document.getElementById('debug-file'),
  debugCoords: document.getElementById('debug-coords'),
  debugGpx: document.getElementById('debug-gpx'),
  debugColor: document.getElementById('debug-color'),
  // Split divider
  splitDivider: document.getElementById('split-divider'),
  // Edit mode elements (will be created dynamically)
  editPanel: null,
  editYaw: null,
  editPitch: null,
  editAutoBearing: null,
  editSaveBtn: null,
  editResetBtn: null,
  editExportBtn: null
};

// ========================================
// Initialize
// ========================================
async function init() {
  try {
    // Get neighborhood ID from URL
    const urlParams = new URLSearchParams(window.location.search);
    const neighborhoodId = urlParams.get('neighborhood');
    
    if (!neighborhoodId) {
      window.location.href = 'index.html';
      return;
    }
    
    // Load saved heading adjustments from localStorage
    loadHeadingAdjustments();
    
    // Load data with aggressive cache busting
    const cacheBuster = `?t=${Date.now()}&v=${Math.random()}`;
    const [neighborhoodsData, indexData] = await Promise.all([
      fetch(`../data/neighborhoods.json${cacheBuster}`).then(r => r.json()),
      fetch(`../data/index.json${cacheBuster}`).then(r => r.json())
    ]);
    
    console.log(`Loaded index.json with ${indexData.photos?.length || 0} photos at ${new Date().toISOString()}`);
    
    // Find neighborhood
    neighborhood = neighborhoodsData.neighborhoods.find(n => n.id === neighborhoodId);
    
    if (!neighborhood) {
      window.location.href = 'index.html';
      return;
    }
    
    // Store folders data for GPX file lookup
    if (indexData.folders) {
      indexData.folders.forEach(f => {
        folders[f.name] = f;
      });
    }
    
    // Get photos for this neighborhood
    photos = getPhotosForNeighborhood(neighborhood, indexData.photos);
    
    if (photos.length === 0) {
      console.error('No photos found for neighborhood');
      return;
    }
    
    // Calculate bearings for all photos
    calculatePhotoBearings();
    
    // Update UI
    document.title = `${neighborhood.name} - Tokyo Walks`;
    elements.neighborhoodName.textContent = neighborhood.name;
    elements.neighborhoodNameJa.textContent = neighborhood.nameJa;
    
    // Initialize viewer and map
    initPanorama();
    initMinimap();
    initControls();
    initEditMode();
    
    // Load first photo
    loadPhoto(0);
    
  } catch (error) {
    console.error('Failed to initialize viewer:', error);
  }
}

// ========================================
// Heading Adjustments - Load/Save
// ========================================
function loadHeadingAdjustments() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      headingAdjustments = JSON.parse(saved);
      console.log(`Loaded ${Object.keys(headingAdjustments).length} heading adjustments`);
    }
  } catch (e) {
    console.error('Failed to load heading adjustments:', e);
    headingAdjustments = {};
  }
}

function saveHeadingAdjustments() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(headingAdjustments));
    console.log(`Saved ${Object.keys(headingAdjustments).length} heading adjustments`);
  } catch (e) {
    console.error('Failed to save heading adjustments:', e);
  }
}

function getPhotoHeading(photo, index) {
  // Priority 1: Check localStorage for local edits (not yet saved to index.json)
  const localSaved = headingAdjustments[photo.path];
  if (localSaved) {
    return { 
      yaw: localSaved.yaw, 
      pitch: localSaved.pitch, 
      roll: localSaved.roll || 0,
      source: 'local' 
    };
  }
  
  // Priority 2: Check index.json data (permanent storage)
  if (photo.yaw !== undefined || photo.pitch !== undefined || photo.roll !== undefined) {
    return { 
      yaw: photo.yaw || 0, 
      pitch: photo.pitch || 0,
      roll: photo.roll || 0,
      source: 'index' 
    };
  }
  
  // Default to center of image (yaw: 0)
  return { yaw: 0, pitch: 0, roll: 0, source: 'default' };
}

// ========================================
// Calculate Bearings for All Photos
// ========================================
function calculatePhotoBearings() {
  // Group photos by folder
  const photosByFolder = {};
  photos.forEach((photo, index) => {
    if (!photosByFolder[photo.folder]) {
      photosByFolder[photo.folder] = [];
    }
    photosByFolder[photo.folder].push({ photo, index });
  });
  
  // Calculate bearing for each photo based on direction to next photo in same folder
  Object.values(photosByFolder).forEach(folderPhotos => {
    // Sort by timestamp
    folderPhotos.sort((a, b) => new Date(a.photo.timestamp) - new Date(b.photo.timestamp));
    
    folderPhotos.forEach((item, i) => {
      let bearing = 0;
      
      if (i < folderPhotos.length - 1) {
        // Calculate bearing to next photo
        const next = folderPhotos[i + 1].photo;
        bearing = calculateBearing(
          item.photo.lat, item.photo.lon,
          next.lat, next.lon
        );
      } else if (i > 0) {
        // Last photo: use bearing from previous
        const prev = folderPhotos[i - 1].photo;
        bearing = calculateBearing(
          prev.lat, prev.lon,
          item.photo.lat, item.photo.lon
        );
      }
      
      photoBearings[item.index] = bearing;
    });
  });
  
  console.log(`Calculated bearings for ${Object.keys(photoBearings).length} photos`);
}

// ========================================
// Get Photos for Neighborhood
// ========================================
function getPhotosForNeighborhood(neighborhood, allPhotos) {
  return allPhotos.filter(photo => {
    if (photo.date !== neighborhood.date) return false;
    if (!photo.lat || !photo.lon) return false;
    
    if (neighborhood.timeRange) {
      const photoTime = new Date(photo.timestamp).getTime();
      const startTime = new Date(neighborhood.timeRange.start).getTime();
      const endTime = new Date(neighborhood.timeRange.end).getTime();
      if (photoTime < startTime || photoTime > endTime) return false;
    }
    
    return true;
  }).sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
}

// ========================================
// Initialize Pannellum Panorama Viewers (Dual for transitions)
// ========================================
function initPanorama() {
  // Initialize both panorama viewers
  viewers.a = createViewer('panorama-a');
  viewers.b = createViewer('panorama-b');
  viewer = viewers.a; // Start with viewer A as active
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
    northOffset: 0,
    showFullscreenCtrl: false,
    showZoomCtrl: false,
    friction: 0.15,
    yaw: 0,
    pitch: 0
  });
}

// ========================================
// Preloading System
// ========================================
function preloadAdjacentPhotos(currentIdx) {
  // Preload next and previous photos
  const indicesToPreload = [];
  
  for (let i = 1; i <= PRELOAD_COUNT; i++) {
    if (currentIdx + i < photos.length) indicesToPreload.push(currentIdx + i);
    if (currentIdx - i >= 0) indicesToPreload.push(currentIdx - i);
  }
  
  indicesToPreload.forEach(idx => {
    if (!preloadCache.has(idx)) {
      const photo = photos[idx];
      const imagePath = `../photos/output/${photo.path}`;
      const img = new Image();
      
      preloadCache.set(idx, { img, loaded: false, path: imagePath });
      
      img.onload = () => {
        const cached = preloadCache.get(idx);
        if (cached) cached.loaded = true;
      };
      
      img.src = imagePath;
    }
  });
  
  // Clean up old cache entries (keep only nearby photos)
  const keepRange = PRELOAD_COUNT + 2;
  for (const [idx] of preloadCache) {
    if (Math.abs(idx - currentIdx) > keepRange) {
      preloadCache.delete(idx);
    }
  }
}

function isPhotoPreloaded(idx) {
  const cached = preloadCache.get(idx);
  return cached && cached.loaded;
}

// ========================================
// Initialize Mapbox Minimap
// ========================================
function initMinimap() {
  // Destroy existing map if it exists
  if (map) {
    map.remove();
    map = null;
  }
  
  map = new mapboxgl.Map({
    container: 'minimap',
    style: 'mapbox://styles/mapbox/dark-v11',
    center: [neighborhood.center[1], neighborhood.center[0]],
    zoom: 16,
    attributionControl: false
  });
  
    // Use 'once' instead of 'on' to ensure this only runs once
  map.once('load', () => {
    console.log(`Map loaded, processing ${photos.length} photos`);
    
    // Remove existing source and layers if they exist (in case of reload)
    if (map.getSource('photos')) {
      console.log('Removing existing photos source and layers');
      // Remove layers first
      if (map.getLayer('photos-path')) map.removeLayer('photos-path');
      if (map.getLayer('photos-glow')) map.removeLayer('photos-glow');
      if (map.getLayer('photos-points')) map.removeLayer('photos-points');
      // Remove source
      map.removeSource('photos');
    }
    
    // Group photos by folder (each folder = one route/direction)
    const routesByFolder = {};
    
    photos.forEach((photo, index) => {
      if (photo.lat && photo.lon && photo.folder) {
        if (!routesByFolder[photo.folder]) {
          routesByFolder[photo.folder] = [];
        }
        routesByFolder[photo.folder].push({ photo, index });
      }
    });
    
    console.log(`Grouped photos into ${Object.keys(routesByFolder).length} routes`);
    
    // Generate distinct colors for each route
    const folderNames = Object.keys(routesByFolder);
    const routeColors = generateRouteColors(folderNames.length);
    
    // Build features for each route
    const allFeatures = [];
    
    folderNames.forEach((folder, routeIndex) => {
      const routePhotos = routesByFolder[folder];
      const color = routeColors[routeIndex];
      
      // Sort by timestamp within folder
      routePhotos.sort((a, b) => new Date(a.photo.timestamp) - new Date(b.photo.timestamp));
      
      // Track coordinate usage WITHIN THIS ROUTE ONLY for offsetting overlapping points
      const routeCoordCounts = {};
      
      // Create line for this route (using original coordinates)
      const lineCoords = routePhotos.map(r => [r.photo.lon, r.photo.lat]);
      if (lineCoords.length > 1) {
        allFeatures.push({
          type: 'Feature',
          properties: { color: color, routeIndex: routeIndex },
          geometry: {
            type: 'LineString',
            coordinates: lineCoords
          }
        });
      }
      
      // Create points for this route with offset for overlapping coords WITHIN THIS ROUTE
      routePhotos.forEach((r, routePhotoIndex) => {
        const key = `${r.photo.lat.toFixed(6)},${r.photo.lon.toFixed(6)}`;
        if (!routeCoordCounts[key]) {
          routeCoordCounts[key] = 0;
        }
        const count = routeCoordCounts[key];
        routeCoordCounts[key]++;
        
        // Store color for this photo (for debug display)
        photoColors[r.index] = color;
        
        // Apply spiral offset for overlapping points WITHIN THE SAME ROUTE
        // Only offset if there are multiple photos at the same location in this route
        const offset = count > 0 ? getPointOffset(count) : { lat: 0, lon: 0 };
        
        // Calculate bearing to next photo (direction of travel)
        let bearing = 0; // Default to North if no direction can be determined
        if (routePhotoIndex < routePhotos.length - 1) {
          const nextPhoto = routePhotos[routePhotoIndex + 1].photo;
          bearing = calculateBearing(
            r.photo.lat, r.photo.lon,
            nextPhoto.lat, nextPhoto.lon
          );
        } else if (routePhotoIndex > 0) {
          // Last photo: use bearing from previous photo
          const prevPhoto = routePhotos[routePhotoIndex - 1].photo;
          bearing = calculateBearing(
            prevPhoto.lat, prevPhoto.lon,
            r.photo.lat, r.photo.lon
          );
        }
        
        allFeatures.push({
          type: 'Feature',
          properties: { 
            index: r.index, 
            color: color,
            routeIndex: routeIndex,
            bearing: bearing
          },
          geometry: {
            type: 'Point',
            coordinates: [r.photo.lon + offset.lon, r.photo.lat + offset.lat]
          }
        });
      });
    });
    
    const pointCount = allFeatures.filter(f => f.geometry.type === 'Point').length;
    const lineCount = allFeatures.filter(f => f.geometry.type === 'LineString').length;
    console.log(`Adding ${allFeatures.length} features to map (${pointCount} points, ${lineCount} lines)`);
    
    map.addSource('photos', {
      type: 'geojson',
      data: {
        type: 'FeatureCollection',
        features: allFeatures
      }
    });
    
    // Path layer (drawn first, underneath dots)
    map.addLayer({
      id: 'photos-path',
      type: 'line',
      source: 'photos',
      filter: ['==', '$type', 'LineString'],
      layout: {
        'line-join': 'round',
        'line-cap': 'round'
      },
      paint: {
        'line-color': ['get', 'color'],
        'line-width': 2.5,
        'line-opacity': 0.8
      }
    });
    
    updateLineVisibility();
    
    // Create arrow icon using canvas (Mapbox doesn't support SVG)
    if (!map.hasImage('arrow-icon')) {
      const size = 24;
      const canvas = document.createElement('canvas');
      canvas.width = size;
      canvas.height = size;
      const ctx = canvas.getContext('2d');
      
      // Draw arrow pointing UP (North = 0 degrees, Mapbox rotates clockwise from North)
      ctx.strokeStyle = 'white';
      ctx.lineWidth = 2.5;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      
      // Arrow body (vertical line pointing up)
      ctx.beginPath();
      ctx.moveTo(size / 2, size - 4);
      ctx.lineTo(size / 2, 6);
      ctx.stroke();
      
      // Arrow head pointing up
      ctx.beginPath();
      ctx.moveTo(size / 2 - 5, 10);
      ctx.lineTo(size / 2, 4);
      ctx.lineTo(size / 2 + 5, 10);
      ctx.stroke();
      
      // Create image data from canvas
      const imageData = ctx.getImageData(0, 0, size, size);
      map.addImage('arrow-icon', imageData, { sdf: true });
    }
    
    addArrowLayers();
    
    function addArrowLayers() {
      
      // Glow layer for all arrows (shadow effect)
      map.addLayer({
        id: 'photos-glow',
        type: 'symbol',
        source: 'photos',
        filter: ['==', '$type', 'Point'],
        layout: {
          'icon-image': 'arrow-icon',
          'icon-size': 1.2,
          'icon-rotate': ['get', 'bearing'],
          'icon-rotation-alignment': 'map',
          'icon-allow-overlap': true,
          'icon-ignore-placement': true
        },
        paint: {
          'icon-color': ['get', 'color'],
          'icon-opacity': 0.3
        }
      });
      
      // All photo arrows (on top)
      map.addLayer({
        id: 'photos-points',
        type: 'symbol',
        source: 'photos',
        filter: ['==', '$type', 'Point'],
        layout: {
          'icon-image': 'arrow-icon',
          'icon-size': 0.8,
          'icon-rotate': ['get', 'bearing'],
          'icon-rotation-alignment': 'map',
          'icon-allow-overlap': true,
          'icon-ignore-placement': true
        },
        paint: {
          'icon-color': ['get', 'color'],
          'icon-opacity': 1
        }
      });
      
      // Click handler for arrows
      map.on('click', 'photos-points', (e) => {
        if (e.features && e.features.length > 0) {
          const index = e.features[0].properties.index;
          loadPhoto(index);
        }
      });
      
      // Change cursor on hover
      map.on('mouseenter', 'photos-points', () => {
        map.getCanvas().style.cursor = 'pointer';
      });
      
      map.on('mouseleave', 'photos-points', () => {
        map.getCanvas().style.cursor = '';
      });
    }
    
    // Add active marker (will be updated when photo changes)
    addActiveMarker();
    
    // Fit bounds
    const coordinates = photos.map(p => [p.lon, p.lat]);
    if (coordinates.length > 0) {
      const bounds = coordinates.reduce((bounds, coord) => {
        return bounds.extend(coord);
      }, new mapboxgl.LngLatBounds(coordinates[0], coordinates[0]));
      
      map.fitBounds(bounds, {
        padding: 40,
        duration: 0
      });
    }
  });
}

// ========================================
// Add Active Marker to Minimap
// ========================================
function addActiveMarker() {
  const el = document.createElement('div');
  el.className = 'active-marker';
  
  // Set initial color to match the first photo's route
  const initialColor = photoColors[0] || '#22d3ee';
  el.style.borderColor = initialColor;
  el.style.boxShadow = `0 0 0 4px ${initialColor}33, 0 2px 8px rgba(0, 0, 0, 0.4)`;
  
  activeMarker = new mapboxgl.Marker({
    element: el,
    anchor: 'center'
  })
    .setLngLat([photos[0].lon, photos[0].lat])
    .addTo(map);
}

// ========================================
// Load Photo (with smooth transitions)
// ========================================
function loadPhoto(index, forceImmediate = false) {
  if (index < 0 || index >= photos.length) return;
  if (isTransitioning && !forceImmediate) return; // Prevent overlapping transitions
  
  const previousIndex = currentIndex;
  const isForward = index > previousIndex;
  const isAdjacent = Math.abs(index - previousIndex) === 1;
  const shouldAnimate = isAdjacent && !forceImmediate && previousIndex !== index;
  
  if (shouldAnimate && isForward) {
    // Smooth forward transition
    transitionToPhoto(index, true);
  } else if (shouldAnimate && !isForward) {
    // Backward transition (simpler fade)
    transitionToPhoto(index, false);
  } else {
    // Direct load (initial load, jump, or forced)
    loadPhotoImmediate(index);
  }
}

// Direct load without animation (for initial load or jumps)
function loadPhotoImmediate(index) {
  const photo = photos[index];
  currentIndex = index;
  
  // Show loading only if not preloaded
  if (!isPhotoPreloaded(index)) {
    elements.loading.classList.remove('hidden');
  }
  
  const heading = getPhotoHeading(photo, index);
  const imagePath = `../photos/output/${photo.path}`;
  
  // Use active viewer
  const activeContainer = activeViewer === 'a' ? elements.panoramaA : elements.panoramaB;
  const currentViewer = viewers[activeViewer];
  
  // Destroy and recreate
  currentViewer.destroy();
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
    horizonRoll: heading.roll
  });
  
  viewer = viewers[activeViewer];
  
  viewer.on('load', () => {
    elements.loading.classList.add('hidden');
    preloadAdjacentPhotos(index);
  });
  
  updatePhotoUI(index, photo);
}

// Smooth animated transition
function transitionToPhoto(index, isForward) {
  isTransitioning = true;
  
  const photo = photos[index];
  currentIndex = index;
  
  const heading = getPhotoHeading(photo, index);
  const imagePath = `../photos/output/${photo.path}`;
  
  // Get current and next viewer
  const currentViewerKey = activeViewer;
  const nextViewerKey = activeViewer === 'a' ? 'b' : 'a';
  const currentContainer = currentViewerKey === 'a' ? elements.panoramaA : elements.panoramaB;
  const nextContainer = nextViewerKey === 'a' ? elements.panoramaA : elements.panoramaB;
  
  // Show motion overlay for forward movement
  if (isForward) {
    elements.transitionOverlay.classList.add('active');
  }
  
  // Prepare next panorama and crossfade (no rotation - headings are manually aligned)
  prepareNextPanorama(nextViewerKey, nextContainer, imagePath, heading, () => {
    performCrossfade(currentContainer, nextContainer, nextViewerKey, () => {
      // Cleanup
      elements.transitionOverlay.classList.remove('active');
      isTransitioning = false;
      preloadAdjacentPhotos(index);
    });
  });
  
  updatePhotoUI(index, photo);
}

// Prepare the next panorama in the inactive viewer
function prepareNextPanorama(viewerKey, container, imagePath, heading, callback) {
  let callbackFired = false;
  
  const safeCallback = () => {
    if (!callbackFired) {
      callbackFired = true;
      callback();
    }
  };
  
  const existingViewer = viewers[viewerKey];
  if (existingViewer) {
    existingViewer.destroy();
  }
  
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
    horizonRoll: heading.roll
  });
  
  viewers[viewerKey].on('load', safeCallback);
  
  // Timeout fallback in case load event doesn't fire
  setTimeout(safeCallback, 2000);
}

// Perform the crossfade between panoramas
function performCrossfade(outgoingContainer, incomingContainer, incomingViewerKey, callback) {
  // Set up classes for transition
  outgoingContainer.classList.add('panorama-outgoing');
  outgoingContainer.classList.remove('panorama-active');
  incomingContainer.classList.add('panorama-active');
  
  // Trigger zoom-in effect on outgoing (pushing forward into the scene)
  requestAnimationFrame(() => {
    outgoingContainer.classList.add('panorama-zoom-in');
  });
  
  // Wait for transition to complete
  setTimeout(() => {
    // Clean up outgoing
    outgoingContainer.classList.remove('panorama-outgoing', 'panorama-zoom-in');
    
    // Switch active viewer
    activeViewer = incomingViewerKey;
    viewer = viewers[activeViewer];
    
    callback();
  }, CROSSFADE_DURATION);
}

// Update all UI elements for a photo
function updatePhotoUI(index, photo) {
  // Update UI
  elements.photoInfo.textContent = formatTimestamp(photo.timestamp);
  
  // Update button states
  elements.prevBtn.disabled = index === 0;
  elements.nextBtn.disabled = index === photos.length - 1;
  
  // Update counter (1-based for display)
  if (elements.navCounterInput) {
    elements.navCounterInput.value = index + 1;
    elements.navCounterInput.max = photos.length;
    elements.navCounterInput.min = 1;
  }
  if (elements.navCounterTotal) {
    elements.navCounterTotal.textContent = `/ ${photos.length}`;
  }
  
  // Update debug info
  if (elements.debugFolder) {
    elements.debugFolder.textContent = photo.folder || 'N/A';
    elements.debugFile.textContent = photo.filename || 'N/A';
    elements.debugCoords.textContent = photo.lat && photo.lon 
      ? `${photo.lat.toFixed(6)}, ${photo.lon.toFixed(6)}` 
      : 'No coordinates';
    
    // Get GPX file from folder data
    const folderData = folders[photo.folder];
    elements.debugGpx.textContent = folderData?.gpx_file || 'No GPX';
    
    const color = photoColors[index] || '#999';
    elements.debugColor.innerHTML = `<span class="debug-color-swatch" style="background:${color}"></span>${color}`;
  }
  
  // Update map
  updateMapPosition(photo);
  
  // Update active marker
  updateActiveMarker(index);
  
  // Update edit panel if in edit mode
  if (editMode) {
    updateEditPanel();
  }
}

// ========================================
// Update Map Position
// ========================================
function updateMapPosition(photo) {
  if (!map) return;
  
  map.flyTo({
    center: [photo.lon, photo.lat],
    zoom: 17,
    duration: 500
  });
}

// ========================================
// Update Active Marker
// ========================================
function updateActiveMarker(index) {
  if (activeMarker && photos[index]) {
    activeMarker.setLngLat([photos[index].lon, photos[index].lat]);
    
    // Update color to match the current photo's route
    const el = activeMarker.getElement();
    const color = photoColors[index] || '#22d3ee';
    el.style.borderColor = color;
    el.style.boxShadow = `0 0 0 4px ${color}33, 0 2px 8px rgba(0, 0, 0, 0.4)`;
  }
}

// ========================================
// Toggle Route Lines
// ========================================
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
// Initialize Controls
// ========================================
function initControls() {
  // Previous/Next buttons
  elements.prevBtn.addEventListener('click', () => {
    loadPhoto(currentIndex - 1);
  });
  
  elements.nextBtn.addEventListener('click', () => {
    loadPhoto(currentIndex + 1);
  });
  
  // Jump to photo by number
  if (elements.navCounterInput) {
    const jumpToPhoto = () => {
      const num = parseInt(elements.navCounterInput.value, 10);
      if (!isNaN(num) && num >= 1 && num <= photos.length) {
        loadPhoto(num - 1);
      } else {
        elements.navCounterInput.value = currentIndex + 1;
      }
    };
    elements.navCounterInput.addEventListener('change', jumpToPhoto);
    elements.navCounterInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        jumpToPhoto();
      }
    });
  }
  
  // Keyboard navigation
  document.addEventListener('keydown', (e) => {
    // Don't handle keys if typing in an input
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
  
  // Minimap toggle - split screen mode
  elements.minimapToggle.addEventListener('click', () => {
    const container = document.querySelector('.viewer-container');
    container.classList.toggle('split-screen');
    
    // Show/hide divider and update layout
    if (container.classList.contains('split-screen')) {
      elements.splitDivider.style.display = 'block';
      // Reset to 50/50 split when entering split-screen mode
      splitRatio = 0.5;
      updateSplitLayout();
    } else {
      elements.splitDivider.style.display = 'none';
      // Reset nav-controls and debug panel positions when exiting split-screen
      if (elements.navControls) {
        elements.navControls.style.bottom = '';
      }
      if (elements.debugPanel) {
        elements.debugPanel.style.bottom = '';
      }
    }
    
    // Resize both map and panoramas after transition
    setTimeout(() => {
      map.resize();
      if (viewers.a) viewers.a.resize();
      if (viewers.b) viewers.b.resize();
    }, 300);
  });
  
  // Line toggle - show/hide route lines
  if (elements.lineToggle) {
    elements.lineToggle.addEventListener('click', () => {
      lineVisible = !lineVisible;
      updateLineVisibility();
    });
  }
  
  // Debug toggle - minimize/expand
  if (elements.debugToggle) {
    elements.debugToggle.addEventListener('click', () => {
      debugExpanded = !debugExpanded;
      elements.debugPanel.classList.toggle('collapsed', !debugExpanded);
      elements.debugContent.style.display = debugExpanded ? 'block' : 'none';
      
      // Rotate icon
      const icon = elements.debugToggle.querySelector('svg');
      icon.style.transform = debugExpanded ? 'rotate(0deg)' : 'rotate(180deg)';
    });
  }
  
  // Split divider drag functionality
  initSplitDivider();
}

// ========================================
// Split Divider Control
// ========================================
function initSplitDivider() {
  const divider = elements.splitDivider;
  if (!divider) return;
  
  divider.addEventListener('mousedown', startDrag);
  document.addEventListener('mousemove', drag);
  document.addEventListener('mouseup', stopDrag);
  
  // Touch support
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
  
  // Disable transitions during drag for smooth interaction
  const panoramaContainer = document.querySelector('.panorama-container');
  const minimapContainer = document.querySelector('.minimap-container');
  const divider = elements.splitDivider;
  
  panoramaContainer.style.transition = 'none';
  minimapContainer.style.transition = 'none';
  divider.style.transition = 'none';
  if (elements.navControls) {
    elements.navControls.style.transition = 'none';
  }
  if (elements.debugPanel) {
    elements.debugPanel.style.transition = 'none';
  }
}

function drag(e) {
  if (!isDragging) return;
  
  e.preventDefault();
  const container = document.querySelector('.viewer-container');
  if (!container.classList.contains('split-screen')) return;
  
  const clientY = e.touches ? e.touches[0].clientY : e.clientY;
  const containerRect = container.getBoundingClientRect();
  const relativeY = clientY - containerRect.top;
  const containerHeight = containerRect.height;
  
  // Calculate split ratio (0 = all panorama at top, 1 = all map at bottom)
  splitRatio = Math.max(0.1, Math.min(0.9, relativeY / containerHeight));
  
  updateSplitLayout();
}

function stopDrag() {
  if (!isDragging) return;
  
  isDragging = false;
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  
  // Re-enable transitions after drag
  const panoramaContainer = document.querySelector('.panorama-container');
  const minimapContainer = document.querySelector('.minimap-container');
  const divider = elements.splitDivider;
  
  panoramaContainer.style.transition = '';
  minimapContainer.style.transition = '';
  divider.style.transition = '';
  if (elements.navControls) {
    elements.navControls.style.transition = '';
  }
  if (elements.debugPanel) {
    elements.debugPanel.style.transition = '';
  }
}

function updateSplitLayout() {
  const container = document.querySelector('.viewer-container');
  if (!container.classList.contains('split-screen')) return;
  
  const panoramaHeight = splitRatio * 100;
  const mapHeight = (1 - splitRatio) * 100;
  const dividerPosition = splitRatio * 100;
  
  // Update panorama container
  const panoramaContainer = document.querySelector('.panorama-container');
  panoramaContainer.style.height = `${panoramaHeight}%`;
  
  // Update minimap container
  const minimapContainer = document.querySelector('.minimap-container');
  minimapContainer.style.top = `${dividerPosition}%`;
  minimapContainer.style.height = `${mapHeight}%`;
  
  // Update divider position
  elements.splitDivider.style.top = `${dividerPosition}%`;
  
  // Update nav-controls position - position them at bottom of panorama container
  // Since panorama container is at top with height = splitRatio * 100%,
  // nav-controls should be at bottom: (1 - splitRatio) * 100% + 2rem from viewport bottom
  const navControlsBottom = (1 - splitRatio) * 100;
  elements.navControls.style.bottom = `calc(${navControlsBottom}% + 2rem)`;
  
  // Update debug panel position - keep it relative to panorama container bottom
  if (elements.debugPanel) {
    elements.debugPanel.style.bottom = `calc(${navControlsBottom}% + 6rem)`;
  }
  
  // Resize map and panoramas
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
  // Create edit panel HTML
  const editPanelHTML = `
    <div class="edit-panel" id="edit-panel">
      <div class="edit-header">
        <div class="edit-title">Adjust Heading</div>
        <div class="edit-hint">Press E to toggle edit mode</div>
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
          <span class="status-text">Using auto-bearing</span>
        </div>
        <div class="edit-buttons">
          <button class="edit-btn edit-btn-primary" id="edit-save-btn">
            Save (Cmd+S)
          </button>
          <button class="edit-btn edit-btn-secondary" id="edit-use-current-btn">
            Use Current View
          </button>
          <button class="edit-btn edit-btn-secondary" id="edit-reset-btn">
            Reset to Auto
          </button>
        </div>
        <div class="edit-export">
          <button class="edit-btn edit-btn-export" id="edit-export-btn">
            Export Local Edits
          </button>
          <button class="edit-btn edit-btn-clear" id="edit-clear-btn">
            Clear Local Edits
          </button>
        </div>
        <div class="edit-export-count" id="edit-export-count"></div>
      </div>
    </div>
  `;
  
  // Insert edit panel into DOM
  document.querySelector('.viewer-container').insertAdjacentHTML('beforeend', editPanelHTML);
  
  // Get element references
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
  
  // Create grid overlay
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
  
  // Set up event listeners
  elements.editSaveBtn.addEventListener('click', saveCurrentHeading);
  elements.editResetBtn.addEventListener('click', resetCurrentHeading);
  elements.editUseCurrentBtn.addEventListener('click', useCurrentView);
  elements.editExportBtn.addEventListener('click', exportAdjustments);
  elements.editClearBtn.addEventListener('click', clearLocalEdits);
  elements.editGridCheckbox.addEventListener('change', toggleGrid);
  
  // Apply heading when input changes
  elements.editYaw.addEventListener('input', applyEditedHeading);
  elements.editPitch.addEventListener('input', applyEditedHeading);
  elements.editRoll.addEventListener('input', applyEditedRoll);
  
  // Update export count
  updateExportCount();
}

function toggleEditMode() {
  editMode = !editMode;
  elements.editPanel.classList.toggle('visible', editMode);
  
  if (editMode) {
    updateEditPanel();
  } else {
    // Hide grid when exiting edit mode
    if (elements.gridOverlay) {
      elements.gridOverlay.classList.remove('visible');
    }
    if (elements.editGridCheckbox) {
      elements.editGridCheckbox.checked = false;
    }
  }
  
  console.log(`Edit mode: ${editMode ? 'ON' : 'OFF'}`);
}

function toggleGrid() {
  const showGrid = elements.editGridCheckbox.checked;
  elements.gridOverlay.classList.toggle('visible', showGrid);
}

function updateEditPanel() {
  if (!elements.editPanel) return;
  
  const photo = photos[currentIndex];
  const autoBearing = photoBearings[currentIndex] || 0;
  const heading = getPhotoHeading(photo, currentIndex);
  
  // Update GPS bearing display (for reference)
  elements.editAutoBearing.textContent = `${autoBearing.toFixed(1)}°`;
  
  // Update input values
  elements.editYaw.value = heading.yaw.toFixed(1);
  elements.editPitch.value = heading.pitch.toFixed(1);
  elements.editRoll.value = heading.roll.toFixed(1);
  
  // Update status based on source
  const statusText = elements.editStatus.querySelector('.status-text');
  if (heading.source === 'local') {
    elements.editStatus.classList.add('has-custom');
    elements.editStatus.classList.remove('has-index');
    statusText.textContent = 'Local edit (not exported)';
  } else if (heading.source === 'index') {
    elements.editStatus.classList.add('has-custom');
    elements.editStatus.classList.add('has-index');
    statusText.textContent = 'Saved in index.json';
  } else {
    elements.editStatus.classList.remove('has-custom');
    elements.editStatus.classList.remove('has-index');
    statusText.textContent = 'Using default (center)';
  }
  
  updateExportCount();
}

function applyEditedHeading() {
  const yaw = parseFloat(elements.editYaw.value) || 0;
  const pitch = parseFloat(elements.editPitch.value) || 0;
  
  // Apply to current viewer
  viewer.setYaw(yaw);
  viewer.setPitch(pitch);
}

function applyEditedRoll() {
  // Roll requires reloading the panorama (Pannellum limitation)
  const photo = photos[currentIndex];
  const imagePath = `../photos/output/${photo.path}`;
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
    horizonRoll: roll
  });
  viewer = viewers[activeViewer];
}

function useCurrentView() {
  // Get current viewer yaw/pitch and put into inputs
  const currentYaw = viewer.getYaw();
  const currentPitch = viewer.getPitch();
  
  elements.editYaw.value = currentYaw.toFixed(1);
  elements.editPitch.value = currentPitch.toFixed(1);
}

function saveCurrentHeading() {
  const photo = photos[currentIndex];
  const yaw = parseFloat(elements.editYaw.value) || 0;
  const pitch = parseFloat(elements.editPitch.value) || 0;
  const roll = parseFloat(elements.editRoll.value) || 0;
  
  // Save adjustment
  headingAdjustments[photo.path] = { yaw, pitch, roll };
  saveHeadingAdjustments();
  
  // Update UI
  updateEditPanel();
  
  // Show feedback
  showEditFeedback('Saved!');
}

function resetCurrentHeading() {
  const photo = photos[currentIndex];
  
  // Remove local adjustment
  delete headingAdjustments[photo.path];
  saveHeadingAdjustments();
  
  // Note: This only clears local edits. To clear index.json heading,
  // run: python scripts/import_heading_adjustments.py --clear
  
  // Reload photo
  loadPhoto(currentIndex);
  
  // Update UI
  updateEditPanel();
  
  // Show feedback
  const hasIndexHeading = photo.yaw !== undefined || photo.pitch !== undefined || photo.roll !== undefined;
  showEditFeedback(hasIndexHeading ? 'Reset to index value' : 'Reset to default');
}

function showEditFeedback(message) {
  const statusText = elements.editStatus.querySelector('.status-text');
  const originalText = statusText.textContent;
  
  statusText.textContent = message;
  statusText.classList.add('feedback');
  
  setTimeout(() => {
    statusText.classList.remove('feedback');
    updateEditPanel(); // Restore proper status
  }, 1500);
}

function updateExportCount() {
  const localCount = Object.keys(headingAdjustments).length;
  if (elements.editExportCount) {
    elements.editExportCount.textContent = localCount > 0 ? `(${localCount} local edits)` : '(no local edits)';
  }
}

function exportAdjustments() {
  const count = Object.keys(headingAdjustments).length;
  
  if (count === 0) {
    alert('No heading adjustments to export.');
    return;
  }
  
  // Create export data
  const exportData = {
    exported: new Date().toISOString(),
    count: count,
    adjustments: headingAdjustments
  };
  
  // Create filename with date and time
  const now = new Date();
  const timestamp = now.toISOString().replace(/[:.]/g, '-').slice(0, 19);
  
  // Create download
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
  
  if (count === 0) {
    showEditFeedback('No local edits to clear');
    return;
  }
  
  if (confirm(`Clear ${count} local edit(s)? Make sure you've imported them first.`)) {
    headingAdjustments = {};
    saveHeadingAdjustments();
    loadPhoto(currentIndex); // Reload to show index.json values
    updateEditPanel();
    showEditFeedback(`Cleared ${count} local edits`);
  }
}

// ========================================
// Utilities
// ========================================
function formatTimestamp(timestamp) {
  const date = new Date(timestamp);
  return date.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true
  });
}

// Calculate bearing (direction) between two points in degrees
// Returns bearing from point1 to point2 (0 = North, 90 = East, etc.)
function calculateBearing(lat1, lon1, lat2, lon2) {
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const lat1Rad = lat1 * Math.PI / 180;
  const lat2Rad = lat2 * Math.PI / 180;
  
  const y = Math.sin(dLon) * Math.cos(lat2Rad);
  const x = Math.cos(lat1Rad) * Math.sin(lat2Rad) - 
            Math.sin(lat1Rad) * Math.cos(lat2Rad) * Math.cos(dLon);
  
  const bearing = Math.atan2(y, x);
  return (bearing * 180 / Math.PI + 360) % 360; // Convert to degrees and normalize to 0-360
}

// Calculate offset for overlapping points (spiral pattern)
function getPointOffset(index) {
  if (index === 0) {
    return { lat: 0, lon: 0 };
  }
  
  // Spiral offset - each subsequent point gets placed in a circle around the original
  const offsetDistance = 0.00003; // ~3 meters at Tokyo's latitude
  const angle = (index * 137.5) * (Math.PI / 180); // Golden angle for even distribution
  const radius = offsetDistance * Math.sqrt(index);
  
  return {
    lat: radius * Math.cos(angle),
    lon: radius * Math.sin(angle)
  };
}

// Generate visually distinct colors for routes
function generateRouteColors(count) {
  const baseColors = [
    '#22d3ee', // cyan
    '#f97316', // orange
    '#a855f7', // purple
    '#10b981', // emerald
    '#f43f5e', // rose
    '#3b82f6', // blue
    '#eab308', // yellow
    '#ec4899', // pink
    '#14b8a6', // teal
    '#8b5cf6', // violet
    '#84cc16', // lime
    '#06b6d4', // cyan-500
    '#f59e0b', // amber
    '#6366f1', // indigo
    '#ef4444', // red
    '#0ea5e9', // sky
    '#d946ef', // fuchsia
    '#22c55e', // green
  ];
  
  const colors = [];
  for (let i = 0; i < count; i++) {
    colors.push(baseColors[i % baseColors.length]);
  }
  return colors;
}

// ========================================
// Start
// ========================================
document.addEventListener('DOMContentLoaded', init);
