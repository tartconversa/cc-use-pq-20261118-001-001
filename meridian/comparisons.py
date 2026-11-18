"""保留分母的窗口、地区与议题对比。

每行同时给出人工认定后的分母/倾向，以及认定前的原始稿件键分母，
使研究员能直接看到故事身份认定对趋势和贡献的影响。
"""
from collections import defaultdict
from .contracts import report_query
from .errors import require


class ComparisonEngine:
    def __init__(self, analytics):
        self.analytics = analytics

    @staticmethod
    def _region_map(report):
        return {row["region"]: row for row in report["regions"]}

    @staticmethod
    def _stance_comparison(left, right):
        rows = {}
        for stance in sorted(set(left.get("stance_counts", {})) | set(right.get("stance_counts", {}))):
            old = left.get("stance_counts", {}).get(stance, 0)
            new = right.get("stance_counts", {}).get(stance, 0)
            old_share = left.get("stance_shares", {}).get(stance)
            new_share = right.get("stance_shares", {}).get(stance)
            rows[stance] = {
                "left_count": old, "right_count": new, "count_delta": new - old,
                "left_share": old_share, "right_share": new_share,
                "share_delta": (round(new_share - old_share, 6)
                                if old_share is not None and new_share is not None else None)}
        return rows

    def _region_comparison(self, previous, current):
        old_count = previous.get("story_count", 0)
        new_count = current.get("story_count", 0)
        before_left = previous.get("before_determination", {})
        before_right = current.get("before_determination", {})
        return {
            "left_stories": old_count, "right_stories": new_count,
            "story_delta": new_count - old_count,
            "stances": self._stance_comparison(previous, current),
            "before_determination": {
                "left_stories": before_left.get("story_count", 0),
                "right_stories": before_right.get("story_count", 0),
                "story_delta": before_right.get("story_count", 0) - before_left.get("story_count", 0),
                "stances": self._stance_comparison(before_left, before_right),
            },
            "determination_effect": {
                "left_story_delta": old_count - before_left.get("story_count", 0),
                "right_story_delta": new_count - before_right.get("story_count", 0),
            },
        }

    def windows(self, left_query, right_query):
        left = self.analytics.report(left_query)
        right = self.analytics.report(right_query)
        require(left["query"]["regions"] == right["query"]["regions"], "incompatible_query",
                "region filters must match for comparison")
        require(left["query"]["topics"] == right["query"]["topics"], "incompatible_query",
                "topic filters must match for comparison")
        require(left["query"]["audiences"] == right["query"]["audiences"], "incompatible_query",
                "audience filters must match for comparison")
        left_regions = self._region_map(left)
        right_regions = self._region_map(right)
        rows = []
        for region in sorted(set(left_regions) | set(right_regions)):
            previous = left_regions.get(region, {})
            current = right_regions.get(region, {})
            rows.append({"region": region, **self._region_comparison(previous, current)})
        return {"left_query_hash": left["query_hash"], "right_query_hash": right["query_hash"],
                "left_head": left["event_head"], "right_head": right["event_head"],
                "left_excluded": left["excluded"], "right_excluded": right["excluded"],
                "left_determination": left["determination"],
                "right_determination": right["determination"],
                "regions": rows}

    def regions(self, query, left_region, right_region):
        spec = report_query(query)
        require(left_region != right_region, "same_region", "choose distinct regions")
        if spec["regions"]:
            require(left_region in spec["regions"] and right_region in spec["regions"],
                    "incompatible_query", "both regions must belong to query")
        report = self.analytics.report(spec)
        regions = self._region_map(report)
        require(left_region in regions and right_region in regions, "region_missing",
                "both regions need eligible stories")
        left = regions[left_region]
        right = regions[right_region]
        stances = {}
        for stance in sorted(set(left["stance_counts"]) | set(right["stance_counts"])):
            left_share = left["stance_counts"].get(stance, 0) / left["story_count"]
            right_share = right["stance_counts"].get(stance, 0) / right["story_count"]
            stances[stance] = {"left_share": round(left_share, 6),
                               "right_share": round(right_share, 6),
                               "difference": round(right_share - left_share, 6)}
        before = {}
        for stance in sorted(set(left["before_determination"]["stance_shares"])
                             | set(right["before_determination"]["stance_shares"])):
            before[stance] = {
                "left_share": left["before_determination"]["stance_shares"].get(stance),
                "right_share": right["before_determination"]["stance_shares"].get(stance)}
            if before[stance]["left_share"] is not None and before[stance]["right_share"] is not None:
                before[stance]["difference"] = round(
                    before[stance]["right_share"] - before[stance]["left_share"], 6)
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "left": left, "right": right, "stances": stances,
                "before_determination_stances": before,
                "determination": {"left": left["determination"],
                                  "right": right["determination"]},
                "excluded": report["excluded"],
                "story_identity_issues": report["story_identity_issues"]}

    def topic_matrix(self, query):
        report = self.analytics.report(query)
        matrix = defaultdict(lambda: defaultdict(lambda: {"stories": 0, "stances": defaultdict(int)}))
        baseline = defaultdict(lambda: defaultdict(lambda: 0))
        for story in report["stories"]:
            cell = matrix[story["region"]][story["topic"]]
            cell["stories"] += 1
            cell["stances"][story["stance"]] += 1
        for story in report["baseline_stories"]:
            baseline[story["region"]][story["topic"]] += 1
        rows = []
        for region, topics in sorted(matrix.items()):
            for topic, cell in sorted(topics.items()):
                rows.append({"region": region, "topic": topic, "story_count": cell["stories"],
                             "stance_counts": dict(sorted(cell["stances"].items())),
                             "source_key_story_count": baseline[region][topic]})
        require(sum(row["story_count"] for row in rows) == report["story_count"], "matrix_mismatch",
                "topic matrix does not reconcile to report")
        require(sum(row["source_key_story_count"] for row in rows)
                == report["source_key_story_count"], "matrix_mismatch",
                "source-key matrix does not reconcile to report")
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "total_stories": report["story_count"],
                "source_key_total_stories": report["source_key_story_count"],
                "rows": rows}
