"""本地证据与冻结报告的 HTTP 接口。"""
import argparse
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from .errors import MeridianError
from .service import MeridianService


class SourceInput(BaseModel):
    source_id: str
    region: str
    outlet: str
    topics: list[str]
    audiences: list[str]


class ObservationInput(BaseModel):
    origin: str
    external_id: str
    source_id: str
    story_key: str
    published_at: str
    received_at: str
    headline: str
    url: str
    expected_revision: int | None = None


class DecisionInput(BaseModel):
    decision_id: str
    origin: str
    external_id: str
    revision: int
    reviewer: str
    topic: str
    stance: str
    audience: str
    reason: str


class AdjudicationInput(BaseModel):
    origin: str
    external_id: str
    revision: int
    adjudicator: str
    topic: str
    stance: str
    audience: str
    reason: str


class ReportInput(BaseModel):
    start: str
    end: str
    regions: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    audiences: list[str] = Field(default_factory=list)


class ReleaseInput(BaseModel):
    release_id: str
    query: ReportInput
    token: str
    minimum_stories: int = 1
    maximum_exclusion_share: float = 0.4


class BatchInput(BaseModel):
    batch_id: str
    rows: list[dict]
    expected_count: int | None = None
    manifest_hash: str | None = None
    limit: int | None = None


class RetractionInput(BaseModel):
    reason: str
    expected_revision: int


class FrameInput(BaseModel):
    frame_id: str
    source_id: str
    start: str
    end: str
    expected_count: int


class WindowComparisonInput(BaseModel):
    left: ReportInput
    right: ReportInput


class RegionComparisonInput(BaseModel):
    query: ReportInput
    left_region: str
    right_region: str


class StoryLinkMemberInput(BaseModel):
    origin: str
    external_id: str


class StoryLinkInput(BaseModel):
    link_id: str
    region: str
    members: list[StoryLinkMemberInput]
    determined_by: str
    reason: str


class StoryLinkRevocationInput(BaseModel):
    revoked_by: str
    reason: str


