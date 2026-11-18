import unittest
from tests.common import ServiceCase
from meridian.demo import decision, query
from meridian.errors import MeridianError


def link(link_id, members, *, region="North", determined_by="dana", reason=None):
    return {"link_id": link_id, "region": region,
            "members": [{"origin": "local-fixture", "external_id": external_id}
                        for external_id in members],
            "determined_by": determined_by,
            "reason": reason or "Headlines and text match the same event"}


class StoryLinkTests(ServiceCase):
    def test_distinct_source_keys_require_manual_determination(self):
        self.accepted("a", "north-a", "wire-alpha")
        self.accepted("b", "north-b", "wire-beta")
        report = self.report()
        self.assertEqual(report["story_count"], 2)
        self.assertEqual(report["source_key_story_count"], 2)
        self.assertFalse(report["determination"]["active"])

        result = self.service.story_links.determine(link("shared", ["a", "b"]))
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["active_member_count"], 2)
        report = self.report()
        self.assertEqual(report["story_count"], 1)
        self.assertEqual(report["source_key_story_count"], 2)
        self.assertEqual(report["determination"]["story_delta"], -1)
        self.assertEqual(report["before_determination"]["story_count"], 2)
        self.assertEqual(report["after_determination"]["story_count"], 1)
        story = report["stories"][0]
        self.assertEqual(story["link_id"], "shared")
        self.assertEqual(story["original_story_keys"], ["wire-alpha", "wire-beta"])
        self.assertEqual({member["original_story_key"] for member in story["members"]},
                         {"wire-alpha", "wire-beta"})

    def test_similar_story_keys_are_not_merged_without_determination(self):
        self.accepted("a", "north-a", "same-slug")
        self.accepted("b", "north-b", "same-slug")
        report = self.report()
        # 现有分组仍按来源 story_key 去重；但只要没有认定，报告不会声称跨来源合并。
        self.assertEqual(report["linked_story_count"], 0)
        self.assertFalse(all(story["link_id"] for story in report["stories"]))

    def test_determination_requires_active_members_two_sources_and_region(self):
        self.accepted("a", "north-a", "one")
        with self.assertRaises(MeridianError) as caught:
            self.service.story_links.determine(link("solo", ["a"]))
        self.assertEqual(caught.exception.code, "invalid_story_link")

        self.add("b", "north-a", "two")
        self.code("b")
        with self.assertRaises(MeridianError) as caught:
            self.service.story_links.determine(link("same-source", ["a", "b"]))
        self.assertEqual(caught.exception.code, "story_link_same_source")

        self.accepted("c", "south-a", "three")
        with self.assertRaises(MeridianError) as caught:
            self.service.story_links.determine(link("cross-region", ["a", "c"]))
        self.assertEqual(caught.exception.code, "story_link_region_conflict")

    def test_member_cannot_belong_to_two_active_links(self):
        self.service.sources.register({"source_id": "north-c", "region": "North",
                                       "outlet": "Outlet D",
                                       "topics": ["technology", "culture"],
                                       "audiences": ["general", "students"]})
        for external_id, source_id in (("a", "north-a"), ("b", "north-b"),
                                       ("c", "north-c")):
            self.accepted(external_id, source_id, external_id)
        self.service.story_links.determine(link("first", ["a", "b"]))
        with self.assertRaises(MeridianError) as caught:
            self.service.story_links.determine(link("second", ["a", "c"]))
        self.assertEqual(caught.exception.code, "story_link_conflict")

    def test_determination_is_idempotent_and_traceable(self):
        self.accepted("a", "north-a", "alpha")
        self.accepted("b", "north-b", "beta")
        first = self.service.story_links.determine(link("shared", ["a", "b"]))
        head = self.service.store.head()
        second = self.service.story_links.determine(link("shared", ["a", "b"]))
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(head, self.service.store.head())
        described = self.service.story_links.describe("shared")
        self.assertEqual(described["determined_by"], "dana")
        self.assertIn("same event", described["reason"])
        self.assertEqual([event["kind"] for event in self.service.story_links.events("shared")],
                         ["story_identity_determined"])
        with self.assertRaises(MeridianError) as caught:
            self.service.story_links.determine(link("shared", ["a", "b"], reason="different"))
        self.assertEqual(caught.exception.code, "story_link_conflict")

    def test_report_shows_before_after_denominator_and_stance(self):
        self.accepted("a", "north-a", "alpha")
        self.add("b", "north-b", "beta")
        self.service.coding.submit(decision("b", "alice", "negative"))
        self.service.coding.submit(decision("b", "bob", "negative"))
        self.service.story_links.determine(link("shared", ["a", "b"]))
        report = self.report()
        region = report["regions"][0]
        self.assertEqual(region["story_count"], 1)
        self.assertEqual(region["before_determination"]["story_count"], 2)
        self.assertEqual(region["before_determination"]["stance_counts"],
                         {"negative": 1, "positive": 1})
        self.assertEqual(region["after_determination"]["stance_counts"], {"mixed": 1})
        self.assertEqual(region["determination"]["story_delta"], -1)
        self.assertEqual(region["determination"]["stance_count_delta"]["positive"], -1)
        self.assertEqual(report["stories"][0]["stance"], "mixed")

    def test_region_comparison_reports_both_denominators(self):
        self.accepted("a", "north-a", "alpha")
        self.accepted("b", "north-b", "beta")
        self.accepted("c", "south-a", "gamma")
        self.service.story_links.determine(link("north-shared", ["a", "b"]))
        result = self.service.comparisons.regions(query() | {"regions": ["North", "South"]},
                                                  "North", "South")
        self.assertEqual(result["left"]["story_count"], 1)
        self.assertEqual(result["left"]["before_determination"]["story_count"], 2)
        self.assertEqual(result["determination"]["left"]["story_delta"], -1)
        self.assertIn("before_determination_stances", result)


if __name__ == "__main__":
    unittest.main()
