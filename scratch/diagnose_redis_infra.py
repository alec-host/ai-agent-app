import asyncio
import json
import logging
import traceback
from src.config import settings
import redis.asyncio as redis

logging.basicConfig(level=logging.DEBUG)

async def verify_redis():
    print(f"--- ATTEMPTING REDIS CONNECTION ---")
    print(f"Host: {settings.REDIS_HOST}")
    print(f"Port: {settings.REDIS_PORT}")
    print(f"Pass: {'[MASKED]' if settings.REDIS_PASS else 'None'}")
    
    r = redis.Redis(
        host=settings.REDIS_HOST,
        port=int(settings.REDIS_PORT),
        password=settings.REDIS_PASS or None,
        db=0,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3
    )
    
    try:
        # Test 1: PING
        print("\n1. Testing Ping...")
        ping_res = await r.ping()
        print(f"Ping successful: {ping_res}")
        
        # Test 2: APPEND
        print("\n2. Testing Write Access...")
        test_key = "matterminer:chat_history:test_tenant:default"
        await r.rpush(test_key, json.dumps({"role": "user", "content": "test_message"}))
        print("Write successful!")
        
        # Test 3: RETRIEVE
        print("\n3. Testing Read Access...")
        data = await r.lrange(test_key, -10, -1)
        print(f"Read successful. Items found: {len(data)}")
        for d in data:
            print(" -", d)
            
        # Test 4: CLEANUP
        print("\n4. Testing Cleanup...")
        await r.delete(test_key)
        print("Cleanup successful.")
        
    except redis.AuthenticationError:
        print("\n[!] FATAL: Redis Authentication Error. Your REDIS_PASS does not match the server.")
    except redis.ConnectionError as e:
        print(f"\n[!] FATAL: Redis Connection Error. Cannot reach {settings.REDIS_HOST}:{settings.REDIS_PORT}.")
        print("Trace:", e)
    except Exception as e:
        print(f"\n[!] Unexpected Error: {str(e)}")
        traceback.print_exc()
    finally:
        await r.close()
        print("\n--- TEST COMPLETE ---")

if __name__ == "__main__":
    asyncio.run(verify_redis())
