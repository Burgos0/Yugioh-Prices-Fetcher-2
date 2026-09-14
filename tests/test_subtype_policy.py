import unittest

from app.subtype_policy import select_subtype


class SubtypePolicyTests(unittest.TestCase):
    def test_prefers_1st_edition_on_first_sight_regardless_of_file_order(self):
        forward = [{"subTypeName": "Unlimited", "marketPrice": 5.0},
                   {"subTypeName": "1st Edition", "marketPrice": 20.0}]
        reversed_order = list(reversed(forward))

        result_forward = select_subtype(forward, tracked_subtype=None)
        result_reversed = select_subtype(reversed_order, tracked_subtype=None)

        self.assertEqual(result_forward["status"], "established")
        self.assertEqual(result_forward["subtype"], "1st Edition")
        self.assertEqual(result_forward["item"]["marketPrice"], 20.0)
        # Order-independence: reversing the file order must not change the result.
        self.assertEqual(result_reversed, result_forward)

    def test_deterministic_fallback_when_no_1st_edition_present(self):
        forward = [{"subTypeName": "Unlimited", "marketPrice": 5.0},
                   {"subTypeName": "Limited", "marketPrice": 9.0}]
        reversed_order = list(reversed(forward))

        result_forward = select_subtype(forward, tracked_subtype=None)
        result_reversed = select_subtype(reversed_order, tracked_subtype=None)

        # Alphabetically-first subtype, not "whichever came last in the file".
        self.assertEqual(result_forward["subtype"], "Limited")
        self.assertEqual(result_forward, result_reversed)

    def test_tracked_subtype_is_kept_even_if_listed_first_or_last(self):
        items = [{"subTypeName": "1st Edition", "marketPrice": 20.0},
                 {"subTypeName": "Unlimited", "marketPrice": 5.0}]
        result = select_subtype(items, tracked_subtype="Unlimited")
        self.assertEqual(result["status"], "tracked")
        self.assertEqual(result["subtype"], "Unlimited")
        self.assertEqual(result["item"]["marketPrice"], 5.0)

    def test_missing_tracked_subtype_never_silently_switches(self):
        items = [{"subTypeName": "1st Edition", "marketPrice": 20.0}]
        result = select_subtype(items, tracked_subtype="Unlimited")
        self.assertEqual(result["status"], "missing_tracked")
        self.assertIsNone(result["item"])

    def test_single_subtype_is_used_directly(self):
        items = [{"subTypeName": "Unlimited", "marketPrice": 3.5}]
        result = select_subtype(items, tracked_subtype=None)
        self.assertEqual(result["status"], "established")
        self.assertEqual(result["subtype"], "Unlimited")
        self.assertEqual(result["item"]["marketPrice"], 3.5)


if __name__ == '__main__':
    unittest.main()
