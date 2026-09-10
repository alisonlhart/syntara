#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "fastapi>=0.110",
#     "httpx>=0.27",
#     "pyyaml>=6.0",
#     "jsonschema>=4.18",
#     "sqlmodel>=0.0.14",
#     "pytest>=8.0",
#     "psycopg[binary]>=3.1",
# ]
# ///
"""End-to-end registry + advertisement prototype for the ``http_request`` node.

    ┌───────────────────────────────────────────────────────────────────┐
    │ CONTROL-PLANE SDK DELIVERABLE — the registration & advertisement   │
    │ half of the Node SDK. Where test_register.py proves validate ->    │
    │ compile -> persist for a single row, this proves the *full* loop    │
    │ the visual builder depends on: a REST surface that ingests a        │
    │ manifest, persists the compiled descriptor to PostgreSQL, and       │
    │ advertises it back to the React Flow canvas.                        │
    └───────────────────────────────────────────────────────────────────┘

The prototype wires three layers together:

1. **Storage** - a ``NodeType`` SQLModel table matching the target platform DDL,
   including the ``CHECK (execution_type != 'container' OR image_ref IS NOT NULL)``
   constraint. The ``descriptor`` column is JSONB on PostgreSQL and falls back to
   generic JSON on SQLite so the same model runs in-memory in CI.
2. **SDK pipeline** - load ``manifest.yaml``, validate it against
   ``common-definitions.json`` (Draft-07, via ``$ref``), and compile it into the
   runtime ``node-definition.json`` descriptor.
3. **Advertisement API** - a FastAPI app implementing the registry REST contract
   documented in ``node-sdk-architecture.md`` (standard ``{data, meta}`` envelope,
   id-addressable resources, pagination + filtering) plus the two React Flow
   visual-builder additions that doc calls out:

     * ``POST /api/v1/node-types``                    - register (ingests a
       YAML/JSON manifest, validates, compiles, upserts). Returns ``{data: record}``.
     * ``GET  /api/v1/node-types``                    - list envelope; filter by
       ``category`` / ``execution_type`` / ``enabled``; ``?view=palette`` returns
       the React Flow drawer summaries instead of the record projection.
     * ``GET  /api/v1/node-types/{id_or_name}``       - fetch one record; the path
       accepts a UUID *or* a node ``name`` (the UI-facing alias).
     * ``GET  /api/v1/node-types/{id_or_name}/descriptor`` - the raw compiled
       ``node-definition.json``, unwrapped, for direct canvas form rendering.

Run standalone (fetches deps in an ephemeral env):

    uv run schemas/examples/http-request/test_postgres_registry.py

Or under pytest once deps are installed:

    pytest schemas/examples/http-request/test_postgres_registry.py

Set ``SYNTARA_TEST_DATABASE_URL`` to a reachable PostgreSQL URL to exercise the
real JSONB column and DDL CHECK constraint instead of the SQLite default, e.g.

    SYNTARA_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/syntara_test
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.testclient import TestClient
from jsonschema import Draft7Validator
from sqlalchemy import JSON, MetaData
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import (
    CheckConstraint,
    Column,
    Field,
    Session,
    SQLModel,
    UniqueConstraint,
    create_engine,
    select,
)

HERE = Path(__file__).resolve().parent
SCHEMAS_ROOT = HERE.parent.parent  # schemas/
MANIFEST_PATH = HERE / "manifest.yaml"
COMMON_DEFINITIONS_PATH = SCHEMAS_ROOT / "common-definitions.json"
COMPILED_PATH = HERE / "node-definition.json"

# Shared, hardened execution-plane HTTP executor image. A real build pipeline
# injects the freshly built image ref at compile/publish time (see
# build-manifest.sh: `ao-sdk publish --image <image-ref>`); the register endpoint
# uses this as the default for container nodes when the caller supplies none.
DEFAULT_CONTAINER_IMAGE = "quay.io/ahetheri/http-executor:dev"

# Point this at a reachable PostgreSQL to run the DB-configuration path against a
# real JSONB column + DDL CHECK instead of the in-memory SQLite default.
POSTGRES_URL_ENV = "SYNTARA_TEST_DATABASE_URL"

# React Flow palette fallback glyphs, keyed by node category. A manifest may
# override this per node via a top-level ``icon`` field (see the NodePaletteIcon
# definition in common-definitions.json).
ICON_BY_CATEGORY = {
    "action": "cube",
    "task": "terminal",
    "workflow": "project-diagram",
    "trigger": "bolt",
}

# List pagination default, matching the documented registry API meta envelope.
DEFAULT_PAGE_LIMIT = 50


# ---------------------------------------------------------------------------
# Registry model - matches the target platform node_types DDL.
# ---------------------------------------------------------------------------
class NodeCategory(StrEnum):
    ACTION = "action"
    TASK = "task"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"


class NodeExecutionType(StrEnum):
    IN_PROCESS = "in_process"
    CONTAINER = "container"


class RegistryModel(SQLModel):
    """Base with a private MetaData so this prototype's ``node_types`` table does
    not collide with the identically named table in the sibling
    ``test_register.py`` when both are collected in a single pytest session."""

    metadata = MetaData()


class NodeType(RegistryModel, table=True):
    """Registry record for one compiled node definition.

    The ``descriptor`` column stores the full compiled ``node-definition.json``
    so the canvas can render input forms without a frontend deployment. The
    CHECK constraint ties container execution to a resolvable image reference,
    and ``(name, version)`` is unique so multiple versions of a node can coexist.
    """

    __tablename__ = "node_types"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_node_types_name_version"),
        CheckConstraint(
            "execution_type != 'container' OR image_ref IS NOT NULL",
            name="node_types_container_requires_image_ref",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True)
    display_name: str
    version: str = Field(default="1.0.0")
    # Persist the enum *value* ("action"/"container") as TEXT + CHECK IN (...),
    # matching the target DDL. native_enum=False keeps this portable across
    # SQLite and PostgreSQL and stores the value, not the member name.
    category: NodeCategory = Field(
        sa_column=Column(
            SAEnum(
                NodeCategory,
                native_enum=False,
                values_callable=lambda e: [m.value for m in e],
            ),
            index=True,
            nullable=False,
        )
    )
    execution_type: NodeExecutionType = Field(
        sa_column=Column(
            SAEnum(
                NodeExecutionType,
                native_enum=False,
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        )
    )
    image_ref: str | None = Field(default=None)
    descriptor: dict[str, Any] = Field(
        sa_column=Column(JSONB().with_variant(JSON(), "sqlite"), nullable=False)
    )
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# SDK pipeline: load -> validate -> compile.
# ---------------------------------------------------------------------------
def load_manifest() -> dict[str, Any]:
    with MANIFEST_PATH.open() as handle:
        return yaml.safe_load(handle)


def load_common_definitions() -> dict[str, Any]:
    with COMMON_DEFINITIONS_PATH.open() as handle:
        return json.load(handle)


def build_validator() -> Draft7Validator:
    """Build a node meta-schema validator wired to common-definitions.json.

    The node meta-schema references shared platform types via ``$ref`` (as every
    manifest must), so taxonomy fields validate against the single source of
    truth rather than being re-declared here.
    """
    common = load_common_definitions()
    node_schema: dict[str, Any] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "nodeType": {"type": "string", "minLength": 1},
            "displayName": {"type": "string", "minLength": 1},
            "icon": {"$ref": "common-definitions.json#/definitions/NodePaletteIcon"},
            "category": {"$ref": "common-definitions.json#/definitions/NodeCategory"},
            "execution_type": {
                "$ref": "common-definitions.json#/definitions/NodeExecutionType"
            },
            "inputs": {"$ref": "common-definitions.json#/definitions/NodeInputs"},
            "outputs": {"type": "object"},
            "metadata": {
                "type": "object",
                "properties": {
                    "workload_classification": {
                        "$ref": "common-definitions.json#/definitions/WorkloadClassification"
                    },
                    "resource_requirements": {
                        "$ref": "common-definitions.json#/definitions/ResourceRequirements"
                    },
                    "scheduling_controls": {
                        "$ref": "common-definitions.json#/definitions/SchedulingControls"
                    },
                    "dependencies": {
                        "$ref": "common-definitions.json#/definitions/DependencyDeclaration"
                    },
                },
            },
        },
        "required": ["nodeType", "category", "execution_type", "inputs", "outputs"],
    }

    # jsonschema >= 4.18 uses the `referencing` library; fall back to the legacy
    # RefResolver on older installs so this harness stays portable.
    try:
        from referencing import Registry, Resource

        registry = Registry().with_resources(
            [
                ("common-definitions.json", Resource.from_contents(common)),
                (common["$id"], Resource.from_contents(common)),
            ]
        )
        return Draft7Validator(node_schema, registry=registry)
    except ImportError:  # pragma: no cover - legacy jsonschema
        from jsonschema import RefResolver

        resolver = RefResolver(
            base_uri="",
            referrer=node_schema,
            store={"common-definitions.json": common, common["$id"]: common},
        )
        return Draft7Validator(node_schema, resolver=resolver)


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty when valid)."""
    validator = build_validator()
    errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.path))
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors]


