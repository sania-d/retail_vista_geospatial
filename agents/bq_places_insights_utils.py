"""Module for fetching places insights from BigQuery and updating agent state."""

import os
import json
import logging
from typing import Dict, Any
import google.genai as genai
from google.cloud import bigquery

logger = logging.getLogger(__name__)

# Fetch project ID from environment
PROJECT_ID = os.getenv("PROJECT_ID", "fynd-jio-ccp-non-prod")


def get_hex_radius(resolution: int) -> int:
    """
    Returns the approximate circumradius in meters for a given H3 resolution
     to cover the entire boundary.
    """
    # Mapping based on average area and circumradius calculation
    res_radii = {9: 200, 8: 550, 7: 1400, 6: 3700}
    return res_radii.get(
        resolution, 500
    )  # Fallback to 500 if resolution not in mapping


def fetch_places_insights_h3(
    polygon_wkt: str,
    state: Dict[str, Any],
    resolution: int,
    radius: int,
    target_types: list = None,
    analysis_key: str = None,
) -> None:
    """
    Fetches places insights counts per type for top H3 cells in a polygon
    and updates the state directly.

    Args:
        polygon_wkt (str): WKT representation of the polygon area to search.
        state (dict): The agent's state dictionary to be updated.
        resolution (int): H3 resolution to use.
        radius (int): Geography radius in meters for proximity search.
    """
    client = bigquery.Client(project=PROJECT_ID)

    # Fallback to hardcoded types if not provided
    if target_types is None:
        target_types = [
            "restaurant",
            "cafe",
            "transit_station",
            "bus_station",
            "train_station",
            "fast_food_restaurant",
            "hamburger_restaurant",
            "pizza_restaurant",
            "sandwich_shop",
            "fine_dining_restaurant",
            "steak_house",
            "sushi_restaurant",
            "bus_stop",
            "subway_station",
            "light_rail_station",
            "indian_restaurant",
            "north_indian_restaurant",
            "south_indian_restaurant",
            "shopping_mall",
        ]

    res = resolution
    logger.info("Using resolution %s passed from agent.", res)

    query = f"""
    DECLARE search_polygon GEOGRAPHY DEFAULT ST_GEOGFROMTEXT('{polygon_wkt}');

    WITH all_hexes AS (
      SELECT 
        h3_cell_index AS geo_id,
        ST_CENTROID(geography) AS geo,
        count
      FROM `{PROJECT_ID}.places_insights___in___sample.PLACES_COUNT_PER_H3`(
        JSON_OBJECT(
          'geography', search_polygon,
          'types', {json.dumps(target_types)},
          'business_status', ['OPERATIONAL'],
          'h3_resolution', {res}
        )
      )
      ORDER BY count DESC
      LIMIT 10000
    ),
    hexes_for_func AS (
      SELECT geo_id, geo FROM all_hexes
    )

    SELECT 
      t.geo_id AS h3_cell_index,
      v2.place_type,
      v2.place_count
    FROM all_hexes t
    JOIN `{PROJECT_ID}.places_insights___in___sample.PLACES_COUNT_PER_TYPE_V2`(
      TABLE hexes_for_func,
      {json.dumps(target_types)},
      JSON_OBJECT(
        'geography_radius', {radius},
        'business_status', ['OPERATIONAL']
      )
    ) v2 ON t.geo_id = v2.geo_id;
    """

    try:
        logger.info(
            "Executing BQ Places Insights query for polygon with resolution %s...", res
        )
        logger.info("Query: %s", query)
        query_job = client.query(query)
        results = query_job.to_dataframe()

        insights = []
        # Group by hex to aggregate diverse rows coming from BigQuery
        hex_grouped = results.groupby("h3_cell_index")

        for hex_id, group in hex_grouped:
            # Pre-populate specific types to 0 with _count suffix dynamically
            specific_insights = {t + "_count": 0 for t in target_types}

            for _, row in group.iterrows():
                p_type = str(row["place_type"])
                count = int(row["place_count"])
                specific_insights[p_type + "_count"] = count

            poi_sum = int(group["place_count"].sum())
            specific_insights["poi_count"] = poi_sum

            # Synthesize a text breakdown for details
            breakdown_str = ", ".join(
                [f"{k}: {v}" for k, v in specific_insights.items()]
            )

            hex_item = {
                "hex_id": str(hex_id),
                "details": f"Total Places counted: {poi_sum}, {breakdown_str}",
                "info": specific_insights,  # Counts reside directly at this level
                "tag": "places_bq_insights_counts",  # Exact tag mapping requested
                "res": res,
            }
            insights.append(hex_item)

        # Update state directly
        if analysis_key is None:
            analysis_key = "polygon_analysis"
            for key in [
                "polygon_analysis",
                "catchment_analysis",
                "competitor_analysis",
            ]:
                if key in state:
                    analysis_key = key
                    break

        if analysis_key not in state:
            state[analysis_key] = {}

        # Dump directly at higher key, bypassing dataset_insights nesting!
        state[analysis_key]["bq_places_insights"] = insights

        # Add bq_places_insights to hex_dataset_ids to track it
        if "dataset_insights" not in state[analysis_key]:
            state[analysis_key]["dataset_insights"] = {}
        if "hex_dataset_ids" not in state[analysis_key]["dataset_insights"]:
            state[analysis_key]["dataset_insights"]["hex_dataset_ids"] = []
        if (
            "bq_places_insights"
            not in state[analysis_key]["dataset_insights"]["hex_dataset_ids"]
        ):
            state[analysis_key]["dataset_insights"]["hex_dataset_ids"].append(
                "bq_places_insights"
            )

        logger.info(
            "Successfully updated state with bq_places_insights list under %s",
            analysis_key,
        )

    except Exception as e:
        logger.error("Error in fetch_places_insights_h3: %s", e)
        if analysis_key is None:
            analysis_key = "polygon_analysis"
            for key in [
                "polygon_analysis",
                "catchment_analysis",
                "competitor_analysis",
            ]:
                if key in state:
                    analysis_key = key
                    break

        if analysis_key not in state:
            state[analysis_key] = {}
        if "dataset_insights" not in state[analysis_key]:
            state[analysis_key]["dataset_insights"] = {}
        state[analysis_key]["dataset_insights"]["bq_places_insights_error"] = str(e)


