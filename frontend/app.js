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

// Train type → colour mapping (by code prefix)
const TRAIN_TYPE_COLORS = {
    G: '#d33c1f',   // 高速动车组 / 高铁
    D: '#2563eb',   // 动车组
    C: '#0891b2',   // 城际列车
    Z: '#7c3aed',   // 直达特快
    T: '#ea580c',   // 特快
    K: '#16a34a',   // 快速
};
const TRAIN_TYPE_OTHER = '#6b7280';  // 普通旅客列车等
const TRAIN_TYPE_LEGEND = [
    { prefix: 'G', label: 'G — 高速动车组 (高铁)', color: TRAIN_TYPE_COLORS.G },
    { prefix: 'D', label: 'D — 动车组',           color: TRAIN_TYPE_COLORS.D },
    { prefix: 'C', label: 'C — 城际列车',         color: TRAIN_TYPE_COLORS.C },
    { prefix: 'Z', label: 'Z — 直达特快',         color: TRAIN_TYPE_COLORS.Z },
    { prefix: 'T', label: 'T — 特快',             color: TRAIN_TYPE_COLORS.T },
    { prefix: 'K', label: 'K — 快速',             color: TRAIN_TYPE_COLORS.K },
    { prefix: '',  label: '其他号码',             color: TRAIN_TYPE_OTHER },
];

// The first stop of every reachable route is 芜湖 itself. Visually we want
// the polyline to emerge from clearly outside the hub marker, so we push
// the first vertex HUB_PX_OFFSET pixels outward (in the current zoom's
// screen-pixel space) along the line's direction. The offset is a screen-
// pixel distance, NOT a fixed lat/lng distance, so it has to be re-applied
// every time the zoom changes — otherwise zooming out would shrink the
// visible gap and the hub would swallow the line head.
const HUB_PX_OFFSET = 2;

const DEBOUNCE_MS = 1000;

// Tick labels shown under the slider, in minutes. The slider's native
// thumb position is rendered linearly (value/max), but the tick labels are
// laid out with `space-between` so they're equally spaced visually. To make
// them match, the slider value is mapped to minutes through a piecewise-
// linear function anchored on these tick positions. Net effect: the "60" tick
// sits at slider thumb position 8.3% (1/12) of the way along, "120" at 16.7%
// (2/12), and so on — and dragging the thumb to that position produces
// exactly 60 / 120 / ... minutes of reachability, not a different value.
const TICKS = [0, 60, 120, 240, 360, 480, 600, 720, 960, 1200, 1440, 1920, 2428];
const SLIDER_MAX = 1000;

function posToMin(pos) {
    // Map [0, SLIDER_MAX] → [0, len-1] segments, then linearly interpolate
    // within the segment so the endpoints land exactly on TICKS values.
    pos = Math.max(0, Math.min(SLIDER_MAX, pos));
    const p = pos / SLIDER_MAX * (TICKS.length - 1);
    const i = Math.min(Math.floor(p), TICKS.length - 2);
    const t = p - i;
    return Math.round(TICKS[i] + (TICKS[i + 1] - TICKS[i]) * t);
}

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
    // Push the attribution to bottom-left so the legend (bottom-right) has room
    map.attributionControl.setPosition('bottomleft');

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
        deselectRoute();
    });

    // Keep hub-to-line gap constant across zoom changes. Without this, a
    // polyline drawn at zoom 11 would render with a 1.4 px gap at zoom 7
    // and the hub would swallow the line head.
    map.on('zoomend', recomputeRouteOffsets);

    // Legend in top-left
    initLegend();
}

async function loadData() {
    showLoading(true);
    try {
        const resp = await fetch('data/reach.json');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        fullData = await resp.json();
        const slider = document.getElementById('time-slider');
        slider.max = SLIDER_MAX;
        slider.value = 0;
        renderDataSubtitle(fullData);
    } catch (e) {
        document.getElementById('status-time').textContent = '数据加载失败';
        document.getElementById('data-subtitle').textContent = '';
        console.error(e);
    } finally {
        showLoading(false);
    }
}

