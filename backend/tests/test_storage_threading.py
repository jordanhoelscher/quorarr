"""``os.statvfs`` must not run on the event loop.

``/media`` is a read-only bind of a **hard** NFS mount, a documented house
failure mode. A hung statvfs on the loop parks every route including
``/health``, so the container healthcheck fails and Docker restart-loops the
app -- reading like an application bug rather than a dead NAS.
"""

import os
import threading

import httpx

from pensieve.clients.base import CachedHTTP
from pensieve.services import storage
from tests.conftest import make_settings


class _Stat:
    f_frsize = 4096
    f_blocks = 1000
    f_bavail = 400


async def test_summary_calls_statvfs_off_the_event_loop(tmp_path, monkeypatch):
    calling_threads: list[str] = []

    def fake_statvfs(path):
        calling_threads.append(threading.current_thread().name)
        return _Stat()

    monkeypatch.setattr(os, "statvfs", fake_statvfs)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    settings = make_settings(tmp_path)

    await storage.summary(http, settings)

    assert calling_threads
    assert calling_threads[0] != threading.current_thread().name
