# -*- coding: utf-8 -*-
"""
TrueNAS Scale JSON-RPC 2.0 over WebSocket client for SOPHIA Systems.

Replaces the deprecated /api/v2.0 REST API (removed in TrueNAS 26.04).
Uses a persistent multiplexed WebSocket connection so all concurrent
coordinator polls share one authenticated socket.

Connection lifecycle:
  - Lazy connect on first call, authenticated via auth.login_with_api_key
  - Background reader task dispatches responses by request ID to Futures
  - On any transport error, connection is torn down; next call reconnects
  - close() must be called on HA entry unload to clean up the reader task
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

_SKIP_IFACES = ("lo", "br-", "vnet", "docker", "tunl", "kube", "virbr", "bond")


class TrueNASClient:

    def __init__(self, url: str, api_key: str, verify_ssl: bool = True) -> None:
        self._ws_url = self._to_ws_url(url)
        self._api_key = api_key
        self._verify_ssl = verify_ssl

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._reader_task: Optional[asyncio.Task] = None

        # Pending futures keyed by request ID
        self._pending: Dict[int, asyncio.Future] = {}
        self._req_id: int = 0

        # Serialize reconnection attempts so concurrent callers
        # don't each try to open a new socket simultaneously
        self._connect_lock: asyncio.Lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # URL helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _to_ws_url(url: str) -> str:
        """Convert http(s):// base URL to wss:///api/current.

        TrueNAS Scale 25.x enforces secure transport for API key auth -
        using plain ws:// causes the key to be auto-revoked.
        Always upgrade to wss://, SSL verification is handled separately.
        """
        url = url.rstrip("/")
        if url.startswith("https://"):
            return url.replace("https://", "wss://", 1) + "/api/current"
        if url.startswith("http://"):
            return url.replace("http://", "wss://", 1) + "/api/current"
        return f"wss://{url}/api/current"

    # -------------------------------------------------------------------------
    # Connection management
    # -------------------------------------------------------------------------

    async def _ensure_connected(self) -> bool:
        """Return True if connected; reconnect if needed (serialized)."""
        if self._is_alive():
            return True
        async with self._connect_lock:
            # Re-check after acquiring lock - another coroutine may have
            # already reconnected while we were waiting
            if self._is_alive():
                return True
            return await self._connect()

    def _is_alive(self) -> bool:
        return (
            self._ws is not None
            and not self._ws.closed
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def _connect(self) -> bool:
        """Open WebSocket, start reader task, authenticate.

        TrueNAS Scale 25.x requires auth.login_with_api_key as a
        post-connect RPC call over wss://. The Bearer header on the
        WS upgrade does NOT establish an authenticated session -
        core.ping works unauthenticated which caused false confidence.
        Only auth.login_with_api_key elevates the session to authenticated.
        """
        await self._teardown()
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
            self._ws = await self._session.ws_connect(
                self._ws_url,
                heartbeat=30,
                ssl=False,
                timeout=aiohttp.ClientWSTimeout(ws_receive=60),
            )
            loop = asyncio.get_running_loop()
            self._reader_task = loop.create_task(
                self._reader_loop(), name="truenas_ws_reader"
            )
            # Yield so the reader task is scheduled before the first RPC send
            await asyncio.sleep(0)

            # Authenticate the session via RPC
            try:
                result = await self._call("auth.login_with_api_key", [self._api_key], timeout=10)
                _LOGGER.debug("TrueNAS WS: auth.login_with_api_key returned %r (type: %s)", result, type(result).__name__)
            except Exception as exc:
                _LOGGER.error("TrueNAS WS: auth RPC failed at %s: %s", self._ws_url, exc)
                await self._teardown()
                return False

            if not result:
                _LOGGER.error(
                    "TrueNAS WS: auth.login_with_api_key returned %r at %s - "
                    "regenerate the API key in TrueNAS UI -> Credentials -> API Keys "
                    "and update it in SOPHIA Systems -> Configure.",
                    result, self._ws_url,
                )
                await self._teardown()
                return False

            _LOGGER.info("TrueNAS WS: connected and authenticated at %s", self._ws_url)
            return True
        except Exception as exc:
            _LOGGER.error("TrueNAS WS connect failed (%s): %s", self._ws_url, exc)
            await self._teardown()
            return False

    async def _teardown(self) -> None:
        """Cancel reader, reject pending futures, close socket and session."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        self._reader_task = None

        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    async def close(self) -> None:
        """Public shutdown hook - call from HA entry unload."""
        await self._teardown()

    # -------------------------------------------------------------------------
    # WebSocket reader / multiplexer
    # -------------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Background task: pump WS frames and resolve pending Futures by ID."""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.debug("TrueNAS WS frame error: %s", self._ws.exception())
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.debug("TrueNAS WS reader exception: %s", exc)
        finally:
            # Cancel any futures still waiting - the socket is gone
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.cancel()
            self._pending.clear()
            _LOGGER.debug("TrueNAS WS reader exited")

    def _dispatch(self, raw: str) -> None:
        """Parse one JSON-RPC response frame and resolve the matching Future."""
        try:
            data = json.loads(raw)
        except ValueError as exc:
            _LOGGER.debug("TrueNAS WS bad JSON: %s", exc)
            return
        req_id = data.get("id")
        fut = self._pending.pop(req_id, None)
        if fut is None or fut.done():
            return
        if "error" in data:
            fut.set_exception(RuntimeError(f"JSON-RPC error: {data['error']}"))
        else:
            fut.set_result(data.get("result"))

    # -------------------------------------------------------------------------
    # JSON-RPC call primitives
    # -------------------------------------------------------------------------

    async def _call(self, method: str, params: Any = None, timeout: int = 15) -> Any:
        """Send a JSON-RPC request and await the response Future.

        Caller is responsible for ensuring the socket is open before calling.
        """
        if params is None:
            params = []
        self._req_id += 1
        req_id = self._req_id

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": req_id,
        })
        try:
            await self._ws.send_str(payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise

    async def _rpc(self, method: str, params: Any = None, timeout: int = 15) -> Any:
        """Public-facing RPC: ensure connected, call, disconnect on transport error.

        JSON-RPC application errors (RuntimeError from _dispatch) do NOT tear
        down the connection - the socket is still alive and usable.
        Only transport-level failures (ConnectionError, CancelledError from a
        dropped socket, aiohttp exceptions) tear down and force reconnect.
        """
        if not await self._ensure_connected():
            raise ConnectionError(
                f"TrueNAS WS unavailable at {self._ws_url}"
            )
        try:
            return await self._call(method, params, timeout=timeout)
        except RuntimeError:
            # JSON-RPC error response - socket is still healthy, just re-raise
            raise
        except Exception as exc:
            _LOGGER.debug("TrueNAS RPC '%s' transport error: %s - dropping connection", method, exc)
            await self._teardown()
            raise

    # -------------------------------------------------------------------------
    # Public API  (same interface as the old REST client)
    # -------------------------------------------------------------------------

    async def test_connection(self) -> bool:
        try:
            await self._rpc("system.info", timeout=10)
            return True
        except Exception as exc:
            _LOGGER.error("TrueNAS connection test failed: %s", exc)
            return False

    async def get_system_info(self) -> Dict[str, Any]:
        try:
            d = await self._rpc("system.info")
            _LOGGER.debug("TrueNAS system.info raw: %r", d)
            return {
                "hostname":       d.get("hostname", "unknown"),
                "version":        d.get("version", "unknown"),
                "uptime_seconds": d.get("uptime_seconds", 0),
                "loadavg":        d.get("loadavg", [0.0, 0.0, 0.0]),
                "physmem_gb":     round(d.get("physmem", 0) / (1024 ** 3), 1),
            }
        except Exception as exc:
            _LOGGER.debug("TrueNAS system_info error: %s", exc)
            return {}

    async def get_pools(self) -> List[Dict[str, Any]]:
        try:
            data = await self._rpc("pool.query")
            _LOGGER.debug("TrueNAS pool.query raw: %r", data)
            result = []
            for p in data:
                size  = p.get("size") or 1
                alloc = p.get("allocated") or 0
                result.append({
                    "name":          p.get("name", "unknown"),
                    "status":        p.get("status", "UNKNOWN"),
                    "healthy":       bool(p.get("healthy", False)),
                    "size_tb":       round(p.get("size", 0) / (1024 ** 4), 2),
                    "free_tb":       round(p.get("free", 0) / (1024 ** 4), 2),
                    "allocated_pct": round(alloc / size * 100, 1),
                })
            return result
        except Exception as exc:
            _LOGGER.debug("TrueNAS pools error: %s", exc)
            return []

    async def get_sensors(self) -> List[Dict[str, Any]]:
        """IPMI/BMC sensors via TrueNAS sensor endpoint."""
        try:
            data = await self._rpc("sensor.query")
            return [
                {
                    "name":  s.get("name", ""),
                    "value": s.get("value"),
                    "unit":  s.get("unit", ""),
                    "type":  s.get("type", "unknown"),
                }
                for s in data
                if s.get("value") is not None
            ]
        except Exception as exc:
            _LOGGER.debug("TrueNAS sensors error: %s", exc)
            return []

    async def get_disk_temperatures(self) -> Dict[str, Optional[float]]:
        """Per-disk temperatures via SMART data.

        TrueNAS 25.x disk.temperatures signature:
          disk.temperatures(name: list[str], include_thresholds: bool = False)
        The old {powermode: NEVER} second arg was removed; pass False for
        include_thresholds to get a simple {devname: temp_celsius} dict.
        """
        try:
            disks = await self._rpc("disk.query")
            _LOGGER.debug("TrueNAS disk.query raw count: %d, first: %r", len(disks) if disks else 0, disks[0] if disks else None)
            names = [
                d["name"] for d in disks
                if d.get("name") and not d["name"].startswith("cd")
            ]
            if not names:
                return {}
            result = await self._rpc(
                "disk.temperatures",
                [names, False],
            )
            if isinstance(result, dict):
                return {k: v for k, v in result.items() if v is not None}
            return {}
        except Exception as exc:
            _LOGGER.debug("TrueNAS disk temps error: %s", exc)
            return {}

    async def get_interfaces(self) -> List[Dict[str, Any]]:
        """Physical network interfaces with cumulative byte counters."""
        try:
            data = await self._rpc("interface.query")
            _LOGGER.debug("TrueNAS interface.query raw: %r", data)
            result = []
            for iface in data:
                name = iface.get("name", "")
                if any(name.startswith(p) for p in _SKIP_IFACES):
                    continue
                state = iface.get("state") or {}
                result.append({
                    "name":       name,
                    "link_state": state.get("link_state", "UNKNOWN"),
                    "speed_mbps": state.get("speed"),
                    "rx_bytes":   state.get("rx_bytes", 0),
                    "tx_bytes":   state.get("tx_bytes", 0),
                })
            return result
        except Exception as exc:
            _LOGGER.debug("TrueNAS interfaces error: %s", exc)
            return []