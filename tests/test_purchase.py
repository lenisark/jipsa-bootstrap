import unittest, importlib.util
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/purchase.py'
def load():
    spec = importlib.util.spec_from_file_location('purchase_uut', SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
KNOWN = ['전부서 공통','인사총무','매입부','영업본부','영업시스템본부','CX파트']

class DeptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_exact(self):
        self.assertEqual(self.m.normalize_dept('매입부', KNOWN, {}), ('매입부', False))
    def test_alias(self):
        self.assertEqual(self.m.normalize_dept('영업1본부', KNOWN, {'영업1본부':'영업본부'}), ('영업본부', False))
    def test_contains(self):
        self.assertEqual(self.m.normalize_dept(' 전부서 ', KNOWN, {}), ('전부서 공통', False))
    def test_new(self):
        self.assertEqual(self.m.normalize_dept('신설팀', KNOWN, {}), ('신설팀', True))

if __name__ == '__main__':
    unittest.main()
