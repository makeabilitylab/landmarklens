/**
 * 360° Street View Application
 *
 * Smooth crossfade transitions between panoramas using two
 * stacked Pannellum instances, plus image preloading.
 *
 * The GoPro export may have an offset between the image center
 * and the actual forward direction. Use the "Forward offset"
 * slider to correct this — the value is applied as the initial
 * yaw for every frame.
 */

// ─── State ─────────────────────────────────────────────────
let frameData = null;
let currentIndex = 0;
let yawOffset = 0;          // Corrects the "forward" direction in the panorama
let headingOffset = 0;       // For map indicator (kept separate)
let map = null;
let markers = [];
let routeLines = [];  // one polyline per route (by source)
let directionMarker = null;
let yawPollTimer = null;

// Dual-viewer system for crossfade
const viewers = [null, null];
let activeLayer = 0;
let isTransitioning = false;

// Preload cache
const preloadCache = new Map();
const PRELOAD_AHEAD = 3;

const FRAMES_BASE = 'frames/';

// ─── Initialize ────────────────────────────────────────────
async function init() {
    try {
        const resp = await fetch('data.json');
        frameData = await resp.json();
        console.log(`Loaded ${frameData.frame_count} frames, ${frameData.total_distance_m}m route`);
    } catch (err) {
        console.error('Failed to load data.json:', err);
        document.getElementById('pano-layer-0').innerHTML = `
            <div style="display:flex;align-items:center;justify-content:center;height:100%;color:#ff5252;font-size:18px;padding:40px;text-align:center;">
                Failed to load data.json. Run: <code>python3 script/extract_frames.py</code>
            </div>`;
        return;
    }

    initMap();
    loadFrame(0);
    bindEvents();
    startYawHUD();
}

// ─── Live Yaw HUD ──────────────────────────────────────────
function startYawHUD() {
    const el = document.getElementById('yaw-display');
    yawPollTimer = setInterval(() => {
        const v = viewers[activeLayer];
        if (!v) return;
        const yaw = v.getYaw();
        // Normalize to 0–360
        const display = ((yaw % 360) + 360) % 360;
        el.textContent = `Yaw: ${display.toFixed(1)}°`;
    }, 50); // 20fps update
}

// ─── Image Preloader ───────────────────────────────────────
function getImageUrl(index) {
    return FRAMES_BASE + frameData.frames[index].filename;
}

function preloadImage(index) {
    if (index < 0 || index >= frameData.frames.length) return;
    const url = getImageUrl(index);
    if (preloadCache.has(url)) return;
    const img = new Image();
    img.src = url;
    preloadCache.set(url, img);
}

function preloadAround(index) {
    for (let d = 1; d <= PRELOAD_AHEAD; d++) {
        preloadImage(index + d);
        preloadImage(index - d);
    }
}

// ─── Dual-Viewer Crossfade ─────────────────────────────────
function loadFrame(index) {
    if (!frameData || index < 0 || index >= frameData.frames.length) return;
    if (isTransitioning) return;

    const frame = frameData.frames[index];
    const imageUrl = getImageUrl(index);

    // First load — no transition needed
    if (viewers[activeLayer] === null) {
        currentIndex = index;
        viewers[activeLayer] = createViewer(
            `pano-layer-${activeLayer}`, imageUrl, yawOffset, 0, 110
        );
        preloadAround(index);
        updateUI(frame);
        updateMap(frame);
        return;
    }

    // Preserve user's look direction
    const prevYaw = viewers[activeLayer].getYaw();
    const prevPitch = viewers[activeLayer].getPitch();
    const prevHfov = viewers[activeLayer].getHfov();

    // Prepare back layer
    const nextLayer = 1 - activeLayer;
    const nextEl = document.getElementById(`pano-layer-${nextLayer}`);
    const activeEl = document.getElementById(`pano-layer-${activeLayer}`);

    if (viewers[nextLayer]) {
        viewers[nextLayer].destroy();
        viewers[nextLayer] = null;
    }

    nextEl.classList.remove('active');
    viewers[nextLayer] = createViewer(
        `pano-layer-${nextLayer}`, imageUrl, prevYaw, prevPitch, prevHfov
    );

    isTransitioning = true;
    currentIndex = index;

    let didFire = false;
    const onReady = () => {
        if (didFire) return;
        didFire = true;

        nextEl.classList.add('active');
        activeEl.classList.remove('active');

        setTimeout(() => {
            activeLayer = nextLayer;
            isTransitioning = false;
        }, 320);
    };

    viewers[nextLayer].on('load', onReady);
    setTimeout(() => { if (!didFire) onReady(); }, 150);

    preloadAround(index);
    updateUI(frame);
    updateMap(frame);
}

function createViewer(containerId, imageUrl, yaw, pitch, hfov) {
    return pannellum.viewer(containerId, {
        type: 'equirectangular',
        panorama: imageUrl,
        autoLoad: true,
        showControls: false,
        compass: false,
        hfov: hfov,
        yaw: yaw,
        pitch: pitch,
        mouseZoom: true,
        friction: 0.15,
        minHfov: 50,
        maxHfov: 120,
    });
}

