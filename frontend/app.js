/* train_crecc — interactive reachability map
 *
 * 1. Fetch /api/reach (Cloudflare Pages Function) — returns the full
 *    static dataset (hub + stations + routes).
 * 2. Render the hub + all stations as dots.
 * 3. On slider change, debounce 1s, then re-render reachable stations
 *    + their route polylines.
 */

const DEBOUNCE_MS = 1000;
const PALETTE = ['#1f6feb', '#ff6b35', '#3fb950', '#a371f7', '#f78166',
                 '#56d4dd', '#d2a8ff', '#ffd166', '#06d6a0', '#ef476f'];

let fullData = null;        // {hub, max_minutes, stations: [...]}
let map = null;
let hubMarker = null;
let stationLayer = null;    // L.layerGroup of dimmed station markers
let reachableLayer = null;  // L.layerGroup of highlighted reachable stations + routes
let sidebarList = null;
let sliderTimeout = null;
let activeTime = 0;
let sidebarCollapsed = false;

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

    // Dark CartoDB basemap (matches our color theme)
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '© OpenStreetMap contributors © CARTO',
        subdomains: 'abcd',
        maxZoom: 19,
    }).addTo(map);

    stationLayer = L.layerGroup().addTo(map);
    reachableLayer = L.layerGroup().addTo(map);
}

async function loadData() {
    showLoading(true);
    try {
        const resp = await fetch('/api/reach');
        if (!resp.ok) throw new Error(`API error: ${resp.status}`);
        fullData = await resp.json();
        // Set slider max from server (capped at 720 for sanity)
        const slider = document.getElementById('time-slider');
        slider.max = Math.min(720, fullData.max_minutes || 720);
    } catch (e) {
        console.error('loadData failed:', e);
        // Fall back to direct JSON (works for local dev)
        try {
            const resp2 = await fetch('data/reach.json');
            if (resp2.ok) {
                fullData = await resp2.json();
                const slider = document.getElementById('time-slider');
                slider.max = Math.min(720, fullData.max_minutes || 720);
            } else {
                document.getElementById('status-time').textContent = '数据加载失败';
                return;
            }
        } catch (e2) {
            document.getElementById('status-time').textContent = '数据加载失败';
            return;
        }
    } finally {
        showLoading(false);
    }
}

function renderAllStations() {
    if (!fullData) return;

    // Hub marker
    const hubIcon = L.divIcon({
        className: 'hub-icon',
        html: '<div class="hub-marker"></div>',
        iconSize: [24, 24],
        iconAnchor: [12, 12],
    });
    hubMarker = L.marker([fullData.hub.lat, fullData.hub.lon], { icon: hubIcon })
        .addTo(map)
        .bindPopup(`<strong>${fullData.hub.name}</strong><br/>数据枢纽`);

    // All station markers (dimmed by default; lit up when reachable)
    fullData.stations.forEach((s) => {
        const icon = L.divIcon({
            className: 'station-icon',
            html: '<div class="station-marker dimmed"></div>',
            iconSize: [10, 10],
            iconAnchor: [5, 5],
        });
        const m = L.marker([s.lat, s.lon], { icon })
            .bindPopup(stationPopupHtml(s));
        m.on('click', () => focusStation(s));
        stationLayer.addLayer(m);
    });

    // Fit map to China bounds
    map.fitBounds([[18, 75], [54, 135]]);
}

function stationPopupHtml(s) {
    return `<strong>${s.name}</strong>${s.city ? ` (${s.city})` : ''}<br/>
        方向: ${s.direction || '?'} · ${s.min_minutes} 分钟起<br/>
        最快车次: ${s.fastest_train_code}<br/>
        经停 ${s.train_count} 趟车`;
}

function setupSlider() {
    const slider = document.getElementById('time-slider');
    slider.addEventListener('input', (e) => {
        const n = parseInt(e.target.value, 10);
        document.getElementById('time-display').textContent = n;
        // Update track fill via CSS
        const pct = (n / parseInt(slider.max, 10)) * 100;
        slider.style.setProperty('--fill', pct + '%');
        clearTimeout(sliderTimeout);
        // Show loading bar immediately
        showLoading(true);
        sliderTimeout = setTimeout(() => {
            updateVisualization(n);
        }, DEBOUNCE_MS);
    });
}

