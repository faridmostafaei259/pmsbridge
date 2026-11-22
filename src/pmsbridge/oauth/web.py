"""The OAuth authorization server: discovery documents, client registration, authorization.

`register(app)` mounts the routes. The token endpoint, refresh and revocation are not served yet.
"""

from fastapi import FastAPI

from pmsbridge.oauth import authorize, metadata, registration


def register(app: FastAPI) -> None:
    metadata.register_routes(app)
    registration.register_routes(app)
    authorize.register_routes(app)
