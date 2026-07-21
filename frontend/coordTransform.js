/* coordTransform.js — WGS-84 ↔ GCJ-02 coordinate transform
 *
 * China requires all "publicly visible" maps to use the GCJ-02 ("Mars")
 * coordinate system instead of the global WGS-84 standard. The result is a
 * non-uniform offset of ~50–500 m depending on location; markers drawn with
 * raw WGS-84 lat/lon will visibly drift when overlaid on AMap / Tencent /
 * Google-CN tiles.
 *
 * Strategy: keep the database in WGS-84 (the source of truth, matching
 * crecc.com / Nominatim), and convert at render time only. The data file
 * (reach.json) ships as WGS-84; applyTransformStations() runs once after
 * loadData() and mutates the lat/lon in-place on hub, stations, and every
 * route stop. From the renderer's perspective nothing else changes — every
 * existing L.marker / L.polyline call keeps working unchanged.
 *
 * Outside mainland China the offset is meaningless and slightly wrong, so
 * applyTransformStations() falls back to the input coordinates for points
 * outside the rough AOI (lon 72–137.8, lat 0.8–55.8). Domestic data only
 * (Wuhu hub + China stations) so this is belt-and-suspenders.
 *
 * The implementation is the standard 4-step Krasovsky-1940 projection that
 * every Chinese map library uses; see the comments inside transform_lat/_lon
 * for the original sin-term constants. This is the same algorithm that
 * appears in gcoord, coordtransform, and the AMap docs.
 */

(function (root) {
    'use strict';

    const KRASOVSKY_A = 6378240.0;            // semi-major axis (m)
    const KRASOVSKY_EE = 0.00669342162296594323; // first eccentricity squared

    function outOfChina(lon, lat) {
        // Loose bounding box. Rejecting outside-China points keeps the
        // algorithm from applying a non-zero offset to foreign data where
        // the offset is undefined / wrong.
        return lon < 72.004 || lon > 137.8347 || lat < 0.8293 || lat > 55.8271;
    }

    function transformLat(x, y) {
        let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
            + 0.2 * Math.sqrt(Math.abs(x));
        ret += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0;
        ret += ((20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin((y / 3.0) * Math.PI)) * 2.0) / 3.0;
        ret += ((160.0 * Math.sin((y / 12.0) * Math.PI) + 320 * Math.sin((y * Math.PI) / 30.0)) * 2.0) / 3.0;
        return ret;
    }

    function transformLon(x, y) {
        let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
            + 0.1 * Math.sqrt(Math.abs(x));
        ret += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0;
        ret += ((20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin((x / 3.0) * Math.PI)) * 2.0) / 3.0;
        ret += ((150.0 * Math.sin((x / 12.0) * Math.PI) + 300.0 * Math.sin((x / 30.0) * Math.PI)) * 2.0) / 3.0;
        return ret;
    }

    // WGS-84 → GCJ-02. Mutates the input object? No — returns a new object
    // with only lat/lon changed. The station data also has a `route` array
    // of stops; we recurse into that separately because not every consumer
    // wants the same mutation pattern.
    function wgs84ToGcj02(lon, lat) {
        if (outOfChina(lon, lat)) return [lon, lat];
        let dLat = transformLat(lon - 105.0, lat - 35.0);
        let dLon = transformLon(lon - 105.0, lat - 35.0);
        const radLat = (lat / 180.0) * Math.PI;
        let magic = Math.sin(radLat);
        magic = 1 - KRASOVSKY_EE * magic * magic;
        const sqrtMagic = Math.sqrt(magic);
        dLat = (dLat * 180.0) / (((KRASOVSKY_A * (1 - KRASOVSKY_EE)) / (magic * sqrtMagic)) * Math.PI);
        dLon = (dLon * 180.0) / ((KRASOVSKY_A / sqrtMagic) * Math.cos(radLat) * Math.PI);
        return [lon + dLon, lat + dLat];
    }

    // Apply GCJ-02 to a station-like object: mutate lat/lon in place.
    // Returns the same object for chaining. The `route` array (if present)
    // is walked one level deep and each stop's lat/lon is also shifted.
    function transformStation(station) {
        if (!station || typeof station.lat !== 'number' || typeof station.lon !== 'number') {
            return station;
        }
        const [newLon, newLat] = wgs84ToGcj02(station.lon, station.lat);
        station.lat = newLat;
        station.lon = newLon;
        if (Array.isArray(station.route)) {
            station.route.forEach(transformStation);
        }
        return station;
    }

    // Walk the full data tree (hub + stations[*] + their nested route stops)
    // and convert every WGS-84 lat/lon to GCJ-02. Idempotent-ish — running
    // it twice would double-offset, so loadData() guards it with a flag
    // (data.__transformed) to prevent re-conversion on hot reloads.
    function applyTransformStations(data) {
        if (!data || data.__transformed) return data;
        if (data.hub) transformStation(data.hub);
        if (Array.isArray(data.stations)) {
            data.stations.forEach(transformStation);
        }
        data.__transformed = true;
        return data;
    }

    // Expose — `root` is window in browsers. Keeping the IIFE pattern so the
    // names don't leak into the global scope.
    root.CoordTransform = {
        wgs84ToGcj02,
        applyTransformStations,
    };
})(typeof window !== 'undefined' ? window : globalThis);
