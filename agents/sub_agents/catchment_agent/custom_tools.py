"""This module defines custom tools for the catchment agent."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from typing import List, Optional

from google import genai
from google.adk import Runner
from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService
from google.adk.tools import ToolContext
from google.cloud import bigquery
from google.cloud.exceptions import NotFound

from agents.common_utils import (
    convert_travel_constraint_to_radius,
    get_processing_resolution,
    locate_by_coordinates,
    resolve_address_to_coordinates,
    resolve_mapurl_to_coordinates,
    resolve_pincode_to_coordinates,
)
from agents.places_text_search_utils import (
    create_polygon_shapely,
    search_places_in_polygon,
)
from agents.query_bq_spatial_utils import (
    fetch_bq_table_geospatial_data,
    process_table_geospatial_results,
)
from agents.sub_agents.catchment_agent.prompt import get_filter_agent_instructions

from agents.bq_places_insights_utils import fetch_places_insights_h3, get_hex_radius

# Configure standard logger
logger = logging.getLogger(__name__)

# Fetch essential environment variables required for BigQuery connections
PROJECT_ID = os.getenv("PROJECT_ID")
LOCATION = os.getenv("LOCATION")
DATASET_ID = os.getenv("DATASET_ID")
MAPS_API_KEY = os.getenv("MAPS_API_KEY")


def get_table_sample_data(table_id: str) -> str:
    """
    Fetches the first 10 rows of a BigQuery table to inspect the actual data values, casing, and
    format. Use this tool before writing a SQL filter condition or selecting attributes to
    understand what the data looks like.

    Args:
        table_id: The specific table name to inspect (e.g. 'retail_asset_master').
    """
    client = bigquery.Client(project=PROJECT_ID)
    try:
        # Clean the input to ensure we only have the bare table name
        safe_table_id = table_id.split(".")[-1]
        table_path = f"{PROJECT_ID}.{DATASET_ID}.{safe_table_id}"
        query = f"SELECT * FROM `{table_path}` LIMIT 10"

        query_job = client.query(query)
        df_sample = query_job.to_dataframe()

        # Convert datetime objects to string before json conversion
        for col in df_sample.select_dtypes(
            include=["datetime64", "datetimetz"]
        ).columns:
            df_sample[col] = df_sample[col].astype(str)

        print("first 10 rows: ")
        print(df_sample.head())
        return df_sample.to_json(orient="records", indent=2)
    except Exception as e:
        logger.error("Error fetching sample data for %s: %s", table_id, e)
        return f"Error fetching sample data for {table_id}: {str(e)}"


def query_bq_table_geospatial_radius(
    target_lat: float,
    target_lon: float,
    optimal_radius_meters: float,
    table_id: str,
    tool_context: ToolContext,
    lat_column: str = "latitude",
    lon_column: str = "longitude",
    select_clause: Optional[str] = None,
    filter_condition: Optional[str] = None,
    short_name: str = "Catchment Data",
) -> str:
    """
    Executes a multi-table geographic search in BigQuery, looking for points of interest
    within a specific radius of a target latitude/longitude coordinate.

    This function calls fetch_bq_table_geospatial_data to get results and
    process_table_geospatial_results to aggregate them, and then updates the tool state.
    """
    processing_resolution = get_processing_resolution(optimal_radius_meters)

    all_results = fetch_bq_table_geospatial_data(
        target_lat,
        target_lon,
        optimal_radius_meters,
        table_id,
        lat_column,
        lon_column,
        select_clause,
        filter_condition,
    )

    radius_poly = create_polygon_shapely(
        lat=target_lat, lon=target_lon, radius_meters=optimal_radius_meters
    )
    processing_result = process_table_geospatial_results(
        all_results=all_results,
        table_id=table_id,
        lat_column="latitude",
        lon_column="longitude",
        processing_resolution=processing_resolution,
        radius_poly=radius_poly,
    )

    if processing_result["status"] == "error":
        return processing_result["message"]
    if processing_result["status"] == "empty":
        return "DATA_NOT_FOUND"

    # Filter hexes for building density table where point count < 25
    if (
        processing_result.get("use_h3")
        and "ghs_obat_building_density_india_master" in table_id.lower()
    ):
        processing_result["hex_codes"] = [
            h
            for h in processing_result["hex_codes"]
            if h.get("info", {}).get("poi_count", 0) >= 25
        ]
        if not processing_result["hex_codes"]:
            return "DATA_NOT_FOUND"

    use_h3 = processing_result["use_h3"]

    ca = tool_context.state.get("catchment_analysis", {})
    if hasattr(ca, "model_dump"):
        ca = ca.model_dump()
    if "dataset_insights" not in ca:
        ca["dataset_insights"] = {}

    display_name = table_id.split(".")[-1].replace("_", " ").title()

    if use_h3:
        hex_codes = processing_result["hex_codes"]
        if "hex_dataset_ids" not in ca["dataset_insights"]:
            ca["dataset_insights"]["hex_dataset_ids"] = []
        if table_id not in ca["dataset_insights"]["hex_dataset_ids"]:
            ca["dataset_insights"]["hex_dataset_ids"].append(table_id)

        if table_id not in ca["dataset_insights"]:
            ca["dataset_insights"][table_id] = {}
        ca["dataset_insights"][table_id]["dataset_details"] = display_name

        if "hex_codes" not in ca["dataset_insights"][table_id]:
            ca["dataset_insights"][table_id]["hex_codes"] = []
        ca["dataset_insights"][table_id]["hex_codes"].extend(hex_codes)
    else:
        markers = processing_result["markers"]
        if "marker_dataset_ids" not in ca["dataset_insights"]:
            ca["dataset_insights"]["marker_dataset_ids"] = []
        if table_id not in ca["dataset_insights"]["marker_dataset_ids"]:
            ca["dataset_insights"]["marker_dataset_ids"].append(table_id)

        if table_id not in ca["dataset_insights"]:
            ca["dataset_insights"][table_id] = {}
        ca["dataset_insights"][table_id]["dataset_details"] = display_name

        if "markers" not in ca["dataset_insights"][table_id]:
            ca["dataset_insights"][table_id]["markers"] = []
        ca["dataset_insights"][table_id]["markers"].extend(markers)

    tool_context.state["catchment_analysis"] = ca

    return (
        f"Success! Processed {len(all_results)} points. State was updated using "
        f"{'H3 Grid Aggregation' if use_h3 else 'Markers.'}"
    )


def calculate_catchment_radius(
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
        time_threshold: Target travel time in SECONDS (e.g., 1800 for 30 minutes if area is
            region within the city, 7200 for 2 hours if area is city).
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

    ca = tool_context.state.get("catchment_analysis", {})
    if hasattr(ca, "model_dump"):
        ca = ca.model_dump()
    ca["catchment_analysis_radius"] = result["optimal_radius_meters"]
    ca["user_travel_mode"] = result["user_travel_mode"]
    # ca["catchment_marker_point"] = [result["target_lat"], result["target_lon"]]
    tool_context.state["catchment_analysis"] = ca

    try:
        from state import debug_dump_state

        debug_dump_state(tool_context.state)
    except Exception:
        pass

    return (
        f"The optimal search space is: {result['optimal_radius_meters']} meters "
        f"when considering {result['user_travel_mode']} as travel mode."
    )


def identify_coordinates(
    input_str: str, tool_context: ToolContext, input_type: Optional[str] = None
) -> str:
    """
    Identifies coordinates based on input (address, pincode, map URL, or raw coordinates)
    and updates the state at "catchment_analysis"."catchment_marker_point" and
    "Catchment_analysis"."marker_point".

    Args:
        input_str: The input string containing address, pincode, URL, or coordinates.
        tool_context: ToolContext to access and update state.
        input_type: Optional hint about the input type ('address', 'pincode', 'map_url',
            'coordinates').
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
            return _update_state_with_res(res, tool_context, method)
        else:
            logger.warning(
                "Failed to resolve as %s, falling back to auto-detection.", input_type
            )
            print(f"Failed to resolve as {input_type}, falling back to auto-detection.")

    # Fallback to auto-detection if no input_type or if specific resolution failed

    # 1. Try locate_by_coordinates (cheap, regex based)
    res = locate_by_coordinates(input_str)
    if res["status"] == "success":
        return _update_state_with_res(res, tool_context, "locate_by_coordinates")

    # 2. Try resolve_mapurl_to_coordinates if it looks like a URL
    if "http" in input_str or "maps" in input_str:
        res = resolve_mapurl_to_coordinates(input_str)
        if res["status"] == "success":
            return _update_state_with_res(
                res, tool_context, "resolve_mapurl_to_coordinates"
            )

    # 3. Try resolve_pincode_to_coordinates if it looks like a pincode
    # Assuming India pincode (6 digits) or generic digits
    if input_str.isdigit() and len(input_str) == 6:
        res = resolve_pincode_to_coordinates(input_str)
        if res["status"] == "success":
            return _update_state_with_res(
                res, tool_context, "resolve_pincode_to_coordinates"
            )

    # 4. Fallback to resolve_address_to_coordinates
    res = resolve_address_to_coordinates(input_str)
    if res["status"] == "success":
        return _update_state_with_res(
            res, tool_context, "resolve_address_to_coordinates"
        )

    return f"Failed to identify coordinates from input: {input_str}"