def create_app(path="meridian.sqlite3"):
    app = FastAPI(title="Meridian evidence analytics", version="1.0")
    service = MeridianService(path)
    app.state.service = service

    @app.exception_handler(MeridianError)
    async def domain_error(_, error):
        from fastapi.responses import JSONResponse
        status = 404 if error.code.endswith("_missing") else 409 if "conflict" in error.code or error.code in {"stale_preview", "stale_observation"} else 422
        return JSONResponse(status_code=status, content=error.as_dict())

    @app.on_event("shutdown")
    def close():
        service.close()

    @app.get("/status")
    def status():
        return service.status()

    @app.post("/sources")
    def register_source(payload: SourceInput):
        return service.sources.register(payload.model_dump())

    @app.get("/sources")
    def list_sources():
        return service.sources.list()

    @app.post("/sources/{source_id}/deactivate")
    def deactivate_source(source_id: str):
        return service.sources.deactivate(source_id)

    @app.post("/observations")
    def ingest(payload: ObservationInput):
        data = payload.model_dump(exclude={"expected_revision"})
        return service.observations.ingest(data, expected_revision=payload.expected_revision)

    @app.get("/observations/{origin}/{external_id}")
    def observation(origin: str, external_id: str):
        return service.observations.describe(origin, external_id)

    @app.get("/observations/{origin}/{external_id}/lineage")
    def lineage(origin: str, external_id: str):
        return service.observations.lineage(origin, external_id)

    @app.get("/observations/{origin}/{external_id}/history")
    def observation_history(origin: str, external_id: str):
        return service.provenance.observation_history(origin, external_id)

    @app.get("/observations/{origin}/{external_id}/coding-lineage")
    def coding_lineage(origin: str, external_id: str):
        return service.provenance.coding_lineage(origin, external_id)

    @app.post("/observations/{origin}/{external_id}/retract")
    def retract(origin: str, external_id: str, payload: RetractionInput):
        return service.observations.retract(origin, external_id, payload.reason,
                                            expected_revision=payload.expected_revision)

    @app.post("/decisions")
    def submit_decision(payload: DecisionInput):
        return service.coding.submit(payload.model_dump())

    @app.get("/decisions/{origin}/{external_id}/{revision}")
    def coding_status(origin: str, external_id: str, revision: int):
        return service.coding.status(origin, external_id, revision)

    @app.post("/adjudications")
    def adjudicate(payload: AdjudicationInput):
        return service.coding.adjudicate(payload.model_dump())

    @app.post("/story-links")
    def determine_story_link(payload: StoryLinkInput):
        return service.story_links.determine(
            {**payload.model_dump(), "members": [item.model_dump() for item in payload.members]})

    @app.get("/story-links")
    def list_story_links(state: str | None = None):
        return service.story_links.list(state=state)

    @app.get("/story-links/revoked")
    def revoked_story_links(region: str | None = None):
        query = {"start": "0001-01-01T00:00:00+00:00", "end": "9999-01-01T00:00:00+00:00",
                 "regions": [region] if region else [], "topics": [], "audiences": []}
        return service.provenance.revoked_links(query)

    @app.get("/story-links/{link_id}")
    def story_link(link_id: str):
        result = service.story_links.describe(link_id)
        result["events"] = service.story_links.events(link_id)
        return result

    @app.post("/story-links/{link_id}/revoke")
    def revoke_story_link(link_id: str, payload: StoryLinkRevocationInput):
        return service.story_links.revoke(link_id, payload.model_dump())

    @app.get("/observations/{origin}/{external_id}/story-links")
    def observation_story_links(origin: str, external_id: str):
        return {"origin": origin, "external_id": external_id,
                "links": service.story_links.history_for(origin, external_id)}

    @app.post("/frames")
    def open_frame(payload: FrameInput):
        return service.frames.open(**payload.model_dump())

    @app.get("/frames/{frame_id}")
    def frame(frame_id: str):
        return service.frames.describe(frame_id)

    @app.post("/frames/{frame_id}/close")
    def close_frame(frame_id: str):
        return service.frames.close(frame_id)

    @app.post("/batches")
    def import_batch(payload: BatchInput):
        service.batches.begin(payload.batch_id, payload.rows, expected_count=payload.expected_count,
                              manifest_hash=payload.manifest_hash)
        return service.batches.step(payload.batch_id, payload.rows, limit=payload.limit)

    @app.get("/batches/{batch_id}")
    def batch_status(batch_id: str):
        return service.batches.status(batch_id)

    @app.post("/reports")
    def report(payload: ReportInput):
        return service.analytics.report(payload.model_dump())

    @app.post("/reports/preview")
    def preview(payload: ReportInput, minimum_stories: int = 1, maximum_exclusion_share: float = 0.4):
        return service.releases.preview(payload.model_dump(), minimum_stories=minimum_stories,
                                        maximum_exclusion_share=maximum_exclusion_share)

    @app.post("/reports/readiness")
    def readiness(payload: ReportInput):
        return service.diagnostics.readiness(payload.model_dump())

    @app.post("/reports/review-queue")
    def review_queue(payload: ReportInput):
        return service.diagnostics.queue(payload.model_dump())

    @app.post("/reports/story-conflicts")
    def story_conflicts(payload: ReportInput):
        return service.diagnostics.story_conflicts(payload.model_dump())

    @app.post("/reports/identity-conflicts")
    def identity_conflicts(payload: ReportInput):
        return service.diagnostics.identity_conflicts(payload.model_dump())



    @app.post("/reports/story")
    def story_detail(payload: ReportInput, region: str, story_key: str, link_id: str | None = None):
        return service.provenance.story_detail(payload.model_dump(), region, story_key,
                                               link_id=link_id)

    @app.post("/reports/topic-matrix")
    def topic_matrix(payload: ReportInput):
        return service.comparisons.topic_matrix(payload.model_dump())

    @app.post("/reports/export")
    def export_report(payload: ReportInput):
        return service.exports.current(payload.model_dump())

    @app.post("/reports/source-breakdown")
    def source_breakdown(payload: ReportInput):
        return service.exports.source_breakdown(payload.model_dump())

    @app.post("/reports/compare-windows")
    def compare_windows(payload: WindowComparisonInput):
        return service.comparisons.windows(payload.left.model_dump(), payload.right.model_dump())

    @app.post("/reports/compare-regions")
    def compare_regions(payload: RegionComparisonInput):
        return service.comparisons.regions(payload.query.model_dump(), payload.left_region, payload.right_region)

    @app.post("/releases")
    def publish(payload: ReleaseInput):
        return service.releases.publish(payload.release_id, payload.query.model_dump(), payload.token,
                                        minimum_stories=payload.minimum_stories,
                                        maximum_exclusion_share=payload.maximum_exclusion_share)

    @app.get("/releases/{release_id}")
    def release(release_id: str):
        return service.releases.describe(release_id)

    @app.get("/releases/{release_id}/compare")
    def compare(release_id: str):
        return service.releases.compare(release_id)

    @app.get("/releases/{release_id}/changes")
    def release_changes(release_id: str):
        return service.provenance.release_changes(release_id)

    @app.get("/releases/{release_id}/export")
    def export_release(release_id: str):
        return service.exports.frozen(release_id)

    @app.get("/audit")
    def audit():
        return service.audit.summary()

    @app.get("/events")
    def events(after: int = 0, limit: int = 100):
        return service.audit.events(after, limit)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="meridian.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

