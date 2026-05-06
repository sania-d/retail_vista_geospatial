"""Utility functions for performing Google Places text searches restricted to H3 grid cells."""

from typing import Any
import os
import json
import logging
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from google.cloud import bigquery
from google import genai
import h3
try:
    import shapely.geometry as sg
    SPATIAL_LIBS_AVAILABLE = True
except ImportError:
    SPATIAL_LIBS_AVAILABLE = False
    class sg:
        class Polygon:
            pass
        class Point:
            pass

load_dotenv()


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID", "fynd-jio-ccp-non-prod")
DATASET_ID = os.getenv("DATASET_ID", "RetailVista_1")
MAPS_API_KEY = os.getenv("MAPS_API_KEY")


def create_polygon_shapely(
    lat: float, lon: float, radius_meters: float, resolution: int = 6
) -> Any:
    """
    Creates a circular polygon buffer around a point and returns it as a Shapely Polygon.
    """
    if not SPATIAL_LIBS_AVAILABLE:
        return {"center_lat": lat, "center_lon": lon, "radius_meters": radius_meters}

    import geopandas as gpd
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[sg.Point(lon, lat)], crs="EPSG:4326")
    gdf_buffered = gdf.to_crs(epsg=3857).buffer(radius_meters, resolution=resolution)
    return gdf_buffered.to_crs(epsg=4326).geometry.values[0]


def get_h3_distributed_points_for_polygon(poly: Any, resolution=7):
    """
    Generates H3 cell centers that fall within a given polygon.
    """
    if not SPATIAL_LIBS_AVAILABLE or isinstance(poly, dict):
        center_lat = poly["center_lat"] if isinstance(poly, dict) else 19.0
        center_lon = poly["center_lon"] if isinstance(poly, dict) else 72.0
        radius_meters = poly["radius_meters"] if isinstance(poly, dict) else 5000.0

        center_hex = h3.latlng_to_cell(center_lat, center_lon, resolution) if hasattr(h3, "latlng_to_cell") else h3.geo_to_h3(center_lat, center_lon, resolution)
        spacing = 2400.0 if resolution == 7 else 800.0
        k = max(1, int(radius_meters / spacing))

        cells = h3.grid_disk(center_hex, k) if hasattr(h3, "grid_disk") else h3.k_ring(center_hex, k)

        all_results = []
        for cell in cells:
            c_lat, c_lon = h3.cell_to_latlng(cell) if hasattr(h3, "cell_to_latlng") else h3.h3_to_geo(cell)
            all_results.append((c_lat, c_lon, cell))
        return all_results

    if poly.geom_type == "MultiPolygon":
        polys = poly.geoms
    else:
        polys = [poly]

    all_hexagons = []
    seen = set()
    for p in polys:
        outer = [(lat, lon) for lon, lat in p.exterior.coords]
        try:
            h3_poly = h3.LatLngPoly(outer)
            cells = h3.polygon_to_cells(h3_poly, resolution)
        except AttributeError:
            cells = h3.polyfill(
                {
                    "type": "Polygon",
                    "coordinates": [[(lon, lat) for lon, lat in p.exterior.coords]],
                },
                resolution,
            )
        for cell in cells:
            if cell not in seen:
                seen.add(cell)
                all_hexagons.append(cell)

    if not all_hexagons:
        return []

    # Convert all hexes to points at once
    coords = [
        h3.cell_to_latlng(h) if hasattr(h3, "cell_to_latlng") else h3.h3_to_geo(h)
        for h in all_hexagons
    ]
    lats, lons = zip(*coords)

    import geopandas as gpd

    # Create GeoDataFrame
    gdf_points = gpd.GeoDataFrame(
        {"hex_id": all_hexagons, "lat": lats, "lon": lons},
        geometry=[sg.Point(lon, lat) for lat, lon in coords],
        crs="EPSG:4326",
    )

    # Filter using vectorized 'within'
    gdf_filtered = gdf_points[gdf_points.geometry.within(poly)]

    return list(zip(gdf_filtered["lat"], gdf_filtered["lon"], gdf_filtered["hex_id"]))


