"""This module defines the Catchment Analyzer Sub-Agent."""

import logging
import os

from dotenv import load_dotenv
from google.adk.agents import Agent

from .custom_tools import (
    calculate_catchment_radius,
    fetch_catchment_data_parallel,
    fetch_places_data_parallel,
    identify_coordinates,
)
from .prompt import get_catchment_analyzer_instructions

load_dotenv()

MODEL = os.getenv("MODEL")
logger = logging.getLogger(__name__)

# Define the Catchment Analyzer Sub-Agent
root_agent = Agent(
    name="catchment_analyzer_agent",
    model=MODEL,
    instruction=get_catchment_analyzer_instructions,
    tools=[
        calculate_catchment_radius,
        identify_coordinates,
        fetch_catchment_data_parallel,
        fetch_places_data_parallel,
    ],
    disallow_transfer_to_parent=True,
)
