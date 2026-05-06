"""Common utility functions and classes for route calculations and address resolution.

This module provides tools for interaction with Google Maps Routing and Geocoding APIs,
as well as BigQuery dataset discovery instruments.
"""

import os
import re
import logging
from typing import List, Dict, Union, Optional, Tuple, Any
from dataclasses import dataclass, asdict
try:
    import geopandas as gpd
    from shapely.geometry import Point
    from shapely.wkt import loads
    SPATIAL_LIBS_AVAILABLE = True
except ImportError:
    SPATIAL_LIBS_AVAILABLE = False
import numpy as np
import requests
from concurrent.futures import ThreadPoolExecutor
import googlemaps
from google.maps import routing_v2
from google.adk.tools import ToolContext
from google.cloud import bigquery
from google.cloud.exceptions import NotFound
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID")
MAPS_API_KEY = os.getenv("MAPS_API_KEY")


@dataclass
class RouteMetrics:
    """Dataclass to store and aggregate route calculation metrics like time and distance."""

    total_points: int = 0
    min_time: float = float("inf")
    max_time: float = 0.0
    avg_time: float = 0.0
    min_distance: float = float("inf")
    max_distance: float = 0.0
    avg_distance: float = 0.0
    p90_time: float = 0.0
    p90_distance: float = 0.0


class RouteCalculator:
    """Wrapper class for Google Maps Routes API for specialized path calculations."""

    def __init__(self, api_key: str):
        self.client = routing_v2.RoutesClient(client_options={"api_key": api_key})
        self.field_mask = "routes.distance_meters,routes.duration"

    def compute_single_route(self, args: Tuple) -> Tuple[float, float]:
        """
        Computes a single route between origin and destination using Google Maps Routes API.

        Args:
            args: A tuple containing (s_lat, s_lon, d_lat, d_lon, user_travel_mode).

        Returns:
            A tuple of (duration_seconds, distance_km).
        """
        s_lat, s_lon, d_lat, d_lon, user_travel_mode = args

        TRAVEL_MODE_MAP = {
            "drive": routing_v2.RouteTravelMode.DRIVE,
            "bicycle": routing_v2.RouteTravelMode.BICYCLE,
            "walk": routing_v2.RouteTravelMode.WALK,
            "two_wheeler": routing_v2.RouteTravelMode.TWO_WHEELER,
            "transit": routing_v2.RouteTravelMode.TRANSIT,
        }

        selected_travel_mode = TRAVEL_MODE_MAP.get(
            user_travel_mode.lower(), routing_v2.RouteTravelMode.DRIVE
        )

        origin = routing_v2.Waypoint(
            location=routing_v2.Location(
                lat_lng={"latitude": s_lat, "longitude": s_lon}
            )
        )
        destination = routing_v2.Waypoint(
            location=routing_v2.Location(
                lat_lng={"latitude": d_lat, "longitude": d_lon}
            )
        )

        if user_travel_mode.lower() in ["walk", "bicycle"]:
            request = routing_v2.ComputeRoutesRequest(
                origin=origin,
                destination=destination,
                travel_mode=selected_travel_mode,
                units=routing_v2.Units.METRIC,
            )
        else:
            request = routing_v2.ComputeRoutesRequest(
                origin=origin,
                destination=destination,
                travel_mode=selected_travel_mode,
                routing_preference=routing_v2.RoutingPreference.TRAFFIC_AWARE,
                units=routing_v2.Units.METRIC,
                route_modifiers={"avoid_highways": True, "avoid_ferries": True},
            )

        try:
            response = self.client.compute_routes(
                request=request, metadata=[("x-goog-fieldmask", self.field_mask)]
            )
            if not response.routes:
                return float("inf"), 0.0

            # Find the best route in the response
            best_route = min(response.routes, key=lambda r: r.duration.seconds)
            return (
                float(best_route.duration.seconds),
                best_route.distance_meters / 1000.0,
            )
        except Exception as e:
            logger.error("API Error for point %s,%s: %s", d_lat, d_lon, e)
            return float("inf"), 0.0





import math

