"""Durable, scoped conversation images. No Job authority or pixel projections."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from embodied_runtime.images import ImageInput, ImageLimits
from embodied_runtime.jobs.images import validate_image


@dataclass(frozen=True, slots=True)
class MediaRetentionConfiguration:
    enabled: bool = True
    database_path: Path = Path("data/conversation-media.sqlite3")
    operator_scope: str = "primary_operator"
    conversation_scope: str = "default"
    max_age_days: int = 7
    max_storage_bytes: int = 64 * 1024 * 1024
    max_items: int = 256

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("conversation_media.enabled must be boolean")
        for name in ("operator_scope", "conversation_scope"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                raise ValueError(f"conversation_media.{name} must be a bounded scope identifier")
        for name, ceiling in (("max_age_days", 3650), ("max_storage_bytes", 1024**3),
                              ("max_items", 4096)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"conversation_media.{name} must be an integer from 1 to {ceiling}")
        if not isinstance(self.database_path, Path) or not str(self.database_path).strip():
            raise ValueError("conversation_media.database_path is required")


class MediaUnavailableError(ValueError):
    pass


class ConversationMediaStore:
    """SQLite bytes and metadata committed together; bounded indexes and cleanup.

    Retired metadata keeps retry identities for a bounded window. Quota exhaustion
    rejects persistence; it never silently deletes a live image to fit a new one.
    """

    def __init__(self, config=MediaRetentionConfiguration(), *, clock=lambda: datetime.now(UTC)):
        self.config, self.clock = config, clock
        if not config.enabled:
            raise ValueError("conversation media persistence is disabled")
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(config.database_path)
        self._db.row_factory = sqlite3.Row
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("unsupported conversation media schema")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA secure_delete=ON")
            with self._db:
                self._db.execute("""CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, operator_scope TEXT NOT NULL, conversation_scope TEXT NOT NULL,
                    channel TEXT NOT NULL, source_message_id TEXT NOT NULL, attachment_index INTEGER NOT NULL,
                    received_at TEXT NOT NULL, media_type TEXT NOT NULL, width INTEGER NOT NULL,
                    height INTEGER NOT NULL, integrity_hash TEXT NOT NULL, byte_size INTEGER NOT NULL,
                    message_text TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, label TEXT NOT NULL,
                    UNIQUE(operator_scope, conversation_scope, channel, source_message_id, attachment_index))""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS pixels (
                    id TEXT PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE, data BLOB NOT NULL)""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS request_media (
                    request_id TEXT NOT NULL, media_id TEXT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
                    PRIMARY KEY(request_id, media_id))""")
                self._db.execute("CREATE INDEX IF NOT EXISTS media_recent ON media "
                                 "(operator_scope, conversation_scope, received_at DESC, source_message_id, attachment_index)")
                self._db.execute("""CREATE TABLE IF NOT EXISTS media_ingestions (
                    operator_scope TEXT NOT NULL, conversation_scope TEXT NOT NULL,
                    channel TEXT NOT NULL, source_message_id TEXT NOT NULL,
                    attachment_count INTEGER NOT NULL CHECK(attachment_count BETWEEN 1 AND 16),
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('partial','processing','completed','failed')),
                    PRIMARY KEY(operator_scope, conversation_scope, channel, source_message_id))""")
                if version == 1:
                    # v1 request links were written before cognition: an analysis may have run.
                    # Unlinked partial downloads must remain recoverable, not become duplicates.
                    self._db.execute("""INSERT OR IGNORE INTO media_ingestions
                        SELECT operator_scope, conversation_scope, channel, source_message_id,
                               max(attachment_index)+1, min(received_at), 'processing'
                        FROM media m WHERE EXISTS (SELECT 1 FROM request_media r WHERE r.media_id=m.id)
                        GROUP BY operator_scope, conversation_scope, channel, source_message_id""")
                self._db.execute("PRAGMA user_version=2")
            config.database_path.chmod(0o600)
            self.cleanup()
        except BaseException:
            self._db.close()
            raise

    def _scope(self):
        return (self.config.operator_scope, self.config.conversation_scope)

    def _cutoff(self):
        return (self.clock().astimezone(UTC) - timedelta(days=self.config.max_age_days)).isoformat()

    def cleanup(self, limit=32):
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("cleanup limit must be 1..32")
        with self._db:
            rows = self._db.execute("SELECT id FROM media WHERE operator_scope=? AND conversation_scope=? "
                                    "AND status='available' AND received_at<=? ORDER BY received_at LIMIT ?",
                                    (*self._scope(), self._cutoff(), limit)).fetchall()
            for row in rows:
                self._db.execute("DELETE FROM pixels WHERE id=?", (row['id'],))
                self._db.execute("UPDATE media SET status='expired' WHERE id=?", (row['id'],))
            # Bounded tombstones: preserve recent retry identities after expiry/deletion.
            self._db.execute("DELETE FROM media WHERE id IN (SELECT id FROM media WHERE operator_scope=? AND conversation_scope=? "
                "AND status!='available' ORDER BY received_at DESC LIMIT ? OFFSET ?)",
                (*self._scope(), limit, self.config.max_items * 4))
            self._db.execute("DELETE FROM media_ingestions WHERE rowid IN ("
                "SELECT rowid FROM media_ingestions WHERE operator_scope=? AND conversation_scope=? "
                "AND received_at<=? ORDER BY received_at LIMIT ?)",
                (*self._scope(), self._cutoff(), limit))
            self._db.execute("DELETE FROM media_ingestions WHERE rowid IN ("
                "SELECT rowid FROM media_ingestions WHERE operator_scope=? AND conversation_scope=? "
                "ORDER BY received_at DESC, rowid DESC LIMIT ? OFFSET ?)",
                (*self._scope(), limit, self.config.max_items * 4))
        return len(rows)

    def ingestion_state(self, channel, identity):
        row = self._db.execute("SELECT attachment_count, received_at, status FROM media_ingestions "
            "WHERE operator_scope=? AND conversation_scope=? AND channel=? AND source_message_id=?",
            (*self._scope(), channel, identity)).fetchone()
        return dict(row) if row is not None else None

    def begin_ingestion(self, channel, identity, attachment_count, received_at):
        if type(attachment_count) is not int or not 1 <= attachment_count <= 16:
            raise ValueError("invalid ingestion attachment count")
        if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
            raise ValueError("invalid ingestion message identity")
        if received_at.utcoffset() is None:
            raise ValueError("receipt timestamp must be offset-aware")
        self.cleanup()
        with self._db:
            # Reuse the first actual receipt time, including v1 partial records.
            first = self._db.execute("SELECT min(received_at) FROM media WHERE operator_scope=? "
                "AND conversation_scope=? AND channel=? AND source_message_id=?",
                (*self._scope(), channel, identity)).fetchone()[0]
            self._db.execute("INSERT OR IGNORE INTO media_ingestions VALUES (?,?,?,?,?,?, 'partial')",
                (*self._scope(), channel, identity, attachment_count,
                 first or received_at.astimezone(UTC).isoformat()))
            state = self.ingestion_state(channel, identity)
            if state['attachment_count'] != attachment_count:
                raise ValueError("duplicate message attachment count changed")
        return state

    def load_message_attachment(self, channel, identity, index, *, limits=ImageLimits()):
        row = self._db.execute("SELECT id FROM media WHERE operator_scope=? AND conversation_scope=? "
            "AND channel=? AND source_message_id=? AND attachment_index=?",
            (*self._scope(), channel, identity, index)).fetchone()
        if row is None:
            return None
        # Deleted, expired or corrupt records fail honestly; never download replacements.
        image, = self.load_many([row['id']], limits=limits)
        from embodied_runtime.attachments import ImageAttachment
        return ImageAttachment(image.media_type, image.data, reference=image.reference,
            label=image.label, source=image.source, received_at=image.received_at,
            source_message_id=identity, attachment_index=index)

    def claim_ingestion(self, channel, identity):
        """Claim once after the worker validates the complete ordered attachment set.

        The durable claim precedes cognition: a crash leaves an indeterminate
        processing record, which must not replay possibly completed effects.
        """
        with self._db:
            cursor = self._db.execute("UPDATE media_ingestions SET status='processing' WHERE "
                "operator_scope=? AND conversation_scope=? AND channel=? AND source_message_id=? "
                "AND status='partial'", (*self._scope(), channel, identity))
        return cursor.rowcount == 1

    def finish_ingestion(self, channel, identity, status):
        if status not in ('completed', 'failed'):
            raise ValueError("invalid ingestion outcome")
        with self._db:
            self._db.execute("UPDATE media_ingestions SET status=? WHERE operator_scope=? AND "
                "conversation_scope=? AND channel=? AND source_message_id=? AND status='processing'",
                (status, *self._scope(), channel, identity))

    def has_message(self, channel, identity):
        return self._db.execute("SELECT 1 FROM media WHERE operator_scope=? AND conversation_scope=? "
            "AND channel=? AND source_message_id=? LIMIT 1", (*self._scope(), channel, identity)).fetchone() is not None

    def retain(self, attachments, *, channel, message_identity, message_text="", limits=ImageLimits()):
        metadata = limits.validate(attachments, decode=True)
        self.cleanup()
        records = []
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            for index, (image, meta) in enumerate(zip(attachments, metadata)):
                identity = image.source_message_id or message_identity
                attachment_index = image.attachment_index if image.source_message_id else index
                key = (*self._scope(), channel, identity, attachment_index)
                existing = self._db.execute("SELECT * FROM media WHERE operator_scope=? AND conversation_scope=? "
                    "AND channel=? AND source_message_id=? AND attachment_index=?", key).fetchone()
                if existing is not None:
                    if existing['integrity_hash'] != meta.content_version:
                        raise ValueError("duplicate media identity has different content")
                    if self._project(existing)['status'] != 'available':
                        raise MediaUnavailableError("previously retained media is unavailable; retry not saved")
                    records.append(self._project(existing))
                    continue
                count, size = self._db.execute("SELECT count(*), coalesce(sum(byte_size),0) FROM media "
                                                "WHERE status='available'").fetchone()
                if count >= self.config.max_items or size + len(image.data) > self.config.max_storage_bytes:
                    raise ValueError("conversation media storage quota exceeded")
                received = image.received_at or self.clock()
                if received.utcoffset() is None:
                    raise ValueError("receipt timestamp must be offset-aware")
                reference = 'MEDIA' + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]
                self._db.execute("INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    reference, *key[:2], channel, identity, attachment_index,
                    received.astimezone(UTC).isoformat(), meta.media_type, meta.width, meta.height,
                    meta.content_version, len(image.data), message_text[:2000], 'available', image.source,
                    image.label or f'attachment {attachment_index + 1}'))
                self._db.execute("INSERT INTO pixels VALUES (?,?)", (reference, image.data))
                records.append(self._project(self._row(reference)))
        return tuple(records)

    def _row(self, reference):
        row = self._db.execute("SELECT * FROM media WHERE id=? AND operator_scope=? AND conversation_scope=?",
                               (reference, *self._scope())).fetchone()
        if row is None:
            raise MediaUnavailableError("image unavailable: missing, foreign scope or legacy-unretained")
        return row

    def _project(self, row):
        result = {key: row[key] for key in ('channel', 'source_message_id', 'attachment_index', 'received_at',
                  'media_type', 'width', 'height', 'integrity_hash', 'byte_size', 'status')}
        result.update(image_ref=row['id'], source=row['source'], storage='conversation_media', label=row['label'], captured_at=None,
                      operator_scope=row['operator_scope'], conversation_scope=row['conversation_scope'],
                      message_excerpt=row['message_text'][:300])
        if row['status'] == 'available' and row['received_at'] <= self._cutoff():
            result['status'] = 'expired'
        return result

    def list_images(self, *, query="", after=None, before=None, limit=8, offset=0):
        self.cleanup()
        if (not isinstance(query, str) or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 16
                or type(offset) is not int or not 0 <= offset <= self.config.max_items * 4):
            raise ValueError("invalid bounded media query")
        predicate, values = "", [*self._scope(), self._cutoff()]
        for name, value, comparison in (("after", after, ">="), ("before", before, "<")):
            if value is not None:
                parsed = datetime.fromisoformat(value)
                if parsed.utcoffset() is None:
                    raise ValueError(f"{name} requires offset-aware receipt time")
                predicate += f" AND received_at{comparison}?"
                values.append(parsed.astimezone(UTC).isoformat())
        # Literal substring search, never SQL/FTS syntax or a claim of visual search.
        predicate += " AND instr(lower(message_text),lower(?))>0"
        values.extend((query, limit + 1, offset))
        rows = self._db.execute("SELECT * FROM media WHERE operator_scope=? AND conversation_scope=? "
            "AND received_at>? AND status='available'" + predicate +
            " ORDER BY received_at DESC, source_message_id DESC, attachment_index ASC LIMIT ? OFFSET ?", values).fetchall()
        return {"status": "ok", "images": [self._project(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
                "meaning": "receipt_order_and_message_text_only_not_visual_search"}

    def load_many(self, references, *, limits=ImageLimits()):
        if not isinstance(references, (list, tuple)) or not references or len(references) > limits.max_images:
            raise ValueError("image count exceeds configured maximum or selection is empty")
        rows = [self._row(reference) for reference in references]
        if sum(row['byte_size'] for row in rows) > limits.max_total_bytes:
            raise ValueError("aggregate image bytes exceed configured maximum")
        if sum(row['width'] * row['height'] for row in rows) > limits.max_total_pixels:
            raise ValueError("aggregate decoded image pixels exceed configured maximum")
        images = []
        for row in rows:
            if self._project(row)['status'] != 'available':
                raise MediaUnavailableError(f"image unavailable: {self._project(row)['status']}")
            pixels = self._db.execute("SELECT data FROM pixels WHERE id=?", (row['id'],)).fetchone()
            if pixels is None or hashlib.sha256(pixels['data']).hexdigest() != row['integrity_hash']:
                raise MediaUnavailableError("image unavailable: missing or corrupt bytes")
            images.append(ImageInput(pixels['data'], row['media_type'], row['width'], row['height'],
                row['id'], row['label'], row['source'],
                received_at=datetime.fromisoformat(row['received_at'])))
        limits.validate(images, decode=True)
        return tuple(images)

    def link_request(self, references, request_id):
        with self._db:
            for reference in references:
                self._row(reference)
                self._db.execute("INSERT OR IGNORE INTO request_media VALUES (?,?)", (request_id, reference))
                self._db.execute("DELETE FROM request_media WHERE media_id=? AND request_id NOT IN "
                    "(SELECT request_id FROM request_media WHERE media_id=? ORDER BY rowid DESC LIMIT 8)",
                    (reference, reference))

    def delete(self, references):
        if not isinstance(references, list) or not 1 <= len(references) <= 16:
            raise ValueError("delete requires 1..16 exact image IDs")
        with self._db:
            for reference in references:
                self._row(reference)
                self._db.execute("DELETE FROM pixels WHERE id=?", (reference,))
                self._db.execute("UPDATE media SET status='deleted' WHERE id=?", (reference,))
        return {"status": "applied", "deleted_image_refs": references}

    def close(self):
        self._db.close()
