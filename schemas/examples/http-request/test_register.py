#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml>=6.0",
#     "jsonschema>=4.18",
#     "sqlmodel>=0.0.14",
#     "pytest>=8.0",
#     "psycopg[binary]>=3.1",
# ]
# ///
"""End-to-end registration test for the ``http_request`` node prototype.

This harness validates the Node SDK Architecture pipeline for a single node,
from authoring source to registry row:

1. **Validate** ``manifest.yaml`` against ``common-definitions.json`` - the sole
   platform meta-schema - using ``jsonschema`` (Draft-07). A node meta-schema
   ``$ref``s the shared enums/structures so the manifest's taxonomy fields are
   checked against the canonical definitions.
2. **Compile** ``manifest.yaml`` into ``node-definition.json`` (the build
   artifact the registry persists), mirroring ``build-manifest.sh``.
3. **Register** a ``NodeTypeDescriptor`` SQLModel row carrying the compiled
   descriptor and verify the ``node_types`` CHECK constraint that ties
   ``image_ref`` to ``execution_type`` (container => image required;
   in_process => image forbidden).

Run standalone (fetches deps in an ephemeral env):

    uv run schemas/examples/http-request/test_register.py

Or under pytest once deps are installed:

    pytest schemas/examples/http-request/test_register.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from jsonschema import Draft7Validator
from sqlalchemy import JSON
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlmodel import CheckConstraint, Column, Field, Session, SQLModel, UniqueConstraint, create_engine, select

HERE = Path(__file__).resolve().parent
SCHEMAS_ROOT = HERE.parent.parent  # schemas/
MANIFEST_PATH = HERE / "manifest.yaml"
COMMON_DEFINITIONS_PATH = SCHEMAS_ROOT / "common-definitions.json"
COMPILED_PATH = HERE / "node-definition.json"

# The node runs on the shared, hardened execution-plane HTTP executor image.
# The orchestrator applies adapter.map_result() to translate that image's native
# {ok, status_code, ...} output into the platform StandardOutputWrapper.
IMAGE_REF = "quay.io/ahetheri/http-executor:dev"

# Set this to a PostgreSQL URL to exercise the real JSONB descriptor column and
# the DDL CHECK constraints against Postgres instead of the SQLite default, e.g.
#   SYNTARA_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/syntara_test
POSTGRES_URL_ENV = "SYNTARA_TEST_DATABASE_URL"


# ---------------------------------------------------------------------------
# Registry model - mirrors NodeTypeDescriptor from node-sdk-architecture.md.
# The descriptor column keeps JSONB on PostgreSQL but falls back to generic
# JSON on SQLite so the same model is testable without a live Postgres.
# ---------------------------------------------------------------------------
class NodeCategory(StrEnum):
    ACTION = "action"
    TASK = "task"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"


class NodeExecutionType(StrEnum):
    IN_PROCESS = "in_process"
    CONTAINER = "container"


class NodeTypeDescriptor(SQLModel, table=True):
    """Registry record for a single compiled node definition."""

    __tablename__ = "node_types"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_node_types_name_version"),
        CheckConstraint(
            "(execution_type = 'container' AND image_ref IS NOT NULL) OR "
            "(execution_type = 'in_process' AND image_ref IS NULL)",
            name="node_types_image_ref_by_execution_type",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True)
    # Store the enum *value* ("container") as TEXT + CHECK IN (...), matching the
    # node_types DDL in node-sdk-architecture.md. native_enum=False keeps this
    # portable across SQLite and PostgreSQL (no native ENUM type to manage) and
    # persists the value, not the member name ("CONTAINER").
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
    descriptor: dict[str, Any] = Field(
        sa_column=Column(JSONB().with_variant(JSON(), "sqlite"), nullable=False)
    )
    image_ref: str | None = Field(default=None)
    version: str = Field(default="1.0.0")
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_manifest() -> dict[str, Any]:
    with MANIFEST_PATH.open() as handle:
        return yaml.safe_load(handle)


def load_common_definitions() -> dict[str, Any]:
    with COMMON_DEFINITIONS_PATH.open() as handle:
        return json.load(handle)


def build_validator() -> Draft7Validator:
    """Build a node meta-schema validator wired to common-definitions.json.

    The node meta-schema deliberately references the shared platform types via
    ``$ref`` (exactly as the architecture requires of every manifest), so the
    manifest's taxonomy fields are validated against the single source of truth
    rather than re-declared here.
    """
    common = load_common_definitions()
    node_schema: dict[str, Any] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "nodeType": {"type": "string", "minLength": 1},
            "category": {"$ref": "common-definitions.json#/definitions/NodeCategory"},
            "execution_type": {
                "$ref": "common-definitions.json#/definitions/NodeExecutionType"
            },
            "inputs": {"type": "object"},
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
    # RefResolver on older installs so this test is portable.
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


def compile_manifest() -> dict[str, Any]:
    """Compile manifest.yaml -> node-definition.json (build artifact)."""
    descriptor = load_manifest()
    with COMPILED_PATH.open("w") as handle:
        json.dump(descriptor, handle, indent=2)
    return descriptor


def make_engine(url: str = "sqlite://"):
    engine = create_engine(url)
    # Start from a clean schema (Postgres persists across runs; SQLite is fresh).
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_manifest_validates_against_common_definitions() -> None:
    manifest = load_manifest()
    validator = build_validator()
    errors = sorted(validator.iter_errors(manifest), key=lambda e: e.path)
    assert not errors, "manifest failed validation:\n" + "\n".join(
        f"  - {'/'.join(map(str, e.path))}: {e.message}" for e in errors
    )


def test_manifest_declares_expected_taxonomy() -> None:
    manifest = load_manifest()
    assert manifest["nodeType"] == "http_request"
    assert manifest["category"] == "action"
    assert manifest["execution_type"] == "container"
    assert manifest["metadata"]["workload_classification"] == "action"


def test_compile_produces_required_definition_keys() -> None:
    descriptor = compile_manifest()
    for key in ("nodeType", "category", "execution_type", "inputs", "outputs"):
        assert key in descriptor, f"compiled definition missing '{key}'"
    assert COMPILED_PATH.exists()
    # Compiled artifact must round-trip as JSON.
    json.loads(COMPILED_PATH.read_text())


def test_register_container_node_succeeds() -> None:
    descriptor = compile_manifest()
    engine = make_engine()
    with Session(engine) as session:
        record = NodeTypeDescriptor(
            name="http_request",
            category=NodeCategory.ACTION,
            execution_type=NodeExecutionType.CONTAINER,
            image_ref=IMAGE_REF,
            descriptor=descriptor,
        )
        session.add(record)
        session.commit()
        session.refresh(record)

        fetched = session.exec(
            select(NodeTypeDescriptor).where(NodeTypeDescriptor.name == "http_request")
        ).one()
        assert fetched.id is not None
        assert fetched.image_ref == IMAGE_REF
        assert fetched.descriptor["nodeType"] == "http_request"
        assert fetched.descriptor["outputs"]["allOf"][0]["$ref"].endswith(
            "StandardOutputWrapper"
        )


def test_check_constraint_rejects_container_without_image_ref() -> None:
    descriptor = compile_manifest()
    engine = make_engine()
    with Session(engine) as session:
        record = NodeTypeDescriptor(
            name="http_request",
            category=NodeCategory.ACTION,
            execution_type=NodeExecutionType.CONTAINER,
            image_ref=None,  # violates the CHECK constraint
            descriptor=descriptor,
        )
        session.add(record)
        with pytest.raises(IntegrityError):
            session.commit()


def test_check_constraint_rejects_in_process_with_image_ref() -> None:
    descriptor = compile_manifest()
    engine = make_engine()
    with Session(engine) as session:
        record = NodeTypeDescriptor(
            name="http_request",
            category=NodeCategory.ACTION,
            execution_type=NodeExecutionType.IN_PROCESS,
            image_ref=IMAGE_REF,  # forbidden for in_process
            descriptor=descriptor,
        )
        session.add(record)
        with pytest.raises(IntegrityError):
            session.commit()


def test_register_and_constraints_on_postgres() -> None:
    """Exercise the real JSONB column + DDL CHECK constraints on PostgreSQL.

    Skips unless SYNTARA_TEST_DATABASE_URL points at a reachable Postgres. This
    is the DB-configuration verification path: the descriptor lands in a real
    JSONB column and the container/image_ref CHECK is enforced by Postgres DDL.
    """
    url = os.environ.get(POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"set {POSTGRES_URL_ENV} to a PostgreSQL URL to run this path")

    try:
        engine = make_engine(url)
    except Exception as exc:  # noqa: BLE001 - connection issues => skip, not fail
        pytest.skip(f"could not connect to {POSTGRES_URL_ENV}: {exc}")

    descriptor = compile_manifest()
    try:
        # Valid container node registers and round-trips through JSONB.
        with Session(engine) as session:
            session.add(
                NodeTypeDescriptor(
                    name="http_request",
                    category=NodeCategory.ACTION,
                    execution_type=NodeExecutionType.CONTAINER,
                    image_ref=IMAGE_REF,
                    descriptor=descriptor,
                )
            )
            session.commit()
            fetched = session.exec(
                select(NodeTypeDescriptor).where(NodeTypeDescriptor.name == "http_request")
            ).one()
            assert fetched.descriptor["nodeType"] == "http_request"

        # DDL CHECK: container node without image_ref is rejected by Postgres.
        with Session(engine) as session:
            session.add(
                NodeTypeDescriptor(
                    name="http_request",
                    version="1.0.1",
                    category=NodeCategory.ACTION,
                    execution_type=NodeExecutionType.CONTAINER,
                    image_ref=None,
                    descriptor=descriptor,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        SQLModel.metadata.drop_all(engine)


# ---------------------------------------------------------------------------
# Standalone runner (mirrors pytest, prints a readable summary)
# ---------------------------------------------------------------------------
def _run_standalone() -> int:
    tests = [
        test_manifest_validates_against_common_definitions,
        test_manifest_declares_expected_taxonomy,
        test_compile_produces_required_definition_keys,
        test_register_container_node_succeeds,
        test_check_constraint_rejects_container_without_image_ref,
        test_check_constraint_rejects_in_process_with_image_ref,
        test_register_and_constraints_on_postgres,
    ]
    passed = skipped = failed = 0
    for test in tests:
        try:
            test()
        except pytest.skip.Exception as exc:  # type: ignore[attr-defined]
            skipped += 1
            print(f"  - {test.__name__}: SKIP ({exc})")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"  ✗ {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ✓ {test.__name__}")
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
