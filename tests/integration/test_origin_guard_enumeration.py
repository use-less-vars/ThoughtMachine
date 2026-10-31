"""Pin the origin/peer gate to its SINGLE enforcement point.

The web UI backend enforces "is this request from a browser on this machine?"
in exactly one place: ``AuthBoundaryMiddleware`` in
``web_ui/backend/auth_boundary.py``.  Per-route copies of the origin guard (and
per-route peer-address gates on the health probes) used to exist as well; a
second copy of a security decision is a second thing that can drift, so they
were deleted.  This file pins that the single copy is really single, and that
it is composed into the app where it can actually refuse:

P1  no module under ``web_ui/backend/`` other than the three mechanism modules
    calls ``_origin_check_request`` / ``_origin_allowed`` / ``_localhost_only``
    at all -- neither in a route handler nor anywhere else.
P2  the middleware takes both predicates from the mechanism modules (via
    ``ast``), and its own body calls them: the gate is not a re-implementation.
P3  the app's middleware stack puts ``CORSMiddleware`` OUTSIDE the boundary, so
    a credential-less preflight is answered before the gate sees it.
P4  behaviourally, a foreign ``Origin`` is refused with 403 on non-GET routes
    drawn from four different modules, while a loopback ``Origin`` is served.
P5  behaviourally, a non-loopback PEER is refused with 403 on the health
    probes, while a loopback peer is served.
5b  parity: the running app exposes exactly the non-GET routes the AST scan
    sees, so no route can reach the app without being visible to P1.

The subject of the test is the invariant, not a fixed list of routes. If this
test can be made green by re-adding a per-route guard, the test is wrong: P1
would have to be relaxed to allow it.
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

#: Predicates that decide "is this caller from this machine / this origin?", in
#: every spelling they are known by: ``localhost_only`` (the definition in
#: ``local_gate``) is imported under the ``_localhost_only`` alias elsewhere, so
#: banning only one spelling would let the other back in.  Outside the three
#: mechanism modules nobody may call any of them: the middleware is the only caller.
GUARD_NAMES = frozenset({"_origin_check_request", "_origin_allowed", "localhost_only", "_localhost_only"})

#: The mechanism modules themselves: the definition of each predicate
#: (``origin_policy``, ``local_gate``) and the one place that uses them
#: (``auth_boundary``).
MECHANISM_MODULES = frozenset({"origin_policy.py", "local_gate.py", "auth_boundary.py"})

ORIGIN_POLICY_MODULE = "web_ui.backend.origin_policy"
LOCAL_GATE_MODULE = "web_ui.backend.local_gate"

#: The two predicates the boundary itself must import and call (P2):
#: (module the predicate is defined in, predicate name).
BOUNDARY_PREDICATES = (
    (ORIGIN_POLICY_MODULE, "_origin_check_request"),
    (LOCAL_GATE_MODULE, "localhost_only"),
)

#: A browser origin that is not this machine, and one that is
#: (``_ORIGIN_RE`` trusts ``http(s)://{localhost,127.0.0.1}[:port]``).
FOREIGN_ORIGIN = "http://evil.example"
TRUSTED_ORIGIN = "http://127.0.0.1:5173"

#: Loopback peer, spelled as a socket address so the test harness trust flag
#: (keyed on the peer NAME ``testclient``) cannot bypass the gate here.
LOOPBACK_PEER = ("127.0.0.1", 51234)
REMOTE_PEER = ("8.8.8.8", 5555)

#: Behavioural sample for P4: (handler_function_name, module_file_name).  One
#: route per module, never one route per method shape: what the single gate has
#: to prove is that it is wired into every router, not that each shape of
#: handler behaves.  Resolved through the same AST enumeration as 5b -- never by
#: importing the handler -- so a renamed or moved handler fails here loudly.
#: The bodies sent are empty objects: a 422/400 from body validation is the
#: CONTROL's expected outcome, because the gate must run before the handler.
FOREIGN_ORIGIN_SAMPLE = [
    ("set_mode", "config_routes.py"),
    ("upsert_provider", "provider_routes.py"),
    ("save_prompt", "prompt_routes.py"),
    ("create_directory", "server.py"),
]

#: P5 paths: one from ``server.py`` and one from ``health_routes.py``, both
#: exempt from the credential but NOT from the peer gate.
PEER_GATED_PATHS = ("/health", "/api/system/health")

_Route = namedtuple("_Route", "method path handler module line")


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


class _GuardCallVisitor(ast.NodeVisitor):
    """Collect every call to a name in ``GUARD_NAMES``, tagged with its enclosing function."""

    def __init__(self, module):
        self.module = module
        self.scope = []
        self.offenders = []

    def _visit_function(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node):
        name = _callee_name(node)
        if name in GUARD_NAMES:
            enclosing = self.scope[-1] if self.scope else "<module>"
            self.offenders.append(f"{self.module}:{node.lineno} {name} ({enclosing})")
        self.generic_visit(node)


def _guard_call_sites():
    """Every guard call outside the mechanism modules, as ``module:line name (handler)``."""
    offenders = []
    for source in sorted(BACKEND_DIR.glob("*.py")):
        if source.name in MECHANISM_MODULES:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        visitor = _GuardCallVisitor(source.name)
        visitor.visit(tree)
        offenders.extend(visitor.offenders)
    return offenders


def _imported_names(tree):
    """Map module name -> set of names imported from it, over all ``ImportFrom`` nodes."""
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.setdefault(node.module, set()).update(alias.name for alias in node.names)
    return imported


def _called_guard_names(tree):
    """The subset of ``GUARD_NAMES`` called anywhere in ``tree``."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name in GUARD_NAMES:
                found.add(name)
    return found


