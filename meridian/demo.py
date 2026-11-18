"""运行本地合成数据的端到端验收场景。"""
import json
import tempfile
from pathlib import Path
from .service import MeridianService


def source(source_id, region, outlet):
    return {"source_id": source_id, "region": region, "outlet": outlet,
            "topics": ["technology", "culture"], "audiences": ["general", "students"]}


def observation(external_id, source_id, story_key, published_at="2026-11-01T10:00:00+00:00"):
    return {"origin": "local-fixture", "external_id": external_id, "source_id": source_id,
            "story_key": story_key, "published_at": published_at,
            "received_at": "2026-11-02T09:00:00+00:00",
            "headline": f"Synthetic observation {external_id}",
            "url": f"https://example.invalid/{external_id}"}


def decision(external_id, reviewer, stance, topic="technology", audience="general"):
    return {"decision_id": f"{external_id}-{reviewer}", "origin": "local-fixture",
            "external_id": external_id, "revision": 1, "reviewer": reviewer,
            "topic": topic, "stance": stance, "audience": audience,
            "reason": "Synthetic reviewer judgment based on supplied fixture"}


def query():
    return {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
            "regions": ["North"], "topics": [], "audiences": []}


def story_link(link_id, external_ids, region="North"):
    return {"link_id": link_id, "region": region,
            "members": [{"origin": "local-fixture", "external_id": external_id}
                        for external_id in external_ids],
            "determined_by": "reviewer-c",
            "reason": "Synthetic manual determination: same event in both outlets"}


def scenario():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "scenario.sqlite3")
        service = MeridianService(path)
        service.sources.register(source("s1", "North", "Outlet A"))
        service.sources.register(source("s2", "North", "Outlet B"))
        service.sources.register(source("s3", "South", "Outlet C"))
        rows = [observation("a", "s1", "story-1"), observation("b", "s2", "story-1"),
                observation("c", "s1", "story-2"), observation("d", "s2", "story-2-wire")]
        first = service.batches.step("fixture-batch", rows, limit=1)
        assert first["cursor"] == 1
        batch = service.batches.step("fixture-batch", rows)
        assert batch["accepted"] == 4 and batch["state"] == "closed"
        for external_id in ("a", "b", "c", "d"):
            service.coding.submit(decision(external_id, "reviewer-a", "positive"))
            service.coding.submit(decision(external_id, "reviewer-b", "positive"))
        before = service.releases.preview(query())
        assert before["report"]["story_count"] == 3
        service.story_links.determine(story_link("story-2-linked", ["c", "d"]))
        preview = service.releases.preview(query())
        assert preview["quality"]["passed"]
        assert preview["report"]["story_count"] == 2
        assert preview["report"]["source_key_story_count"] == 3
        assert preview["report"]["determination"]["story_delta"] == -1
        release = service.releases.publish("november", query(), preview["token"])
        assert release["report"]["story_count"] == 2
        assert release["report"]["source_key_story_count"] == 3
        service.observations.retract("local-fixture", "c", "bad source record", expected_revision=1)
        service.observations.retract("local-fixture", "d", "bad source record", expected_revision=1)
        comparison = service.releases.compare("november")
        assert comparison["story_count_delta"] == -1
        assert service.releases.describe("november")["report"]["story_count"] == 2
        service.story_links.revoke("story-2-linked", {"revoked_by": "reviewer-d",
                                                       "reason": "Synthetic revocation: records differ"})
        changes = service.provenance.release_changes("november")
        assert changes["link_changes"][0]["change"] == "removed"
        audit = service.audit.run()
        assert audit["ok"]
        result = {"batch": batch["state"], "released_stories": 2,
                  "released_source_key_stories": 3,
                  "current_delta": comparison["story_count_delta"],
                  "revoked_link_change": changes["link_changes"][0]["change"],
                  "audit": audit["ok"]}
        service.close()
        return result


if __name__ == "__main__":
    print(json.dumps(scenario(), ensure_ascii=False, sort_keys=True))

