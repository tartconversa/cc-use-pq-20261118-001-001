import unittest
from tests.common import ServiceCase
from meridian.demo import decision, observation, query
from meridian.errors import MeridianError


def link(link_id="shared", members=("a", "b"), *, reason="Same event across outlets"):
    return {"link_id": link_id, "region": "North",
            "members": [{"origin": "local-fixture", "external_id": external_id}
                        for external_id in members],
            "determined_by": "dana", "reason": reason}


class StoryLinkLifecycleTests(ServiceCase):
    def _pair(self):
        self.accepted("a", "north-a", "alpha")
        self.add("b", "north-b", "beta")
        self.service.coding.submit(decision("b", "alice", "positive"))
        self.service.coding.submit(decision("b", "bob", "positive"))
        self.service.story_links.determine(link())

    def test_correction_of_one_member_recomputes_members(self):
        self._pair()
        before = self.report()
        self.assertEqual(before["story_count"], 1)
        self.assertEqual(before["stories"][0]["observation_count"], 2)

        changed = observation("b", "north-b", "revised-slug")
        self.service.observations.ingest(changed, expected_revision=1)
        report = self.report()
        self.assertEqual(report["excluded"]["uncoded"], 1)
        self.assertEqual(report["story_count"], 1)
        story = report["stories"][0]
        self.assertEqual(story["observation_count"], 1)
        self.assertEqual(story["representative"], {"origin": "local-fixture", "external_id": "a"})
        self.assertEqual([member["external_id"] for member in story["members"]], ["a"])
        history = self.service.story_links.describe("shared")["members"]
        member_b = [member for member in history if member["external_id"] == "b"][0]
        self.assertEqual(member_b["determined_revision"], 1)
        self.assertEqual(member_b["current_revision"], 2)
        self.assertEqual(member_b["current_story_key"], "revised-slug")

    def test_corrected_member_recoded_appears_again_without_new_link(self):
        self._pair()
        changed = observation("b", "north-b", "revised-slug")
        self.service.observations.ingest(changed, expected_revision=1)
        alternate = decision("b", "alice", "negative")
        alternate["revision"] = 2
        alternate["decision_id"] = "b-alice-r2"
        self.service.coding.submit(alternate)
        second = decision("b", "bob", "negative")
        second["revision"] = 2
        second["decision_id"] = "b-bob-r2"
        self.service.coding.submit(second)
        report = self.report()
        story = report["stories"][0]
        self.assertEqual(story["observation_count"], 2)
        self.assertEqual(story["stance"], "mixed")
        self.assertEqual({member["revision"] for member in story["members"]}, {1, 2})

    def test_retraction_removes_member_but_keeps_original_key(self):
        self._pair()
        self.service.observations.retract("local-fixture", "b", "withdrawn by outlet",
                                          expected_revision=1)
        report = self.report()
        self.assertEqual(report["story_count"], 1)
        self.assertEqual(report["stories"][0]["observation_count"], 1)
        self.assertEqual(report["excluded"]["retracted"], 1)
        link = self.service.story_links.describe("shared")
        self.assertEqual(link["active_member_count"], 2)  # 认定仍是两人的成员集合
        member = [item for item in link["members"] if item["external_id"] == "b"][0]
        self.assertEqual(member["observation_status"], "retracted")
        self.assertEqual(member["original_story_key"], "beta")

    def test_revocation_restores_source_keys_and_keeps_audit_trail(self):
        self._pair()
        preview_before = self.service.releases.preview(query())
        release = self.service.releases.publish("release", query(), preview_before["token"])
        self.assertEqual(release["report"]["story_count"], 1)

        revoked = self.service.story_links.revoke(
            "shared", {"revoked_by": "dana", "reason": "Different events after correction"})
        self.assertEqual(revoked["state"], "revoked")
        report = self.report()
        self.assertEqual(report["story_count"], 2)
        self.assertEqual(report["source_key_story_count"], 2)
        self.assertEqual(report["determination"]["story_delta"], 0)
        with self.assertRaises(MeridianError) as caught:
            self.service.story_links.determine(link())
        self.assertEqual(caught.exception.code, "story_link_revoked")

        changes = self.service.provenance.release_changes("release")
        self.assertEqual(changes["frozen"]["story_count"], 1)
        self.assertEqual(changes["current"]["story_count"], 2)
        self.assertEqual(changes["link_changes"][0]["change"], "removed")
        self.assertEqual(changes["link_changes"][0]["revoked_by"], "dana")
        self.assertFalse(changes["same_result"])
        self.assertEqual(self.service.releases.describe("release")["report"]["story_count"], 1)

        history = self.service.provenance.observation_history("local-fixture", "a")
        self.assertEqual(history["story_links"][0]["state"], "revoked")
        self.assertEqual(history["story_links"][0]["revoke_reason"],
                         "Different events after correction")

    def test_revocation_is_idempotent_only_for_same_request(self):
        self._pair()
        first = self.service.story_links.revoke(
            "shared", {"revoked_by": "dana", "reason": "same request"})
        head = self.service.store.head()
        second = self.service.story_links.revoke(
            "shared", {"revoked_by": "dana", "reason": "same request"})
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(head, self.service.store.head())
        with self.assertRaises(MeridianError) as caught:
            self.service.story_links.revoke(
                "shared", {"revoked_by": "erin", "reason": "another reason"})
        self.assertEqual(caught.exception.code, "story_link_conflict")

    def test_cross_source_stance_conflict_blocks_release(self):
        self.accepted("a", "north-a", "alpha")
        self.add("b", "north-b", "beta")
        self.service.coding.submit(decision("b", "alice", "negative"))
        self.service.coding.submit(decision("b", "bob", "negative"))
        self.service.story_links.determine(link())
        preview = self.service.releases.preview(query())
        self.assertIn("cross_source_stance_conflict", preview["quality"]["reasons"])
        self.assertEqual(preview["report"]["determination"]["stance_count_delta"],
                         {"mixed": 1, "negative": -1, "positive": -1})
        with self.assertRaises(MeridianError) as caught:
            self.service.releases.publish("release", query(), preview["token"])
        self.assertEqual(caught.exception.code, "quality_failed")

    def test_unresolved_topic_conflict_blocks_release(self):
        self.accepted("a", "north-a", "alpha", )
        self.add("b", "north-b", "beta")
        wrong = decision("b", "alice", "positive")
        wrong["topic"] = "culture"
        wrong["decision_id"] = "b-alice-culture"
        self.service.coding.submit(wrong)
        second = decision("b", "bob", "positive")
        second["topic"] = "culture"
        second["decision_id"] = "b-bob-culture"
        self.service.coding.submit(second)
        self.service.story_links.determine(link())
        report = self.report()
        self.assertEqual(len(report["story_identity_issues"]), 1)
        self.assertEqual(report["excluded"]["story_identity_conflict"], 2)
        preview = self.service.releases.preview(query())
        self.assertIn("story_identity_incomplete", preview["quality"]["reasons"])
        self.assertEqual(preview["story_identity_issues"][0]["kind"], "mixed_topic_audience")

    def test_identity_conflict_appears_in_diagnostics(self):
        self.accepted("a", "north-a", "alpha")
        self.add("b", "north-b", "beta")
        wrong = decision("b", "alice", "positive")
        wrong["topic"] = "culture"
        wrong["decision_id"] = "b-alice-culture"
        self.service.coding.submit(wrong)
        second = decision("b", "bob", "positive")
        second["topic"] = "culture"
        second["decision_id"] = "b-bob-culture"
        self.service.coding.submit(second)
        self.service.story_links.determine(link())
        identity = self.service.diagnostics.identity_conflicts(query())
        self.assertEqual(identity["count"], 1)
        readiness = self.service.diagnostics.readiness(query())
        self.assertFalse(readiness["ready"])
        self.assertIn("story_identity_conflict", readiness["warnings"])
        queue = self.service.diagnostics.queue(query())
        self.assertEqual(queue["by_status"]["story_identity_conflict"], 2)

    def test_link_id_does_not_collide_with_unlinked_source_key(self):
        self.accepted("a", "north-a", "alpha")
        self.accepted("b", "north-b", "beta")
        self.accepted("c", "north-a", "shared")
        # 认定 id 与另一篇未认定的原始稿件键同名，报告仍须分成两个故事。
        self.service.story_links.determine(link("shared", ["a", "b"]))
        report = self.report()
        self.assertEqual(report["story_count"], 2)
        identities = {(story["story_key"], story["link_id"]) for story in report["stories"]}
        self.assertEqual(identities, {("shared", "shared"), ("shared", None)})
        with self.assertRaises(MeridianError) as caught:
            self.service.provenance.story_detail(query(), "North", "shared")
        self.assertEqual(caught.exception.code, "story_ambiguous")
        detail = self.service.provenance.story_detail(query(), "North", "shared", link_id="shared")
        self.assertEqual(detail["link"]["link_id"], "shared")
        self.assertEqual({item["external_id"] for item in detail["observations"]}, {"a", "b"})
        plain = [story for story in report["stories"] if story["link_id"] is None][0]
        self.assertEqual(plain["members"][0]["external_id"], "c")

    def test_story_trace_links_to_reasons_and_current_members(self):
        self._pair()
        trace = self.service.provenance.story_detail(query(), "North", "shared")
        self.assertEqual(trace["story"]["observation_count"], 2)
        self.assertEqual(trace["link"]["determined_by"], "dana")
        self.assertEqual(trace["link"]["reason"], "Same event across outlets")
        self.assertEqual(len(trace["link"]["events"]), 1)
        self.assertEqual({item["external_id"] for item in trace["observations"]}, {"a", "b"})
        self.assertEqual({item["original_story_key"] for item in trace["observations"]},
                         {"alpha", "beta"})

    def test_region_row_drills_to_story_members(self):
        self._pair()
        region = self.report()["regions"][0]
        self.assertEqual(region["story_count"], 1)
        self.assertEqual(region["before_determination"]["story_count"], 2)
        identity = region["story_identities"][0]
        self.assertEqual(identity["story_key"], "shared")
        self.assertEqual(identity["determined_by"], "dana")
        self.assertEqual(identity["determination_reason"], "Same event across outlets")
        self.assertEqual(identity["original_story_keys"], ["alpha", "beta"])
        self.assertEqual(identity["outlets"], ["Outlet A", "Outlet B"])

    def test_dispute_inside_link_blocks_release_until_resolved(self):
        self._pair()
        alternate = decision("b", "alice", "negative")
        alternate["decision_id"] = "b-alice-negative"
        with self.assertRaises(MeridianError) as caught:
            self.service.coding.submit(alternate)
        self.assertEqual(caught.exception.code, "reviewer_conflict")
        self.service.coding.submit(decision("b", "carol", "negative"))
        self.assertEqual(self.report()["excluded"], {"disputed": 1})
        self.assertFalse(self.service.diagnostics.readiness(query())["ready"])

    def test_frozen_release_keeps_original_denominator_and_basis(self):
        self._pair()
        preview = self.service.releases.preview(query())
        release = self.service.releases.publish("release", query(), preview["token"])
        frozen = self.service.exports.frozen("release")
        self.assertEqual(frozen["report"]["story_count"], 1)
        self.assertEqual(frozen["report"]["source_key_story_count"], 2)
        self.assertEqual(frozen["report"]["determination"]["story_delta"], -1)
        self.assertTrue(self.service.exports.verify(frozen)["valid"])
        self.assertEqual({row["external_id"] for row in frozen["evidence"]}, {"a", "b"})
        self.assertEqual({row["link_id"] for row in frozen["evidence"]}, {"shared"})
        self.assertEqual({row["story_identity"] for row in frozen["evidence"]}, {"shared"})

    def test_stance_conflict_after_correction_blocks_release(self):
        self._pair()
        changed = observation("b", "north-b", "revised-slug")
        self.service.observations.ingest(changed, expected_revision=1)
        alternate = decision("b", "alice", "negative")
        alternate["revision"] = 2
        alternate["decision_id"] = "b-alice-r2"
        self.service.coding.submit(alternate)
        second = decision("b", "bob", "negative")
        second["revision"] = 2
        second["decision_id"] = "b-bob-r2"
        self.service.coding.submit(second)
        preview = self.service.releases.preview(query())
        self.assertEqual(preview["report"]["stories"][0]["stance"], "mixed")
        self.assertIn("cross_source_stance_conflict", preview["quality"]["reasons"])
        conflicts = self.service.diagnostics.story_conflicts(query())
        self.assertEqual(conflicts["count"], 1)
        self.assertEqual(conflicts["conflicts"][0]["story_key"], "shared")
        self.assertEqual(conflicts["conflicts"][0]["stances"], ["negative", "positive"])

    def test_source_breakdown_shows_before_after_contribution(self):
        self._pair()
        result = self.service.exports.source_breakdown(query())
        north_a = [row for row in result["sources"] if row["source_id"] == "north-a"][0]
        self.assertEqual(north_a["story_contribution"]["before_determination"],
                         {"story_count": 1, "stance_counts": {"positive": 1}})
        self.assertEqual(north_a["story_contribution"]["after_determination"],
                         {"story_count": 1, "stance_counts": {"positive": 1}})
        self.assertEqual(result["source_key_story_count"], 2)
        self.assertEqual(result["story_count"], 1)

    def test_current_bundle_matches_link_members(self):
        self._pair()
        bundle = self.service.exports.current(query())
        self.assertTrue(self.service.exports.verify(bundle)["valid"])
        eligible = [row for row in bundle["evidence"] if row["category"] == "eligible"]
        self.assertEqual({row["story_identity"] for row in eligible}, {"shared"})
        self.assertEqual(bundle["story_links"][0]["link_id"], "shared")

    def test_audit_accepts_consistent_links_and_detects_tampering(self):
        self._pair()
        self.assertTrue(self.service.audit.run()["ok"])
        self.service.store.db.execute(
            "UPDATE story_link_members SET active=0 WHERE link_id='shared' AND external_id='b'")
        kinds = [issue["kind"] for issue in self.service.audit.run()["issues"]]
        self.assertIn("story_link_membership_state", kinds)
        self.service.store.db.execute(
            "UPDATE story_link_members SET active=1 WHERE link_id='shared'")
        self.service.store.db.execute("UPDATE story_links SET state='revoked' WHERE link_id='shared'")
        kinds = [issue["kind"] for issue in self.service.audit.run()["issues"]]
        self.assertIn("revoked_link_membership", kinds)

    def test_frames_still_count_original_observations_after_merge(self):
        self.accepted("a", "north-a", "alpha")
        self.accepted("b", "north-b", "beta")
        for source_id in ("north-a", "north-b"):
            self.service.frames.open(f"frame-{source_id}", source_id,
                                     query()["start"], query()["end"], 1)
        for source_id in ("north-a", "north-b"):
            self.service.frames.close(f"frame-{source_id}")
        self.service.story_links.determine(link())
        coverage = self.service.frames.quality(query())
        self.assertTrue(coverage["passed"])
        for frame in coverage["frames"]:
            self.assertEqual(frame["expected_count"], 1)
            self.assertEqual(frame["actual_count"], 1)
            self.assertEqual(frame["member_count"], 1)
            self.assertEqual(frame["coverage_basis"], "source_original_observation")
        preview = self.service.releases.preview(query())
        self.assertTrue(preview["quality"]["passed"])

    def test_window_comparison_shows_determination_effect(self):
        self.accepted("a", "north-a", "alpha")
        self.accepted("b", "north-b", "beta")
        self.service.story_links.determine(link())
        result = self.service.comparisons.windows(query(), query())
        row = result["regions"][0]
        self.assertEqual(row["left_stories"], 1)
        self.assertEqual(row["before_determination"]["left_stories"], 2)
        self.assertEqual(row["determination_effect"]["left_story_delta"], -1)

    def test_source_contribution_shows_both_views(self):
        self._pair()
        result = self.service.provenance.source_contribution(query(), "north-a")
        self.assertEqual(result["contribution"]["after_determination_stories"], 1)
        self.assertEqual(result["contribution"]["before_determination_stories"], 1)
        item = result["stories"][0]
        self.assertEqual(item["combined_observation_count"], 2)
        self.assertEqual(item["determined_by"], "dana")
        self.assertEqual(result["baseline_stories"][0]["source_external_ids"], ["a"])

    def test_revoked_links_report_has_dates_and_members(self):
        self._pair()
        self.service.story_links.revoke("shared", {"revoked_by": "dana",
                                                   "reason": "not the same event"})
        result = self.service.provenance.revoked_links(query())
        self.assertEqual(result["count"], 1)
        entry = result["links"][0]
        self.assertEqual(entry["revoked_by"], "dana")
        self.assertEqual(entry["revoke_reason"], "not the same event")
        self.assertEqual(len(entry["members"]), 2)
        self.assertEqual([event["kind"] for event in entry["events"]],
                         ["story_identity_determined", "story_identity_revoked"])


if __name__ == "__main__":
    unittest.main()
