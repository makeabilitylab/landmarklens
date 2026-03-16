// ========================================
// Tokyo 360° Street View - Home Page
// ========================================

mapboxgl.accessToken = 'pk.eyJ1IjoiY3Jlc2NlbmRvY2h1IiwiYSI6ImNpdGR5MWZ5aDAycjIyc3A5ZHoxZzRwMGsifQ.nEaSxm520v7TpKAy2GG_kA';

const NEIGHBORHOOD_META = {
  ginza:      { name: 'Ginza',      nameJa: '銀座',   description: "Tokyo's premier upscale shopping and entertainment district, where luxury boutiques meet traditional kabuki theatre." },
  kinshicho:  { name: 'Kinshichō',  nameJa: '錦糸町', description: 'A vibrant commercial hub in eastern Tokyo blending modern malls with old-school izakaya culture.' },
  koenji:     { name: 'Kōenji',     nameJa: '高円寺', description: 'A bohemian neighborhood famous for vintage clothing shops, live music venues, and its annual Awa Odori festival.' },
  nezu:       { name: 'Nezu',       nameJa: '根津',   description: "One of Tokyo's few remaining old-town districts, known for its charming shrine, narrow lanes, and quiet cafés." },
  ningyocho:  { name: 'Ningyōchō',  nameJa: '人形町', description: 'A historic merchant quarter in Nihonbashi with Edo-era charm, famous for its traditional crafts, sweet shops, and atmospheric backstreets.' },
};

let frameData = null;
let neighborhoods = [];
let maps = {};

// ========================================
// Initialize
// ========================================
async function init() {
  try {
    const resp = await fetch('data.json');
    frameData = await resp.json();

    const grouped = {};
    frameData.frames.forEach(frame => {
      if (frame.lat_snapped != null && frame.lon_snapped != null) {
        frame = { ...frame, lat_original: frame.lat, lon_original: frame.lon, lat: frame.lat_snapped, lon: frame.lon_snapped };
      }
      const hood = frame.neighborhood || 'unknown';
      if (!grouped[hood]) grouped[hood] = [];
      grouped[hood].push(frame);
    });

    neighborhoods = Object.entries(grouped).map(([id, frames]) => {
      const meta = NEIGHBORHOOD_META[id] || { name: id, nameJa: '', description: '' };
      const lats = frames.map(f => f.lat);
      const lons = frames.map(f => f.lon);
      const center = [
        (Math.min(...lats) + Math.max(...lats)) / 2,
        (Math.min(...lons) + Math.max(...lons)) / 2,
      ];
      const totalDist = frames.reduce((sum, f) => sum + (f.distance_to_next || 0), 0);

      return { id, ...meta, center, frames, totalDistance: totalDist };
    });

    renderNeighborhoodCards();
    updateFooter();
  } catch (error) {
    console.error('Failed to initialize:', error);
    document.getElementById('footer-text').textContent = 'Failed to load data.';
  }
}

// ========================================
// Footer
// ========================================
function updateFooter() {
  const totalPhotos = frameData ? frameData.frame_count : 0;
  const totalDist = frameData ? (frameData.total_distance_m / 1000).toFixed(1) : 0;
  document.getElementById('footer-text').textContent =
    `${totalPhotos.toLocaleString()} panoramic photos · ${totalDist} km of routes`;
}

