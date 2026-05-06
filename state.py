"""
Shared plain dictionary state definitions for analysis.
"""

from __future__ import annotations
from typing import List, Optional, Dict
import json

GLOBAL_STATE_STORE = {}


def debug_dump_state(state_obj, label="STATE UPDATE", thread_id="default"):
    """Helper to dump the raw tool_context.state to disk for debugging endpoints."""

    class SafeEncoder(json.JSONEncoder):
        def default(self, obj):
            if hasattr(obj, "model_dump"):
                return obj.model_dump()
            elif isinstance(obj, set):
                return list(obj)
            return str(obj)

    try:
        if hasattr(state_obj, "model_dump"):
            data = state_obj.model_dump(mode="json")
        elif isinstance(state_obj, dict):
            # Safe native dictionary copy
            data = dict(state_obj)
        else:
            # Proxy object from CopilotKit. Avoid `dict(state_obj)` as it evaluates `state_obj[0]` and raises KeyError: 0.
            data = {}
            if hasattr(state_obj, "keys"):
                for k in state_obj.keys():
                    data[k] = state_obj[k]
            elif hasattr(state_obj, "__dict__"):
                data = dict(state_obj.__dict__)
            else:
                try:
                    data = dict(state_obj)
                except Exception:
                    data = {"_proxy_repr": repr(state_obj)}

        if "bq_context" in data:
            data.pop("bq_context")

        # print(f"\n======== {label} ========")
        # print(json.dumps(data["_value"], indent=2, cls=SafeEncoder))
        # print("=============================\n")

        # Save to in-memory store instead of file
        # Automatically determine thread_id if present in state
        # Gracefully handle both _value (to skip _delta) and plain dictionaries
        actual_state = data
        if isinstance(data, dict) and "_value" in data:
            actual_state = data["_value"]
        elif hasattr(state_obj, "_value"):
            actual_state = getattr(state_obj, "_value")

        extracted_thread_id = (
            actual_state.get("_ag_ui_thread_id", thread_id)
            if isinstance(actual_state, dict)
            else thread_id
        )
        GLOBAL_STATE_STORE[extracted_thread_id] = actual_state

        if extracted_thread_id != "default":
            GLOBAL_STATE_STORE["default"] = actual_state

        # We can still write to file for backward compatibility or direct inspection,
        # but the primary storage is now in-memory. Let's keep file for now to avoid breaking anything else immediately.
        # with open(f"{thread_id}.json", "w") as f:
        #     json.dump(data["_value"], f, indent=2, cls=SafeEncoder)

    except Exception as e:
        import traceback

        print(f"Failed to dump state: {repr(e)}")
        traceback.print_exc()
