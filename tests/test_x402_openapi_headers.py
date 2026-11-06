"""OpenAPI declares both x402 versions' payment headers on every paid route (#609, #428).

The declarations are documentation only (`openapi_extra`): these tests also
check that the schema is otherwise exactly what FastAPI generates without them,
and that no route gained a header parameter, so validation, 422s and payment
handling are unchanged. The runtime goldens (test_x402_v1_golden*.py) cover the
responses themselves.
"""
import copy
import json

import pytest
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from app.api.endpoints import chunks, data, pool, stamps, stamps_for_owner
from app.main import app
from app.x402 import middleware
from app.x402.codec_v2 import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER
from app.x402.middleware import PROTECTED_ENDPOINTS, is_protected_endpoint
from app.x402.openapi_docs import (
    PAYMENT_REQUIRED_HEADERS, RECEIPT_HEADERS, REQUEST_HEADERS, X_PAYMENT_RESPONSE_HEADER,
    X_PAYMENT_TRANSACTION_HEADER, paid_route_openapi,
)
from app.x402.payment_headers import PAYMENT_SIGNATURE_HEADER, X_PAYMENT_HEADER

PAID_OPERATIONS = {
    ("post", "/api/v1/stamps/"),
    ("patch", "/api/v1/stamps/{stamp_id}/extend"),
    ("post", "/api/v1/stamps/for-owner"),
    ("post", "/api/v1/data/"),
    ("post", "/api/v1/data/manifest"),
    ("post", "/api/v1/chunks/credit"),
    ("post", "/api/v1/pool/acquire"),
}
OPTIONAL_PAYMENT = {("post", "/api/v1/pool/acquire")}

# The routers the paid routes are declared on. app.routes holds FastAPI's
# included-router wrappers, not these APIRoutes.
ENDPOINT_ROUTERS = (stamps.router, stamps_for_owner.router, data.router, chunks.router, pool.router)


def _endpoint_routes():
    return [r for router in ENDPOINT_ROUTERS for r in router.routes if isinstance(r, APIRoute)]


@pytest.fixture(scope="module")
def schema():
    return app.openapi()


def _operations(schema):
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            yield method, path, op


def _payment_params(op):
    names = {n.lower() for n in REQUEST_HEADERS}
    return [p for p in op.get("parameters", []) if p["in"] == "header" and p["name"].lower() in names]


def test_header_names():
    assert REQUEST_HEADERS == ("X-PAYMENT", "PAYMENT-SIGNATURE")
    assert PAYMENT_REQUIRED_HEADERS == ("PAYMENT-REQUIRED",)
    assert RECEIPT_HEADERS == ("X-PAYMENT-RESPONSE", "PAYMENT-RESPONSE", "X-Payment-Transaction")
    assert X_PAYMENT_RESPONSE_HEADER == middleware.X_PAYMENT_RESPONSE_HEADER
    assert (X_PAYMENT_HEADER, PAYMENT_SIGNATURE_HEADER) == REQUEST_HEADERS
    assert (PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER) == ("PAYMENT-REQUIRED", "PAYMENT-RESPONSE")
    assert X_PAYMENT_TRANSACTION_HEADER == "X-Payment-Transaction"


def test_the_paid_operations_are_the_protected_endpoints(schema):
    """Every operation x402 prices is in PAID_OPERATIONS, so a new paid route
    without the declarations fails here."""
    priced = {(m, p) for m, p, _ in _operations(schema) if is_protected_endpoint(m.upper(), p)}
    assert priced == PAID_OPERATIONS
    assert {m.lower() for m, _ in PROTECTED_ENDPOINTS} == {m for m, _ in PAID_OPERATIONS}


@pytest.mark.parametrize("method,path", sorted(PAID_OPERATIONS))
def test_request_headers_of_both_versions_are_declared_optional(schema, method, path):
    op = schema["paths"][path][method]
    params = _payment_params(op)
    assert [p["name"] for p in params] == list(REQUEST_HEADERS)
    for p in params:
        assert p["required"] is False
        assert p["schema"] == {"type": "string"}
        assert "PAYMENT_HEADER_CONFLICT" in p["description"]
    v1, v2 = params
    assert "v1" in v1["description"]
    assert "v2" in v2["description"] and "X402_VERSION_UNSUPPORTED" in v2["description"]
    if (method, path) in OPTIONAL_PAYMENT:
        assert all("optional" in p["description"] for p in params)
    else:
        assert all("gets 402" in p["description"] for p in params)


