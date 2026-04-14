# MatterMiner Core: Login → API Key Authorization Migration

> **Migration Date:** 2026-04-14
> **Status:** In Progress
> **Objective:** Replace the interactive email/password login workflow for all MatterMiner Core service calls with a static `Authorization` header powered by `CORE_API_KEY` from `config.py`.

---

## Table of Contents

1. [Current Architecture (As-Is)](#1-current-architecture-as-is)
2. [Target Architecture (To-Be)](#2-target-architecture-to-be)
3. [Impact Analysis & File Map](#3-impact-analysis--file-map)
4. [Implementation Phases](#4-implementation-phases)
5. [Error Handling & UX Strategy](#5-error-handling--ux-strategy)
6. [Testing Plan](#6-testing-plan)
7. [Rollback Strategy](#7-rollback-strategy)
8. [Change Log](#8-change-log)

---

## 1. Current Architecture (As-Is)

The system previously used a **multi-step interactive login** to authenticate with MatterMiner Core:

```
User → "Create a contact"
   → AI detects Core intent
   → Pre-flight: GET /hasGrantToken?email=...
   → Core returns 404 / Not Found
   → AI surfaces 🔒 Login Card (auth_required)
   → User enters email + password
   → AI calls POST /login {email, password}
   → Core returns {token: "jwt..."}
   → AI calls POST /contact (with Bearer token)
   → Core returns ✅ Contact Created
```

### Pain Points

| Problem | Impact | Original Location |
| :--- | :--- | :--- |
| User must manually login every session | High friction, workflow interruption | `main.py:L277-304` |
| `has_valid_token()` pre-flight call adds latency | Extra HTTP roundtrip per request | `main.py:L289-304` |
| `authenticate_to_core` tool exists as an LLM tool | AI sees passwords, security concern | `tools.py:L259-273` |
| Login card surfaces during active workflows | Breaks user flow mid-creation | `core_agent.py:L14-20` |
| 404 "Not Found" triggers login redirect | Conflates missing data with auth failure | `matterminer_core.py:L58-68` |

---

## 2. Target Architecture (To-Be)

Replace the entire login flow with a **server-side API key** injected into every Core request via `Authorization` header. The key is sourced from `settings.CORE_API_KEY` (defined in `config.py:L30`).

```
User → "Create a contact"
   → No login gate needed
   → AI calls POST /contact with Authorization: Bearer {CORE_API_KEY}
   → Core returns ✅ Contact Created
   → AI confirms to user
```

### Key Design Decisions

| Decision | Rationale |
| :--- | :--- |
| **API key injected at `MatterMinerCoreClient` level** | Single point of change; all downstream methods automatically inherit the header |
| **Remove `authenticate_to_core` tool entirely** | Eliminates password exposure in LLM context window |
| **Remove `has_valid_token()` pre-flight checks** | Eliminates latency; API key is always valid or rejected at the edge |
| **Differentiate 401/403 from 404** | 404 = "Not Found" (data issue), 401/403 = "Invalid API Key" (config issue) |
| **Keep `X-Tenant-ID` header** | Multi-tenancy is orthogonal to auth; API key authenticates the *service*, tenant header scopes the *data* |

---

## 3. Impact Analysis & File Map

### Files Modified

| # | File | Change Type | Risk |
| :--- | :--- | :--- | :--- |
| 1 | `src/remote_services/matterminer_core.py` | **Core** — Inject API key into headers, remove `login()`, update 404 handler | 🔴 High |
| 2 | `src/main.py` | **Core** — Remove pre-flight `has_valid_token()` gates in both `/ai/chat` and `/ai/chat/stream` | 🔴 High |
| 3 | `src/agents/core_agent.py` | **Core** — Remove `authenticate_to_core` handler, update auth_required responses | 🟡 Medium |
| 4 | `src/agent_manager.py` | **Medium** — Remove `authenticate_to_core` from `core_funcs` routing list | 🟡 Medium |
| 5 | `src/tools.py` | **Medium** — Remove `authenticate_to_core` tool definition | 🟢 Low |
| 6 | `src/prompts.py` | **Low** — Update LOGIN SAFETY prompt section | 🟢 Low |
| 7 | `src/utils.py` | **Low** — Review password/token redaction masks | 🟢 Low |
| 8 | `.agents/workflows/create-matter.md` | **Low** — Remove Step 2 (login card check) | 🟢 Low |

### Files NOT Modified (Verified Safe)

| File | Reason |
| :--- | :--- |
| `src/remote_services/google_core.py` | Google Calendar uses its own OAuth flow — completely separate from Core auth |
| `src/agents/calendar_agent.py` | Only handles Google Calendar auth, not Core auth |
| `src/remote_services/redis_memory.py` | No auth dependency |
| `src/remote_services/wallet_service.py` | Uses its own header passthrough |

---

## 4. Implementation Phases

### Phase 1: Transport Layer — Inject API Key into `MatterMinerCoreClient`

**File:** `src/remote_services/matterminer_core.py`

Changes:
- **1A.** Modified `_get_headers()` to inject `settings.CORE_API_KEY` as a static `Authorization: Bearer` header
- **1B.** Removed `login()` method — no longer called by any part of the system
- **1C.** Removed `set_auth_token()` and `is_authenticated()` methods
- **1D.** Removed `self.access_token` and `self.user_profile` from `__init__()`
- **1E.** Updated 404 response handler — 404 is now treated as data error; 401/403 are API key errors
- **1F.** Removed `has_valid_token()` method

### Phase 2: Remove Pre-Flight Auth Gates in Main Endpoints

**File:** `src/main.py`

Changes:
- **2A.** Removed Core auth gate from `/ai/chat` endpoint (was at L277-304)
- **2B.** Removed equivalent gate from `/ai/chat/stream` endpoint (was at L672-690)
- **2C.** Removed `is_login_attempt` keyword detection from both endpoints

### Phase 3: Remove `authenticate_to_core` from Agent Layer

Changes:
- **3A.** Removed `authenticate_to_core` tool definition from `src/tools.py`
- **3B.** Removed handler in `src/agents/core_agent.py` (`handle_core_ops()`)
- **3C.** Removed from dispatcher routing table in `src/agent_manager.py`

### Phase 4: Update System Prompt & Error Messages — COMPLETED

- Updated prompt guidance in `src/prompts.py` (LOGIN SAFETY → API KEY SAFETY)
- Renamed `_get_auth_required_response()` to `_get_api_key_error_response()` in `core_agent.py`
- Updated all 5 handler blocks (countries, event, contact, client, matter) to check for `api_key_error` instead of `auth_required`
- All error messages now surface admin-facing guidance instead of login card prompts

### Phase 5: Update Workflow Documentation — COMPLETED

- Updated `create-matter.md` workflow doc
- Removed login card reference in Step 2

### Phase 6: Testing — COMPLETED

Updated test files:
- `tests/test_404_auth_trigger.py` — Rewritten: 404 → data error, 401/403 → api_key_error
- `tests/test_client_workflow_prod.py` — Rewritten: auth_required → api_key_error assertions
- `tests/test_remote_core_services.py` — Removed login test, added 6 API key header tests

New test suite:
- `tests/test_api_key_auth.py` — 12 tests covering transport, error taxonomy, tool removal, config validation, security

**Test Results: 30/30 migration tests passing, 93/109 full suite passing (16 pre-existing failures unrelated to migration)**

---

## 5. Error Handling & UX Strategy

### New Error Status Taxonomy

| HTTP Status | Status String | User-Facing Message | When |
| :--- | :--- | :--- | :--- |
| `200` | `"success"` | Normal operation | API key valid, request succeeded |
| `401` | `"api_key_error"` | "System configuration issue. Please contact your administrator." | API key is missing or invalid |
| `403` | `"api_key_error"` | "System access denied. Please contact your administrator." | API key lacks permissions |
| `404` | `"error"` | "The requested resource was not found." | Legitimate data not found |
| `500` | `"error"` | "An internal error occurred. Please try again." | Server-side failure |

### UX Improvements

1. **Zero login friction**: Users can immediately start creating contacts, clients, matters, and events
2. **No login card interruptions**: Active workflows will never be interrupted by auth prompts
3. **Clear error differentiation**: "Not found" vs "auth error" are now distinct
4. **Reduced latency**: Removal of `has_valid_token()` pre-flight check eliminates an HTTP roundtrip

---

## 6. Testing Plan

### Test Matrix

| Test Category | Description |
| :--- | :--- |
| **Unit: API Key Header** | Verify `CORE_API_KEY` appears in all outbound Core requests |
| **Unit: 401/403 Handling** | Mock Core returning 401/403 and verify `api_key_error` status |
| **Unit: 404 No Longer Triggers Auth** | Mock Core returning 404 and verify it's treated as data error |
| **Integration: Contact Workflow** | Full create_contact cycle without login |
| **Integration: Client Workflow** | Full create_client cycle without login |
| **Integration: Matter Workflow** | Full create_matter with lookups, without login |
| **Integration: Event Workflows** | Standard + All-Day event creation without login |
| **Regression: Streaming Endpoint** | `/ai/chat/stream` works without pre-flight gate |
| **Regression: Google Calendar** | Google OAuth flow is completely unaffected |
| **Security: No Password in Context** | Verify `authenticate_to_core` tool is fully removed |

---

## 7. Rollback Strategy

### Rollback Triggers

- Core API rejects the API key format
- Multi-tenant isolation breaks
- Downstream Node.js backend doesn't support `Authorization: Bearer` for API keys

### Rollback Plan

1. **Git revert:** All changes are in a single logical commit → `git revert <commit>`
2. **Feature flag alternative:** Add `CORE_AUTH_MODE` to `config.py`:
   - `"api_key"` (new default) — uses `CORE_API_KEY`
   - `"login"` (legacy) — uses interactive email/password login
   - Allows instant rollback via `.env` change without a code deploy

---

## 8. Change Log

| Date | Phase | Description | Author |
| :--- | :--- | :--- | :--- |
| 2026-04-14 | 1, 2, 3 | Transport layer, pre-flight gates, tool removal | Antigravity AI |
| 2026-04-14 | 4 | System prompt, error messages, helper function rename | Antigravity AI |
| 2026-04-14 | 5 | Workflow documentation update | Antigravity AI |
| 2026-04-14 | 6 | Test suite updates + new API key auth tests (30/30 passing) | Antigravity AI |
