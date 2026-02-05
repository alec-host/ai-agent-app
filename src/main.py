import os
import json
import uuid
import time
import logging
import httpx

import sentry_sdk

from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, Header, HTTPException, Request, Depends, status

from src.tools import TOOLS
from src.prompts import get_legal_system_prompt
from src.config import settings
from src.logger import logger

from pydantic import BaseModel
from openai import AsyncOpenAI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # [STARTUP LOGIC]
    # Initialize shared resources
    app.state.http_client = httpx.AsyncClient(
        base_url=settings.NODE_SERVICE_URL,
        headers={"User-Agent": "Legal-AI-Agent/1.0"},
        verify=False,
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

from fastapi import APIRouter, status

# --- 1. Health Check Endpoint ---
@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """
    Verifies the Agentic AI service is running and can reach 
    the Node.js calendar backend.
    """
    health_status = {
        "service": settings.APP_NAME,
        "status": "online",
        "dependencies": {
            "node_backend": "unknown",
            "openai_api": "connected" # Basic check assuming key is loaded
        }
    }

    try:
        # Ping the Node.js service (adjust path if your Node app has its own /health)
        # We use a short timeout so the health check doesn't hang
        #--->response = await app.state.http_client.get("/", timeout=2.0)
        response = await app.state.http_client.get("/", timeout=5.0)
        
        if response.status_code < 500:
            health_status["dependencies"]["node_backend"] = "reachable"
        else:
            health_status["dependencies"]["node_backend"] = "error_response"
            
    except Exception as e:
        # This will print the full technical error to your console
        print(f"DEBUG: Connection to {settings.NODE_SERVICE_URL} failed!")
        print(f"ERROR TYPE: {type(e).__name__}")
        print(f"ERROR MESSAGE: {str(e)}")        
        
        logger.error(f"Health check failed to reach Node: {str(e)}") # Add this line
        health_status["status"] = "degraded"
        health_status["dependencies"]["node_backend"] = f"unreachable: {str(e)}"

    return health_status

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
    def __init__(self, tenant_id: str, http_client: httpx.AsyncClient, correlation_id: str):
        # 1. Store the correlation_id and update headers
        self.correlation_id = correlation_id
        self.headers = {
            "X-Tenant-ID": tenant_id,
            "X-Correlation-ID": correlation_id  # Pass the ID to Node.js
        }
        self.client = http_client 
        self.timeout = httpx.Timeout(15.0)

    async def request(self, method: str, path: str, json_data: dict = None):
        # 2. Use the SHARED self.client instead of creating a new one
        url = f"{NODE_SERVICE_URL}{path}"
        
        # Log with the Correlation ID for easier tracing
        logger.info(f"[{self.correlation_id}] Agent calling Node Service: {method} {url}")
        
        try:
            response = await self.client.request(
                method, 
                url, 
                json=json_data, 
                headers=self.headers, # Use our updated headers
                timeout=self.timeout
            )
            
            if response.status_code >= 400:
                logger.error(f"[{self.correlation_id}] Node Service Error: {response.text}")
                return {"error": "Backend service error", "status": response.status_code}
            
            return response.json()
            
        except Exception as e:
            logger.error(f"[{self.correlation_id}] Connection to Node failed: {str(e)}")
            return {"error": "Connection failed"}

# --- 5. Request Models ---
class ChatRequest(BaseModel):
    prompt: str

# --- 6. The Core Reasoning Endpoint ---
@app.post("/ai/chat")
async def handle_agent_query(
    req: ChatRequest, 
    request: Request,
    auth: dict = Depends(verify_tenant_access)
):
    tenant_id = auth["tenant_id"]
    user_role = auth["role"]
    
    corr_id = request.state.correlation_id
    
    calendar = CalendarServiceClient(
        tenant_id,
        request.app.state.http_client,
        correlation_id=corr_id
    )
    
    # GENERATE THE DYNAMIC PROMPT
    system_instructions = get_legal_system_prompt(tenant_id, user_role)

    # 1. Consult the LLM
    messages = [
        {"role": "system", "content": system_instructions},
        {"role": "user", "content": req.prompt}
    ]
    
    ai_client = request.app.state.ai_client
    
    response = await ai_client.chat.completions.create(
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
        
        # High-visibility log for the agent's "thinking"
        logger.warning(f"🤖 AGENT DECISION: Calling {func_name} with args {args}")        

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
async def trigger_sync(request: Request, auth: dict = Depends(verify_tenant_access)):
    tenant_id = auth["tenant_id"]
    
    calendar = CalendarServiceClient(
        tenant_id, 
        request.app.state.http_client
    )
    
    return await calendar.request("POST", "/events/sync-google")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    
    # Extract info for the log
    method = request.method
    path = request.url.path
    tenant_id = request.headers.get("X-Tenant-ID", "N/A")
    
    with sentry_sdk.configure_scope() as scope:
        scope.set_tag("tenant_id", tenant_id)

    # Process the request
    response = await call_next(request)
    
    # Calculate duration
    process_time = (time.time() - start_time) * 1000
    
    # Log the results
    logger.info(
        f"REQ: {method} {path} | Tenant: {tenant_id} | "
        f"Status: {response.status_code} | Time: {process_time:.2f}ms"
    )
    
    return response

@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    # 1. Generate or Capture the ID
    # We check the header first so if your Frontend generates an ID, we reuse it.
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    
    # 2. Attach to request.state 
    # This is the "secret sauce" that lets your /ai/chat route find it later.
    request.state.correlation_id = correlation_id
    
    # 3. Tag Sentry
    # This ensures any crash reported to Sentry includes the ID automatically.
    sentry_sdk.set_tag("correlation_id", correlation_id)

    start_time = time.time()
    
    try:
        # 4. Process the request
        response = await call_next(request)
        
        # 5. Inject the ID into the outgoing Response headers
        # This allows you to see the ID in your Browser Inspect -> Network tab.
        response.headers["X-Correlation-ID"] = correlation_id
        
        duration = (time.time() - start_time) * 1000
        logger.info(f"[{correlation_id}] {request.method} {request.url.path} - {response.status_code} ({duration:.2f}ms)")
        
        return response

    except Exception as e:
        # 6. Log failures with the ID so you can find the exact trace
        logger.error(f"[{correlation_id}] Request failed: {str(e)}")
        raise e