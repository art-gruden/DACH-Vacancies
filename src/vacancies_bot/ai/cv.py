"""Optional CV processing (opt-in, separate consent).

* The uploaded file is read in memory and **never stored**; only its SHA-256, size and type are kept.
* Text is extracted locally (pypdf / python-docx) and scrubbed of contact data, dates of birth, addresses,
  links and sensitive personal lines *before* anything else sees it (also before an external AI model).
* The scrubbed text is kept encrypted (Fernet, ``CV_ENCRYPTION_KEY``) only until the user confirms the extracted
  facts or until ``CV_RETENTION_HOURS``; without a key it is not stored at all.
"""

from __future__ import annotations

import hashlib
import io
import re
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from ..db.models import CvDocument
from ..db.session import Database, utcnow
from ..logging import get_logger

log = get_logger(__name__)

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE), "[link]"),
    (re.compile(r"(?:linkedin|xing|github)\.com/\S+", re.IGNORECASE), "[link]"),
    (re.compile(r"(?<!\w)(?:\+|00)\d[\d\s()/.-]{7,}\d"), "[phone]"),
    (re.compile(r"(?<!\w)0\d{2,4}[\s/-]?\d{3,}[\s-]?\d{0,6}"), "[phone]"),
    (re.compile(r"\b\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2}\b"), "[date]"),
    (re.compile(r"\b(?:\d{5})\s+[A-ZÄÖÜ][\wäöüß-]+"), "[address]"),
    (re.compile(r"\b[\wäöüß-]+(?:straße|strasse|str\.|weg|gasse|platz|allee)\s*\d+\w?", re.IGNORECASE), "[address]"),
    (re.compile(r"\b(?:ул\.|улица|вул\.|вулиця)\s*[\w-]+\s*\d*", re.IGNORECASE), "[address]"),
]
_SENSITIVE_LINE = re.compile(
    r"(geburts|date of birth|born|дата рождения|дата народження|geboren|nationalit|staatsangeh|citizenship|гражданств|громадянств|"
    r"familienstand|marital|семейное|сімейний|religion|konfession|вероисповед|geschlecht|gender|пол:|стать|"
    r"photo|foto|фото|kinder|children|дети|діти|adresse|address|адрес|telefon|phone|телефон|e-mail|email)",
    re.IGNORECASE,
)


class CvError(Exception):
    pass


def extract_text(data: bytes, mime: str, filename: str = "") -> str:
    name = filename.lower()
    if mime == PDF or name.endswith(".pdf"):
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise CvError("encrypted PDF")
            return "\n".join((p.extract_text() or "") for p in reader.pages[:10])
        except CvError:
            raise
        except Exception as exc:
            raise CvError("unreadable PDF") from exc
    if mime == DOCX or name.endswith(".docx"):
        from docx import Document

        try:
            doc = Document(io.BytesIO(data))
        except Exception as exc:
            raise CvError("unreadable DOCX") from exc
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    raise CvError("unsupported type")


def scrub_pii(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if _SENSITIVE_LINE.search(line) and len(line) < 160:
            continue  # the whole line (e.g. "Geburtsdatum: …", "Nationalität: …", "Telefon: …")
        for rx, repl in _PATTERNS:
            line = rx.sub(repl, line)
        lines.append(line)
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def drop_header_name(text: str) -> str:
    """CVs usually start with the candidate's name; a short first line without job words is removed."""
    lines = text.strip().splitlines()
    if lines and len(lines[0].split()) <= 4 and not re.search(r"\d|engineer|developer|manager|entwickler|инженер|разработчик", lines[0], re.I):
        return "\n".join(lines[1:])
    return text


class CvStore:
    def __init__(self, db: Database, key: str, retention_hours: int = 24, max_bytes: int = 5 * 1024 * 1024) -> None:
        self.db = db
        self.fernet = Fernet(key.encode()) if key else None
        self.retention = timedelta(hours=retention_hours)
        self.max_bytes = max_bytes

    def prepare(self, data: bytes, mime: str, filename: str) -> tuple[str, str]:
        if len(data) > self.max_bytes:
            raise CvError("too big")
        text = scrub_pii(drop_header_name(extract_text(data, mime, filename)))
        if len(text) < 30:
            raise CvError("no text (scanned PDF?)")
        return text, hashlib.sha256(data).hexdigest()

    async def remember(self, user_id: int, sha: str, mime: str, size: int, text: str, extracted: dict) -> int:
        enc = self.fernet.encrypt(text.encode()) if self.fernet else None
        async with self.db.session() as s:
            doc = CvDocument(user_id=user_id, file_sha256=sha, mime=mime[:100], size_bytes=size, status="extracted",
                             encrypted_text=enc, extracted=extracted, delete_after=utcnow() + self.retention, created_at=utcnow())
            s.add(doc)
            await s.flush()
            return doc.id

    async def latest(self, user_id: int) -> CvDocument | None:
        async with self.db.session() as s:
            return (await s.execute(select(CvDocument).where(CvDocument.user_id == user_id, CvDocument.deleted_at.is_(None))
                                    .order_by(CvDocument.id.desc()).limit(1))).scalar_one_or_none()

    def decrypt(self, doc: CvDocument) -> str | None:
        if not (self.fernet and doc.encrypted_text):
            return None
        try:
            return self.fernet.decrypt(doc.encrypted_text).decode()
        except InvalidToken:
            return None

    async def wipe(self, user_id: int, *, confirmed: bool = False) -> int:
        async with self.db.session() as s:
            docs = list((await s.execute(select(CvDocument).where(CvDocument.user_id == user_id, CvDocument.deleted_at.is_(None)))).scalars())
            for d in docs:
                d.encrypted_text, d.extracted, d.deleted_at = None, None, utcnow()
                d.status = "confirmed" if confirmed else "deleted"
            return len(docs)

    async def purge_expired(self) -> int:
        async with self.db.session() as s:
            docs = list((await s.execute(select(CvDocument).where(CvDocument.deleted_at.is_(None), CvDocument.delete_after < utcnow()))).scalars())
            for d in docs:
                d.encrypted_text, d.extracted, d.deleted_at, d.status = None, None, utcnow(), "deleted"
            return len(docs)
