/* train_crecc — interactive reachability map
 *
 * Static-only architecture (no Cloudflare Function needed):
 *   - fetch data/reach.json on page load
 *   - all filtering is client-side (O(1) lookup per station)
 *   - slider debounce 1s → re-render reachable stations + route polylines
 *
 * Selection state:
 *   - clicking a station/sidebar item/polyline → selectRoute(station)
 *   - selected route is highlighted; other routes dimmed
 *   - detail panel pops up showing full stops list
 *   - ESC / blank-map click / close button → deselect
 */

const DEBOUNCE_MS = 1000;
const PALETTE = ['#c14a35', '#2b6cb0', '#2c7a51', '#6b46a8', '#b7791f',
                 '#0e7c86', '#a02e2e', '#c05621', '#2d3748', '#9b2c2c'];

let fullData = null;        // {hub, max_minutes, stations: [...]}
let map = null;
let stationLayer = null;    // L.layerGroup of all station markers
let reachableLayer = null;  // L.layerGroup of reachable stations + routes
let sidebarList = null;
let sliderTimeout = null;

// Selection state
let selectedStation = null;     // station object
let routeLayerRegistry = new Map();   // station_id → array of polylines (shadow + visible + hit)
let markerRegistry = new Map();       // station_id → L.marker

document.addEventListener('DOMContentLoaded', init);

async function init() {
    setupSlider();
    setupSidebar();
    setupDetailPanel();
    initMap();
    await loadData();
    renderAllStations();
    updateVisualization(0);
}

function initMap() {
    map = L.map('map', {
        center: [31.35, 118.39],
        zoom: 7,
        minZoom: 4,
        maxZoom: 11,
        zoomControl: true,
        attributionControl: true,
    });

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors',
        subdomains: 'abc',
        maxZoom: 19,
    }).addTo(map);

    // Groups (order matters — routes behind markers)
    L.layerGroup().addTo(map);           // placeholder for routes behind
    reachableLayer = L.layerGroup().addTo(map);
    stationLayer  = L.layerGroup().addTo(map);

    // Click on empty map → deselect
    map.on('click', (e) => {
        // Only deselect when clicking the map background, not a marker/polyline
        // (those handle their own click → selectRoute, and stop propagation
        // is implicit via L.DomEvent)
        deselectRoute();
    });
}

async function loadData() {
    showLoading(true);
    try {
        const resp = await fetch('data/reach.json');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        fullData = await resp.json();
        const slider = document.getElementById('time-slider');
        slider.max = Math.min(2428, fullData.max_minutes || 2428);
        slider.value = 0;
    } catch (e) {
        document.getElementById('status-time').textContent = '数据加载失败';
        console.error(e);
    } finally {
        showLoading(false);
    }
}

function renderAllStations() {
    if (!fullData) return;

    // Hub marker — small orange pulsing dot
    const hubIcon = L.divIcon({
        className: 'hub-icon',
        html: '<div class="hub-marker"></div>',
        iconSize: [16, 16],
        iconAnchor: [8, 8],
    });
    L.marker([fullData.hub.lat, fullData.hub.lon], { icon: hubIcon, zIndexOffset: 9999, interactive: false })
        .addTo(map)
        .bindPopup(`<strong>${fullData.hub.name}</strong><br/>数据枢纽`);

    // All station markers (initially dimmed)
    fullData.stations.forEach((s) => {
        const m = makeStationMarker(s, false);
        stationLayer.addLayer(m);
        markerRegistry.set(s.id, m);
    });

    map.fitBounds([[18, 75], [54, 135]]);
}

function makeStationMarker(s, isReachable) {
    const bg = isReachable ? directionColor(s.direction) : '';
    const cls = isReachable ? '' : 'dimmed';
    const icon = L.divIcon({
        className: 'station-icon',
        html: `<div class="station-marker ${cls}" ${bg ? `style="background:${bg}"` : ''}></div>`,
        iconSize: [10, 10],
        iconAnchor: [5, 5],
    });
    const m = L.marker([s.lat, s.lon], { icon, zIndexOffset: isReachable ? 500 : 100 })
        .bindPopup(stationPopupHtml(s));
    m.on('click', (e) => {
        L.DomEvent.stopPropagation(e);
        focusStation(s);
        selectRoute(s);
    });
    return m;
}

