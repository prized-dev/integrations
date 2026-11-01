import unittest

from temps import c_to_f, convert, f_to_c


class TempsTest(unittest.TestCase):
    def test_c_to_f(self):
        self.assertEqual(c_to_f(100), 212)
        self.assertEqual(c_to_f(-40), -40)

    def test_f_to_c(self):
        self.assertEqual(f_to_c(32), 0)

    def test_convert(self):
        self.assertEqual(convert("21C"), "21 C = 69.8 F")
        self.assertEqual(convert("70f"), "70 F = 21.1 C")
        with self.assertRaises(ValueError):
            convert("10X")


if __name__ == "__main__":
    unittest.main()
