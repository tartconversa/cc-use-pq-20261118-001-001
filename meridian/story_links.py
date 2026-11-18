"""可追溯的人工故事身份认定与撤销。

来源提供的 story_key 只描述单篇稿件，不能可靠地跨来源识别同一故事。
本模块记录研究员明确给出的成员集合；确定和撤销都在单一事务内完成，
并保留认定人、理由、成员快照和事件边界，供报告与导出复核。
"""
from .contracts import digest, story_link, story_link_revocation, utc_now
from .errors import MeridianError, require
from .storage import decode


class StoryIdentities:
    def __init__(self, store, observations, sources):
        self.store = store
        self.observations = observations
        self.sources = sources

    # -- 认定 ---------------------------------------------------------
    def determine(self, payload):
        data = story_link(payload)
        with self.store.transaction():
            return self._determine_locked(data)

    def _determine_locked(self, data):
        existing = self.store.one("SELECT * FROM story_links WHERE link_id=?", (data["link_id"],))
        if existing:
            saved = self.describe(data["link_id"])
            same_request = (saved["region"] == data["region"]
                            and saved["determined_by"] == data["determined_by"]
                            and saved["reason"] == data["reason"]
                            and saved["members_requested"] == data["members"])
            require(same_request, "story_link_conflict",
                    "story link identifier was reused with different content")
            require(saved["state"] == "active", "story_link_revoked",
                    "story link was revoked and cannot be replayed")
            return saved | {"duplicate": True}

        snapshot = [self._resolve_member(item, data["region"]) for item in data["members"]]
        source_ids = {member["source_id"] for member in snapshot}
        require(len(source_ids) >= 2, "story_link_same_source",
                "story link needs observations from at least two sources")

        for member in snapshot:
            conflict = self.store.one(
                "SELECT m.link_id FROM story_link_members m JOIN story_links l "
                "ON l.link_id=m.link_id WHERE m.origin=? AND m.external_id=? "
                "AND m.active=1 AND l.state='active'",
                (member["origin"], member["external_id"]))
            require(conflict is None, "story_link_conflict",
                    "observation already belongs to an active story link",
                    origin=member["origin"], external_id=member["external_id"],
                    link_id=conflict["link_id"] if conflict else None)

        now = utc_now()
        with self.store.transaction():
            event_no = self.store.event("story_identity_determined", data["link_id"],
                                        digest({"request": data, "snapshot": snapshot}), now)
            self.store.db.execute(
                "INSERT INTO story_links(link_id,region,determined_by,reason,state,version,"
                "created_at,created_event_no) VALUES(?,?,?,?,'active',1,?,?)",
                (data["link_id"], data["region"], data["determined_by"], data["reason"],
                 now, event_no))
            for member in snapshot:
                self.store.db.execute(
                    "INSERT INTO story_link_members(link_id,origin,external_id,source_id,"
                    "original_story_key,head_revision,active) VALUES(?,?,?,?,?,?,1)",
                    (data["link_id"], member["origin"], member["external_id"],
                     member["source_id"], member["original_story_key"], member["revision"]))
        return self.describe(data["link_id"]) | {"duplicate": False}

    def _resolve_member(self, item, region):
        row = self.store.one(
            "SELECT o.* FROM observations o JOIN observation_heads h "
            "ON h.origin=o.origin AND h.external_id=o.external_id AND h.revision=o.revision "
            "WHERE o.origin=? AND o.external_id=?",
            (item["origin"], item["external_id"]))
        require(row is not None, "observation_missing", "story link member is not found",
                origin=item["origin"], external_id=item["external_id"])
        require(row["status"] == "active", "stale_observation",
                "story link members must be active head revisions",
                origin=item["origin"], external_id=item["external_id"])
        source = self.sources.describe(row["source_id"])
        require(source["region"] == region, "story_link_region_conflict",
                "story link member belongs to another region",
                origin=item["origin"], external_id=item["external_id"],
                region=source["region"])
        return {"origin": item["origin"], "external_id": item["external_id"],
                "source_id": row["source_id"],
                "original_story_key": decode(row["payload"])["story_key"],
                "revision": row["revision"]}

    # -- 撤销 ---------------------------------------------------------
    def revoke(self, link_id, payload):
        data = story_link_revocation(payload)
        with self.store.transaction():
            return self._revoke_locked(link_id, data)

    def _revoke_locked(self, link_id, data):
        row = self.store.one("SELECT * FROM story_links WHERE link_id=?", (link_id,))
        require(row is not None, "story_link_missing", "story link not found")
        if row["state"] == "revoked":
            same = row["revoked_by"] == data["revoked_by"] and row["revoke_reason"] == data["reason"]
            require(same, "story_link_conflict", "story link already has another revocation")
            return self.describe(link_id) | {"duplicate": True}
        now = utc_now()
        with self.store.transaction():
            event_no = self.store.event("story_identity_revoked", link_id,
                                        digest({"link_id": link_id, **data}), now)
            self.store.db.execute(
                "UPDATE story_links SET state='revoked',version=version+1,revoked_by=?,"
                "revoke_reason=?,revoked_at=?,revoked_event_no=? WHERE link_id=?",
                (data["revoked_by"], data["reason"], now, event_no, link_id))
            self.store.db.execute(
                "UPDATE story_link_members SET active=0 WHERE link_id=?", (link_id,))
        return self.describe(link_id) | {"duplicate": False}

    # -- 读取 ---------------------------------------------------------
    def describe(self, link_id):
        row = self.store.one("SELECT * FROM story_links WHERE link_id=?", (link_id,))
        require(row is not None, "story_link_missing", "story link not found")
        members = []
        for item in self.store.all(
                "SELECT * FROM story_link_members WHERE link_id=? ORDER BY origin,external_id",
                (link_id,)):
            members.append(self._describe_member(item))
        return {
            "link_id": row["link_id"], "region": row["region"],
            "determined_by": row["determined_by"], "reason": row["reason"],
            "state": row["state"], "version": row["version"],
            "created_at": row["created_at"], "created_event_no": row["created_event_no"],
            "revoked_by": row["revoked_by"], "revoke_reason": row["revoke_reason"],
            "revoked_at": row["revoked_at"], "revoked_event_no": row["revoked_event_no"],
            "members": members,
            "members_requested": sorted(
                [{"origin": item["origin"], "external_id": item["external_id"]} for item in members],
                key=lambda item: (item["origin"], item["external_id"])),
            "active_member_count": sum(1 for item in members if item["linked"]),
        }

    def _describe_member(self, item):
        head = self.store.one(
            "SELECT o.* FROM observations o JOIN observation_heads h ON h.origin=o.origin "
            "AND h.external_id=o.external_id AND h.revision=o.revision "
            "WHERE o.origin=? AND o.external_id=?", (item["origin"], item["external_id"]))
        source = self.sources.describe(item["source_id"])
        current = {"revision": None, "story_key": None, "status": "missing"}
        if head is not None:
            current = {"revision": head["revision"],
                       "story_key": decode(head["payload"])["story_key"],
                       "status": head["status"]}
        return {"origin": item["origin"], "external_id": item["external_id"],
                "source_id": source["source_id"], "outlet": source["outlet"],
                "region": source["region"],
                "original_story_key": item["original_story_key"],
                "determined_revision": item["head_revision"],
                "current_revision": current["revision"],
                "current_story_key": current["story_key"],
                "observation_status": current["status"],
                "linked": bool(item["active"])}

    def list(self, *, state=None):
        if state is None:
            rows = self.store.all("SELECT link_id FROM story_links ORDER BY link_id")
        else:
            rows = self.store.all("SELECT link_id FROM story_links WHERE state=? ORDER BY link_id",
                                  (state,))
        return [self.describe(row["link_id"]) for row in rows]

    def active_memberships(self):
        """(origin, external_id) -> active link summary，供报告实时解析故事身份。"""
        rows = self.store.all(
            "SELECT m.origin,m.external_id,m.link_id,m.source_id,m.original_story_key,"
            "m.head_revision,l.region,l.determined_by,l.reason,l.version,l.created_at "
            "FROM story_link_members m JOIN story_links l "
            "ON l.link_id=m.link_id WHERE m.active=1 AND l.state='active' "
            "ORDER BY m.link_id,m.origin,m.external_id")
        return {(row["origin"], row["external_id"]): dict(row) for row in rows}

    def membership_for(self, origin, external_id):
        row = self.store.one(
            "SELECT m.link_id FROM story_link_members m JOIN story_links l "
            "ON l.link_id=m.link_id WHERE m.origin=? AND m.external_id=? "
            "AND m.active=1 AND l.state='active'", (origin, external_id))
        return self.describe(row["link_id"]) if row else None

    def history_for(self, origin, external_id):
        """一篇稿件历次被人工并入的故事身份，包括已撤销的认定。"""
        rows = self.store.all(
            "SELECT m.link_id,m.active FROM story_link_members m "
            "WHERE m.origin=? AND m.external_id=? ORDER BY m.link_id",
            (origin, external_id))
        result = []
        for row in rows:
            link = self.describe(row["link_id"])
            link["membership_active"] = bool(row["active"])
            result.append(link)
        return result

    def events(self, link_id):
        self.describe(link_id)
        return [dict(row) for row in self.store.all(
            "SELECT event_no,kind,entity_key,occurred_at FROM events WHERE entity_key=? "
            "ORDER BY event_no", (link_id,))]
