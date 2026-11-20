import unittest
from app import *

class ContractTests(unittest.TestCase):
    def test_00(self):
        assert subtract(iter([]),iter([])) == []
    def test_01(self):
        assert subtract(iter([(0, 10)]),iter([])) == [(0, 10)]
    def test_02(self):
        assert subtract(iter([(0, 10)]),iter([(3, 7)])) == [(0, 3), (7, 10)]
    def test_03(self):
        assert subtract(iter([(0, 10)]),iter([(0, 10)])) == []
    def test_04(self):
        assert subtract(iter([(0, 10)]),iter([(-5, 20)])) == []
    def test_05(self):
        assert subtract(iter([(0, 10)]),iter([(10, 20)])) == [(0, 10)]
    def test_06(self):
        assert subtract(iter([(0, 10)]),iter([(-5, 0)])) == [(0, 10)]
    def test_07(self):
        assert subtract(iter([(0, 5), (5, 10)]),iter([])) == [(0, 10)]
    def test_08(self):
        assert subtract(iter([(5, 10), (0, 7)]),iter([(2, 6)])) == [(0, 2), (6, 10)]
    def test_09(self):
        assert subtract(iter([(0, 0)]),iter([])) == []
    def test_10(self):
        assert subtract(iter([(0, 10)]),iter([(4, 4)])) == [(0, 10)]
    def test_11(self):
        assert subtract(iter([(0, 10)]),iter([(2, 4), (4, 6)])) == [(0, 2), (6, 10)]
    def test_12(self):
        assert subtract(iter([(-10, -2)]),iter([(-8, -4)])) == [(-10, -8), (-4, -2)]
    def test_13(self):
        assert subtract(iter([(0, 2), (4, 6)]),iter([(1, 5)])) == [(0, 1), (5, 6)]
    def test_14(self):
        assert subtract(iter([(0, 9)]),iter([(1, 2), (3, 4), (5, 6)])) == [(0, 1), (2, 3), (4, 5), (6, 9)]
    def test_15(self):
        assert subtract(iter([(0, 9), (0, 9)]),iter([(2, 3), (2, 3)])) == [(0, 2), (3, 9)]
    def test_16(self):
        assert subtract(iter([(1000000000000000000000000000000, 1000000000000000000000000000005)]),iter([(1000000000000000000000000000001, 1000000000000000000000000000004)])) == [(1000000000000000000000000000000, 1000000000000000000000000000001), (1000000000000000000000000000004, 1000000000000000000000000000005)]
    def test_17(self):
        with self.assertRaisesRegex(ValueError,"^invalid interval$"): subtract([(2, 1)],[])
    def test_18(self):
        with self.assertRaisesRegex(ValueError,"^invalid interval$"): subtract([],[(2, 1)])
    def test_19(self):
        with self.assertRaisesRegex(ValueError,"^invalid interval$"): subtract([(True, 3)],[])
    def test_20(self):
        with self.assertRaisesRegex(ValueError,"^invalid interval$"): subtract([],[(True, 3)])
    def test_21(self):
        with self.assertRaisesRegex(ValueError,"^invalid interval$"): subtract([(0, False)],[])
    def test_22(self):
        with self.assertRaisesRegex(ValueError,"^invalid interval$"): subtract([],[(0, False)])
    def test_23(self):
        with self.assertRaisesRegex(ValueError,"^invalid interval$"): subtract([(0, 2.0)],[])
    def test_24(self):
        with self.assertRaisesRegex(ValueError,"^invalid interval$"): subtract([],[(0, 2.0)])
    def test_25(self):
        a=[(3,7),(0,5)]; b=[(2,4)]
        subtract(a,b)
        assert a==[(3,7),(0,5)] and b==[(2,4)]
