from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import ssl
import subprocess
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.request import parse_http_list, parse_keqv_list

import httpx
import pytest

from fronius_emulator.shelly import (
    ShellyClient,
    ShellyConnectionError,
    ShellyPayloadError,
    ShellySourceConfig,
)

PAYLOAD = {"apower": 500, "voltage": 230, "current": 2.2, "aenergy": {"total": 1000}}


@asynccontextmanager
async def endpoint(respond, ssl_context=None):
    handlers = set()
    failures = []

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            await respond(request.decode(), reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as exc:
            failures.append(exc)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ssl_context)
    port = server.sockets[0].getsockname()[1]
    try:
        scheme = "https" if ssl_context else "http"
        yield f"{scheme}://127.0.0.1:{port}", handlers
    finally:
        server.close()
        await server.wait_closed()
        tasks = list(handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not failures, failures


async def reply(writer, body=None, status="200 OK", headers=""):
    body = json.dumps(PAYLOAD).encode() if body is None else body
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
        f"Connection: close\r\n{headers}\r\n".encode()
        + body
    )
    await writer.drain()


@pytest.mark.parametrize("mode", ["drip", "no_headers", "no_body"])
def test_total_deadline_cancels_real_http_and_allows_recovery(mode):
    calls = 0

    async def respond(request, reader, writer):
        nonlocal calls
        calls += 1
        if calls > 2:
            await reply(writer)
        elif mode == "no_headers":
            await reader.read()
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n")
            await writer.drain()
            if mode == "no_body":
                await reader.read()
            else:
                for _ in range(10000):
                    writer.write(b" ")
                    await writer.drain()
                    await asyncio.sleep(0.005)

    async def scenario():
        async with endpoint(respond) as (url, handlers):
            client = ShellyClient(
                ShellySourceConfig("test", url, "L1", read_timeout=1, total_timeout=0.08)
            )
            try:
                for _ in range(2):
                    async with asyncio.timeout(2):
                        with pytest.raises(ShellyConnectionError):
                            await client.fetch_async()
                assert (await client.fetch_async()).power_w == 500
                async with asyncio.timeout(2):
                    while handlers:
                        await asyncio.sleep(0.005)
            finally:
                await client.aclose()
            assert client._async_http is None

    asyncio.run(scenario())


def test_cancelling_fetch_closes_connection_without_pinning_auto_direction():
    started, disconnected = asyncio.Event(), asyncio.Event()

    async def respond(request, reader, writer):
        started.set()
        assert await reader.read() == b""
        disconnected.set()

    async def scenario():
        async with endpoint(respond) as (url, _):
            client = ShellyClient(ShellySourceConfig("test", url, "L1"))
            task = asyncio.create_task(client.fetch_async())
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(disconnected.wait(), 2)
                assert client._auto_uses_negative is None
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("algorithm", ["MD5", "SHA-256"])
def test_real_digest_challenge_matches_sync_transport(algorithm):
    authenticated = 0
    password, realm, nonce = "test-password", "shelly-test", "test-nonce"
    digest = hashlib.md5 if algorithm == "MD5" else hashlib.sha256

    def hash_text(value):
        return digest(value.encode()).hexdigest()

    async def respond(request, reader, writer):
        nonlocal authenticated
        headers = dict(
            (name.lower(), value.strip())
            for line in request.split("\r\n")[1:]
            if line
            for name, value in [line.split(":", 1)]
        )
        auth = headers.get("authorization")
        if auth is None:
            await reply(
                writer,
                b"",
                "401 Unauthorized",
                f'WWW-Authenticate: Digest realm="{realm}", nonce="{nonce}", '
                f'algorithm={algorithm}, qop="auth"\r\n',
            )
            return
        fields = parse_keqv_list(parse_http_list(auth.removeprefix("Digest ")))
        assert fields["username"] == "admin"
        assert fields["uri"] == "/rpc/Switch.GetStatus?id=0"
        ha1 = hash_text(f"admin:{realm}:{password}")
        ha2 = hash_text(f"GET:{fields['uri']}")
        expected = hash_text(
            f"{ha1}:{nonce}:{fields['nc']}:{fields['cnonce']}:{fields['qop']}:{ha2}"
        )
        assert fields["response"] == expected
        authenticated += 1
        await reply(writer)

    async def scenario():
        async with endpoint(respond) as (url, _):
            source = ShellySourceConfig("test", url, "L1", username="admin", password=password)
            client = ShellyClient(source)
            try:
                sync_reading = await asyncio.to_thread(client.fetch, 1234.5)
                async_reading = await client.fetch_async(1234.5)
                assert async_reading == sync_reading
                assert (await client.fetch_async(1234.5)) == sync_reading
                assert authenticated == 3
            finally:
                await client.aclose()

    asyncio.run(scenario())


def test_redirects_are_followed_and_cookies_do_not_leak_between_polls():
    initial_requests = []

    async def respond(request, reader, writer):
        if request.startswith("GET /rpc/"):
            initial_requests.append(request)
            await reply(
                writer, b"", "302 Found", "Location: /status\r\nSet-Cookie: foo=bar; Path=/\r\n"
            )
        else:
            assert "cookie: foo=bar" in request.lower()
            await reply(writer)

    async def scenario():
        async with endpoint(respond) as (url, _):
            client = ShellyClient(ShellySourceConfig("test", url, "L1"))
            try:
                for _ in range(2):
                    assert (await client.fetch_async()).power_w == 500
            finally:
                await client.aclose()
        assert len(initial_requests) == 2
        assert all("cookie:" not in request.lower() for request in initial_requests)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,body,expected",
    [
        ("503 Unavailable", b"unavailable", ShellyConnectionError),
        ("200 OK", b"invalid JSON", ShellyPayloadError),
        ("200 OK", b"[]", ShellyPayloadError),
    ],
)
def test_async_errors_keep_existing_exception_types(status, body, expected):
    async def respond(request, reader, writer):
        await reply(writer, body, status)

    async def scenario():
        async with endpoint(respond) as (url, _):
            client = ShellyClient(ShellySourceConfig("test", url, "L1"))
            try:
                with pytest.raises(expected):
                    await client.fetch_async()
            finally:
                await client.aclose()

    asyncio.run(scenario())


def test_total_deadline_includes_connection_setup(monkeypatch):
    cancelled = asyncio.Event()

    async def connect_stalled(self, *args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", connect_stalled)

    async def scenario():
        client = ShellyClient(ShellySourceConfig("test", "127.0.0.1", "L1", total_timeout=0.01))
        try:
            with pytest.raises(ShellyConnectionError):
                await client.fetch_async()
            assert cancelled.is_set()
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_tls_verification_and_requests_ca_bundle_are_preserved(tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if not openssl:
        bundled = Path("C:/Program Files/Git/usr/bin/openssl.exe")
        openssl = str(bundled) if bundled.exists() else None
    if not openssl:
        pytest.skip("OpenSSL required to generate an ephemeral local test certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    async def respond(request, reader, writer):
        await reply(writer)

    async def scenario():
        async with endpoint(respond, context) as (url, _):
            monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
            monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
            client = ShellyClient(ShellySourceConfig("test", url, "L1"))
            try:
                with pytest.raises(ShellyConnectionError):
                    await client.fetch_async()
            finally:
                await client.aclose()
            monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
            monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path / "missing.pem"))
            try:
                expected = await asyncio.to_thread(client.fetch, 1.0)
                assert await client.fetch_async(1.0) == expected
            finally:
                await client.aclose()

    asyncio.run(scenario())
