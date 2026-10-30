"""Refresh the eight payment-export columns; credentials stay in an existing config."""
import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

HEADERS = ['account', 'payment_id', 'paid_at', 'amount', 'currency', 'status', 'payment_completed_at', 'updated_at']
SQL = """SELECT account, payment_id, paid_at, amount, currency, status,
                 payload->>'updateDT', updated_at_db
          FROM public.wb_payment_history ORDER BY account, paid_at, payment_id"""


def sheet_date(value):
    if value is None:
        return ''
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Source timestamp has no timezone')
    local = value.astimezone(ZoneInfo('Europe/Moscow')).replace(tzinfo=None)
    return (local - datetime(1899, 12, 30)).total_seconds() / 86400


def api(session, method, url, **kwargs):
    response = session.request(method, url, timeout=120, **kwargs)
    if not response.ok:
        raise RuntimeError('Sheets HTTP ' + str(response.status_code))
    return response.json()


def verify(actual, expected):
    if len(actual) != len(expected):
        raise ValueError('Verification row count mismatch')
    for row_number, (got, wanted) in enumerate(zip(actual, expected), 1):
        got = got + [''] * (8 - len(got))
        if len(got) != 8:
            raise ValueError('Verification column count mismatch')
        for col, (a, b) in enumerate(zip(got, wanted), 1):
            if isinstance(b, float):
                matches = isinstance(a, (int, float)) and abs(a - b) < 1e-7
            else:
                matches = a == b
            if not matches:
                raise ValueError(f'Verification mismatch at row {row_number}, column {col}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--spreadsheet', required=True)
    parser.add_argument('--gid', type=int, required=True)
    parser.add_argument('--sheet', required=True)
    args = parser.parse_args()
    from dotenv import load_dotenv
    import psycopg
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession

    load_dotenv(args.config, override=False, interpolate=False)
    required = ['PGHOST', 'PGUSER', 'PGPASSWORD', 'PGDATABASE', 'GOOGLE_APPLICATION_CREDENTIALS']
    if any(not os.environ.get(k) for k in required):
        raise ValueError('Missing database or Google configuration')
    key = Path(os.environ['GOOGLE_APPLICATION_CREDENTIALS']).expanduser()
    if not key.is_absolute():
        key = args.config.resolve().parent / key
    credentials = Credentials.from_service_account_file(
        str(key), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    session = AuthorizedSession(credentials)
    base = 'https://sheets.googleapis.com/v4/spreadsheets/' + args.spreadsheet
    meta = api(session, 'GET', base, params={'fields': 'sheets.properties'})
    target = next((s['properties'] for s in meta['sheets'] if s['properties']['sheetId'] == args.gid), None)
    if not target or target['title'] != args.sheet:
        raise ValueError('Target sheet ID or title mismatch')
    url = base + '/values/' + quote("'" + args.sheet.replace("'", "''") + "'!A:H", safe='')
    def read():
        return api(session, 'GET', url, params={'valueRenderOption': 'UNFORMATTED_VALUE',
            'dateTimeRenderOption': 'SERIAL_NUMBER'}).get('values', [])
    before = read()
    if not before or before[0] != HEADERS:
        raise ValueError('Expected eight export headers; refusing to replace unknown contents')

    with psycopg.connect(host=os.environ['PGHOST'], port=int(os.environ.get('PGPORT', '5432')),
            user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'], dbname=os.environ['PGDATABASE'],
            connect_timeout=15, sslmode=os.environ.get('PGSSLMODE', 'prefer'),
            options='-c default_transaction_read_only=on -c statement_timeout=30000') as conn:
        source = conn.execute(SQL).fetchall()
    if not source or len({(r[0], r[1]) for r in source}) != len(source):
        raise ValueError('Empty source or duplicate payment keys')
    rows = [HEADERS] + [[r[0], r[1], sheet_date(r[2]), float(r[3]), r[4], r[5],
                        sheet_date(r[6]), sheet_date(r[7])] for r in source]
    for row in rows:
        for value in row:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError('Non-finite source number')
            if not isinstance(value, (str, int, float)):
                raise ValueError('Unexpected source value type')

    runs = Path(__file__).resolve().parent / 'runs'
    runs.mkdir(exist_ok=True)
    (runs / 'payments-previous.json').write_text(json.dumps(before, ensure_ascii=False), encoding='utf-8')
    if read() != before:
        raise ValueError('Sheet changed during export preparation')
    requests = []
    end_row = max(len(rows), len(before))
    if target['gridProperties']['rowCount'] < end_row:
        requests.append({'appendDimension': {'sheetId': args.gid, 'dimension': 'ROWS',
            'length': end_row - target['gridProperties']['rowCount']}})
    requests.append({'updateCells': {
        'range': {'sheetId': args.gid, 'startRowIndex': 0, 'endRowIndex': end_row,
                  'startColumnIndex': 0, 'endColumnIndex': 8},
        'rows': [{'values': [{'userEnteredValue': {'numberValue': v} if isinstance(v, (int, float))
                    else {'stringValue': v}} for v in row]} for row in rows],
        'fields': 'userEnteredValue'}})
    for start, end, fmt in [(0, 2, {'type': 'TEXT'}),
            (2, 3, {'type': 'DATE', 'pattern': 'dd.mm.yyyy'}),
            (3, 4, {'type': 'NUMBER', 'pattern': '0.00'}), (4, 6, {'type': 'TEXT'}),
            (6, 8, {'type': 'DATE', 'pattern': 'dd.mm.yyyy'})]:
        requests.append({'repeatCell': {'range': {'sheetId': args.gid,
            'startRowIndex': 1, 'endRowIndex': len(rows), 'startColumnIndex': start, 'endColumnIndex': end},
            'cell': {'userEnteredFormat': {'numberFormat': fmt}}, 'fields': 'userEnteredFormat.numberFormat'}})
    api(session, 'POST', base + ':batchUpdate', json={'requests': requests})
    verify(read(), rows)
    report = {'completed_at_moscow': datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),
              'payments': len(source), 'accounts': len({r[0] for r in source}), 'verified': True}
    (runs / 'payments-last-success.json').write_text(json.dumps(report), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    log_dir = Path(__file__).resolve().parent / 'runs'
    log_dir.mkdir(exist_ok=True)
    with (log_dir / 'payments.log').open('a', encoding='utf-8', buffering=1) as logfile:
        sys.stdout = logfile
        print(datetime.now().isoformat(), 'START', flush=True)
        try:
            main()
        except Exception as exc:
            # Driver exception text can contain credentials; log the class only.
            print(datetime.now().isoformat(), 'FAILED', type(exc).__name__, flush=True)
            raise SystemExit(1)
