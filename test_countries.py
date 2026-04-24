import asyncio
import logging
from src.remote_services.matterminer_core import MatterMinerCoreClient

logging.basicConfig(level=logging.INFO)

async def test_api():
    client = MatterMinerCoreClient(
        base_url="https://dev.matterminer.com",
        tenant_id="test",
        user_email="test@test.com"
    )
    res = await client.get_countries()
    print("Result get_countries:", res)
    await client.close()

if __name__ == "__main__":
    asyncio.run(test_api())
