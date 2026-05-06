"""Custom tools for location identification, catchment area analysis, and place search."""

import os
import json
import logging
from typing import Optional

from google.adk.tools import ToolContext

from agents.common_utils import (
    convert_travel_constraint_to_radius,
    resolve_address_to_coordinates,
    resolve_pincode_to_coordinates,
    resolve_mapurl_to_coordinates,
    locate_by_coordinates,
    get_processing_resolution,
)

from agents.places_text_search_utils import (
    create_polygon_shapely,
    search_places_in_polygon,
)

from agents.bq_places_insights_utils import (
    fetch_places_insights_h3,
    get_hex_radius,
    get_target_types_from_gemini,
)

logger = logging.getLogger(__name__)

# Fetch essential environment variables required for BigQuery connections
PROJECT_ID = os.getenv("PROJECT_ID")
LOCATION = os.getenv("LOCATION")
DATASET_ID = os.getenv("DATASET_ID")
TOOL_CONTEXT_KEY = "general_analysis"


def identify_coordinates(
    input_str: str, tool_context: ToolContext, input_type: Optional[str] = None
) -> str:
    """
    Identifies coordinates based on input (address, pincode, map URL, or raw coordinates)
    and updates the state at "general_analysis"."general_analysis_marker_point".

    Args:
        input_str: The input string containing address, pincode, URL, or coordinates.
        tool_context: ToolContext to access and update state.
        input_type: Optional hint about the input type
            ('address', 'pincode', 'map_url', 'coordinates').
    """
    res = {"status": "error"}

    if input_type:
        input_type = input_type.lower()
        if input_type == "coordinates":
            res = locate_by_coordinates(input_str)
            method = "locate_by_coordinates"
        elif input_type == "map_url":
            res = resolve_mapurl_to_coordinates(input_str)
            method = "resolve_mapurl_to_coordinates"
        elif input_type == "pincode":
            res = resolve_pincode_to_coordinates(input_str)
            method = "resolve_pincode_to_coordinates"
        elif input_type == "address":
            res = resolve_address_to_coordinates(input_str)
            method = "resolve_address_to_coordinates"

        if res["status"] == "success":
            return _update_coordinate_state_with_res(res, tool_context, method)
        logger.warning(
            "Failed to resolve as %s, falling back to auto-detection.", input_type
        )
        print(f"Failed to resolve as {input_type}, falling back to auto-detection.")

    # Fallback to auto-detection if no input_type or if specific resolution failed
    # 1. Try locate_by_coordinates (cheap, regex based)
    res = locate_by_coordinates(input_str)
    if res["status"] == "success":
        return _update_coordinate_state_with_res(
            res, tool_context, "locate_by_coordinates"
        )

    # 2. Try resolve_mapurl_to_coordinates if it looks like a URL
    if "http" in input_str or "maps" in input_str:
        res = resolve_mapurl_to_coordinates(input_str)
        if res["status"] == "success":
            return _update_coordinate_state_with_res(
                res, tool_context, "resolve_mapurl_to_coordinates"
            )

    # 3. Try resolve_pincode_to_coordinates if it looks like a pincode
    # Assuming India pincode (6 digits) or generic digits
    if input_str.isdigit() and len(input_str) == 6:
        res = resolve_pincode_to_coordinates(input_str)
        if res["status"] == "success":
            return _update_coordinate_state_with_res(
                res, tool_context, "resolve_pincode_to_coordinates"
            )

    # 4. Fallback to resolve_address_to_coordinates
    res = resolve_address_to_coordinates(input_str)
    if res["status"] == "success":
        return _update_coordinate_state_with_res(
            res, tool_context, "resolve_address_to_coordinates"
        )

    return f"Failed to identify coordinates from input: {input_str}"