function stationPopupHtml(s) {
    return `<strong>${s.name}</strong>${s.city ? ` (${s.city})` : ''}<br/>
        方向: ${s.direction || '?'} · ${s.min_minutes} 分钟起<br/>
        最快车次: ${s.fastest_train_code}<br/>
        经停 ${s.train_count} 趟车<br/>
        <em style="color:var(--text-dim);font-size:11px;">点击查看完整经停 →</em>`;
}

/* ── Slider ──────────────────────────────────── */

function setupSlider() {
    const slider = document.getElementById('time-slider');
    slider.addEventListener('input', (e) => {
        const n = parseInt(e.target.value, 10);
        document.getElementById('time-display').textContent = n;
        clearTimeout(sliderTimeout);
        showLoading(true);
        sliderTimeout = setTimeout(() => {
            updateVisualization(n);
        }, DEBOUNCE_MS);
    });
}

function setupSidebar() {
    sidebarList = document.getElementById('station-list');
    document.getElementById('close-sidebar').addEventListener('click', () => {
        document.getElementById('sidebar').classList.add('collapsed');
        document.getElementById('open-sidebar').hidden = false;
    });
    document.getElementById('open-sidebar').addEventListener('click', () => {
        document.getElementById('sidebar').classList.remove('collapsed');
        document.getElementById('open-sidebar').hidden = true;
    });
}

function setupDetailPanel() {
    document.getElementById('close-detail').addEventListener('click', (e) => {
        e.stopPropagation();
        deselectRoute();
    });
    // ESC to deselect
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && selectedStation) {
            deselectRoute();
        }
    });
}

/* ── Core update: this fires after each debounced slider stop ── */

function updateVisualization(n) {
    if (!fullData) return;

    const reachable = fullData.stations.filter((s) => s.min_minutes <= n);

    document.getElementById('status-time').textContent = `${n} 分钟内`;
    document.getElementById('status-count').textContent = `${reachable.length} 站可达`;

    // 1. Rebuild station markers (dimmed/lit)
    stationLayer.clearLayers();
    markerRegistry.clear();
    fullData.stations.forEach((s) => {
        const m = makeStationMarker(s, s.min_minutes <= n);
        stationLayer.addLayer(m);
        markerRegistry.set(s.id, m);
    });

    // 2. Draw reachable routes
    reachableLayer.clearLayers();
    routeLayerRegistry.clear();
    reachable.forEach((s) => drawRoute(s));

    // 3. Sidebar
    updateSidebar(reachable);

    // 4. Re-apply selection state (in case slider moved while selected)
    if (selectedStation) {
        applySelectionState(n);
    }

    // 5. Auto-hide loading
    setTimeout(() => showLoading(false), 80);
}

function drawRoute(s) {
    const stops = s.route;
    if (!stops || stops.length < 1) return;

    const hub = [fullData.hub.lat, fullData.hub.lon];
    const latlngs = stops.map((p) => [p.lat, p.lon]);

    // The backend puts 芜湖 at route[0]. Visually offset the hub point
    // outward (12 px toward the first non-hub stop) so the polyline
    // emerges from clearly outside the 16px hub marker.
    if (latlngs.length >= 2) {
        const hubPx = map.latLngToLayerPoint(hub);
        const secondPx = map.latLngToLayerPoint(latlngs[1]);
        const dx = secondPx.x - hubPx.x;
        const dy = secondPx.y - hubPx.y;
        const len = Math.hypot(dx, dy) || 1;
        const offsetHubPx = L.point(
            hubPx.x + (dx / len) * 12,
            hubPx.y + (dy / len) * 12
        );
        latlngs[0] = map.layerPointToLatLng(offsetHubPx);
    }

    const color = directionColor(s.direction);

    // Visible polyline (with subtle white shadow underneath for contrast)
    const shadow = L.polyline(latlngs, {
        color: '#ffffff',
        weight: 5,
        opacity: 0.85,
        smoothFactor: 1,
        className: 'route-shadow',
    }).addTo(reachableLayer);

    const visible = L.polyline(latlngs, {
        color,
        weight: 3.5,
        opacity: 1,
        smoothFactor: 1,
    }).addTo(reachableLayer);

    // Fat invisible hit zone for tooltip on hover + click selection
    const hit = L.polyline(latlngs, {
        color: '#000',
        weight: 14,
        opacity: 0,
        interactive: true,
        className: 'route-hit',
    }).addTo(reachableLayer);

    hit.bindTooltip(
        `<strong>${s.name}</strong> · ${s.min_minutes}m · ${s.fastest_train_code}`,
        { sticky: true, direction: 'top' }
    );
    hit.on('click', (e) => {
        L.DomEvent.stopPropagation(e);
        selectRoute(s);
    });

    routeLayerRegistry.set(s.id, { shadow, visible, hit });
}

