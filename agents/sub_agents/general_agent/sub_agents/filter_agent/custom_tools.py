"""Custom tools for data inspection, tables sample extraction, and geospatial searching."""

import os
import logging
from typing import Optional, List
from google.cloud import bigquery
from google.adk.tools import ToolContext

from agents.common_utils import (
    convert_travel_constraint_to_radius,
    resolve_address_to_coordinates,
    resolve_pincode_to_coordinates,
    resolve_mapurl_to_coordinates,
    locate_by_coordinates,
)
from agents.sub_agents.general_agent.custom_tools import (
    _update_coordinate_state_with_res,
)
from agents.query_bq_spatial_utils import (
    fetch_bq_table_geospatial_data,
    process_table_geospatial_results,
)
from agents.common_utils import get_processing_resolution

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID")
DATASET_ID = os.getenv("DATASET_ID")
TOOL_CONTEXT_KEY = "general_analysis"


def get_table_sample_data(table_id: str) -> str:
    """
    Fetches the first 10 rows of a BigQuery table
    to inspect the actual data values, casing, and format.
    Use this tool before writing a SQL filter condition or selecting attributes
    to understand what the data looks like.

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

        return df_sample.to_json(orient="records", indent=2)
    except Exception as e:
        logger.error("Error fetching sample data for %s: %s", table_id, e)
        return f"Error fetching sample data for {table_id}: {str(e)}"


def get_table_description(table_id: str) -> str:
    """
    Fetches the description of a BigQuery table to understand its purpose and content.

    Args:
        table_id: The specific table name to inspect (e.g. 'retail_asset_master').
    """
    client = bigquery.Client(project=PROJECT_ID)
    try:
        safe_table_id = table_id.split(".")[-1]
        table_path = f"{PROJECT_ID}.{DATASET_ID}.{safe_table_id}"
        table = client.get_table(table_path)
        return table.description or "No description available."
    except Exception as e:
        logger.error("Error fetching table description for %s: %s", table_id, e)
        return f"Error fetching table description: {str(e)}"


def get_unique_column_values(
    table_id: str, column_names: List[str], tool_context: ToolContext
) -> str:
    """
    Fetches unique values from specified columns in a BigQuery table.
    Use this to understand what values exist in columns before applying filters.

    Args:
        table_id: Fully qualified table ID (PROJECT.DATASET.TABLE) or just table name.
        column_names: A list of column names to inspect.
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
                    f"Warning: Too many unique values ({unique_count}) for column '{col}'."
                )
                continue

            unique_values = row[f"{safe_alias}_vals"] or []

            if len(unique_values) > 100:
                unique_values_str = [str(v) for v in unique_values[:100]]
                all_results.append(
                    f"Warning: Found more than 100 unique values for column '{col}'. "
                    f"Showing first 100: {', '.join(unique_values_str)}"
                )
            else:
                unique_values_str = [str(v) for v in unique_values]
                all_results.append(
                    f"Unique values for column '{col}': {', '.join(unique_values_str)}"
                )

    except Exception as e:
        logger.error("Error fetching unique values in %s: %s", table_id, e)
        return f"Error fetching unique values: {str(e)}"

    return "\n\n".join(all_results)


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
    short_name: str = "Data",
) -> str:
    """
    Executes a multi-table geographic search in BigQuery, looking for points of interest
    within a specific radius of a target latitude/longitude coordinate.
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

    processing_result = process_table_geospatial_results(
        all_results=all_results,
        table_id=table_id,
        lat_column=lat_column,
        lon_column=lon_column,
        processing_resolution=processing_resolution,
    )

    if processing_result["status"] == "error":
        return processing_result["message"]
    if processing_result["status"] == "empty":
        return "DATA_NOT_FOUND"

    use_h3 = processing_result["use_h3"]

    ca = tool_context.state.get(TOOL_CONTEXT_KEY, {})
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

    tool_context.state[TOOL_CONTEXT_KEY] = ca

    try:
        from state import debug_dump_state

        debug_dump_state(
            tool_context.state, label=f"bq_filter_agent_and_update_state ({method})"
        )
    except Exception:
        pass

    return (
        f"Success! Processed {len(all_results)} points. "
        f"State was updated using {'H3 Grid Aggregation' if use_h3 else 'Markers.'}"
    )


def identify_default_radius(
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


def identify_default_coordinates(
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
        else:
            logger.warning(
                "Failed to resolve as %s, falling back to auto-detection.",
                input_type,
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