def _update_coordinate_state_with_res(
    res: dict, tool_context: ToolContext, method: str
) -> str:
    lat = res["data"]["lat"]
    lng = res["data"]["lng"]

    # Update existing state for compatibility
    ca = tool_context.state.get(f"{TOOL_CONTEXT_KEY}", {})
    if hasattr(ca, "model_dump"):
        ca = ca.model_dump()
    ca[f"{TOOL_CONTEXT_KEY}_marker_point"] = [lat, lng]
    tool_context.state[f"{TOOL_CONTEXT_KEY}"] = ca

    # Update state as requested by user
    if f"{TOOL_CONTEXT_KEY}" not in tool_context.state:
        tool_context.state[f"{TOOL_CONTEXT_KEY}"] = {}
    # tool_context.state[f"{TOOL_CONTEXT_KEY}"]["marker_point"] = [lat, lng]

    try:
        from state import debug_dump_state

        debug_dump_state(
            tool_context.state,
            label=f"identify_coordinates_and_update_state ({method})",
        )
    except Exception:
        pass

    return f"Successfully resolved coordinates to [{lat}, {lng}] using {method} and updated state."


def calculate_optimal_radius(
    target_lat: float,
    target_lon: float,
    tool_context: ToolContext,
    time_threshold: Optional[int] = None,
    distance_threshold: Optional[float] = None,
    initial_radius: Optional[float] = 10,
    user_travel_mode: Optional[str] = "drive",
) -> str:
    """
    Calculates the optimal search radius based on time or distance constraints
    and updates the tool context state.

    Args:
        target_lat: Latitude of center point.
        target_lon: Longitude of center point.
        tool_context: Tool context.
        time_threshold: Target travel time in SECONDS (e.g., 1800 for 30 minutes).
        distance_threshold: Target road distance in kilometers (e.g., 5.0 for 5km).
        initial_radius: Starting air-distance radius in km. Default is 10.
        user_travel_mode: Travel mode (drive, walk, bike, transit).
    """
    result = convert_travel_constraint_to_radius(
        target_lat=target_lat,
        target_lon=target_lon,
        time_threshold=time_threshold,
        distance_threshold=distance_threshold,
        initial_radius=initial_radius,
        user_travel_mode=user_travel_mode,
    )

    ca = tool_context.state.get(f"{TOOL_CONTEXT_KEY}", {})
    if hasattr(ca, "model_dump"):
        ca = ca.model_dump()
    ca[f"{TOOL_CONTEXT_KEY}_radius"] = result["optimal_radius_meters"]
    ca["user_travel_mode"] = result["user_travel_mode"]
    tool_context.state[f"{TOOL_CONTEXT_KEY}"] = ca

    try:
        from state import debug_dump_state

        debug_dump_state(tool_context.state)
    except Exception:
        pass

    return (
        f"The optimal search space is: {result['optimal_radius_meters']} meters "
        f"when considering {result['user_travel_mode']} as travel mode."
    )


