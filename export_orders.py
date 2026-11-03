"""Refresh owned WB order sheets from a single read-only, reconciled snapshot."""
import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parent
MOSCOW = ZoneInfo('Europe/Moscow')
HEADERS = ['date', 'nm_id', 'wild', 'fbs_qnt', 'fbo_qnt', 'funnel_orders', 'updated_at']
LOG = logging.getLogger('orders')


def period(days, now=None):
    today = (now or datetime.now(MOSCOW)).astimezone(MOSCOW).date()
    if days < 1:
        raise ValueError('Days must be positive')
    return today - timedelta(days=days), today


def make_rows(source, reference, stamp):
    if not source:
        raise ValueError('Empty database snapshot')
    rows = [HEADERS]
    seen = set()
    totals = defaultdict(lambda: [0, 0, 0])
    for day, nm, wild, fbs, fbo, funnel, mappings in source:
        if nm is None or not wild or mappings != 1:
            raise ValueError('Missing or ambiguous wild mapping')
        key = (day.isoformat(), str(nm))
        if key in seen:
            raise ValueError('Duplicate date and nm_id')
        seen.add(key)
        fbs, fbo, funnel = int(fbs), int(fbo), int(funnel)
        if fbs < 0 or funnel < 0 or fbo != max(funnel - fbs, 0):
            raise ValueError('Invalid order counts or FBO formula')
        if not any((fbs, fbo, funnel)):
            continue
        rows.append([key[0], key[1], wild, fbs, fbo, funnel, stamp])
        group = totals[(key[0], wild.strip().lower())]
        for i, value in enumerate((fbs, fbo, funnel)):
            group[i] += value
    expected = {}
    for day, wild, fbs, fbo, funnel in reference:
        key = (day.isoformat(), wild.strip().lower())
        if key in expected:
            raise ValueError('Ambiguous original wild group')
        expected[key] = (int(fbs), int(fbo), int(funnel))
    if totals.keys() != expected.keys():
        raise ValueError('Original SQL group set mismatch')
    differences = []
    for key, original in expected.items():
        actual = totals[key]
        if actual[0] != original[0] or actual[2] != original[2]:
            raise ValueError('FBS or funnel differs from original SQL')
        delta = actual[1] - original[1]
        if delta:
            differences.append(delta)
    if len(rows) == 1:
        raise ValueError('Empty export after filtering')
    report = {'rows': len(rows) - 1, 'wild_groups': len(expected),
              'fbs_funnel_exact': True,
              'fbo_different_wild_groups': len(differences),
              'fbo_delta_total': sum(differences),
              'clipped_nm_rows': sum(r[3] > r[5] for r in rows[1:]),
              'snapshot_moscow': stamp}
    return rows, report


def source_snapshot(days):
    import psycopg
    start, end = period(days)
    required = ['PGHOST', 'PGUSER', 'PGPASSWORD', 'PGDATABASE']
    if any(not os.environ.get(k) for k in required):
        raise ValueError('Missing PostgreSQL configuration')
    with psycopg.connect(host=os.environ['PGHOST'], port=os.environ.get('PGPORT', '5432'),
            user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'],
            dbname=os.environ['PGDATABASE'], connect_timeout=15,
            sslmode=os.environ.get('PGSSLMODE', 'prefer'),
            options='-c default_transaction_read_only=on -c statement_timeout=120000') as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        if conn.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
            raise ValueError('Read-only database connection required')
        stamp = conn.execute("SELECT to_char(transaction_timestamp() AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI:SS')").fetchone()[0]
        params = {'start': start, 'end': end}
        source = conn.execute((PROJECT / 'sql/orders_by_nm.sql').read_text(encoding='utf-8'), params).fetchall()
        reference = conn.execute((PROJECT / 'sql/orders_reference.sql').read_text(encoding='utf-8'), params).fetchall()
    rows, report = make_rows(source, reference, stamp)
    report.update({'from': start.isoformat(), 'through': (end - timedelta(days=1)).isoformat()})
    return rows, report


