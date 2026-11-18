"""Private material ledger: one row per supplied file and per page.

Question text lives on Cairn ``question`` rows. This ledger records provenance,
page kind, and whether a transcription was checked. It is created in the local
database and is not a public catalog.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cairn.models.base import Base
from cairn.models.learner import utcnow


class MaterialFile(Base):
    __tablename__ = "material_file"
    __table_args__ = (
        UniqueConstraint("learner_id", "sha256", name="uq_material_file_learner_sha"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learner.id"), nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_name: Mapped[str] = mapped_column(String(255), nullable=False)
    inventory_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    group_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    media: Mapped[str] = mapped_column(String(16), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    printed_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stored_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MaterialPage(Base):
    __tablename__ = "material_page"
    __table_args__ = (
        UniqueConstraint("file_id", "page_number", name="uq_material_page_file_page"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("material_file.id"), nullable=False, index=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learner.id"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ocr_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocr_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_run")
    review_needed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    image_relpath: Mapped[str | None] = mapped_column(String(255), nullable=True)
    figure_retained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    companion_page_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    question_id: Mapped[int | None] = mapped_column(ForeignKey("question.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MaterialLink(Base):
    """One supplied page can back several exercises, or a solution page."""

    __tablename__ = "material_link"
    __table_args__ = (
        UniqueConstraint("page_id", "question_id", "role", name="uq_material_link_page_question_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("material_page.id"), nullable=False, index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("question.id"), nullable=False, index=True)
    exercise_key: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False)