def _enumerate_routes():
    """Enumerate the routes declared in ``web_ui/backend/*.py`` from the AST alone.

    Returns ``(routes, un_addable_handlers)``; ``un_addable_handlers`` collects the
    non-GET routes whose decorator path could not be resolved to a full API path,
    so that a route can never dodge 5b's parity check by being invisible to the scan.
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
                    )
                )
    return routes, un_addable_handlers


def _routes_by_handler(routes):
    """Group ``routes`` by handler function name."""
    grouped = {}
    for route in routes:
        grouped.setdefault(route.handler, []).append(route)
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


def test_p1_no_route_level_guard_survives_outside_the_boundary():
    """P1: the guard is called in exactly one module, and never by a route handler."""
    offenders = _guard_call_sites()
    scanned = sorted(
        source.name for source in BACKEND_DIR.glob("*.py") if source.name not in MECHANISM_MODULES
    )
    print(f"p1 scanned_modules={len(scanned)} guard_call_sites={len(offenders)}")
    assert scanned, f"no backend modules found under {BACKEND_DIR}"
    assert offenders == [], (
        "per-route origin/peer guards must not exist: the AuthBoundaryMiddleware is the single "
        "enforcement point (delete the guard, do not add the module to MECHANISM_MODULES): "
        + "; ".join(offenders)
    )


def test_p2_the_boundary_imports_and_calls_both_predicates():
    """P2: the middleware is a caller of the mechanism, not a second copy of it."""
    source = BACKEND_DIR / "auth_boundary.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported = _imported_names(tree)

    expected = set()
    for module, predicate in BOUNDARY_PREDICATES:
        expected.add(predicate)
        assert predicate in imported.get(module, set()), (
            f"auth_boundary.py must import {predicate} from {module}; "
            f"imported: {sorted(imported.get(module, set()))}"
        )

    missing = sorted(expected - _called_guard_names(tree))
    assert missing == [], f"auth_boundary.py imports but never calls {missing}"


def test_p3_cors_is_registered_outside_the_boundary():
    """P3: CORS must be the outer layer, so preflights never reach the gate."""
    _ensure_repo_root_on_sys_path()
    from web_ui.backend import server

    classes = [middleware.cls for middleware in server.app.user_middleware]
    assert server.CORSMiddleware in classes, f"CORSMiddleware is not registered; stack is {classes}"
    assert server.AuthBoundaryMiddleware in classes, f"AuthBoundaryMiddleware is not registered; stack is {classes}"

    cors_index = classes.index(server.CORSMiddleware)
    boundary_index = classes.index(server.AuthBoundaryMiddleware)
    assert cors_index < boundary_index, (
        "CORSMiddleware must be OUTSIDE (lower index than) AuthBoundaryMiddleware, else a preflight "
        f"is refused by the credential gate: cors={cors_index} boundary={boundary_index}"
    )


def test_p4_foreign_origin_is_refused_and_loopback_origin_is_served():
    """P4: the single gate refuses a foreign Origin on routes from four modules."""
    _ensure_repo_root_on_sys_path()
    from fastapi.testclient import TestClient
    from web_ui.backend import auth_token, server

    modules = [module for _, module in FOREIGN_ORIGIN_SAMPLE]
    assert len(set(modules)) == len(modules), f"the sample must span distinct modules, got {modules}"

    routes, un_addable_handlers = _enumerate_routes()
    assert un_addable_handlers == [], "unresolvable non-GET routes: " + "; ".join(un_addable_handlers)
    grouped = _routes_by_handler(routes)

    token = auth_token.ensure_token()
    client = TestClient(server.app, client=LOOPBACK_PEER, raise_server_exceptions=False)
    for handler, module in FOREIGN_ORIGIN_SAMPLE:
        matches = [route for route in grouped.get(handler, []) if route.module == module]
        assert len(matches) == 1, (
            f"{handler}: expected exactly one route in {module}, found "
            f"{[(route.method, route.path) for route in matches]}"
        )
        route = matches[0]
        assert route.method != GET, f"{handler} ({module}) is {route.method} {route.path}; the sample needs a non-GET route"
        path = _concretise(route.path)
        credential = {"Authorization": f"Bearer {token}"}

        refused = client.request(route.method, path, headers={"Origin": FOREIGN_ORIGIN, **credential}, json={})
        assert refused.status_code == 403, (
            f"{route.method} {route.path} ({module}:{route.line}) answered {refused.status_code} for a foreign "
            f"Origin, expected 403 from the boundary; a 404/405 means the sampled path or method is wrong, "
            f"not that the assertion should be relaxed. body={refused.text!r}"
        )
        assert "origin" in refused.text.lower(), (
            f"{route.method} {route.path} refused with 403 but not for the origin: body={refused.text!r}"
        )

        served = client.request(route.method, path, headers={"Origin": TRUSTED_ORIGIN, **credential}, json={})
        assert served.status_code not in (403, 404, 405), (
            f"{route.method} {route.path} ({module}:{route.line}) answered {served.status_code} for the loopback "
            f"Origin {TRUSTED_ORIGIN}; 403 means the gate refuses a trusted origin, 404/405 means the sampled "
            f"path or method is wrong. body={served.text!r}"
        )


def test_p5_non_loopback_peer_is_refused_on_the_health_probes():
    """P5: a remote peer is refused on the probes that no longer carry their own gate."""
    _ensure_repo_root_on_sys_path()
    from fastapi.testclient import TestClient
    from web_ui.backend import server

    remote = TestClient(server.app, client=REMOTE_PEER, raise_server_exceptions=False)
    local = TestClient(server.app, client=LOOPBACK_PEER, raise_server_exceptions=False)
    for path in PEER_GATED_PATHS:
        refused = remote.get(path)
        assert refused.status_code == 403, (
            f"GET {path} answered {refused.status_code} for non-loopback peer {REMOTE_PEER}, expected 403 "
            f"from the boundary's peer gate; body={refused.text!r}"
        )
        served = local.get(path)
        assert served.status_code == 200, (
            f"GET {path} answered {served.status_code} for loopback peer {LOOPBACK_PEER}, expected 200 "
            f"(the probe must stay served on this machine); body={served.text!r}"
        )


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
