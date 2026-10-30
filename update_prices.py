"""One-off price sync. Preview by default; use --apply to write."""

import argparse
import csv
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

SPREADSHEET = ''
SHEET_ID = 0
SHEET = "Заказы белые ТЕСТ"
KEY = None
SQL = """
SELECT DISTINCT ON (a.local_vendor_code)
    a.local_vendor_code, f.seller_price
FROM wb_order_feed f
LEFT JOIN article a ON a.nm_id = f.nm_id
WHERE a.local_vendor_code IS NOT NULL
ORDER BY a.local_vendor_code, f.created_at DESC
"""


def normalize(value):
    return " ".join(str(value).split()).casefold()


def column_name(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def at(row, index):
    return row[index] if index < len(row) else ""


def header_index(headers, name):
    found = [i for i, value in enumerate(headers) if normalize(value) == name]
    if len(found) != 1:
        raise ValueError(f"Заголовок {name!r}: найдено {len(found)}, требуется ровно один.")
    return found[0]


def plan_changes(rows, prices):
    if not rows:
        raise ValueError("Строка заголовков 4 пуста.")
    vendor_col = header_index(rows[0], "wild")
    price_col = header_index(rows[0], "цена продажная плановая")
    changes = []
    stats = dict(missing=0, null_price=0, formula=0, unchanged=0)
    for row_number, row in enumerate(rows[1:], start=5):
        vendor = str(at(row, vendor_col)).strip()
        if not vendor:
            continue
        if vendor not in prices:
            stats["missing"] += 1
            continue
        price = prices[vendor]
        if price is None:
            stats["null_price"] += 1
            continue
        number = float(price)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"Недопустимая цена для {vendor}: {price}")
        old = at(row, price_col)
        if isinstance(old, str) and old.startswith("="):
            stats["formula"] += 1
            continue
        try:
            if Decimal(str(old)) == Decimal(str(price)):
                stats["unchanged"] += 1
                continue
        except InvalidOperation:
            pass
        changes.append({
            "row": row_number, "vendor": vendor, "old": old,
            "new": number,
            "range": f"'{SHEET}'!{column_name(price_col + 1)}{row_number}",
        })
    return changes, stats


def api(session, method, url, **kwargs):
    response = session.request(method, url, timeout=90, **kwargs)
    if not response.ok:
        # Google errors do not include the private key or DB password.
        raise RuntimeError(f"Google Sheets HTTP {response.status_code}: {response.text[:1500]}")
    return response.json()


def main():
    global SPREADSHEET, SHEET_ID, SHEET
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Записать после просмотра и подтверждения")
    parser.add_argument("--key", type=Path, default=KEY)
    parser.add_argument("--config", type=Path, help="Existing credentials environment file")
    args = parser.parse_args()
    from dotenv import load_dotenv
    project = Path(__file__).resolve().parent
    load_dotenv(project / "prices.local.env", override=False, interpolate=False)
    env_path = args.config or Path(os.environ.get("CREDENTIALS_ENV_FILE", project / ".env"))
    load_dotenv(env_path, override=False, interpolate=False)
    SPREADSHEET = os.environ.get("PRICES_SPREADSHEET_ID", "")
    SHEET_ID = int(os.environ.get("PRICES_SHEET_GID", "0"))
    SHEET = os.environ.get("PRICES_SHEET_NAME", SHEET)
    key_value = args.key or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_value:
        parser.error("Missing Google credentials path")
    args.key = Path(key_value).expanduser()
    if not args.key.is_absolute():
        args.key = env_path.resolve().parent / args.key
    required = ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE", "PRICES_SPREADSHEET_ID", "PRICES_SHEET_GID")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error("Missing database secrets: " + ", ".join(missing))
    import psycopg
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_file(
        str(args.key), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    session = AuthorizedSession(credentials)
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET}"
    metadata = api(session, "GET", base, params={
        "fields": "sheets.properties(sheetId,title,gridProperties(rowCount))"
    })
    sheet = next((s["properties"] for s in metadata["sheets"]
                  if s["properties"]["sheetId"] == SHEET_ID), None)
    if not sheet or sheet["title"] != SHEET:
        raise ValueError("Название листа или gid не совпадают с заданными. Запись отменена.")
    read_url = base + "/values/" + quote(
        f"'{SHEET}'!4:{sheet['gridProperties']['rowCount']}", safe=""
    )

    def read_rows():
        return api(session, "GET", read_url,
                   params={"valueRenderOption": "FORMULA"}).get("values", [])

    rows = read_rows()
    password = os.environ.get("PGPASSWORD")
    with psycopg.connect(
        host=os.environ["PGHOST"], port=int(os.environ.get("PGPORT", "5432")), dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"], password=password, connect_timeout=15,
        sslmode=os.environ.get("PGSSLMODE", "prefer"),
        options="-c default_transaction_read_only=on -c statement_timeout=120000",
    ) as connection:
        prices = {str(code).strip(): price for code, price in connection.execute(SQL)}
    del password
    changes, stats = plan_changes(rows, prices)
    print(f"Лист: {SHEET}. Артикулов из БД: {len(prices)}.")
    print(f"Изменений: {len(changes)}. Пропуски: {stats}")
    for change in changes[:15]:
        print(f"Строка {change['row']}: {change['vendor']}: {change['old']!r} -> {change['new']}")
    if not changes:
        print("Запись не требуется.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report = Path(__file__).resolve().parent / "runs" / f"prices-{stamp}.csv"
    report.parent.mkdir(exist_ok=True)
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["row", "vendor", "old", "new", "range"])
        writer.writeheader()
        writer.writerows(changes)
    print(f"Полный список изменений и прежних значений: {report}")
    if not args.apply:
        print("Предпросмотр завершён. Для записи запустите с --apply.")
        return
    if input("Для записи показанных изменений введите UPDATE: ").strip() != "UPDATE":
        print("Отменено.")
        return
    if read_rows() != rows:
        raise RuntimeError("Лист изменился после чтения. Запустите скрипт заново.")
    result = api(session, "POST", base + "/values:batchUpdate", json={
        "valueInputOption": "RAW",
        "data": [{"range": change["range"], "values": [[change["new"]]]}
                 for change in changes],
    })
    print(f"Обновлено ячеек: {result.get('totalUpdatedCells', 0)}")
    after = read_rows()
    price_col = header_index(after[0], "цена продажная плановая")
    mismatches = [c["row"] for c in changes
                  if at(after[c["row"] - 4], price_col) != c["new"]]
    if mismatches:
        raise RuntimeError(f"Проверка после записи: несовпадения в строках {mismatches[:20]}")
    print("Проверка после записи пройдена.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nОперация прервана.")
        raise SystemExit(1)