// Show "数据更新于 YYYY-MM-DD HH:MM" under the title. Falls back gracefully
// if the field isn't in the JSON yet (e.g. an older reach.json cached at the
// edge).
function renderDataSubtitle(data) {
    const el = document.getElementById('data-subtitle');
    if (!el) return;
    const ts = data?.last_updated;
    if (!ts) {
        el.textContent = '';
        return;
    }
    // Accept "2026-06-26 02:04:49" (sqlite meta value) or ISO 8601.
    const m = String(ts).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (!m) {
        el.textContent = `数据更新于 ${ts}`;
        return;
    }
    el.textContent = `数据更新于 ${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
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

    // Note: the map's initial view is set by updateVisualization(0) at the
    // bottom of init() — that path calls fitMapToView, which centres on the
    // hub at maxZoom. We deliberately do NOT fitBounds to all of China
    // here; the user's brief is to start tight on 芜湖 and let the view
    // expand as the slider increases.
}

function makeStationMarker(s, isReachable) {
    const bg = isReachable ? trainTypeColor(s.fastest_train_code) : '';
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
        const n = posToMin(parseInt(e.target.value, 10));
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

    // 5. Fit the map to the current reachable set (or to the hub at n=0)
    fitMapToView(n, reachable);

    // 6. Auto-hide loading
    setTimeout(() => showLoading(false), 80);
}

function drawRoute(s) {
    const stops = s.route;
    if (!stops || stops.length < 1) return;

    const hub = [fullData.hub.lat, fullData.hub.lon];
    const latlngs = stops.map((p) => [p.lat, p.lon]);

    // The backend puts 芜湖 at route[0]. Visually offset the hub point
    // outward toward the first non-hub stop so the polyline emerges from
    // clearly outside the hub marker + its box-shadow halo. 2 px is a
    // minimal nudge — it just clears the marker by a couple of pixels so
    // the line head doesn't tuck under the hub. The same offset is re-applied
    // on every zoomend (see recomputeRouteOffsets) so the screen-pixel gap
    // stays constant as the user zooms in or out.
    offsetFirstStopFromHub(latlngs, hub);

    const color = trainTypeColor(s.fastest_train_code);

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

    // Persist the original color so deselect can restore it.
    routeLayerRegistry.set(s.id, { shadow, visible, hit, color });
}

/* ── Hub offset (re-applied on every zoom change) ─────────── */

// Push the first vertex of a polyline outward from the hub so the line
// starts HUB_PX_OFFSET pixels (in current screen space) away from the hub
// marker. Mutates latlngs[0] in place.
function offsetFirstStopFromHub(latlngs, hub) {
    if (latlngs.length < 2) return;
    const hubPx = map.latLngToLayerPoint(hub);
    const secondPx = map.latLngToLayerPoint(latlngs[1]);
    const dx = secondPx.x - hubPx.x;
    const dy = secondPx.y - hubPx.y;
    const len = Math.hypot(dx, dy) || 1;
    const offsetHubPx = L.point(
        hubPx.x + (dx / len) * HUB_PX_OFFSET,
        hubPx.y + (dy / len) * HUB_PX_OFFSET
    );
    latlngs[0] = map.layerPointToLatLng(offsetHubPx);
}

// Re-apply the hub offset to every polyline in the registry. Called on
// zoomend (and after fitMapToView via the same event) so the screen-pixel
// gap between hub and line head stays constant regardless of zoom level.
// Without this, a polyline drawn at zoom 11 with a 22 px gap would render
// as a ~1.4 px gap after fitBounds zoomed out to zoom 7, and the hub would
// swallow the line head.
function recomputeRouteOffsets() {
    if (!fullData || routeLayerRegistry.size === 0) return;
    const hub = [fullData.hub.lat, fullData.hub.lon];
    routeLayerRegistry.forEach((layers) => {
        const latlngs = layers.visible.getLatLngs();
        if (!latlngs || latlngs.length < 2) return;
        offsetFirstStopFromHub(latlngs, hub);
        layers.visible.setLatLngs(latlngs);
        if (layers.shadow) {
            const sLats = layers.shadow.getLatLngs();
            sLats[0] = latlngs[0];
            layers.shadow.setLatLngs(sLats);
        }
        if (layers.hit) {
            const hLats = layers.hit.getLatLngs();
            hLats[0] = latlngs[0];
            layers.hit.setLatLngs(hLats);
        }
    });
}

function trainTypeColor(code) {
    if (!code || code.length === 0) return TRAIN_TYPE_OTHER;
    const prefix = code[0];
    return TRAIN_TYPE_COLORS[prefix] || TRAIN_TYPE_OTHER;
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
// Dimmed state (other routes when something is selected) — greyed out
const ROUTE_STYLE_DIM     = { opacity: 0.15, weight: 3.5, color: '#6b7280' };
const SHADOW_STYLE_DIM    = { opacity: 0.10, weight: 5,   color: '#6b7280' };
// Highlighted state (the selected route) — fatter stroke, keep original train type colour.
// Shadow stays white but light, just enough for a subtle outline halo — at higher
// opacity the white shadow overpowered the lighter train type colours (C cyan,
// K grey, Z purple) and made the line look pure white. Keep shadow weight just
// slightly wider than the visible stroke so only ~1px of white halo shows.
const ROUTE_STYLE_ACTIVE  = { opacity: 1,    weight: 6.5 };
const SHADOW_STYLE_ACTIVE = { opacity: 0.4,  weight: 8,   color: '#ffffff'      };

function selectRoute(station) {
    selectedStation = station;

    // 1. Show detail panel with stops
    renderDetailPanel(station);

    // 2. Apply dim/highlight to map
    const slider = document.getElementById('time-slider');
    const n = posToMin(parseInt(slider.value, 10));
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
            // Selected route: fatter stroke, keep original train type colour
            layers.visible.setStyle({ ...ROUTE_STYLE_ACTIVE, color: layers.color });
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

/* ── Map view fitting ─────────────────────────── */

// View-fitting policy:
//   - n === 0  → reset to the tightest view centred on the hub (maxZoom=11
//                or the configured max), so the user can see the hub as a
//                single point. No routes are drawn at n=0, so this is purely
//                a "start state" view.
//   - n  >  0  → fit bounds to all reachable stations + the hub, with 5% of
//                the map's screen size as padding on every side. That keeps
//                every line fully inside the viewport with a small breathing
//                margin, regardless of how far the routes spread. The centre
//                shifts to whatever the bounds' centre is (may no longer be
//                the hub if the reachable set is asymmetric).
// Stations on the very edge of the reachable set (the furthest ones) define
// the bounds, so even a long single route to e.g. 哈尔滨 will pull the view
// to the north-east and the padding keeps the line endpoint from touching
// the edge.
function fitMapToView(n, reachable) {
    if (!fullData) return;
    const hubLatLng = [fullData.hub.lat, fullData.hub.lon];

    if (n === 0) {
        // Tight hub-centred view at the maximum zoom. No animation — this is
        // the initial state of the app and we want it set immediately.
        map.setView(hubLatLng, map.getMaxZoom(), { animate: false });
        return;
    }

    // Build a bounds object from the hub + every reachable station.
    const bounds = L.latLngBounds([hubLatLng]);
    reachable.forEach((s) => bounds.extend([s.lat, s.lon]));

    // Pad the fit by 5% of the map's current screen size on every side. The
    // shape stays rectangular so we can use Leaflet's symmetric padding.
    const sz = map.getSize();
    const padX = Math.round(sz.x * 0.05);
    const padY = Math.round(sz.y * 0.05);
    map.fitBounds(bounds, {
        padding: L.point(padX, padY),
        animate: true,
        duration: 0.6,
    });
}

/* ── Legend (top-left) ────────────────────────── */

function initLegend() {
    const container = L.DomUtil.create('div', 'train-type-legend');
    container.innerHTML = TRAIN_TYPE_LEGEND.map(({ prefix, label, color }) =>
        `<div class="legend-row">` +
        `<span class="legend-swatch" style="background:${color}"></span>` +
        `<span class="legend-label">${label}</span>` +
        `</div>`
    ).join('');
    // Attach to top-left corner as a Leaflet control
    const LegendControl = L.Control.extend({
        onAdd() { return container; },
    });
    new LegendControl({ position: 'bottomright' }).addTo(map);
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
        // Restore the original per-direction color.
        layers.visible.setStyle({ ...ROUTE_STYLE_DEFAULT, color: layers.color });
        layers.shadow.setStyle({ ...SHADOW_STYLE_DEFAULT, color: '#ffffff' });
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
