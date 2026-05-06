"""Module for defining the Competitor Analyzer Sub-Agent."""

import logging
import os

from dotenv import load_dotenv
from google.adk.agents import Agent

from .custom_tools import lookup_pincode, run_competitor_analysis
from .prompt import get_competitor_analyzer_instructions

load_dotenv()

MODEL = os.getenv("MODEL")
logger = logging.getLogger(__name__)

# Define the Competitor Analyzer Sub-Agent
root_agent = Agent(
    name="competitor_analyzer_agent",
    model=MODEL,
    instruction=get_competitor_analyzer_instructions,
    tools=[run_competitor_analysis, lookup_pincode],
    disallow_transfer_to_parent=True,
)
