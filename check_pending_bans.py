import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv("bot/.env")
sys.path.append("bot")
from infrastructure import database

async def main():
    pool = await database.get_db_pool()
    if not pool:
        print("Could not connect to DB")
        return
        
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT * FROM banned_players WHERE announced = 0')
            print("Unannounced (pending) bans in DB:", await cur.fetchall())
            
            await cur.execute('SELECT * FROM banned_players')
            print("Total bans in DB:", len(await cur.fetchall()))
    pool.close()
    await pool.wait_closed()

asyncio.run(main())
