"""Golden conformance harness — runs the canonical contract corpus
(vendored from @openmaxai/openmax-agent-sdk, see contract/PROVENANCE.md)
through this SDK's REAL code paths.

Passing this corpus is, per upstream CONTRACT.md, the definition of
"protocol-conformant" for any SDK in any language.
"""

import json
from pathlib import Path

from cws_agent_sdk.codec import classify_frame, classify_system_event
from cws_agent_sdk.contract import normalize_for_contract

import pytest

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).parent.parent / "contract"
FIXTURES = ROOT / "fixtures" / "v1"
SCHEMAS = ROOT / "schemas" / "v1"


def _load(dirname):
    d = FIXTURES / dirname
    return sorted(d.glob("*.json"))


def _schema(name):
    return json.loads((SCHEMAS / name).read_text())


def _validator(schema):
    """Validator with all local schemas registered by their $id (offline $ref)."""
    from referencing import Registry, Resource

    resources = []
    for p in SCHEMAS.glob("*.schema.json"):
        doc = json.loads(p.read_text())
        if doc.get("$id"):
            resources.append((doc["$id"], Resource.from_contents(doc)))
    registry = Registry().with_resources(resources)
    return jsonschema.Draft202012Validator(schema, registry=registry)


def _validate(instance, schema):
    _validator(schema).validate(instance)


# -- frame classification ------------------------------------------------------


@pytest.mark.parametrize("path", _load("frame-classification"), ids=lambda p: p.stem)
def test_frame_classification(path):
    fx = json.loads(path.read_text())
    assert classify_frame(fx["input"]) == fx["expected"], fx.get("description")


# -- system event classification ------------------------------------------------


@pytest.mark.parametrize(
    "path", _load("system-event-classification"), ids=lambda p: p.stem
)
def test_system_event_classification(path):
    fx = json.loads(path.read_text())
    raw = fx["input"]
    event = raw if isinstance(raw, str) else raw.get("event", "")
    assert classify_system_event(event) == fx["expected"], fx.get("description")


# -- normalized inbound message --------------------------------------------------


def _assert_subset(expected: dict, actual: dict, ctx: str):
    for key, want in expected.items():
        if key == "decisionReasonIncludes":
            assert want in actual["decision"]["reason"], (
                f"{ctx}: decision.reason {actual['decision']['reason']!r} "
                f"does not include {want!r}"
            )
        elif key == "decisionOwnerNameHint":
            assert actual["decision"].get("ownerNameHint") == want, ctx
        elif key == "senderIdAbsent":
            assert "senderId" not in actual, f"{ctx}: senderId should be absent"
        else:
            assert actual.get(key) == want, (
                f"{ctx}: field {key!r} = {actual.get(key)!r}, want {want!r}"
            )


def test_contract_does_not_emit_deprecated_org_slug():
    """SDK alpha.2 uses org_id/orgId as the sole runtime organization key."""
    fx = json.loads((_load("inbound-message")[0]).read_text())
    actual = normalize_for_contract(
        fx["org"], fx["frame"], fx["detail"], fx["conversation"]
    )
    assert actual["orgId"] == fx["org"]["org_id"]
    assert "orgSlug" not in actual


# -- inbound message normalization ----------------------------------------------


@pytest.mark.parametrize("path", _load("inbound-message"), ids=lambda p: p.stem)
def test_inbound_message_normalization(path):
    fx = json.loads(path.read_text())
    via = fx.get("via") or ("sync" if "sync" in path.stem else "ws")
    actual = normalize_for_contract(
        fx.get("org") or {},
        fx.get("frame") or {},
        fx.get("detail") or {},
        fx.get("conversation"),
        via=via,
    )
    _assert_subset(fx["expected"], actual, path.stem)
    _validate(actual, _schema("inbound-message.schema.json"))


# -- wake request / result (schema conformance) -----------------------------------


@pytest.mark.parametrize("path", _load("wake-request"), ids=lambda p: p.stem)
def test_wake_request_schema(path):
    fx = json.loads(path.read_text())
    schema = _schema("wake-request.schema.json")
    if fx.get("expectValid", True):
        _validate(fx["input"], schema)
    else:
        with pytest.raises(jsonschema.ValidationError):
            _validate(fx["input"], schema)


@pytest.mark.parametrize("path", _load("wake-result"), ids=lambda p: p.stem)
def test_wake_result_schema(path):
    fx = json.loads(path.read_text())
    schema = _schema("wake-result.schema.json")
    if fx.get("expectValid", True):
        _validate(fx["input"], schema)
    else:
        with pytest.raises(jsonschema.ValidationError):
            _validate(fx["input"], schema)
