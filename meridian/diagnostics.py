"""解释观察记录未进入比较结果的原因，并暴露未解决的认定冲突。"""
from collections import Counter, defaultdict
from .contracts import report_query
from .errors import require


class ReviewDiagnostics:
    def __init__(self, store, observations, sources, coding, analytics, frames):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding
        self.analytics = analytics
        self.frames = frames

    def queue(self, query, *, include_retracted=False):
        spec = report_query(query)
        report = self.analytics.report(spec)
        conflicted = {issue["link_id"] for issue in report["story_identity_issues"]}
        pending = []
        for row in self.analytics._rows(spec):
            item = row["item"]
            data = item["data"]
            state = row["state"]
            category = self.analytics._classify(row, spec)
            if category == "eligible" and row.get("membership") \
                    and row["membership"]["link_id"] in conflicted:
                category = "story_identity_conflict"
            if category not in {"uncoded", "disputed", "retracted", "story_identity_conflict"}:
                continue
            if category == "retracted" and not include_retracted:
                continue
            pending.append({"origin": data["origin"], "external_id": data["external_id"],
                            "revision": item["revision"], "source_id": data["source_id"],
                            "region": row["source"]["region"], "published_at": data["published_at"],
                            "status": category,
                            "reviewers": [decision["reviewer"] for decision in state["decisions"]],
                            "story_key": data["story_key"],
                            "link_id": row["membership"]["link_id"] if row.get("membership") else None})
        order = {"story_identity_conflict": 0, "disputed": 1, "uncoded": 2, "retracted": 3}
        pending.sort(key=lambda row: (order[row["status"]], row["published_at"],
                                      row["origin"], row["external_id"]))
        totals = Counter(row["status"] for row in pending)
        return {"query": spec, "total": len(pending), "by_status": dict(sorted(totals.items())),
                "items": pending}

    def observation_explanation(self, origin, external_id, query):
        spec = report_query(query)
        item = self.observations.describe(origin, external_id)
        data = item["data"]
        source = self.sources.describe(data["source_id"])
        membership = (self.analytics.story_links.membership_for(origin, external_id)
                      if self.analytics.story_links is not None else None)
        reasons = []
        if not spec["start"] <= data["published_at"] < spec["end"]:
            reasons.append("outside_window")
        if spec["regions"] and source["region"] not in spec["regions"]:
            reasons.append("outside_region")
        state = self.coding.status(origin, external_id, item["revision"])
        category = None
        if not reasons:
            category = self.analytics._classify(
                {"item": item, "source": source, "state": state,
                 "membership": ({"link_id": membership["link_id"]} if membership else None)}, spec)
            if category == "eligible" and membership:
                report = self.analytics.report(spec)
                if membership["link_id"] in {issue["link_id"]
                                             for issue in report["story_identity_issues"]}:
                    category = "story_identity_conflict"
            if category != "eligible":
                reasons.append(category)
        report = self.analytics.report(spec)
        identity = membership["link_id"] if membership else data["story_key"]
        matches = [story for story in report["stories"] if story["region"] == source["region"]
                   and story["story_key"] == identity]
        if category == "eligible" and not matches:
            reasons.append("aggregation_gap")
        return {"origin": origin, "external_id": external_id, "revision": item["revision"],
                "source": source, "coding_status": state["status"],
                "original_story_key": data["story_key"],
                "story_identity": identity,
                "story_link": ({"link_id": membership["link_id"],
                                "determined_by": membership["determined_by"],
                                "reason": membership["reason"],
                                "state": membership["state"]} if membership else None),
                "eligible": not reasons, "reasons": reasons, "story_rows": matches,
                "query_hash": report["query_hash"], "event_head": report["event_head"]}

    def _story_groups(self, spec):
        grouped = defaultdict(list)
        for row in self.analytics._rows(spec):
            if self.analytics._classify(row, spec) != "eligible":
                continue
            coding = row["state"]["coding"]
            key = (row["source"]["region"], self.analytics._identity_token(row),
                   coding["topic"], coding["audience"])
            grouped[key].append(row)
        return grouped

    def story_conflicts(self, query):
        spec = report_query(query)
        conflicts = []
        for key, items in sorted(self._story_groups(spec).items()):
            stances = {row["state"]["coding"]["stance"] for row in items}
            if len(stances) > 1:
                conflicts.append({
                    "region": key[0], "story_key": key[1][1], "topic": key[2],
                    "audience": key[3],
                    "identity": self.analytics._identity_meta(items[0]),
                    "stances": sorted(stances),
                    "observations": [{"origin": row["item"]["data"]["origin"],
                                      "external_id": row["item"]["data"]["external_id"],
                                      "source_id": row["source"]["source_id"],
                                      "original_story_key": (row["membership"]["original_story_key"]
                                                             if row.get("membership")
                                                             else row["item"]["data"]["story_key"]),
                                      "stance": row["state"]["coding"]["stance"],
                                      "link_id": (row["membership"]["link_id"]
                                                  if row.get("membership") else None)}
                                     for row in items]})
        return {"query": spec, "count": len(conflicts), "conflicts": conflicts}

    def identity_conflicts(self, query):
        spec = report_query(query)
        report = self.analytics.report(spec)
        issues = []
        for issue in report["story_identity_issues"]:
            link = (self.analytics.story_links.describe(issue["link_id"])
                    if self.analytics.story_links is not None else None)
            issues.append({**issue, "link": link})
        return {"query": spec, "count": len(issues), "issues": issues}

    def readiness(self, query, *, minimum_stories=1, maximum_exclusion_share=0.4):
        report = self.analytics.report(query)
        quality = self.analytics.quality(report, minimum_stories=minimum_stories,
                                         maximum_exclusion_share=maximum_exclusion_share)
        coverage = self.frames.quality(report["query"])
        queue = self.queue(query)
        conflicts = self.story_conflicts(query)
        warnings = []
        if not coverage["passed"]:
            warnings.append("collection_coverage")
        if conflicts["count"]:
            warnings.append("story_coding_conflicts")
        if queue["by_status"].get("disputed", 0):
            warnings.append("unresolved_decisions")
        if report["story_identity_issues"]:
            warnings.append("story_identity_conflict")
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "quality": quality, "coverage": coverage, "queue": queue["by_status"],
                "story_conflicts": conflicts["count"],
                "story_identity_issues": report["story_identity_issues"],
                "warnings": warnings,
                "ready": quality["passed"] and not warnings}
