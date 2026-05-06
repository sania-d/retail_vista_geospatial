"""Utilities for performing spatial queries against BigQuery tables."""

import os
import json
import logging

from typing import List, Dict, Any, Optional
from google.cloud import bigquery
import h3
try:
    import shapely.geometry as sg
    SPATIAL_LIBS_AVAILABLE = True
except ImportError:
    SPATIAL_LIBS_AVAILABLE = False
    class sg:
        class Polygon:
            pass
        class MultiPolygon:
            pass
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID")
DATASET_ID = os.getenv("DATASET_ID")


def create_sql_query_with_radius(
    lat_column: str,
    lon_column: str,
    table_path: str,
    select_clause: str = None,
    filter_condition: dict = None,
) -> str:
    """
    Generates a parameterized BigQuery standard SQL query for spatial filtering.
    """
    raw_select = str(select_clause or "").strip().replace('"', "")
    is_wildcard = not raw_select or raw_select == "*"
    if is_wildcard:
        base_cols = f"* EXCEPT({lat_column}, {lon_column})"
    else:
        col_list = [c.strip() for c in raw_select.split(",") if c.strip()]
        filtered_list = [
            f"`{c.strip('`')}`"
            for c in col_list
            if c.lower() not in ("latitude", "longitude", "*")
        ]
        base_cols = ", ".join(filtered_list)

    if base_cols:
        select_clause = (
            f"{base_cols}, {lat_column} AS latitude, {lon_column} AS longitude"
        )
    else:
        select_clause = f"{lat_column} AS latitude, {lon_column} AS longitude"

    if (
        (filter_condition or str(filter_condition).strip())
        and str(filter_condition).strip() != "None"
        and str(filter_condition).strip() != ""
    ):
        cond = str(filter_condition).strip()
        if cond.upper().startswith("AND ") or cond.upper().startswith("OR "):
            filter_clause = f" {cond}"
        else:
            filter_clause = f" AND {cond}"
    else:
        filter_clause = ""

    query = f"""
        SELECT 
            {select_clause},
            ST_DISTANCE(
                SAFE.ST_GEOGPOINT({lon_column}, {lat_column}),
                ST_GEOGPOINT(@lon, @lat)
            ) as distance_from_center
        FROM `{table_path}`
        WHERE 
            SAFE.ST_GEOGPOINT({lon_column}, {lat_column}) IS NOT NULL
            AND ST_DWITHIN(
                SAFE.ST_GEOGPOINT({lon_column}, {lat_column}),
                ST_GEOGPOINT(@lon, @lat), 
                @radius
            ){filter_clause}
        ORDER BY distance_from_center ASC
    """
    return query


def create_sql_query_with_polygon(
    table_path: str,
    lat_column: str,
    lon_column: str,
    select_clause: str = None,
    filter_condition: str = None,
) -> str:
    """
    Generates a parameterized BigQuery standard SQL query for spatial filtering within a polygon.
    """
    raw_select = str(select_clause or "").strip().replace('"', "")
    is_wildcard = not raw_select or raw_select == "*"

    if is_wildcard:
        base_cols = f"* EXCEPT({lat_column}, {lon_column})"
    else:
        col_list = [c.strip() for c in raw_select.split(",") if c.strip()]
        exclude_list = ["latitude", "longitude", "*"]
        exclude_list.append(lat_column.lower())
        exclude_list.append(lon_column.lower())

        filtered_list = [
            f"`{c.strip('`')}`" for c in col_list if c.lower() not in exclude_list
        ]
        base_cols = ", ".join(filtered_list)

    if base_cols:
        select_clause = (
            f"{base_cols}, {lat_column} AS latitude, {lon_column} AS longitude"
        )
    else:
        select_clause = f"{lat_column} AS latitude, {lon_column} AS longitude"

    if (
        (filter_condition or str(filter_condition).strip())
        and str(filter_condition).strip() != "None"
        and str(filter_condition).strip() != ""
    ):
        cond = str(filter_condition).strip()
        if cond.upper().startswith("AND ") or cond.upper().startswith("OR "):
            filter_clause = f" {cond}"
        else:
            filter_clause = f" AND {cond}"
    else:
        filter_clause = ""

    where_clause = (
        f"SAFE.ST_GEOGPOINT({lon_column}, {lat_column}) IS NOT NULL AND "
        f"ST_CONTAINS(ST_GEOGFROMTEXT(@polygon_wkt), SAFE.ST_GEOGPOINT({lon_column}, {lat_column}))"
    )

    query = f"""
        SELECT 
            {select_clause}
        FROM `{table_path}`
        WHERE 
            {where_clause}{filter_clause}
    """
    return query


