import logging
from urllib.parse import urlparse, unquote
import aiomysql
import config

async def get_db_pool():
    if not config.DB_CONNECTION_URL:
        logging.error("DB_CONNECTION_URL is not set!")
        return None

    # mysql://user:password@host:port/dbname
    parsed = urlparse(config.DB_CONNECTION_URL)
    dbname = parsed.path.lstrip('/')
    
    # 1. Versuche, die Datenbank zu erstellen (falls der User die Rechte dafür hat)
    try:
        temp_pool = await aiomysql.create_pool(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=unquote(parsed.password) if parsed.password else None,
            autocommit=True
        )
        async with temp_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"CREATE DATABASE IF NOT EXISTS `{dbname}`")
        temp_pool.close()
        await temp_pool.wait_closed()
    except Exception as e:
        logging.warning(f"Could not auto-create database (might lack permissions, or it already exists): {e}")

    # 2. Verbinde mit der (nun sicher existierenden) Datenbank
    try:
        pool = await aiomysql.create_pool(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=unquote(parsed.password) if parsed.password else None,
            db=dbname,
            autocommit=True
        )
        return pool
    except Exception as e:
        logging.error(f"Failed to connect to database: {e}")
        return None

async def init_db(pool):
    if not pool:
        return
    
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS server_uptime (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    server_id VARCHAR(255) NOT NULL,
                    status TINYINT(1) NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX(server_id),
                    INDEX(timestamp)
                )
            """)
    logging.info("Database initialized.")

async def log_uptime(pool, server_id, is_online):
    if not pool:
        return
        
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO server_uptime (server_id, status) VALUES (%s, %s)",
                (server_id, 1 if is_online else 0)
            )

async def get_uptime_stats(pool, server_id):
    """Returns uptime percentage for the last 24h, 7d and 30d"""
    if not pool:
        return {"24h": "N/A", "7d": "N/A", "30d": "N/A"}
        
    stats = {}
    intervals = {"24h": 1, "7d": 7, "30d": 30}
    
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for label, days in intervals.items():
                await cur.execute(f"""
                    SELECT 
                        COUNT(*) as total,
                        SUM(status) as online_count
                    FROM server_uptime 
                    WHERE server_id = %s 
                    AND timestamp >= DATE_SUB(NOW(), INTERVAL {days} DAY)
                """, (server_id,))
                
                result = await cur.fetchone()
                total = result[0]
                online_count = result[1] or 0
                
                if total > 0:
                    percentage = (online_count / total) * 100
                    stats[label] = f"{percentage:.1f}%"
                else:
                    stats[label] = "N/A"
                    
    return stats