function directionColor(d) {
    const m = { N: 8, NE: 5, E: 0, SE: 7, S: 2, SW: 3, W: 6, NW: 1 };
    return PALETTE[m[d]] || PALETTE[0];
}

function updateSidebar(reachable) {
    sidebarList.innerHTML = '';
    reachable.forEach((s) => {
        const li = document.createElement('li');
        li.dataset.stationId = s.id;
        li.innerHTML = `
            <div class="row1">
                <span class="name">${s.name}${s.city ? ` <span class="city-hint">· ${s.city}</span>` : ''}</span>
                <span class="min">${s.min_minutes}m</span>
            </div>
            <div class="row2">${s.direction || '?'} · ${s.fastest_train_code} · ${s.train_count} 趟</div>
        `;
        li.addEventListener('click', () => {
            selectRoute(s);
            focusStation(s);
        });
        sidebarList.appendChild(li);
    });
    // Re-mark the selected item
    if (selectedStation) {
        const item = sidebarList.querySelector(`[data-station-id="${selectedStation.id}"]`);
        if (item) item.classList.add('selected');
    }
}

function focusStation(s) {
    map.flyTo([s.lat, s.lon], 8, { duration: 0.8 });
    const m = markerRegistry.get(s.id);
    if (m) setTimeout(() => m.openPopup(), 400);
}

/* ── Selection / detail panel ────────────────── */

// Default visual state for a non-selected reachable route
const ROUTE_STYLE_DEFAULT = { opacity: 1,    weight: 3.5 };
const SHADOW_STYLE_DEFAULT = { opacity: 0.85, weight: 5   };
// Dimmed state (other routes when something is selected)
const ROUTE_STYLE_DIM     = { opacity: 0.12, weight: 3.5 };
const SHADOW_STYLE_DIM    = { opacity: 0.10, weight: 5   };
// Highlighted state (the selected route)
const ROUTE_STYLE_ACTIVE  = { opacity: 1,    weight: 5   };
const SHADOW_STYLE_ACTIVE = { opacity: 0.95, weight: 7   };

function selectRoute(station) {
    selectedStation = station;

    // 1. Show detail panel with stops
    renderDetailPanel(station);

    // 2. Apply dim/highlight to map
    const slider = document.getElementById('time-slider');
    const n = parseInt(slider.value, 10);
    applySelectionState(n);
}