// Distinct colors per route (cycle if more routes than colors)
const ROUTE_COLORS = [
    '#4f8cff', '#e8534f', '#4caf50', '#ffb74d', '#ab47bc',
    '#26c6da', '#ef5350', '#66bb6a', '#ffa726', '#ec407a',
];

// ─── Map Setup ─────────────────────────────────────────────
function initMap() {
    map = L.map('map', {
        zoomControl: true,
        attributionControl: false,
    });

    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        maxZoom: 22,
        subdomains: 'abcd',
    }).addTo(map);

    // Group frames by source (route) and assign colors
    const sources = [...new Set(frameData.frames.map(f => f.source))];
    const colorBySource = {};
    sources.forEach((src, idx) => {
        colorBySource[src] = ROUTE_COLORS[idx % ROUTE_COLORS.length];
    });

    // One polyline per route
    const allBounds = [];
    sources.forEach(source => {
        const coords = frameData.frames.filter(f => f.source === source).map(f => [f.lat, f.lon]);
        if (coords.length < 2) return;
        const color = colorBySource[source];
        const line = L.polyline(coords, {
            color,
            weight: 3,
            opacity: 0.7,
            smoothFactor: 1,
        }).addTo(map);
        routeLines.push(line);
        allBounds.push(line.getBounds());
    });

    // Markers: color by route; active marker stays highlighted
    frameData.frames.forEach((frame, i) => {
        const color = colorBySource[frame.source] || ROUTE_COLORS[0];
        const icon = L.divIcon({
            className: 'frame-marker',
            html: `<div class="frame-marker-dot" style="background:${color}"></div>`,
            iconSize: [14, 14],
            iconAnchor: [7, 7],
        });
        const marker = L.marker([frame.lat, frame.lon], { icon })
            .addTo(map)
            .on('click', () => loadFrame(i));
        markers.push(marker);
    });

    const dirIcon = L.divIcon({
        className: 'direction-marker',
        html: '<div class="arrow" id="direction-arrow"></div>',
        iconSize: [36, 36],
        iconAnchor: [18, 18],
    });
    directionMarker = L.marker([0, 0], { icon: dirIcon, zIndexOffset: 2000 }).addTo(map);

    if (routeLines.length) {
        const allLatLngs = routeLines.flatMap(line => line.getLatLngs());
        map.fitBounds(L.latLngBounds(allLatLngs).pad(0.2));
    }
}

function updateMap(frame) {
    markers.forEach((m, i) => {
        const el = m.getElement();
        if (el) {
            const dot = el.querySelector('.frame-marker') || el;
            dot.classList.toggle('active', i === currentIndex);
        }
    });

    const mapHeading = (frame.heading + headingOffset + 360) % 360;
    directionMarker.setLatLng([frame.lat, frame.lon]);
    const arrow = document.getElementById('direction-arrow');
    if (arrow) {
        arrow.style.transform = `rotate(${mapHeading}deg)`;
    }

    map.panTo([frame.lat, frame.lon], { animate: true, duration: 0.3 });
}

// ─── UI Updates ────────────────────────────────────────────
function updateUI(frame) {
    document.getElementById('frame-counter').textContent =
        `${currentIndex + 1} / ${frameData.frames.length}`;
    document.getElementById('heading-display').textContent =
        `GPS: ${frame.heading.toFixed(0)}°`;

    document.getElementById('btn-prev').disabled = currentIndex === 0;
    document.getElementById('btn-next').disabled = currentIndex === frameData.frames.length - 1;
}

// ─── Events ────────────────────────────────────────────────
function bindEvents() {
    document.getElementById('btn-prev').addEventListener('click', () => {
        if (currentIndex > 0) loadFrame(currentIndex - 1);
    });
    document.getElementById('btn-next').addEventListener('click', () => {
        if (currentIndex < frameData.frames.length - 1) loadFrame(currentIndex + 1);
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowLeft' || e.key === 'a') {
            e.preventDefault();
            if (currentIndex > 0) loadFrame(currentIndex - 1);
        } else if (e.key === 'ArrowRight' || e.key === 'd') {
            e.preventDefault();
            if (currentIndex < frameData.frames.length - 1) loadFrame(currentIndex + 1);
        }
    });

    // Forward offset slider — corrects the "forward" direction in the panorama
    const slider = document.getElementById('yaw-offset');
    const sliderVal = document.getElementById('yaw-offset-val');
    slider.addEventListener('input', () => {
        const newOffset = parseInt(slider.value, 10);
        const delta = newOffset - yawOffset;
        yawOffset = newOffset;
        sliderVal.textContent = `${yawOffset}°`;

        // Apply the delta to the current viewer so the change is live
        const v = viewers[activeLayer];
        if (v) {
            v.setYaw(v.getYaw() + delta);
        }
    });

    // Toggle map size
    document.getElementById('btn-toggle-map').addEventListener('click', () => {
        const panel = document.getElementById('map-panel');
        panel.classList.toggle('expanded');
        setTimeout(() => map.invalidateSize(), 350);
    });
}

// ─── Boot ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);
