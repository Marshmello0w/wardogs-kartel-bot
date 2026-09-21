"""Schema migrations, including one correction for legacy faction sample counts."""

TABLES = (
    """CREATE TABLE IF NOT EXISTS durable_state (
        namespace VARCHAR(40) NOT NULL, item_key VARCHAR(100) NOT NULL, payload LONGTEXT NOT NULL,
        PRIMARY KEY(namespace, item_key)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS round_events (
        id VARCHAR(100) PRIMARY KEY, server_id VARCHAR(50) NOT NULL,
        round_id VARCHAR(36) NOT NULL, kind VARCHAR(40) NOT NULL, payload LONGTEXT NOT NULL,
        created_at DATETIME NOT NULL, INDEX(server_id, created_at)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS event_deliveries (
        event_id VARCHAR(100) NOT NULL, consumer VARCHAR(40) NOT NULL,
        status VARCHAR(20) NOT NULL, PRIMARY KEY(event_id, consumer)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS player_counter_state (
        server_id VARCHAR(50) NOT NULL, steam_id VARCHAR(50) NOT NULL,
        round_id VARCHAR(36) NOT NULL, kills BIGINT NOT NULL, deaths BIGINT NOT NULL,
        cash BIGINT NOT NULL, quality VARCHAR(32) NOT NULL,
        PRIMARY KEY(server_id, steam_id)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS tracked_rounds (
        round_id VARCHAR(36) PRIMARY KEY, server_id VARCHAR(50) NOT NULL,
        quality VARCHAR(32) NOT NULL, history_id INT NULL,
        INDEX(server_id)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS admin_targets (
        steam_id VARCHAR(50) PRIMARY KEY, version BIGINT NOT NULL,
        desired VARCHAR(10) NOT NULL, reason TEXT NOT NULL, admin_mention VARCHAR(100) NOT NULL,
        expires_at DATETIME NULL, ban_id INT NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS admin_jobs (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, steam_id VARCHAR(50) NOT NULL,
        server_id VARCHAR(50) NOT NULL, version BIGINT NOT NULL, action VARCHAR(10) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
        next_attempt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, last_error VARCHAR(100) NULL,
        UNIQUE KEY job_generation(steam_id, server_id, version), INDEX(status, next_attempt)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS legacy_admin_import (
        fingerprint CHAR(64) PRIMARY KEY, payload LONGTEXT NOT NULL, status VARCHAR(20) NOT NULL
        ) ENGINE=InnoDB""",
)

# ``times_seen`` is a legacy column name. Starting with migration 2, it counts
# detected faction entries rather than the number of polling samples.
FACTION_ENTRY_CORRECTION = (
    """CREATE TABLE IF NOT EXISTS player_faction_state (
        server_id VARCHAR(50) NOT NULL, steam_id VARCHAR(50) NOT NULL,
        faction VARCHAR(50) NOT NULL, last_seen DATETIME NOT NULL,
        PRIMARY KEY(server_id, steam_id), INDEX(server_id, last_seen)
        ) ENGINE=InnoDB""",
    # The old values cannot be converted: a value of 39 only says that 39
    # polling samples saw a faction. Keeping it would present false history.
    "DELETE FROM player_faction_stats",
)

IGNORED_FACTION_CORRECTION = (
    # "White" is the unassigned/non-team entry supplied by RCON, never a team.
    "DELETE FROM player_faction_stats WHERE LOWER(TRIM(faction))='white'",
    "DELETE FROM player_faction_state WHERE LOWER(TRIM(faction))='white'",
)

QUEST_SYSTEM = (
    """CREATE TABLE IF NOT EXISTS quest_points (
        steam_id VARCHAR(50) PRIMARY KEY, points BIGINT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS quest_progress (
        steam_id VARCHAR(50) PRIMARY KEY,
        legacy_playtime_imported TINYINT(1) NOT NULL DEFAULT 0,
        eligible_playtime_seconds BIGINT NOT NULL DEFAULT 0,
        awarded_eligible_hours BIGINT NOT NULL DEFAULT 0,
        awarded_cash_blocks BIGINT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS quest_team_playtime (
        week_start DATE NOT NULL, steam_id VARCHAR(50) NOT NULL,
        team VARCHAR(50) NOT NULL, playtime_seconds BIGINT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY(week_start, steam_id, team), INDEX(steam_id, week_start)
        ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS quest_point_ledger (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, steam_id VARCHAR(50) NOT NULL,
        amount BIGINT NOT NULL, kind VARCHAR(40) NOT NULL, reference_key VARCHAR(120) NOT NULL,
        reason VARCHAR(255) NULL, admin_user_id VARCHAR(50) NULL, admin_mention VARCHAR(100) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY quest_reward_once(steam_id, kind, reference_key), INDEX(steam_id, created_at)
        ) ENGINE=InnoDB""",
)

# The older ``player_playtime`` table is an all-time total.  K/D eligibility
# needs a matching calendar-day record so 7- and 30-day boards use the same
# time window for both playtime and player counters.
DAILY_PLAYTIME = (
    """CREATE TABLE IF NOT EXISTS player_daily_playtime (
        server_id VARCHAR(50) NOT NULL, steam_id VARCHAR(50) NOT NULL,
        date DATE NOT NULL, name VARCHAR(255) NOT NULL,
        playtime_seconds BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY(server_id, steam_id, date), INDEX(steam_id, date)
        ) ENGINE=InnoDB""",
)

MIGRATIONS = (
    (1, TABLES),
    (2, FACTION_ENTRY_CORRECTION),
    (3, IGNORED_FACTION_CORRECTION),
    (4, QUEST_SYSTEM),
    (5, DAILY_PLAYTIME),
)


async def migrate(pool):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB""")
            # MySQL DDL commits implicitly; each statement is restart-safe.
            # Apply each version independently so already-running installations
            # also receive newer schema corrections.
            for version, statements in MIGRATIONS:
                await cur.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (version,))
                if await cur.fetchone():
                    continue
                for statement in statements:
                    await cur.execute(statement)
                await cur.execute("INSERT IGNORE INTO schema_migrations(version) VALUES (%s)", (version,))
