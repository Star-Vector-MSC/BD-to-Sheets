"""Refresh the 'defect rate by kazans' sheet: defects / sales per wild over the last N days."""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parent
MOSCOW = ZoneInfo('Europe/Moscow')
HEADERS = ['wild', '% брака', 'Продано', 'Брак', 'Принято складом']


def period(days, now=None):
    today = (now or datetime.now(MOSCOW)).astimezone(MOSCOW).date()
    return today - timedelta(days=days), today


def make_rows(source):
    rows, skipped = [HEADERS], []
    for wild, sold, defect, received, wilds in source:
        if wilds != 1:
            raise ValueError('Ambiguous nm_id to wild mapping')
        if sold <= 0:
            skipped.append((wild, int(defect)))
            continue
        rows.append([wild, round(100 * int(defect) / int(sold), 2), int(sold), int(defect), int(received)])
    if len(rows) == 1:
        raise ValueError('Empty export')
    return rows, skipped


def source_snapshot(days):
    import psycopg
    start, end = period(days)
    with psycopg.connect(host=os.environ['PGHOST'], port=os.environ.get('PGPORT', '5432'),
            user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'],
            dbname=os.environ['PGDATABASE'], connect_timeout=15,
            sslmode=os.environ.get('PGSSLMODE', 'prefer'),
            options='-c default_transaction_read_only=on -c statement_timeout=180000') as conn:
        source = conn.execute((PROJECT / 'sql/defect_rate.sql').read_text(encoding='utf-8'),
                              {'start': start, 'end': end}).fetchall()
    rows, skipped = make_rows(source)
    return rows, {'from': start.isoformat(), 'through': (end - timedelta(days=1)).isoformat(),
                  'wilds': len(rows) - 1, 'without_sales_skipped': len(skipped),
                  'sold': sum(r[2] for r in rows[1:]), 'defects': sum(r[3] for r in rows[1:]),
                  'received': sum(r[4] for r in rows[1:])}


def write_sheet(session, target, rows):
    base = 'https://sheets.googleapis.com/v4/spreadsheets/' + target['spreadsheet']
    meta = session.get(base, params={'fields': 'sheets.properties'}, timeout=60)
    meta.raise_for_status()
    sheet = next((s['properties'] for s in meta.json()['sheets'] if s['properties']['sheetId'] == target['gid']), None)
    if not sheet or sheet['title'] != target['sheet']:
        raise ValueError('Configured sheet gid or title mismatch')
    title = quote("'" + sheet['title'] + "'")
    current = session.get(f'{base}/values/{title}!A:E', timeout=60)
    current.raise_for_status()
    existing = current.json().get('values', [])
    if existing and existing[0][:2] != HEADERS[:2] and any(any(c.strip() for c in r) for r in existing):
        raise ValueError('Sheet has foreign content; refusing to overwrite')
    session.post(f'{base}/values/{title}!A:E:clear', json={}, timeout=60).raise_for_status()
    session.put(f'{base}/values/{title}!A1', params={'valueInputOption': 'RAW'},
                json={'values': rows}, timeout=120).raise_for_status()
    back = session.get(f'{base}/values/{title}!A1:E{len(rows)}',
                       params={'valueRenderOption': 'UNFORMATTED_VALUE'}, timeout=60)
    back.raise_for_status()
    if back.json().get('values', []) != rows:
        raise ValueError('Verification failed after write')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=PROJECT / 'defect_rate.local.json')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text(encoding='utf-8-sig'))
    from dotenv import load_dotenv
    env_path = (args.settings.resolve().parent / settings.get('credentials_env', '.env')).resolve()
    load_dotenv(env_path, override=False, interpolate=False)
    rows, report = source_snapshot(int(settings.get('days', 180)))
    (PROJECT / 'runs').mkdir(exist_ok=True)
    (PROJECT / 'runs' / 'pending.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    if not args.apply:
        print('Preview only; sheet not changed')
        return
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials
    key = (env_path.parent / os.environ['GOOGLE_APPLICATION_CREDENTIALS']).resolve()
    credentials = Credentials.from_service_account_file(
        str(key), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    write_sheet(AuthorizedSession(credentials), settings['target'], rows)
    print('Written and verified', len(rows) - 1, 'rows')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        response = getattr(exc, 'response', None)
        print('FAILED', type(exc).__name__, getattr(response, 'status_code', ''),
              str(exc) if isinstance(exc, ValueError) else '')
        sys.exit(1)
