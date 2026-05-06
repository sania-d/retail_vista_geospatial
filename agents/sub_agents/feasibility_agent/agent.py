"""Module defining the feasibility agent Sub-Agent."""

import os
import logging
import sys
from google.adk.agents import Agent
from agents.sub_agents.feasibility_agent.custom_tools import (
    setup_feasibility_polygon,
    fetch_feasibility_data_parallel,
    fetch_places_data_parallel,
    generate_feasibility_report,
)
from agents.sub_agents.feasibility_agent.prompt import FEASIBILITY_PIPELINE_INSTRUCTIONS

# Setup absolute path fallback for root tools
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, "..", ".."))
if root_dir not in sys.path:
    sys.path.append(root_dir)

MODEL = os.getenv("MODEL")
logger = logging.getLogger(__name__)

# Define the Feasibility Pipeline Sub-Agent
root_agent = Agent(
    name="feasibility_agent",
    model=MODEL,
    instruction=FEASIBILITY_PIPELINE_INSTRUCTIONS,
    tools=[
        setup_feasibility_polygon,
        fetch_feasibility_data_parallel,
        fetch_places_data_parallel,
        generate_feasibility_report,
    ],
    disallow_transfer_to_parent=True,
)