def fetch_bq_table_geospatial_data(
    target_lat: float,
    target_lon: float,
    optimal_radius_meters: float,
    table_id: str,
    lat_column: str = "latitude",
    lon_column: str = "longitude",
    select_clause: Optional[str] = None,
    filter_condition: Optional[str] = None,
) -> List[Dict]:
    """
    Fetches geospatial data from BigQuery for a given table and radius.
    """
    print(
        "inside fetch_bq_table_geospatial_data",
        target_lat,
        target_lon,
        optimal_radius_meters,
    )
    client = bigquery.Client(project=PROJECT_ID)
    all_results = []

    parsed_filter = filter_condition
    if (
        filter_condition
        and isinstance(filter_condition, str)
        and filter_condition.strip().startswith("{")
    ):
        try:
            parsed_filter = json.loads(filter_condition)
        except Exception:
            pass

    safe_table_id = table_id.split(".")[-1]
    table_path = f"{PROJECT_ID}.{DATASET_ID}.{safe_table_id}"

    table_filter = (
        parsed_filter.get(table_id)
        if isinstance(parsed_filter, dict)
        else parsed_filter
    )

    query = create_sql_query_with_radius(
        lat_column, lon_column, table_path, select_clause, table_filter
    )
    print(
        "==================== DEBUG SQL QUERY ====================\n"
        f"{query}\n"
        "========================================================="
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("lon", "FLOAT64", float(target_lon)),
            bigquery.ScalarQueryParameter("lat", "FLOAT64", float(target_lat)),
            bigquery.ScalarQueryParameter(
                "radius", "FLOAT64", float(optimal_radius_meters)
            ),
        ]
    )

    try:
        logger.info(
            f"Querying BQ table {table_path} with radius {optimal_radius_meters}m..."
        )
        query_job = client.query(query, job_config=job_config)
        df_filtered = query_job.to_dataframe()
        records = df_filtered.to_json(orient="records", double_precision=10)
        all_results.extend(json.loads(records))
    except Exception as e:
        err_msg = (
            f"BigQuery Syntax Error in table {table_path}. "
            "Please check your filter_condition SQL syntax and try again. "
            f"Error: {str(e)}"
        )
        logger.error(err_msg)
        all_results.append({"error": err_msg})

    return all_results


def fetch_bq_table_geospatial_data_polygon(
    polygon_wkt: str,
    table_id: str,
    lat_column: str,
    lon_column: str,
    select_clause: Optional[str] = None,
    filter_condition: Optional[str] = None,
) -> List[Dict]:
    """
    Fetches geospatial data from BigQuery for a given table within a polygon.
    """
    client = bigquery.Client(project=PROJECT_ID)
    all_results = []

    parsed_filter = filter_condition
    if (
        filter_condition
        and isinstance(filter_condition, str)
        and filter_condition.strip().startswith("{")
    ):
        try:
            parsed_filter = json.loads(filter_condition)
        except Exception:
            pass

    safe_table_id = table_id.split(".")[-1]
    table_path = f"{PROJECT_ID}.{DATASET_ID}.{safe_table_id}"

    table_filter = (
        parsed_filter.get(table_id)
        if isinstance(parsed_filter, dict)
        else parsed_filter
    )

    query = create_sql_query_with_polygon(
        table_path=table_path,
        lat_column=lat_column,
        lon_column=lon_column,
        select_clause=select_clause,
        filter_condition=table_filter,
    )
    print(
        "==================== DEBUG SQL QUERY ====================\n"
        f"{query}\n"
        "========================================================="
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("polygon_wkt", "STRING", polygon_wkt),
        ]
    )

    try:
        logger.info(f"Querying BQ table {table_path} within polygon...")
        query_job = client.query(query, job_config=job_config)
        df_filtered = query_job.to_dataframe()
        records = df_filtered.to_json(orient="records", double_precision=10)
        all_results.extend(json.loads(records))
    except Exception as e:
        err_msg = (
            f"BigQuery Syntax Error in table {table_path}. "
            "Please check your filter_condition SQL syntax and try again. "
            f"Error: {str(e)}"
        )
        logger.error(err_msg)
        all_results.append({"error": err_msg})

    return all_results


