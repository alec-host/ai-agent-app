import asyncio
import logging
from src.remote_services.matterminer_core import MatterMinerCoreClient

logging.basicConfig(level=logging.INFO)

async def test_api():
    client = MatterMinerCoreClient(
        base_url="https://dev.matterminer.com",
        tenant_id="12345678",
        user_email="test@test.com"
    )
    payload = {
        "title": "Mr.",
        "first_name": "Jill",
        "last_name": "Bill",
        "email": "jill.bill@yopmail.com",
        "contact_type": "primary",
        "country_code": "254",
        "phone_number": "12345678"
    }
    res = await client.create_contact(payload)
    print("Result:", res)
    await client.close()

if __name__ == "__main__":
    asyncio.run(test_api())
