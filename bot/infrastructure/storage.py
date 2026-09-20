import json
from datetime import datetime, timezone
from infrastructure import database


def utcnow():
    # Naive UTC at the SQL boundary; every connection uses UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_state(namespace, key, default=None):
    async with database.transaction() as cur:
        await cur.execute("SELECT payload FROM durable_state WHERE namespace=%s AND item_key=%s", (namespace, key))
        row = await cur.fetchone()
        return json.loads(row['payload']) if row else default


async def put_state(cur, namespace, key, value):
    await cur.execute("""INSERT INTO durable_state(namespace,item_key,payload) VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE payload=VALUES(payload)""", (namespace, key, json.dumps(value)))


async def save_state(namespace, key, value):
    async with database.transaction() as cur:
        await put_state(cur, namespace, key, value)


async def pending_events(consumer, kinds):
    async with database.transaction() as cur:
        await cur.execute(f"""SELECT e.* FROM round_events e LEFT JOIN event_deliveries d
            ON d.event_id=e.id AND d.consumer=%s WHERE d.event_id IS NULL
            AND e.kind IN ({','.join(['%s'] * len(kinds))}) ORDER BY e.created_at,e.id LIMIT 100""",
            (consumer, *kinds))
        return [dict(r, data=json.loads(r['payload'])) for r in await cur.fetchall()]


async def acknowledge(event_id, consumer, status="done"):
    async with database.transaction() as cur:
        await cur.execute("""INSERT INTO event_deliveries(event_id,consumer,status) VALUES (%s,%s,%s)
            ON DUPLICATE KEY UPDATE status=VALUES(status)""", (event_id, consumer, status))
