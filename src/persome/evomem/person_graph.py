"Person identity consolidation and interaction history over evomem."

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ..logger import get
from .engine import EvoMemory
from .models import MemoryLayer, MemoryNode, ReconcileAction, ReconcileOp

_log = get("persome.evomem.person_graph")


_TAG_ENTITY = "person-entity"
_TAG_EVENT = "person-event"
_TAG_SOURCE_ENTITY = "entity"


_META_CANONICAL = "canonical"
_META_ALIASES = "aliases"
_META_CATEGORY = "category"
_META_SIGHTINGS = "sightings"
_META_SOURCE_RECEIPTS = "source_receipts"


def _now() -> datetime:
    return datetime.now(UTC)


def _norm(name: str) -> str:
    folded = unicodedata.normalize("NFKC", name or "").strip()
    folded = " ".join(folded.split())
    return folded.casefold()


def _slug(canonical: str) -> str:
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", canonical or "").strip():
        if ch.isalnum():
            out.append(ch.lower())
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "unknown"


@dataclass
class PersonEvent:
    name: str
    summary: str = ""
    occurred_at: datetime | None = None
    category: str | None = None
    aliases: Sequence[str] = field(default_factory=tuple)
    confidence: float = 1.0
    source_id: str | None = None
    source_kind: str | None = None
    source_receipt: str | None = None


@dataclass
class PersonEntity:
    node_id: str
    canonical: str
    aliases: list[str]
    category: str | None
    sightings: int
    last_seen: datetime | None
    source_receipts: list[str] = field(default_factory=list)

    @property
    def seen_once(self) -> bool:
        return self.sightings <= 1


class PersonNameSource(Protocol):
    def events(self) -> list[PersonEvent]: ...