def get_circle_vertices(lat: float, lon: float, radius_km: float, num_vertices: int = 6) -> list:
    """Pure-Python mathematical generation of circular vertices using distance & bearing."""
    vertices = []
    R_E = 6371.0  # Earth radius in km
    d_R = radius_km / R_E
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)

    for i in range(num_vertices):
        bearing = 2 * math.pi * i / num_vertices
        val_lat = math.asin(
            math.sin(lat_rad) * math.cos(d_R)
            + math.cos(lat_rad) * math.sin(d_R) * math.cos(bearing)
        )
        val_lon = lon_rad + math.atan2(
            math.sin(bearing) * math.sin(d_R) * math.cos(lat_rad),
            math.cos(d_R) - math.sin(lat_rad) * math.sin(val_lat),
        )
        vertices.append((math.degrees(val_lat), math.degrees(val_lon)))
    return vertices


def get_average_duration_parallel(
    lat: float,
    lon: float,
    polygon_gdf: Any,
    max_dis: float,
    user_travel_mode: str,
    calculator: RouteCalculator,
) -> dict:
    """
    Calculates the average travel time and distance from a center point to the vertices
    of a provided polygon in parallel using the Google Maps Routes API.
    """
    if not SPATIAL_LIBS_AVAILABLE or isinstance(polygon_gdf, list):
        coords_list = [(lon, lat) for lat, lon in polygon_gdf] if isinstance(polygon_gdf, list) else get_circle_vertices(lat, lon, max_dis)
    else:
        wkt_string = str(polygon_gdf.geometry.values[0])
        polygon = loads(wkt_string)
        coords_list = list(polygon.exterior.coords)

    lat_lon_list = [
        (lat, lon, d_lat, d_lon, user_travel_mode)
        for d_lon, d_lat in coords_list
    ]

    metrics = RouteMetrics()
    times, distances = [], []

    logger.info(f"Processing {len(lat_lon_list)} points in parallel...")

    # Production optimization: Parallel execution
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(calculator.compute_single_route, lat_lon_list))

    for total_seconds, distance in results:
        if (
            total_seconds == float("inf")
            or distance > (max_dis * 2)
            or distance <= 0.01
        ):
            continue

        metrics.total_points += 1
        times.append(total_seconds)
        distances.append(distance)

        metrics.min_time = min(metrics.min_time, total_seconds)
        metrics.max_time = max(metrics.max_time, total_seconds)
        metrics.min_distance = min(metrics.min_distance, distance)
        metrics.max_distance = max(metrics.max_distance, distance)

    if metrics.total_points > 0:
        metrics.avg_time = sum(times) / metrics.total_points
        metrics.avg_distance = sum(distances) / metrics.total_points

        metrics.p90_time = round(float(np.percentile(times, 90)), 2)
        metrics.p90_distance = round(float(np.percentile(distances, 90)), 2)

    return asdict(metrics)


def create_polygon(
    lat: float, lon: float, resolution: int, radius_km: float
) -> Any:
    """
    Creates a circular polygon buffer around a point. Falls back to list of vertices if libraries missing.
    """
    if not SPATIAL_LIBS_AVAILABLE:
        return get_circle_vertices(lat, lon, radius_km, num_vertices=resolution)
    
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(lon, lat)], crs="EPSG:4326")
    # Buffer in meters (radius * 1000)
    gdf_buffered = gdf.to_crs(epsg=3857).buffer(radius_km * 1000, resolution=resolution)
    return gpd.GeoDataFrame(geometry=gdf_buffered).to_crs(epsg=4326)