function setupSidebar() {
    sidebarList = document.getElementById('station-list');
    document.getElementById('close-sidebar').addEventListener('click', () => {
        sidebarCollapsed = true;
        document.getElementById('sidebar').classList.add('collapsed');
        document.getElementById('open-sidebar').hidden = false;
    });
    document.getElementById('open-sidebar').addEventListener('click', () => {
        sidebarCollapsed = false;
        document.getElementById('sidebar').classList.remove('collapsed');
        document.getElementById('open-sidebar').hidden = true;
    });
}

function updateVisualization(n) {
    if (!fullData) return;
    activeTime = n;

    // Update status
    const reachable = fullData.stations.filter((s) => s.min_minutes <= n);
    document.getElementById('status-time').textContent = `${n} 分钟内`;
    document.getElementById('status-count').textContent = `${reachable.length} 站可达`;

    // Clear previous reachable layers
    reachableLayer.clearLayers();

    // Dim non-reachable, light up reachable
    stationLayer.eachLayer((m) => {
        // (we don't have direct station info on the layer; toggle via className trick)
    });
    // Re-render markers with proper dim state
    stationLayer.clearLayers();
    fullData.stations.forEach((s) => {
        const isReachable = s.min_minutes <= n;
        const icon = L.divIcon({
            className: 'station-icon',
            html: `<div class="station-marker ${isReachable ? '' : 'dimmed'}" `
                + `style="${isReachable ? `background:${directionColor(s.direction)}` : ''}"></div>`,
            iconSize: [10, 10],
            iconAnchor: [5, 5],
        });
        const m = L.marker([s.lat, s.lon], { icon })
            .bindPopup(stationPopupHtml(s));
        m.on('click', () => focusStation(s));
        stationLayer.addLayer(m);

        if (isReachable) {
            drawRoute(s, n);
        }
    });

    // Sidebar list
    updateSidebar(reachable);

    // Hide loading
    setTimeout(() => showLoading(false), 100);
}

function drawRoute(s, n) {
    const routeStops = s.route;
    if (!routeStops || routeStops.length < 2) return;

    // Polyline from hub to this station via intermediate stops
    const latlngs = routeStops.map((p) => [p.lat, p.lon]);
    const color = directionColor(s.direction);

    // Main line
    L.polyline(latlngs, {
        color,
        weight: 2.5,
        opacity: 0.7,
        smoothFactor: 1,
    }).addTo(reachableLayer);

    // Transparent hit zone for hover (from travel_map double-polyline trick)
    L.polyline(latlngs, {
        color: '#000',
        weight: 14,
        opacity: 0,
        interactive: true,
    }).addTo(reachableLayer)
        .bindTooltip(
            `<strong>${s.name}</strong> · ${s.min_minutes}m · ${s.fastest_train_code}`,
            { sticky: true, direction: 'top', className: 'route-tooltip' }
        );
}

function directionColor(d) {
    if (!d) return PALETTE[0];
    // 8-way cardinal directions → palette index
    const map = { N: 8, NE: 5, E: 0, SE: 7, S: 2, SW: 3, W: 6, NW: 1 };
    return PALETTE[map[d]] || PALETTE[0];
}

function updateSidebar(reachable) {
    sidebarList.innerHTML = '';
    reachable.forEach((s) => {
        const li = document.createElement('li');
        li.innerHTML = `
            <div class="row1">
                <span class="name">${s.name}${s.city ? ` <span style="color:var(--text-dim)">· ${s.city}</span>` : ''}</span>
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
    stationLayer.eachLayer((m) => {
        const ll = m.getLatLng();
        if (Math.abs(ll.lat - s.lat) < 1e-5 && Math.abs(ll.lng - s.lon) < 1e-5) {
            m.openPopup();
        }
    });
}

function showLoading(on) {
    const bar = document.getElementById('loading-bar');
    if (on) {
        bar.classList.add('active', 'indeterminate');
    } else {
        bar.classList.remove('active', 'indeterminate');
    }
}