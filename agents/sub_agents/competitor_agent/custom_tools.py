"""This module contains the custom tools used by the competitor agent."""

import json
import logging
from typing import Optional
from google.adk.tools import ToolContext
from agents.query_bq_spatial_utils import lookup_pincode as lookup_pincode_util
from .competitors_pipeline import run_competitor_analysis_pipeline

logger = logging.getLogger(__name__)


def lookup_pincode(area_or_city: str, _tool_context: ToolContext) -> str:
    """
    Looks up the 6-digit Pincode for a given area name (e.g., 'Borivali')
    from the BigQuery pincode boundaries table.
    Use this if you have an area name but need a pincode for analysis.

    Args:
        area_or_city: The name of the area or city or region to lookup
            (e.g., 'Borivali', 'Mumbai', 'Maharashtra').
        tool_context: Framework context for state access.
    """
    logger.info("Tool lookup_pincode called for area: %s", area_or_city)
    try:
        return lookup_pincode_util(area_or_city)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Error in lookup_pincode tool: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


def run_competitor_analysis(
    pincode: str,
    tool_context: ToolContext,
    bq_filter_condition: Optional[str] = "",
    user_query: Optional[str] = None,
) -> str:
    """
    Performs competitor analysis for a given area defined by pincodes.
    It identifies Reliance stores and competitor stores, generates H3 hex grid visualization,
    and provides LLM-based insights.

    Args:
        pincode: Comma-separated string of 6-digit pincodes to analyze.
        tool_context: Framework context for state access.
        bq_filter_condition: Optional SQL filter condition for Reliance stores
            (e.g., "store_type = 'Digital'").
        user_query: Optional original user query to identify broader intent.
    """
    logger.info("Tool run_competitor_analysis called for pincode: %s", pincode)
    try:
        result = run_competitor_analysis_pipeline(
            pincode=pincode,
            bq_filter_condition=bq_filter_condition,
            tool_context=tool_context,
            user_query=user_query,
        )
        return result
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Error in run_competitor_analysis tool: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