class Sheets:
    def __init__(self, credentials):
        from google.auth.transport.requests import AuthorizedSession
        self.session = AuthorizedSession(credentials)

    def api(self, method, url, **kwargs):
        import requests
        # Все запросы скрипта задают абсолютное состояние (значения, размер сетки,
        # фильтр, очистка диапазона), поэтому повтор после таймаута безопасен.
        for attempt in range(5):
            try:
                response = self.session.request(method, url, timeout=120, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 4:
                    raise
                time.sleep(15 * (attempt + 1))
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(3 * (attempt + 1))
                continue
            if not response.ok:
                raise RuntimeError('Sheets HTTP ' + str(response.status_code))
            return response.json()

    @staticmethod
    def base(target):
        return 'https://sheets.googleapis.com/v4/spreadsheets/' + target['spreadsheet']

    def properties(self, target):
        data = self.api('GET', self.base(target), params={'fields': 'sheets.properties'})
        props = [s['properties'] for s in data['sheets']]
        sheet = next((p for p in props if p['sheetId'] == target['gid']), None)
        if not sheet or sheet['title'] != target['sheet']:
            raise ValueError('Configured sheet gid or title mismatch')
        if sheet['gridProperties']['columnCount'] != 7:
            raise ValueError('Expected an owned seven-column export sheet')
        return sheet, props

    def read(self, target, first, last):
        title = target['sheet'].replace("'", "''")
        address = f"'{title}'!A{first}:G{last}"
        return self.api('GET', self.base(target) + '/values/' + quote(address, safe=''),
                        params={'valueRenderOption': 'UNFORMATTED_VALUE'}).get('values', [])

    def preflight(self, target, size):
        sheet, props = self.properties(target)
        old = self.read(target, 1, sheet['gridProperties']['rowCount'])
        if not old or old[0] != HEADERS:
            raise ValueError('Expected export headers; refusing unknown sheet contents')
        cells = sum(p.get('gridProperties', {}).get('rowCount', 0) *
                    p.get('gridProperties', {}).get('columnCount', 0) for p in props)
        growth = max(size - sheet['gridProperties']['rowCount'], 0) * 7
        if cells + growth > 10000000:
            raise ValueError('Spreadsheet cell limit')
        return {'target': target, 'sheet': sheet, 'before': old}

    def replace(self, prepared, rows):
        target, sheet, old = prepared['target'], prepared['sheet'], prepared['before']
        base, gid = self.base(target), target['gid']
        current, _ = self.properties(target)
        old_size = sheet['gridProperties']['rowCount']
        if current['gridProperties'] != sheet['gridProperties'] or self.read(target, 1, old_size) != old:
            raise ValueError('Sheet changed during preparation')
        if len(rows) > old_size:
            self.api('POST', base + ':batchUpdate', json={'requests': [
                {'updateSheetProperties': {'properties': {'sheetId': gid,
                  'gridProperties': {'rowCount': len(rows)}}, 'fields': 'gridProperties.rowCount'}}]})
        title = target['sheet'].replace("'", "''")
        for offset in range(0, len(rows), 10000):
            chunk = rows[offset:offset + 10000]
            address = f"'{title}'!A{offset + 1}:G{offset + len(chunk)}"
            result = self.api('PUT', base + '/values/' + quote(address, safe=''),
                params={'valueInputOption': 'RAW'}, json={'range': address, 'values': chunk})
            if result.get('updatedRows') != len(chunk):
                raise ValueError('Written row count mismatch')
        if len(rows) < old_size:
            address = f"'{title}'!A{len(rows) + 1}:G{old_size}"
            self.api('POST', base + '/values/' + quote(address, safe='') + ':clear', json={})
        self.api('POST', base + ':batchUpdate', json={'requests': [
            {'clearBasicFilter': {'sheetId': gid}},
            {'updateSheetProperties': {'properties': {'sheetId': gid,
                'gridProperties': {'rowCount': len(rows), 'frozenRowCount': 1}},
                'fields': 'gridProperties.rowCount,gridProperties.frozenRowCount'}},
            {'setBasicFilter': {'filter': {'range': {'sheetId': gid, 'startRowIndex': 0,
                'endRowIndex': len(rows), 'startColumnIndex': 0, 'endColumnIndex': 7}}}}]})
        for offset in range(0, len(rows), 20000):
            expected = rows[offset:offset + 20000]
            if self.read(target, offset + 1, offset + len(expected)) != expected:
                raise ValueError('Full post-write verification mismatch')
        final, _ = self.properties(target)
        if final['gridProperties']['rowCount'] != len(rows):
            raise ValueError('Final sheet grid size mismatch')


def relative_path(value, parent):
    path = Path(value).expanduser()
    return path if path.is_absolute() else parent / path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=PROJECT / 'orders.local.json')
    parser.add_argument('--apply', action='store_true', help='Replace the explicitly configured sheets')
    args = parser.parse_args()
    settings_path = args.settings.resolve()
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
    from dotenv import load_dotenv
    env_path = relative_path(settings.get('credentials_env', '.env'), settings_path.parent)
    if not env_path.is_file():
        raise ValueError('Credentials environment file not found')
    load_dotenv(env_path, override=False, interpolate=False)
    from google.oauth2.service_account import Credentials
    key_value = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if not key_value:
        raise ValueError('Missing Google credentials configuration')
    key = relative_path(key_value, env_path.parent)
    credentials = Credentials.from_service_account_file(str(key),
        scopes=['https://www.googleapis.com/auth/spreadsheets'])
    targets = settings['targets']
    if not targets or len({(t['spreadsheet'], t['gid']) for t in targets}) != len(targets):
        raise ValueError('Empty or duplicate target configuration')
    rows, report = source_snapshot(int(settings.get('days', 30)))
    api = Sheets(credentials)
    prepared = [api.preflight(target, len(rows)) for target in targets]
    report['targets'] = len(targets)
    runs = PROJECT / 'runs'
    runs.mkdir(exist_ok=True)
    (runs / 'orders-pending.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    LOG.info('Source reconciled: %s', json.dumps(report))
    if not args.apply:
        LOG.info('Preview only; no sheets changed')
        return
    for index, item in enumerate(prepared, 1):
        (runs / f'orders-previous-{index}.json').write_text(
            json.dumps(item['before'], ensure_ascii=False), encoding='utf-8')
    completed = []
    for index, item in enumerate(prepared, 1):
        api.replace(item, rows)
        completed.append(index)
        (runs / 'orders-last-progress.json').write_text(json.dumps({
            'snapshot_moscow': report['snapshot_moscow'], 'verified_targets': completed,
            'total_targets': len(targets)}), encoding='utf-8')
        LOG.info('Target %s fully verified', index)
    report.update({'completed_at_moscow': datetime.now(MOSCOW).isoformat(), 'verified': True})
    (runs / 'orders-last-success.json').write_text(json.dumps(report), encoding='utf-8')
    LOG.info('SUCCESS %s', json.dumps(report))
    if sys.stdout is not None:
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    runs = PROJECT / 'runs'
    runs.mkdir(exist_ok=True)
    logging.basicConfig(filename=runs / 'orders.log', encoding='utf-8',
                        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        LOG.info('START')
        main()
    except Exception as exc:
        # Driver messages may contain credentials. Log only the exception class.
        LOG.error('FAILED %s', type(exc).__name__)
        if sys.stdout is not None:
            print('FAILED', type(exc).__name__, flush=True)
        raise SystemExit(1)
