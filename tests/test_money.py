import unittest

from eluno_pos.domain.money import (allocate, amount_in_words, div_round_half_up, format_inr, from_decimal_str, percent_of,
                                    to_decimal_str)


class DecimalBoundary(unittest.TestCase):
    def test_round_trip(self):
        for p in (0, 1, 99, 100, 10498, 1907215, -5000):
            self.assertEqual(from_decimal_str(to_decimal_str(p)), p)
        self.assertEqual(to_decimal_str(10498), "104.98")
        self.assertEqual(to_decimal_str(-5), "-0.05")
        self.assertEqual(from_decimal_str("10998.00"), 1099800)
        self.assertEqual(from_decimal_str("7.5"), 750)

    def test_rejects_floats_and_sub_paisa(self):
        with self.assertRaises(TypeError):
            to_decimal_str(104.98)
        with self.assertRaises(TypeError):
            from_decimal_str(104.98)
        for bad in ("1.234", "abc", "", "1,000.00"):
            with self.assertRaises(ValueError):
                from_decimal_str(bad)


class Rounding(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(div_round_half_up(5, 2), 3)
        self.assertEqual(div_round_half_up(4, 2), 2)
        self.assertEqual(div_round_half_up(-5, 2), -3)
        self.assertEqual(div_round_half_up(1099800 * 10000, 11800), 932034)

    def test_percent_of(self):
        self.assertEqual(percent_of(949900, 1500), 142485)
        self.assertEqual(percent_of(99900, 5000), 49950)
        self.assertEqual(percent_of(333, 5000), 167)  # 166.5 -> 167


class Allocation(unittest.TestCase):
    def test_sums_exactly_and_is_deterministic(self):
        for total, weights in ((50000, [1099800, 849900]), (100, [1, 1, 1]), (7, [3, 0, 3]), (0, [5, 5])):
            parts = allocate(total, weights)
            self.assertEqual(sum(parts), total)
            self.assertEqual(parts, allocate(total, weights))
        self.assertEqual(allocate(100, [1, 1, 1]), [34, 33, 33])
        self.assertEqual(allocate(7, [3, 0, 3]), [4, 0, 3])

    def test_refuses_impossible(self):
        with self.assertRaises(ValueError):
            allocate(5, [0, 0])
        with self.assertRaises(ValueError):
            allocate(-1, [1])


class Display(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(format_inr(1099800), "10,998.00")
        self.assertEqual(format_inr(12345678), "1,23,456.78")
        self.assertEqual(format_inr(1234567890), "1,23,45,678.90")
        self.assertEqual(format_inr(5), "0.05")

    def test_amount_in_words(self):
        self.assertEqual(amount_in_words(1907215), "Nineteen thousand seventy-two rupees and fifteen paise")
        self.assertEqual(amount_in_words(100), "One rupee only")
        self.assertEqual(amount_in_words(0), "Zero rupees only")
        self.assertEqual(amount_in_words(12345600), "One lakh twenty-three thousand four hundred fifty-six rupees only")
        self.assertEqual(amount_in_words(1500000000), "One crore fifty lakh rupees only")


if __name__ == "__main__":
    unittest.main()
