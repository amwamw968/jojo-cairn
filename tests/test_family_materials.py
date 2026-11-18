"""Family profile, synthetic material intake, and private-data boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn.digitize.client import DigitizeError
from cairn.family import model_calls_blocked
from cairn.material_check import checked_answer, compute_body
from cairn.material_ingest import MaterialError, ingest_documents, parse_manifest
from cairn.models import init_db, make_engine, make_session_factory
from cairn.models.material import MaterialFile, MaterialPage
from cairn.models.question import Question
from cairn.tutor.llm import LLMError, chat
from cairn.web import create_app

STEM = "练 1\n计算：1+2+3+...+6+5+...+1=\n"
SOLUTION = STEM + "答案 36\n解析 6*6=36\n"


def _pdf(lines: list[str]) -> bytes:
    commands = ["BT /F1 18 Tf"]
    y = 760
    for line in lines:
        safe = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in line)
        safe = safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"1 0 0 1 72 {y} Tm ({safe}) Tj")
        y -= 28
    commands.append("ET")
    stream = "\n".join(commands).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _manifest(question: bytes, answer: bytes) -> dict:
    return {
        "synthetic": True,
        "files": [
            {
                "filename": "synthetic-questions.pdf",
                "label": "SYNTHETIC drill questions",
                "role": "questions",
                "bytes": len(question),
                "sha256": _sha(question),
                "pages": 1,
                "group": "synthetic-drill",
            },
            {
                "filename": "synthetic-answers.pdf",
                "label": "SYNTHETIC drill answers",
                "role": "answer_companion",
                "bytes": len(answer),
                "sha256": _sha(answer),
                "pages": 1,
                "group": "synthetic-drill",
            },
        ],
    }


def _ocr_for(question_text: str, answer_text: str):
    def _read(path: Path):
        from cairn.material_ingest import OcrLine, OcrResult

        text = answer_text if "answers" in path.as_posix() or path.parent.name.endswith("b") else question_text
        # Answer pages and question pages live in different sha directories.
        # The test passes texts by directory order instead: see ingest call.
        return OcrResult([OcrLine(text=text, confidence=0.99)], status="ok")

    return _read


def test_mountain_check_accepts_agreement_and_rejects_a_wrong_printed_answer():
    assert compute_body(STEM) == "36"
    assert checked_answer(STEM, SOLUTION) == "36"
    assert checked_answer(STEM, STEM + "答案 1\n") is None
    assert checked_answer("看不清的图形", "答案 4\n") is None
    messy = "练1\n计算：1+2+3+…+4+5+4+…+1=\n练 Z\n计算：1+2+…+3+4+3+…+1=\n"
    answers = "练1\n计算：1+2+3+…+4+5+4+…+1=\n答案\n25\n练 Z\n计算：1+2+…+3+4+3+…+1=\n答案 16\n"
    from cairn.material_check import parse_exercises
    stems, sols = parse_exercises(messy), parse_exercises(answers)
    assert [item["key"] for item in stems] == ["1", "n2"]
    assert checked_answer(stems[0]["body"], sols[0]["body"]) == "25"
    assert checked_answer(stems[1]["body"], sols[1]["body"]) == "16"


def test_squares_and_powers_need_the_printed_answer():
    squares = "20^2-19^2+18^2-17^2+...+2^2-1^2"
    assert compute_body(squares) == "210"
    assert checked_answer(squares, "答案 210") == "210"
    assert checked_answer("2^8*2^4*2^2*2", "答案 2^15") == "2^15"
    assert checked_answer("2^3*2^2", "答案 99") is None


def test_manifest_rejects_paths_answers_and_duplicate_hashes():
    payload = {"files": [{"filename": "../secret.pdf", "label": "x", "role": "source", "bytes": 1, "sha256": "a" * 64}]}
    with pytest.raises(MaterialError):
        parse_manifest(payload, {})
    payload = {"files": [{"filename": "a.pdf", "label": "x", "role": "source", "bytes": 1, "sha256": "ab" * 32, "answer": "1"}]}
    with pytest.raises(MaterialError):
        parse_manifest(payload, {})
    payload = {"files": [
        {"filename": "a.pdf", "label": "x", "role": "source", "bytes": 1, "sha256": "cd" * 32},
        {"filename": "b.pdf", "label": "y", "role": "source", "bytes": 1, "sha256": "cd" * 32},
    ]}
    with pytest.raises(MaterialError):
        parse_manifest(payload, {})


def test_synthetic_batch_checks_one_question_keeps_figures_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_FAMILY_PROFILE", "jojo")
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    question = _pdf(["SYNTHETIC FIXTURE"])
    answer = _pdf(["SYNTHETIC FIXTURE ANSWER"])
    payload = _manifest(question, answer)
    blobs = {_sha(question): question, _sha(answer): answer}
    documents = parse_manifest(payload, blobs)
    db = tmp_path / "jojo.db"
    url = f"sqlite:///{db}"
    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)

    def fake_ocr(path: Path):
        from cairn.material_ingest import OcrLine, OcrResult

        text = SOLUTION if path.as_posix().find(_sha(answer)[:16]) >= 0 else STEM
        return OcrResult([OcrLine(text=text, confidence=0.99)], status="ok")

    with factory() as session:
        from cairn.family import seed_runtime

        seed_runtime(session)
        report = ingest_documents(session, documents, db_url=url, ocr=fake_ocr)
        session.commit()
        again = ingest_documents(session, documents, db_url=url, ocr=fake_ocr)
        session.commit()
        pages = list(session.scalars(__import__("sqlalchemy").select(MaterialPage)))
        questions = list(session.scalars(__import__("sqlalchemy").select(Question)))
        files = list(session.scalars(__import__("sqlalchemy").select(MaterialFile)))
    assert report["verified_questions"] == 1
    assert report["pages"] == 2
    assert report["by_kind"]["question"] == 1
    assert report["by_kind"]["answer"] == 1
    assert report["figures_retained"] == 2
    assert again["verified_questions"] == 1
    assert len(files) == 2
    assert len(pages) == 2
    assert len([q for q in questions if q.source == "checked-import"]) == 1
    checked = next(q for q in questions if q.source == "checked-import")
    assert checked.answer == "36"
    assert "合成夹具" in checked.content
    assert "不是 Jojo 的真实材料" in checked.content
    assert report["publication"]["published"] is True
    image = next(p.image_relpath for p in pages if p.kind == "question")
    assert (tmp_path / image).is_file()
    assert (tmp_path / image).stat().st_size > 100


def test_wrong_printed_answer_is_not_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_FAMILY_PROFILE", "jojo")
    question = _pdf(["SYNTHETIC"])
    payload = {
        "synthetic": True,
        "files": [{
            "filename": "only.pdf", "label": "SYNTHETIC only", "role": "source_with_answers",
            "bytes": len(question), "sha256": _sha(question), "pages": 1,
        }],
    }
    documents = parse_manifest(payload, {_sha(question): question})
    db = tmp_path / "bad.db"
    url = f"sqlite:///{db}"
    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)

    def fake_ocr(path: Path):
        from cairn.material_ingest import OcrLine, OcrResult
        return OcrResult([OcrLine(text=STEM + "答案 1\n", confidence=0.2)], status="ok")

    with factory() as session:
        from cairn.family import seed_runtime

        seed_runtime(session)
        report = ingest_documents(session, documents, db_url=url, ocr=fake_ocr, publish=True)
        session.commit()
        stored = list(session.scalars(__import__("sqlalchemy").select(Question)))
    assert report["verified_questions"] == 0
    assert stored
    assert all(q.answer is None for q in stored)
    assert all(q.source == "needs-review" for q in stored)


def test_import_route_rejects_malformed_manifest_and_missing_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_FAMILY_PROFILE", "jojo")
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "test-token")
    db = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as client:
        denied = client.post("/materials/import", files={"manifest": ("m.json", b"{}", "application/json")})
        assert denied.status_code == 401
        bad = client.post(
            "/materials/import",
            headers={"Authorization": "Bearer test-token"},
            files={"manifest": ("m.json", b'{"files":[{"filename":"../x.pdf"}]}', "application/json")},
        )
        assert bad.status_code == 400
        assert bad.json()["detail"]
        named = {
            "files": [{
                "filename": "../secret.pdf", "label": "x", "role": "source",
                "bytes": 1, "sha256": "ab" * 32,
            }]
        }
        escaped = client.post(
            "/materials/import",
            headers={"Authorization": "Bearer test-token"},
            files={"manifest": ("m.json", json.dumps(named).encode(), "application/json")},
        )
        assert escaped.status_code == 400
        assert "文件名" in escaped.json()["detail"]
        home = client.get("/learn")
        assert home.status_code == 200
        assert "Jojo" in home.text
        assert "四年级" in home.text
        assert "已核对" in home.text
        settings = client.get("/settings")
        assert "材料与复核" in settings.text
        assert "学习者页面" in settings.text
        empty = client.get("/materials")
        assert "还没有导入材料" in empty.text
        assert "合成夹具" in empty.text


def test_family_profile_blocks_model_clients_and_default_learner_stays_without_it(monkeypatch):
    monkeypatch.delenv("CAIRN_FAMILY_PROFILE", raising=False)
    assert model_calls_blocked() is False
    monkeypatch.setenv("CAIRN_FAMILY_PROFILE", "jojo")
    with pytest.raises(LLMError):
        chat([{"role": "user", "content": "hi"}])
    with pytest.raises(DigitizeError):
        from cairn.digitize.client import describe
        describe("missing.png")


def test_practice_review_persists_for_checked_synthetic_item(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_FAMILY_PROFILE", "jojo")
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "test-token")
    question = _pdf(["SYNTHETIC FIXTURE"])
    answer = _pdf(["SYNTHETIC FIXTURE ANSWER"])
    payload = _manifest(question, answer)
    db = tmp_path / "practice.db"
    url = f"sqlite:///{db}"

    def fake_ocr(path: Path):
        from cairn.material_ingest import OcrLine, OcrResult
        text = SOLUTION if _sha(answer)[:16] in path.as_posix() else STEM
        return OcrResult([OcrLine(text=text, confidence=0.99)], status="ok")

    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        from cairn.family import seed_runtime
        seed_runtime(session)
        ingest_documents(session, parse_manifest(payload, {_sha(question): question, _sha(answer): answer}), db_url=url, ocr=fake_ocr)
        session.commit()
    app = create_app(url)
    with TestClient(app) as client:
        page = client.get("/practice")
        assert page.status_code == 200
        assert "合成夹具" in page.text
        assert "不是 Jojo 的真实材料" in page.text
        assert 'name="item_id"' in page.text
        item = page.text.split('name="item_id" value="', 1)[1].split('"', 1)[0]
        revealed = client.post("/practice/reveal", data={"item_id": item, "learner_answer": "36"})
        assert revealed.status_code == 200
        graded = client.post("/practice/grade", data={"item_id": item, "rating": "good"})
        assert graded.status_code == 200
    with TestClient(create_app(url)) as client:
        again = client.get("/practice")
        assert again.status_code == 200
        summary = client.get("/materials/summary")
        assert summary.status_code == 200
        body = summary.json()
        assert body["verified_questions"] >= 1
        assert body["pages"] == 2
        assert body["missing_files"] == 0


def test_missing_file_is_filled_later_without_a_second_row(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_FAMILY_PROFILE", "jojo")
    blob = _pdf(["SYNTHETIC FIXTURE"])
    digest = _sha(blob)
    base = {
        "filename": "later.pdf", "label": "SYNTHETIC later", "role": "source",
        "bytes": len(blob), "sha256": digest, "pages": 1,
    }
    db = tmp_path / "fill.db"
    url = f"sqlite:///{db}"
    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)

    def fake_ocr(path: Path):
        from cairn.material_ingest import OcrLine, OcrResult
        return OcrResult([OcrLine(text="看不清", confidence=0.2)], status="ok")

    with factory() as session:
        from cairn.family import seed_runtime
        seed_runtime(session)
        first = ingest_documents(session, parse_manifest({"synthetic": True, "files": [base]}, {}), db_url=url, ocr=fake_ocr)
        session.commit()
        second = ingest_documents(
            session, parse_manifest({"synthetic": True, "files": [base]}, {digest: blob}), db_url=url, ocr=fake_ocr,
        )
        session.commit()
        files = list(session.scalars(__import__("sqlalchemy").select(MaterialFile)))
        pages = list(session.scalars(__import__("sqlalchemy").select(MaterialPage)))
    assert first["missing_files"] == 1 and first["pages"] == 0
    assert second["missing_files"] == 0 and second["pages"] == 1
    assert len(files) == 1 and files[0].status == "stored"
    assert len(pages) == 1 and pages[0].figure_retained


def test_visual_review_adds_a_version_without_dropping_the_old_one(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_FAMILY_PROFILE", "jojo")
    question = _pdf(["SYNTHETIC FIXTURE"])
    answer = _pdf(["SYNTHETIC FIXTURE ANSWER"])
    payload = _manifest(question, answer)
    db = tmp_path / "visual.db"
    url = f"sqlite:///{db}"
    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)

    def fake_ocr(path: Path):
        from cairn.material_ingest import OcrLine, OcrResult
        text = SOLUTION if _sha(answer)[:16] in path.as_posix() else STEM
        return OcrResult([OcrLine(text=text, confidence=0.99)], status="ok")

    with factory() as session:
        from cairn.family import seed_runtime
        from cairn.material_ingest import apply_visual_reviews
        from cairn.models.collection import CollectionVersion
        seed_runtime(session)
        ingest_documents(session, parse_manifest(payload, {_sha(question): question, _sha(answer): answer}), db_url=url, ocr=fake_ocr)
        session.commit()
        first_ids = [row.id for row in session.scalars(__import__("sqlalchemy").select(CollectionVersion))]
        page = session.scalars(__import__("sqlalchemy").select(MaterialPage)).first()
        file_row = session.get(MaterialFile, page.file_id)
        apply_visual_reviews(session, [{
            "sha256": file_row.sha256,
            "page_number": page.page_number,
            "exercise_key": "extra",
            "content": "SYNTHETIC FIXTURE 计算：2+2=",
            "answer": "4",
            "analysis": "2+2=4",
        }], db_url=url)
        session.commit()
        versions = list(session.scalars(__import__("sqlalchemy").select(CollectionVersion).order_by(CollectionVersion.number)))
        from cairn.models.error_item import ErrorItem
        errors = list(session.scalars(__import__("sqlalchemy").select(ErrorItem)))
    assert [row.id for row in versions if row.id in first_ids]
    assert len(versions) == 2
    assert versions[0].snapshot_json is not None
    assert len(errors) == 2


def test_private_paths_are_gitignored():
    text = Path(".gitignore").read_text(encoding="utf-8")
    for needle in ("uploads/", "var/", "cairn-original-materials/", "*.pdf"):
        assert needle in text
