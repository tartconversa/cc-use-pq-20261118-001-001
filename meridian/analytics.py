"""区域汇总、显式排除与故事级去重。

故事身份有两种来源：
1. 来源提供的原始 ``story_key``（未获人工认定的稿件仍按此分组）；
2. 研究员明确登记的故事认定（``story_links``）。只有认定中的稿件才跨来源合并，
   相似标题或相似编号不会触发自动合并。

报告同时给出"认定前"（原始稿件键）和"认定后"（人工认定）的分母与倾向分布，
两者使用同一批符合条件的观察记录，差异只来自故事身份认定。

认定只是一个成员集合；成员是否仍可进入报告，仍由该稿件当前版本、撤回状态和
编码状态决定。因此更正、撤回或撤销认定之后，故事成员和分母会随当前数据重算，
已发布报告则保持冻结。
"""
from collections import Counter, defaultdict
from .contracts import digest, report_query
from .errors import require


class EvidenceAnalytics:
    def __init__(self, store, observations, sources, coding, story_links=None):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding
        self.story_links = story_links

    def _memberships(self):
        return self.story_links.active_memberships() if self.story_links is not None else {}

    def _rows(self, spec):
        memberships = self._memberships()
        rows = []
        for item in self.observations.current():
            data = item["data"]
            if not spec["start"] <= data["published_at"] < spec["end"]:
                continue
            source = self.sources.describe(data["source_id"])
            if spec["regions"] and source["region"] not in spec["regions"]:
                continue
            state = self.coding.status(data["origin"], data["external_id"], item["revision"])
            rows.append({"item": item, "source": source, "state": state,
                         "membership": memberships.get((data["origin"], data["external_id"]))})
        return rows

    def _classify(self, row, spec):
        item = row["item"]
        state = row["state"]
        if item["status"] == "retracted":
            return "retracted"
        if state["status"] == "awaiting_review":
            return "uncoded"
        if state["status"] == "disputed":
            return "disputed"
        coding = state["coding"]
        if spec["topics"] and coding["topic"] not in spec["topics"]:
            return "outside_topic"
        if spec["audiences"] and coding["audience"] not in spec["audiences"]:
            return "outside_audience"
        return "eligible"

    # -- 故事身份 -----------------------------------------------------
    @staticmethod
    def _identity_token(row):
        """内部分组令牌。人工认定与原始键分属不同命名空间，避免同名误合并。"""
        membership = row.get("membership")
        if membership:
            return ("manual_determination", membership["link_id"])
        return ("source_story_key", row["item"]["data"]["story_key"])

    @staticmethod
    def _identity_meta(row):
        membership = row.get("membership")
        if membership:
            return {"mode": "manual_determination", "identity_key": membership["link_id"],
                    "link_id": membership["link_id"],
                    "determined_by": membership["determined_by"],
                    "reason": membership["reason"],
                    "link_version": membership["version"],
                    "original_story_key": membership["original_story_key"]}
        key = row["item"]["data"]["story_key"]
        return {"mode": "source_story_key", "identity_key": key, "link_id": None,
                "determined_by": None, "reason": None, "link_version": None,
                "original_story_key": key}

    def _identity_issues(self, rows):
        """认定成员之间存在无法归入同一故事分母的编码冲突时显式暴露。

        ``rows`` 应包含窗口与地区内所有已解决编码的稿件；议题/受众筛选是查询
        选择，不应被误报为认定冲突。报告不会只更新冲突认定中的一部分成员。
        """
        grouped = defaultdict(list)
        for row in rows:
            if row.get("membership"):
                grouped[row["membership"]["link_id"]].append(row)
        issues = []
        for link_id, members in sorted(grouped.items()):
            categories = {(row["state"]["coding"]["topic"], row["state"]["coding"]["audience"])
                          for row in members}
            if len(categories) > 1:
                issues.append({
                    "link_id": link_id, "kind": "mixed_topic_audience",
                    "categories": [{"topic": topic, "audience": audience}
                                   for topic, audience in sorted(categories)],
                    "members": sorted([{"origin": row["item"]["data"]["origin"],
                                        "external_id": row["item"]["data"]["external_id"]}
                                       for row in members], key=lambda item: (item["origin"],
                                                                              item["external_id"])),
                })
        return issues

    @staticmethod
    def _stance(members):
        stances = {row["state"]["coding"]["stance"] for row in members}
        return next(iter(stances)) if len(stances) == 1 else "mixed"

    @staticmethod
    def _share(count, denominator):
        return round(count / denominator, 6) if denominator else None

    @classmethod
    def _distribution(cls, stories, denominator):
        counts = Counter(story["stance"] for story in stories)
        shares = {key: cls._share(value, denominator) for key, value in sorted(counts.items())}
        return dict(sorted(counts.items())), shares

    def _group(self, rows, *, by_identity):
        groups = defaultdict(list)
        for row in rows:
            coding = row["state"]["coding"]
            anchor = self._identity_token(row) if by_identity \
                else ("source_story_key", row["item"]["data"]["story_key"])
            groups[(row["source"]["region"], anchor, coding["topic"], coding["audience"])].append(row)
        return groups

    def _story_rows(self, groups, *, by_identity):
        stories = []
        for (region, anchor, topic, audience), members in sorted(groups.items()):
            ordered = sorted(members, key=lambda row: (
                row["item"]["data"]["published_at"], row["item"]["data"]["source_id"],
                row["item"]["data"]["origin"], row["item"]["data"]["external_id"]))
            identity = self._identity_meta(ordered[0]) if by_identity else {
                "mode": "source_story_key", "identity_key": anchor[1], "link_id": None,
                "determined_by": None, "reason": None, "link_version": None,
                "original_story_key": anchor[1]}
            members_out = [{
                "origin": row["item"]["data"]["origin"],
                "external_id": row["item"]["data"]["external_id"],
                "revision": row["item"]["revision"],
                "source_id": row["item"]["data"]["source_id"],
                "outlet": row["source"]["outlet"],
                "story_key": row["item"]["data"]["story_key"],
                # 认定时登记的原始稿件键不会因后续更正而被覆盖。
                "original_story_key": (row["membership"]["original_story_key"]
                                       if row.get("membership")
                                       else row["item"]["data"]["story_key"]),
                "stance": row["state"]["coding"]["stance"],
            } for row in ordered]
            stories.append({
                "region": region, "story_key": anchor[1], "topic": topic, "audience": audience,
                "stance": self._stance(members),
                "observation_count": len(members),
                "outlets": sorted({row["source"]["outlet"] for row in members}),
                "source_ids": sorted({row["source"]["source_id"] for row in members}),
                "representative": {"origin": ordered[0]["item"]["data"]["origin"],
                                   "external_id": ordered[0]["item"]["data"]["external_id"]},
                "identity": identity,
                "link_id": identity["link_id"],
                "determined_by": identity["determined_by"],
                "determination_reason": identity["reason"],
                "link_version": identity["link_version"],
                "original_story_keys": sorted({item["original_story_key"]
                                               for item in members_out}),
                "members": members_out,
            })
        return stories

    @classmethod
    def _baseline(cls, stories):
        counts, shares = cls._distribution(stories, len(stories))
        return {"story_count": len(stories), "stance_counts": counts, "stance_shares": shares}

    @staticmethod
    def _delta(after, before):
        counts = {stance: after["stance_counts"].get(stance, 0)
                  - before["stance_counts"].get(stance, 0)
                  for stance in sorted(set(after["stance_counts"]) | set(before["stance_counts"]))}
        shares = {}
        for stance in sorted(set(after["stance_shares"]) | set(before["stance_shares"])):
            left = before["stance_shares"].get(stance)
            right = after["stance_shares"].get(stance)
            shares[stance] = round(right - left, 6) if left is not None and right is not None else None
        return {"story_delta": after["story_count"] - before["story_count"],
                "stance_count_delta": counts, "stance_share_delta": shares}

    # -- 报告 ---------------------------------------------------------
    def report(self, payload):
        spec = report_query(payload)
        observed = self._rows(spec)
        # 只看能进入报告类别的成员：撤回或未编码的稿件不参与认定冲突判定，
        # 但它们从故事中移除后必须触发成员重算。
        resolved = [row for row in observed
                    if self._classify(row, spec) in {"eligible", "outside_topic", "outside_audience"}]
        issues = self._identity_issues(resolved)
        conflicted_links = {issue["link_id"] for issue in issues}

        exclusions = Counter()
        eligible = []
        provenance = []
        for row in observed:
            category = self._classify(row, spec)
            if category == "eligible" and row.get("membership") \
                    and row["membership"]["link_id"] in conflicted_links:
                category = "story_identity_conflict"
            data = row["item"]["data"]
            provenance.append({"origin": data["origin"], "external_id": data["external_id"],
                               "revision": row["item"]["revision"],
                               "source_id": data["source_id"], "category": category,
                               "original_story_key": data["story_key"],
                               "story_identity": (row["membership"]["link_id"]
                                                  if row.get("membership") else data["story_key"]),
                               "link_id": row["membership"]["link_id"] if row.get("membership") else None})
            if category != "eligible":
                exclusions[category] += 1
                continue
            eligible.append(row)

        story_rows = self._story_rows(self._group(eligible, by_identity=True), by_identity=True)
        baseline_rows = self._story_rows(self._group(eligible, by_identity=False), by_identity=False)

        by_region = defaultdict(lambda: {"after": [], "before": [], "observations": 0,
                                         "linked_observations": 0, "link_ids": set()})
        for story in story_rows:
            bucket = by_region[story["region"]]
            bucket["after"].append(story)
            bucket["observations"] += story["observation_count"]
            if story["link_id"]:
                bucket["linked_observations"] += story["observation_count"]
                bucket["link_ids"].add(story["link_id"])
        for story in baseline_rows:
            by_region[story["region"]]["before"].append(story)

        regions = []
        for region, data in sorted(by_region.items()):
            after = self._baseline(data["after"])
            before = self._baseline(data["before"])
            regions.append({
                "region": region,
                "story_count": after["story_count"],
                "observation_count": data["observations"],
                "stance_counts": after["stance_counts"],
                "stance_shares": after["stance_shares"],
                "topic_counts": dict(sorted(Counter(story["topic"]
                                                    for story in data["after"]).items())),
                "audience_counts": dict(sorted(Counter(story["audience"]
                                                       for story in data["after"]).items())),
                "before_determination": before,
                "after_determination": after,
                "determination": {
                    **self._delta(after, before),
                    "linked_observations": data["linked_observations"],
                    "link_count": len(data["link_ids"]),
                },
                # 供从地区比较逐层下钻到故事成员与认定理由。
                "story_identities": [{
                    "story_key": story["story_key"], "link_id": story["link_id"],
                    "topic": story["topic"], "audience": story["audience"],
                    "stance": story["stance"],
                    "observation_count": story["observation_count"],
                    "outlets": story["outlets"],
                    "determined_by": story["determined_by"],
                    "determination_reason": story["determination_reason"],
                    "original_story_keys": story["original_story_keys"],
                } for story in data["after"]],
                # 兼容旧报表字段：故事数与倾向分布始终反映人工认定后的结果。
                "source_key_story_count": before["story_count"],
                "source_key_stance_counts": before["stance_counts"],
                "source_key_stance_shares": before["stance_shares"],
            })

        before_all = self._baseline(baseline_rows)
        after_all = self._baseline(story_rows)
        linked_stories = sum(1 for story in story_rows if story["link_id"])
        return {
            "query": spec, "query_hash": digest(spec), "event_head": self.store.head(),
            "observations_in_window": len(observed),
            "eligible_observations": len(eligible),
            "excluded": dict(sorted(exclusions.items())),
            "story_count": len(story_rows),
            "source_key_story_count": len(baseline_rows),
            "linked_story_count": linked_stories,
            "linked_observation_count": sum(story["observation_count"] for story in story_rows
                                            if story["link_id"]),
            "determination": {
                "active": bool(linked_stories),
                "story_delta": len(story_rows) - len(baseline_rows),
                "stance_count_delta": {
                    stance: after_all["stance_counts"].get(stance, 0)
                    - before_all["stance_counts"].get(stance, 0)
                    for stance in sorted(set(after_all["stance_counts"])
                                         | set(before_all["stance_counts"]))},
                "stance_share_delta": {
                    stance: (round(after_all["stance_shares"][stance]
                                   - before_all["stance_shares"][stance], 6)
                             if stance in after_all["stance_shares"]
                             and stance in before_all["stance_shares"] else None)
                    for stance in sorted(set(after_all["stance_shares"])
                                         | set(before_all["stance_shares"]))},
                "linked_stories": linked_stories,
            },
            "before_determination": before_all,
            "after_determination": after_all,
            "story_identity_issues": issues,
            "stories": story_rows,
            "baseline_stories": baseline_rows,
            "regions": regions,
            "provenance": sorted(provenance, key=lambda item: (item["origin"], item["external_id"])),
        }

    def trace_story(self, spec, region, story_key):
        report = self.report(spec)
        rows = [row for row in report["stories"] if row["region"] == region
                and row["story_key"] == story_key]
        require(rows, "story_missing", "story not found in query")
        token = (("manual_determination", rows[0]["link_id"]) if rows[0]["link_id"]
                 else ("source_story_key", story_key))
        observations = []
        for row in self._rows(report["query"]):
            data = row["item"]["data"]
            if row["source"]["region"] != region:
                continue
            if self._identity_token(row) != token:
                continue
            observations.append({
                "origin": data["origin"], "external_id": data["external_id"],
                "revision": row["item"]["revision"],
                "current_story_key": data["story_key"],
                "original_story_key": (row["membership"]["original_story_key"]
                                       if row.get("membership") else data["story_key"]),
                "link_id": row["membership"]["link_id"] if row.get("membership") else None,
                "membership": ({"link_id": row["membership"]["link_id"],
                                "determined_by": row["membership"]["determined_by"],
                                "reason": row["membership"]["reason"]}
                               if row.get("membership") else None),
                "classification": self._classify(row, report["query"]),
                "coding_status": row["state"]["status"],
                "source_id": data["source_id"], "outlet": row["source"]["outlet"]})
        return {"stories": rows, "observations": sorted(
            observations, key=lambda x: (x["origin"], x["external_id"]))}

    def quality(self, report, *, minimum_stories=1, maximum_exclusion_share=0.4):
        require(type(minimum_stories) is int and minimum_stories >= 1, "invalid_gate",
                "minimum_stories invalid")
        require(isinstance(maximum_exclusion_share, (int, float))
                and 0 <= maximum_exclusion_share <= 1, "invalid_gate",
                "maximum_exclusion_share invalid")
        total = report["observations_in_window"]
        reasons = []
        if report["story_count"] < minimum_stories:
            reasons.append("insufficient_stories")
        excluded = sum(report["excluded"].values())
        if total == 0 or excluded / total > maximum_exclusion_share:
            reasons.append("excess_exclusions")
        if report["excluded"].get("disputed", 0):
            reasons.append("unresolved_disputes")
        if any(story["stance"] == "mixed" and story["observation_count"] > 1
               for story in report["stories"]):
            reasons.append("cross_source_stance_conflict")
        if report.get("story_identity_issues"):
            reasons.append("story_identity_incomplete")
        return {"passed": not reasons, "reasons": reasons, "total": total,
                "excluded": excluded, "minimum_stories": minimum_stories,
                "maximum_exclusion_share": maximum_exclusion_share,
                "story_count": report["story_count"],
                "source_key_story_count": report.get("source_key_story_count")}
