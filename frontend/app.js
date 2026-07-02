/* train_crecc — interactive reachability map
 *
 * Static-only architecture (no Cloudflare Function needed):
 *   - fetch data/reach.json on page load
 *   - all filtering is client-side (O(1) lookup per station)
 *   - slider debounce 1s → re-render reachable stations + route polylines
 */

const DEBOUNCE_MS = 1000;
const PALETTE = ['#c14a35', '#2b6cb0', '#2c7a51', '#6b46a8', '#b7791f',
                 '#0e7c86', '#a02e6b', '#c05621', '#2d3748', '#9b2c2c'];

let fullData = null;        // {hub, max_minutes, stations: [...]}
let map = null;
let stationLayer = null;    // L.layerGroup of all station markers
let reachableLayer = null;  // L.layerGroup of reachable stations + routes
let sidebarList = null;
let sliderTimeout = null;

document.addEventListener('DOMContentLoaded', init);

async function init() {
    setupSlider();
    setupSidebar();
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
    m.on('click', () => focusStation(s));
    return m;
}

function stationPopupHtml(s) {
    return `<strong>${s.name}</strong>${s.city ? ` (${s.city})` : ''}<br/>
        方向: ${s.direction || '?'} · ${s.min_minutes} 分钟起<br/>
        最快车次: ${s.fastest_train_code}<br/>
        经停 ${s.train_count} 趟车`;
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

/* ── Core update: this fires after each debounced slider stop ── */

function updateVisualization(n) {
    if (!fullData) return;

    const reachable = fullData.stations.filter((s) => s.min_minutes <= n);

    document.getElementById('status-time').textContent = `${n} 分钟内`;
    document.getElementById('status-count').textContent = `${reachable.length} 站可达`;

    // 1. Rebuild station markers (dimmed/lit)
    stationLayer.clearLayers();
    fullData.stations.forEach((s) => {
        stationLayer.addLayer(makeStationMarker(s, s.min_minutes <= n));
    });

    // 2. Draw reachable routes
    reachableLayer.clearLayers();
    reachable.forEach((s) => drawRoute(s));

    // 3. Sidebar
    updateSidebar(reachable);

    // 4. Auto-hide loading
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
    L.polyline(latlngs, {
        color: '#ffffff',
        weight: 5,
        opacity: 0.85,
        smoothFactor: 1,
    }).addTo(reachableLayer);
    L.polyline(latlngs, {
        color,
        weight: 3.5,
        opacity: 1,
        smoothFactor: 1,
    }).addTo(reachableLayer);

    // Fat invisible hit zone for tooltip on hover
    L.polyline(latlngs, {
        color: '#000',
        weight: 14,
        opacity: 0,
        interactive: true,
    }).addTo(reachableLayer)
        .bindTooltip(
            `<strong>${s.name}</strong> · ${s.min_minutes}m · ${s.fastest_train_code}`,
            { sticky: true, direction: 'top' }
        );
}

function directionColor(d) {
    const m = { N: 8, NE: 5, E: 0, SE: 7, S: 2, SW: 3, W: 6, NW: 1 };
    return PALETTE[m[d]] || PALETTE[0];
}

function updateSidebar(reachable) {
    sidebarList.innerHTML = '';
    reachable.forEach((s) => {
        const li = document.createElement('li');
        li.innerHTML = `
            <div class="row1">
                <span class="name">${s.name}${s.city ? ` <span class="city-hint">· ${s.city}</span>` : ''}</span>
                <span class="min">${s.min_minutes}m</span>
            </div>
            <div class="row2">${s.direction || '?'} · ${s.fastest_train_code} · ${s.train_count} 趟</div>
        `;
        li.addEventListener('click', () => focusStation(s));
        sidebarList.appendChild(li);
    });
}

function focusStation(s) {
    map.flyTo([s.lat, s.lon], 8, { duration: 0.8 });
    // Try to open popup (match by coords on the station layer)
    stationLayer.eachLayer((m) => {
        const ll = m.getLatLng();
        if (Math.abs(ll.lat - s.lat) < 1e-5 && Math.abs(ll.lng - s.lon) < 1e-5) {
            setTimeout(() => m.openPopup(), 100);
        }
    });
}

function showLoading(on) {
    const bar = document.getElementById('loading-bar');
    bar.classList.toggle('active', on);
    bar.classList.toggle('indeterminate', on);
}
