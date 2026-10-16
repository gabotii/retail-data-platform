"""Test helpers shared by unit and integration tests."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient


def app_transport(app: FastAPI) -> httpx.MockTransport:
    """An httpx transport that serves requests from an in-process ASGI app.

    Lets the real ``InventoryApiClient`` talk to the demo API without a network socket.
    """
    client = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        response = client.request(
            request.method,
            request.url.path,
            params=dict(request.url.params),
            headers=dict(request.headers),
        )
        return httpx.Response(
            response.status_code, headers=dict(response.headers), content=response.content
        )

    return httpx.MockTransport(handler)
