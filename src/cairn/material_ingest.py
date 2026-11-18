"""Batch intake for local PDFs and images into Cairn questions.

Source files stay in the operator's private directory. Automated checks never
invent an answer: a question is verified only when ``material_check`` can
recompute a printed answer. Every other readable page stays review-needed.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.family import current_profile, seed_runtime
from cairn.material_check import (
    checked_answer,
    faithful_analysis,
    parse_exercises,
    stem_only,
)
from cairn.models.learner import Learner, utcnow
from cairn.models.material import MaterialFile, MaterialLink, MaterialPage
from cairn.models.question import Question
from cairn.models.scan import Scan, ScanQuestion
from cairn.scan_service import db_dir

_FILE_KEYS = {"filename", "label", "role", "bytes", "sha256", "pages", "group", "synthetic"}
_TOP_KEYS = {"purpose", "files", "boundaries", "synthetic", "notice"}
_ROLES = {"questions", "source", "source_photo", "source_with_answers", "answer_companion"}
_PRIMARY_ROLES = {"questions", "source", "source_photo", "source_with_answers"}
_KINDS = {"question", "answer", "cover", "blank", "unreadable"}
_PUBLISHERS = ("爱学习", "学而思", "高思")


class MaterialError(ValueError):
    """The manifest or a file was rejected before any learning row was trusted."""


@dataclass
class OcrLine:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int] | None = None


@dataclass
class OcrResult:
    lines: list[OcrLine] = field(default_factory=list)
    status: str = "ok"

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines if line.text)

    @property
    def confidence(self) -> float | None:
        if not self.lines:
            return None
        return sum(line.confidence for line in self.lines) / len(self.lines)


@dataclass
class _Blob:
    filename: str
    label: str
    role: str
    sha256: str
    byte_size: int
    manifest_pages: int | None
    group_key: str | None
    synthetic: bool
    data: bytes | None
    evidence: str = ""
    status: str = "stored"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _group_from_label(label: str) -> str | None:
    for marker in ("题目版", "答案解析版", "答案版"):
        if marker in label:
            return label.split(marker)[0].strip(" ，,")
    match = re.search(r"第\s*(\d+)\s*题", label)
    if match and any(token in label for token in ("同题", "照片", "讲义", "Lesson", "Practice")):
        return f"lesson-item-{match.group(1)}"
    return None


def parse_manifest(payload: object, blobs: dict[str, bytes]) -> list[_Blob]:
    """Validate a manifest and match files by SHA256, never by a joined path."""
    if not isinstance(payload, dict) or set(payload) - _TOP_KEYS:
        raise MaterialError("清单含不支持的字段，或不是对象")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise MaterialError("清单的 files 必须是非空列表")
    synthetic_all = payload.get("synthetic") is True
    documents: list[_Blob] = []
    seen: set[str] = set()
    for index, item in enumerate(files, 1):
        if not isinstance(item, dict) or set(item) - _FILE_KEYS:
            raise MaterialError(f"第 {index} 个文件含不支持的字段")
        for key in ("filename", "label", "role", "bytes", "sha256"):
            if key not in item:
                raise MaterialError(f"第 {index} 个文件缺少 {key}")
        filename = item["filename"]
        if not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise MaterialError(f"第 {index} 个文件名不能当路径使用")
        label = item["label"]
        role = item["role"]
        digest = item["sha256"]
        size = item["bytes"]
        if not isinstance(label, str) or not label.strip() or len(label) > 300:
            raise MaterialError(f"第 {index} 个文件的 label 无效")
        if role not in _ROLES:
            raise MaterialError(f"第 {index} 个文件的 role 无效")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MaterialError(f"第 {index} 个文件的 sha256 无效")
        if type(size) is not int or size < 0:
            raise MaterialError(f"第 {index} 个文件的 bytes 无效")
        pages = item.get("pages")
        if pages is not None and (type(pages) is not int or pages < 1 or pages > 500):
            raise MaterialError(f"第 {index} 个文件的 pages 无效")
        group = item.get("group")
        if group is not None and (not isinstance(group, str) or not group.strip() or len(group) > 160):
            raise MaterialError(f"第 {index} 个文件的 group 无效")
        if digest in seen:
            raise MaterialError(f"清单里 SHA256 重复：{digest[:12]}")
        seen.add(digest)
        data = blobs.get(digest)
        status = "stored"
        evidence = ""
        if data is None:
            status = "missing"
            evidence = "清单中的 SHA256 在提供的文件里没有对应字节"
        elif len(data) != size or _sha256(data) != digest:
            status = "rejected"
            evidence = "字节长度或 SHA256 与清单不一致，未入库"
            data = None
        documents.append(
            _Blob(
                filename=filename,
                label=label.strip(),
                role=role,
                sha256=digest,
                byte_size=size if data is None else len(data),
                manifest_pages=pages,
                group_key=(group.strip() if isinstance(group, str) else None) or _group_from_label(label),
                synthetic=synthetic_all or item.get("synthetic") is True,
                data=data,
                evidence=evidence,
                status=status,
            )
        )
    return documents


def load_manifest_directory(manifest_path: Path, roots: list[Path]) -> tuple[dict, list[_Blob]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    blobs: dict[str, bytes] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
                continue
            data = path.read_bytes()
            blobs.setdefault(_sha256(data), data)
    return payload, parse_manifest(payload, blobs)


def _local_ocr(path: Path) -> OcrResult:
    if os.environ.get("CAIRN_OCR_DISABLE", "").strip() == "1":
        return OcrResult(status="not_run")
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return OcrResult(status="unavailable")
    engine = _local_ocr.engine
    if engine is None:
        engine = RapidOCR()
        _local_ocr.engine = engine
    try:
        result, _elapsed = engine(str(path))
    except Exception:
        return OcrResult(status="unavailable")
    lines: list[OcrLine] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        box, text, score = item[0], str(item[1] or ""), float(item[2] or 0)
        bbox = None
        try:
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            bbox = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
        except (TypeError, ValueError, IndexError):
            bbox = None
        if text.strip():
            lines.append(OcrLine(text=text.strip(), confidence=score, bbox=bbox))
    return OcrResult(lines=lines, status="empty" if not lines else "ok")


_local_ocr.engine = None


def _ink_ratio(image) -> float:
    gray = image.convert("L")
    hist = gray.histogram()
    dark = sum(hist[:245])
    total = max(1, gray.size[0] * gray.size[1])
    return dark / total


def _render_pdf(data: bytes, dest_dir: Path) -> list[tuple[Path, float]]:
    import pypdfium2 as pdfium
    from PIL import Image

    dest_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(io.BytesIO(data))
    rendered: list[tuple[Path, float]] = []
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            width, height = page.get_size()
            short = max(1.0, min(width, height))
            scale = min(4.0, max(0.8, 1800.0 / short))
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            if image.mode != "RGB":
                image = image.convert("RGB")
            target = dest_dir / f"p{index + 1:04d}.png"
            image.save(target, format="PNG")
            rendered.append((target, _ink_ratio(image)))
            bitmap.close()
            page.close()
    finally:
        pdf.close()
    return rendered


def _render_image(data: bytes, dest: Path) -> tuple[Path, float]:
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(io.BytesIO(data))
    image = image.convert("RGB")
    image.save(dest, format="PNG")
    return dest, _ink_ratio(image)


def _is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF")


def _exercise_marker(text: str) -> bool:
    return bool(re.search(r"练\s*\d|Practice\s*\d|计算", text or "", flags=re.I))


def classify_page(text: str, confidence: float | None, ink: float | None, role: str, page_number: int) -> tuple[str, str]:
    compact = re.sub(r"\s+", "", text or "")
    if ink is not None and ink < 0.008 and len(compact) < 12:
        return "blank", "墨迹很少，而且几乎没有读出文字"
    if not compact:
        if ink is not None and ink >= 0.02:
            return "unreadable", "页面有内容，但本地识别没有读出文字"
        return "blank", "没有读出文字，页面也接近空白"
    if role == "answer_companion" or ("答案" in text and "解析" in text and role == "source_with_answers"):
        if role == "answer_companion":
            return "answer", "这份文件在清单里是答案伴页"
        return "question", "同一页印有题目和答案；答案未核对前不写入题库"
    if confidence is not None and confidence < 0.45 and not _exercise_marker(text):
        return "unreadable", f"本地识别平均置信度 {confidence:.2f}，不足以当作题面"
    if page_number == 1 and not _exercise_marker(text) and len(compact) < 80:
        return "cover", "首页可读，但没有独立作答项"
    if _exercise_marker(text) or re.search(r"(?m)^\s*\d+\s*[.．、]", text or ""):
        return "question", "读到作答项；文字在核对前都视为未复核"
    if len(compact) < 20:
        return "unreadable", "读出的文字太短，不能判断是题目、封面还是空白"
    if page_number == 1:
        return "cover", "首页没有独立作答项"
    return "question", "整页保留。没有稳定题号，文字未复核"


def _printed_source(text: str) -> str | None:
    for name in _PUBLISHERS:
        if name in text:
            return name
    return None


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _ensure_learner(session: Session) -> Learner:
    learner = session.scalars(select(Learner).order_by(Learner.id.asc()).limit(1)).first()
    if learner is None:
        from cairn.family import new_learner_kwargs

        learner = Learner(**new_learner_kwargs())
        session.add(learner)
        session.flush()
    return learner


def _crop_box(image_path: Path, lines: list[OcrLine], body: str, full: str) -> tuple[int, int, int, int] | None:
    if not lines:
        return None
    snippet = stem_only(body)[:24]
    hits = [line for line in lines if line.bbox and snippet and snippet[:8] in line.text]
    if not hits:
        return None
    top = min(line.bbox[1] for line in hits if line.bbox)
    bottom_candidates = [line.bbox[3] for line in lines if line.bbox and line.bbox[1] >= top]
    if not bottom_candidates:
        return None
    from PIL import Image

    with Image.open(image_path) as image:
        width, height = image.size
    top = max(0, top - 24)
    bottom = min(height, max(bottom_candidates) + 24)
    if bottom - top < 40:
        return None
    return (0, top, width, bottom)


def _write_crop(image_path: Path, box: tuple[int, int, int, int], dest: Path) -> str:
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as image:
        image.convert("RGB").crop(box).save(dest, format="PNG")
    return str(dest)


def _unique_figure(image_path: Path, question_id: int) -> Path:
    dest = image_path.with_name(f"{image_path.stem}-q{question_id}{image_path.suffix}")
    if dest.exists():
        return dest
    try:
        os.link(image_path, dest)
    except OSError:
        shutil.copyfile(image_path, dest)
    return dest


def _attach_scan(
    session: Session,
    learner_id: int,
    question: Question,
    image_path: Path,
    root: Path,
) -> None:
    from PIL import Image

    figure = _unique_figure(image_path, question.id)
    with Image.open(figure) as image:
        width, height = image.size
    relative = _rel(figure, root)
    scan = session.scalar(select(Scan).where(Scan.path == relative))
    if scan is None:
        scan = Scan(
        learner_id=learner_id,
        device_id="material-import",
        device_name="材料导入",
        width=width,
        height=height,
        captured_at=utcnow(),
        path=relative,
        note="material-import",
        source="upload",
        )
        session.add(scan)
        session.flush()
    next_no = (session.scalar(select(func.max(ScanQuestion.no)).where(ScanQuestion.scan_id == scan.id)) or 0) + 1
    session.add(
        ScanQuestion(
            scan_id=scan.id,
            no=int(next_no),
            bbox_json=json.dumps({"bbox": [0, 0, width, height], "page": [width, height], "angle": 0}),
            stem_text="",
            is_wrong=True,
            crop_path=relative,
            question_id=question.id,
        )
    )


def _add_question(
    session: Session,
    learner: Learner,
    *,
    content: str,
    answer: str | None,
    analysis: str | None,
    verified: bool,
    synthetic: bool,
    image_path: Path | None,
    root: Path,
) -> Question:
    profile = current_profile()
    grade = profile.grade_level if profile is not None else learner.grade_level
    prefix = ""
    if synthetic:
        prefix = "【合成夹具，不是 Jojo 的真实材料】\n"
    elif not verified:
        prefix = "【未复核】\n"
    question = Question(
        learner_id=learner.id,
        subject="数学",
        grade_level=grade,
        question_type="fill",
        content=(prefix + content.strip())[:8000],
        answer=answer if verified else None,
        analysis=analysis if verified else None,
        source="checked-import" if verified else "needs-review",
    )
    session.add(question)
    session.flush()
    if image_path is not None and image_path.is_file():
        _attach_scan(session, learner.id, question, image_path, root)
    return question


def _page_questions(
    session: Session,
    learner: Learner,
    page: MaterialPage,
    companion_text: str | None,
    lines: list[OcrLine],
    root: Path,
    synthetic: bool,
) -> list[Question]:
    if page.kind != "question":
        return []
    exercises = parse_exercises(page.ocr_text)
    if not exercises:
        return []
    companion_items = parse_exercises(companion_text or "")
    companion_blocks = {item["key"]: item["body"] for item in companion_items}
    image_path = root / page.image_relpath if page.image_relpath else None
    created: list[Question] = []
    for offset, exercise in enumerate(exercises, 1):
        body = exercise["body"]
        solution = companion_blocks.get(exercise["key"]) if exercise["key"] else None
        if solution is None and offset - 1 < len(companion_items):
            solution = companion_items[offset - 1]["body"]
        if solution is None:
            solution = companion_text
        answer = checked_answer(body, solution)
        verified = answer is not None
        content = stem_only(body)
        if not verified and not re.search(r"计算|[=＝]|\d\s*[+*]|[\^²]", content):
            continue
        if len(re.sub(r"\s+", "", content)) < 8 and not verified:
            continue
        analysis = faithful_analysis(solution, answer) if verified and answer else None
        crop_path = image_path
        if image_path is not None and len(exercises) > 1:
            box = _crop_box(image_path, lines, body, page.ocr_text)
            if box is None:
                from PIL import Image

                with Image.open(image_path) as image:
                    width, height = image.size
                slice_height = max(1, height // len(exercises))
                top = (offset - 1) * slice_height
                bottom = height if offset == len(exercises) else offset * slice_height
                box = (0, top, width, bottom)
            dest = image_path.with_name(f"{image_path.stem}-{exercise['key'] or offset}.png")
            _write_crop(image_path, box, dest)
            crop_path = dest
        question = _add_question(
            session,
            learner,
            content=content or "见页面原图。",
            answer=answer,
            analysis=analysis,
            verified=verified,
            synthetic=synthetic,
            image_path=crop_path,
            root=root,
        )
        if verified:
            page.verified = True
            page.review_needed = False
        session.add(MaterialLink(page_id=page.id, question_id=question.id, exercise_key=exercise["key"], role="stem"))
        created.append(question)
        if page.question_id is None:
            page.question_id = question.id
    if created and any(question.answer is None for question in created):
        page.review_needed = True
    if created and all(question.answer is not None for question in created):
        page.review_needed = False
        page.verified = True
    return created


def _publish_checked(session: Session, learner_id: int, question_ids: list[int]) -> dict:
    """Publish one immutable collection version and pin a tutor-free practice flow."""
    from cairn.collection_service import create, publish, replace_items
    from cairn.models.collection import Collection
    from cairn.workflow.collection_runner import start_active_collection_workflow
    from cairn.workflow.service import publish as publish_workflow
    from cairn.models.workflow import Workflow

    if not question_ids:
        return {"published": False, "practice_new": 0, "practice_existing": 0}
    from cairn.models.collection import CollectionVersion

    seed_runtime(session)
    name = "已核对练习"
    collection = session.scalar(select(Collection).where(Collection.learner_id == learner_id, Collection.name == name))
    previous_version = None
    if collection is not None:
        previous_version = session.scalar(select(CollectionVersion).where(
            CollectionVersion.collection_id == collection.id,
            CollectionVersion.revoked_at.is_(None),
            CollectionVersion.snapshot_json.is_not(None),
        ).order_by(CollectionVersion.number.desc()))
        if previous_version is not None:
            try:
                previous_ids = json.loads(previous_version.snapshot_json).get("question_ids") or []
            except (TypeError, json.JSONDecodeError):
                previous_ids = []
            if previous_ids == question_ids:
                return {"published": True, "already": True, "version_id": previous_version.id,
                        "version_number": previous_version.number,
                        "practice_new": 0, "practice_existing": 0, "item_count": len(question_ids)}
    if collection is None:
        collection = create(
            session,
            learner_id,
            name=name,
            subject="数学",
            tags=["已核对"],
            license="private",
            author="家庭",
            source="本地材料中已通过算式核对的题，不含未复核 OCR",
        )
    items = [
        {"question_id": qid, "tags": ["已核对"], "difficulty": None, "source": "checked-import"}
        for qid in question_ids
    ]
    replace_items(session, learner_id, collection.id, collection.revision, items)
    session.refresh(collection)
    version = publish(session, learner_id, collection.id, collection.revision)
    session.refresh(collection)
    if previous_version is not None:
        # A practice copy can belong to only one collection version. Keep the
        # existing copies and queue only questions that are not already there.
        added = _queue_new_checked(session, learner_id, question_ids)
        return {"published": True, "version_id": version.id, "version_number": version.number,
                "practice_new": added, "practice_existing": len(question_ids) - added,
                "item_count": len(question_ids), "kept_previous_practice": True}
    workflow = session.get(Workflow, "collection")
    graph = {
        "nodes": [
            {"id": "collection-source-1", "node_id": "collection_source", "params": {"version_id": version.id},
             "position": {"x": 80, "y": 90}},
            {"id": "strategy-1", "node_id": "strategy", "params": {"strategy": "fsrs"},
             "position": {"x": 360, "y": 90}},
        ],
        "edges": [{"source": "collection-source-1", "target": "strategy-1"}],
    }
    workflow.draft_json = json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
    workflow.revision += 1
    workflow.title = "题集练习流程"
    session.flush()
    publish_workflow(session, workflow, published_by="operator", learner_id=learner_id)
    new_count, old_count = start_active_collection_workflow(session, learner_id)
    _mirror_figures(session, version.id)
    return {"published": True, "version_id": version.id, "version_number": version.number,
            "practice_new": new_count, "practice_existing": old_count, "item_count": len(question_ids)}


def _queue_new_checked(session: Session, learner_id: int, question_ids: list[int]) -> int:
    """Put newly checked questions on the practice queue without copying old ones."""
    from cairn.models.collection import CollectionPracticeItem
    from cairn.models.error_item import ErrorItem

    already = set(session.scalars(select(CollectionPracticeItem.source_question_id)))
    already.update(session.scalars(select(ErrorItem.question_id).where(ErrorItem.learner_id == learner_id)))
    fresh = [qid for qid in question_ids if qid not in already]
    session.add_all([
        ErrorItem(learner_id=learner_id, question_id=qid, wrong_count=0, confidence=1.0)
        for qid in fresh
    ])
    session.flush()
    return len(fresh)


def _mirror_figures(session: Session, version_id: int) -> None:
    from cairn.models.collection import CollectionPracticeItem

    mappings = list(session.scalars(select(CollectionPracticeItem).where(CollectionPracticeItem.version_id == version_id)))
    for mapping in mappings:
        source = session.scalar(
            select(ScanQuestion).where(ScanQuestion.question_id == mapping.source_question_id, ScanQuestion.crop_path.is_not(None))
        )
        if source is None:
            continue
        already = session.scalar(select(ScanQuestion.id).where(ScanQuestion.question_id == mapping.question_id))
        if already is not None:
            continue
        top_no = session.scalar(select(func.max(ScanQuestion.no)).where(ScanQuestion.scan_id == source.scan_id)) or 0
        session.add(
            ScanQuestion(
                scan_id=source.scan_id,
                no=int(top_no) + 1,
                bbox_json=source.bbox_json,
                stem_text="",
                is_wrong=True,
                crop_path=source.crop_path,
                question_id=mapping.question_id,
            )
        )
    session.flush()


def _save_review_collection(session: Session, learner_id: int, question_ids: list[int]) -> None:
    from cairn.collection_service import create, replace_items
    from cairn.models.collection import Collection

    if not question_ids:
        return
    name = "待复核原页"
    collection = session.scalar(select(Collection).where(Collection.learner_id == learner_id, Collection.name == name))
    if collection is None:
        collection = create(
            session,
            learner_id,
            name=name,
            subject="数学",
            tags=["待复核"],
            license="private",
            author="家庭",
            source="本地识别尚未核对的页面，不进入今天的练习",
        )
    items = [
        {"question_id": qid, "tags": ["待复核"], "difficulty": None, "source": "needs-review"}
        for qid in question_ids
    ]
    replace_items(session, learner_id, collection.id, collection.revision, items)


def ingest_documents(
    session: Session,
    documents: list[_Blob],
    *,
    db_url: str,
    ocr=None,
    publish: bool = True,
) -> dict:
    """Store files, classify every page, and publish only checked questions."""
    learner = _ensure_learner(session)
    root = db_dir(db_url)
    reader = ocr or _local_ocr
    existing = {
        row.sha256: row
        for row in session.scalars(select(MaterialFile).where(MaterialFile.learner_id == learner.id))
    }
    file_rows: list[MaterialFile] = []
    for document in documents:
        prior = existing.get(document.sha256)
        if prior is not None and not (prior.status == "missing" and document.data is not None and document.status == "stored"):
            file_rows.append(prior)
            continue
        stored_path = None
        if document.data is not None and document.status == "stored":
            suffix = ".pdf" if _is_pdf(document.data) else ".img"
            target = root / "originals" / f"{document.sha256}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(document.data)
            stored_path = _rel(target, root)
        if prior is None:
            row = MaterialFile(
                learner_id=learner.id,
                sha256=document.sha256,
                byte_size=document.byte_size,
                storage_name=document.filename,
                inventory_label=document.label,
                role=document.role,
                group_key=document.group_key,
                media="pdf" if document.data and _is_pdf(document.data) else ("missing" if document.data is None else "image"),
                synthetic=document.synthetic,
                status=document.status,
                manifest_pages=document.manifest_pages,
                evidence=document.evidence,
                stored_path=stored_path,
            )
            session.add(row)
            session.flush()
            existing[document.sha256] = row
        else:
            row = prior
            row.byte_size = document.byte_size
            row.storage_name = document.filename
            row.inventory_label = document.label
            row.role = document.role
            row.group_key = document.group_key or row.group_key
            row.media = "pdf" if document.data and _is_pdf(document.data) else "image"
            row.synthetic = document.synthetic
            row.status = "stored"
            row.manifest_pages = document.manifest_pages
            row.evidence = ""
            row.stored_path = stored_path
        file_rows.append(row)
        if document.status != "stored" or document.data is None:
            continue
        page_images: list[tuple[Path, float]] = []
        page_dir = root / "scans" / "materials" / document.sha256[:16]
        try:
            if _is_pdf(document.data):
                page_images = _render_pdf(document.data, page_dir)
            else:
                page_images = [_render_image(document.data, page_dir / "p0001.png")]
        except Exception as exc:
            row.status = "rejected"
            row.evidence = f"页面渲染失败（{type(exc).__name__}），原文件已保留，没有生成题目"
            continue
        row.actual_pages = len(page_images)
        if document.manifest_pages is not None and document.manifest_pages != len(page_images):
            row.evidence = f"清单页数 {document.manifest_pages} 与实际 {len(page_images)} 不一致"
        printed_hits: list[str] = []
        for number, (image_path, ink) in enumerate(page_images, 1):
            result = reader(image_path)
            printed = _printed_source(result.text)
            if printed:
                printed_hits.append(printed)
            kind, evidence = classify_page(result.text, result.confidence, ink, document.role, number)
            if kind not in _KINDS:
                kind = "unreadable"
            session.add(
                MaterialPage(
                    file_id=row.id,
                    learner_id=learner.id,
                    page_number=number,
                    kind=kind,
                    evidence=evidence,
                    ocr_text=result.text[:20000],
                    ocr_confidence=result.confidence,
                    ocr_status=result.status,
                    review_needed=True,
                    verified=False,
                    image_relpath=_rel(image_path, root),
                    figure_retained=image_path.is_file() and image_path.stat().st_size > 0,
                )
            )
        if printed_hits:
            # Keep a printed name only when that exact string was read. Never substitute 高思.
            row.printed_source = printed_hits[0]
        elif "出版社未知" in document.label or "未标明" in document.label:
            row.evidence = (row.evidence + "；" if row.evidence else "") + "清单写明出版社未知，页面文字也未读到出版社名"
    session.flush()
    _build_questions(session, learner, root)
    verified_ids = list(session.scalars(
        select(Question.id).where(Question.learner_id == learner.id, Question.source == "checked-import").order_by(Question.id)
    ))
    review_ids = list(session.scalars(
        select(Question.id).where(Question.learner_id == learner.id, Question.source == "needs-review").order_by(Question.id)
    ))
    publication = {"published": False}
    if publish and verified_ids:
        publication = _publish_checked(session, learner.id, verified_ids)
    if review_ids:
        _save_review_collection(session, learner.id, review_ids)
    session.flush()
    return summarize(session, learner.id, publication)


def _build_questions(session: Session, learner: Learner, root: Path) -> None:
    pages = list(session.scalars(select(MaterialPage).where(MaterialPage.learner_id == learner.id).order_by(MaterialPage.id)))
    files = {row.id: row for row in session.scalars(select(MaterialFile).where(MaterialFile.learner_id == learner.id))}
    by_group: dict[str, list[MaterialFile]] = {}
    for row in files.values():
        if row.group_key:
            by_group.setdefault(row.group_key, []).append(row)
    companion_ids: set[int] = set()
    solution_text: dict[int, str] = {}
    for group_files in by_group.values():
        primaries = [row for row in group_files if row.role in _PRIMARY_ROLES and row.status == "stored"]
        companions = [row for row in group_files if row.role == "answer_companion" and row.status == "stored"]
        if not primaries:
            continue
        primary_pages = [page for page in pages if page.file_id in {row.id for row in primaries}]
        companion_pages = [page for page in pages if page.file_id in {row.id for row in companions}]
        companion_ids.update(page.id for page in companion_pages)
        by_number = {page.page_number: page for page in companion_pages}
        for page in primary_pages:
            match = by_number.get(page.page_number)
            if match is None and len(companion_pages) == 1 and len(primary_pages) == 1:
                match = companion_pages[0]
            if match is not None:
                page.companion_page_id = match.id
                match.companion_page_id = page.id
                match.kind = "answer"
                match.evidence = "与题目页是同一组材料，不另建一道题"
                solution_text[page.id] = match.ocr_text
    made: set[int] = set()
    for page in pages:
        if page.id in companion_ids or page.id in made:
            continue
        if page.question_id is not None or page.kind != "question":
            continue
        file_row = files[page.file_id]
        image = root / page.image_relpath if page.image_relpath else None
        lines: list[OcrLine] = []
        if image is not None and image.is_file() and page.ocr_status == "ok":
            # Boxes are optional; the full page image remains the figure.
            lines = []
        questions = _page_questions(
            session,
            learner,
            page,
            solution_text.get(page.id),
            lines,
            root,
            file_row.synthetic,
        )
        if questions:
            made.add(page.id)


def summarize(session: Session, learner_id: int, publication: dict | None = None) -> dict:
    files = list(session.scalars(select(MaterialFile).where(MaterialFile.learner_id == learner_id)))
    pages = list(session.scalars(select(MaterialPage).where(MaterialPage.learner_id == learner_id)))
    kinds: dict[str, int] = {kind: 0 for kind in sorted(_KINDS)}
    for page in pages:
        kinds[page.kind] = kinds.get(page.kind, 0) + 1
    verified = session.scalar(
        select(func.count()).select_from(Question).where(Question.learner_id == learner_id, Question.source == "checked-import")
    ) or 0
    review = session.scalar(
        select(func.count()).select_from(Question).where(Question.learner_id == learner_id, Question.source == "needs-review")
    ) or 0
    return {
        "files": len(files),
        "missing_files": sum(1 for row in files if row.status == "missing"),
        "rejected_files": sum(1 for row in files if row.status == "rejected"),
        "pages": len(pages),
        "by_kind": kinds,
        "verified_questions": int(verified),
        "review_questions": int(review),
        "review_needed_pages": sum(1 for page in pages if page.review_needed),
        "figures_retained": sum(1 for page in pages if page.figure_retained),
        "printed_sources": sorted({row.printed_source for row in files if row.printed_source}),
        "publication": publication or {"published": False},
        "files_detail": [
            {
                "label": row.inventory_label,
                "role": row.role,
                "status": row.status,
                "synthetic": row.synthetic,
                "manifest_pages": row.manifest_pages,
                "actual_pages": row.actual_pages,
                "printed_source": row.printed_source,
                "evidence": row.evidence,
            }
            for row in files
        ],
    }


def apply_visual_reviews(session: Session, reviews: list[dict], *, db_url: str) -> dict:
    """Record a human or visual check. This does not treat OCR text as verified."""
    from sqlalchemy import delete

    from cairn.models.collection import CollectionItem
    from cairn.models.error_item import ErrorItem

    learner = _ensure_learner(session)
    root = db_dir(db_url)
    updated = 0
    created = 0
    for review in reviews:
        if not isinstance(review, dict):
            raise MaterialError("核对记录必须是对象")
        digest = review.get("sha256")
        page_number = review.get("page_number")
        if not isinstance(digest, str) or type(page_number) is not int:
            raise MaterialError("核对记录缺少 sha256 或页码")
        file_row = session.scalar(select(MaterialFile).where(
            MaterialFile.learner_id == learner.id, MaterialFile.sha256 == digest
        ))
        page = None if file_row is None else session.scalar(select(MaterialPage).where(
            MaterialPage.file_id == file_row.id, MaterialPage.page_number == page_number
        ))
        if file_row is None or page is None:
            raise MaterialError("核对记录没有对应的已导入页面")
        if review.get("kind") in _KINDS:
            page.kind = review["kind"]
        if review.get("evidence"):
            page.evidence = str(review["evidence"])[:500]
        if page.companion_page_id and not (isinstance(review.get("content"), str) and review.get("content").strip()):
            companion = session.get(MaterialPage, page.companion_page_id)
            if companion is not None and companion.verified:
                page.review_needed = False
        if review.get("drop_unreviewed"):
            _drop_unreviewed_page(session, page)
            page.review_needed = False
        content = review.get("content")
        answer = review.get("answer")
        if not isinstance(content, str) or not content.strip() or not isinstance(answer, str) or not answer.strip():
            continue
        key = str(review.get("exercise_key") or "")
        question = _question_for_review(session, page, key)
        analysis = review.get("analysis")
        analysis = analysis.strip() if isinstance(analysis, str) and analysis.strip() else None
        prefix = "【合成夹具，不是 Jojo 的真实材料】\n" if file_row.synthetic else ""
        if question is None:
            question = _add_question(
                session, learner, content=content.strip(), answer=answer.strip(), analysis=analysis,
                verified=True, synthetic=file_row.synthetic, image_path=_page_image(root, page), root=root,
            )
            session.add(MaterialLink(page_id=page.id, question_id=question.id, exercise_key=key, role="stem"))
            created += 1
        else:
            question.content = (prefix + content.strip())[:8000]
            question.answer = answer.strip()
            question.analysis = analysis
            question.source = "checked-import"
            figure = _page_image(root, page)
            if figure is not None:
                _retarget_figure(session, learner.id, question, figure, root)
            updated += 1
        if page.question_id is None:
            page.question_id = question.id
        page.verified = True
        page.review_needed = False
        if page.companion_page_id:
            companion = session.get(MaterialPage, page.companion_page_id)
            if companion is not None and companion.kind == "answer":
                companion.review_needed = False
        if not page.evidence:
            page.evidence = "已对照原页核对，不是未复核的识别文本"
    for answer_page in session.scalars(select(MaterialPage).where(
        MaterialPage.learner_id == learner.id, MaterialPage.kind == "answer", MaterialPage.review_needed.is_(True)
    )):
        if not answer_page.companion_page_id:
            continue
        companion = session.get(MaterialPage, answer_page.companion_page_id)
        if companion is not None and companion.verified:
            answer_page.review_needed = False
    verified_ids = list(session.scalars(
        select(Question.id).where(Question.learner_id == learner.id, Question.source == "checked-import").order_by(Question.id)
    ))
    review_ids = list(session.scalars(
        select(Question.id).where(Question.learner_id == learner.id, Question.source == "needs-review").order_by(Question.id)
    ))
    publication = _publish_checked(session, learner.id, verified_ids) if verified_ids else {"published": False}
    if review_ids:
        _save_review_collection(session, learner.id, review_ids)
    session.flush()
    return {"updated": updated, "created": created, "verified_questions": len(verified_ids), "publication": publication}


def _page_image(root: Path, page: MaterialPage) -> Path | None:
    if not page.image_relpath:
        return None
    path = root / page.image_relpath
    return path if path.is_file() else None


def _question_for_review(session: Session, page: MaterialPage, key: str) -> Question | None:
    page_ids = [page.id]
    if page.companion_page_id:
        page_ids.append(page.companion_page_id)
    links = list(session.scalars(select(MaterialLink).where(MaterialLink.page_id.in_(page_ids))))
    for link in links:
        if key and link.exercise_key == key:
            question = session.get(Question, link.question_id)
            if question is not None and question.source in {"needs-review", "checked-import"}:
                return question
    for link in links:
        question = session.get(Question, link.question_id)
        if question is not None and question.source == "needs-review":
            link.exercise_key = key
            link.page_id = page.id
            return question
    return None


def _drop_unreviewed_page(session: Session, page: MaterialPage) -> None:
    from sqlalchemy import delete

    from cairn.models.collection import CollectionItem
    from cairn.models.error_item import ErrorItem

    links = list(session.scalars(select(MaterialLink).where(MaterialLink.page_id == page.id)))
    for link in links:
        question = session.get(Question, link.question_id)
        if question is None or question.source != "needs-review":
            session.delete(link)
            continue
        if session.scalar(select(ErrorItem.id).where(ErrorItem.question_id == question.id)):
            continue
        session.execute(delete(CollectionItem).where(CollectionItem.question_id == question.id))
        session.execute(delete(ScanQuestion).where(ScanQuestion.question_id == question.id))
        session.delete(link)
        session.delete(question)
    page.question_id = None
    session.flush()


def _retarget_figure(session: Session, learner_id: int, question: Question, image_path: Path, root: Path) -> None:
    from sqlalchemy import delete

    session.execute(delete(ScanQuestion).where(ScanQuestion.question_id == question.id))
    session.flush()
    _attach_scan(session, learner_id, question, image_path, root)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from cairn.models import init_db, make_engine, make_session_factory

    parser = argparse.ArgumentParser(description="把本地清单中的材料导入 Cairn 私有库")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--root", action="append", default=[])
    parser.add_argument("--db", default=os.environ.get("CAIRN_DB_URL") or "sqlite:///./var/private/jojo.db")
    args = parser.parse_args(argv)
    manifest = Path(args.manifest)
    roots = [Path(item) for item in args.root] or [manifest.parent]
    payload, documents = load_manifest_directory(manifest, roots)
    if not isinstance(payload, dict):
        raise MaterialError("清单不是对象")
    engine = make_engine(args.db)
    Path(db_dir(args.db)).mkdir(parents=True, exist_ok=True)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        seed_runtime(session)
        report = ingest_documents(session, documents, db_url=args.db)
        session.commit()
    destination = Path(db_dir(args.db)) / "ingest-report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "pages={pages} verified={verified_questions} review={review_questions} "
        "missing_files={missing_files} rejected_files={rejected_files}".format(**report)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
