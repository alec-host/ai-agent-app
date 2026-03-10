"""
Quick test of the /ai/chat/stream endpoint using httpx streaming.
Run from the project root: python tmp/test_stream.py
"""
import asyncio
import httpx
import json

async def test_stream():
    url = "http://localhost:8001/ai/chat/stream"
    headers = {
        "Content-Type": "application/json",
        "X-Tenant-ID": "12345678",
        "User-Role": "Partner",
        "X-User-Timezone": "UTC"
    }
    body = {"prompt": "Hello, what can you help me with?", "history": []}

    print(f"[TEST] Sending to {url} ...")
    chunk_count = 0

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            print(f"[TEST] Status: {response.status_code}")
            print(f"[TEST] Content-Type: {response.headers.get('content-type')}")
            print(f"[TEST] --- RAW SSE CHUNKS ---")
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    chunk_count += 1
                    try:
                        data = json.loads(line[6:])
                        if 'content' in data:
                            print(f"  [CONTENT] {repr(data['content'])}")
                        if 'action' in data:
                            print(f"  [ACTION] {data['action']}")
                        if data.get('done'):
                            print(f"  [DONE] Stream complete. History length: {len(data.get('history', []))}")
                    except Exception as e:
                        print(f"  [PARSE ERROR] {e}: {line}")

    print(f"\n[TEST] Total chunks received: {chunk_count}")
    if chunk_count > 0:
        print("[TEST] ✅ STREAMING WORKS!")
    else:
        print("[TEST] ❌ No chunks received — stream is empty.")

asyncio.run(test_stream())
