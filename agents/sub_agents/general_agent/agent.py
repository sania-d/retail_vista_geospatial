"""General Agent module for Retail Vista.

This module initializes the general agent and its associated cloud tools.
"""

import logging
import os

from dotenv import load_dotenv
import google.auth
from google.auth.exceptions import GoogleAuthError
from google.adk.agents import Agent
from google.adk.tools.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.tools.bigquery.config import BigQueryToolConfig, WriteMode
import vertexai

# --- Cross-Environment Imports ---
from .custom_tools import (
    calculate_optimal_radius,
    fetch_places_data_parallel,
    identify_coordinates,
)
from .prompt import get_general_agent_instructions
from .sub_agents.filter_agent.agent import root_agent as filter_agent

load_dotenv()


MODEL = os.getenv("MODEL")
logger = logging.getLogger(__name__)

# --- Cloud Tooling Initialization ---
bigquery_mcp_toolset = None
try:
    PROJECT_ID = os.getenv("PROJECT_ID")
    LOCATION = os.getenv("LOCATION")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    logger.info("Vertex AI initialized.")

    # big query toolset
    tool_config = BigQueryToolConfig(write_mode=WriteMode.BLOCKED)
    application_default_credentials, _ = google.auth.default()
    credentials_config = BigQueryCredentialsConfig(
        credentials=application_default_credentials
    )
    bigquery_mcp_toolset = BigQueryToolset(
        bigquery_tool_config=tool_config, tool_filter=["execute_sql", "forecast"]
    )
    logger.info("BigQuery toolset initialized successfully.")
except (GoogleAuthError, AttributeError, ImportError) as e:
    logger.warning("Could not initialize Cloud tools (BigQuery/Vertex): %s", e)
    logger.info("Continuing without BigQuery toolset.")
except Exception as e:
    logger.warning("Could not initialize Cloud tools (BigQuery/Vertex): %s", e)
    logger.info("Continuing without BigQuery toolset.")

tools_list = [
    identify_coordinates,
    calculate_optimal_radius,
    fetch_places_data_parallel,
]
if bigquery_mcp_toolset:
    tools_list.append(bigquery_mcp_toolset)

# Define the General Agent
root_agent = Agent(
    name="general_agent",
    model=MODEL,
    instruction=get_general_agent_instructions,
    sub_agents=[filter_agent],
    tools=tools_list,
    disallow_transfer_to_parent=True,
)