def calculate_distance_between_two_hexagons(row):
    # Convert hex IDs to Lat/Lon coordinates
    coords1 = h3.cell_to_latlng(row["h3_index_x"])
    coords2 = h3.cell_to_latlng(row["h3_index_y"])

    # Calculate distance in meters ('km' for kilometers)
    return h3.great_circle_distance(coords1, coords2, unit="m")


def calculate_h3_distance(row):
    # Get the center of the current hexagon
    hex_lat, hex_lon = h3.cell_to_latlng(row["h3_index"])
    # Calculate distance in meters
    return h3.great_circle_distance(
        (row["target_lat"], row["target_lon"]), (hex_lat, hex_lon), unit="m"
    )


def fill_polygon_with_h3(polygon, res):
    if isinstance(polygon, sg.MultiPolygon):
        print(f"\n\nInside if fill_polygon_with_h3 \n")
        all_hexagons = set()
        for single_poly in polygon.geoms:
            all_hexagons.update(fill_polygon_with_h3(single_poly, res))
        return list(all_hexagons)

    # 1. H3 v4 expects Latitude/Longitude order
    # Extract outer ring and flip from (lon, lat) to (lat, lon)
    outer_ring = [(lat, lon) for lon, lat in polygon.exterior.coords]

    # 2. Extract holes (interiors) and flip those too
    holes = []
    for interior in polygon.interiors:
        holes.append([(lat, lon) for lon, lat in interior.coords])

    # 3. Create the H3-specific "Poly" structure
    # In v4, if the Polygon class is missing, we use h3.LatLngPoly
    # or pass the coordinates to the engine via the geojson_to_geometry logic
    try:
        # Try the LatLngPoly constructor if Polygon is missing
        poly_obj = h3.LatLngPoly(outer_ring, *holes)
    except AttributeError:
        # If that also fails, use the geojson conversion utility
        # which is built into the v4 core
        poly_geojson = {
            "type": "Polygon",
            "coordinates": [
                [
                    (lon, lat) for lat, lon in outer_ring
                ],  # Standard GeoJSON is [lon, lat]
                [[(lon, lat) for lat, lon in hole] for hole in holes],
            ],
        }
        # In v4, this creates the internal 'H3Shape' object
        poly_obj = h3.geojson_to_geometry(poly_geojson)

    # 4. Fill the polygon
    hexagons = h3.polygon_to_cells(poly_obj, res)

    return list(hexagons)


def impute_missing_h3_data(
    grouped_df: pd.DataFrame, radius_poly: sg.Polygon, res: int
) -> pd.DataFrame:
    """
    Generates a complete H3 grid for a given circle and fills missing data
    using nearest neighbor imputation.
    """
    base_hexes = fill_polygon_with_h3(radius_poly, res)

    base_df = pd.DataFrame({"h3_index": base_hexes})
    print(f"Total hexes::: {base_df.shape[0]}")

    grouped_df["h3_index"] = grouped_df.index.astype(str)
    hax_codes_list = grouped_df["h3_index"].tolist()
    base_df["h3_index"] = base_df["h3_index"].astype(str)

    # 4. Cross Join the real queried data ONTO our universal full grid
    grouped = base_df.merge(grouped_df, how="cross")
    grouped["distance_meters"] = grouped.apply(
        calculate_distance_between_two_hexagons, axis=1
    )

    grouped["h3_index"] = grouped["h3_index_x"]
    grouped = (
        grouped.sort_values("distance_meters")
        .drop_duplicates(subset=["h3_index_x"], keep="first")
        .reset_index(drop=True)
    )
    print(f"grouped.shape after drop: {grouped.shape} \n {grouped.columns.tolist()}")

    centroid = radius_poly.centroid
    target_lon = centroid.x
    target_lat = centroid.y
    grouped["target_lon"] = target_lon
    grouped["target_lat"] = target_lat
    grouped["distance_meters_from_centre"] = grouped.apply(
        calculate_h3_distance, axis=1
    )

    # The condition: h3_index is NOT in your list
    mask = ~grouped["h3_index"].isin(hax_codes_list)

    # Update columns 'distance_from_center_ mean,max,and min' only for those rows
    grouped.loc[mask, "distance_from_center_mean"] = grouped.loc[
        mask, "distance_meters_from_centre"
    ]
    grouped.loc[mask, "distance_from_center_max"] = grouped.loc[
        mask, "distance_meters_from_centre"
    ]
    grouped.loc[mask, "distance_from_center_min"] = grouped.loc[
        mask, "distance_meters_from_centre"
    ]

    # This fills EVERY null in those columns using the distance_meters_from_centre value
    for col in [
        "distance_from_center_mean",
        "distance_from_center_max",
        "distance_from_center_min",
    ]:
        grouped[col] = grouped[col].fillna(grouped["distance_meters_from_centre"])

    print(f"grouped.shape: {grouped.shape} \n {grouped.columns.tolist()}")
    grouped.set_index("h3_index", inplace=True)
    grouped.drop(
        columns=[
            "h3_index_x",
            "h3_index_y",
            "distance_meters_from_centre",
            "distance_meters",
            "target_lon",
            "target_lat",
        ],
        errors="ignore",
        inplace=True,
    )
    return grouped


