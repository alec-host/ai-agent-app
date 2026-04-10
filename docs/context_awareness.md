# Universal Context Awareness: Implementation Walkthrough

To make the MatterMiner AI platform "context aware all the time" across both old and new chat history in a scalable and optimized way, we implement memory at three distinct levels.

## 1. The Three-Tier Memory Architecture

| Tier | Name | Purpose | Implementation Strategy |
| :--- | :--- | :--- | :--- |
| **Tier 1** | **Short-Term (Sliding Window)** | Immediate context of the last ~5-10 turns. | Standard refined history window (already partially in `main.py`). |
| **Tier 2** | **Mid-Term (Semantic Retrieval)** | Accessing specific facts from "Old" history. | **Vector RAG on Chat Logs**: Embed past messages and retrieve them only when relevant. |
| **Tier 3** | **Long-Term (The Knowledge Vault)** | Persistent facts that never expire. | **Fact Extraction**: Automatically extract and update key metadata during every turn. |

---

## 2. Implementation Roadmap

### Phase A: The "Fact Extraction" Engine (Tier 3)
Currently, the "Vault" only tracks drafting state. We expand this into a **Global Context Store**.
- **Mechanism**: After each assistant response, run a background task to identify "Global Truths" (e.g., "User is a Senior Partner," "They prefer Africa/Nairobi time").
- **Storage**: Store these in the `metadata` column of the `chatsessions` table.
- **Injection**: Inject these facts into the system prompt via the existing `get_rehydration_context` hook.

### Phase B: Incremental Summarization (Tier 2)
To avoid losing the "thread" of a long conversation:
- **Mechanism**: When the chat history exceeds 20 turns, trigger a "Summarization Turn."
- **Optimization**: Use a fast model (GPT-4o-mini) to summarize the oldest 15 turns into a concise paragraph.
- **Injection**: This summary is stored as a `history_summary` and passed as the *second* message (immediately after the system prompt).

### Phase C: Semantic History Retrieval (Tier 2/3)
For very old messages:
- **Mechanism**: On every user query, perform a quick semantic search against the database of past messages.
- **Optimization**: Only inject the top 2-3 most relevant past segments IF their relevance score is high.

---

## 3. Optimized System Flow

1. **User Prompt** arrives.
2. **Intent Classifier** checks if specific historical recall is needed.
3. **Search Context Vault** for persistent facts.
4. **Semantic Search** past chats for relevant snippets.
5. **Construct Prompt**: Inject system prompt + vault facts + history summary + retrieved snippets + short-term window.
6. **LLM Execution**.
7. **Fact Extraction Worker**: Update the persistent vault asynchronously.

---

## 4. Scalability & Performance Guardrails
- **Lazy Loading**: Do not search the entire chat history on every turn.
- **Asynchronous Processing**: Fact extraction and summarization happen in background tasks.
- **Token Budgeting**: 15% Global Facts, 15% History Summary, 70% Current Task.
