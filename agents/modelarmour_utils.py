"""Module containing utilities for interacting with Google Cloud Model Armor."""

import os
from typing import Optional

from dotenv import load_dotenv

from google.cloud import secretmanager
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse
from google.cloud import modelarmor_v1
from google.api_core.client_options import ClientOptions
from google.genai import types

load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")
MODEL_ARMOUR_TEMPLATE_ID = os.getenv("MODEL_ARMOUR_TEMPLATE_ID")
MODEL_ARMOUR_LOCATION = os.getenv("MODEL_ARMOUR_LOCATION")


# Construct the full template resource name
template_path = (
    f"projects/{PROJECT_ID}/locations/{MODEL_ARMOUR_LOCATION}/"
    f"templates/{MODEL_ARMOUR_TEMPLATE_ID}"
)

# Initialize the client
armor_client = modelarmor_v1.ModelArmorClient(
    client_options=ClientOptions(
        api_endpoint=f"modelarmor.{MODEL_ARMOUR_LOCATION}.rep.googleapis.com"
    )
)


def model_armor_guard(callback_context: CallbackContext) -> Optional[LlmResponse]:
    """
    Intercepts the user prompt before it reaches the LLM,
    evaluating it against the Model Armor template.
    """
    if not MODEL_ARMOUR_TEMPLATE_ID or not MODEL_ARMOUR_LOCATION:
        print("[ModelArmor] Disabled: template ID or location not set.")
        return None

    last_user_message = ""

    if callback_context.user_content:
        last_user_message = callback_context.user_content.parts[-1].text
    print(f"[Callback] Inspecting user message length: {len(last_user_message)}")

    # Package the prompt for Model Armor inspection
    sanitize_request = modelarmor_v1.SanitizeUserPromptRequest(
        name=template_path,
        user_prompt_data=modelarmor_v1.DataItem(text=last_user_message),
    )

    # Execute the template rules against the prompt
    response = armor_client.sanitize_user_prompt(request=sanitize_request)

    # Evaluate the result: Block the request if a critical threat is found
    if response.sanitization_result.filter_match_state.name == "MATCH_FOUND":

        # Iterate over the specific filter results to identify which safety policy was violated.
        # This allows us to provide a more specific rejection message.
        # Iterate over filter results and set message
        message = ""

        for key, value in response.sanitization_result.filter_results.items():
            if (
                key == "pi_and_jailbreak"
                and value.pi_and_jailbreak_filter_result.match_state.name
                == "MATCH_FOUND"
            ):
                print("Jailbreak detected!")
                message += "Jailbreak detected!\n"
            elif (
                key == "sdp"
                and value.sdp_filter_result.inspect_result.match_state.name
                == "MATCH_FOUND"
            ):
                print("Sensitive personal information detected!")
                message += "Sensitive personal information detected!\n"
            elif (
                key == "rai"
                and value.rai_filter_result.match_state.name == "MATCH_FOUND"
            ):
                print("Harmful content detected!")
                message += "Harmful content detected!\n"
            elif (
                key == "malicious_uris"
                and value.malicious_uri_filter_result.match_state.name == "MATCH_FOUND"
            ):
                print("Malicious URIs detected!")
                message += "Malicious URIs detected!\n"
            elif (
                key == "csam"
                and value.csam_filter_filter_result.match_state.name == "MATCH_FOUND"
            ):
                print("CSAM detected!")
                message += "CSAM detected!\n"

        return types.Content(
            role="model",
            parts=[types.Part(text=f"""REQUEST BLOCKED BY MODEL ARMOUR : {message}""")],
        )

    return None


def access_secret_version(secret_id, version_id="latest"):
    """Accesses the payload for the given secret version if one exists."""
    # Create the Secret Manager client.
    client = secretmanager.SecretManagerServiceClient()

    # Build the resource name of the secret version.
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/{version_id}"

    # Access the secret version.
    response = client.access_secret_version(request={"name": name})

    # Return the decoded payload.
    return response.payload.data.decode("UTF-8")
