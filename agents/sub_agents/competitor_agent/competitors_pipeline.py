"""
This module defines the competitor analysis pipeline and related tools.
"""

import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.cloud import bigquery
from google.adk.tools import ToolContext
import h3

import pandas as pd
from pydantic import BaseModel

try:
    import shapely.geometry as sg
    from shapely import wkt
    from shapely.ops import unary_union
    SPATIAL_LIBS_AVAILABLE = True
except ImportError:
    SPATIAL_LIBS_AVAILABLE = False
    class sg:
        class Polygon:
            pass
    wkt = None
    unary_union = None

# Import v4 utils
from agents.places_text_search_utils import (
    search_places_in_polygon,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID", "fynd-jio-ccp-non-prod")
DATASET_ID = os.getenv("DATASET_ID", "RetailVista_1")
MAPS_API_KEY = os.getenv("MAPS_API_KEY")


def get_optimal_resolution(area_sq_km: float, max_hexes: int = 150) -> int:
    """
    Calculates the optimal H3 resolution to keep the expected number of hexes below max_hexes.
    """
    res_areas = {9: 0.105, 8: 0.737, 7: 5.161, 6: 36.129}
    for res in [9, 8, 7, 6]:
        expected_hexes = area_sq_km / res_areas[res]
        if expected_hexes <= max_hexes:
            return res
    return 6


class CompetitorInsightHex(BaseModel):
    """Pydantic model for competitor insights in a specific H3 hexagon."""

    hex_id: str
    lat: float
    lon: float
    reliance_count: int
    competitor_count: int
    insight_summary: str


class CompetitorReport(BaseModel):
    """Pydantic model for the overall competitor report."""

    hex_insights: list[CompetitorInsightHex]
    detailed_analysis: str


def get_polygon_from_bq(pincodes_str: str) -> Optional[sg.Polygon]:
    """Fetches geometry_wkt for given pincodes or area names from BigQuery
    and returns their union.
    """
    import re

    tokens = re.split(r"[,\s]+", str(pincodes_str))
    expanded_inputs = [t.strip() for t in tokens if t.strip()]

    digits = []
    names = []

    for p_str in expanded_inputs:
        if p_str.isdigit():
            digits.append(p_str)
        else:
            names.append(p_str)

    conditions = []
    if digits:
        conditions.append(f"Pincode IN ({', '.join(digits)})")
    if names:
        for n in names:
            n_clean = n.lower()
            if n_clean == "mumbai":
                conditions.append(
                    "( (LOWER(Office_Name) LIKE '%mumbai%' OR LOWER(Division) LIKE '%mumbai%')"
                    " AND LOWER(Division) NOT LIKE '%navi mumbai%' )"
                )
            else:
                conditions.append(
                    f"(LOWER(Office_Name) LIKE '%{n_clean}%' OR LOWER(Division) LIKE '%{n_clean}%')"
                )

    if not conditions:
        return None

    where_clause = " OR ".join(conditions)

    client = bigquery.Client(project=PROJECT_ID)
    table_id = f"{PROJECT_ID}.{DATASET_ID}.pincode_boundaries_mmr"

    query = f"SELECT geometry_wkt FROM `{table_id}` WHERE {where_clause}"

    try:
        query_job = client.query(query)
        rows = list(query_job.result())
        polygons = []
        for row in rows:
            if row["geometry_wkt"]:
                polygons.append(wkt.loads(row["geometry_wkt"]))
        if not polygons:
            return None
        return unary_union(polygons)
    except Exception as e:
        logger.error("Error fetching polygons: %s", e)
        return None


def fetch_reliance_stores_polygon(
    tool_context: ToolContext, filter_condition: str, resolution: int = 7
) -> list[dict]:
    """Fetches Reliance stores within a polygon area from BigQuery."""
    client = bigquery.Client(project=PROJECT_ID)
    table_path = f"{PROJECT_ID}.{DATASET_ID}.retail_asset_master"

    ca = tool_context.state.get("competitor_analysis", {})
    polygon_coords = ca.get("polygon_coords")
    if not polygon_coords:
        logger.error("polygon_coords missing in competitor_analysis state.")
        return []

    poly = sg.shape(polygon_coords)
    polygon_wkt = poly.wkt

    filter_clause = ""
    if filter_condition and filter_condition.strip():
        cond = filter_condition.strip()
        if cond.upper().startswith("AND ") or cond.upper().startswith("OR "):
            filter_clause = f" {cond}"
        else:
            filter_clause = f" AND {cond}"

    query = f"""
        SELECT 
            *,
            latitude as lat,
            longitude as lon
        FROM `{table_path}`
        WHERE 
            SAFE.ST_GEOGPOINT(longitude, latitude) IS NOT NULL
            AND ST_CONTAINS(ST_GEOGFROMTEXT('{polygon_wkt}'), ST_GEOGPOINT(longitude, latitude))
            {filter_clause}
    """

    logger.info("Final Reliance Stores BQ Query:\n%s", query)

    all_results = []
    try:
        query_job = client.query(query)
        df = query_job.to_dataframe()
        for i, row in df.iterrows():
            lat = row.get("lat")
            lon = row.get("lon")
            hex_id = None
            if pd.notna(lat) and pd.notna(lon):
                try:
                    if hasattr(h3, "latlng_to_cell"):
                        hex_id = h3.latlng_to_cell(float(lat), float(lon), resolution)
                    else:
                        hex_id = h3.geo_to_h3(float(lat), float(lon), resolution)
                except:
                    pass
            details_parts = []
            info_dict = {}
            for k, v in row.items():
                if pd.notna(v) and k.lower() not in (
                    "latitude",
                    "longitude",
                    "lat",
                    "lon",
                    "id",
                    "place_id",
                    "hex_id",
                ):
                    val = str(v) if not isinstance(v, (str, int, float, bool)) else v
                    details_parts.append(f"{k}: {val}")
                    info_dict[k] = val

            all_results.append(
                {
                    "type": "Reliance",
                    "name": row.get("store_name", "Reliance Store"),
                    "lat": lat,
                    "lon": lon,
                    "place_id": str(row.get("id", f"rel_{i}")),
                    "hex_id": hex_id,
                    "details": (
                        " | ".join(details_parts)
                        if details_parts
                        else row.get("store_name", "Reliance Store")
                    ),
                    "info": info_dict,
                }
            )
    except Exception as e:
        logger.error("BQ Fetch Error for reliance: %s", e)
    return all_results


def generate_competitor_insights_llm(hex_data_summary: dict) -> str:
    """Generates detailed competitor insights and a markdown report using LLM."""
    client = genai.Client(vertexai=True, project=PROJECT_ID, location="us-central1")
    profiles_text = json.dumps(hex_data_summary, indent=2)

    prompt = f"""
    You are an expert site selection and competitor analyst.
    Here is an aggregated overview of map areas containing "Reliance" stores vs "Competitor" stores, including their addresses and approximate locations:
    {profiles_text}
    
    Please provide a detailed, beautifully formatted Markdown analysis.
    CRITICAL REQUIREMENT: Do NOT use or mention the raw hex IDs (e.g., '87608b440ffffff') in your analysis. Instead, identify and refer to areas using human-readable neighborhood names, prominent street names, or landmarks derived from the provided addresses and coordinates.
    
    Identify:
    - Locations/Addresses where Reliance is dominating.
    - Locations/Addresses where Competitors are leading.
    - Whitespace opportunities (no Reliance stores, but maybe competitors or open).
    - Recommendations for opening new store locations or other useful insights.
    - Specific observations you see from the data.
    
    Output JSON exactly satisfying this schema:
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": CompetitorReport,
            },
        )
        data = json.loads(response.text.strip())
        return data.get("detailed_analysis", "No detailed analysis provided.")
    except Exception as e:
        logger.error("Ranking failed: %s", e)
        return "Error generating analysis."


def run_competitor_analysis_pipeline(
    pincode: str,
    bq_filter_condition: str,
    tool_context: ToolContext,
    user_query: str = None,
) -> str:
    """Runs the complete competitor analysis pipeline for given pincodes or filters."""
    logger.info(
        "Starting competitors pipeline for pincodes: %s filters=%s",
        pincode,
        bq_filter_condition,
    )

    # 1. Generate Polygon from Pincode
    poly = get_polygon_from_bq(pincode)
    if not poly:
        return json.dumps(
            {
                "status": "error",
                "message": f"Could not generate polygon for pincode: {pincode}",
            }
        )

    # Store polygon in state for cross-function passing
    if "competitor_analysis" not in tool_context.state:
        tool_context.state["competitor_analysis"] = {}
    tool_context.state["competitor_analysis"]["polygon_coords"] = poly.__geo_interface__

    # poly = poly.convex_hull if poly.geom_type == 'MultiPolygon' else poly

    # Calculate optimal resolution based on area
    try:
        area_sq_km = poly.area * 11700
        resolution = get_optimal_resolution(area_sq_km)
        logger.info(
            "Dynamic Scale chosen: Resolution %s based on area %.2f sq km",
            resolution,
            area_sq_km,
        )
    except Exception as e:
        logger.error("Failed to calculate dynamic resolution: %s. Defaulting to 7.", e)
        resolution = 7

    # 2. Fetch Reliance Stores
    reliance_stores = fetch_reliance_stores_polygon(
        tool_context, bq_filter_condition, resolution=resolution
    )

    # 3. Fetch Competitors using v4 search_places_in_polygon
    # We construct a gemini prompt to get queries if not provided
    gemini_prompt = None

    allowed_table_a_types = [
        # Automotive
        "car_dealer",
        "car_rental",
        "car_repair",
        "car_wash",
        "ebike_charging_station",
        "electric_vehicle_charging_station",
        "gas_station",
        "parking",
        "parking_garage",
        "parking_lot",
        "rest_stop",
        "tire_shop",
        "truck_dealer",
        # Business
        "business_center",
        "corporate_office",
        "coworking_space",
        "farm",
        "manufacturer",
        "ranch",
        "supplier",
        "television_studio",
        # Culture
        "art_gallery",
        "art_museum",
        "art_studio",
        "auditorium",
        "castle",
        "cultural_landmark",
        "fountain",
        "historical_place",
        "history_museum",
        "monument",
        "museum",
        "performing_arts_theater",
        "sculpture",
        # Education
        "academic_department",
        "educational_institution",
        "library",
        "preschool",
        "primary_school",
        "research_institute",
        "school",
        "secondary_school",
        "university",
        # Entertainment and Recreation
        "adventure_sports_center",
        "amphitheatre",
        "amusement_center",
        "amusement_park",
        "aquarium",
        "banquet_hall",
        "barbecue_area",
        "botanical_garden",
        "bowling_alley",
        "casino",
        "childrens_camp",
        "city_park",
        "comedy_club",
        "community_center",
        "concert_hall",
        "convention_center",
        "cultural_center",
        "cycling_park",
        "dance_hall",
        "dog_park",
        "event_venue",
        "ferris_wheel",
        "garden",
        "hiking_area",
        "historical_landmark",
        "indoor_playground",
        "internet_cafe",
        "karaoke",
        "live_music_venue",
        "marina",
        "miniature_golf_course",
        "movie_rental",
        "movie_theater",
        "national_park",
        "night_club",
        "observation_deck",
        "off_roading_area",
        "opera_house",
        "paintball_center",
        "park",
        "philharmonic_hall",
        "picnic_ground",
        "planetarium",
        "plaza",
        "roller_coaster",
        "tourist_attraction",
        "video_arcade",
        "vineyard",
        "visitor_center",
        "water_park",
        "wedding_venue",
        "wildlife_park",
        "wildlife_refuge",
        "zoo",
        # Finance
        "accounting",
        "atm",
        "bank",
        # Food and Drink
        "acai_shop",
        "afghani_restaurant",
        "african_restaurant",
        "american_restaurant",
        "argentinian_restaurant",
        "asian_fusion_restaurant",
        "asian_restaurant",
        "australian_restaurant",
        "austrian_restaurant",
        "bagel_shop",
        "bakery",
        "bangladeshi_restaurant",
        "bar",
        "bar_and_grill",
        "barbecue_restaurant",
        "basque_restaurant",
        "bavarian_restaurant",
        "beer_garden",
        "brazilian_restaurant",
        "breakfast_restaurant",
        "british_restaurant",
        "bubble_tea_shop",
        "buffet_restaurant",
        "burger_restaurant",
        "burmese_restaurant",
        "cafe",
        "cafeteria",
        "cajun_restaurant",
        "cambodian_restaurant",
        "canadian_restaurant",
        "candy_store",
        "cantonese_restaurant",
        "caribbean_restaurant",
        "cat_cafe",
        "chinese_restaurant",
        "chocolate_shop",
        "coffee_shop",
        "confectionery",
        "creperie",
        "cuban_restaurant",
        "czech_restaurant",
        "deli",
        "dessert_restaurant",
        "dessert_shop",
        "dim_sum_restaurant",
        "diner",
        "dog_cafe",
        "donut_shop",
        "dumpling_restaurant",
        "dutch_restaurant",
        "eastern_european_restaurant",
        "ethiopian_restaurant",
        "european_restaurant",
        "falafel_restaurant",
        "family_restaurant",
        "fast_food_restaurant",
        "filipino_restaurant",
        "fine_dining_restaurant",
        "french_restaurant",
        "fusion_restaurant",
        "german_restaurant",
        "greek_restaurant",
        "grill",
        "gujarati_restaurant",
        "hawaiian_restaurant",
        "health_food_restaurant",
        "ice_cream_shop",
        "indian_restaurant",
        "indonesian_restaurant",
        "irish_pub",
        "italian_restaurant",
        "japanese_restaurant",
        "jewish_restaurant",
        "juice_shop",
        "korean_restaurant",
        "lebanese_restaurant",
        "machine_shop",
        "malaysian_restaurant",
        "mediterranean_restaurant",
        "mexican_restaurant",
        "middle_eastern_restaurant",
        "moroccan_restaurant",
        "noodle_shop",
        "north_indian_restaurant",
        "oyster_bar_restaurant",
        "pakistani_restaurant",
        "pastry_shop",
        "persian_restaurant",
        "peruvian_restaurant",
        "pizza_delivery",
        "pizza_restaurant",
        "polish_restaurant",
        "portuguese_restaurant",
        "pub",
        "ramen_restaurant",
        "restaurant",
        "romanian_restaurant",
        "russian_restaurant",
        "sandwich_shop",
        "scandinavian_restaurant",
        "seafood_restaurant",
        "senegalese_restaurant",
        "singaporean_restaurant",
        "slovak_restaurant",
        "south_indian_restaurant",
        "southeast_asian_restaurant",
        "spanish_restaurant",
        "steak_house",
        "sushi_restaurant",
        "swiss_restaurant",
        "syrian_restaurant",
        "taiwanese_restaurant",
        "tapas_restaurant",
        "thai_restaurant",
        "turkish_restaurant",
        "vegan_restaurant",
        "vegetarian_restaurant",
        "venezuelan_restaurant",
        "vietnamese_restaurant",
        "wine_bar",
        # Government
        "city_hall",
        "courthouse",
        "embassy",
        "fire_station",
        "government_office",
        "local_government_office",
        "police",
        "post_office",
        # Health and Wellness
        "chiropractor",
        "dental_clinic",
        "dentist",
        "doctor",
        "drugstore",
        "hospital",
        "medical_clinic",
        "medical_lab",
        "pharmacy",
        "physiotherapist",
        "spa",
        "veterinary_care",
        # Lodging
        "bed_and_breakfast",
        "campground",
        "cottage",
        "guest_house",
        "hostel",
        "hotel",
        "lodging",
        "motel",
        "resort_hotel",
        "rv_park",
        # Services
        "beauty_salon",
        "cemetery",
        "church",
        "electrician",
        "florist",
        "funeral_home",
        "hair_care",
        "hardware_store",
        "hindu_temple",
        "insurance_agency",
        "jewelry_store",
        "laundry",
        "lawyer",
        "locksmith",
        "mosque",
        "moving_company",
        "painter",
        "pet_store",
        "plumber",
        "real_estate_agency",
        "roofing_contractor",
        "storage",
        "synagogue",
        "travel_agency",
        # Shopping
        "book_store",
        "clothing_store",
        "convenience_store",
        "department_store",
        "electronics_store",
        "furniture_store",
        "gift_shop",
        "grocery_store",
        "home_goods_store",
        "liquor_store",
        "market",
        "optician",
        "shoe_store",
        "shopping_mall",
        "sporting_goods_store",
        "store",
        "supermarket",
        "wholesaler",
        # Transportation
        "airport",
        "bus_station",
        "bus_stop",
        "ferry_terminal",
        "heliport",
        "light_rail_station",
        "subway_station",
        "taxi_stand",
        "train_station",
        "transit_station",
    ]

    gemini_prompt = f"""
    You are an AI assistant working for Reliance, India's largest retailer. Your persona is that of a strategic market analyst focusing on the Mumbai and Navi Mumbai region.
   
    Analyze the user query: '{user_query or "Identify competitors for Reliance"}'.
   
    Your goal is to generate appropriate search queries for the Google Places Text Search API to find relevant competitor places specifically within the Mumbai and Navi Mumbai regions (or areas looking to expand within Mumbai).
   
    ### Competitor Information
    You can use the below competitors information while doing your analysis. Smaller grocery stores, kirana shops may not be considered competition for Reliance brands.
    - JioMart: BigBasket (Tata), Blinkit (Zomato), Zepto, Swiggy Instamart, Amazon Fresh.
    - SMART Bazaar/Reliance Fresh: DMart, More Retail, Spencer's Retail, Big Bazaar.
    - 7-Eleven: Twenty Four Seven, local convenience stores.
    - Milkbasket: BigBasket Daily, Supr Daily (Swiggy), Country Delight, local milk vendors.
    - Reliance Digital: Croma (Tata), Vijay Sales, Amazon, Flipkart, brand-exclusive stores.
    - AJIO: Myntra, Flipkart, Amazon Fashion, Nykaa Fashion, Bewakoof.
    - Trends: Westside (Tata), Pantaloons, Max Fashion, Shoppers Stop, Zudio (Tata).
    - Yousta: Zudio (Tata), H&M, Max Fashion, Bewakoof.
    - Azorte: Zara, H&M, Lifestyle, Shoppers Stop.
    - Reliance Jewels: Tanishq (Tata), Kalyan Jewellers, Malabar Gold & Diamonds, CaratLane.
    - Hamleys: FirstCry, Crossword, Amazon, local toy shops.
    - Tira: Nykaa, Sephora, Myntra Beauty, Purplle.
    - Urban Ladder: Pepperfry, IKEA, WoodenStreet, Flipkart Furniture.
    - Zivame: Clovia, Amante, Enamor, Marks & Spencer.
    - Netmeds: Tata 1mg, PharmEasy, Apollo 24/7, Flipkart Health+.
    - Campa/Sosyo: Coca-Cola, PepsiCo, Parle Agro.
    - FMCG Brands (Independence, Good Life, etc.): ITC, Tata Consumer Products, Adani Wilmar, Hindustan Unilever (HUL).
   
    You are NOT limited to the above list. You MUST use Google Search effectively to discover other relevant major competitors and high-footfall brands in the same category, especially those that might be strong in the Mumbai and Navi Mumbai region. Consider searching for "top competitor brands for [Category] in Mumbai" to find names you might not know.
   
    Instructions for Query Generation:
    - If specific brands are mentioned (e.g., Croma), generate queries for them.
    - If the context implies 'similar' or a category (e.g., 'Croma or similar'), ALSO generate queries for other major competitors in that category (both from the list above and discovered via search).
    - If no specific brand is mentioned, search for all typical competitor brands matching the category.
    - Ignore local shops or competitors who cannot compete with a brand like Reliance.
    **IMPORTANT RULE**
    - When a single specific brand is mentioned, generate a query for that brand only. Do not generate queries for other brands in this scenario other than the one mentioned.
   
    CRITICAL CONSTRAINTS:
    1. Generate search queries for identified competitor brands. Include type in search query (e.g., "Kohinoor Electronics Store").
    2. Do combine multiple brands into a single query.
    3. Determine the valid 'included_type' strictly from Table A types:
       {', '.join(allowed_table_a_types)}
    4. Reliance or any of its sub-brands (e.g., Reliance Digital, Reliance Smart, Trends, etc.) is NOT a competitor of itself. NEVER include Reliance or its sub-brands in search queries.
   
    Always include two keys "search_queries" and "included_type" in the output JSON.
    search_queries is a list of dictionaries where each dictionary contains the search query as the key and the value as "competitor".
    included_type is the valid Table A type.
    Output JSON only.
    Example: {{
        "search_queries": [{{"Kohinoor Electronics":"competitor"}},{{"Vijay Sales":"competitor"}}],
        "included_type": "electronics_store"
    }}
    """

    logger.info("Calling v4 search_places_in_polygon...")
    competitor_places = search_places_in_polygon(
        poly, gemini_prompt=gemini_prompt, output_res=resolution, page_size=5
    )

    # Determine central point for marker representation

    # State preparation (following v2 structure)
    if "competitor_analysis" not in tool_context.state:
        tool_context.state["competitor_analysis"] = {"dataset_insights": {}}

    ca = tool_context.state["competitor_analysis"]

    # Set pincode directly at root level
    ca["pincode"] = pincode

    if "dataset_insights" not in ca:
        ca["dataset_insights"] = {}
    dataset_insights = ca["dataset_insights"]

    if "marker_dataset_ids" not in dataset_insights:
        dataset_insights["marker_dataset_ids"] = []

    bq_table_id = f"{PROJECT_ID}.{DATASET_ID}.retail_asset_master"
    if bq_table_id not in dataset_insights["marker_dataset_ids"]:
        dataset_insights["marker_dataset_ids"].append(bq_table_id)

    if bq_table_id not in dataset_insights:
        dataset_insights[bq_table_id] = {
            "dataset_details": "Retail Asset Master",
            "markers": [],
        }

    if "places_insights" not in ca:
        ca["places_insights"] = {"markers": []}

    hex_summary = {}

    # Populate Markers and Aggregate Reliance Stores
    for s in reliance_stores:
        dataset_insights[bq_table_id]["markers"].append(
            {
                "lat": float(s["lat"]),
                "long": float(s["lon"]),
                "place_id": s.get("place_id"),
                "details": s.get("details") or s.get("name"),
                "info": s.get("info", {}),
                "tag": "reliance_store",
            }
        )

        h3_idx = s.get("hex_id")
        if h3_idx:
            if h3_idx not in hex_summary:
                center = (
                    h3.cell_to_latlng(h3_idx)
                    if hasattr(h3, "cell_to_latlng")
                    else h3.h3_to_geo(h3_idx)
                )
                hex_summary[h3_idx] = {
                    "reliance": 0,
                    "competitor": 0,
                    "names": set(),
                    "addresses": set(),
                    "center_lat": center[0],
                    "center_lon": center[1],
                }
            hex_summary[h3_idx]["reliance"] += 1
            hex_summary[h3_idx]["names"].add(s.get("name", "Reliance Store"))

    # Populate Markers and Aggregate Competitor Places
    for p in competitor_places:
        details_parts = []
        info_dict = {}
        for k, v in p.items():
            if v and k.lower() not in ("lat", "lng", "place_id", "hex_id"):
                val = str(v) if isinstance(v, (list, dict)) else v
                details_parts.append(f"{k}: {val}")
                info_dict[k] = val

        ca["places_insights"]["markers"].append(
            {
                "lat": float(p.get("lat")),
                "long": float(p.get("lng")),
                "place_id": p.get("place_id"),
                "details": (
                    " | ".join(details_parts) if details_parts else p.get("name")
                ),
                "info": info_dict,
                "tag": "competitor",
            }
        )

        h3_idx = p.get("hex_id")
        if h3_idx:
            if h3_idx not in hex_summary:
                center = (
                    h3.cell_to_latlng(h3_idx)
                    if hasattr(h3, "cell_to_latlng")
                    else h3.h3_to_geo(h3_idx)
                )
                hex_summary[h3_idx] = {
                    "reliance": 0,
                    "competitor": 0,
                    "names": set(),
                    "addresses": set(),
                    "center_lat": center[0],
                    "center_lon": center[1],
                }
            hex_summary[h3_idx]["competitor"] += 1
            hex_summary[h3_idx]["names"].add(p.get("name", "Unknown"))
            if p.get("address") and p.get("address") != "Unknown":
                hex_summary[h3_idx]["addresses"].add(p.get("address"))

    if reliance_stores or competitor_places:

        # Setup hex datasets
        hex_table_id = "competitor_hex_insights"

        if hex_table_id not in ca:
            ca[hex_table_id] = {
                "dataset_details": "Competitor Presence Map",
                "hex_codes": [],
            }

        # JSON formatting structure mapping
        json_hex_summary = {}
        for h3_idx, stats in hex_summary.items():
            r_cnt = stats["reliance"]
            c_cnt = stats["competitor"]

            tag = "Both_Presence"
            if r_cnt > 0 and c_cnt == 0:
                tag = "Reliance_Only"
            elif c_cnt > 0 and r_cnt == 0:
                tag = "Competitor_Only"

            stores_str = ", ".join(list(stats["names"])[:3])
            ca[hex_table_id]["hex_codes"].append(
                {
                    "hex_id": h3_idx,
                    "details": f"Reliance: {r_cnt}, Competitors: {c_cnt}. Stores: {stores_str}",
                    "tag": tag,
                    "res": resolution,
                }
            )

            json_hex_summary[h3_idx] = {
                "reliance_count": r_cnt,
                "competitor_count": c_cnt,
                "store_names": list(stats["names"]),
                "addresses": list(stats["addresses"])[:5],
                "approx_location": f"Lat: {stats['center_lat']:.4f}, "
                f"Lon: {stats['center_lon']:.4f}",
            }

        # 5. Generate Insights
        final_analysis = generate_competitor_insights_llm(json_hex_summary)
    else:
        final_analysis = "No stores or competitors found in this polygon."

    ca["insights"] = [final_analysis]
    tool_context.state["competitor_analysis"] = ca

    from state import debug_dump_state

    debug_dump_state(tool_context.state)

    return json.dumps(
        {
            "status": "success",
            "total_reliance": len(reliance_stores),
            "total_competitors": len(competitor_places),
            "final_result_text": final_analysis,
        },
        indent=4,
    )