def _update_state_with_res(res: dict, tool_context: ToolContext, method: str) -> str:
    lat = res["data"]["lat"]
    lng = res["data"]["lng"]

    # Update existing state for compatibility
    ca = tool_context.state.get("catchment_analysis", {})
    if hasattr(ca, "model_dump"):
        ca = ca.model_dump()
    ca["catchment_marker_point"] = [lat, lng]
    tool_context.state["catchment_analysis"] = ca

    try:
        from state import debug_dump_state

        debug_dump_state(
            tool_context.state,
            label=f"identify_coordinates_and_update_state ({method})",
        )
    except Exception:
        pass

    return f"Successfully resolved coordinates to [{lat}, {lng}] using {method} and updated state."


def get_unique_column_values(
    table_id: str, column_names: List[str], tool_context: ToolContext
) -> str:
    """
    Fetches unique values from specified columns in a BigQuery table.
    Use this to understand what values exist in columns before applying filters.
    Please select multiple column names which you think are useful based on the first 10 rows
    returned by `get_table_sample_data`.

    Args:
        table_id: Fully qualified table ID (PROJECT.DATASET.TABLE) or just table name.
        column_names: A list of column names to inspect.
    """
    client = bigquery.Client(project=PROJECT_ID)

    # Clean the input to ensure we have the fully qualified name or deduce it
    parts = table_id.split(".")
    if len(parts) == 1:
        table_path = f"{PROJECT_ID}.{DATASET_ID}.{table_id}"
    elif len(parts) == 2:
        table_path = f"{PROJECT_ID}.{parts[0]}.{parts[1]}"
    else:
        table_path = table_id.replace(":", ".")

    all_results = []
    select_clauses = []
    safe_columns = []

    for i, col in enumerate(column_names):
        safe_alias = f"col_{i}"
        safe_columns.append((col, safe_alias))
        select_clauses.append(f"APPROX_COUNT_DISTINCT(`{col}`) AS `{safe_alias}_count`")
        select_clauses.append(
            f"ARRAY_AGG(DISTINCT `{col}` IGNORE NULLS LIMIT 101) AS `{safe_alias}_vals`"
        )

    query = f"SELECT \n  {', \n  '.join(select_clauses)} \nFROM `{table_path}`"

    try:
        query_job = client.query(query)
        results = list(query_job.result())

        if not results:
            return "No data found in table."

        row = results[0]

        for col, safe_alias in safe_columns:
            unique_count = row[f"{safe_alias}_count"]

            if unique_count > 100:
                all_results.append(
                    f"Warning: Too many unique values ({unique_count}) for column '{col}'. "
                    "Please do not run this tool for high cardinality columns."
                )
                continue

            unique_values = row[f"{safe_alias}_vals"] or []

            if len(unique_values) > 100:
                unique_values_str = [str(v) for v in unique_values[:100]]
                all_results.append(
                    f"Warning: Found more than 100 unique values for column '{col}'. Found "
                    f"{unique_count}. Showing first 100: {', '.join(unique_values_str)}"
                )
            else:
                unique_values_str = [str(v) for v in unique_values]
                all_results.append(
                    f"Unique values for column '{col}': {', '.join(unique_values_str)}"
                )

        print("unique column values results: ", all_results)
    except Exception as e:
        logger.error("Error fetching unique values in %s: %s", table_id, e)
        return f"Error fetching unique values: {str(e)}"

    return "\n\n".join(all_results)