@pytest.mark.parametrize("method,path", sorted(PAID_OPERATIONS))
def test_402_declares_payment_required(schema, method, path):
    resp = schema["paths"][path][method]["responses"]["402"]
    assert resp["description"]
    assert list(resp["headers"]) == [PAYMENT_REQUIRED_HEADER]
    assert resp["headers"][PAYMENT_REQUIRED_HEADER]["schema"] == {"type": "string"}
    assert "v2" in resp["headers"][PAYMENT_REQUIRED_HEADER]["description"]
    assert ("Only when a payment header is sent" in resp["description"]) == ((method, path) in OPTIONAL_PAYMENT)


@pytest.mark.parametrize("method,path", sorted(PAID_OPERATIONS))
def test_every_success_response_declares_the_receipts(schema, method, path):
    responses = schema["paths"][path][method]["responses"]
    success = [code for code in responses if code.startswith("2")]
    assert success
    for code in success:
        resp = responses[code]
        assert resp.get("description"), (path, code)  # a declared response, not one made up
        assert list(resp["headers"]) == list(RECEIPT_HEADERS), (path, code)
        for header in resp["headers"].values():
            assert header["schema"] == {"type": "string"} and header["description"]


def test_for_owner_202_has_the_receipts_too(schema):
    responses = schema["paths"]["/api/v1/stamps/for-owner"]["post"]["responses"]
    assert set(responses) >= {"201", "202", "402"}


def test_unpaid_operations_declare_none_of_it(schema):
    header_names = {h.lower() for h in REQUEST_HEADERS + PAYMENT_REQUIRED_HEADERS + RECEIPT_HEADERS}
    for method, path, op in _operations(schema):
        if (method, path) in PAID_OPERATIONS:
            continue
        text = json.dumps(op).lower()
        assert not _payment_params(op), (method, path)
        assert "402" not in op.get("responses", {}), (method, path)
        for resp in op.get("responses", {}).values():
            assert not header_names & {h.lower() for h in resp.get("headers", {})}, (method, path)
        assert '"name": "payment-signature"' not in text


def test_no_route_gained_a_header_parameter():
    """The headers are schema text, not parameters: FastAPI neither requires nor
    validates them, so no 422 changes."""
    names = {h.lower() for h in REQUEST_HEADERS}

    def header_params(dependant):
        found = [p.alias.lower() for p in dependant.header_params]
        for sub in dependant.dependencies:
            found += header_params(sub)
        return found

    routes = _endpoint_routes()
    assert sum(1 for r in routes if r.openapi_extra) == len(PAID_OPERATIONS)
    for route in routes:
        assert not names & set(header_params(route.dependant)), route.path


def test_the_schema_is_otherwise_what_fastapi_generates(monkeypatch):
    """Strip the declarations from the full schema and it equals the schema
    FastAPI builds when it does not merge the routes' openapi_extra."""
    import fastapi.openapi.utils as openapi_utils

    full = copy.deepcopy(app.openapi())
    extras = {id(r.openapi_extra) for r in _endpoint_routes() if r.openapi_extra}
    skipped = []
    merge = openapi_utils.deep_dict_update

    def merge_all_but_the_extras(main, update):
        if id(update) in extras:
            skipped.append(update)
            return
        merge(main, update)

    monkeypatch.setattr(openapi_utils, "deep_dict_update", merge_all_but_the_extras)
    bare = get_openapi(title=app.title, version=app.version, openapi_version=app.openapi_version,
                       summary=app.summary, description=app.description, terms_of_service=app.terms_of_service,
                       contact=app.contact, license_info=app.license_info, routes=app.routes,
                       webhooks=app.webhooks.routes, tags=app.openapi_tags, servers=app.servers,
                       separate_input_output_schemas=app.separate_input_output_schemas)
    assert len(skipped) == len(PAID_OPERATIONS)
    for method, path in PAID_OPERATIONS:
        op = full["paths"][path][method]
        op["parameters"] = [p for p in op["parameters"] if p not in _payment_params(op)]
        if not op["parameters"]:
            del op["parameters"]
        del op["responses"]["402"]
        for code, resp in op["responses"].items():
            if code.startswith("2"):
                del resp["headers"]
    assert full == bare


def test_each_call_builds_new_objects():
    a = paid_route_openapi([200])
    b = paid_route_openapi([200])
    a["parameters"][0]["description"] = "changed"
    a["responses"]["200"]["headers"].clear()
    a["responses"]["402"]["headers"].clear()
    assert b["parameters"][0]["description"] != "changed"
    assert b["responses"]["200"]["headers"] and b["responses"]["402"]["headers"]


def test_success_codes_are_normalised():
    assert set(paid_route_openapi([201, 202])["responses"]) == {"201", "202", "402"}
    assert paid_route_openapi([200], payment_optional=True)["parameters"][0]["required"] is False
