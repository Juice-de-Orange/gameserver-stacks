"""Flat view of every route of the app, for the tests that inspect the routing table.

The pinned FastAPI no longer lets `include_router` copy a router's routes into `app.routes`; the
app holds one nested entry per included router instead. Tests that ask "which paths exist",
"which route answers this request" or "what does this endpoint depend on" need the effective
routes (prefix applied), in dispatch order.
"""
from fastapi.routing import iter_route_contexts


def flat_routes(app) -> list:
    """Effective routes in dispatch order; each has `path`, `name`, `methods`, `matches()` and,
    for API routes, `dependant`.

    The order is the one Starlette dispatches in: an included router answers with the first of
    its routes that matches fully, so "first full match in this list" is the route that handles
    a request.
    """
    return list(iter_route_contexts(app.routes))