def fetch_places_data_parallel(user_query: str, tool_context: ToolContext) -> str:
    """
    Fetches places of interest (competitors, transport hubs, restaurants)
    in parallel within the catchment area.

    Args:
        user_query: The user's query to identify relevant competitors (e.g., "grocery stores").
        tool_context: ToolContext to access and update state.
    """
    logger.info("Starting fetch_places_data_parallel for query: %s", user_query)

    ca = tool_context.state.get(f"{TOOL_CONTEXT_KEY}", {})
    if not ca:
        return f"Error: {TOOL_CONTEXT_KEY} missing in state."

    optimal_radius = ca.get(f"{TOOL_CONTEXT_KEY}_radius")
    coords = ca.get(f"{TOOL_CONTEXT_KEY}_marker_point")

    if (
        coords == "Not yet resolved, get from the parent agent"
        or optimal_radius == "Not yet set"
    ):
        return "Error: Coordinates or optimal_radius missing in state."

    try:
        lat = float(coords[0])
        lon = float(coords[1])
        optimal_radius = float(optimal_radius)
    except (IndexError, TypeError, ValueError) as e:
        return f"Error parsing coordinates or radius: {e}"

    # Create polygon
    poly = create_polygon_shapely(lat, lon, optimal_radius)

    # Construct prompt
    gemini_prompt = f"""### Query Optimization Prompt
    You are a search query optimizer for the Google Maps API. Your goal is to transform the user's request into specific, searchable strings grouped by category.

    ---

    ### ⛔ STRICT RULES
    1. **Categorical Grouping:** If the user asks for "competitors" or multiple brands, group similar brands into a single query string (e.g., "Croma, Reliance Digital, Vijay Sales near me"). 
    2. **Intent Adherence:** - If the user asks for electronics, group electronics brands.
    - If the user asks for supermarkets, group supermarket brands.
    3. **Localized Phrasing:** Every generated query string must end with the phrase **"near me"**.
    4. **No Explanations:** Do not provide any conversational text, addresses, or rebranding history.
    5. **JSON Output:** Return strictly a valid JSON object where the **Key** is the grouped search string and the **Value** is the category.

    ### 🌟 Examples
        * **User:** "Show me electronics and supermarkets nearby"
            **Output:** {{ "Croma, Reliance Digital, and Vijay Sales near me": "retail",
                          "Dmart, Big Bazaar, Star Bazaar near me": "supermarket" }}

        * **User:** "Show me electronics stores in Mumbai"
            **Output:** `{{ "Major electronics stores like Croma and Reliance Digital near me":
                          "Retail Store" }}`

        * **User:** "Hospitals in Borivali"
            **Output:** `{{ "Multi-specialty hospitals nearby": "Healthcare" }}`

        * **User:** "Show me Croma stores within 1km from Mumbai"
            **Output:** `{{ "Croma stores near me": "Retail Store" }}`
        ---

        ### Execution
        **User Query:** "{user_query}"
    """

    try:
        output_res = get_processing_resolution(optimal_radius)

        results = search_places_in_polygon(
            poly, gemini_prompt=gemini_prompt, output_res=output_res
        )

        # Update state
        if "places_insights" not in ca:
            ca["places_insights"] = {"markers": [], "hex_codes": []}
        else:
            if "hex_codes" not in ca["places_insights"]:
                ca["places_insights"]["hex_codes"] = []

        # Group results by hex for summary
        hex_data_map = {}

        for s in results:
            details = f"name: {s.get('name')} | address: {s.get('address')}"
            if s.get("rating"):
                details += f" | rating: {s.get('rating')}"
            info_dict = {}
            for k, v in s.items():
                if isinstance(v, str) and k.lower() not in (
                    "lat",
                    "lng",
                    "latitude",
                    "longitude",
                    "place_id",
                    "id",
                ):
                    info_dict[k] = v

            q_type = s.get("type", "unknown")

            ca["places_insights"]["markers"].append(
                {
                    "lat": float(s["lat"]),
                    "long": float(s["lng"]),
                    "details": details,
                    "info": info_dict,
                    "google_maps_url": s.get("google_maps_url"),
                    "place_id": s.get("place_id"),
                    "tag": q_type,
                }
            )

            # Aggregate for hex summary
            h_id = s.get("hex_id")
            if h_id:
                if h_id not in hex_data_map:
                    hex_data_map[h_id] = {}
                hex_data_map[h_id][q_type] = hex_data_map[h_id].get(q_type, 0) + 1

        # Build hex_codes list
        for h_id, counts in hex_data_map.items():
            summary_parts = [f"{k}: {v}" for k, v in counts.items()]
            details_str = ", ".join(summary_parts)

            ca["places_insights"]["hex_codes"].append(
                {
                    "hex_id": h_id,
                    "details": details_str,
                    "tag": "places_h3_aggregation",
                    "res": output_res,
                }
            )

        tool_context.state[f"{TOOL_CONTEXT_KEY}"] = ca

        try:
            from state import debug_dump_state

            debug_dump_state(
                tool_context.state, label=f"place_api_and_update_state ({method})"
            )
        except Exception:
            pass

        try:
            # Dynamic selection of target types based on user query
            target_types = get_target_types_from_gemini(user_query)
            logger.info("Selected target types for BQ insights: %s", target_types)

            radius = get_hex_radius(output_res)
            fetch_places_insights_h3(
                poly.wkt,
                tool_context.state,
                output_res,
                radius,
                target_types=target_types,
                analysis_key="general_analysis",
            )
        except Exception as e:
            logger.error("Failed to run fetch_places_insights_h3: %s", e)

        return json.dumps(
            {
                "status": "success",
                "total_places": len(results),
                "message": f"Found {len(results)} places.",
            },
            indent=4,
        )

    except Exception as e:
        logger.error("fetch_places_data_parallel failed: %s", e)
        return f"Error: {e}"