def convert_travel_constraint_to_radius(
    target_lat: float,
    target_lon: float,
    time_threshold: Optional[int] = None,
    distance_threshold: Optional[float] = None,
    initial_radius: Optional[float] = 10,
    user_travel_mode: Optional[str] = "drive",
) -> Dict[str, Any]:
    """
    Iteratively finds the optimal search radius (in meters) from a center point to match
    a travel time (seconds) or road distance (km) constraint. Use this when the user
    asks for things 'within X minutes' or 'within X km driving distance'.

    Args:
        target_lat: The latitude of the center point.
        target_lon: The longitude of the center point.
        time_threshold: Target travel time in seconds (e.g., 1800 for 30 minutes if area is region
            within the city, 7200 for 2 hours if area is city).
        distance_threshold: Target road distance in kilometers (e.g., 5.0 for 5km drive).
        initial_radius: The starting air-distance radius in km to begin the search. Default is 10.0.
        user_travel_mode: The travel mode inferred from user input to be considered while
            calculating the distance. Other values include: walk, bike, transit, bicycle,
            two_wheeler.

    Returns:
        A dictionary containing 'optimal_radius_meters', 'user_travel_mode',
            'target_lat', and 'target_lon'.
    """

    calculator = RouteCalculator(MAPS_API_KEY)
    current_radius = initial_radius
    resolution = 6
    max_iterations = 5

    # Determine which mode we are in
    if time_threshold is not None:
        mode = "TIME"
        limit = time_threshold
        lower_bound = limit * 0.90
        upper_bound = limit
    elif distance_threshold is not None:
        mode = "DISTANCE"
        limit = distance_threshold
        lower_bound = limit * 0.90
        upper_bound = limit
    else:
        raise ValueError("At least one threshold (time or distance) must be provided.")

    logger.info("Targeting %s window: %s - %s", mode, lower_bound, upper_bound)
    print(f"Targeting {mode} window: {lower_bound} - {upper_bound} limit: {limit}")

    for i in range(max_iterations):
        poly_gdf = create_polygon(target_lat, target_lon, resolution, current_radius)
        res = get_average_duration_parallel(
            target_lat,
            target_lon,
            poly_gdf,
            current_radius,
            user_travel_mode,
            calculator,
        )

        actual_val = res["p90_time"] if mode == "TIME" else res["p90_distance"]

        logger.info(
            "Iter %s: Radius %.2fkm -> Avg %s: %.2f",
            i + 1,
            current_radius,
            mode,
            actual_val,
        )
        print(
            f"Iter {i+1}: Radius {current_radius:.2f}km -> Avg {mode}: {actual_val:.2f}"
        )

        # 1. Success Condition
        if lower_bound <= actual_val <= upper_bound:
            logger.info(
                "Success: Found optimal radius %.2fkm within target window.",
                current_radius,
            )
            return {
                "optimal_radius_meters": int(current_radius * 1000),
                "user_travel_mode": user_travel_mode,
                "target_lat": target_lat,
                "target_lon": target_lon,
            }

        # 2. Calculate Adjustment Ratio
        ratio = actual_val / limit

        if ratio == 0:
            ratio = 0.5

        new_radius = current_radius / ratio
        current_radius = round(new_radius, 3)

    logger.warning("Reached max iterations. Returning best approximation.")
    return {
        "optimal_radius_meters": int(current_radius * 1000),
        "user_travel_mode": user_travel_mode,
        "target_lat": target_lat,
        "target_lon": target_lon,
    }


def get_dataset_tables_info_as_string(
    project_id: str, dataset_id: str, short_description: bool = True
) -> str:
    """
    Lists all tables in a BigQuery dataset and returns a beautifully formatted
    string containing their IDs, descriptions, general info, and schemas.

    Args:
        project_id (str): Your Google Cloud Project ID.
        dataset_id (str): The BigQuery Dataset ID.

    Returns:
        str: A formatted string containing all table metadata.
    """
    client = bigquery.Client(project=project_id)
    dataset_ref = f"{project_id}.{dataset_id}"

    output_lines = []
    output_lines.append(f"REPORT FOR DATASET: {dataset_ref}")
    output_lines.append("=" * 60)

    try:
        tables = list(client.list_tables(dataset_ref))

        if not tables:
            return f"No tables found in dataset: {dataset_ref}"

        for table_item in tables:
            table = client.get_table(table_item.reference)

            t_id = table.table_id
            t_desc = table.description or ""
            if short_description:
                t_desc = t_desc.split("**All Column Details:**")[0].strip()
            t_full_id = table.full_table_id
            t_type = table.table_type
            t_created = (
                table.created.strftime("%Y-%m-%d %H:%M:%S")
                if table.created
                else "Unknown"
            )
            t_rows = table.num_rows
            t_bytes = table.num_bytes

            output_lines.append(f"\n--- Table: {t_id} ---")
            output_lines.append(f"\n{t_desc}\n")
            output_lines.append(f"Full ID     : {t_full_id}")
            output_lines.append(f"Type        : {t_type}")
            output_lines.append(f"Created     : {t_created}")
            output_lines.append(f"Rows        : {t_rows}")
            output_lines.append(f"Size (Bytes): {t_bytes}")

            output_lines.append("-" * 60)

        tables_schema = "\n".join(output_lines)
        return tables_schema
    except NotFound:
        return f"Error: Dataset {dataset_ref} not found."
    except Exception as e:
        return f"An error occurred: {e}"


