"""
This module provides custom tools for execution by the feasibility agent,
including BigQuery data fetching and H3 grid processing.
"""

# Standard library imports
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
from typing import List, Optional

# Third-party library imports
from google.adk import Runner
from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService
from google.adk.tools import ToolContext
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery
from google import genai
import h3
from pydantic import BaseModel
try:
    from shapely import wkt
    from shapely.ops import unary_union
    SPATIAL_LIBS_AVAILABLE = True
except ImportError:
    SPATIAL_LIBS_AVAILABLE = False
    wkt = None
    unary_union = None

# First-party local library imports
from agents.places_text_search_utils import (
    search_places_in_polygon,
)
from agents.query_bq_spatial_utils import (
    fetch_bq_table_geospatial_data_polygon,
    process_table_geospatial_results,
)
from agents.sub_agents.feasibility_agent.prompt import get_filter_agent_instructions

from agents.bq_places_insights_utils import fetch_places_insights_h3, get_hex_radius

# Configure standard logger
logger = logging.getLogger(__name__)
try:
    fh = logging.FileHandler("feasibility_agent_tools.log")
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh.setFormatter(formatter)
    logger.addHandler(fh)
except Exception:
    pass

# Fetch essential environment variables required for BigQuery connections
PROJECT_ID = os.getenv("PROJECT_ID")
LOCATION = os.getenv("LOCATION")
DATASET_ID = os.getenv("DATASET_ID")


def get_table_sample_data(table_id: str) -> str:
    """
    Fetches the first 10 rows of a BigQuery table to inspect the actual data values,
    casing, and format.
    Use this tool before writing a SQL filter condition or selecting attributes to
    understand what the data looks like.

    Args:
        table_id: The specific table name to inspect (e.g. 'retail_asset_master').
    """
    client = bigquery.Client(project=PROJECT_ID)
    try:
        safe_table_id = table_id.split(".")[-1]
        table_path = f"{PROJECT_ID}.{DATASET_ID}.{safe_table_id}"
        query = f"SELECT * FROM `{table_path}` LIMIT 10"

        query_job = client.query(query)
        df_sample = query_job.to_dataframe()

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


def get_optimal_resolution(area_sq_km: float, max_hexes: int = 150) -> int:
    """
    Calculates the optimal H3 resolution to keep the expected number of hexes below max_hexes.
    """
    # Avg area in sq km for resolutions 9, 8, 7, 6
    res_areas = {9: 0.105, 8: 0.737, 7: 5.161, 6: 36.129}

    # Try resolutions from most granular to coarsest
    for res in [9, 8, 7, 6]:
        expected_hexes = area_sq_km / res_areas[res]
        if expected_hexes <= max_hexes:
            return res

    return 6  # Fallback to 6 for extremely large areas


