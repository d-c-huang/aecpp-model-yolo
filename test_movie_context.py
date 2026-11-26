import unittest

import numpy as np

import movie_context as context


class MovieContextTests(unittest.TestCase):
    def profile(self):
        return {"status": "confirmed", "cast": [{"role": "至尊宝", "actor": "周星驰", "aliases": ["阿宝"]}],
                "synopsis": "人物穿越时空寻找爱人。", "events": [{"start_ms": 2000, "end_ms": 4000,
                                                              "text": "英雄与旧友重逢", "roles": ["至尊宝"]}]}

    def test_confirmed_context_keeps_future_events_out_of_earlier_rows(self):
        profile = self.profile()
        encoded = context.encode_profile(profile, [0, 1000, 2000, 3000, 4000])
        self.assertEqual(encoded.shape, (5, 99))
        self.assertTrue(np.isfinite(encoded).all())
        self.assertTrue(np.any(encoded[:, :32]))
        self.assertTrue(np.any(encoded[:, 32:64]))
        self.assertFalse(encoded[:2, 64:96].any())
        self.assertTrue(encoded[2:4, 64:96].any())
        self.assertFalse(encoded[4, 64:96].any())
        self.assertEqual(encoded[:, -1].tolist(), [0, 0, 1, 1, 0])
        np.testing.assert_array_equal(encoded, context.encode_profile(profile, [0, 1000, 2000, 3000, 4000]))
        profile["status"] = "draft"
        self.assertFalse(context.encode_profile(profile, [0, 2000]).any())

    def test_aliases_do_not_confuse_actors_or_ambiguous_character_names(self):
        profile = self.profile()
        aliases = context.role_aliases(profile)
        self.assertEqual(context.canonical_role("阿宝", aliases), "至尊宝")
        self.assertEqual(context.canonical_role("周星驰", aliases), "周星驰")
        profile["cast"].append({"role": "另一个人", "actor": "周星驰", "aliases": ["阿宝"]})
        self.assertEqual(context.canonical_role("阿宝", context.role_aliases(profile)), "阿宝")


if __name__ == "__main__":
    unittest.main()