def process_table_geospatial_results(
    all_results: List[Dict],
    table_id: str,
    processing_resolution: int = 8,
    lat_column: str = "latitude",
    lon_column: str = "longitude",
    use_h3_only: bool = False,
    radius_poly: sg.Polygon = None,
) -> Dict[str, Any]:
    """
    Processes geospatial results and decides whether to aggregate as H3 or Markers.
    """
    df = pd.DataFrame(all_results)

    if "error" in df.columns and len(df.columns) == 1:
        return {
            "status": "error",
            "message": f"Cannot process state - the query failed: {df['error'].iloc[0]}",
        }

    if df.empty:
        return {"status": "empty"}

    ignored_keys = [
        lat_column.lower(),
        lon_column.lower(),
        "latitude",
        "longitude",
        "zipcode",
        "pincode",
        "id",
        "place_id",
    ]
    numeric_cols = []

    for col in df.select_dtypes(include=[np.number]).columns:
        if col.lower() not in ignored_keys:
            numeric_cols.append(col)

    count = len(df)
    has_numerics = len(numeric_cols) > 0

    safe_table_id = table_id.split(".")[-1]
    table_path = f"{PROJECT_ID}.{DATASET_ID}.{safe_table_id}"

    if use_h3_only:
        use_h3 = True
    elif "retail_asset_master" in table_path.lower():
        use_h3 = False
    elif has_numerics:
        use_h3 = True
    elif count >= 30:
        use_h3 = True
    else:
        use_h3 = False

    if use_h3:
        lat_span = df[lat_column].max() - df[lat_column].min()
        lon_span = df[lon_column].max() - df[lon_column].min()
        max_span = max(lat_span, lon_span)

        if processing_resolution is None:
            if max_span > 10.0:
                res = 4
            elif max_span > 5.0:
                res = 5
            elif max_span > 1.0:
                res = 6
            elif max_span > 0.5:
                res = 7
            elif max_span > 0.1:
                res = 8
            elif max_span > 0.02:
                res = 9
            else:
                res = 10
        else:
            res = processing_resolution

        is_new_h3 = hasattr(h3, "latlng_to_cell")
        h3_func = h3.latlng_to_cell if is_new_h3 else h3.geo_to_h3

        def get_h3(lat_val, lon_val):
            if pd.notna(lat_val) and pd.notna(lon_val):
                try:
                    return h3_func(float(lat_val), float(lon_val), res)
                except Exception:
                    pass
            return None

        df["h3_index"] = [
            get_h3(lat, lon) for lat, lon in zip(df[lat_column], df[lon_column])
        ]
        df_valid = df.dropna(subset=["h3_index"]).copy()

        if df_valid.empty:
            return {
                "status": "error",
                "message": "Failed to convert any valid coordinates to H3 hexes.",
            }

        agg_rules = {"h3_index": "count"}

        if has_numerics:
            try:
                standard_funcs = ["mean", "max", "min"]
                for col in numeric_cols:
                    agg_rules[col] = standard_funcs
            except Exception as e:
                logger.error(
                    f"Manual aggregation strategy assignment failed: {e}. Falling back to 'mean'."
                )
                for col in numeric_cols:
                    agg_rules[col] = "mean"

        grouped = df_valid.groupby("h3_index").agg(agg_rules)
        grouped = grouped.round(2)

        if isinstance(grouped.columns, pd.MultiIndex):
            grouped.columns = [
                "_".join(col).strip() if isinstance(col, tuple) else str(col)
                for col in grouped.columns.values
            ]
        else:
            grouped.columns = [str(col) for col in grouped.columns]

        # ====================================================
        # Impute missing H3 data
        if radius_poly is not None and res is not None:
            grouped = impute_missing_h3_data(
                grouped_df=grouped,
                radius_poly=radius_poly,
                res=res,
            )
        # ====================================================

        if "h3_index" in grouped.columns:
            grouped = grouped.rename(columns={"h3_index": "poi_count"})
        if "h3_index_count" in grouped.columns:
            grouped = grouped.rename(columns={"h3_index_count": "poi_count"})

        count_col = next((col for col in grouped.columns if "count" in col), None)

        if count_col:
            grouped["details"] = "Total POIs: " + grouped[count_col].astype(str)
        else:
            grouped["details"] = ""

        for col in grouped.columns:
            if col == count_col or col == "details":
                continue
            grouped["details"] += ", " + col + ": " + grouped[col].astype(str)

        info_cols = [c for c in grouped.columns if c != "details"]
        # Handle NaN values for JSON compliance
        grouped["info"] = grouped[info_cols].apply(
            lambda x: {k: (None if pd.isna(v) else v) for k, v in x.to_dict().items()},
            axis=1,
        )

        grouped_reset = grouped.reset_index().rename(columns={"h3_index": "hex_id"})
        grouped_reset["tag"] = "dynamic_h3_aggregation"
        grouped_reset["res"] = res
        hex_codes = grouped_reset[["hex_id", "details", "info", "tag", "res"]].to_dict(
            "records"
        )

        return {"status": "success", "use_h3": True, "hex_codes": hex_codes, "res": res}
    else:
        markers = []
        for i, r in enumerate(all_results):
            lat = r.get(lat_column) or r.get("latitude") or r.get("Latitude")
            lon = r.get(lon_column) or r.get("longitude") or r.get("Longitude")
            label = str(
                r.get("label")
                or r.get("name")
                or r.get("Name")
                or r.get("store_name")
                or f"Point {i+1}"
            )

            # Combine all string-related column values for details
            details_parts = []
            info_dict = {}
            for k, v in r.items():
                if isinstance(v, str) and k.lower() not in (
                    lat_column.lower(),
                    lon_column.lower(),
                    "latitude",
                    "longitude",
                    "place_id",
                    "id",
                ):
                    details_parts.append(f"{k}: {v}")
                    info_dict[k] = v

            details = " | ".join(details_parts) if details_parts else label

            if lat and lon:
                markers.append(
                    {
                        "lat": float(lat),
                        "long": float(lon),
                        "place_id": str(
                            r.get("place_id") or r.get("id") or f"marker_{i}"
                        ),
                        "details": details,
                        "info": info_dict,
                        "tag": None,
                    }
                )

        return {"status": "success", "use_h3": False, "markers": markers}


def lookup_pincode(area_name: str) -> str:
    """
    Looks up the 6-digit Pincode for a given area name (e.g., 'Borivali')
    from the BigQuery pincode boundaries table.
    Use this if the user provides an area name but not a pincode for grid search.

    Args:
        area_name (str): The name of the area to lookup (e.g., 'Borivali').
    """

    project_id = os.getenv("PROJECT_ID", "fynd-jio-ccp-non-prod")
    dataset_id = os.getenv("DATASET_ID", "RetailVista_1")
    table_id = f"{project_id}.{dataset_id}.pincode_boundaries_mmr"

    client = bigquery.Client(project=project_id)

    query = f"""
    SELECT DISTINCT Pincode, Office_Name, Division
    FROM `{table_id}`
    WHERE LOWER(Office_Name) LIKE '%{area_name.lower()}%'
       OR LOWER(Division) LIKE '%{area_name.lower()}%'
    """

    try:
        query_job = client.query(query)
        results = [dict(row) for row in query_job.result()]

        if not results:
            return json.dumps(
                {
                    "status": "success",
                    "message": f"No Pincodes found for '{area_name}'",
                    "matches": [],
                }
            )

        return json.dumps(
            {"status": "success", "area": area_name, "matches": results}, indent=4
        )

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
