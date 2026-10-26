"""Pin the origin-guard invariant for every non-GET route of the web UI backend.

5a enumerates the FastAPI routes straight out of ``web_ui/backend/*.py`` with
``ast`` (no imports, no regex) and asserts that every non-GET route refuses
foreign origins: it either calls ``_origin_check_request`` inline, or it is one
of the pinned ``STRICT_ORIGIN_NON_GET`` routes that use the stricter
``_origin_allowed`` helper.  5b cross-checks that enumeration against
``app.openapi()``, so a route cannot reach the running app without also being
seen by the AST scan.  5c resolves the pinned tuple lists back to their source
decorator paths, and 5d samples one route per mechanism shape behaviourally.

The subject of the test is the invariant, not a fixed list of routes: adding a
route must show up in the scan automatically. If this test can be made green by removing a route's guard, the test is wrong. If it can be made green by adding a route without the guard, the test is wrong.
"""

import ast
import sys
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "web_ui" / "backend"

ROUTE_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
ROUTE_METHODS_UPPER = frozenset(method.upper() for method in ROUTE_METHODS)
GET = "GET"
ORIGIN_PREDICATES = frozenset({"_origin_check_request", "_origin_allowed"})

# GET routes that touch the filesystem and therefore carry a guard as well.
# (handler_function_name, full_openapi_path, predicate_name); the handler name is
# spelled exactly as the function is defined in the source.
FILESYSTEM_GET_MUST_GUARD = [
    ("vault_repair_status", "/api/vault/repair/status", "_origin_allowed"),
    ("browse_directory", "/api/browse", "_origin_check_request"),
]

# Non-GET routes guarded by the stricter helper instead of _origin_check_request.
# (handler_function_name, full_openapi_path, predicate_name)
STRICT_ORIGIN_NON_GET = [
    ("vault_repair_apply", "/api/vault/repair/apply", "_origin_allowed"),
]

# Behavioural sample: one route per mechanism shape (POST-with-body, POST-no-body,
# PUT, DELETE).  ``route_label`` is the handler function name, resolved to
# (method, path) with the same AST prefix resolution as 5a/5c - never by
# importing the handler - so a renamed or moved handler fails here loudly.
# (route_label, mechanism_shape, body_builder_or_None)
REPRESENTATIVE_FOREIGN_ORIGIN_SAMPLE = [
    # POST-with-body: resolve_workspace_path takes no path or query parameter, so the only input that can pre-empt the handler is a body schema failure; {"path": ...} satisfies ResolvePathBody.
    ("resolve_workspace_path", "POST-with-body", lambda: {"path": "/tmp"}),
    # POST-no-body: complete_onboarding takes nothing but the request, so there is no body validation that could 422 before the guard runs.
    ("complete_onboarding", "POST-no-body", None),
    # PUT: put_dockerfile takes one path parameter and no body model, so it cannot 422 before the guard, and the guard fires before it would write anything.
    ("put_dockerfile", "PUT", None),
    # DELETE: delete_provider with an unknown provider_id is a no-op if it is ever reached, and the guard fires long before that.
    ("delete_provider", "DELETE", None),
]

# The four pinned shape labels and the HTTP method each one must resolve to.
EXPECTED_METHOD_BY_SHAPE = {
    "POST-with-body": "POST",
    "POST-no-body": "POST",
    "PUT": "PUT",
    "DELETE": "DELETE",
}

_Route = namedtuple("_Route", "method path handler module line predicates")


