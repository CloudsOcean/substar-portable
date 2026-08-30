from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import zlib
from collections import OrderedDict
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from substar_core.artifacts import atomic_write_json
from substar_core.domain import ChangeProvenance, DocumentRevision, EditorDocument


PROJECT_MANIFEST_SCHEMA = "substar.project-store.sqlite.v3"
PROJECT_DATABASE_SCHEMA = 1
CHECKPOINT_INTERVAL = 50
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CACHE_LIMIT = 4
_CACHE_LOCK = threading.RLock()
_REVISION_CACHE: OrderedDict[tuple[str, str], DocumentRevision] = OrderedDict()


class ProjectStoreError(RuntimeError):
    pass


class ProjectConflictError(ProjectStoreError):
    pass


class ProjectIntegrityError(ProjectStoreError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _compress_json(value: Any) -> tuple[bytes, str]:
    encoded = _canonical_bytes(value)
    return zlib.compress(encoded, level=6), _sha256_bytes(encoded)


def _decompress_json(blob: bytes, checksum: str) -> Any:
    try:
        encoded = zlib.decompress(blob)
    except zlib.error as exc:
        raise ProjectIntegrityError("revision payload is not valid compressed data") from exc
    if _sha256_bytes(encoded) != checksum:
        raise ProjectIntegrityError("revision checksum mismatch")
    try:
        return json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectIntegrityError("revision payload is unreadable") from exc


def _entity_patch(
    before: tuple[Any, ...], after: tuple[Any, ...], *, id_attribute: str
) -> dict[str, Any]:
    before_by_id = {getattr(item, id_attribute): item for item in before}
    after_by_id = {getattr(item, id_attribute): item for item in after}
    return {
        "upsert": [
            item.to_dict()
            for item in after
            if before_by_id.get(getattr(item, id_attribute)) != item
        ],
        "remove": [item_id for item_id in before_by_id if item_id not in after_by_id],
    }


def _ordered_entity_patch(
    before: tuple[Any, ...], after: tuple[Any, ...], *, id_attribute: str
) -> dict[str, Any]:
    patch = _entity_patch(before, after, id_attribute=id_attribute)
    before_ids = [getattr(item, id_attribute) for item in before]
    after_ids = [getattr(item, id_attribute) for item in after]
    prefix = 0
    while (
        prefix < len(before_ids)
        and prefix < len(after_ids)
        and before_ids[prefix] == after_ids[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(before_ids) - prefix
        and suffix < len(after_ids) - prefix
        and before_ids[-1 - suffix] == after_ids[-1 - suffix]
    ):
        suffix += 1
    order_splice = None
    if before_ids != after_ids:
        before_end = len(before_ids) - suffix if suffix else len(before_ids)
        after_end = len(after_ids) - suffix if suffix else len(after_ids)
        order_splice = {
            "start": prefix,
            "delete_count": before_end - prefix,
            "insert_ids": after_ids[prefix:after_end],
        }
    return {**patch, "order_splice": order_splice}


def _cue_patch(before: tuple[Any, ...], after: tuple[Any, ...]) -> dict[str, Any]:
    before_by_id = {item.cue_id: item for item in before}
    after_by_id = {item.cue_id: item for item in after}

    def without_index(item: Any) -> dict[str, Any]:
        value = item.to_dict()
        value.pop("index", None)
        return value

    before_ids = [item.cue_id for item in before]
    after_ids = [item.cue_id for item in after]
    prefix = 0
    while (
        prefix < len(before_ids)
        and prefix < len(after_ids)
        and before_ids[prefix] == after_ids[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(before_ids) - prefix
        and suffix < len(after_ids) - prefix
        and before_ids[-1 - suffix] == after_ids[-1 - suffix]
    ):
        suffix += 1
    order_splice = None
    if before_ids != after_ids:
        before_end = len(before_ids) - suffix if suffix else len(before_ids)
        after_end = len(after_ids) - suffix if suffix else len(after_ids)
        order_splice = {
            "start": prefix,
            "delete_count": before_end - prefix,
            "insert_ids": after_ids[prefix:after_end],
        }
    return {
        "upsert": [
            item.to_dict()
            for item in after
            if item.cue_id not in before_by_id
            or without_index(before_by_id[item.cue_id]) != without_index(item)
        ],
        "remove": [item_id for item_id in before_by_id if item_id not in after_by_id],
        "order_splice": order_splice,
    }


def _document_patch(before: EditorDocument, after: EditorDocument) -> dict[str, Any]:
    before_changes = before.changes
    changes_are_append = after.changes[: len(before_changes)] == before_changes
    return {
        "schema_version": "substar.document-patch.v1",
        "properties": after.properties.to_dict() if before.properties != after.properties else None,
        "presentation": (
            after.presentation.to_dict() if before.presentation != after.presentation else None
        ),
        "source_tokens": _entity_patch(
            before.source_tokens, after.source_tokens, id_attribute="token_id"
        ),
        "display_tokens": _ordered_entity_patch(
            before.display_tokens, after.display_tokens, id_attribute="token_id"
        ),
        "cues": _cue_patch(before.cues, after.cues),
        "groups": _entity_patch(before.groups, after.groups, id_attribute="group_id"),
        "changes_mode": "append" if changes_are_append else "replace",
        "changes": [
            item.to_dict()
            for item in (
                after.changes[len(before_changes) :]
                if changes_are_append
                else after.changes
            )
        ],
    }


def _apply_entity_patch(
    items: list[dict[str, Any]], patch: Mapping[str, Any], *, id_key: str, sort_key: str | None = None
) -> list[dict[str, Any]]:
    removed = {str(value) for value in patch.get("remove", [])}
    upserts = {str(item[id_key]): item for item in patch.get("upsert", [])}
    result: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item[id_key])
        if item_id in removed:
            continue
        result.append(upserts.pop(item_id, item))
    result.extend(upserts.values())
    if sort_key is not None:
        result.sort(key=lambda item: int(item[sort_key]))
    return result


def _apply_ordered_entity_patch(
    items: list[dict[str, Any]], patch: Mapping[str, Any], *, id_key: str
) -> list[dict[str, Any]]:
    updated = _apply_entity_patch(items, patch, id_key=id_key)
    by_id = {str(item[id_key]): item for item in updated}
    # ``order_splice`` is expressed against the complete pre-patch order.  Keep
    # that order intact until after the splice; filtering removed entities first
    # shifts the splice and deletes the following entity a second time.
    order = [str(item[id_key]) for item in items]
    splice = patch.get("order_splice")
    if splice:
        start = int(splice["start"])
        delete_count = int(splice["delete_count"])
        order[start : start + delete_count] = [
            str(value) for value in splice.get("insert_ids", [])
        ]
    order = [item_id for item_id in order if item_id in by_id]
    known = set(order)
    for item_id in by_id:
        if item_id not in known:
            order.append(item_id)
            known.add(item_id)
    return [by_id[item_id] for item_id in order]


def _apply_document_patch(
    document: dict[str, Any], patch: Mapping[str, Any]
) -> dict[str, Any]:
    if patch.get("schema_version") != "substar.document-patch.v1":
        raise ProjectIntegrityError("unsupported document patch schema")
    result = dict(document)
    if patch.get("properties") is not None:
        result["properties"] = patch["properties"]
    if patch.get("presentation") is not None:
        result["presentation"] = patch["presentation"]
    result["source_tokens"] = _apply_entity_patch(
        list(result["source_tokens"]), patch.get("source_tokens", {}), id_key="token_id", sort_key="index"
    )
    result["display_tokens"] = _apply_ordered_entity_patch(
        list(result["display_tokens"]), patch.get("display_tokens", {}), id_key="token_id"
    )
    current_cues = _apply_ordered_entity_patch(
        list(result["cues"]), patch.get("cues", {}), id_key="cue_id"
    )
    result["cues"] = [
        {**cue, "index": index} for index, cue in enumerate(current_cues)
    ]
    if patch.get("groups"):
        result["groups"] = _apply_entity_patch(
            list(result.get("groups", [])), patch.get("groups", {}), id_key="group_id"
        )
        if not result["groups"]:
            result.pop("groups", None)
    changes = list(patch.get("changes", []))
    result["changes"] = (
        [*result.get("changes", []), *changes]
        if patch.get("changes_mode") == "append"
        else changes
    )
    return result


def _cache_key(root: Path, revision_id: str) -> tuple[str, str]:
    return str(root.resolve()), revision_id


def _cache_get(root: Path, revision_id: str) -> DocumentRevision | None:
    key = _cache_key(root, revision_id)
    with _CACHE_LOCK:
        revision = _REVISION_CACHE.get(key)
        if revision is not None:
            _REVISION_CACHE.move_to_end(key)
        return revision


def _cache_put(root: Path, revision: DocumentRevision) -> None:
    key = _cache_key(root, revision.revision_id)
    with _CACHE_LOCK:
        _REVISION_CACHE[key] = revision
        _REVISION_CACHE.move_to_end(key)
        while len(_REVISION_CACHE) > _CACHE_LIMIT:
            _REVISION_CACHE.popitem(last=False)


class ProjectStore:
    """Transactional project storage backed by SQLite WAL and compact revision patches."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.manifest_path = self.root / "manifest.json"
        self.database_path = self.root / "project.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.database_path, timeout=5.0, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except sqlite3.Error as exc:
            raise ProjectIntegrityError("project database is unavailable") from exc

    @classmethod
    def create(cls, root: str | Path, *, project_id: str) -> "ProjectStore":
        if not _SAFE_ID.fullmatch(project_id):
            raise ValueError("project_id contains unsafe characters")
        store = cls(root)
        if store.manifest_path.exists() or store.database_path.exists():
            raise ProjectStoreError(f"project already exists: {store.root}")
        store.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            store.manifest_path,
            {
                "schema_version": PROJECT_MANIFEST_SCHEMA,
                "database_schema": PROJECT_DATABASE_SCHEMA,
                "project_id": project_id,
            },
        )
        with closing(store._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE revisions (
                    revision_number INTEGER PRIMARY KEY,
                    revision_id TEXT NOT NULL UNIQUE,
                    parent_revision_id TEXT,
                    created_at TEXT NOT NULL,
                    complete INTEGER NOT NULL,
                    provenance_json TEXT NOT NULL,
                    document_hash TEXT NOT NULL,
                    snapshot_blob BLOB,
                    patch_blob BLOB,
                    inverse_patch_blob BLOB,
                    payload_sha256 TEXT NOT NULL,
                    inverse_sha256 TEXT,
                    CHECK ((snapshot_blob IS NOT NULL) != (patch_blob IS NOT NULL))
                );
                CREATE INDEX revisions_parent_idx ON revisions(parent_revision_id);
                """
            )
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("schema_version", str(PROJECT_DATABASE_SCHEMA)),
                    ("project_id", project_id),
                    ("document_id", ""),
                ],
            )
        return store

    @classmethod
    def open(cls, root: str | Path) -> "ProjectStore":
        store = cls(root)
        store.load_manifest()
        return store

    def _marker(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            raise ProjectStoreError(f"project manifest does not exist: {self.manifest_path}")
        try:
            marker = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectIntegrityError("project manifest is unreadable") from exc
        if marker.get("schema_version") != PROJECT_MANIFEST_SCHEMA:
            raise ProjectIntegrityError(
                f"unsupported project manifest schema: {marker.get('schema_version')!r}"
            )
        if int(marker.get("database_schema", 0)) != PROJECT_DATABASE_SCHEMA:
            raise ProjectIntegrityError("unsupported project database schema")
        if not self.database_path.is_file():
            raise ProjectIntegrityError("project database does not exist")
        return marker

    def load_manifest(self) -> dict[str, Any]:
        marker = self._marker()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT revision_id, revision_number, parent_revision_id, created_at, "
                    "complete, document_hash, payload_sha256 FROM revisions ORDER BY revision_number"
                ).fetchall()
                document_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='document_id'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise ProjectIntegrityError("project database is unreadable") from exc
        revisions = [
            {
                "revision_id": row["revision_id"],
                "revision_number": row["revision_number"],
                "parent_revision_id": row["parent_revision_id"],
                "path": f"project.sqlite3#revision/{row['revision_number']}",
                "sha256": row["payload_sha256"],
                "complete": bool(row["complete"]),
                "created_at": row["created_at"],
                "document_hash": row["document_hash"],
            }
            for row in rows
        ]
        return {
            "schema_version": PROJECT_MANIFEST_SCHEMA,
            "project_id": marker["project_id"],
            "document_id": document_row["value"] if document_row else "",
            "latest_revision_id": revisions[-1]["revision_id"] if revisions else None,
            "revision_count": len(revisions),
            "revisions": revisions,
        }

    def list_revision_metadata(
        self,
        *,
        limit: int | None = None,
        before_revision_number: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return revision-list fields without reconstructing document payloads."""
        self._marker()
        clauses: list[str] = []
        parameters: list[Any] = []
        if before_revision_number is not None:
            clauses.append("revision_number < ?")
            parameters.append(int(before_revision_number))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        suffix = " ORDER BY revision_number DESC"
        if limit is not None:
            suffix += " LIMIT ?"
            parameters.append(max(1, int(limit)))
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT revision_id, revision_number, parent_revision_id, created_at, "
                    "complete, provenance_json, document_hash FROM revisions"
                    + where
                    + suffix,
                    parameters,
                ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectIntegrityError("project database is unreadable") from exc
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                provenance = ChangeProvenance.from_dict(
                    json.loads(row["provenance_json"])
                ).to_dict()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProjectIntegrityError(
                    "revision provenance metadata is invalid"
                ) from exc
            result.append(
                {
                    "revision_id": row["revision_id"],
                    "revision_number": row["revision_number"],
                    "parent_revision_id": row["parent_revision_id"],
                    "created_at": row["created_at"],
                    "complete": bool(row["complete"]),
                    "document_hash": row["document_hash"],
                    "provenance": provenance,
                }
            )
        return result

    def _resolve_row(
        self, connection: sqlite3.Connection, revision: str | int
    ) -> sqlite3.Row:
        if isinstance(revision, int):
            row = connection.execute(
                "SELECT * FROM revisions WHERE revision_number=?", (revision,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM revisions WHERE revision_id=?", (revision,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown revision: {revision!r}")
        return row

    def _load_revision_with_connection(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> DocumentRevision:
        cached = _cache_get(self.root, str(row["revision_id"]))
        if cached is not None:
            return cached
        snapshot = connection.execute(
            "SELECT * FROM revisions WHERE revision_number<=? AND snapshot_blob IS NOT NULL "
            "ORDER BY revision_number DESC LIMIT 1",
            (row["revision_number"],),
        ).fetchone()
        if snapshot is None:
            raise ProjectIntegrityError("revision chain has no checkpoint")
        document_value = _decompress_json(
            snapshot["snapshot_blob"], snapshot["payload_sha256"]
        )
        patches = connection.execute(
            "SELECT patch_blob, payload_sha256 FROM revisions "
            "WHERE revision_number>? AND revision_number<=? ORDER BY revision_number",
            (snapshot["revision_number"], row["revision_number"]),
        ).fetchall()
        for patch_row in patches:
            if patch_row["patch_blob"] is None:
                raise ProjectIntegrityError("revision chain contains an unexpected checkpoint")
            document_value = _apply_document_patch(
                document_value,
                _decompress_json(patch_row["patch_blob"], patch_row["payload_sha256"]),
            )
        # Verify the exact reconstructed payload before schema defaults are
        # applied by ``EditorDocument.from_dict``.  New optional fields may be
        # added with backward-compatible defaults; hashing only the normalized
        # document would then make an intact legacy revision look corrupt.
        stored_document_hash = str(row["document_hash"])
        serialized_document_hash = _sha256_bytes(_canonical_bytes(document_value))
        try:
            document = EditorDocument.from_dict(document_value)
            provenance = ChangeProvenance.from_dict(
                json.loads(row["provenance_json"])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProjectIntegrityError("revision content is invalid") from exc
        document_hash = document.content_hash()
        if stored_document_hash not in {
            serialized_document_hash,
            document_hash,
        }:
            raise ProjectIntegrityError("revision document checksum mismatch")
        revision = DocumentRevision(
            revision_id=row["revision_id"],
            revision_number=row["revision_number"],
            document=document,
            parent_revision_id=row["parent_revision_id"],
            provenance=provenance,
            created_at=row["created_at"],
            document_hash=document_hash,
        )
        _cache_put(self.root, revision)
        return revision

    def save(
        self,
        document: EditorDocument,
        *,
        provenance: ChangeProvenance,
        expected_revision_id: str | None = None,
    ) -> DocumentRevision:
        self._marker()
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            latest_row = connection.execute(
                "SELECT * FROM revisions ORDER BY revision_number DESC LIMIT 1"
            ).fetchone()
            latest_id = latest_row["revision_id"] if latest_row else None
            if expected_revision_id is not None and expected_revision_id != latest_id:
                raise ProjectConflictError(
                    f"expected revision {expected_revision_id!r}, latest is {latest_id!r}"
                )
            document_id_row = connection.execute(
                "SELECT value FROM metadata WHERE key='document_id'"
            ).fetchone()
            current_document_id = document_id_row["value"] if document_id_row else ""
            if current_document_id not in ("", document.document_id):
                raise ProjectStoreError("a project cannot change its document_id")
            latest = (
                self._load_revision_with_connection(connection, latest_row)
                if latest_row is not None
                else None
            )
            # Completion describes the latest accepted document, not the
            # project forever. Any later document revision invalidates that
            # acceptance unless this revision is explicitly setting it.
            if document.complete and provenance.operation != "set_complete_attribute":
                document = replace(
                    document,
                    properties=replace(document.properties, complete=False),
                )
            revision = DocumentRevision.create(
                revision_number=(latest_row["revision_number"] + 1 if latest_row else 1),
                document=document,
                parent_revision_id=latest_id,
                provenance=provenance,
            )
            checkpoint = (
                latest is None
                or revision.revision_number % CHECKPOINT_INTERVAL == 0
                or provenance.operation == "checkpoint"
            )
            if checkpoint:
                snapshot_blob, payload_sha = _compress_json(document.to_dict())
                patch_blob = None
                inverse_blob = None
                inverse_sha = None
            else:
                patch_blob, payload_sha = _compress_json(
                    _document_patch(latest.document, document)
                )
                # Undo/redo navigates immutable forward revisions and never
                # reads inverse_patch_blob. Keep the nullable legacy columns so
                # old databases remain readable, but stop doubling every new
                # patch with an unused reverse copy.
                inverse_blob = None
                inverse_sha = None
                snapshot_blob = None
            connection.execute(
                "INSERT INTO revisions(revision_number, revision_id, parent_revision_id, created_at, "
                "complete, provenance_json, document_hash, snapshot_blob, patch_blob, "
                "inverse_patch_blob, payload_sha256, inverse_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.revision_number,
                    revision.revision_id,
                    revision.parent_revision_id,
                    revision.created_at,
                    int(document.complete),
                    json.dumps(provenance.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    revision.document_hash,
                    snapshot_blob,
                    patch_blob,
                    inverse_blob,
                    payload_sha,
                    inverse_sha,
                ),
            )
            if not current_document_id:
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='document_id'",
                    (document.document_id,),
                )
            connection.execute("COMMIT")
        except ProjectConflictError:
            if "connection" in locals():
                connection.execute("ROLLBACK")
            raise
        except ProjectStoreError:
            if "connection" in locals():
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if "connection" in locals():
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise ProjectStoreError("project transaction failed") from exc
        finally:
            if "connection" in locals():
                connection.close()
        _cache_put(self.root, revision)
        return revision

    def load_latest(self) -> DocumentRevision | None:
        self._marker()
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM revisions ORDER BY revision_number DESC LIMIT 1"
                ).fetchone()
                return self._load_revision_with_connection(connection, row) if row else None
        except sqlite3.Error as exc:
            raise ProjectIntegrityError("project database is unreadable") from exc

    def load_revision(self, revision: str | int) -> DocumentRevision:
        self._marker()
        try:
            with closing(self._connect()) as connection:
                row = self._resolve_row(connection, revision)
                return self._load_revision_with_connection(connection, row)
        except sqlite3.Error as exc:
            raise ProjectIntegrityError("project database is unreadable") from exc

    @classmethod
    def clear_memory_cache(cls, root: str | Path | None = None) -> None:
        with _CACHE_LOCK:
            if root is None:
                _REVISION_CACHE.clear()
                return
            resolved = str(Path(root).resolve())
            for key in [key for key in _REVISION_CACHE if key[0] == resolved]:
                _REVISION_CACHE.pop(key, None)
