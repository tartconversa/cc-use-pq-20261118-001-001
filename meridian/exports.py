"""导出可独立检查的完整报告证据包。"""
from collections import Counter, defaultdict
from .contracts import digest, report_query, text
from .errors import require


class EvidenceExporter:
    def __init__(self, store, observations, sources, coding, analytics, releases, story_links=None):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding
        self.analytics = analytics
        self.releases = releases
        self.story_links = story_links

    def _evidence_rows(self, report):
        rows = []
        query = report["query"]
        for row in self.analytics._rows(query):
            item = row["item"]
            data = item["data"]
            state = row["state"]
            category = self.analytics._classify(row, query)
            membership = row.get("membership")
            if category == "eligible" and membership and membership["link_id"] in {
                    issue["link_id"] for issue in report["story_identity_issues"]}:
                category = "story_identity_conflict"
            coding = state.get("coding")
            rows.append({"origin": data["origin"], "external_id": data["external_id"],
                         "revision": item["revision"], "observation_hash": item["hash"],
                         "source_id": data["source_id"], "region": row["source"]["region"],
                         "story_key": data["story_key"], "published_at": data["published_at"],
                         "category": category, "coding": coding,
                         "story_identity": (membership["link_id"] if membership
                                            else data["story_key"]),
                         "link_id": membership["link_id"] if membership else None,
                         "original_story_key": data["story_key"],
                         "reviewer_ids": [d["decision_id"] for d in state["decisions"]]})
        rows.sort(key=lambda item: (item["origin"], item["external_id"]))
        return rows

    def current(self, query):
        report = self.analytics.report(query)
        evidence = self._evidence_rows(report)
        bundle = {"kind": "current", "report": report, "evidence": evidence,
                  "event_head": report["event_head"]}
        if self.story_links is not None:
            bundle["story_links"] = self.story_links.list()
        bundle["bundle_hash"] = digest(bundle)
        return bundle

    def frozen(self, release_id):
        release_id = text(release_id, "release_id")
        release = self.releases.describe(release_id)
        report = release["report"]
        evidence = report["provenance"]
        bundle = {"kind": "frozen", "release_id": release_id, "report": report,
                  "evidence": evidence, "event_head": release["event_head"],
                  "preview_token": release["preview_token"]}
        bundle["bundle_hash"] = digest(bundle)
        return bundle

    @staticmethod
    def verify(bundle):
        require(isinstance(bundle, dict), "invalid_bundle", "bundle must be an object")
        required = {"kind", "report", "evidence", "event_head", "bundle_hash"}
        require(required <= set(bundle), "invalid_bundle", "bundle fields missing")
        actual = digest({key: value for key, value in bundle.items() if key != "bundle_hash"})
        require(actual == bundle["bundle_hash"], "bundle_hash_mismatch", "bundle content changed")
        report = bundle["report"]
        require(report["event_head"] == bundle["event_head"], "bundle_head_mismatch",
                "report and evidence head differ")
        require(report["story_count"] == len(report["stories"]), "bundle_count_mismatch",
                "story count differs from rows")
        require(report.get("source_key_story_count", len(report.get("baseline_stories", [])))
                == len(report.get("baseline_stories", [])), "bundle_count_mismatch",
                "source-key story count differs from baseline rows")
        categories = Counter(row["category"] for row in bundle["evidence"])
        require(sum(categories.values()) == report["observations_in_window"], "bundle_count_mismatch",
                "observation count differs from evidence")
        require(categories["eligible"] == report["eligible_observations"], "bundle_count_mismatch",
                "eligible count differs from evidence")
        for category, count in report["excluded"].items():
            require(categories[category] == count, "bundle_count_mismatch",
                    "exclusion differs from evidence", category=category)
        eligible_members = [row for row in bundle["evidence"] if row["category"] == "eligible"]
        by_identity = Counter(
            (row.get("region"), EvidenceExporter._evidence_token(row))
            for row in eligible_members)
        story_counts = Counter()
        for story in report["stories"]:
            token = (("manual_determination", story["link_id"]) if story.get("link_id")
                     else ("source_story_key", story["story_key"]))
            story_counts[(story["region"], token)] += story["observation_count"]
        if any(row.get("region") is None for row in eligible_members):
            # 旧冻结证据行不带地区；回退为仅按故事身份核对，仍能发现成员错配。
            by_identity = Counter(EvidenceExporter._evidence_token(row)
                                  for row in eligible_members)
            story_counts = Counter()
            for story in report["stories"]:
                token = (("manual_determination", story["link_id"]) if story.get("link_id")
                         else ("source_story_key", story["story_key"]))
                story_counts[token] += story["observation_count"]
        require(dict(by_identity) == story_counts, "bundle_count_mismatch",
                "story members differ from eligible evidence")
        return {"valid": True, "bundle_hash": bundle["bundle_hash"],
                "stories": report["story_count"], "observations": sum(categories.values())}

    @staticmethod
    def _evidence_token(row):
        """兼容旧冻结证据行：优先人工认定，缺失时回退到原始稿件键。"""
        if row.get("link_id"):
            return ("manual_determination", row["link_id"])
        return ("source_story_key", row.get("original_story_key") or row.get("story_key")
                or row.get("story_identity"))

    @staticmethod
    def _evidence_identity(row):
        return EvidenceExporter._evidence_token(row)[1]

    def source_breakdown(self, query):
        bundle = self.current(query)
        by_source = defaultdict(lambda: Counter())
        for row in bundle["evidence"]:
            by_source[row["source_id"]][row["category"]] += 1
        report = bundle["report"]
        conflicted = {issue["link_id"] for issue in report["story_identity_issues"]}
        after_groups = defaultdict(lambda: defaultdict(list))
        before_groups = defaultdict(lambda: defaultdict(list))
        for row in bundle["evidence"]:
            if row["category"] != "eligible":
                continue
            coding = row.get("coding") or {}
            after_key = (row["region"], row.get("story_identity", row["story_key"]),
                         coding.get("topic"), coding.get("audience"))
            before_key = (row["region"], row["story_key"],
                          coding.get("topic"), coding.get("audience"))
            after_groups[row["source_id"]][after_key].append(row)
            before_groups[row["source_id"]][before_key].append(row)

        def stance_map(groups):
            stances = Counter()
            for members in groups.values():
                values = {member["coding"]["stance"] for member in members}
                stances[next(iter(values)) if len(values) == 1 else "mixed"] += 1
            return dict(sorted(stances.items()))

        result = []
        for source_id, counts in sorted(by_source.items()):
            source = self.sources.describe(source_id)
            after = after_groups.get(source_id, {})
            before = before_groups.get(source_id, {})
            result.append({"source_id": source_id, "outlet": source["outlet"],
                           "region": source["region"], "categories": dict(sorted(counts.items())),
                           "observations": sum(counts.values()),
                           "story_contribution": {
                               "before_determination": {
                                   "story_count": len(before),
                                   "stance_counts": stance_map(before),
                               },
                               "after_determination": {
                                   "story_count": len(after),
                                   "stance_counts": stance_map(after),
                               },
                           },
                           "has_story_identity_conflict": any(
                               row.get("link_id") in conflicted
                               for row in bundle["evidence"] if row["source_id"] == source_id)})
        require(sum(row["observations"] for row in result) == bundle["report"]["observations_in_window"],
                "breakdown_mismatch", "source counts do not reconcile")
        return {"query_hash": bundle["report"]["query_hash"],
                "event_head": bundle["event_head"], "sources": result,
                "total_observations": bundle["report"]["observations_in_window"],
                "story_count": bundle["report"]["story_count"],
                "source_key_story_count": bundle["report"]["source_key_story_count"],
                "determination": bundle["report"]["determination"]}

    def story_evidence(self, query, region, story_key):
        spec = report_query(query)
        report = self.analytics.report(spec)
        story = [row for row in report["stories"]
                 if row["region"] == region and row["story_key"] == story_key]
        require(story, "story_missing", "story not found")
        rows = [row for row in self._evidence_rows(report) if row["region"] == region
                and (row.get("story_identity", row["story_key"]) == story_key)]
        require(sum(row["observation_count"] for row in story) ==
                sum(row["category"] == "eligible" for row in rows),
                "story_evidence_mismatch", "story count differs from evidence")
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "story": story, "evidence": rows,
                "before_determination": report["before_determination"],
                "after_determination": report["after_determination"]}
