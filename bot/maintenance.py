"""Offline maintenance: migrations, job status and a runtime state export.

Run only with the bot stopped when preparing a deployment or rollback.
This command never calls RCON or Discord. It uses the configured database.
"""
import argparse
import asyncio
import json
from pathlib import Path
from core.runtime import write_state
from infrastructure import database


async def run(args):
    try:
        pool = await database.get_db_pool()
        if pool is None:
            raise RuntimeError('Database unavailable; verify configuration')
        if args.command == 'migrate':
            print('Schema migrations applied; existing statistics preserved.')
            return
        async with database.transaction() as cur:
            if args.command == 'status':
                await cur.execute('SELECT status,COUNT(*) AS count FROM admin_jobs GROUP BY status')
                print('Admin jobs:', await cur.fetchall())
                await cur.execute("SELECT COUNT(*) AS count FROM legacy_admin_import WHERE status='needs_review'")
                print('Legacy jobs requiring review:', (await cur.fetchone())['count'])
                await cur.execute("SELECT COUNT(*) AS count FROM event_deliveries WHERE status='uncertain'")
                print('Unconfirmed broadcasts:', (await cur.fetchone())['count'])
                return
            await cur.execute("""SELECT j.action,j.server_id,j.steam_id,t.reason,t.admin_mention FROM admin_jobs j
                JOIN admin_targets t ON t.steam_id=j.steam_id AND t.version=j.version
                WHERE j.status='pending' AND j.action=t.desired
                AND (j.action='unban' OR t.expires_at IS NULL OR t.expires_at>UTC_TIMESTAMP())""")
            pending = await cur.fetchall()
            await cur.execute("SELECT item_key,payload FROM durable_state WHERE namespace='voting'")
            votes = {r['item_key']: json.loads(r['payload']) for r in await cur.fetchall()}
            await cur.execute("SELECT item_key,payload FROM durable_state WHERE namespace='round'")
            rounds = {r['item_key']: json.loads(r['payload']) for r in await cur.fetchall()}
        directory = Path(args.output).resolve()
        directory.mkdir(parents=True, exist_ok=False)
        write_state(directory / 'pending_admin_actions.json', list(pending))
        write_state(directory / 'map_vote_state.json', votes)
        write_state(directory / 'round_state.json', rounds)
        print(f'State exported to {directory}. Keep a full SQL backup as well.')
        if any(state.get('change') for state in votes.values()):
            print('Rotation changes remain: inspect their before/after arrays before any rollback.')
    finally:
        await database.close_pool()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('migrate')
    sub.add_parser('status')
    export = sub.add_parser('export-state')
    export.add_argument('--output', required=True, help='New directory; existing directories are never overwritten')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