// ========================================
// Render Neighborhood Cards
// ========================================
function renderNeighborhoodCards() {
  const grid = document.getElementById('neighborhoods-grid');

  neighborhoods.forEach((neighborhood, index) => {
    const card = document.createElement('div');
    card.className = 'neighborhood-card';
    card.style.animationDelay = `${index * 100}ms`;

    const distKm = (neighborhood.totalDistance / 1000).toFixed(1);

    card.innerHTML = `
      <div class="card-map" id="map-${neighborhood.id}"></div>
      <div class="card-overlay"></div>
      <div class="card-content">
        <div class="card-header">
          <h2 class="card-name">${neighborhood.name}</h2>
          <span class="card-name-ja">${neighborhood.nameJa}</span>
        </div>
        <p class="card-description">${neighborhood.description}</p>
        <div class="card-meta">
          <div class="card-meta-item">
            <svg class="card-meta-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="3" width="18" height="18" rx="2"/>
              <circle cx="8.5" cy="8.5" r="1.5"/>
              <path d="M21 15l-5-5L5 21"/>
            </svg>
            <span>${neighborhood.frames.length} photos</span>
          </div>
          <div class="card-meta-item">
            <svg class="card-meta-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M3 17l6-6 4 4 8-8"/>
            </svg>
            <span>${distKm} km</span>
          </div>
        </div>
      </div>
      <div class="card-arrow">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <path d="M5 12h14M12 5l7 7-7 7"/>
        </svg>
      </div>
    `;

    card.addEventListener('click', () => {
      window.location.href = `viewer.html?neighborhood=${neighborhood.id}`;
    });

    grid.appendChild(card);
    setTimeout(() => initCardMap(neighborhood), 50);
  });
}

// ========================================
// Initialize Card Map
// ========================================
function initCardMap(neighborhood) {
  const mapContainer = document.getElementById(`map-${neighborhood.id}`);
  if (!mapContainer) return;

  const map = new mapboxgl.Map({
    container: mapContainer,
    style: 'mapbox://styles/mapbox/dark-v11',
    center: [neighborhood.center[1], neighborhood.center[0]],
    zoom: 15,
    interactive: false,
    attributionControl: false,
  });

  maps[neighborhood.id] = map;

  map.on('load', () => {
    if (neighborhood.frames.length === 0) return;

    const routesBySource = {};
    neighborhood.frames.forEach(frame => {
      if (!frame.lat || !frame.lon) return;
      if (!routesBySource[frame.source]) routesBySource[frame.source] = [];
      routesBySource[frame.source].push(frame);
    });

    const sourceNames = Object.keys(routesBySource);
    const routeColors = generateRouteColors(sourceNames.length);

    const allFeatures = [];
    const allCoordinates = [];

    sourceNames.forEach((source, routeIndex) => {
      const routeFrames = routesBySource[source];
      const color = routeColors[routeIndex];

      const lineCoords = routeFrames.map(f => [f.lon, f.lat]);
      allCoordinates.push(...lineCoords);

      if (lineCoords.length > 1) {
        allFeatures.push({
          type: 'Feature',
          properties: { color },
          geometry: { type: 'LineString', coordinates: lineCoords },
        });
      }

      routeFrames.forEach((f, i) => {
        let bearing = 0;
        if (i < routeFrames.length - 1) {
          bearing = calculateBearing(f.lat, f.lon, routeFrames[i + 1].lat, routeFrames[i + 1].lon);
        } else if (i > 0) {
          bearing = calculateBearing(routeFrames[i - 1].lat, routeFrames[i - 1].lon, f.lat, f.lon);
        }

        allFeatures.push({
          type: 'Feature',
          properties: { color, bearing },
          geometry: { type: 'Point', coordinates: [f.lon, f.lat] },
        });
      });
    });

    const sourceId = `route-${neighborhood.id}`;

    map.addSource(sourceId, {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: allFeatures },
    });

    map.addLayer({
      id: `${sourceId}-line`,
      type: 'line',
      source: sourceId,
      filter: ['==', '$type', 'LineString'],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': ['get', 'color'], 'line-width': 2, 'line-opacity': 0.7 },
    });

    map.addLayer({
      id: `${sourceId}-dots`,
      type: 'circle',
      source: sourceId,
      filter: ['==', '$type', 'Point'],
      paint: {
        'circle-radius': 3,
        'circle-color': ['get', 'color'],
        'circle-opacity': 0.85,
      },
    });

    if (allCoordinates.length > 0) {
      const bounds = allCoordinates.reduce(
        (b, coord) => b.extend(coord),
        new mapboxgl.LngLatBounds(allCoordinates[0], allCoordinates[0])
      );
      map.fitBounds(bounds, { padding: 40, duration: 0 });
    }
  });
}

// ========================================
// Utilities
// ========================================
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
