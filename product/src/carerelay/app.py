"""Portable AfterWard demo entry point for local use and a single Railway replica."""
import argparse
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3

from .discharge_http import WorkspaceServer
from .gemma_ocr import GemmaOCR


def initialize_budget(path, *, total, daily):
    """Create a dedicated deployment ledger without resetting an existing allocation."""
    if not 1 <= daily <= total <= 20000 or daily > 3000:
        raise ValueError('Require 1 <= daily <= total <= 20000 and daily <= 3000.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS campaign_policy (total_maximum INTEGER, public_maximum INTEGER, frozen INTEGER)')
        if not db.execute('SELECT 1 FROM campaign_policy').fetchone():
            db.execute('INSERT INTO campaign_policy VALUES (?,?,0)', (total, total))
        db.execute('CREATE TABLE IF NOT EXISTS budget_policy (daily_maximum INTEGER)')
        if not db.execute('SELECT 1 FROM budget_policy').fetchone():
            db.execute('INSERT INTO budget_policy VALUES (?)', (daily,))
        db.execute('CREATE TABLE IF NOT EXISTS inference_attempts (id TEXT PRIMARY KEY, day TEXT, reserved_at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS attempt_context (id TEXT PRIMARY KEY, purpose TEXT, subject TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS inference_lease (singleton INTEGER PRIMARY KEY, active_id TEXT, last_started TEXT)')
        db.execute('INSERT OR IGNORE INTO inference_lease VALUES (1,NULL,NULL)')
        policy = db.execute('SELECT total_maximum,public_maximum FROM campaign_policy').fetchone()
        previous_daily = db.execute('SELECT daily_maximum FROM budget_policy').fetchone()[0]
        if policy != (total, total) or previous_daily != daily:
            raise ValueError('Configured API limits differ from the persistent ledger. Review the ledger; do not delete it to reset usage.')


def create_server(environ=None):
    env = os.environ if environ is None else environ
    hosted = env.get('AFTERWARD_HOSTED') == '1'
    port = int(env.get('PORT', '8776'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535.')
    public_host = env.get('AFTERWARD_PUBLIC_HOST') or env.get('RAILWAY_PUBLIC_DOMAIN')
    if hosted and not public_host:
        raise ValueError('Set AFTERWARD_PUBLIC_HOST or generate a Railway public domain.')
    data_dir = Path(env.get('AFTERWARD_DATA_DIR', '.afterward-data')).resolve()
    if hosted and not data_dir.parent.is_dir():
        raise ValueError('The persistent data directory must already be mounted for hosted operation.')
    data_dir.mkdir(parents=True, exist_ok=True)
    transcriber = None
    if env.get('AFTERWARD_AMD_ENABLED') == '1':
        if env.get('AFTERWARD_AMD_FREE_QUOTA_CONFIRMED') != '1':
            raise ValueError('Confirm the free AMD entitlement before enabling API calls.')
        expires = env.get('AFTERWARD_API_EXPIRES', '')
        if date.fromisoformat(expires) < datetime.now(timezone.utc).date():
            raise ValueError('The configured API access period has expired.')
        budget = data_dir / 'ocr-budget.sqlite3'
        initialize_budget(budget, total=int(env.get('AFTERWARD_API_TOTAL_LIMIT', '200')),
                          daily=int(env.get('AFTERWARD_API_DAILY_LIMIT', '100')))
        transcriber = GemmaOCR(env.get('AMD_TOKEN_FACTORY_API_KEY') or env.get('AFTERWARD_AMD_API_KEY'),
                              budget, purpose='public_demo', expires=expires).transcribe_page
    # Public deployments accept only packaged invented PDFs, not visitors' health data.
    samples = Path(__file__).parent / 'assets' / 'demo'
    allowed = {hashlib.sha256(p.read_bytes()).hexdigest() for p in samples.glob('*.pdf')} if hosted else None
    server = WorkspaceServer(('0.0.0.0' if hosted else '127.0.0.1', port), data_dir / 'workspaces.sqlite3',
                             page_transcriber=transcriber, public_host=public_host,
                             allowed_pdf_digests=allowed, local_ocr=False)
    return server


def main():
    parser = argparse.ArgumentParser(description='Run the AfterWard demonstration workspace.')
    parser.parse_args()
    try:
        server = create_server()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(f'AfterWard listening on port {server.server_port}; AMD OCR {"enabled" if server.store.page_transcriber else "disabled"}.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