def query_bq_table_geospatial_polygon(
    table_id: str,
    tool_context: ToolContext,
    lat_column: str,
    lon_column: str,
    select_clause: Optional[str] = None,
    filter_condition: Optional[str] = None,
    short_name: str = "Feasibility Data",
) -> str:
    """
    Executes a multi-table geographic search in BigQuery, looking for points of interest
    within a specific polygon.

    Args:
        table_id: Table to query.
        tool_context: Tool context.
        lat_column: Name of the latitude column (Required).
        lon_column: Name of the longitude column (Required).
        select_clause: Optional SELECT clause.
        filter_condition: Optional filter condition.
        short_name: Optional display name.
    """
    pa = tool_context.state.get("polygon_analysis", {})
    polygon_coords = pa.get("polygon_coords")
    if not polygon_coords:
        return "Error: polygon_coords missing in state. Please resolve pincodes first."

    try:
        from shapely.geometry import shape

        poly = shape(polygon_coords)
        polygon_wkt = poly.wkt
        area_sq_km = poly.area * 11700
        processing_resolution = get_optimal_resolution(area_sq_km)
        logger.info(
            f"Dynamic Scale chosen: Resolution {processing_resolution} "
            f"based on area {area_sq_km:.2f} sq km"
        )
    except Exception as e:
        wkt_snippet = polygon_wkt[:100] if polygon_wkt else "None"
        logger.error(
            f"Failed to calculate dynamic resolution from polygon: {e}. "
            f"Defaulting to None. WKT snippet: {wkt_snippet}..."
        )
        processing_resolution = None

    all_results = fetch_bq_table_geospatial_data_polygon(
        polygon_wkt=polygon_wkt,
        table_id=table_id,
        lat_column=lat_column,
        lon_column=lon_column,
        select_clause=select_clause,
        filter_condition=filter_condition,
    )

    processing_result = process_table_geospatial_results(
        all_results=all_results,
        table_id=table_id,
        lat_column="latitude",
        lon_column="longitude",
        processing_resolution=processing_resolution,
        use_h3_only=True,
        radius_poly=poly,
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

    pa = tool_context.state.get("polygon_analysis", {})
    if hasattr(pa, "model_dump"):
        pa = pa.model_dump()
    if "dataset_insights" not in pa:
        pa["dataset_insights"] = {}

    display_name = table_id.split(".")[-1].replace("_", " ").title()

    if use_h3:
        hex_codes = processing_result["hex_codes"]
        if "hex_dataset_ids" not in pa["dataset_insights"]:
            pa["dataset_insights"]["hex_dataset_ids"] = []
        if table_id not in pa["dataset_insights"]["hex_dataset_ids"]:
            pa["dataset_insights"]["hex_dataset_ids"].append(table_id)

        if table_id not in pa["dataset_insights"]:
            pa["dataset_insights"][table_id] = {}
        pa["dataset_insights"][table_id]["dataset_details"] = display_name

        if "hex_codes" not in pa["dataset_insights"][table_id]:
            pa["dataset_insights"][table_id]["hex_codes"] = []
        pa["dataset_insights"][table_id]["hex_codes"].extend(hex_codes)
    else:
        markers = processing_result["markers"]
        if "marker_dataset_ids" not in pa["dataset_insights"]:
            pa["dataset_insights"]["marker_dataset_ids"] = []
        if table_id not in pa["dataset_insights"]["marker_dataset_ids"]:
            pa["dataset_insights"]["marker_dataset_ids"].append(table_id)

        if table_id not in pa["dataset_insights"]:
            pa["dataset_insights"][table_id] = {}
        pa["dataset_insights"][table_id]["dataset_details"] = display_name

        if "markers" not in pa["dataset_insights"][table_id]:
            pa["dataset_insights"][table_id]["markers"] = []
        pa["dataset_insights"][table_id]["markers"].extend(markers)

    tool_context.state["polygon_analysis"] = pa

    method = "H3 Grid Aggregation" if use_h3 else "Markers."
    return f"Success! Processed {len(all_results)} points. State was updated using {method}"


def get_unique_column_values(
    table_id: str, column_names: List[str], tool_context: ToolContext
) -> str:
    """
    Fetches unique values from specified columns in a BigQuery table.
    """
    client = bigquery.Client(project=PROJECT_ID)

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
                    f"Please do not run this tool for high cardinality columns."
                )
                continue

            unique_values = row[f"{safe_alias}_vals"] or []

            if len(unique_values) > 100:
                unique_values_str = [str(v) for v in unique_values[:100]]
                all_results.append(
                    f"Warning: Found more than 100 unique values for column '{col}'. "
                    f"Found {unique_count}. Showing first 100: {', '.join(unique_values_str)}"
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


def setup_feasibility_polygon(pincodes: str, tool_context: ToolContext) -> str:
    """
    Resolves a list of comma-separated pincodes or area names into a single unioned polygon
    and stores its WKT in the tool context state.
    Use this tool FIRST before running feasibility analysis to define the search area.

    Args:
        pincodes: Comma-separated string of pincodes (e.g., "400066, 400091") or area names.
        tool_context: ToolContext to access and update state.
    """
    client = bigquery.Client(project=PROJECT_ID)
    table_id = f"{PROJECT_ID}.{DATASET_ID}.pincode_boundaries_mmr"

    cleaned_pincodes = re.sub(
        r"\s*\b(?:and|&)\b\s*", ",", str(pincodes), flags=re.IGNORECASE
    )
    major_tokens = [t.strip() for t in cleaned_pincodes.split(",") if t.strip()]

    digits = []
    names = []

    for token in major_tokens:
        if " " in token:
            sub_tokens = [st.strip() for st in token.split() if st.strip()]
            if all(st.isdigit() for st in sub_tokens):
                digits.extend(sub_tokens)
            else:
                names.append(token)
        else:
            if token.isdigit():
                digits.append(token)
            else:
                names.append(token)

    conditions = []
    if digits:
        conditions.append(f"Pincode IN ({', '.join(digits)})")
    if names:
        for n in names:
            n_clean = n.lower()
            if n_clean == "mumbai":
                conditions.append(
                    "( (LOWER(Office_Name) LIKE '%mumbai%' OR "
                    "LOWER(Division) LIKE '%mumbai%') AND "
                    "LOWER(Division) NOT LIKE '%navi mumbai%' )"
                )
            else:
                conditions.append(
                    f"(LOWER(Office_Name) LIKE '%{n_clean}%' OR LOWER(Division) LIKE '%{n_clean}%')"
                )

    if not conditions:
        return "Error: No valid pincodes or area names provided."

    where_clause = " OR ".join(conditions)
    query = f"SELECT geometry_wkt FROM `{table_id}` WHERE {where_clause}"

    try:
        query_job = client.query(query)
        rows = list(query_job.result())
        if not rows:
            return f"Error: No boundary found for inputs '{pincodes}' in BigQuery."

        polygons = []
        for row in rows:
            if row["geometry_wkt"]:
                polygons.append(wkt.loads(row["geometry_wkt"]))

        if not polygons:
            return "Error: No valid geometries found for the provided inputs."

        unioned_poly = unary_union(polygons)

        # Clear previous analysis data to prevent state accumulation across different runs
        pa = {"polygon_coords": unioned_poly.__geo_interface__}
        tool_context.state["polygon_analysis"] = pa

        from state import debug_dump_state

        debug_dump_state(tool_context.state)

        return (
            f"Success! Resolved {len(polygons)} regions into a single polygon. "
            f"Area: {unioned_poly.area:.6f} sq degrees. State updated."
        )
    except Exception as e:
        logger.error("Error resolving pincodes %s: %s", pincodes, e)
        return f"Error: Failed to resolve pincodes due to: {e}"


def fetch_feasibility_data_parallel(user_query: str, tool_context: ToolContext) -> str:
    """
    Queries multiple BigQuery tables in parallel using ThreadPoolExecutor.
    """
    client = genai.Client(vertexai=True, project=PROJECT_ID, location="us-central1")
    table_list = get_list_of_tables_in_dataset(PROJECT_ID, DATASET_ID)
    table_list = [t for t in table_list if t != "pincode_boundaries_mmr"]

    pa = tool_context.state.get("polygon_analysis", {})
    polygon_coords = pa.get("polygon_coords")

    if not polygon_coords:
        return "Error: polygon_coords missing in state. Please resolve pincodes first."

    filter_instructions = get_filter_agent_instructions(
        user_query=user_query, PROJECT_ID=PROJECT_ID, DATASET_ID=DATASET_ID
    )

    filter_agent = Agent(
        name="filter_and_get_nearby_points_agent",
        model=os.getenv("MODEL"),
        instruction=filter_instructions,
        tools=[
            query_bq_table_geospatial_polygon,
            get_table_sample_data,
            get_unique_column_values,
        ],
    )

    def _process_single_table(table_id: str) -> dict:
        table_desc = get_table_desc(PROJECT_ID, DATASET_ID, table_id)

        async def run_subagent():
            session_service = InMemorySessionService()
            initial_state = {"polygon_analysis": {"polygon_coords": polygon_coords}}
            await session_service.create_session(
                app_name="retailvista",
                user_id="user",
                session_id=f"session_{table_id}",
                state=initial_state,
            )

            runner = Runner(
                agent=filter_agent,
                app_name="retailvista",
                session_service=session_service,
            )

            input_text = f"""
            For table: {table_id} please perform feasibility analysis.
            The polygon area is stored in the state. 
            Table schema for which I want to filter currently: {table_desc}
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
            spa = {}
            if session and hasattr(session, "state"):
                spa = session.state.get("polygon_analysis", {})
            elif session and isinstance(session, dict):
                spa = session.get("state", {}).get("polygon_analysis", {})

            return {"table_id": table_id, "status": "Success", "polygon_analysis": spa}

        res = asyncio.run(run_subagent())
        return res

    results = []
    with ThreadPoolExecutor(max_workers=len(table_list)) as executor:
        futures = [executor.submit(_process_single_table, t) for t in table_list]
        for future in futures:
            results.append(future.result())

    pa = tool_context.state.get("polygon_analysis") or {}
    if "dataset_insights" not in pa:
        pa["dataset_insights"] = {}

    for r in results:
        if r.get("status") == "Success" and r.get("polygon_analysis"):
            tid = r.get("table_id", "")
            pa_sub = r.get("polygon_analysis", {})
            di = pa_sub.get("dataset_insights", {})
            table_data = {}
            for key, val in di.items():
                if key == tid or key.endswith(f".{tid}"):
                    table_data = val
                    break

            mode = "H3" if "hex_codes" in table_data else "Marker"
            data = table_data.get("hex_codes") or table_data.get("markers") or []
            sname = table_data.get("dataset_details", "Feasibility Data")

            if tid not in pa["dataset_insights"]:
                pa["dataset_insights"][tid] = {}
            pa["dataset_insights"][tid]["dataset_details"] = sname

            if mode == "H3":
                if "hex_dataset_ids" not in pa["dataset_insights"]:
                    pa["dataset_insights"]["hex_dataset_ids"] = []
                if tid not in pa["dataset_insights"]["hex_dataset_ids"]:
                    pa["dataset_insights"]["hex_dataset_ids"].append(tid)
                pa["dataset_insights"][tid]["hex_codes"] = data
            else:
                if "marker_dataset_ids" not in pa["dataset_insights"]:
                    pa["dataset_insights"]["marker_dataset_ids"] = []
                if tid not in pa["dataset_insights"]["marker_dataset_ids"]:
                    pa["dataset_insights"]["marker_dataset_ids"].append(tid)
                pa["dataset_insights"][tid]["markers"] = data

    tool_context.state["polygon_analysis"] = pa

    summary_results = []
    for r in results:
        summary_item = {k: v for k, v in r.items() if k != "data"}
        summary_results.append(summary_item)

    return f"Processed {len(table_list)} tables. Summary: {json.dumps(summary_results, indent=2)}"


def fetch_places_data_parallel(brand_name: str, tool_context: ToolContext) -> str:
    """
    Fetches places of interest (competitors, transport hubs, restaurants) in
    parallel within the polygon.

    Args:
        brand_name: The name of the brand to find competitors for (e.g., 'Reliance Digital').
        tool_context: The tool context containing state.
    """
    logger.info("Starting fetch_places_data_parallel for brand: %s", brand_name)

    pa = tool_context.state.get("polygon_analysis", {})
    if not pa:
        return "Error: polygon_analysis missing in state."

    polygon_coords = pa.get("polygon_coords")
    if not polygon_coords:
        return "Error: polygon_coords missing in state."

    try:
        from shapely.geometry import shape

        poly = shape(polygon_coords)
    except Exception as e:
        return f"Error parsing polygon GeoJSON: {e}"

    gemini_prompt = f"""
    You are an AI assistant working for Reliance, India's largest retailer. Your persona is that of a strategic market analyst focusing on the Mumbai and Navi Mumbai region.
    
    To get as much relevant information as possible for the Mumbai market and discover brands beyond the provided examples, you MUST use Google Search efficiently. 
    
    Instructions for Search and Query Generation:
    1. **Competitors for '{brand_name}':**
       - The examples 'Croma, Vijay Sales' are for reference only.
       - Perform effective searches to discover major competitor brands in India, with a
         specific focus on those strong in the Mumbai/Navi Mumbai region for the
         category of '{brand_name}'. Consider searching for "top competitor brands of
         {brand_name} in Mumbai" or "major retail chains for [Category] in Mumbai" to
         find names you might not know.
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
        # Dynamic resolution based on polygon span
        area_sq_km = poly.area * 11700
        output_res = get_optimal_resolution(area_sq_km)

        results = search_places_in_polygon(
            poly, gemini_prompt=gemini_prompt, output_res=output_res
        )

        if "places_insights" not in pa:
            pa["places_insights"] = {"markers": [], "hex_codes": []}

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

            pa["places_insights"]["markers"].append(
                {
                    "lat": float(s["lat"]),
                    "long": float(s["lng"]),
                    "details": details,
                    "info": info_dict,
                    "tag": q_type,
                }
            )

            h_id = s.get("hex_id")
            if h_id:
                if h_id not in hex_data_map:
                    hex_data_map[h_id] = {}
                hex_data_map[h_id][q_type] = hex_data_map[h_id].get(q_type, 0) + 1

        for h_id, counts in hex_data_map.items():
            summary_parts = [f"{k}: {v}" for k, v in counts.items()]
            details_str = ", ".join(summary_parts)

            pa["places_insights"]["hex_codes"].append(
                {
                    "hex_id": h_id,
                    "details": details_str,
                    "tag": "places_h3_aggregation",
                    "res": output_res,
                }
            )

        tool_context.state["polygon_analysis"] = pa

        from state import debug_dump_state

        debug_dump_state(tool_context.state)

        try:
            radius = get_hex_radius(output_res)
            fetch_places_insights_h3(
                poly.wkt,
                tool_context.state,
                output_res,
                radius,
                analysis_key="polygon_analysis",
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
        return f"Error parsing coordinates or radius: {e}"


class FeasibilityReportOutput(BaseModel):
    report_markdown: str
    top_hex_ids: List[str]


def generate_feasibility_report(
    tool_context: ToolContext,
    store_type: str = "Retail Store",
    area_name: str = "Mumbai",
    top_n: int = 3,
) -> str:
    """
    Generates the final feasibility report by aggregating data from all tables
    and ranking the top picks.
    Use this tool LAST after fetching all data.

    Args:
        store_type: Type of store (e.g., "Reliance Digital", "Cafe").
        area_name: The name of the area/region being analyzed (e.g., "Borivali", "Bandra").
        top_n: Number of top locations to recommend.
    """
    pa = tool_context.state.get("polygon_analysis", {})
    if not pa:
        return "Error: polygon_analysis missing in state."

    di = pa.get("dataset_insights", {})
    places_insights = pa.get("places_insights", {})

    # Identify and drop uninhabited hexes (Building count < 15 AND 0 Places POIs)
    hexes_to_drop = set()
    places_hex_ids = set(
        item.get("hex_id") for item in places_insights.get("hex_codes", [])
    )

    for tid, table_data in di.items():
        if "ghs_obat_building_density_india_master" in tid.lower():
            for item in table_data.get("hex_codes", []):
                h_id = item.get("hex_id")
                poi_count = item.get("info", {}).get("poi_count", 0)
                if poi_count < 15 and h_id not in places_hex_ids:
                    hexes_to_drop.add(h_id)
                    logger.info(
                        f"Dropping hex {h_id} (Building count {poi_count} < 15 and 0 Places POIs)"
                    )

    # Apply filter to state to clean up UI and report
    if hexes_to_drop:
        logger.info("Total hexes dropped: %s", len(hexes_to_drop))
        for tid, table_data in di.items():
            if "hex_codes" in table_data:
                table_data["hex_codes"] = [
                    h
                    for h in table_data["hex_codes"]
                    if h.get("hex_id") not in hexes_to_drop
                ]
        if "hex_codes" in places_insights:
            places_insights["hex_codes"] = [
                h
                for h in places_insights["hex_codes"]
                if h.get("hex_id") not in hexes_to_drop
            ]

    hex_profiles = {}

    for tid, table_data in di.items():
        if tid in ["hex_dataset_ids", "marker_dataset_ids"]:
            continue

        # Process hex_codes if available
        hex_codes = table_data.get("hex_codes", [])
        for item in hex_codes:
            h_id = item.get("hex_id")
            details = item.get("details", "")
            if h_id not in hex_profiles:
                hex_profiles[h_id] = {"details": []}
            hex_profiles[h_id]["details"].append(f"[{tid}] {details}")

        # Process markers if available (fallback)
        markers = table_data.get("markers", [])
        for item in markers:
            lat = item.get("lat")
            lon = item.get("long")
            details = item.get("details", "")
            if lat and lon:
                res = 8
                is_new_h3 = hasattr(h3, "latlng_to_cell")
                h3_func = h3.latlng_to_cell if is_new_h3 else h3.geo_to_h3
                try:
                    h_id = h3_func(float(lat), float(lon), res)
                    if h_id not in hex_profiles:
                        hex_profiles[h_id] = {"details": []}
                    hex_profiles[h_id]["details"].append(f"[{tid}] {details}")
                except Exception as e:
                    logger.error(
                        "Failed to convert marker to H3 for table %s: %s", tid, e
                    )

    # Process places_insights with summarization
    places_markers = places_insights.get("markers", [])
    places_by_hex = {}
    for item in places_markers:
        lat = item.get("lat")
        lon = item.get("long")
        details = item.get("details", "")
        tag = item.get("tag", "unknown")
        if lat and lon:
            res = 8
            is_new_h3 = hasattr(h3, "latlng_to_cell")
            h3_func = h3.latlng_to_cell if is_new_h3 else h3.geo_to_h3
            try:
                h_id = h3_func(float(lat), float(lon), res)
                if h_id not in places_by_hex:
                    places_by_hex[h_id] = {}
                if tag not in places_by_hex[h_id]:
                    places_by_hex[h_id][tag] = []
                places_by_hex[h_id][tag].append(details)
            except Exception as e:
                logger.error("Failed to convert place marker to H3: %s", e)

    for h_id, tags in places_by_hex.items():
        summary_parts = []
        for tag, details_list in tags.items():
            names = []
            for d in details_list:
                parts = d.split(" | ")
                name_part = parts[0] if parts else d
                if name_part.startswith("name: "):
                    names.append(name_part[6:])
                else:
                    names.append(name_part)
            names_str = ", ".join(names[:3])
            if len(names) > 3:
                names_str += f" and {len(names) - 3} more"
            summary_parts.append(
                f"{tag.capitalize()}: {len(details_list)} ({names_str})"
            )

        if h_id not in hex_profiles:
            hex_profiles[h_id] = {"details": []}
        hex_profiles[h_id]["details"].append(f"[Places] {' | '.join(summary_parts)}")

    for h_id, profile in hex_profiles.items():
        profile["details_summary"] = " | ".join(profile["details"])

    if not hex_profiles:
        return "Error: No data found to analyze."

    client = genai.Client(vertexai=True, project=PROJECT_ID, location="us-central1")
    profiles_text = json.dumps(hex_profiles, indent=2)

    prompt = f"""
    You are a Strategic Business Expansion Consultant. Write your final report in a professional, insightful, and strategic tone.
    
    IMPORTANT: Do NOT include standard memo headers such as "TO:", "FROM:", "DATE:", or "SUBJECT:" at the beginning of the report. Start directly with the main title.
    
    Here are evaluated micro-market (hexagon) profiles for a new {store_type} in {area_name}:
    {profiles_text}
    
    Select the Top {top_n} best locations. Evaluate them HOLISTICALLY based on the data.
    - High Density & Spending Capacity: Prioritize areas with high values in building density or wealth tables.
    - Favorable Competitive Landscape: Look at Places data for competitors.
    
    Output a comprehensive, beautifully formatted Markdown response.
    
    At the very end of your response, provide a JSON block containing the list of selected H3 Hex IDs.
    Example:
    ```json
    {{
      "top_hex_ids": ["88608b4735fffff", "88608b442bfffff"]
    }}
    ```

    Include exact headings in the report:
    # Site Selection Analysis: Top {top_n} Strategic Locations in {area_name}
    ## Methodology
    ## Contrast Analysis: Winners vs. Disqualified Zones
    ## Top {top_n} Selections: Strategic In-Depth Analysis
    """

    try:
        response = client.models.generate_content(
            model=os.getenv("MODEL"), contents=prompt
        )

        full_text = response.text

        # Extract JSON block for top_hex_ids
        top_hexes = []
        try:
            json_match = re.search(r"```json\s*(.*?)\s*```", full_text, re.DOTALL)
            if json_match:
                json_data = json.loads(json_match.group(1))
                top_hexes = json_data.get("top_hex_ids", [])
            else:
                # Fallback: search for H3 hex IDs in the text using regex
                h3_pattern = r"8[0-9a-fA-F]{14}"
                top_hexes = re.findall(h3_pattern, full_text)
                top_hexes = list(dict.fromkeys(top_hexes))[:top_n]
        except Exception as je:
            logger.warning("Failed to parse top_hex_ids from JSON block: %s", je)
            h3_pattern = r"8[0-9a-fA-F]{14}"
            top_hexes = re.findall(h3_pattern, full_text)
            top_hexes = list(dict.fromkeys(top_hexes))[:top_n]

        # Clean the report by removing the JSON block
        report = re.sub(r"```json\s*.*?\s*```", "", full_text, flags=re.DOTALL).strip()
        if len(report) < 100:
            report = full_text

        # Save top hexes to state for frontend
        if "polygon_analysis" not in tool_context.state:
            tool_context.state["polygon_analysis"] = {}
        pa = dict(tool_context.state["polygon_analysis"])
        pa["top_n"] = top_hexes
        tool_context.state["polygon_analysis"] = pa

        # Reverse Geocode top hexes and replace in report
        for i, hex_id in enumerate(top_hexes):
            try:
                lat, lon = (
                    h3.cell_to_latlng(hex_id)
                    if hasattr(h3, "cell_to_latlng")
                    else h3.h3_to_geo(hex_id)
                )

                # Initialize maps client
                MAPS_API_KEY = os.getenv("MAPS_API_KEY")
                import googlemaps

                gmaps = googlemaps.Client(key=MAPS_API_KEY)

                reverse_geocode_result = gmaps.reverse_geocode((lat, lon))
                if reverse_geocode_result:
                    address = reverse_geocode_result[0].get(
                        "formatted_address", f"({lat:.4f}, {lon:.4f})"
                    )
                else:
                    address = f"({lat:.4f}, {lon:.4f})"
            except Exception as ge:
                logger.error("Failed to reverse geocode %s: %s", hex_id, ge)
                address = f"Hex {hex_id}"

            # Replace hex_id with address in the report
            report = report.replace(hex_id, f"**{address}**")

        # Save result to state for frontend
        pa["result"] = report
        tool_context.state["polygon_analysis"] = pa
        return report

    except Exception as e:
        logger.error("Failed to generate report: %s", e)
        return f"Error: Failed to generate report due to: {e}"


def get_table_desc(project_id: str, dataset_id: str, table_id: str) -> str:
    """Fetches the description of a specific BigQuery table."""
    client = bigquery.Client(project=project_id)
    table_ref = f"{project_id}.{dataset_id}.{table_id}"
    try:
        table = client.get_table(table_ref)
        return table.description or ""
    except api_exceptions.GoogleAPICoreError as e:
        return f"An error occurred: {e}"


def get_list_of_tables_in_dataset(project_id: str, dataset_id: str) -> str:
    """Lists the IDs of all tables in a given BigQuery dataset."""
    client = bigquery.Client(project=project_id)
    dataset_ref = f"{project_id}.{dataset_id}"
    try:
        tables = list(client.list_tables(dataset_ref))
        if not tables:
            return f"No tables found in dataset: {dataset_ref}"
        names = [t.table_id for t in tables]
        return names
    except api_exceptions.GoogleAPICoreError as e:
        return f"An error occurred: {e}"
