"""Additive migrations. Existing histories and aggregate statistics are preserved."""

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


async def migrate(pool):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB""")
            await cur.execute("SELECT version FROM schema_migrations WHERE version = 1")
            if await cur.fetchone():
                return
            # MySQL DDL commits implicitly; each statement is restart-safe.
            for statement in TABLES:
                await cur.execute(statement)
            await cur.execute("INSERT IGNORE INTO schema_migrations(version) VALUES (1)")
