"""Parent material review and the separate learner home.

These routes stay on the reader service. Studio remains a separate origin and
is not linked from the learner page. Import writes require the operator token.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select

from cairn.family import current_profile
from cairn.material_ingest import MaterialError, ingest_documents, parse_manifest, summarize
from cairn.models.collection import CollectionVersion
from cairn.models.learner import Learner
from cairn.models.material import MaterialPage
from cairn.models.question import Question
from cairn.scan_service import db_dir
from cairn.workflow.api import _operator


def register(app, render) -> None:
    @app.get("/learn")
    def learn_home(request: Request):
        profile = current_profile()
        with request.app.state.session_factory() as session:
            learner = session.get(Learner, request.app.state.learner_id)
            verified = session.scalar(
                select(func.count()).select_from(Question).where(
                    Question.learner_id == request.app.state.learner_id,
                    Question.source == "checked-import",
                )
            ) or 0
            review = session.scalar(
                select(func.count()).select_from(Question).where(
                    Question.learner_id == request.app.state.learner_id,
                    Question.source == "needs-review",
                )
            ) or 0
        name = learner.name if learner is not None else "学习者"
        grade = "四年级" if learner is not None and learner.grade_level == "primary-4" else "练习"
        return render(
            request,
            "learn",
            learner_name=name,
            grade_label=grade if profile or (learner and learner.grade_level == "primary-4") else "练习",
            verified_count=int(verified),
            review_count=int(review),
            family=profile,
        )

    @app.get("/materials")
    def materials_page(request: Request):
        profile = current_profile()
        with request.app.state.session_factory() as session:
            report = summarize(session, request.app.state.learner_id)
            pages = list(session.scalars(
                select(MaterialPage).where(MaterialPage.learner_id == request.app.state.learner_id)
                .order_by(MaterialPage.file_id, MaterialPage.page_number).limit(80)
            ))
            rows = [
                {
                    "page_id": page.id,
                    "page_number": page.page_number,
                    "kind": page.kind,
                    "review_needed": page.review_needed,
                    "verified": page.verified,
                    "figure": bool(page.figure_retained),
                    "evidence": page.evidence,
                }
                for page in pages
            ]
        error = request.query_params.get("error") or ""
        return render(
            request,
            "materials",
            report=report,
            rows=rows,
            error=error,
            family=profile,
            nav_active="settings",
        )

    @app.get("/materials/summary")
    def materials_summary(request: Request):
        with request.app.state.session_factory() as session:
            report = summarize(session, request.app.state.learner_id)
            versions = session.scalar(select(func.count()).select_from(CollectionVersion)) or 0
        public = {key: report[key] for key in (
            "files", "missing_files", "rejected_files", "pages", "by_kind",
            "verified_questions", "review_questions", "review_needed_pages", "figures_retained",
            "printed_sources",
        )}
        public["collection_versions"] = int(versions)
        public["published"] = int(versions) > 0
        return JSONResponse(public)

    @app.get("/materials/pages/{page_id}/image")
    def material_image(request: Request, page_id: int):
        with request.app.state.session_factory() as session:
            page = session.get(MaterialPage, page_id)
            if page is None or page.learner_id != request.app.state.learner_id or not page.image_relpath:
                return JSONResponse({"detail": "找不到这一页"}, status_code=404)
            root = db_dir(request.app.state.db_url)
            path = (root / page.image_relpath).resolve()
            if root.resolve() not in path.parents or not path.is_file():
                return JSONResponse({"detail": "页面图片不在私有库里"}, status_code=404)
        return FileResponse(path)

    @app.post("/materials/import")
    async def materials_import(request: Request):
        try:
            _operator(request)
        except Exception as exc:
            status = getattr(exc, "status_code", 401)
            detail = getattr(exc, "detail", "operator 凭据无效")
            return JSONResponse({"detail": detail}, status_code=status)
        form = await request.form()
        upload = form.get("manifest")
        if upload is None or not getattr(upload, "filename", ""):
            return JSONResponse({"detail": "需要清单文件"}, status_code=400)
        try:
            payload = json.loads((await upload.read()).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({"detail": "清单不是 UTF-8 JSON"}, status_code=400)
        blobs: dict[str, bytes] = {}
        for item in form.getlist("files"):
            if not getattr(item, "filename", ""):
                continue
            data = await item.read()
            import hashlib
            blobs[hashlib.sha256(data).hexdigest()] = data
        try:
            documents = parse_manifest(payload, blobs)
        except MaterialError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        with request.app.state.session_factory() as session:
            try:
                report = ingest_documents(session, documents, db_url=request.app.state.db_url)
                session.commit()
            except MaterialError as exc:
                session.rollback()
                return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "pages": report["pages"], "verified_questions": report["verified_questions"],
                             "review_questions": report["review_questions"], "missing_files": report["missing_files"]})