def resolve_address_to_coordinates(address_query: str) -> dict:
    """
    Actor: Strategy Planner
    Goal: Converts a physical address text search into precise coordinates
    without using the MCP toolset.
    """
    # Initialize the Google Maps client
    gmaps = googlemaps.Client(key=MAPS_API_KEY)

    try:
        # 1. Use the Geocoding API to find the address
        geocode_result = gmaps.geocode(address_query)

        if geocode_result:
            # Extract the first result's geometry
            location = geocode_result[0]["geometry"]["location"]
            formatted_address = geocode_result[0].get(
                "formatted_address", address_query
            )

            lat = location["lat"]
            lng = location["lng"]

            # 2. Return the standardized JSON format
            return {
                "status": "success",
                "action": "CENTER_MAP",
                "data": {
                    "lat": lat,
                    "lng": lng,
                    "zoom": 15,  # Standard zoom for specific addresses
                    "label": formatted_address,
                },
                "metadata": {
                    "method": "direct_geocoding_api",
                    "original_input": address_query,
                },
            }

        return {
            "status": "error",
            "message": f"Could not find coordinates for address: {address_query}",
        }

    except Exception as e:
        return {"status": "error", "message": f"API Error: {str(e)}"}


def resolve_pincode_to_coordinates(pincode: str) -> dict:
    """
    Actor: Strategy Planner
    Goal: Takes a postal code (Pin Code) and returns precise coordinates
    to visualize the region on the map.

    Workflow:
    1. Input: Pin Code (e.g., "416416")
    2. Action: Geocodes the pin code to find its geometric center.
    3. Output: JSON-ready dict for frontend map centering.
    """
    # Initialize the client
    gmaps = googlemaps.Client(key=MAPS_API_KEY)

    try:
        # Perform Geocoding for the pin code
        # Specifying 'components' ensures we look for postal codes specifically
        geocode_result = gmaps.geocode(
            components={"postal_code": pincode, "country": "IN"}
        )

        if geocode_result:
            location = geocode_result[0]["geometry"]["location"]
            lat = location["lat"]
            lng = location["lng"]

            # Use a slightly wider zoom (13-14) for pin codes as they represent areas
            return {
                "status": "success",
                "action": "CENTER_MAP",
                "data": {
                    "lat": lat,
                    "lng": lng,
                    "zoom": 13,
                    "address": geocode_result[0].get("formatted_address"),
                },
                "metadata": {"method": "pincode_geocoding", "input_pincode": pincode},
            }

        return {"status": "error", "message": f"Pin code {pincode} not found."}

    except Exception as e:
        return {"status": "error", "message": str(e)}