def is_hex_inhabited(hex_id: str) -> str:
    """
    Verifies if an H3 hex cell is inhabited based on building density in BigQuery.

    Args:
        hex_id: The H3 cell ID.

    Returns:
        "inhabited" if building count > 0, else "uninhabited".
    """
    client = bigquery.Client(project=PROJECT_ID)
    table_id = f"{PROJECT_ID}.{DATASET_ID}.ghs_obat_building_density_india_master"

    # Get boundary of hex
    boundary = (
        h3.cell_to_boundary(hex_id)
        if hasattr(h3, "cell_to_boundary")
        else h3.h3_to_geo_boundary(hex_id)
    )

    # Convert to Shapely Polygon and then WKT
    # H3 returns (lat, lon), Shapely expects (lon, lat)
    poly = sg.Polygon([(lon, lat) for lat, lon in boundary])
    wkt_str = poly.wkt

    query = f"""
    SELECT COUNT(*) as cnt 
    FROM `{table_id}`
    WHERE ST_WITHIN(geo, ST_GEOGFROMTEXT(@wkt))
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("wkt", "STRING", wkt_str),
        ]
    )

    try:
        query_job = client.query(query, job_config=job_config)
        res = list(query_job.result())
        cnt = res[0]["cnt"] if res else 0
        if cnt > 0:
            return "inhabited"
    except Exception as e:
        logger.error("BQ inhabitant check failed for hex %s: %s", hex_id, e)
    return "uninhabited"


def text_search_for_hex(
    hex_id: str,
    search_queries: list[str],
    included_type: str = None,
    page_size: int = 10,
) -> list[dict]:
    """
    Performs Google Places Text Search restricted to the bounding box of an H3
    hex for multiple queries in parallel.

    Args:
        hex_id: The H3 cell ID.
        search_queries: A list of text queries to search for.
        included_type: Optional primary place type filter (e.g., 'electronics_store').
        page_size: Maximum number of results to return per page (default 10).

    Returns:
        A list of dictionaries containing found places.
    """
    if not MAPS_API_KEY:
        logger.error("MAPS_API_KEY missing.")
        return []

    # Get boundary of hex
    boundary = (
        h3.cell_to_boundary(hex_id)
        if hasattr(h3, "cell_to_boundary")
        else h3.h3_to_geo_boundary(hex_id)
    )

    # Find bounding box
    lats = [lat for lat, lon in boundary]
    lons = [lon for lat, lon in boundary]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": MAPS_API_KEY,
        "X-Goog-FieldMask": (
            "places.displayName,places.location,places.formattedAddress,"
            "places.googleMapsUri,places.id,places.rating"
        ),
    }

    url = "https://places.googleapis.com/v1/places:searchText"

    all_results = []
    seen_places = set()

    def run_query(query):
        payload = {
            "textQuery": query,
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": min_lat, "longitude": min_lon},
                    "high": {"latitude": max_lat, "longitude": max_lon},
                }
            },
            "pageSize": page_size,
        }
        if included_type:
            payload["includedType"] = included_type

        local_results = []
        try:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                places = response.json().get("places", [])
                logger.info(
                    "TextSearch for hex %s query '%s' returned %s results (pageSize=%s)",
                    hex_id,
                    query,
                    len(places),
                    page_size,
                )
                for p in places:
                    loc = p.get("location", {})
                    local_results.append(
                        {
                            "name": p.get("displayName", {}).get("text", "Unknown"),
                            "address": p.get("formattedAddress", "Unknown"),
                            "lat": loc.get("latitude"),
                            "lng": loc.get("longitude"),
                            "google_maps_url": p.get("googleMapsUri", ""),
                            "place_id": p.get("id", "text_search"),
                            "rating": p.get("rating", 0.0),
                        }
                    )
            else:
                logger.error(
                    "TextSearch failed for hex %s query '%s': %s",
                    hex_id,
                    query,
                    response.status_code,
                )
        except Exception as e:
            logger.error("TextSearch error for hex %s query '%s': %s", hex_id, query, e)

        return local_results

    # Run queries in parallel
    with ThreadPoolExecutor(max_workers=min(len(search_queries), 5)) as executor:
        futures = [executor.submit(run_query, q) for q in search_queries]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            for r in res:
                pid = r.get("place_id", r["name"])
                if pid not in seen_places:
                    seen_places.add(pid)
                    all_results.append(r)

    return all_results


def search_places_in_polygon(
    poly: sg.Polygon,
    search_queries: list[str] = None,
    gemini_prompt: str = None,
    output_res: int = 8,
    page_size: int = 10,
) -> list[dict]:
    """
    Orchestrates a parallel search for places within a polygon using H3 grid
    and optional Gemini grounding.

    Args:
        poly: A shapely.geometry.Polygon object.
        search_queries: A list of text queries to search for (used if gemini_prompt is None).
        gemini_prompt: Optional prompt for Gemini to generate search queries using Maps grounding.
        output_res: Output H3 resolution level.
        page_size: Maximum number of results to return per page (default 10).

    Returns:
        A list of dictionaries containing found places.
    """
    if not MAPS_API_KEY:
        logger.error("MAPS_API_KEY missing.")
        return []
    gemini_output = None
    included_type = ""
    # 1. Generate H3 hexes
    resolution = min(7, output_res)
    raw_hex_points = get_h3_distributed_points_for_polygon(poly, resolution=resolution)
    if not raw_hex_points:
        logger.warning("No H3 hexes generated for the polygon.")
        return []

    # 2. Handle Gemini Call if prompt provided
    # queries = search_queries or []
    if gemini_prompt:
        try:
            from google.genai import types

            client = genai.Client(
                vertexai=True, project=os.getenv("GOOGLE_CLOUD_PROJECT", PROJECT_ID), location="us-central1"
            )

            logger.info("Calling Gemini with Google Search grounding for queries...")
            search_tool = types.Tool(google_search=types.GoogleSearch())
            response = client.models.generate_content(
                model="gemini-2.5-pro",
                contents=gemini_prompt,
                config=types.GenerateContentConfig(tools=[search_tool], temperature=0),
            )
            text_output = response.text.strip()
            # Expecting a JSON array of strings
            if text_output.startswith("```json"):
                text_output = text_output[7:]
            elif text_output.startswith("```"):
                text_output = text_output[3:]
            if text_output.endswith("```"):
                text_output = text_output[:-3]
            text_output = text_output.strip()

            gemini_output = json.loads(text_output)
            logger.info("Gemini generated gemini_output: %s", gemini_output)
        except Exception as e:
            logger.error("Gemini grounding failed: %s", e)
            if not gemini_output:
                return []

        if not gemini_output:
            logger.warning("No search gemini_output available.")
            return []

        if "search_queries" in gemini_output:
            queries = gemini_output["search_queries"]
            if "included_type" in gemini_output:
                included_type = gemini_output["included_type"]
        else:
            queries = gemini_output
    else:
        queries = search_queries or []
    # 3. Flattened Execution
    # Generate tasks: (hex_id, query)
    tasks = []
    for lat, lon, hex_id in raw_hex_points:
        for q in queries:
            tasks.append((hex_id, q))

    logger.info(
        "Generated %s search tasks (%s hexes x %s queries).",
        len(tasks),
        len(raw_hex_points),
        len(queries),
    )

    all_results = []
    seen_places = set()

    # Worker function for a single task
    def execute_task(task):
        hex_id, query = task

        if isinstance(query, dict):
            query_str = list(query.keys())[0]
            q_type = query[query_str]
        else:
            query_str = query
            q_type = (
                queries.get(query, "unknown")
                if isinstance(queries, dict)
                else "unknown"
            )

        results = text_search_for_hex(
            hex_id, [query_str], included_type=included_type, page_size=page_size
        )

        for r in results:
            r["type"] = q_type
            # Compute hex_id at desired output resolution using place coordinates
            if output_res == resolution:
                r["hex_id"] = hex_id
            elif hasattr(h3, "latlng_to_cell"):
                r["hex_id"] = h3.latlng_to_cell(r["lat"], r["lng"], output_res)
            else:
                r["hex_id"] = h3.geo_to_h3(r["lat"], r["lng"], output_res)

        return results

    # Run all tasks in a single pool
    max_workers = min(len(tasks), 20)

    logger.info("Running tasks with %s workers...", max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(execute_task, t) for t in tasks]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            for r in res:
                pid = r.get("place_id", r["name"])
                if pid not in seen_places:
                    seen_places.add(pid)
                    all_results.append(r)

    # 4. Filter results to keep only those inside the polygon
    filtered_results = []
    for r in all_results:
        if not SPATIAL_LIBS_AVAILABLE or isinstance(poly, dict):
            import math
            lat1, lon1 = poly["center_lat"], poly["center_lon"]
            lat2, lon2 = r["lat"], r["lng"]
            R = 6371000.0  # Earth radius in meters
            d_lat = math.radians(lat2 - lat1)
            d_lon = math.radians(lon2 - lon1)
            a = (
                math.sin(d_lat / 2) ** 2
                + math.cos(math.radians(lat1))
                * math.cos(math.radians(lat2))
                * math.sin(d_lon / 2) ** 2
            )
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            distance = R * c
            if distance <= poly["radius_meters"]:
                filtered_results.append(r)
        else:
            if poly.intersects(sg.Point(r["lng"], r["lat"])):
                filtered_results.append(r)

    logger.info(
        "Found %s raw results, %s inside polygon.",
        len(all_results),
        len(filtered_results),
    )
    return filtered_results
