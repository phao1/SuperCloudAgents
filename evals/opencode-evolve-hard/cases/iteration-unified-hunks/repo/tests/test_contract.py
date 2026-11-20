import unittest
from app import *

class ContractTests(unittest.TestCase):
    def test_00(self):
        assert apply('','')==''
    def test_01(self):
        assert apply('a\n','')=='a\n'
    def test_02(self):
        assert apply('','@@ -0,0 +1,1 @@\n+x\n')=='x\n'
    def test_03(self):
        assert apply('a\n','@@ -1,1 +0,0 @@\n-a\n')==''
    def test_04(self):
        assert apply('a\nb\nc\n','@@ -2,1 +2,1 @@\n-b\n+B\n')=='a\nB\nc\n'
    def test_05(self):
        assert apply('a\n','@@ -1,0 +2,1 @@\n+b\n')=='a\nb\n'
    def test_06(self):
        assert apply('a\n','@@ -0,0 +1,1 @@\n+b\n')=='b\na\n'
    def test_07(self):
        assert apply('中\n🙂\n','@@ -1,2 +1,2 @@\n 中\n-🙂\n+星\n')=='中\n星\n'
    def test_08(self):
        assert apply('a\nb\nc\n','@@ -1,1 +1,1 @@\n-a\n+A\n@@ -3,1 +3,1 @@\n-c\n+C\n')=='A\nb\nC\n'
    def test_09(self):
        assert apply('a\nb\n','@@ -1,1 +1,2 @@\n a\n+x\n@@ -2,1 +3,1 @@\n-b\n+B\n')=='a\nx\nB\n'
    def test_10(self):
        assert apply('a\n','@@ -0,0 +0,0 @@\n')=='a\n'
    def test_11(self):
        assert apply('a\n','@@ -1,1 +1,1 @@\n a\n')=='a\n'
    def test_12(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'--- a\n')
    def test_13(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1 +1 @@\n-a\n+b\n')
    def test_14(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1,1 +1,1 @@\n-x\n+b\n')
    def test_15(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1,2 +1,1 @@\n-a\n+b\n')
    def test_16(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1,1 +1,2 @@\n-a\n+b\n')
    def test_17(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -2,1 +2,1 @@\n-a\n+b\n')
    def test_18(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1,1 +9,1 @@\n-a\n+b\n')
    def test_19(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1,1 +1,1 @@\n-a\n+b')
    def test_20(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1,1 +1,1 @@\n?a\n')
    def test_21(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1,1 +1,1 @@\n-a\n+b\n\\ No newline at end of file\n')
    def test_22(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -0,1 +1,1 @@\n-a\n+b\n')
    def test_23(self):
        with self.assertRaisesRegex(ValueError,"^invalid diff$"): apply("a\n",'@@ -1,1 +1,1 @@\n-a\n+b\n@@ -0,0 +1,0 @@\n')