def resolve_mapurl_to_coordinates(map_url: str) -> dict:
    """
    Actor: Strategy Planner
    Goal: Cloud-safe extraction of coordinates from Google Maps links.
    Removes Selenium to prevent Vertex AI deployment failures.
    """

    def extract_precise_coords(url):
        # 1. Precise data coordinates (!3d / !4d) - most accurate
        precise_regex = r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)"
        match = re.search(precise_regex, url)
        if match:
            return float(match.group(1)), float(match.group(2)), 18

        # 2. Camera center coordinates (@lat,long) - standard for desktop links
        camera_regex = r"@(-?\d+\.\d+),(-?\d+\.\d+)(?:,(\d+)z)?"
        match = re.search(camera_regex, url)
        if match:
            lat, lon, zoom = match.groups()
            return float(lat), float(lon), int(zoom) if zoom else 15

        # 3. Query parameter format (?ll=lat,lng) - common in older links
        ll_regex = r"ll=(-?\d+\.\d+),(-?\d+\.\d+)"
        match = re.search(ll_regex, url)
        if match:
            return float(match.group(1)), float(match.group(2)), 15

        return None

    # Step 1: Resolve Redirects using Requests
    try:
        # Use a real User-Agent to avoid being blocked by Google's "browser verification"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        # allow_redirects=True is the default but made explicit here
        response = requests.get(
            map_url, headers=headers, timeout=10, allow_redirects=True
        )
        final_url = response.url

        coords_data = extract_precise_coords(final_url)
        if coords_data:
            lat, lng, zoom = coords_data

            return {
                "status": "success",
                "action": "CENTER_MAP",
                "data": {"lat": lat, "lng": lng, "zoom": zoom},
                "metadata": {"method": "requests_redirect", "resolved_url": final_url},
            }
    except Exception as e:
        # Silently fail to Step 2
        pass

    # Step 2: Fallback - Use Geocoding API if URL doesn't contain coordinates
    # (Optional but highly recommended for cloud robustness)
    try:
        # If config is available, we can use the API as a last resort
        import config

        gmaps = googlemaps.Client(key=MAPS_API_KEY)
        # Often the link itself can be geocoded as a "place" if it's a short URL
        geocode_result = gmaps.geocode(map_url)

        if geocode_result:
            location = geocode_result[0]["geometry"]["location"]

            return {
                "status": "success",
                "action": "CENTER_MAP",
                "data": {"lat": location["lat"], "lng": location["lng"], "zoom": 16},
                "metadata": {"method": "geocoding_api_fallback"},
            }
    except Exception:
        pass

    return {
        "status": "error",
        "message": "Cloud-safe link expansion failed. Could not find coordinates in URL.",
    }


def locate_by_coordinates(coord_input: str) -> dict:
    """
    Actor: Strategy Planner
    Goal: Parses raw coordinate text to extract Lat/Long for map visualization.

    Workflow:
    1. Input: String containing coordinates (e.g., "16.85, 74.58" or "(16.85, 74.58)")
    2. Action: Uses Regex to find float pairs and validates geographic bounds.
    3. Output: JSON-ready dict for frontend map centering.
    """
    # Regex to find two decimal numbers separated by comma or space
    # Handles optional minus signs and various spacing
    coord_regex = r"(-?\d+\.\d+)\s*[,|\s]\s*(-?\d+\.\d+)"

    match = re.search(coord_regex, coord_input)

    if match:
        try:
            lat = float(match.group(1))
            lng = float(match.group(2))

            # Validation: Earth coordinate limits
            if not (-90 <= lat <= 90):
                return {
                    "status": "error",
                    "message": "Latitude must be between -90 and 90.",
                }
            if not (-180 <= lng <= 180):
                return {
                    "status": "error",
                    "message": "Longitude must be between -180 and 180.",
                }

            return {
                "status": "success",
                "action": "CENTER_MAP",
                "data": {
                    "lat": lat,
                    "lng": lng,
                    "zoom": 17,  # High zoom because coordinates are precise
                },
                "metadata": {
                    "method": "coordinate_parsing",
                    "original_input": coord_input,
                },
            }
        except ValueError:
            return {"status": "error", "message": "Invalid number format."}

    return {
        "status": "error",
        "message": "Could not find a valid Lat/Long pair in the input.",
    }


def access_secret_version(secret_id, version_id="latest"):
    # Create the Secret Manager client.
    client = secretmanager.SecretManagerServiceClient()

    # Build the resource name of the secret version.
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/{version_id}"

    # Access the secret version.
    response = client.access_secret_version(request={"name": name})

    # Return the decoded payload.
    return response.payload.data.decode("UTF-8")


def get_processing_resolution(optimal_radius_meters: float) -> int:
    """
    Determines the H3 processing resolution based on the target radius.
    """
    if optimal_radius_meters <= 2500:
        return 9
    elif optimal_radius_meters <= 5000:
        return 8
    elif optimal_radius_meters <= 15000:
        return 7
    else:
        return 6
