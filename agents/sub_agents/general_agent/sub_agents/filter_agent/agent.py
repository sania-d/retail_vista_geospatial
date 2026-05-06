"""Filter Agent module."""

import logging
import os

from dotenv import load_dotenv
from google.adk.agents import Agent

# Correct relative imports by using absolute paths to avoid E0402 error
from agents.sub_agents.general_agent.sub_agents.filter_agent.custom_tools import (
    get_table_description,
    get_table_sample_data,
    get_unique_column_values,
    identify_default_coordinates,
    identify_default_radius,
    query_bq_table_geospatial_radius,
)
from agents.sub_agents.general_agent.sub_agents.filter_agent.prompt import (
    get_filter_agent_instructions,
)

load_dotenv()

MODEL = os.getenv("MODEL")
logger = logging.getLogger(__name__)

root_agent = Agent(
    name="filter_agent",
    model=MODEL,
    instruction=get_filter_agent_instructions,
    tools=[
        get_table_description,
        get_table_sample_data,
        get_unique_column_values,
        query_bq_table_geospatial_radius,
        identify_default_coordinates,
        identify_default_radius,
    ],
    disallow_transfer_to_parent=True,
)
