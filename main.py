"""Module for the main FastAPI application."""

import os
from dotenv import load_dotenv

load_dotenv()

from ag_ui_adk import ADKAgent, add_adk_fastapi_endpoint
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.cloud.sql.connector import create_async_connector
import uvicorn
from agents.main_agent import root_agent

remote_deployment = eval(os.getenv("remote_deployment"))


if remote_deployment == False:
    session_service = InMemorySessionService()
else:
    db_pwd = os.getenv("db_pass_secret")

    async def get_async_conn():
        """Creates an asynchronous connection to Cloud SQL."""
        connector = await create_async_connector(ip_type="PRIVATE")

        async def getconn():
            conn = await connector.connect_async(
                os.getenv("INSTANCE_CONNECTION_NAME"),
                "asyncpg",
                user=os.getenv("DB_USER"),
                password=db_pwd,
                db=os.getenv("DB_NAME"),
            )
            return conn

        sql_conn = await getconn()
        return sql_conn

    session_service = DatabaseSessionService(
        db_url="postgresql+asyncpg://", async_creator=get_async_conn
    )
# --- AG-UI Wrapper ---
ag_ui_agent = ADKAgent(
    adk_agent=root_agent,
    app_name="default",
    session_service=session_service,
    use_thread_id_as_session_id=True,
    cleanup_interval_seconds=400,  # 5 minutes default
    max_sessions_per_user=None,  # No limit by default
    delete_session_on_cleanup=False,
    save_session_to_memory_on_cleanup=False,
)
from fastapi.middleware.cors import CORSMiddleware

# --- FastAPI Server ---
app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173"
    ],  # Specify origin when allow_credentials is True
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/state")
def get_debug_state(thread_id: str = "default"):
    """Debug route for the frontend to inspect recently modified tool_context.state."""
    from state import GLOBAL_STATE_STORE

    # Try in-memory first
    state_data = GLOBAL_STATE_STORE.get(thread_id)
    if state_data:
        return JSONResponse(content=state_data)

    return JSONResponse(
        content={"error": f"No state found for thread '{thread_id}' in memory."}
    )


@app.get("/api/bq/tables")
def list_bq_tables():
    """Get all tables present in the BQ dataset."""
    from google.cloud import bigquery
    import os

    project_id = os.getenv("PROJECT_ID")
    dataset_id = os.getenv("DATASET_ID")

    if not project_id or not dataset_id:
        return JSONResponse(
            status_code=500,
            content={
                "error": "PROJECT_ID or DATASET_ID environment variables are not set."
            },
        )

    try:
        client = bigquery.Client(project=project_id)
        dataset_ref = f"{project_id}.{dataset_id}"
        tables = list(client.list_tables(dataset_ref))

        result = []
        for table in tables:
            result.append(
                {
                    "table_id": table.table_id,
                    "full_table_id": table.full_table_id,
                    "table_type": table.table_type,
                }
            )

        return JSONResponse(
            content={"status": "success", "dataset": dataset_ref, "tables": result}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to list BigQuery tables: {str(e)}"},
        )


@app.get("/api/sql/latest-state")
async def get_sql_state(id: str = None, user_id: str = None):
    """Fetches the latest session state from Cloud SQL based on user_id or id."""
    import os
    from google.cloud.sql.connector import create_async_connector

    instance_connection_name = os.getenv("INSTANCE_CONNECTION_NAME")
    db_user = os.getenv("DB_USER")
    db_name = os.getenv("DB_NAME")
    db_pwd = os.getenv("db_pass_secret")

    if not instance_connection_name:
        return JSONResponse(
            status_code=500,
            content={
                "error": "INSTANCE_CONNECTION_NAME is not set; cannot access Cloud SQL."
            },
        )

    query = "SELECT state, create_time, update_time FROM public.sessions"
    conditions = []
    params = []

    if id:
        params.append(id)
        conditions.append(f"id = ${len(params)}")
    if user_id:
        params.append(user_id)
        conditions.append(f"user_id = ${len(params)}")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY update_time DESC LIMIT 1"

    try:
        connector = await create_async_connector(ip_type="PRIVATE")
        conn = await connector.connect_async(
            instance_connection_name,
            "asyncpg",
            user=db_user,
            password=db_pwd,
            db=db_name,
        )

        # Fetch the record using asyncpg
        row = await conn.fetchrow(query, *params)
        await conn.close()

        if not row:
            return JSONResponse(
                content={
                    "status": "empty",
                    "message": "No session found matching criteria.",
                }
            )

        # Build the response object
        res = {
            "state": row["state"],
            "create_time": (
                row["create_time"].isoformat()
                if hasattr(row["create_time"], "isoformat")
                else str(row["create_time"])
            ),
            "update_time": (
                row["update_time"].isoformat()
                if hasattr(row["update_time"], "isoformat")
                else str(row["update_time"])
            ),
        }
        return JSONResponse(content={"status": "success", "data": res})
    except Exception as e:
        return JSONResponse(
            status_code=500, content={"error": f"Cloud SQL query failed: {str(e)}"}
        )


# Register the canonical ag_ui_adk endpoint
add_adk_fastapi_endpoint(
    app,
    ag_ui_agent,
    path="/agents/copilotkit",
    # _inject_state_in_tool_context=True,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")
