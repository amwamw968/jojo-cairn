"""Shared fixtures: independent in-memory DB per test."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from cairn.models import Learner, Question, init_db, make_engine, make_session_factory
from cairn.ocr import engine as ocr_engine
from cairn.voice import speech_cache


_SAMPLE_DIR = Path(__file__).resolve().parents[1] / "docs" / "samples"
_SAMPLE_NAMES = frozenset({
    "hi-redbg.jpg",
    "user-20261101.jpg",
    "manual-crop.jpg",
    "samsung-scan.jpg",
})


def _skip_if_unpublished_sample(path: Path) -> None:
    """Skip only when this call touches an upstream photo this snapshot omits."""
    try:
        resolved = path.resolve()
    except OSError:
        return
    if resolved.parent != _SAMPLE_DIR or resolved.name not in _SAMPLE_NAMES:
        return
    if Path.is_file(path):
        return
    pytest.skip(
        "未公开的上游样例图不在本快照，本用例不记为失败："
        f"docs/samples/{resolved.name}"
    )


@pytest.fixture(autouse=True)
def _isolate_unpublished_samples(monkeypatch):
    """Upstream page photos are not in this public tree. Missing them is a skip.

    Tests that supply their own images, and tests that run once those files are
    placed in ``docs/samples/``, keep their original pass/fail result.
    """
    original_read_bytes = Path.read_bytes
    original_is_file = Path.is_file

    def read_bytes(self, *args, **kwargs):
        _skip_if_unpublished_sample(self)
        return original_read_bytes(self, *args, **kwargs)

    def is_file(self, *args, **kwargs):
        if self.parent == _SAMPLE_DIR and self.name in _SAMPLE_NAMES and not original_is_file(self):
            pytest.skip(
                "未公开的上游样例图不在本快照，本用例不记为失败："
                f"docs/samples/{self.name}"
            )
        return original_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "is_file", is_file)

    import cv2

    original_imread = cv2.imread

    def imread(filename, *args, **kwargs):
        _skip_if_unpublished_sample(Path(filename))
        return original_imread(filename, *args, **kwargs)

    monkeypatch.setattr(cv2, "imread", imread)


@pytest.fixture(autouse=True)
def _restore_ocr_engine():
    """每个测试结束后把 OCR 引擎缓存放回原样。

    上传链路现在会真做一次方向判定（`scan_service.save_upload` → `detect_page_angle`），
    任何"上传一张照片"的用例都会顺手把模型加载起来。不还原的话，
    `test_ocr.test_engine_is_lazy_on_import`（断言模块级缓存仍是 None）会被别人的
    副作用带红——它和那些用例无关。
    """
    saved = ocr_engine._engine
    yield
    ocr_engine._engine = saved


@pytest.fixture(autouse=True)
def _fresh_tts_cache(tmp_path, monkeypatch):
    """每个测试独立的 TTS 磁盘缓存目录，并重置默认缓存单例。

    防两类污染：测试之间共享缓存条目；测试把条目写进真实运行用的
    data/tts-cache（实测会让 test_xiaozhi_audio 的假 runner 第二轮收不到调用）。
    """
    monkeypatch.setenv(speech_cache.ENV_CACHE_DIR, str(tmp_path / "tts-cache"))
    monkeypatch.setattr(speech_cache, "_default_cache", None)
    yield


@pytest.fixture()
def session() -> Session:
    engine = make_engine("sqlite://")
    init_db(engine)
    factory = make_session_factory(engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def learner(session: Session) -> Learner:
    ln = Learner(name="test-child", grade_level="primary-3")
    session.add(ln)
    session.commit()
    return ln


@pytest.fixture()
def question(session: Session, learner: Learner) -> Question:
    """One question owned by the fixture learner (a question always has an owner)."""
    q = Question(
        learner_id=learner.id,
        subject="math",
        grade_level="primary-3",
        question_type="fill",
        content="12 + 15 = ?",
        answer="27",
        analysis="12+15=27",
    )
    session.add(q)
    session.commit()
    return q
