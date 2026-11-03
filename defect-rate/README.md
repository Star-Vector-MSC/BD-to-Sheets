# Процент брака по казанам

Заполняет лист «% брака по казанам»: колонки `wild`, `% брака`, `Продано`, `Брак`, `Принято складом`. Период — последние 180 дней по Москве без текущего дня (`days` в конфиге).

- **Продано:** `daily_fin_reports_full`, предмет «Казаны», операция и тип документа «Продажа», сумма `quantity` по `sale_dt`.
- **Брак:** `goods_returns_dev`, предмет «Казаны», `return_type = 'Возврат брака'`, уникальные `srid` по `created_at`.
- **Принято складом:** `incoming_items` + `incoming_documents` по `doc_date`, только `is_valid`; возвраты (`incoming_returns`) не учитываются.
- **wild:** `nm_id` → `article.local_vendor_code`. Wild без продаж за период пропускаются.
- **% брака** = Брак / Продано × 100.

```powershell
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 -B export_defect_rate.py          # превью
.\.venv\Scripts\python.exe -X utf8 -B export_defect_rate.py --apply  # запись и проверка
```

Конфигурация — по `defect_rate.example.json` (локальный файл не публикуется). Сервисному аккаунту нужен доступ редактора к таблице. Скрипт отказывается писать в лист с чужим содержимым.
