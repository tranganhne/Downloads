"""Lưu trữ SQLite qua SQLAlchemy: lịch sử run, LLM Cache (khóa SHA-256), phản hồi của nhà phát triển.

Prototype dùng create_all(). Alembic (migration) chưa được cấu hình trong bản này.
"""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


def _now():
    return datetime.now(timezone.utc)


class RunRow(Base):
    __tablename__ = 'runs'
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(16))
    project_path: Mapped[str] = mapped_column(Text)
    plan_path: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default='{}')


class LLMCacheRow(Base):
    __tablename__ = 'llm_cache'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)   # SHA-256 hex
    response: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FeedbackRow(Base):
    __tablename__ = 'feedback'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)   # = violation.id
    verdict: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Storage:
    def __init__(self, url: str = 'sqlite:///archcheck.db'):
        self.engine = create_engine(url, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)

    # ---- runs ----
    def save_run(self, run_id, status, project_path, plan_path, summary: dict):
        with Session(self.engine) as s:
            s.merge(RunRow(run_id=run_id, status=status, project_path=project_path,
                           plan_path=plan_path, summary=json.dumps(summary, ensure_ascii=False)))
            s.commit()

    def get_run(self, run_id) -> dict | None:
        with Session(self.engine) as s:
            row = s.get(RunRow, run_id)
            if row is None:
                return None
            return {'run_id': row.run_id, 'status': row.status, 'project_path': row.project_path,
                    'plan_path': row.plan_path, 'summary': json.loads(row.summary)}

    # ---- LLM cache ----
    @staticmethod
    def make_cache_key(model: str, prompt: str) -> str:
        payload = json.dumps({'model': model, 'prompt': prompt}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode('utf8')).hexdigest()

    def cache_get(self, key: str) -> str | None:
        with Session(self.engine) as s:
            row = s.get(LLMCacheRow, key)
            return row.response if row else None

    def cache_put(self, key: str, response: str):
        with Session(self.engine) as s:
            s.merge(LLMCacheRow(key=key, response=response))
            s.commit()

    # ---- feedback ----
    def add_feedback(self, fingerprint: str, verdict: str, note: str = ''):
        with Session(self.engine) as s:
            s.add(FeedbackRow(fingerprint=fingerprint, verdict=verdict, note=note))
            s.commit()

    def latest_feedback(self) -> dict[str, tuple[str, str]]:
        """fingerprint -> (verdict, note) mới nhất."""
        with Session(self.engine) as s:
            rows = s.scalars(select(FeedbackRow).order_by(FeedbackRow.id)).all()
        return {r.fingerprint: (r.verdict, r.note) for r in rows}
