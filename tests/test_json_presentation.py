import json
import unittest
from decimal import Decimal

from spotguard.util import pretty_json


class JsonPresentationTests(unittest.TestCase):
    def test_nested_decimal_values_render_as_fixed_point_strings(self):
        rendered = pretty_json({"outer": [Decimal("0.00007000"), {"price": Decimal("78482.01000000")}]})
        self.assertIn('"0.00007000"', rendered)
        self.assertIn('"78482.01000000"', rendered)
        self.assertNotIn("7e-05", rendered.lower())
        self.assertEqual(json.loads(rendered)["outer"][0], "0.00007000")

    def test_unsupported_object_still_raises_type_error(self):
        with self.assertRaises(TypeError):
            pretty_json({"value": object()})


if __name__ == "__main__":
    unittest.main()