def fetch_catchment_data_parallel(user_query: str, tool_context: ToolContext) -> str:
    """
    Queries multiple BigQuery tables in parallel using ThreadPoolExecutor.
    For each table, it uses Gemini to decide column filters, then executes the query.

    Args:
        user_query (str): The original user query/task context (e.g. "Catchment analysis "
            "for Starbucks").
        tool_context (ToolContext): Framework context for state access.

    Returns:
        str: Summary of parallel execution results.
    """
    # 1. Initialize GenAI Client
    client = genai.Client(vertexai=True, project=PROJECT_ID, location="us-central1")
    table_list = get_list_of_tables_in_dataset(PROJECT_ID, DATASET_ID)
    table_list = [t for t in table_list if t != "pincode_boundaries_mmr"]
    print(f"\n\nTable list: {table_list}")
    catchment_analysis = tool_context.state.get("catchment_analysis", {})
    if catchment_analysis:
        optimal_radius = catchment_analysis.get(
            "catchment_analysis_radius", "Not yet set"
        )
        coords = catchment_analysis.get(
            "catchment_marker_point", "Not yet resolved, get from the parent agent"
        )
    else:
        optimal_radius = "Not yet set"
        coords = "Not yet resolved, get from the parent agent"
    lat = coords[0]
    lon = coords[1]
    print(
        f"\n\ncoords: {coords}, optimal_radius: {optimal_radius}, PROJECT_ID: {PROJECT_ID}, "
        f"DATASET_ID: {DATASET_ID}"
    )

    filter_instructions = get_filter_agent_instructions(
        user_query=user_query, PROJECT_ID=PROJECT_ID, DATASET_ID=DATASET_ID
    )

    # Define the Orchestrator Agent (Strategy Planner)
    filter_agent = Agent(
        name="filter_and_get_nearby_points_agent",
        model=os.getenv("MODEL"),
        instruction=filter_instructions,
        tools=[
            query_bq_table_geospatial_radius,
            get_table_sample_data,
            get_unique_column_values,
        ],
    )
    print("AGENT INITIALIZED")

    def _process_single_table(table_id: str) -> dict:
        table_desc = get_table_desc(PROJECT_ID, DATASET_ID, table_id)
        logger.info("Processing table via subagent: %s", table_id)
        print(f"#####:  Processing table via subagent: {table_id}")

        async def run_subagent():
            session_service = InMemorySessionService()
            await session_service.create_session(
                app_name="retailvista", user_id="user", session_id=f"session_{table_id}"
            )

            runner = Runner(
                agent=filter_agent,
                app_name="retailvista",
                session_service=session_service,
            )

            input_text = f"""
            For table: {table_id} please perform catchment analysis for latitude: {lat} and
            longitude: {lon} and radius: {optimal_radius}. Table schema for which I want to
            filter currently: {table_desc}
            """
            from google.genai import types
            from google.adk.agents.run_config import RunConfig

            new_msg = types.Content(role="user", parts=[types.Part(text=input_text)])
            run_config = RunConfig(max_llm_calls=15)
            async for _ in runner.run_async(
                user_id="user",
                session_id=f"session_{table_id}",
                new_message=new_msg,
                run_config=run_config,
            ):
                pass

            session = await session_service.get_session(
                app_name="retailvista", user_id="user", session_id=f"session_{table_id}"
            )
            ca = {}
            if session and hasattr(session, "state"):
                ca = session.state.get("catchment_analysis", {})
            elif session and isinstance(session, dict):
                ca = session.get("state", {}).get("catchment_analysis", {})

            # print(f"DEBUG: session state for {table_id}: {ca}")

            return {"table_id": table_id, "status": "Success", "catchment_analysis": ca}

        res = asyncio.run(run_subagent())
        return res

    results = []
    with ThreadPoolExecutor(max_workers=len(table_list)) as executor:
        futures = [executor.submit(_process_single_table, t) for t in table_list]
        for future in futures:
            results.append(future.result())

    # 🤝 Merge state sequentially in main thread to avoid race conditions
    ca = tool_context.state.get("catchment_analysis") or {}
    if "dataset_insights" not in ca:
        ca["dataset_insights"] = {}

    for r in results:
        if r.get("status") == "Success" and r.get("catchment_analysis"):
            tid = r.get("table_id", "")
            ca_sub = r.get("catchment_analysis", {})
            di = ca_sub.get("dataset_insights", {})
            table_data = {}
            for key, val in di.items():
                if key == tid or key.endswith(f".{tid}"):
                    table_data = val
                    break

            mode = "H3" if "hex_codes" in table_data else "Marker"
            data = table_data.get("hex_codes") or table_data.get("markers") or []
            sname = table_data.get("dataset_details", "Catchment Data")

            if tid not in ca["dataset_insights"]:
                ca["dataset_insights"][tid] = {}
            ca["dataset_insights"][tid]["dataset_details"] = sname

            if mode == "H3":
                if "hex_dataset_ids" not in ca["dataset_insights"]:
                    ca["dataset_insights"]["hex_dataset_ids"] = []
                if tid not in ca["dataset_insights"]["hex_dataset_ids"]:
                    ca["dataset_insights"]["hex_dataset_ids"].append(tid)
                ca["dataset_insights"][tid]["hex_codes"] = data
            else:
                if "marker_dataset_ids" not in ca["dataset_insights"]:
                    ca["dataset_insights"]["marker_dataset_ids"] = []
                if tid not in ca["dataset_insights"]["marker_dataset_ids"]:
                    ca["dataset_insights"]["marker_dataset_ids"].append(tid)
                ca["dataset_insights"][tid]["markers"] = data

    tool_context.state["catchment_analysis"] = ca

    try:
        from state import debug_dump_state

        debug_dump_state(tool_context.state, label="MERGED STATE")
    except Exception as e:
        logger.error("Failed to dump merged state: %s", e)

    summary_results = []
    for r in results:
        summary_item = {k: v for k, v in r.items() if k != "data"}
        summary_results.append(summary_item)

    return f"Processed {len(table_list)} tables. Summary: {json.dumps(summary_results, indent=2)}"


