"""Async SIP client for BTicino Classe 100X.

Maintains a persistent TLS 1.3 mTLS connection to vdesip.bs.iotleg.com:5228
and handles SIP REGISTER + incoming INVITE dialogs.

No external dependencies — pure Python asyncio + ssl.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import string
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .const import SIP_HOST, SIP_PORT, SIP_EXPIRES, SIP_RENEW

_LOGGER = logging.getLogger(__name__)


# ── SIP helpers ────────────────────────────────────────────────────────────────

def _rand(n: int = 8) -> str:
    return "".join(random.choices(string.hexdigits.lower(), k=n))


def _branch() -> str:
    return "z9hG4bK-ha-" + _rand(16)


def _get_header(raw: str, name: str) -> str:
    """Return first matching header value (case-insensitive)."""
    nl = name.lower() + ":"
    for line in raw.split("\r\n"):
        if line.lower().startswith(nl):
            return line.split(":", 1)[1].strip()
    return ""


def _all_headers(raw: str, name: str) -> List[str]:
    """Return all matching header values."""
    nl = name.lower() + ":"
    return [
        line.split(":", 1)[1].strip()
        for line in raw.split("\r\n")
        if line.lower().startswith(nl)
    ]


def _tag(raw: str) -> str:
    """Extract tag= param from From/To header."""
    m = re.search(r";tag=([^\s;,>]+)", raw)
    return m.group(1) if m else ""


def _uri(raw: str) -> str:
    """Extract SIP URI from '<sip:...>' or bare string."""
    m = re.search(r"<([^>]+)>", raw)
    return m.group(1) if m else raw.split(";")[0].strip()


# ── Dialog state ───────────────────────────────────────────────────────────────

@dataclass
class SIPDialog:
    """Tracks a single incoming INVITE dialog."""
    call_id:        str
    from_hdr:       str        # original From: header line value
    to_hdr:         str        # original To: header line value
    via_hdrs:       List[str]  # all Via: header values
    cseq_hdr:       str        # CSeq: header value
    remote_contact: str        # Contact URI of remote party
    local_tag:      str        # our To-tag
    invite_body:    str = ""   # original SDP offer (for reference)
    state:          str = "ringing"   # ringing → answering → answered → ended
    local_cseq:     int = 1


# ── SIP Client ────────────────────────────────────────────────────────────────

class BticinoSIPClient:
    """
    Persistent outbound SIP-over-TLS 1.3 client.

    Connects to the Legrand/BTicino cloud SIP proxy and maintains registration.
    Incoming INVITEs (doorbell rings) are surfaced via the on_call_incoming
    callback. Call lifecycle (answer, reject, door-open) is managed via the
    public async methods.
    """

    def __init__(
        self,
        sip_uri:      str,
        sip_password: str,
        cert_path:    str,
        key_path:     str,
        ca_path:      str,
        dtmf_command: str = "#",
        on_call_incoming: Optional[Callable[[str, str], None]] = None,
        on_call_ended:    Optional[Callable[[str], None]]      = None,
    ) -> None:
        self.sip_uri      = sip_uri
        self.sip_password = sip_password
        self._cert_path   = cert_path
        self._key_path    = key_path
        self._ca_path     = ca_path
        self.dtmf_command = dtmf_command

        self.on_call_incoming = on_call_incoming
        self.on_call_ended    = on_call_ended

        self._domain   = sip_uri.split("@")[1]
        self._from_tag = _rand(10)
        self._call_id  = _rand(16) + "@ha"
        self._cseq     = 1

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._local_ip: str = "127.0.0.1"

        self._dialogs:  Dict[str, SIPDialog] = {}
        self._registered = False
        self._running    = False
        self._task:   Optional[asyncio.Task] = None

    # ── Public lifecycle ───────────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Start the client. Runs until async_stop() is called."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="bticino_sip")

    async def async_stop(self) -> None:
        """Gracefully stop the client."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_connection()

    @property
    def is_registered(self) -> bool:
        return self._registered

    @property
    def ringing_calls(self) -> List[str]:
        """Return call_ids currently in 'ringing' state."""
        return [cid for cid, d in self._dialogs.items() if d.state == "ringing"]

    # ── Public SIP actions ─────────────────────────────────────────────────────

    async def async_answer_and_open(self, call_id: str) -> bool:
        """
        Answer the incoming call identified by call_id, then immediately send
        the door-open DTMF command, then hang up.

        Returns True if the sequence was dispatched successfully.
        """
        dialog = self._dialogs.get(call_id)
        if not dialog or dialog.state != "ringing":
            _LOGGER.warning("answer_and_open: no ringing dialog %s", call_id)
            return False

        # 200 OK + SDP
        sdp = self._build_sdp()
        dialog.state = "answering"
        await self._send_response(dialog, 200, "OK", body=sdp,
                                  content_type="application/sdp")
        _LOGGER.info("Answered call %s — waiting for ACK", call_id)

        # Wait up to 5 s for ACK
        for _ in range(50):
            await asyncio.sleep(0.1)
            if self._dialogs.get(call_id, dialog).state == "answered":
                break

        # Send door-open INFO (even if ACK didn't arrive — servers usually accept)
        await self._send_info_open(dialog)
        return True

    async def async_reject_call(self, call_id: str) -> None:
        """Decline an incoming call with 486 Busy Here."""
        dialog = self._dialogs.pop(call_id, None)
        if not dialog:
            return
        await self._send_response(dialog, 486, "Busy Here")
        _LOGGER.info("Rejected call %s", call_id)

    # ── Internal lifecycle ─────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        delay = 5
        while self._running:
            try:
                await self._connect_and_run()
                delay = 5
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.error("SIP error: %s — retry in %ds", exc, delay)
                self._registered = False
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def _connect_and_run(self) -> None:
        import ssl as _ssl

        def _build_ssl_ctx() -> "_ssl.SSLContext":
            c = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
            c.load_cert_chain(self._cert_path, self._key_path)
            c.load_verify_locations(cafile=self._ca_path)
            c.check_hostname = False
            c.verify_mode = _ssl.CERT_OPTIONAL
            c.minimum_version = _ssl.TLSVersion.TLSv1_2
            return c

        loop = asyncio.get_running_loop()
        ctx = await loop.run_in_executor(None, _build_ssl_ctx)

        _LOGGER.info("Connecting to %s:%d", SIP_HOST, SIP_PORT)
        self._reader, self._writer = await asyncio.open_connection(
            SIP_HOST, SIP_PORT, ssl=ctx
        )
        sockname = self._writer.get_extra_info("sockname")
        self._local_ip = sockname[0] if sockname else "127.0.0.1"
        _LOGGER.info("TLS connected from %s", self._local_ip)

        await self._send_register()

        await asyncio.gather(
            self._receive_loop(),
            self._reregister_loop(),
        )

    async def _close_connection(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    # ── REGISTER ──────────────────────────────────────────────────────────────

    async def _send_register(self, expires: int = SIP_EXPIRES) -> None:
        msg = (
            f"REGISTER sip:{self._domain} SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS {self._local_ip};branch={_branch()};rport\r\n"
            f"From: <sip:{self.sip_uri}>;tag={self._from_tag}\r\n"
            f"To: <sip:{self.sip_uri}>\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._cseq} REGISTER\r\n"
            f"Contact: <sip:{self.sip_uri};transport=tls>\r\n"
            f"Max-Forwards: 70\r\n"
            f"Expires: {expires}\r\n"
            f"User-Agent: HomeAssistant-BTicino/1.0\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._cseq += 1
        await self._send(msg)

    async def _reregister_loop(self) -> None:
        while self._running:
            await asyncio.sleep(SIP_EXPIRES - SIP_RENEW)
            if not self._running:
                break
            try:
                await self._send_register()
                _LOGGER.debug("Re-registered")
            except Exception as exc:
                _LOGGER.error("Re-register failed: %s", exc)
                raise

    # ── Receive loop ───────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        buf = b""
        while self._running:
            try:
                chunk = await asyncio.wait_for(self._reader.read(65536), timeout=90)
                if not chunk:
                    _LOGGER.info("Server closed connection")
                    break
                buf += chunk
                while True:
                    msg, buf = self._parse(buf)
                    if msg is None:
                        break
                    asyncio.create_task(self._dispatch(msg))
            except asyncio.TimeoutError:
                # RFC 5626 keepalive ping (double CRLF)
                await self._send("\r\n\r\n")
            except asyncio.CancelledError:
                raise

    def _parse(self, buf: bytes):
        """
        Extract one complete SIP message from the byte buffer.
        Returns (msg_dict, remaining) or (None, buf) if incomplete.
        """
        # Strip leading keepalive CRLFs
        while buf.startswith(b"\r\n"):
            buf = buf[2:]
        if not buf:
            return None, buf

        sep = buf.find(b"\r\n\r\n")
        if sep == -1:
            return None, buf

        header_bytes = buf[:sep]
        rest = buf[sep + 4:]
        headers = header_bytes.decode("utf-8", "replace")

        # Parse Content-Length
        clen = 0
        for line in headers.split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                try:
                    clen = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
                break

        if len(rest) < clen:
            return None, buf  # incomplete body

        body = rest[:clen].decode("utf-8", "replace")
        return {"headers": headers, "body": body}, rest[clen:]

    # ── Dispatcher ────────────────────────────────────────────────────────────

    async def _dispatch(self, msg: dict) -> None:
        first = msg["headers"].split("\r\n")[0]
        _LOGGER.debug("SIP ← %s", first[:100])

        if first.startswith("SIP/2.0 "):
            await self._on_response(msg, first)
        elif first.startswith("INVITE "):
            await self._on_invite(msg)
        elif first.startswith("ACK "):
            await self._on_ack(msg)
        elif first.startswith("BYE "):
            await self._on_bye(msg)
        elif first.startswith("CANCEL "):
            await self._on_cancel(msg)
        elif first.startswith("OPTIONS "):
            await self._on_options(msg)

    # ── Response handler ──────────────────────────────────────────────────────

    async def _on_response(self, msg: dict, status_line: str) -> None:
        parts  = status_line.split(" ", 2)
        code   = int(parts[1]) if len(parts) >= 2 else 0
        reason = parts[2] if len(parts) >= 3 else ""
        cseq   = _get_header(msg["headers"], "CSeq")
        method = cseq.split()[-1] if cseq else ""
        cid    = _get_header(msg["headers"], "Call-ID")

        if 200 <= code < 300:
            if method == "REGISTER":
                _LOGGER.info("SIP registered ✓")
                self._registered = True
            elif method == "INFO":
                _LOGGER.info("Door-open INFO acknowledged (200 OK)")
                dialog = self._dialogs.get(cid)
                if dialog and dialog.state == "answered":
                    asyncio.create_task(self._send_bye(dialog))
            elif method == "BYE":
                self._dialogs.pop(cid, None)
        elif code == 401:
            _LOGGER.warning("SIP 401 — mTLS certificate may be invalid or expired")
        elif code == 403:
            _LOGGER.error("SIP 403 Forbidden")
        elif code >= 300:
            _LOGGER.warning("SIP %d %s (method=%s)", code, reason, method)

    # ── INVITE ────────────────────────────────────────────────────────────────

    async def _on_invite(self, msg: dict) -> None:
        h   = msg["headers"]
        cid = _get_header(h, "Call-ID")

        # Guard re-INVITE (call already exists)
        if cid in self._dialogs:
            dialog = self._dialogs[cid]
            # Re-INVITE during active call — just send 200 OK again
            if dialog.state == "answered":
                sdp = self._build_sdp()
                await self._send_response(dialog, 200, "OK", body=sdp,
                                          content_type="application/sdp")
            return

        local_tag = _rand(10)
        contact   = _get_header(h, "Contact")
        dialog = SIPDialog(
            call_id        = cid,
            from_hdr       = _get_header(h, "From"),
            to_hdr         = _get_header(h, "To"),
            via_hdrs       = _all_headers(h, "Via"),
            cseq_hdr       = _get_header(h, "CSeq"),
            remote_contact = _uri(contact) if contact else _uri(_get_header(h, "From")),
            local_tag      = local_tag,
            invite_body    = msg["body"],
        )
        self._dialogs[cid] = dialog

        caller = _uri(dialog.from_hdr)
        _LOGGER.info("🔔 Doorbell! call_id=%s from=%s", cid, caller)

        # 100 Trying (no tag yet)
        await self._send_response(dialog, 100, "Trying", with_tag=False)
        # 180 Ringing (with our tag)
        await self._send_response(dialog, 180, "Ringing", with_tag=True)

        if self.on_call_incoming:
            try:
                self.on_call_incoming(cid, caller)
            except Exception as exc:
                _LOGGER.error("on_call_incoming error: %s", exc)

    async def _on_ack(self, msg: dict) -> None:
        cid    = _get_header(msg["headers"], "Call-ID")
        dialog = self._dialogs.get(cid)
        if dialog and dialog.state == "answering":
            dialog.state = "answered"
            _LOGGER.info("ACK received — call %s active", cid)

    async def _on_bye(self, msg: dict) -> None:
        h   = msg["headers"]
        cid = _get_header(h, "Call-ID")

        # 200 OK for BYE
        via_block = "\r\n".join(f"Via: {v}" for v in _all_headers(h, "Via"))
        await self._send(
            f"SIP/2.0 200 OK\r\n"
            f"{via_block}\r\n"
            f"From: {_get_header(h, 'From')}\r\n"
            f"To: {_get_header(h, 'To')}\r\n"
            f"Call-ID: {cid}\r\n"
            f"CSeq: {_get_header(h, 'CSeq')}\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._dialogs.pop(cid, None)
        _LOGGER.info("Call %s ended (BYE)", cid)
        if self.on_call_ended:
            try:
                self.on_call_ended(cid)
            except Exception as exc:
                _LOGGER.error("on_call_ended error: %s", exc)

    async def _on_cancel(self, msg: dict) -> None:
        h   = msg["headers"]
        cid = _get_header(h, "Call-ID")
        # 200 OK for CANCEL
        via_block = "\r\n".join(f"Via: {v}" for v in _all_headers(h, "Via"))
        await self._send(
            f"SIP/2.0 200 OK\r\n"
            f"{via_block}\r\n"
            f"From: {_get_header(h, 'From')}\r\n"
            f"To: {_get_header(h, 'To')}\r\n"
            f"Call-ID: {cid}\r\n"
            f"CSeq: {_get_header(h, 'CSeq')}\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._dialogs.pop(cid, None)
        _LOGGER.info("Call %s cancelled", cid)
        if self.on_call_ended:
            try:
                self.on_call_ended(cid)
            except Exception as exc:
                _LOGGER.error("on_call_ended error: %s", exc)

    async def _on_options(self, msg: dict) -> None:
        h = msg["headers"]
        via_block = "\r\n".join(f"Via: {v}" for v in _all_headers(h, "Via"))
        await self._send(
            f"SIP/2.0 200 OK\r\n"
            f"{via_block}\r\n"
            f"From: {_get_header(h, 'From')}\r\n"
            f"To: {_get_header(h, 'To')};tag={_rand(6)}\r\n"
            f"Call-ID: {_get_header(h, 'Call-ID')}\r\n"
            f"CSeq: {_get_header(h, 'CSeq')}\r\n"
            f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, INFO\r\n"
            f"Content-Length: 0\r\n\r\n"
        )

    # ── Response builder ──────────────────────────────────────────────────────

    async def _send_response(
        self,
        dialog:       SIPDialog,
        code:         int,
        reason:       str,
        with_tag:     bool  = True,
        body:         str   = "",
        content_type: str   = "",
    ) -> None:
        via_block = "\r\n".join(f"Via: {v}" for v in dialog.via_hdrs)
        to_hdr    = dialog.to_hdr
        if with_tag and ";tag=" not in to_hdr:
            to_hdr = f"{to_hdr};tag={dialog.local_tag}"

        contact_hdr = (
            f"Contact: <sip:{self.sip_uri};transport=tls>\r\n"
            if 200 <= code < 300 else ""
        )
        ctype_hdr = f"Content-Type: {content_type}\r\n" if content_type and body else ""
        body_bytes = body.encode()

        await self._send(
            f"SIP/2.0 {code} {reason}\r\n"
            f"{via_block}\r\n"
            f"From: {dialog.from_hdr}\r\n"
            f"To: {to_hdr}\r\n"
            f"Call-ID: {dialog.call_id}\r\n"
            f"CSeq: {dialog.cseq_hdr}\r\n"
            f"{contact_hdr}"
            f"{ctype_hdr}"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"\r\n"
            f"{body}"
        )

    # ── In-dialog requests ────────────────────────────────────────────────────

    async def _send_info_open(self, dialog: SIPDialog) -> None:
        """Send SIP INFO with DTMF to trigger door relay."""
        body = f"Signal={self.dtmf_command}\r\nDuration=160\r\n"
        dialog.local_cseq += 1

        # In a UAS dialog, From/To are flipped relative to the original INVITE
        from_hdr = dialog.to_hdr
        if ";tag=" not in from_hdr:
            from_hdr = f"{from_hdr};tag={dialog.local_tag}"
        to_hdr = dialog.from_hdr

        await self._send(
            f"INFO {dialog.remote_contact} SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS {self._local_ip};branch={_branch()};rport\r\n"
            f"From: {from_hdr}\r\n"
            f"To: {to_hdr}\r\n"
            f"Call-ID: {dialog.call_id}\r\n"
            f"CSeq: {dialog.local_cseq} INFO\r\n"
            f"Content-Type: application/dtmf-relay\r\n"
            f"Content-Length: {len(body.encode())}\r\n"
            f"\r\n"
            f"{body}"
        )
        _LOGGER.info("Door-open INFO sent (DTMF=%s)", self.dtmf_command)

    async def _send_bye(self, dialog: SIPDialog) -> None:
        """Terminate an active call."""
        dialog.local_cseq += 1
        from_hdr = dialog.to_hdr
        if ";tag=" not in from_hdr:
            from_hdr = f"{from_hdr};tag={dialog.local_tag}"

        await self._send(
            f"BYE {dialog.remote_contact} SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS {self._local_ip};branch={_branch()};rport\r\n"
            f"From: {from_hdr}\r\n"
            f"To: {dialog.from_hdr}\r\n"
            f"Call-ID: {dialog.call_id}\r\n"
            f"CSeq: {dialog.local_cseq} BYE\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._dialogs.pop(dialog.call_id, None)
        _LOGGER.info("BYE sent (call %s)", dialog.call_id)

    # ── SDP builder ───────────────────────────────────────────────────────────

    def _build_sdp(self) -> str:
        """Minimal SDP for call acceptance (no real media needed — door opens via INFO)."""
        ts = int(time.time())
        return (
            f"v=0\r\n"
            f"o=- {ts} {ts} IN IP4 {self._local_ip}\r\n"
            f"s=HomeAssistant\r\n"
            f"c=IN IP4 {self._local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio 5004 RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\n"
            f"a=fmtp:101 0-16\r\n"
            f"a=sendrecv\r\n"
        )

    # ── Raw send ──────────────────────────────────────────────────────────────

    async def _send(self, msg: str) -> None:
        if not self._writer:
            raise RuntimeError("SIP: not connected")
        first = msg.split("\r\n")[0] if msg.strip() else "(keepalive)"
        _LOGGER.debug("SIP → %s", first[:80])
        self._writer.write(msg.encode())
        await self._writer.drain()
