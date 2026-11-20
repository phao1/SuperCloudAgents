import unittest
from app import *

class ContractTests(unittest.TestCase):
    def test_00(self):
        assert order(iter([]))==[]
    def test_01(self):
        assert order(iter(['1.0.0']))==['1.0.0']
    def test_02(self):
        assert order(iter(['2.0.0', '1.9.9']))==['1.9.9', '2.0.0']
    def test_03(self):
        assert order(iter(['1.0.0', '1.0.0-a']))==['1.0.0-a', '1.0.0']
    def test_04(self):
        assert order(iter(['1.0.0-10', '1.0.0-2']))==['1.0.0-2', '1.0.0-10']
    def test_05(self):
        assert order(iter(['1.0.0-a', '1.0.0-9']))==['1.0.0-9', '1.0.0-a']
    def test_06(self):
        assert order(iter(['1.0.0-a.b', '1.0.0-a']))==['1.0.0-a', '1.0.0-a.b']
    def test_07(self):
        assert order(iter(['1.0.0+z', '1.0.0+a']))==['1.0.0+z', '1.0.0+a']
    def test_08(self):
        assert order(iter(['1.0.0-a+z', '1.0.0-a+b']))==['1.0.0-a+z', '1.0.0-a+b']
    def test_09(self):
        assert order(iter(['1.0.0-a', '1.0.0-A']))==['1.0.0-A', '1.0.0-a']
    def test_10(self):
        assert order(iter(['1.0.0+01', '1.0.0']))==['1.0.0+01', '1.0.0']
    def test_11(self):
        assert order(iter(['1.0.0--', '1.0.0-0']))==['1.0.0-0', '1.0.0--']
    def test_12(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1'])
    def test_13(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.2'])
    def test_14(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'01.2.3'])
    def test_15(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.02.3'])
    def test_16(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.2.03'])
    def test_17(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'v1.2.3'])
    def test_18(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",' 1.2.3'])
    def test_19(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.2.3 '])
    def test_20(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.2.3-01'])
    def test_21(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.2.3-a..b'])
    def test_22(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.2.3+'])
    def test_23(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.2.3-'])
    def test_24(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'١.2.3'])
    def test_25(self):
        with self.assertRaisesRegex(ValueError,"^invalid version$"): order(["1.0.0",'1.2.3-a_b'])
    def test_26(self):
        v=['2.0.0','1.0.0']
        order(v)
        assert v==['2.0.0','1.0.0']
