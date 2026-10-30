import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock

from export_orders import HEADERS, Sheets, make_rows, period
from update_prices import plan_changes


class OrderSnapshotTests(unittest.TestCase):
    def test_fbo_only_article_is_preserved_and_clipping_is_reported(self):
        day = date(2026, 10, 29)
        rows, report = make_rows([
            (day, 11, 'wild1', 5, 0, 2, 1),
            (day, 12, 'wild1', 0, 5, 5, 1),
        ], [(day, 'wild1', 5, 2, 7)], 'snapshot')
        self.assertEqual(rows, [HEADERS,
            ['2026-10-29', '11', 'wild1', 5, 0, 2, 'snapshot'],
            ['2026-10-29', '12', 'wild1', 0, 5, 5, 'snapshot']])
        self.assertEqual(report['fbo_delta_total'], 3)
        self.assertEqual(report['fbo_different_wild_groups'], 1)

    def test_missing_fbo_only_article_fails_original_funnel_reconciliation(self):
        day = date(2026, 10, 29)
        with self.assertRaisesRegex(ValueError, 'FBS or funnel'):
            make_rows([(day, 11, 'wild1', 5, 0, 2, 1)],
                      [(day, 'wild1', 5, 2, 7)], 'snapshot')

    def test_wrong_fbs_count_fails_before_any_sheet_write(self):
        day = date(2026, 10, 29)
        with self.assertRaisesRegex(ValueError, 'FBS or funnel'):
            make_rows([(day, 11, 'wild1', 7, 0, 5, 1)],
                      [(day, 'wild1', 5, 0, 5)], 'snapshot')

    def test_duplicate_nm_keys_rejected(self):
        day = date(2026, 10, 29)
        row = (day, 11, 'wild1', 1, 0, 1, 1)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            make_rows([row, row], [(day, 'wild1', 2, 0, 2)], 'snapshot')

    def test_missing_or_ambiguous_mapping_rejected(self):
        day = date(2026, 10, 29)
        for wild, count in [(None, None), ('wild1', 2)]:
            with self.subTest(wild=wild, count=count), self.assertRaisesRegex(ValueError, 'mapping'):
                make_rows([(day, 11, wild, 1, 0, 1, count)], [], 'snapshot')

    def test_rolling_period_uses_moscow_date_and_excludes_today(self):
        start, end = period(30, datetime(2026, 10, 29, 22, 0, tzinfo=timezone.utc))
        self.assertEqual((start, end), (date(2026, 9, 30), date(2026, 10, 30)))
        with self.assertRaises(ValueError):
            period(0)


class SheetWriteTests(unittest.TestCase):
    def setup_client(self):
        client = Sheets.__new__(Sheets)
        target = {'spreadsheet': 'example', 'gid': 17, 'sheet': 'Orders'}
        sheet = {'sheetId': 17, 'title': 'Orders',
                 'gridProperties': {'rowCount': 4, 'columnCount': 7, 'frozenRowCount': 1}}
        before = [HEADERS, ['old'] * 7, ['old'] * 7, ['old'] * 7]
        rows = [HEADERS, ['2026-10-29', '11', 'wild1', 1, 2, 3, 'snapshot']]
        final = dict(sheet, gridProperties=dict(sheet['gridProperties'], rowCount=2))
        client.properties = Mock(side_effect=[(sheet, [sheet]), (final, [final])])
        client.api = Mock(side_effect=[{'updatedRows': 2}, {}, {}])
        return client, target, sheet, before, rows

    def test_shorter_snapshot_clears_only_owned_tail_and_verifies_all_values(self):
        client, target, sheet, before, rows = self.setup_client()
        client.read = Mock(side_effect=[before, rows])
        client.replace({'target': target, 'sheet': sheet, 'before': before}, rows)
        clear = client.api.call_args_list[1]
        self.assertEqual(clear.args[0], 'POST')
        self.assertTrue(clear.args[1].endswith('%27Orders%27%21A3%3AG4:clear'))
        requests = client.api.call_args_list[2].kwargs['json']['requests']
        self.assertEqual(requests[1]['updateSheetProperties']['properties']['sheetId'], 17)
        self.assertEqual(client.read.call_args_list[-1].args[1:], (1, 2))

    def test_sheet_changed_since_preflight_stops_without_write(self):
        client, target, sheet, before, rows = self.setup_client()
        client.read = Mock(return_value=[HEADERS, ['manual edit']])
        with self.assertRaisesRegex(ValueError, 'changed'):
            client.replace({'target': target, 'sheet': sheet, 'before': before}, rows)
        client.api.assert_not_called()

    def test_changed_cell_detected_by_full_post_write_verification(self):
        client, target, sheet, before, rows = self.setup_client()
        client.read = Mock(side_effect=[before, [HEADERS, ['wrong']]])
        with self.assertRaisesRegex(ValueError, 'post-write'):
            client.replace({'target': target, 'sheet': sheet, 'before': before}, rows)


if __name__ == '__main__':
    unittest.main()