def fetch_places_data_parallel(user_query: str, tool_context: ToolContext) -> str:
    """
    Fetches places of interest (competitors, transport hubs, restaurants) in parallel within
    the catchment area.

    Args:
        user_query: The user's query to identify relevant competitors (e.g., "grocery stores").
        tool_context: ToolContext to access and update state.
    """
    logger.info("Starting fetch_places_data_parallel for query: %s", user_query)

    ca = tool_context.state.get("catchment_analysis", {})
    if not ca:
        return "Error: catchment_analysis missing in state."

    optimal_radius = ca.get("catchment_analysis_radius")
    coords = ca.get("catchment_marker_point")

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

    gemini_prompt = f"""
    You are an AI assistant working for Reliance, India's largest retailer. Your persona is that of a strategic market analyst focusing on the Mumbai and Navi Mumbai region.
    
    Your goal is to generate appropriate search queries for the Google Places Text Search API to find relevant places specifically within the Mumbai and Navi Mumbai regions (or areas looking to expand within Mumbai).
    
    To get as much relevant information as possible for the Mumbai market and discover brands beyond the provided examples, you MUST use Google Search efficiently. 
    
    Instructions for Search and Query Generation:
    1. **Competitors for '{user_query}':**
       - The examples 'Croma, Vijay Sales' are for reference only.
       - Perform effective searches to discover major competitor brands in India, with a specific
         focus on those strong in the Mumbai/Navi Mumbai region for the category of '{user_query}'.
         Consider searching for "top competitor brands of {user_query} in Mumbai" or "major retail
         chains for [Category] in Mumbai" to find names you might not know.
       - Synthesize the search results to create a comprehensive list of major competitor brands relevant to this specific market.
       - Output a clean string of specific brand names separated by commas. Do NOT include small local shops.
    
    2. **Public Transport Hubs:**
       - Ensure queries include all major types of transit hubs relevant to the Mumbai metropolitan area (e.g., Mumbai Metro Station, Mumbai Local Train Station, Bus Depot, Inter-city Bus Terminals).
    
    3. **Restaurants & Food Hubs:**
       - Use search to identify high-footfall restaurant chains or categories popular in Mumbai (e.g., Fine Dining, QSR chains like Starbucks, McDonald's, etc.) to ensure the query yields rich results.
    
    CRITICAL CONSTRAINTS:
    1. Each query must be a clean string of search terms or brand names, separated by commas if listing multiple. Do NOT include phrases like "search for" or "identify places like" in the query string itself.
    2. Output the result strictly as a valid JSON object mapping the search query to its type (competitor, transport, or restaurant).
    3. Do not include markdown formatting in the output.
    4. Try to generate highly targeted and rich queries that will maximize the results returned by the Places API.
    
    Example Output:
    {{
      "Croma, Vijay Sales, Kohinoor, Lotus Electronics": "competitor",
      "Mumbai Metro Station, Local Train Station, Bus Depot": "transport",
      "Fine Dining Restaurants, Starbucks, 5 Star Hotels, Hard Rock Cafe": "restaurant"
    }}
    """

    try:
        output_res = get_processing_resolution(optimal_radius)  # Match BQ resolution
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
            details_parts = []
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
                    details_parts.append(f"{k}: {v}")
                    info_dict[k] = v

            details = (
                " | ".join(details_parts) if details_parts else f"name: {s.get('name')}"
            )
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
                    "info": counts,
                    "tag": "places_h3_aggregation",
                    "res": output_res,
                }
            )

        tool_context.state["catchment_analysis"] = ca

        try:
            radius = get_hex_radius(output_res)
            fetch_places_insights_h3(
                poly.wkt,
                tool_context.state,
                output_res,
                radius,
                analysis_key="catchment_analysis",
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


def get_table_desc(project_id: str, dataset_id: str, table_id: str) -> str:
    client = bigquery.Client(project=project_id)
    table_ref = f"{project_id}.{dataset_id}.{table_id}"
    try:
        table = client.get_table(table_ref)
        return table.description or ""
    except NotFound:
        return f"Error: Table {table_ref} not found."
    except Exception as e:
        return f"An error occurred: {e}"


def get_list_of_tables_in_dataset(project_id: str, dataset_id: str) -> str:
    """Lists all tables in the BigQuery dataset."""
    client = bigquery.Client(project=project_id)
    dataset_ref = f"{project_id}.{dataset_id}"
    try:
        tables = list(client.list_tables(dataset_ref))
        if not tables:
            return f"No tables found in dataset: {dataset_ref}"
        names = [t.table_id for t in tables]
        return names
    except NotFound:
        return f"Error: Dataset {dataset_ref} not found."
    except Exception as e:
        return f"An error occurred: {e}"