def get_target_types_from_gemini(user_query: str) -> list:
    """
    Uses Gemini to select relevant place types from Google Maps Table A based on user query.
    """
    client = genai.Client(vertexai=True, project=PROJECT_ID, location="us-central1")

    # Full list of Table A types
    all_types = [
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
        "business_center",
        "corporate_office",
        "coworking_space",
        "farm",
        "manufacturer",
        "ranch",
        "supplier",
        "television_studio",
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
        "academic_department",
        "educational_institution",
        "library",
        "preschool",
        "primary_school",
        "research_institute",
        "school",
        "secondary_school",
        "university",
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
        "go_karting_venue",
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
        "skateboard_park",
        "state_park",
        "tourist_attraction",
        "video_arcade",
        "vineyard",
        "visitor_center",
        "water_park",
        "wedding_venue",
        "wildlife_park",
        "wildlife_refuge",
        "zoo",
        "public_bath",
        "public_bathroom",
        "stable",
        "accounting",
        "atm",
        "bank",
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
        "belgian_restaurant",
        "bistro",
        "brazilian_restaurant",
        "breakfast_restaurant",
        "brewery",
        "brewpub",
        "british_restaurant",
        "brunch_restaurant",
        "buffet_restaurant",
        "burmese_restaurant",
        "burrito_restaurant",
        "cafe",
        "cafeteria",
        "cajun_restaurant",
        "cake_shop",
        "californian_restaurant",
        "cambodian_restaurant",
        "candy_store",
        "cantonese_restaurant",
        "caribbean_restaurant",
        "cat_cafe",
        "chicken_restaurant",
        "chicken_wings_restaurant",
        "chilean_restaurant",
        "chinese_noodle_restaurant",
        "chinese_restaurant",
        "chocolate_factory",
        "chocolate_shop",
        "cocktail_bar",
        "coffee_roastery",
        "coffee_shop",
        "coffee_stand",
        "colombian_restaurant",
        "confectionery",
        "croatian_restaurant",
        "cuban_restaurant",
        "czech_restaurant",
        "danish_restaurant",
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
        "fish_and_chips_restaurant",
        "fondue_restaurant",
        "food_court",
        "french_restaurant",
        "fusion_restaurant",
        "gastropub",
        "german_restaurant",
        "greek_restaurant",
        "gyro_restaurant",
        "halal_restaurant",
        "hamburger_restaurant",
        "hawaiian_restaurant",
        "hookah_bar",
        "hot_dog_restaurant",
        "hot_dog_stand",
        "hot_pot_restaurant",
        "hungarian_restaurant",
        "ice_cream_shop",
        "indian_restaurant",
        "indonesian_restaurant",
        "irish_pub",
        "irish_restaurant",
        "israeli_restaurant",
        "italian_restaurant",
        "japanese_curry_restaurant",
        "japanese_izakaya_restaurant",
        "japanese_restaurant",
        "juice_shop",
        "kebab_shop",
        "korean_barbecue_restaurant",
        "korean_restaurant",
        "latin_american_restaurant",
        "lebanese_restaurant",
        "lounge_bar",
        "malaysian_restaurant",
        "meal_delivery",
        "meal_takeaway",
        "mediterranean_restaurant",
        "mexican_restaurant",
        "middle_eastern_restaurant",
        "mongolian_barbecue_restaurant",
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
        "salad_shop",
        "sandwich_shop",
        "scandinavian_restaurant",
        "seafood_restaurant",
        "shawarma_restaurant",
        "snack_bar",
        "soul_food_restaurant",
        "soup_restaurant",
        "south_american_restaurant",
        "south_indian_restaurant",
        "southwestern_us_restaurant",
        "spanish_restaurant",
        "sports_bar",
        "sri_lankan_restaurant",
        "steak_house",
        "sushi_restaurant",
        "swiss_restaurant",
        "taco_restaurant",
        "taiwanese_restaurant",
        "tapas_restaurant",
        "tea_house",
        "tex_mex_restaurant",
        "thai_restaurant",
        "tibetan_restaurant",
        "tonkatsu_restaurant",
        "turkish_restaurant",
        "ukrainian_restaurant",
        "vegan_restaurant",
        "vegetarian_restaurant",
        "vietnamese_restaurant",
        "western_restaurant",
        "wine_bar",
        "winery",
        "yakiniku_restaurant",
        "yakitori_restaurant",
        "administrative_area_level_1",
        "administrative_area_level_2",
        "country",
        "locality",
        "postal_code",
        "school_district",
        "city_hall",
        "courthouse",
        "embassy",
        "fire_station",
        "government_office",
        "local_government_office",
        "neighborhood_police_station",
        "police",
        "post_office",
        "chiropractor",
        "dental_clinic",
        "dentist",
        "doctor",
        "drugstore",
        "general_hospital",
        "hospital",
        "massage",
        "massage_spa",
        "medical_center",
        "medical_clinic",
        "medical_lab",
        "pharmacy",
        "physiotherapist",
        "sauna",
        "skin_care_clinic",
        "spa",
        "tanning_studio",
        "wellness_center",
        "yoga_studio",
        "apartment_building",
        "apartment_complex",
        "condominium_complex",
        "housing_complex",
        "bed_and_breakfast",
        "budget_japanese_inn",
        "campground",
        "camping_cabin",
        "cottage",
        "extended_stay_hotel",
        "farmstay",
        "guest_house",
        "hostel",
        "hotel",
        "inn",
        "japanese_inn",
        "lodging",
        "mobile_home_park",
        "motel",
        "private_guest_room",
        "resort_hotel",
        "rv_park",
        "beach",
        "island",
        "lake",
        "mountain_peak",
        "nature_preserve",
        "river",
        "scenic_spot",
        "woods",
        "buddhist_temple",
        "church",
        "hindu_temple",
        "mosque",
        "shinto_shrine",
        "synagogue",
        "aircraft_rental_service",
        "association_or_organization",
        "astrologer",
        "barber_shop",
        "beautician",
        "beauty_salon",
        "body_art_service",
        "catering_service",
        "cemetery",
        "chauffeur_service",
        "child_care_agency",
        "consultant",
        "courier_service",
        "electrician",
        "employment_agency",
        "florist",
        "food_delivery",
        "foot_care",
        "funeral_home",
        "hair_care",
        "hair_salon",
        "insurance_agency",
        "laundry",
        "lawyer",
        "locksmith",
        "makeup_artist",
        "marketing_consultant",
        "moving_company",
        "nail_salon",
        "non_profit_organization",
        "painter",
        "pet_boarding_service",
        "pet_care",
        "plumber",
        "psychic",
        "real_estate_agency",
        "roofing_contractor",
        "service",
        "shipping_service",
        "storage",
        "summer_camp_organizer",
        "tailor",
        "telecommunications_service_provider",
        "tour_agency",
        "tourist_information_center",
        "travel_agency",
        "veterinary_care",
        "asian_grocery_store",
        "auto_parts_store",
        "bicycle_store",
        "book_store",
        "building_materials_store",
        "butcher_shop",
        "cell_phone_store",
        "clothing_store",
        "convenience_store",
        "cosmetics_store",
        "department_store",
        "discount_store",
        "discount_supermarket",
        "electronics_store",
        "farmers_market",
        "flea_market",
        "food_store",
        "furniture_store",
        "garden_center",
        "general_store",
        "gift_shop",
        "grocery_store",
        "hardware_store",
        "health_food_store",
        "home_goods_store",
        "home_improvement_store",
        "hypermarket",
        "jewelry_store",
        "liquor_store",
        "market",
        "pet_store",
        "shoe_store",
        "shopping_mall",
        "sporting_goods_store",
        "sportswear_store",
        "store",
        "supermarket",
        "tea_store",
        "thrift_store",
        "toy_store",
        "warehouse_store",
        "wholesaler",
        "womens_clothing_store",
        "arena",
        "athletic_field",
        "fishing_charter",
        "fishing_pier",
        "fishing_pond",
        "fitness_center",
        "golf_course",
        "gym",
        "ice_skating_rink",
        "indoor_golf_course",
        "playground",
        "race_course",
        "ski_resort",
        "sports_activity_location",
        "sports_club",
        "sports_coaching",
        "sports_complex",
        "sports_school",
        "stadium",
        "swimming_pool",
        "tennis_court",
        "airport",
        "airstrip",
        "bike_sharing_station",
        "bridge",
        "bus_station",
        "bus_stop",
        "ferry_service",
        "ferry_terminal",
        "heliport",
        "international_airport",
        "light_rail_station",
        "park_and_ride",
        "subway_station",
        "taxi_service",
        "taxi_stand",
        "toll_station",
        "train_station",
        "train_ticket_office",
        "tram_stop",
        "transit_depot",
        "transit_station",
        "transit_stop",
        "transportation_service",
        "truck_stop",
    ]

    prompt = f"""
    You are a strategic analyst specializing in the Mumbai and Navi Mumbai retail market.
    Your task is to identify relevant Google Maps 'place types' from a provided list based on a user's query.
    
    User Query: "{{user_query}}"
    
    List of allowed place types:
    {{all_types}}
    
    Instructions:
    1. Identify which types from the list are highly relevant to the user's intent in the query.
    2. Focus on categories that would provide useful competitive or catchment insights for a retailer.
    3. Return ONLY a valid JSON list of strings containing the selected types.
    4. If no specific types are relevant, return a balanced default list related to general retail and footfall (e.g., ["shopping_mall", "supermarket", "transit_station"]).
    
    Return strictly the JSON list.
    """

    try:
        response = client.models.generate_content(
            model=os.getenv("MODEL"), contents=prompt
        )
        # Clean up response text case of markdown formatting
        text = response.text.strip()
        if text.startswith("```json"):
            text = text.replace("```json", "").replace("```", "").strip()
        elif text.startswith("```"):
            text = text.replace("```", "").strip()

        result = json.loads(text)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.error("Error selecting place types with Gemini: %s", e)
        return ["shopping_mall", "supermarket", "transit_station"]
