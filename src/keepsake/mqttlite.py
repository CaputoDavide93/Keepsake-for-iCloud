"""A small MQTT 3.1.1 publisher on the standard library.

The container image has no pip and no compiled dependencies, so the optional
``paho-mqtt`` package is never there. This is the part of an MQTT client that
Keepsake needs: connect (with a login and a last-will message), publish at
QoS 0 or 1, retained or not, and disconnect. It never subscribes.

The surface is the small subset of ``paho.mqtt.client.Client`` that the
notifier uses, so a test can hand in a fake with the same four calls.
"""

from __future__ import annotations

import socket
import struct
from typing import Optional

CONNACK_REFUSED = {
    1: "the broker does not support this protocol version",
    2: "the broker refused the client name",
    3: "the broker is not available",
    4: "the broker refused the user name or password",
    5: "the broker refused this client as not authorised",
}


class MqttError(Exception):
    """A broker call failed. The text is a plain sentence with no secret in it."""


def _string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def _remaining(length: int) -> bytes:
    out = bytearray()
    while True:
        digit, length = length % 128, length // 128
        out.append(digit | (0x80 if length else 0))
        if not length:
            return bytes(out)


class PublishInfo:
    """What ``publish`` returns. The call has already waited for QoS 1."""

    def wait_for_publish(self, timeout: Optional[float] = None) -> None:
        return None


class MqttClient:
    def __init__(self, client_id: str = "keepsake", timeout: float = 10.0) -> None:
        self.client_id = client_id
        self.timeout = timeout
        self._user = ""
        self._password = ""
        self._will: Optional[tuple] = None
        self._sock: Optional[socket.socket] = None
        self._packet_id = 0

    def username_pw_set(self, user: str, password: str = "") -> None:
        self._user, self._password = user, password

    def will_set(self, topic: str, payload: str, retain: bool = True) -> None:
        self._will = (topic, payload.encode("utf-8"), retain)

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _read(self, count: int) -> bytes:
        assert self._sock is not None
        data = b""
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise MqttError("the broker closed the connection")
            data += chunk
        return data

    def _packet(self) -> tuple:
        first = self._read(1)[0]
        length, shift = 0, 0
        while True:
            byte = self._read(1)[0]
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
            if shift > 21:
                raise MqttError("the broker sent a packet that could not be read")
        return first, self._read(length)

    def connect(self, host: str, port: int = 1883, keepalive: int = 120) -> None:
        try:
            self._sock = socket.create_connection((host, port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        except socket.timeout:
            self._sock = None
            raise MqttError("the broker did not answer in time") from None
        except OSError as exc:
            self._sock = None
            raise MqttError("could not reach the broker: "
                            f"{exc.strerror or type(exc).__name__}") from None
        flags = 0x02                                    # a clean session
        payload = _string(self.client_id)
        if self._will:
            topic, message, retain = self._will
            flags |= 0x04 | (0x20 if retain else 0)
            payload += _string(topic) + struct.pack("!H", len(message)) + message
        if self._user:
            flags |= 0x80
            payload += _string(self._user)
            if self._password:
                flags |= 0x40
                payload += _string(self._password)
        body = _string("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload
        try:
            self._sock.sendall(b"\x10" + _remaining(len(body)) + body)
            first, rest = self._packet()
        except (OSError, MqttError) as exc:
            self.close()
            raise MqttError(str(exc) if isinstance(exc, MqttError)
                            else "the broker did not answer the connection") from None
        if first >> 4 != 2 or len(rest) < 2:
            self.close()
            raise MqttError("the broker did not answer like an MQTT broker")
        if rest[1] != 0:
            self.close()
            raise MqttError(CONNACK_REFUSED.get(rest[1], "the broker refused the connection"))

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0) -> PublishInfo:
        if self._sock is None:
            raise MqttError("not connected to the broker")
        data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload or b"")
        head = 0x30 | (0x01 if retain else 0) | (qos << 1)
        body = _string(topic)
        if qos:
            self._packet_id = self._packet_id % 65535 + 1
            body += struct.pack("!H", self._packet_id)
        body += data
        try:
            self._sock.sendall(bytes([head]) + _remaining(len(body)) + body)
            if qos:
                first, rest = self._packet()
                if first >> 4 != 4 or struct.unpack("!H", rest[:2])[0] != self._packet_id:
                    raise MqttError("the broker did not confirm the message")
        except OSError:
            self.close()
            raise MqttError("the connection to the broker was lost") from None
        except MqttError:
            self.close()
            raise
        return PublishInfo()

    def disconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.sendall(b"\xe0\x00")
            except OSError:
                pass
        self.close()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