function applySelectionState(n) {
    if (!selectedStation) return;

    document.body.classList.add('route-selected');

    // Apply dim/highlight to every route in the registry
    const selectedId = selectedStation.id;
    routeLayerRegistry.forEach((layers, stationId) => {
        const isSelected = stationId === selectedId;
        if (isSelected) {
            layers.visible.setStyle(ROUTE_STYLE_ACTIVE);
            layers.shadow.setStyle(SHADOW_STYLE_ACTIVE);
            // bring selected to front so it sits on top of dimmed siblings
            layers.visible.bringToFront();
            layers.shadow.bringToFront();
        } else {
            layers.visible.setStyle(ROUTE_STYLE_DIM);
            layers.shadow.setStyle(SHADOW_STYLE_DIM);
        }
    });

    // Dim all station markers except the endpoints of the selected route
    const highlightIds = new Set([selectedId]);
    if (selectedStation.route) {
        selectedStation.route.forEach((stop) => {
            // Find station id by lat/lon match
            const match = fullData.stations.find(
                (s) => Math.abs(s.lat - stop.lat) < 1e-5 && Math.abs(s.lon - stop.lon) < 1e-5
            );
            if (match) highlightIds.add(match.id);
        });
    }
    markerRegistry.forEach((marker, stationId) => {
        const isHighlighted = highlightIds.has(stationId);
        const el = marker.getElement();
        if (!el) return;
        const dot = el.querySelector('.station-marker');
        if (!dot) return;
        if (isHighlighted) {
            dot.classList.remove('marker-other');
        } else {
            dot.classList.add('marker-other');
        }
    });

    // Update sidebar selection marker
    document.querySelectorAll('#station-list li').forEach((li) => {
        li.classList.toggle('selected', parseInt(li.dataset.stationId, 10) === selectedId);
    });
}

function renderDetailPanel(station, n) {
    document.getElementById('detail-name').textContent =
        station.name + (station.city ? ` · ${station.city}` : '');
    document.getElementById('detail-meta').innerHTML =
        `方向 <strong>${station.direction || '?'}</strong> · ` +
        `最快 <strong>${station.min_minutes}</strong> 分钟 · ` +
        `车次 <strong>${station.fastest_train_code}</strong> · ` +
        `共 <strong>${station.train_count}</strong> 趟车`;

    const stops = station.route || [];
    const list = document.getElementById('detail-stops');
    list.innerHTML = '';
    stops.forEach((stop, idx) => {
        const li = document.createElement('li');
        if (idx === 0) li.classList.add('hub');
        li.innerHTML = `
            <span class="stop-name">${stop.name}</span>
            <span class="stop-time">${formatRunMin(stop.run_min)}</span>
        `;
        list.appendChild(li);
    });

    const panel = document.getElementById('detail-panel');
    panel.classList.remove('hidden');
}

function formatRunMin(min) {
    if (min === 0) return '起点';
    if (min < 60) return `+${min}m`;
    const h = Math.floor(min / 60);
    const m = min % 60;
    return m === 0 ? `+${h}h` : `+${h}h${m}m`;
}

function deselectRoute() {
    if (!selectedStation) return;
    // Capture the id BEFORE clearing selectedStation so we can un-z-order
    // the previously-highlighted polyline explicitly.
    const prevId = selectedStation.id;
    selectedStation = null;
    document.body.classList.remove('route-selected');
    document.getElementById('detail-panel').classList.add('hidden');

    // Reset ALL route layers to their default state — including the
    // previously-selected one. Without resetting it, its `.leaflet-front`
    // class and SVG z-order would linger and (overlapping with neighbours)
    // make it look "still highlighted" even though opacity/weight match.
    routeLayerRegistry.forEach((layers, stationId) => {
        layers.visible.setStyle(ROUTE_STYLE_DEFAULT);
        layers.shadow.setStyle(SHADOW_STYLE_DEFAULT);
        if (stationId === prevId) {
            // Undo the bringToFront that applySelectionState did on select,
            // so the previously-selected polyline returns to its natural
            // draw order in the overlay pane.
            layers.visible.bringToBack();
            layers.shadow.bringToBack();
        }
    });

    // Reset markers
    markerRegistry.forEach((marker) => {
        const el = marker.getElement();
        if (!el) return;
        const dot = el.querySelector('.station-marker');
        if (dot) dot.classList.remove('marker-other');
    });

    // Reset sidebar selection
    document.querySelectorAll('#station-list li.selected').forEach((li) => li.classList.remove('selected'));
}

function showLoading(on) {
    const bar = document.getElementById('loading-bar');
    bar.classList.toggle('active', on);
    bar.classList.toggle('indeterminate', on);
}