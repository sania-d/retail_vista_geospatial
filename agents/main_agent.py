"""This module defines the main agent and orchestrates the sub-agents."""

import os
from dotenv import load_dotenv
from google.adk.agents import Agent
from agents.sub_agents.catchment_agent.agent import root_agent as catchment_agent
from agents.sub_agents.general_agent.agent import root_agent as general_agent
from agents.sub_agents.competitor_agent.agent import (
    root_agent as competitor_analyzer_agent,
)
from agents.sub_agents.feasibility_agent.agent import root_agent as feasibility_agent
from agents.modelarmour_utils import model_armor_guard
from .prompt import MAIN_AGENT_INSTRUCTIONS

load_dotenv()

MODEL = os.getenv("MODEL")

root_agent = Agent(
    name="main_agent",
    model=MODEL,
    instruction=MAIN_AGENT_INSTRUCTIONS,
    sub_agents=[
        catchment_agent,
        general_agent,
        competitor_analyzer_agent,
        feasibility_agent,
    ],
    tools=[],
    before_agent_callback=[model_armor_guard],
)