def compile_manifest(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compile manifest.yaml -> node-definition.json (the persisted artifact).

    When no manifest is provided the on-disk ``manifest.yaml`` is loaded. The
    compiled descriptor is written back to ``node-definition.json`` so the build
    artifact stays in sync, mirroring ``build-manifest.sh``.
    """
    descriptor = manifest if manifest is not None else load_manifest()
    with COMPILED_PATH.open("w") as handle:
        json.dump(descriptor, handle, indent=2)
    return descriptor


# ---------------------------------------------------------------------------
# Advertisement helpers: descriptor / row -> API representations.
# ---------------------------------------------------------------------------
def _version_key(version: str) -> tuple[int, ...]:
    """Coarse numeric sort key so the registry can pick the latest version."""
    return tuple(int(n) for n in re.findall(r"\d+", version)) or (0,)


def _latest(rows: list[NodeType]) -> NodeType:
    return max(rows, key=lambda r: _version_key(r.version))


def _latest_per_name(rows: list[NodeType]) -> list[NodeType]:
    """Collapse multiple versions to the highest version per node name."""
    by_name: dict[str, NodeType] = {}
    for row in rows:
        current = by_name.get(row.name)
        if current is None or _version_key(row.version) > _version_key(current.version):
            by_name[row.name] = row
    return sorted(by_name.values(), key=lambda r: r.display_name)


def _summarize_whitespace(text: str) -> str:
    """Collapse a multi-line manifest description into a single palette line."""
    return re.sub(r"\s+", " ", text).strip()


def _palette_summary(row: NodeType) -> dict[str, Any]:
    """Project a row into the React Flow drag-and-drop drawer summary (camelCase)."""
    descriptor = row.descriptor
    icon = descriptor.get("icon") or ICON_BY_CATEGORY.get(str(row.category), "cube")
    return {
        "name": row.name,
        "displayName": row.display_name,
        "category": str(row.category),
        "executionType": str(row.execution_type),
        "version": row.version,
        "description": _summarize_whitespace(descriptor.get("description", "")),
        "icon": icon,
    }


def _list_item(row: NodeType) -> dict[str, Any]:
    """The compact list projection from the documented registry API."""
    return {
        "id": str(row.id),
        "name": row.name,
        "category": str(row.category),
        "execution_type": str(row.execution_type),
        "enabled": row.enabled,
    }


def _record(row: NodeType) -> dict[str, Any]:
    """The full single-resource record (snake_case) from the documented API."""
    return {
        "id": str(row.id),
        "name": row.name,
        "display_name": row.display_name,
        "category": str(row.category),
        "execution_type": str(row.execution_type),
        "image_ref": row.image_ref,
        "version": row.version,
        "enabled": row.enabled,
        "descriptor": row.descriptor,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# FastAPI application factory.
# ---------------------------------------------------------------------------
def create_app(engine) -> FastAPI:
    """Build the advertisement API bound to a specific engine.

    The session dependency is bound to the provided engine so tests can inject a
    fresh in-memory database while production wires in a real PostgreSQL engine.
    """
    app = FastAPI(title="Syntara Node Registry", version="1.0.0")

    def get_session():
        with Session(engine) as session:
            yield session

    def _resolve(session: Session, id_or_name: str) -> NodeType:
        """Resolve a path segment that is either a UUID or a node name.

        Name lookups return the latest version — the UI-facing addressing mode.
        """
        try:
            node_id = UUID(id_or_name)
        except ValueError:
            rows = session.exec(
                select(NodeType).where(NodeType.name == id_or_name)
            ).all()
            if not rows:
                raise HTTPException(404, f"node type '{id_or_name}' is not registered")
            return _latest(list(rows))
        row = session.get(NodeType, node_id)
        if row is None:
            raise HTTPException(404, f"node type '{id_or_name}' is not registered")
        return row

    @app.post("/api/v1/node-types", status_code=201)
    def register_node_type(
        manifest_body: Any = Body(
            ...,
            media_type="application/yaml",
            description="A node manifest as YAML or JSON.",
        ),
        image_ref: str | None = None,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Validate, compile, and upsert a node manifest into the registry."""
        # Accept either a raw YAML/JSON document (bytes/str) or a parsed object.
        if isinstance(manifest_body, (bytes, str)):
            try:
                manifest = yaml.safe_load(manifest_body)
            except yaml.YAMLError as exc:
                raise HTTPException(400, f"manifest is not valid YAML/JSON: {exc}")
        else:
            manifest = manifest_body
        if not isinstance(manifest, dict):
            raise HTTPException(400, "manifest must be a mapping")

        errors = validate_manifest(manifest)
        if errors:
            raise HTTPException(
                422, {"message": "manifest failed schema validation", "errors": errors}
            )

        descriptor = compile_manifest(manifest)
        name = descriptor["nodeType"]
        version = descriptor.get("version", "1.0.0")
        execution_type = NodeExecutionType(descriptor["execution_type"])

        # A real build injects the freshly built image ref; default container
        # nodes to the shared executor image, and forbid one for in_process.
        resolved_image = image_ref
        if execution_type == NodeExecutionType.CONTAINER and not resolved_image:
            resolved_image = DEFAULT_CONTAINER_IMAGE
        if execution_type == NodeExecutionType.IN_PROCESS:
            resolved_image = None

        # Upsert on (name, version): re-publishing the same version updates in
        # place, a new version inserts a new row.
        existing = session.exec(
            select(NodeType)
            .where(NodeType.name == name)
            .where(NodeType.version == version)
        ).one_or_none()
        row = existing or NodeType(name=name, version=version)
        if existing is None:
            session.add(row)

        row.display_name = descriptor.get("displayName", name)
        row.category = NodeCategory(descriptor["category"])
        row.execution_type = execution_type
        row.image_ref = resolved_image
        row.descriptor = descriptor
        row.enabled = True
        row.updated_at = datetime.utcnow()

        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, f"registration violates a constraint: {exc.orig}")
        session.refresh(row)
        return {"data": _record(row)}

    @app.get("/api/v1/node-types")
    def list_node_types(
        category: str | None = None,
        execution_type: str | None = None,
        enabled: bool | None = None,
        view: str | None = Query(
            None, description="Set to 'palette' for React Flow drawer summaries."
        ),
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """List node types with the documented ``{data, meta}`` envelope."""
        stmt = select(NodeType)
        try:
            if category is not None:
                stmt = stmt.where(NodeType.category == NodeCategory(category))
            if execution_type is not None:
                stmt = stmt.where(
                    NodeType.execution_type == NodeExecutionType(execution_type)
                )
        except ValueError as exc:
            raise HTTPException(400, f"invalid filter value: {exc}")
        if enabled is not None:
            stmt = stmt.where(NodeType.enabled == enabled)

        rows = list(
            session.exec(stmt.order_by(NodeType.name, NodeType.version)).all()
        )

        if view == "palette":
            # Drawer view: one entry per node (latest version), UI camelCase shape.
            summaries = [_palette_summary(row) for row in _latest_per_name(rows)]
            return {
                "data": summaries,
                "meta": {"total": len(summaries), "limit": limit, "offset": offset},
            }

        page = rows[offset : offset + limit]
        return {
            "data": [_list_item(row) for row in page],
            "meta": {"total": len(rows), "limit": limit, "offset": offset},
        }

    @app.get("/api/v1/node-types/{id_or_name}")
    def get_node_type(
        id_or_name: str,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Fetch a single node type record (id- or name-addressable)."""
        return {"data": _record(_resolve(session, id_or_name))}

    @app.get("/api/v1/node-types/{id_or_name}/descriptor")
    def get_node_descriptor(
        id_or_name: str,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Return the raw compiled descriptor, unwrapped, for the canvas renderer.

        This is the one deliberate exception to the standard envelope: the React
        Flow canvas consumes ``node-definition.json`` verbatim to render forms,
        default values, field groupings, and port bindings.
        """
        return _resolve(session, id_or_name).descriptor

    return app


def make_engine(url: str | None = None):
    """Create an engine + schema. In-memory SQLite by default; Postgres if given."""
    if url:
        engine = create_engine(url)
    else:
        # A single shared in-memory connection so the schema persists across the
        # separate sessions each TestClient request opens.
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    RegistryModel.metadata.drop_all(engine)
    RegistryModel.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def engine():
    eng = make_engine(os.environ.get(POSTGRES_URL_ENV) or None)
    try:
        yield eng
    finally:
        RegistryModel.metadata.drop_all(eng)


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app(engine))


def _register_manifest(client: TestClient):
    """POST the on-disk http-request manifest as raw YAML."""
    return client.post(
        "/api/v1/node-types",
        content=MANIFEST_PATH.read_text(),
        headers={"Content-Type": "application/yaml"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_register_http_request_succeeds(client: TestClient) -> None:
    """Test 1: POST /node-types successfully registers the http-request node."""
    response = _register_manifest(client)
    assert response.status_code == 201, response.text
    record = response.json()["data"]
    assert record["name"] == "http_request"
    assert record["execution_type"] == "container"
    assert record["enabled"] is True
    # Container node was assigned the default executor image so the DDL CHECK
    # (container => image_ref present) is satisfied.
    assert record["image_ref"] == DEFAULT_CONTAINER_IMAGE
    UUID(record["id"])  # id is a real UUID


def test_palette_summary_matches_ui_contract(client: TestClient) -> None:
    """Test 2: GET /node-types?view=palette returns the React Flow drawer summary."""
    assert _register_manifest(client).status_code == 201

    response = client.get("/api/v1/node-types", params={"view": "palette"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"data", "meta"}
    assert payload["meta"] == {"total": 1, "limit": 50, "offset": 0}

    summaries = payload["data"]
    assert isinstance(summaries, list) and len(summaries) == 1
    summary = summaries[0]
    assert set(summary) == {
        "name",
        "displayName",
        "category",
        "executionType",
        "version",
        "description",
        "icon",
    }
    assert summary["name"] == "http_request"
    assert summary["displayName"] == "HTTP Request"
    assert summary["category"] == "action"
    assert summary["executionType"] == "container"
    assert summary["version"] == "1.0.0"
    assert summary["icon"] == "globe"
    # Description is collapsed to a single palette-friendly line.
    assert "\n" not in summary["description"]
    assert summary["description"].startswith("Non-blocking HTTP/HTTPS API orchestrator")


def test_list_envelope_matches_documented_contract(client: TestClient) -> None:
    """The default list view uses the documented {data, meta} + filters contract."""
    assert _register_manifest(client).status_code == 201

    response = client.get(
        "/api/v1/node-types", params={"category": "action", "enabled": "true"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["meta"] == {"total": 1, "limit": 50, "offset": 0}
    item = payload["data"][0]
    assert set(item) == {"id", "name", "category", "execution_type", "enabled"}
    assert item["execution_type"] == "container"

    # A non-matching filter yields an empty page, not an error.
    empty = client.get("/api/v1/node-types", params={"category": "trigger"}).json()
    assert empty["data"] == [] and empty["meta"]["total"] == 0


def test_fetch_record_by_id_and_by_name(client: TestClient) -> None:
    """A record is addressable by UUID and by its node name (the UI alias)."""
    record = _register_manifest(client).json()["data"]
    node_id = record["id"]

    by_id = client.get(f"/api/v1/node-types/{node_id}")
    by_name = client.get("/api/v1/node-types/http_request")
    assert by_id.status_code == by_name.status_code == 200
    assert by_id.json()["data"]["id"] == by_name.json()["data"]["id"] == node_id
    assert "descriptor" in by_id.json()["data"]


def test_canvas_descriptor_exposes_inputs_and_output_envelope(
    client: TestClient,
) -> None:
    """Test 3: GET /node-types/{name}/descriptor returns the raw descriptor."""
    assert _register_manifest(client).status_code == 201

    response = client.get("/api/v1/node-types/http_request/descriptor")
    assert response.status_code == 200, response.text
    descriptor = response.json()  # raw node-definition.json, unwrapped

    # Draft-07 inputs schema is present for dynamic form rendering.
    inputs = descriptor["inputs"]
    assert "url" in inputs["properties"]
    assert inputs["properties"]["url"]["type"] == "string"
    assert inputs["required"] == ["url", "method"]

    # Output envelope references the immutable StandardOutputWrapper.
    assert descriptor["outputs"]["allOf"][0]["$ref"].endswith("StandardOutputWrapper")
    assert "Result" in descriptor["outputs"]["properties"]


def test_check_constraint_rejects_container_without_image_ref(engine) -> None:
    """Test 4: a container node with image_ref=None violates the CHECK constraint."""
    descriptor = compile_manifest()
    with Session(engine) as session:
        session.add(
            NodeType(
                name="http_request_bad",
                display_name="HTTP Request (bad)",
                category=NodeCategory.ACTION,
                execution_type=NodeExecutionType.CONTAINER,
                image_ref=None,  # violates: container requires image_ref
                descriptor=descriptor,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_register_same_version_is_idempotent_upsert(client: TestClient) -> None:
    """Re-registering the same (name, version) updates in place, no duplicate."""
    assert _register_manifest(client).status_code == 201
    assert _register_manifest(client).status_code == 201
    payload = client.get("/api/v1/node-types").json()
    assert payload["meta"]["total"] == 1


def test_get_unknown_node_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/node-types/does_not_exist").status_code == 404
    assert (
        client.get("/api/v1/node-types/does_not_exist/descriptor").status_code == 404
    )


def test_register_rejects_invalid_manifest(client: TestClient) -> None:
    """A manifest missing required taxonomy fields is rejected at validation."""
    broken = {"nodeType": "broken", "inputs": {"properties": {}}}
    response = client.post("/api/v1/node-types", json=broken)
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Standalone runner (mirrors pytest, prints a readable summary)
# ---------------------------------------------------------------------------
def _run_standalone() -> int:
    def fresh_client() -> TestClient:
        return TestClient(
            create_app(make_engine(os.environ.get(POSTGRES_URL_ENV) or None))
        )

    def fresh_engine():
        return make_engine(os.environ.get(POSTGRES_URL_ENV) or None)

    cases: list[tuple[str, Any]] = [
        ("test_register_http_request_succeeds", lambda: test_register_http_request_succeeds(fresh_client())),
        ("test_palette_summary_matches_ui_contract", lambda: test_palette_summary_matches_ui_contract(fresh_client())),
        ("test_list_envelope_matches_documented_contract", lambda: test_list_envelope_matches_documented_contract(fresh_client())),
        ("test_fetch_record_by_id_and_by_name", lambda: test_fetch_record_by_id_and_by_name(fresh_client())),
        ("test_canvas_descriptor_exposes_inputs_and_output_envelope", lambda: test_canvas_descriptor_exposes_inputs_and_output_envelope(fresh_client())),
        ("test_check_constraint_rejects_container_without_image_ref", lambda: test_check_constraint_rejects_container_without_image_ref(fresh_engine())),
        ("test_register_same_version_is_idempotent_upsert", lambda: test_register_same_version_is_idempotent_upsert(fresh_client())),
        ("test_get_unknown_node_returns_404", lambda: test_get_unknown_node_returns_404(fresh_client())),
        ("test_register_rejects_invalid_manifest", lambda: test_register_rejects_invalid_manifest(fresh_client())),
    ]
    passed = skipped = failed = 0
    for name, run in cases:
        try:
            run()
        except pytest.skip.Exception as exc:  # type: ignore[attr-defined]
            skipped += 1
            print(f"  - {name}: SKIP ({exc})")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ✓ {name}")
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