def _callee_name(call):
    """Return the bare name of a call's callee (``foo`` for ``foo()`` and ``x.foo()``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_bindings(tree):
    """Return (router_prefixes, app_objects) bound at module level in ``tree``."""
    prefixes = {}
    app_objects = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        binding = _callee_name(node.value)
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if binding == "APIRouter":
            prefix = ""
            for keyword in node.value.keywords:
                value = keyword.value
                if keyword.arg == "prefix" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    prefix = value.value
            for name in names:
                prefixes[name] = prefix
        elif binding == "FastAPI":
            app_objects.update(names)
    return prefixes, app_objects


def _route_decorators(function):
    """Yield (method_upper, path_literal, owner_name) for each route decorator on ``function``."""
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        method = decorator.func.attr
        if method not in ROUTE_METHODS:
            continue
        if not decorator.args:
            continue
        literal = decorator.args[0]
        if not (isinstance(literal, ast.Constant) and isinstance(literal.value, str)):
            continue
        owner = decorator.func.value
        yield method.upper(), literal.value, (owner.id if isinstance(owner, ast.Name) else None)


def _guard_calls(function):
    """Return the set of origin predicates called anywhere inside ``function``."""
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name in ORIGIN_PREDICATES:
                found.add(name)
    return found


def _enumerate_routes():
    """Enumerate the routes declared in ``web_ui/backend/*.py`` from the AST alone.

    Returns ``(routes, un_addable_handlers)``; ``un_addable_handlers`` collects the
    non-GET routes whose decorator path could not be resolved to a full API path,
    so that a route can never dodge the invariant by being invisible to the scan.
    """
    routes = []
    un_addable_handlers = []
    for source in sorted(BACKEND_DIR.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        prefixes, app_objects = _module_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for method, literal, owner in _route_decorators(node):
                if owner in prefixes:
                    full_path = prefixes[owner] + literal
                elif owner in app_objects or literal.startswith("/api/"):
                    full_path = literal
                else:
                    full_path = None
                if full_path is None:
                    if method != GET:
                        un_addable_handlers.append(
                            "{module}:{line} {method} {handler} (decorator path {literal!r})".format(
                                module=source.name, line=node.lineno, method=method, handler=node.name, literal=literal
                            )
                        )
                    continue
                routes.append(
                    _Route(
                        method=method,
                        path=full_path,
                        handler=node.name,
                        module=source.name,
                        line=node.lineno,
                        predicates=frozenset(_guard_calls(node)),
                    )
                )
    return routes, un_addable_handlers


def _routes_by_path(routes):
    """Group ``routes`` by full path."""
    grouped = {}
    for route in routes:
        grouped.setdefault(route.path, []).append(route)
    return grouped


def _ensure_repo_root_on_sys_path():
    """Make ``web_ui`` importable when this file is run outside pytest's rootdir."""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _concretise(path):
    """Replace every ``{path_param}`` placeholder with a literal sample value."""
    segments = []
    for segment in path.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            segments.append("__sample__")
        else:
            segments.append(segment)
    return "/".join(segments)


def test_5a_presence_every_non_get_route_calls_the_origin_guard():
    """Presence: no non-GET route exists without an origin guard (AST only)."""
    routes, un_addable_handlers = _enumerate_routes()
    non_get = [route for route in routes if route.method != GET]
    grouped = _routes_by_path(routes)
    print(
        "5a discovered routes={total} non_get={non_get} guarded_by_origin_check_request={check} "
        "guarded_by_origin_allowed={allowed}".format(
            total=len(routes),
            non_get=len(non_get),
            check=sum(1 for route in non_get if "_origin_check_request" in route.predicates),
            allowed=sum(1 for route in non_get if "_origin_allowed" in route.predicates),
        )
    )

    # (5) Hard escape hatch: a non-GET route the scan cannot resolve would skip
    # the invariant silently, so it fails loudly here, naming module and function.
    assert len(un_addable_handlers) == 0, (
        "non-GET routes that could not be resolved to a full API path (fix the resolver or the route, never skip): "
        + "; ".join(un_addable_handlers)
    )

    # (1a) The invariant itself: every non-GET route calls the general helper, or
    # is one of the pinned strict-helper routes. Never neither.
    strict_paths = {entry[1] for entry in STRICT_ORIGIN_NON_GET}
    unguarded = [
        "{method} {path} -> {handler} ({module}:{line})".format(
            method=route.method, path=route.path, handler=route.handler, module=route.module, line=route.line
        )
        for route in non_get
        if "_origin_check_request" not in route.predicates and route.path not in strict_paths
    ]
    assert unguarded == [], (
        "non-GET routes that neither call _origin_check_request nor appear in STRICT_ORIGIN_NON_GET: "
        + "; ".join(unguarded)
    )

    # (1b) The strict list is exhaustive over the strict helper: a non-GET route may
    # not adopt _origin_allowed without being pinned above, or the list would be a
    # permission slip for one path instead of a census of the strict mechanism.
    unpinned_strict = [
        "{method} {path} -> {handler} ({module}:{line})".format(
            method=route.method, path=route.path, handler=route.handler, module=route.module, line=route.line
        )
        for route in non_get
        if "_origin_allowed" in route.predicates and route.path not in strict_paths
    ]
    assert unpinned_strict == [], (
        "non-GET routes that call _origin_allowed but are missing from STRICT_ORIGIN_NON_GET: "
        + "; ".join(unpinned_strict)
    )

    # FILESYSTEM_GET_MUST_GUARD is a "these GETs must be guarded" list, not an
    # "only these GETs may be guarded" list, so it is deliberately not exhaustive
    # over GET routes that carry a guard - do not add such an assertion.

    # (2) Every STRICT_ORIGIN_NON_GET entry exists, is non-GET and calls _origin_allowed.
    strict_problems = []
    for handler, path, predicate in STRICT_ORIGIN_NON_GET:
        matches = grouped.get(path, [])
        if len(matches) != 1:
            strict_problems.append(f"{path}: expected exactly one route, found {len(matches)}")
            continue
        route = matches[0]
        if route.method == GET:
            strict_problems.append(f"{path}: listed as non-GET but the source decorator is GET")
        if route.handler != handler:
            strict_problems.append(f"{path}: handler is {route.handler}, list says {handler}")
        if predicate not in route.predicates:
            strict_problems.append(
                f"{path}: {route.handler} does not call {predicate} (calls {sorted(route.predicates)})"
            )
    assert strict_problems == [], "STRICT_ORIGIN_NON_GET disagrees with the source: " + "; ".join(strict_problems)

    # (4) A strict-helper route must not also call the general helper, or the
    # stricter refusal could be masked by the weaker check.
    mixed = [
        "{method} {path} -> {handler}".format(method=route.method, path=route.path, handler=route.handler)
        for _, path, _ in STRICT_ORIGIN_NON_GET
        for route in grouped.get(path, [])
        if "_origin_check_request" in route.predicates
    ]
    assert mixed == [], "strict-helper routes that also call _origin_check_request: " + "; ".join(mixed)

    # (3) Every FILESYSTEM_GET_MUST_GUARD entry exists, is GET and calls the named predicate.
    filesystem_problems = []
    for handler, path, predicate in FILESYSTEM_GET_MUST_GUARD:
        matches = grouped.get(path, [])
        if len(matches) != 1:
            filesystem_problems.append(f"{path}: expected exactly one route, found {len(matches)}")
            continue
        route = matches[0]
        if route.method != GET:
            filesystem_problems.append(f"{path}: listed as GET but the source decorator is {route.method}")
        if route.handler != handler:
            filesystem_problems.append(f"{path}: handler is {route.handler}, list says {handler}")
        if predicate not in route.predicates:
            filesystem_problems.append(
                f"{path}: {route.handler} does not call {predicate} (calls {sorted(route.predicates)})"
            )
    assert filesystem_problems == [], "FILESYSTEM_GET_MUST_GUARD disagrees with the source: " + "; ".join(filesystem_problems)


def test_5b_openapi_operations_match_the_ast_enumeration():
    """Parity: the running app exposes exactly the non-GET routes the AST scan sees."""
    _ensure_repo_root_on_sys_path()
    from web_ui.backend.server import app

    routes, un_addable_handlers = _enumerate_routes()
    assert len(un_addable_handlers) == 0, (
        "non-GET routes that could not be resolved to a full API path: " + "; ".join(un_addable_handlers)
    )
    ast_pairs = {(route.method, route.path) for route in routes if route.method != GET}

    openapi_pairs = set()
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            upper = method.upper()
            if upper in ROUTE_METHODS_UPPER:
                openapi_pairs.add((upper, path))
    openapi_non_get = {pair for pair in openapi_pairs if pair[0] != GET}

    only_openapi = sorted(openapi_non_get - ast_pairs)
    only_ast = sorted(ast_pairs - openapi_non_get)
    print(
        "5b openapi_operations={ops} openapi_non_get={openapi} ast_non_get={ast}".format(
            ops=len(openapi_pairs), openapi=len(openapi_non_get), ast=len(ast_pairs)
        )
    )
    assert len(openapi_non_get) == len(ast_pairs), (
        "non-GET operation count mismatch: openapi={openapi} ast={ast}; only_in_openapi={only_openapi}; "
        "only_in_ast={only_ast}".format(openapi=len(openapi_non_get), ast=len(ast_pairs), only_openapi=only_openapi, only_ast=only_ast)
    )
    assert not only_openapi and not only_ast, (
        "(method, path) sets differ: openapi={openapi} ast={ast}; only_in_openapi={only_openapi}; "
        "only_in_ast={only_ast}".format(openapi=len(openapi_non_get), ast=len(ast_pairs), only_openapi=only_openapi, only_ast=only_ast)
    )


def test_5c_pinned_tuples_resolve_to_the_source_decorator_paths():
    """The pinned handler names, methods and paths agree with the AST decorator paths."""
    routes, _ = _enumerate_routes()
    grouped = _routes_by_path(routes)

    for expected_methods, entries in (
        (frozenset({GET}), FILESYSTEM_GET_MUST_GUARD),
        (ROUTE_METHODS_UPPER - {GET}, STRICT_ORIGIN_NON_GET),
    ):
        for handler, path, predicate in entries:
            at_path = grouped.get(path, [])
            matches = [route for route in at_path if route.handler == handler]
            assert len(matches) == 1, "{path}: expected one route defined by {handler}, found handlers {found}".format(
                path=path, handler=handler, found=[route.handler for route in at_path]
            )
            route = matches[0]
            assert route.path == path, f"{handler}: resolved to {route.path}, list says {path}"
            assert route.method in expected_methods, (
                f"{handler}: resolved to {route.method} {route.path} in {route.module}:{route.line}, "
                f"but the list is restricted to {sorted(expected_methods)}"
            )
            assert predicate in ORIGIN_PREDICATES, f"{handler}: unknown origin predicate {predicate}"


def test_5d_foreign_origin_is_refused_once_per_mechanism_shape():
    """Behavioural sample: a foreign Origin is refused with 403 on one route per mechanism shape.

    Behavioural coverage is sampled, not exhaustive. Presence (AST + OpenAPI) proves every non-GET route calls the guard; 5d proves the guard refuses on one route per mechanism shape. 33 of 37 routes are proven by presence only. This is deliberate - exhaustive behavioural coverage would turn 5d into a payload-fixture maintenance burden.
    5d sends valid bodies; a malformed body would return 422 before the handler runs, which does not test the guard.
    """
    _ensure_repo_root_on_sys_path()
    from fastapi.testclient import TestClient
    from web_ui.backend.server import app

    routes, _ = _enumerate_routes()
    by_handler = {}
    for route in routes:
        by_handler.setdefault(route.handler, []).append(route)

    shapes = [mechanism_shape for _, mechanism_shape, _ in REPRESENTATIVE_FOREIGN_ORIGIN_SAMPLE]
    assert len(shapes) == 4, f"expected exactly 4 sampled shapes, found {shapes}"
    assert shapes == list(EXPECTED_METHOD_BY_SHAPE), (
        f"the sample must cover each pinned shape exactly once ({list(EXPECTED_METHOD_BY_SHAPE)}), found {shapes}"
    )
    client = TestClient(app)
    for route_label, mechanism_shape, body_builder in REPRESENTATIVE_FOREIGN_ORIGIN_SAMPLE:
        assert mechanism_shape in EXPECTED_METHOD_BY_SHAPE, f"unknown mechanism shape {mechanism_shape!r}"
        expected_method = EXPECTED_METHOD_BY_SHAPE[mechanism_shape]
        matches = by_handler.get(route_label, [])
        assert len(matches) == 1, f"{route_label}: expected one route, found {[(route.method, route.path) for route in matches]}"
        route = matches[0]
        assert route.method == expected_method, (
            f"{route_label}: sample declares mechanism shape {mechanism_shape} (expected {expected_method}) "
            f"but the route is {route.method} {route.path}"
        )
        assert route.method != GET, f"{route_label}: the sample must exercise a non-GET route, got {route.method} {route.path}"
        request_kwargs = {"headers": {"Origin": "http://evil.example"}}
        if body_builder is not None:
            request_kwargs["json"] = body_builder()
        response = getattr(client, route.method.lower())(_concretise(route.path), **request_kwargs)
        assert response.status_code == 403, (
            f"{mechanism_shape} {route.method} {route.path} ({route.handler}) answered {response.status_code}, expected 403; "
            "a 404 or 405 means the sampled path or method is wrong, not that the assertion should be relaxed"
        )
