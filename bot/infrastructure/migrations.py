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

REWARD_SHOP = (
    """CREATE TABLE IF NOT EXISTS reward_requests (
        id CHAR(36) PRIMARY KEY, steam_id VARCHAR(50) NOT NULL, kind VARCHAR(30) NOT NULL,
        server_id VARCHAR(50) NOT NULL, faction VARCHAR(50) NULL, duration_kind VARCHAR(12) NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending', reason VARCHAR(255) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME NULL,
        INDEX(status, created_at), INDEX(steam_id, created_at)
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS vip_memberships (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, steam_id VARCHAR(50) NOT NULL, server_id VARCHAR(50) NOT NULL,
        duration_kind VARCHAR(12) NOT NULL, points_cost INT NOT NULL DEFAULT 0,
        status VARCHAR(32) NOT NULL, source VARCHAR(20) NOT NULL DEFAULT 'shop',
        ordered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, activated_at DATETIME NULL,
        expires_at DATETIME NULL, removed_at DATETIME NULL,
        discord_message_id BIGINT NULL, expiry_message_id BIGINT NULL,
        admin_user_id VARCHAR(50) NULL, admin_mention VARCHAR(100) NULL,
        INDEX(server_id, status), INDEX(steam_id, server_id, status), INDEX(status, expires_at)
    ) ENGINE=InnoDB""",
)

# The hardened faction-execution lifecycle includes
# ``reconciliation_required`` (23 characters).  Existing installations created
# the original status column at 20 characters, so widen it before that state is
# ever written.
REWARD_SHOP_HARDENING = (
    "ALTER TABLE reward_requests MODIFY status VARCHAR(32) NOT NULL DEFAULT 'pending'",
)

SEED_QUEST = (
    """CREATE TABLE IF NOT EXISTS seed_sessions (
        id CHAR(36) PRIMARY KEY, server_id VARCHAR(50) NOT NULL,
        status VARCHAR(24) NOT NULL DEFAULT 'active',
        activated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        activated_by VARCHAR(100) NULL, ended_at DATETIME NULL,
        ended_by VARCHAR(100) NULL, end_reason VARCHAR(32) NULL,
        INDEX(server_id, status), INDEX(status, activated_at)
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS seed_server_state (
        server_id VARCHAR(50) PRIMARY KEY, session_id CHAR(36) NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX(session_id)
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS seed_participants (
        session_id CHAR(36) NOT NULL, steam_id VARCHAR(50) NOT NULL,
        initial_awarded TINYINT(1) NOT NULL DEFAULT 0,
        observed_at DATETIME NULL, continuous_seconds BIGINT NOT NULL DEFAULT 0,
        continuous_intervals INT NOT NULL DEFAULT 0,
        awarded_intervals INT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY(session_id, steam_id), INDEX(steam_id, session_id)
    ) ENGINE=InnoDB""",
)

DAILY_PLAYTIME = (
    """CREATE TABLE IF NOT EXISTS player_daily_playtime (
        server_id VARCHAR(50) NOT NULL, steam_id VARCHAR(50) NOT NULL,
        date DATE NOT NULL, playtime_seconds BIGINT NOT NULL DEFAULT 0,
        legacy_seconds BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY(server_id, steam_id, date), INDEX(date)
    ) ENGINE=InnoDB""",
    # Existing playtime has no dates. Seed today's bucket once, preserving the
    # user's requested starting value for 7/30-day rankings. INSERT IGNORE
    # makes a partially completed migration safe to retry after a restart.
    """INSERT IGNORE INTO player_daily_playtime
        (server_id, steam_id, date, playtime_seconds, legacy_seconds)
        SELECT server_id, steam_id, UTC_DATE(), playtime_seconds, playtime_seconds
        FROM player_playtime""",
)

MIGRATIONS = (
    (1, TABLES),
    (2, FACTION_ENTRY_CORRECTION),
    (3, IGNORED_FACTION_CORRECTION),
    (4, QUEST_SYSTEM),
    (5, REWARD_SHOP),
    # A prior development deployment used migration number 5 before this
    # feature existed. Re-run the idempotent schema at a fresh version so an
    # already marked database cannot miss the two shop tables.
    (6, REWARD_SHOP),
    (7, REWARD_SHOP_HARDENING),
    (8, SEED_QUEST),
    (9, DAILY_PLAYTIME),
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
