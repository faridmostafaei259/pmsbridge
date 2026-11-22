"""The OAuth authorization server: discovery documents, client registration, authorization.

`register(app)` mounts the routes: discovery documents, client registration, authorization and
consent, the token and revocation endpoints, and the connected-apps pages.
"""

from fastapi import FastAPI

from pmsbridge.oauth import apps, authorize, metadata, registration, token_endpoint


def register(app: FastAPI) -> None:
    metadata.register_routes(app)
    registration.register_routes(app)
    authorize.register_routes(app)
    token_endpoint.register_routes(app)
    apps.register_routes(app)
