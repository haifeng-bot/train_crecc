/**
 * Cloudflare Pages Function: GET /api/reach
 *
 * Query params:
 *   - min=<int>   filter to stations with min_minutes <= min
 *   - full=1      return the full dataset (no filter) — default behavior
 *                 when min is absent or 0
 *
 * The static reach.json is bundled at build time via Vite/esbuild.
 */
import reachData from '../../frontend/data/reach.json';

export async function onRequestGet(context) {
    const url = new URL(context.request.url);
    const minParam = url.searchParams.get('min');
    const min = minParam ? parseInt(minParam, 10) : 0;

    let stations = reachData.stations;
    if (min > 0) {
        stations = stations.filter((s) => s.min_minutes <= min);
    }

    return new Response(
        JSON.stringify({
            hub: reachData.hub,
            max_minutes: reachData.max_minutes,
            generated_at: reachData.generated_at,
            min: min,
            station_count: stations.length,
            stations: stations,
        }),
        {
            headers: {
                'Content-Type': 'application/json; charset=utf-8',
                'Cache-Control': 'public, max-age=300',
                'Access-Control-Allow-Origin': '*',
            },
        }
    );
}
