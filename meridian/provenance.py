"""证据血缘和冻结视图与当前视图的差异解释。

故事身份认定是人工、可撤销的成员集合。这里提供从地区比较追到故事成员、
认定理由与撤销影响的路径，并区分已发布报告（冻结）与当前数据。
"""
from collections import Counter, defaultdict
from .contracts import report_query
from .errors import require


class ProvenanceExplorer:
    def __init__(self, store, observations, coding, analytics, releases, story_links=None):
        self.store = store
        self.observations = observations
        self.coding = coding
        self.analytics = analytics
        self.releases = releases
        self.story_links = story_links

    @staticmethod
    def _story_index(report):
        index = {}
        for item in report["stories"]:
            token = (("manual_determination", item["link_id"]) if item.get("link_id")
                     else ("source_story_key", item["story_key"]))
            index[(item["region"], token, item["topic"], item["audience"])] = item
        return index

    @staticmethod
    def _story_key(key):
        token = key[1]
        return {"region": key[0], "story_key": token[1], "topic": key[2], "audience": key[3]}

    @staticmethod
    def _members(story):
        if not story:
            return set()
        return {(member["origin"], member["external_id"], member.get("revision"))
                for member in story.get("members", [])}

    def _link_changes(self, old_report, current_report):
        before = {story["link_id"]: story for story in old_report["stories"]
                  if story.get("link_id")}
        after = {story["link_id"]: story for story in current_report["stories"]
                 if story.get("link_id")}
        changes = []
        for link_id in sorted(set(before) | set(after)):
            old = before.get(link_id)
            new = after.get(link_id)
            old_members = self._members(old)
            new_members = self._members(new)
            if old is not None and new is not None and old_members == new_members \
                    and {key: old[key] for key in ("stance", "topic", "audience", "observation_count")} \
                    == {key: new[key] for key in ("stance", "topic", "audience", "observation_count")}:
                continue
            if old is None:
                change = "added"
            elif new is None:
                change = "removed"
            else:
                change = "revised"
            entry = {
                "link_id": link_id, "change": change,
                "members_before": sorted(old_members), "members_after": sorted(new_members),
                "story_before": old, "story_after": new,
            }
            if self.story_links is not None:
                try:
                    link = self.story_links.describe(link_id)
                    entry["link_state"] = link["state"]
                    entry["determined_by"] = link["determined_by"]
                    entry["reason"] = link["reason"]
                    entry["revoked_by"] = link["revoked_by"]
                    entry["revoke_reason"] = link["revoke_reason"]
                    entry["revoked_at"] = link["revoked_at"]
                except Exception:
                    entry["link_state"] = None
            changes.append(entry)
        return changes

    def release_changes(self, release_id):
        old = self.releases.describe(release_id)
        current = self.analytics.report(old["report"]["query"])
        before = self._story_index(old["report"])
        after = self._story_index(current)
        changes = []
        for key in sorted(set(before) | set(after)):
            left = before.get(key)
            right = after.get(key)
            if left == right:
                continue
            changes.append({"key": self._story_key(key),
                            "change": "added" if left is None else "removed" if right is None else "revised",
                            "before": left, "after": right})
        excluded_before = old["report"]["excluded"]
        excluded_after = current["excluded"]
        exclusion_changes = {kind: {"before": excluded_before.get(kind, 0),
                                    "after": excluded_after.get(kind, 0)}
                             for kind in sorted(set(excluded_before) | set(excluded_after))
                             if excluded_before.get(kind, 0) != excluded_after.get(kind, 0)}
        link_changes = self._link_changes(old["report"], current)
        return {"release_id": release_id, "frozen_head": old["event_head"],
                "current_head": current["event_head"], "changed_stories": changes,
                "link_changes": link_changes,
                "exclusion_changes": exclusion_changes,
                "frozen": {"story_count": old["report"]["story_count"],
                           "source_key_story_count": old["report"].get("source_key_story_count"),
                           "determination": old["report"].get("determination")},
                "current": {"story_count": current["story_count"],
                            "source_key_story_count": current["source_key_story_count"],
                            "determination": current["determination"]},
                "same_result": not changes and not exclusion_changes and not link_changes}

    def observation_history(self, origin, external_id):
        revisions = self.observations.lineage(origin, external_id)
        result = []
        for item in revisions:
            decisions = self.coding.decisions_for(origin, external_id, item["revision"])
            state = self.coding.status(origin, external_id, item["revision"])
            events = self.store.all("SELECT event_no,kind,occurred_at FROM events WHERE entity_key=? ORDER BY event_no",
                                    (f"{origin}/{external_id}",))
            result.append({"revision": item["revision"], "is_head": item["is_head"],
                           "status": item["status"], "hash": item["hash"],
                           "supersedes": item["supersedes"], "data": item["data"],
                           "decision_ids": [row["decision_id"] for row in decisions],
                           "coding_status": state["status"],
                           "events": [dict(row) for row in events]})
        return {"origin": origin, "external_id": external_id, "revisions": result,
                "story_links": (self.story_links.history_for(origin, external_id)
                                if self.story_links is not None else [])}

    def _baseline_story_index(self, report):
        return {(item["region"], ("source_story_key", item["story_key"]),
                 item["topic"], item["audience"]): item
                for item in report.get("baseline_stories", [])}

    def source_contribution(self, query, source_id):
        spec = report_query(query)
        report = self.analytics.report(spec)
        conflicted = {issue["link_id"] for issue in report["story_identity_issues"]}
        rows = [row for row in self.analytics._rows(spec)
                if row["source"]["source_id"] == source_id]
        require(rows, "source_outside_query", "source has no observations in query")
        categories = Counter()
        stories = defaultdict(list)
        baseline_stories = defaultdict(list)
        by_external = {}
        for row in rows:
            category = self.analytics._classify(row, spec)
            if category == "eligible" and row.get("membership") \
                    and row["membership"]["link_id"] in conflicted:
                category = "story_identity_conflict"
            categories[category] += 1
            if category != "eligible":
                continue
            data = row["item"]["data"]
            coding = row["state"]["coding"]
            pair = (data["origin"], data["external_id"])
            by_external[pair] = row
            key = (row["source"]["region"], self.analytics._identity_token(row),
                   coding["topic"], coding["audience"])
            stories[key].append(pair)
            baseline_key = (row["source"]["region"], ("source_story_key", data["story_key"]),
                            coding["topic"], coding["audience"])
            baseline_stories[baseline_key].append(pair)
        index = self._story_index(report)
        baseline_index = self._baseline_story_index(report)
        story_items = []
        for key, ids in sorted(stories.items()):
            combined = index.get(key)
            story_items.append({
                "key": self._story_key(key),
                "identity": {"mode": ("manual_determination"
                                      if combined and combined["link_id"] else "source_story_key"),
                             "link_id": combined["link_id"] if combined else None},
                "source_members": [{"origin": origin, "external_id": external_id}
                                   for origin, external_id in sorted(ids)],
                "source_external_ids": sorted(external_id for _, external_id in ids),
                "source_contribution": len(ids),
                "combined_observation_count": combined["observation_count"] if combined else None,
                "combined_stance": combined["stance"] if combined else None,
                "combined_outlets": combined["outlets"] if combined else None,
                "original_story_keys": sorted({by_external[pair]["item"]["data"]["story_key"]
                                               for pair in ids}),
                "determined_by": combined["determined_by"] if combined else None,
                "determination_reason": combined["determination_reason"] if combined else None,
            })
        baseline_items = []
        for key, ids in sorted(baseline_stories.items()):
            story = baseline_index.get(key)
            baseline_items.append({
                "key": self._story_key(key),
                "source_members": [{"origin": origin, "external_id": external_id}
                                   for origin, external_id in sorted(ids)],
                "source_external_ids": sorted(external_id for _, external_id in ids),
                "story_observation_count": story["observation_count"] if story else None,
                "stances": sorted({by_external[pair]["state"]["coding"]["stance"]
                                   for pair in ids}),
            })
        after_story_stances = Counter(item["combined_stance"] for item in story_items)
        before_story_stances = Counter(
            (item["stances"][0] if len(item["stances"]) == 1 else "mixed")
            for item in baseline_items)
        after_observation_stances = Counter(
            by_external[pair]["state"]["coding"]["stance"]
            for ids in stories.values() for pair in ids)
        return {"source_id": source_id, "query_hash": report["query_hash"],
                "event_head": report["event_head"], "observation_count": len(rows),
                "categories": dict(sorted(categories.items())),
                "story_identity_issues": report["story_identity_issues"],
                "stories": story_items,
                "baseline_stories": baseline_items,
                "stance_comparison": {
                    "before_determination": {
                        "story_stances": dict(sorted(before_story_stances.items())),
                        "observation_stances": dict(sorted(after_observation_stances.items())),
                    },
                    "after_determination": {
                        "story_stances": dict(sorted(after_story_stances.items())),
                        "observation_stances": dict(sorted(after_observation_stances.items())),
                    },
                },
                "contribution": {
                    "after_determination_stories": len(story_items),
                    "before_determination_stories": len(baseline_items),
                    "after_determination_observations": sum(len(ids) for ids in stories.values()),
                    "before_determination_observations": sum(len(ids)
                                                             for ids in baseline_stories.values()),
                }}

    def story_detail(self, query, region, identity_key, *, link_id=None):
        """从地区比较追到单个故事的成员、认定理由和撤销后的变化。

        ``link_id`` 用于在人工认定与未认定稿件恰好同名时明确选择认定故事。
        """
        spec = report_query(query)
        report = self.analytics.report(spec)
        stories = [story for story in report["stories"]
                   if story["region"] == region and story["story_key"] == identity_key
                   and (link_id is None or story["link_id"] == link_id)]
        require(stories, "story_missing", "story not found in query")
        if len(stories) > 1:
            require(link_id is not None, "story_ambiguous",
                    "story key matches both a determination and source-key groups",
                    story_keys=[story["link_id"] for story in stories])
        story = stories[0]
        link = None
        if story["link_id"] and self.story_links is not None:
            link = self.story_links.describe(story["link_id"])
            link["events"] = self.story_links.events(story["link_id"])
            for member in link["members"]:
                member["membership_history"] = self.story_links.history_for(
                    member["origin"], member["external_id"])
        observations = []
        token = (("manual_determination", story["link_id"]) if story["link_id"]
                 else ("source_story_key", identity_key))
        for row in self.analytics._rows(spec):
            if row["source"]["region"] != region:
                continue
            if self.analytics._identity_token(row) != token:
                continue
            data = row["item"]["data"]
            observations.append({"origin": data["origin"], "external_id": data["external_id"],
                                 "revision": row["item"]["revision"],
                                 "current_story_key": data["story_key"],
                                 "original_story_key": (row["membership"]["original_story_key"]
                                                        if row.get("membership")
                                                        else data["story_key"]),
                                 "source_id": data["source_id"], "outlet": row["source"]["outlet"],
                                 "classification": self.analytics._classify(row, spec),
                                 "coding_status": row["state"]["status"],
                                 "stance": (row["state"]["coding"]["stance"]
                                            if row["state"]["status"] in {"consensus", "adjudicated"}
                                            else None)})
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "story": story, "link": link,
                "observations": sorted(observations,
                                       key=lambda item: (item["origin"], item["external_id"])),
                "before_determination": report["before_determination"],
                "after_determination": report["after_determination"],
                "story_identity_issues": report["story_identity_issues"]}

    def revoked_links(self, query=None):
        spec = report_query(query) if query is not None else None
        links = self.story_links.list(state="revoked") if self.story_links is not None else []
        if spec is not None:
            links = [link for link in links
                     if not spec["regions"] or link["region"] in spec["regions"]]
        result = []
        for link in links:
            result.append({
                "link_id": link["link_id"], "region": link["region"],
                "determined_by": link["determined_by"], "reason": link["reason"],
                "created_at": link["created_at"], "created_event_no": link["created_event_no"],
                "revoked_by": link["revoked_by"], "revoke_reason": link["revoke_reason"],
                "revoked_at": link["revoked_at"], "revoked_event_no": link["revoked_event_no"],
                "members": link["members"],
                "events": self.story_links.events(link["link_id"]),
            })
        return {"query": spec, "count": len(result), "links": result}

    def coding_lineage(self, origin, external_id):
        item = self.observations.describe(origin, external_id)
        decisions = self.coding.decisions_for(origin, external_id, item["revision"])
        status = self.coding.status(origin, external_id, item["revision"])
        key = f"{origin}/{external_id}/{item['revision']}"
        ruling = self.store.one("SELECT * FROM adjudications WHERE case_key=?", (key,))
        return {"origin": origin, "external_id": external_id, "revision": item["revision"],
                "observation_hash": item["hash"], "status": status["status"],
                "decisions": decisions, "adjudication":
                {field: ruling[field] for field in ("adjudicator", "topic", "stance", "audience",
                                                   "reason", "event_no")} if ruling else None,
                "story_links": (self.story_links.history_for(origin, external_id)
                                if self.story_links is not None else [])}
