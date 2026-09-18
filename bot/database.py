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
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS leaderboard (
                    server_id VARCHAR(255) NOT NULL,
                    steam_id VARCHAR(255) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    lifetime_kills INT DEFAULT 0,
                    lifetime_deaths INT DEFAULT 0,
                    lifetime_cash INT DEFAULT 0,
                    current_match_kills INT DEFAULT 0,
                    current_match_deaths INT DEFAULT 0,
                    current_match_cash INT DEFAULT 0,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (server_id, steam_id)
                )
            """)
    
            await cur.execute('''
                CREATE TABLE IF NOT EXISTS player_daily_stats (
                    server_id VARCHAR(255) NOT NULL,
                    steam_id VARCHAR(255) NOT NULL,
                    date DATE NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    kills INT DEFAULT 0,
                    deaths INT DEFAULT 0,
                    cash INT DEFAULT 0,
                    PRIMARY KEY (server_id, steam_id, date)
                )
            ''')
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

async def update_player_stats(pool, server_id, steam_id, name, kills, deaths, cash):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('''
                SELECT current_match_kills, current_match_deaths, current_match_cash 
                FROM leaderboard 
                WHERE server_id = %s AND steam_id = %s
            ''', (server_id, steam_id))
            row = await cur.fetchone()

            added_kills = kills
            added_deaths = deaths
            added_cash = cash

            if row:
                old_kills, old_deaths, old_cash = row
                added_kills = kills if kills < old_kills else (kills - old_kills)
                added_deaths = deaths if deaths < old_deaths else (deaths - old_deaths)
                added_cash = cash if cash < old_cash else (cash - old_cash)

                await cur.execute('''
                    UPDATE leaderboard 
                    SET name = %s,
                        lifetime_kills = lifetime_kills + %s,
                        lifetime_deaths = lifetime_deaths + %s,
                        lifetime_cash = lifetime_cash + %s,
                        current_match_kills = %s,
                        current_match_deaths = %s,
                        current_match_cash = %s,
                        last_seen = CURRENT_TIMESTAMP
                    WHERE server_id = %s AND steam_id = %s
                ''', (name, added_kills, added_deaths, added_cash, kills, deaths, cash, server_id, steam_id))
            else:
                await cur.execute('''
                    INSERT INTO leaderboard (
                        server_id, steam_id, name, 
                        lifetime_kills, lifetime_deaths, lifetime_cash,
                        current_match_kills, current_match_deaths, current_match_cash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (server_id, steam_id, name, kills, deaths, cash, kills, deaths, cash))
            
            # Log in daily stats
            if added_kills > 0 or added_deaths > 0 or added_cash > 0 or not row:
                await cur.execute('''
                    INSERT INTO player_daily_stats (server_id, steam_id, date, name, kills, deaths, cash)
                    VALUES (%s, %s, CURDATE(), %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        kills = kills + VALUES(kills),
                        deaths = deaths + VALUES(deaths),
                        cash = cash + VALUES(cash)
                ''', (server_id, steam_id, name, added_kills, added_deaths, added_cash))

        await conn.commit()

async def get_top_players(pool, server_id, timeframe="all", limit=10):
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if timeframe == "all":
                await cur.execute('''
                    SELECT name, lifetime_kills as kills, lifetime_deaths as deaths, lifetime_cash as cash
                    FROM leaderboard
                    WHERE server_id = %s
                    ORDER BY lifetime_kills DESC, lifetime_deaths ASC
                    LIMIT %s
                ''', (server_id, limit))
            else:
                days = 7 if timeframe == "7d" else 30
                await cur.execute(f'''
                    SELECT l.name, SUM(d.kills) as kills, SUM(d.deaths) as deaths, SUM(d.cash) as cash
                    FROM player_daily_stats d
                    JOIN leaderboard l ON d.server_id = l.server_id AND d.steam_id = l.steam_id
                    WHERE d.server_id = %s AND d.date >= DATE_SUB(CURDATE(), INTERVAL {days} DAY)
                    GROUP BY d.steam_id, l.name
                    ORDER BY kills DESC, deaths ASC
                    LIMIT %s
                ''', (server_id, limit))
            return await cur.fetchall()