class EmptyPersonNameSource:
    def events(self) -> list[PersonEvent]:  # noqa: D102
        return []


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO timestamp, ALWAYS returning an aware datetime (naive → UTC).

    Real stores mix naive and aware strings (minute-granularity legacy rows vs
    tz-suffixed ones); a mixed list makes ``sort`` raise TypeError — which the
    relation extractor's fail-safe then swallows into "0 people, 0 edges".
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _meta_of(node: MemoryNode) -> dict:
    try:
        data = json.loads(node.schema_summary or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


class PersonGraph:
    def __init__(
        self,
        memory: EvoMemory,
        *,
        cfg: object | None = None,
        name_source: PersonNameSource | None = None,
        min_confidence: float = 0.6,
    ) -> None:
        self._mem = memory
        self._cfg = cfg
        self._source = name_source or EmptyPersonNameSource()
        self._min_confidence = min_confidence
        self._reserved_owner_keys: set[str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "person_graph_enabled", False))

    def _owner_keys(self) -> set[str]:
        if self._reserved_owner_keys is None:
            try:
                from . import owner_identity

                self._reserved_owner_keys = {
                    _norm(alias) for alias in owner_identity.reserved_aliases(self._cfg)
                }
            except Exception:  # noqa: BLE001 - identity protection is fail-safe
                self._reserved_owner_keys = set()
        return self._reserved_owner_keys

    def ingest(self) -> list[str]:
        if not self.enabled:
            return []
        touched: list[str] = []
        for event in self._source.events():
            canonical = self.record(event)
            if canonical is not None:
                touched.append(canonical)
        return touched

    def record(self, event: PersonEvent) -> str | None:
        if not self.enabled:
            return None
        norm = _norm(event.name)
        if not norm:
            return None
        owner_keys = self._owner_keys()
        if norm in owner_keys or any(_norm(alias) in owner_keys for alias in event.aliases):
            _log.debug("person_graph: reserve owner identity %r", event.name)
            return None

        # A Point is already the durable, receipt-bearing statement.  Treating
        # it as an interaction used to copy its body into a second
        # ``person-event`` Point and supersede the identity head once per source
        # Point.  Point-backed inputs now discover the roster identity only;
        # entry-backed activity remains the interaction timeline.
        point_backed = event.source_kind == "point"
        source_point = self._point_source_node(event) if point_backed else None
        if source_point is not None:
            # Entry mentions can be newer than the Point they mention.  If the
            # entry created a slug roster first, attach the later same-file
            # fact receipt without treating it as another interaction.  This
            # gives a later raw identity a durable, order-independent bridge.
            self._attach_point_receipt_to_roster(source_point, event)

        adopted, identity_blocked = self._adopt_late_identity_if_safe(event, norm)
        if identity_blocked:
            return None
        if adopted is not None:
            if point_backed:
                return adopted.canonical
            existing = adopted
        elif point_backed:
            matching_nodes = self._matching_entity_nodes(norm, event.aliases)
            if len(matching_nodes) > 1:
                _log.debug(
                    "person_graph: ambiguous point-backed roster %r; leave heads untouched",
                    event.name,
                )
                return None
            if matching_nodes:
                return self._to_entity(matching_nodes[0]).canonical
            existing = None
        else:
            existing = self._find_entity(norm, event.aliases)

        if (
            existing is not None
            and event.source_id
            and self._has_source_event(existing, event.source_id)
        ):
            return existing.canonical

        if existing is None and event.confidence < self._min_confidence:
            _log.debug("person_graph: skip low-confidence first sighting %r", event.name)
            return None

        if existing is None:
            canonical = event.name.strip()
            entity = self._create_entity(event, canonical)
        else:
            canonical = existing.canonical
            entity = self._merge_entity(existing, event)

        if not point_backed:
            self._append_event(entity_canonical=entity.canonical, event=event)
        return entity.canonical

    def _create_entity(self, event: PersonEvent, canonical: str) -> PersonEntity:
        aliases = _dedup_aliases([canonical, *event.aliases])
        adoptable = self._adoptable_identity(canonical)
        source_receipts = _dedup_aliases(
            [
                event.source_receipt or "",
                (f"⟨{adoptable.node_id}:{adoptable.file_name}⟩" if adoptable is not None else ""),
            ]
        )
        meta = {
            _META_CANONICAL: canonical,
            _META_ALIASES: aliases,
            _META_CATEGORY: event.category,
            _META_SIGHTINGS: 1,
            _META_SOURCE_RECEIPTS: source_receipts,
        }
        nid = (
            self._supersede_entity(adoptable.node_id, canonical, meta)
            if adoptable is not None
            else self._commit_entity(canonical, meta)
        )
        return PersonEntity(
            node_id=nid,
            canonical=canonical,
            aliases=aliases,
            category=event.category,
            sightings=1,
            last_seen=event.occurred_at or _now(),
            source_receipts=source_receipts,
        )

    def _merge_entity(self, existing: PersonEntity, event: PersonEvent) -> PersonEntity:
        aliases = _dedup_aliases([*existing.aliases, event.name, *event.aliases])
        category = existing.category or event.category
        sightings = existing.sightings + 1
        meta = {
            _META_CANONICAL: existing.canonical,
            _META_ALIASES: aliases,
            _META_CATEGORY: category,
            _META_SIGHTINGS: sightings,
            _META_SOURCE_RECEIPTS: existing.source_receipts,
        }
        nid = self._supersede_entity(existing.node_id, existing.canonical, meta)
        return PersonEntity(
            node_id=nid,
            canonical=existing.canonical,
            aliases=aliases,
            category=category,
            sightings=sightings,
            last_seen=event.occurred_at or _now(),
            source_receipts=existing.source_receipts,
        )

    def _adopt_late_identity(
        self, existing: PersonEntity, identity: MemoryNode, event: PersonEvent
    ) -> PersonEntity:
        """Merge one derived roster head with one later raw identity Point."""
        canonical = identity.content.strip()
        aliases = _dedup_aliases([canonical, existing.canonical, *existing.aliases, *event.aliases])
        source_receipts = _dedup_aliases(
            [
                *existing.source_receipts,
                event.source_receipt or "",
                f"⟨{identity.node_id}:{identity.file_name}⟩",
            ]
        )
        meta = {
            _META_CANONICAL: canonical,
            _META_ALIASES: aliases,
            _META_CATEGORY: existing.category or event.category,
            # Point-backed observations are not interaction sightings.
            _META_SIGHTINGS: existing.sightings,
            _META_SOURCE_RECEIPTS: source_receipts,
        }
        nid = self._supersede_entities([existing.node_id, identity.node_id], canonical, meta)
        return PersonEntity(
            node_id=nid,
            canonical=canonical,
            aliases=aliases,
            category=existing.category or event.category,
            sightings=existing.sightings,
            last_seen=existing.last_seen,
            source_receipts=source_receipts,
        )

    def _point_source_node(self, event: PersonEvent) -> MemoryNode | None:
        prefix = "entity:point:"
        source_id = str(event.source_id or "")
        if not source_id.startswith(prefix):
            return None
        node = self._mem.store.get(source_id.removeprefix(prefix))
        if node is None or not node.is_latest or not (node.file_name or "").startswith("person-"):
            return None
        return node

    def _raw_identity_nodes(self, file_name: str) -> list[MemoryNode]:
        return [
            node
            for node in self._mem.store.all_latest()
            if node.file_name == file_name and _TAG_SOURCE_ENTITY in (node.tags or "").split()
        ]

    def _roster_nodes(self, file_name: str) -> list[MemoryNode]:
        return [node for node in self._entity_nodes() if node.file_name == file_name]

    @staticmethod
    def _has_source_receipt_for_file(receipts: Sequence[str], file_name: str) -> bool:
        return any(str(receipt).endswith(f":{file_name}⟩") for receipt in receipts)

    def _attach_point_receipt_to_roster(self, source_point: MemoryNode, event: PersonEvent) -> None:
        """Attach a same-file fact receipt to one provable slug roster.

        Raw identity Points still use the stricter adoption path below.  This
        helper is only for a durable fact that arrived after an entry mention;
        multiple heads, a mismatched file slug, or a missing receipt remain a
        fail-closed no-op.
        """
        if _TAG_SOURCE_ENTITY in (source_point.tags or "").split():
            return
        receipt = str(event.source_receipt or "")
        if not receipt or not receipt.endswith(f":{source_point.file_name}⟩"):
            return
        roster_nodes = self._roster_nodes(source_point.file_name)
        if len(roster_nodes) != 1:
            return
        existing = self._to_entity(roster_nodes[0])
        expected_slug = source_point.file_name.removeprefix("person-").removesuffix(".md")
        if not any(
            _slug(name) == expected_slug for name in (existing.canonical, *existing.aliases)
        ):
            return
        # Only bridge a newly-created entry roster once. An empty receipt set
        # is an unverifiable legacy head and must not be laundered into the
        # automatic-adoption path; once any same-file Point receipt exists,
        # later facts remain evidence Points rather than roster revisions.
        if not existing.source_receipts or self._has_source_receipt_for_file(
            existing.source_receipts, source_point.file_name
        ):
            return
        source_receipts = _dedup_aliases([*existing.source_receipts, receipt])
        meta = {
            _META_CANONICAL: existing.canonical,
            _META_ALIASES: existing.aliases,
            _META_CATEGORY: existing.category,
            _META_SIGHTINGS: existing.sightings,
            _META_SOURCE_RECEIPTS: source_receipts,
        }
        self._supersede_entity(existing.node_id, existing.canonical, meta)

    def _adopt_late_identity_if_safe(
        self, event: PersonEvent, norm: str
    ) -> tuple[PersonEntity | None, bool]:
        """Return an adopted roster, or signal that identity evidence is ambiguous."""
        file_name = f"person-{_slug(event.name)}.md"
        identity_nodes = self._raw_identity_nodes(file_name)
        if not identity_nodes:
            return None, False
        roster_nodes = self._roster_nodes(file_name)
        identity = identity_nodes[0] if len(identity_nodes) == 1 else None
        safe_identity = (
            identity is not None
            and identity.file_name == f"person-{_slug(identity.content)}.md"
            and _norm(identity.content) == norm
        )
        matching_nodes = self._matching_entity_nodes(norm, event.aliases)
        matching_ids = {node.node_id for node in matching_nodes}
        roster_ids = {node.node_id for node in roster_nodes}
        if not safe_identity or len(roster_nodes) > 1 or not matching_ids <= roster_ids:
            _log.debug(
                "person_graph: ambiguous point identity %r; leave current heads untouched",
                event.name,
            )
            return None, True
        if not roster_nodes:
            return None, False

        existing = self._to_entity(roster_nodes[0])
        expected_slug = file_name.removeprefix("person-").removesuffix(".md")
        if not any(
            _slug(name) == expected_slug for name in (existing.canonical, *existing.aliases)
        ):
            _log.debug(
                "person_graph: roster %r does not claim file identity %r; leave untouched",
                existing.canonical,
                file_name,
            )
            return None, True
        if not self._has_source_receipt_for_file(existing.source_receipts, file_name):
            _log.debug(
                "person_graph: legacy roster %r has no point receipt; leave untouched",
                event.name,
            )
            return None, True
        return self._adopt_late_identity(existing, identity, event), False

    def _adoptable_identity(self, canonical: str) -> MemoryNode | None:
        """Return the one raw entity Point that can become the roster head.

        File, tag, and normalized body must all agree.  Multiple candidates are
        left untouched rather than choosing an arbitrary owner-visible Point.
        """
        file_name = f"person-{_slug(canonical)}.md"
        candidates = self._raw_identity_nodes(file_name)
        return (
            candidates[0]
            if len(candidates) == 1 and _norm(candidates[0].content) == _norm(canonical)
            else None
        )

    def _commit_entity(self, canonical: str, meta: dict) -> str:
        """Create a roster identity directly, without an immediate same-text revision."""
        from .engine import _new_id

        now = _now()
        node = MemoryNode(
            node_id=_new_id(now),
            content=canonical,
            layer=MemoryLayer.L5_KNOWLEDGE,
            is_latest=True,
            memory_at=now,
            gmt_created=now,
            user_id=self._mem.user_id,
            agent_id=self._mem.agent_id,
            file_name=f"person-{_slug(canonical)}.md",
            tags=_TAG_ENTITY,
            schema_summary=json.dumps(meta, ensure_ascii=False),
        )
        return self._mem.commit_node(node)

    def _supersede_entity(self, old_id: str, canonical: str, meta: dict) -> str:
        return self._supersede_entities([old_id], canonical, meta)

    def _supersede_entities(self, old_ids: Sequence[str], canonical: str, meta: dict) -> str:
        from .engine import _new_id

        old_ids = list(dict.fromkeys(old_id for old_id in old_ids if old_id))
        now = _now()
        node = MemoryNode(
            node_id=_new_id(now),
            content=canonical,
            layer=MemoryLayer.L5_KNOWLEDGE,
            supersedes=old_ids,
            is_latest=True,
            memory_at=now,
            gmt_created=now,
            user_id=self._mem.user_id,
            agent_id=self._mem.agent_id,
            file_name=f"person-{_slug(canonical)}.md",
            tags=_TAG_ENTITY,
            schema_summary=json.dumps(meta, ensure_ascii=False),
        )
        return self._mem.commit_supersede_many(node, old_ids=old_ids)

    def _append_event(self, *, entity_canonical: str, event: PersonEvent) -> str:
        """Append one event node without superseding or entering a chain."""
        op = ReconcileOp(
            action=ReconcileAction.ADD,
            content=event.summary or f"One interaction with {entity_canonical}",
            layer=MemoryLayer.L5_KNOWLEDGE,
        )

        from .engine import _new_id

        now = _now()
        node = MemoryNode(
            node_id=_new_id(now),
            content=op.content,
            layer=MemoryLayer.L5_KNOWLEDGE,
            is_latest=True,
            memory_at=event.occurred_at or now,
            gmt_created=now,
            user_id=self._mem.user_id,
            agent_id=self._mem.agent_id,
            file_name=f"person-{_slug(entity_canonical)}.md",
            tags=_TAG_EVENT,
            occurred_at=(event.occurred_at or now).isoformat(),
            schema_summary=json.dumps(
                {_META_CANONICAL: entity_canonical, "source_id": event.source_id},
                ensure_ascii=False,
            ),
        )
        return self._mem.commit_node(node)

    def _has_source_event(self, entity: PersonEntity, source_id: str) -> bool:
        wanted = {_norm(name) for name in (entity.canonical, *entity.aliases) if _norm(name)}
        for node in self._mem.store.all_latest():
            if _TAG_EVENT not in (node.tags or "").split():
                continue
            meta = _meta_of(node)
            if _norm(str(meta.get(_META_CANONICAL, ""))) not in wanted:
                continue
            if str(meta.get("source_id") or "") == source_id:
                return True
        return False

    def _entity_nodes(self) -> list[MemoryNode]:

        # the file taxonomy IS the kind axis's SSOT, so an adjudicated retype

        # roster (and out of knows-edge extraction) by construction.
        owner_keys = self._owner_keys()
        out: list[MemoryNode] = []
        for node in self._mem.store.all_latest():
            if _TAG_ENTITY not in (node.tags or "").split() or not (
                node.file_name or ""
            ).startswith("person-"):
                continue
            meta = _meta_of(node)
            names = [
                str(meta.get(_META_CANONICAL) or node.content),
                *(str(alias) for alias in (meta.get(_META_ALIASES) or [])),
            ]
            if any(_norm(name) in owner_keys for name in names):
                continue
            out.append(node)
        return out

    def _find_entity(self, norm_name: str, extra_aliases: Iterable[str]) -> PersonEntity | None:
        nodes = self._matching_entity_nodes(norm_name, extra_aliases)
        return self._to_entity(nodes[0]) if nodes else None

    def _matching_entity_nodes(
        self, norm_name: str, extra_aliases: Iterable[str]
    ) -> list[MemoryNode]:
        wanted = {norm_name, *(_norm(a) for a in extra_aliases)}
        wanted.discard("")
        matches: list[MemoryNode] = []
        for node in self._entity_nodes():
            meta = _meta_of(node)
            cand_canonical = _norm(meta.get(_META_CANONICAL, node.content))
            known = {_norm(a) for a in meta.get(_META_ALIASES, [])}
            known.add(cand_canonical)
            known.discard("")

            if norm_name in known or cand_canonical in wanted:
                matches.append(node)
        return matches

    def _to_entity(self, node: MemoryNode, meta: dict | None = None) -> PersonEntity:
        meta = meta if meta is not None else _meta_of(node)
        return PersonEntity(
            node_id=node.node_id,
            canonical=meta.get(_META_CANONICAL) or node.content,
            aliases=_dedup_aliases(meta.get(_META_ALIASES, []) or [node.content]),
            category=meta.get(_META_CATEGORY),
            sightings=int(meta.get(_META_SIGHTINGS, 1) or 1),
            last_seen=node.memory_at or node.gmt_created,
            source_receipts=_dedup_aliases(meta.get(_META_SOURCE_RECEIPTS, [])),
        )

    def list_persons(self, *, min_sightings: int = 1) -> list[PersonEntity]:
        people = [self._to_entity(n) for n in self._entity_nodes()]
        people = [p for p in people if p.sightings >= min_sightings]
        people.sort(key=lambda p: p.last_seen or datetime.min.replace(tzinfo=UTC), reverse=True)
        return people

    def person_timeline(self, name: str) -> list[MemoryNode]:
        norm = _norm(name)
        if not norm:
            return []
        entity = self._find_entity(norm, [])
        if entity is None:
            return []
        known_names = {_norm(name) for name in (entity.canonical, *entity.aliases) if _norm(name)}
        events: list[MemoryNode] = []
        for node in self._mem.store.all_latest():
            if _TAG_EVENT not in (node.tags or "").split():
                continue
            meta = _meta_of(node)
            if _norm(meta.get(_META_CANONICAL, "")) in known_names:
                events.append(node)
        events.sort(
            key=lambda n: (
                _parse_ts(n.occurred_at)
                or _aware(n.memory_at)
                or _aware(n.gmt_created)
                or datetime.min.replace(tzinfo=UTC)
            )
        )
        return events

    def build_person_context(self, name: str, *, max_events: int = 8) -> str:
        norm = _norm(name)
        if not norm:
            return ""
        entity = self._find_entity(norm, [])
        if entity is None:
            return ""
        timeline = self.person_timeline(entity.canonical)

        descr: list[str] = []
        if entity.category:
            descr.append(entity.category)
        other_aliases = [a for a in entity.aliases if _norm(a) != _norm(entity.canonical)]
        if other_aliases:
            descr.append("Aliases: " + ", ".join(other_aliases))
        descr.append(f"{entity.sightings} interaction(s)")
        header = entity.canonical + " (" + ", ".join(descr) + ")"

        lines = [header]
        for node in reversed(timeline[-max_events:]):
            when = _parse_ts(node.occurred_at) or node.memory_at or node.gmt_created
            stamp = when.strftime("%Y-%m-%d %H:%M") if when else "time unknown"
            summary = (node.content or "").strip() or "one interaction"
            lines.append(f"- {stamp} {summary}")
        if not timeline:
            lines.append("- (No interaction details recorded yet.)")
        return "\n".join(lines)


def _dedup_aliases(aliases: Iterable[str]) -> list[str]:
    """Deduplicate aliases by normalized key while preserving source order."""
    seen: set[str] = set()
    out: list[str] = []
    for a in aliases:
        text = (a or "").strip()
        key = _norm(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out
