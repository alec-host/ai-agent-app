import os
import json
import logging
import httpx

from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, Header, HTTPException, Depends, status

from src.config import settings
from pydantic import BaseModel
from openai import AsyncOpenAI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # [STARTUP LOGIC]
    # Initialize shared resources
    app.state.http_client = httpx.AsyncClient(
        base_url=settings.NODE_SERVICE_URL,
        timeout=httpx.Timeout(15.0)
    )
    app.state.ai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    
    print(f"--- {settings.APP_NAME} Started Successfully ---")
    
    yield  # --- The app runs here ---

    # [SHUTDOWN LOGIC]
    # This runs when you hit Ctrl+C or stop the process
    print(f"--- {settings.APP_NAME} Shutting Down ---")
    
    # Close the global HTTP client pool
    await app.state.http_client.aclose()
    
    # If you later add a real DB engine (like SQLAlchemy):
    # await engine.dispose()
    
    print("Resources cleaned up. Goodbye.")

app = FastAPI(title=settings.APP_NAME,description=settings.APP_DESCRIPTION, lifespan=lifespan)
#client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("legal-agentic-ai")

# --- 2. Security & Multi-tenancy Guardrails ---
async def verify_tenant_access(
    x_tenant_id: str = Header(...), 
    user_role: str = Header("Associate")
):
    """
    Dependency to ensure every request carries a Tenant ID and User Role.
    This acts as the primary guardrail for the Agent.
    """
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="X-Tenant-ID is required.")
    return {"tenant_id": x_tenant_id, "role": user_role.lower()}

# --- 3. Node.js Backend API Client ---
class CalendarServiceClient:
    """Async client to communicate with the existing Node.js Microservice."""
    def __init__(self, tenant_id: str):
        self.headers = {"X-Tenant-ID": tenant_id}
        self.timeout = httpx.Timeout(15.0)

    async def request(self, method: str, path: str, json_data: dict = None):
        async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout) as ac:
            url = f"{NODE_SERVICE_URL}{path}"
            logger.info(f"Agent calling Node Service: {method} {url}")
            response = await ac.request(method, url, json=json_data)
            
            if response.status_code >= 400:
                logger.error(f"Node Service Error: {response.text}")
                return {"error": "Backend service error", "status": response.status_code}
            return response.json()

# --- 4. Agentic AI Tool Schemas ---
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_all_events",
            "description": "Retrieves all calendar events for the current tenant."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_event_by_id",
            "description": "Gets specific details for a calendar event using its ID.",
            "parameters": {
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_event",
            "description": "Creates a new custom calendar event or deadline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start_time": {"type": "string", "description": "ISO format"},
                    "end_time": {"type": "string", "description": "ISO format"}
                },
                "required": ["title", "start_time", "end_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": "Deletes a calendar event. REQUIRES ADMIN ROLE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "confirmed": {"type": "boolean", "description": "Must be true to execute"}
                },
                "required": ["event_id", "confirmed"]
            }
        }
    }
]

# --- 5. Request Models ---
class ChatRequest(BaseModel):
    prompt: str

# --- 6. The Core Reasoning Endpoint ---
@app.post("/ai/chat")
async def handle_agent_query(
    req: ChatRequest, 
    auth: dict = Depends(verify_tenant_access)
):
    tenant_id = auth["tenant_id"]
    user_role = auth["role"]
    calendar = CalendarServiceClient(tenant_id)

    # 1. Consult the LLM
    messages = [
        {"role": "system", "content": f"You are a legal AI assistant. Current Tenant: {tenant_id}. Role: {user_role}. "
                                     "You cannot delete events unless the user is an 'admin' and has confirmed."},
        {"role": "user", "content": req.prompt}
    ]
    
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=TOOLS,
        tool_choice="auto"
    )

    message = response.choices[0].message
    
    # 2. Handle Tool Calls
    if message.tool_calls:
        tool_call = message.tool_calls[0]
        func_name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)

        # Audit Log Entry
        logger.info(f"AUDIT: Tenant {tenant_id} | User {user_role} | Action {func_name}")

        if func_name == "get_all_events":
            return await calendar.request("GET", "/events")

        if func_name == "get_event_by_id":
            return await calendar.request("GET", f"/events/{args['event_id']}")

        if func_name == "schedule_event":
            return await calendar.request("POST", "/events", args)

        if func_name == "delete_event":
            # REJECTION: Non-admin
            if user_role != "admin":
                return {"response": "Access Denied: Only administrators can delete events."}
            # REJECTION: No confirmation
            if not args.get("confirmed"):
                return {"response": "I need your explicit confirmation to delete this event. Should I proceed?"}
            
            return await calendar.request("DELETE", f"/events/{args['event_id']}")

    # 3. Default Text Response
    return {"response": message.content}

# --- 7. Utility Route for Google Sync ---
@app.post("/ai/sync")
async def trigger_sync(auth: dict = Depends(verify_tenant_access)):
    calendar = CalendarServiceClient(auth["tenant_id"])
    return await calendar.request("POST", "/events/sync-google")