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
from src.agent_manager import execute_tool_call

from src.utils import sanitize_history

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
        url = f"{settings.NODE_SERVICE_URL}{path}"
        
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
            logger.error(f"[{self.correlation_id}] ❌ Backend Service Error:: {str(e)}")
            return {"error": "Service Temporarily Unavailable","technical_details": str(e),"user_friendly_message": "I'm having trouble connecting to the calendar system right now. Please try again in a moment."}

# --- 5. Request Models ---
class ChatMessage(BaseModel):
    role: str # "user" or "assistant" or "tool"
    content: Optional[str] = None
    tool_calls: Optional[list] = None # To track tool context
    
class ChatRequest(BaseModel):
    prompt: str
    history: Optional[List[ChatMessage]] = []

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
    
    services = {
        "calendar": CalendarServiceClient(
            tenant_id,
            request.app.state.http_client,
            correlation_id=corr_id
        )
    }
    
    system_instructions = get_legal_system_prompt(tenant_id, user_role)
    messages = [{"role": "system", "content": system_instructions}]
    
    # 1. Process History
    if req.history:
        recent_history = req.history[-6:]
        # Then, we trim any massive content strings
        clean_history = sanitize_history(recent_history)
        for msg in clean_history:
            # Changed to exclude_none=True (standard Pydantic)
            # messages.append(msg.dict(exclude_none=True))
            messages.append(msg)
    
    messages.append({"role": "user", "content": req.prompt})
    
    ai_client = request.app.state.ai_client
    
    # 2. First AI Call
    response = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=TOOLS,
        tool_choice="auto"
    )

    assistant_message = response.choices[0].message
    
    # --- THE CRITICAL FIX START ---
    # You MUST add the assistant's decision to the messages list
    # so the subsequent 'tool' messages have a parent.
    messages.append(assistant_message)
    # --- THE CRITICAL FIX END ---
    
    if assistant_message.tool_calls:
        for tool_call in assistant_message.tool_calls:
            result_data = await execute_tool_call(
                tool_call,
                services,
                user_role,
                tenant_id 
            )
            
            # Add the tool result
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.function.name,
                "content": json.dumps(result_data)
            })

        # 3. Second AI Call: Generate human-friendly response
        second_response = await ai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        ) 
        
        final_output = second_response.choices[0].message.content
        return {"response": final_output, "history": messages[1:]}
        
    # If no tools were called, return the direct response
    return {"response": assistant_message.content, "history": messages[1:]}
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