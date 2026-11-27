"""Authorization coverage. Fails when someone adds an endpoint and forgets the
authentication or membership check, so the rule stays true as the API grows.

Run from the repository root:  pytest
"""
import inspect
import os

os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-north-1")

from fastapi.routing import APIRoute  # noqa: E402

from app import app  # noqa: E402
from core.auth import get_caller, require_member  # noqa: E402
from core.schemas import StrictModel  # noqa: E402

# Routes anyone may call (API Gateway leaves these without an authorizer).
PUBLIC = {"/", "/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

# Routes that need a signed-in user but have no organization in the URL.
CALLER_ONLY = {("GET", "/me"), ("GET", "/orgs"), ("POST", "/orgs")}

# Fields a client must never be able to send.
FORBIDDEN_FIELDS = {"tenant_id", "org_id", "PK", "SK", "created_by", "entity_type"}


def api_routes():
    routes = []

    def collect(router_routes):
        for route in router_routes:
            if isinstance(route, APIRoute):
                routes.append(route)

            # FastAPI 0.165+ lazy included routers
            included_router = getattr(route, "original_router", None)

            if included_router is not None:
                collect(included_router.routes)

    collect(app.routes)
    return routes

def dependency_calls(dependant):
    for sub in dependant.dependencies:
        yield sub.call
        yield from dependency_calls(sub)


def all_models(base):
    for sub in base.__subclasses__():
        yield sub
        yield from all_models(sub)


def test_every_route_is_public_on_purpose_or_authenticated():
    for route in api_routes():
        if route.path in PUBLIC:
            continue
        calls = set(dependency_calls(route.dependant))
        assert get_caller in calls, f"{sorted(route.methods)} {route.path} is not authenticated"


def test_org_routes_check_membership_and_a_permission():
    org_routes = [r for r in api_routes() if "{org_id}" in r.path]
    assert org_routes, "no organization routes found"
    for route in org_routes:
        calls = set(dependency_calls(route.dependant))
        assert require_member in calls, f"{route.path} does not look up membership"
        assert "require(" in inspect.getsource(route.endpoint), \
            f"{sorted(route.methods)} {route.path} never checks a permission"


def test_routes_without_an_org_in_the_path_are_a_known_list():
    for route in api_routes():
        if "{org_id}" in route.path or route.path in PUBLIC:
            continue
        for method in route.methods:
            assert (method, route.path) in CALLER_ONLY, \
                f"{method} {route.path} has no org in the URL and is not in CALLER_ONLY"


def test_clients_cannot_send_tenant_or_key_fields():
    models = list(all_models(StrictModel))
    assert models, "no request models found"
    for model in models:
        assert model.model_config.get("extra") == "forbid", f"{model.__name__} allows extra fields"
        bad = FORBIDDEN_FIELDS & set(model.model_fields)
        assert not bad, f"{model.__name__} accepts {sorted(bad)}"


def test_no_route_takes_an_org_or_tenant_from_the_query_string():
    for route in api_routes():
        names = {p.name for p in route.dependant.query_params}
        assert not names & {"org_id", "tenant_id"}, f"{route.path} reads org from the query"