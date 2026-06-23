import unittest, importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/supply.py'


def load():
    spec = importlib.util.spec_from_file_location('supply_uut', SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


COLS = {'item': 'Col_I', 'qty': 'Col_Q', 'status': 'Col_S'}
DONE = 'OptDONE'


def rec(rid, item, qty, status_opt):
    return {'id': rid, 'fields': [
        {'column_id': 'Col_I', 'text': item, 'value': item},
        {'column_id': 'Col_Q', 'number': [qty]},
        {'column_id': 'Col_S', 'select': [status_opt]},
    ]}


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_parse_record_done(self):
        e = self.m.parse_record(rec('R1', 'A4용지', 3, 'OptDONE'), COLS, DONE)
        self.assertEqual(e, {'record_id': 'R1', 'raw_item': 'A4용지', 'qty': 3, 'done': True})

    def test_parse_record_not_done(self):
        e = self.m.parse_record(rec('R2', '볼펜', 5, 'OptETC'), COLS, DONE)
        self.assertFalse(e['done'])

    def test_parse_record_missing_qty_zero(self):
        r = {'id': 'R3', 'fields': [{'column_id': 'Col_I', 'text': 'X', 'value': 'X'},
                                    {'column_id': 'Col_S', 'select': ['OptDONE']}]}
        e = self.m.parse_record(r, COLS, DONE)
        self.assertEqual(e['qty'], 0)


if __name__ == '__main__':
    unittest.main()
