"""The standard-library MQTT client and the live status a hub reads.

A tiny broker runs in this process, so the bytes on the wire are really parsed:
what is tested is what a real broker would receive, not a fake of our own call.
"""

from __future__ import annotations

import json
import socket
import struct
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from keepsake.hamqtt import StatusPublisher, slug, title
from keepsake.mqttlite import MqttClient, MqttError
from keepsake.notifier import MqttNotifier
from keepsake.service import Service
from tests.helpers import make_cfg, make_job


def unused_port() -> int:
    """A port nothing listens on: bound and closed again, with no thread inside accept()
    (closing a listening socket another thread is blocked on does not stop it on Linux)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FakeBroker:
    """Accepts connections, answers CONNACK and PUBACK, and records what it was sent."""

    def __init__(self, refuse: int = 0) -> None:
        self.refuse = refuse
        self.connects = []     # {"client", "user", "password", "will": (topic, payload, retain)}
        self.publishes = []    # (topic, payload, retain, qos)
        self.disconnects = 0
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.server.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn, count):
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _packet(self, conn):
        first = self._read(conn, 1)[0]
        length, shift = 0, 0
        while True:
            byte = self._read(conn, 1)[0]
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        return first, self._read(conn, length)

    @staticmethod
    def _str(body, at):
        (size,) = struct.unpack("!H", body[at:at + 2])
        return body[at + 2:at + 2 + size].decode(), at + 2 + size

    def _serve(self, conn) -> None:
        try:
            while True:
                first, body = self._packet(conn)
                kind = first >> 4
                if kind == 1:                                    # CONNECT
                    _, at = self._str(body, 0)
                    flags = body[at + 1]
                    at += 4
                    client, at = self._str(body, at)
                    will = None
                    if flags & 0x04:
                        topic, at = self._str(body, at)
                        (size,) = struct.unpack("!H", body[at:at + 2])
                        will = (topic, body[at + 2:at + 2 + size].decode(), bool(flags & 0x20))
                        at += 2 + size
                    user = password = None
                    if flags & 0x80:
                        user, at = self._str(body, at)
                    if flags & 0x40:
                        password, at = self._str(body, at)
                    with self.lock:
                        self.connects.append({"client": client, "user": user,
                                              "password": password, "will": will})
                    conn.sendall(bytes([0x20, 2, 0, self.refuse]))
                elif kind == 3:                                  # PUBLISH
                    qos = (first >> 1) & 3
                    topic, at = self._str(body, 0)
                    packet_id = None
                    if qos:
                        (packet_id,) = struct.unpack("!H", body[at:at + 2])
                        at += 2
                    with self.lock:
                        self.publishes.append((topic, body[at:].decode(), bool(first & 1), qos))
                    if qos:
                        conn.sendall(bytes([0x40, 2]) + struct.pack("!H", packet_id))
                elif kind == 14:                                 # DISCONNECT
                    with self.lock:
                        self.disconnects += 1
                    return
        except (EOFError, OSError):
            return

    def settle(self, quiet=0.25, timeout=5.0):
        """Wait until the broker has stopped receiving: the count is unchanged for `quiet`."""
        import time
        end, last, since = time.time() + timeout, -1, time.time()
        while time.time() < end:
            with self.lock:
                count = len(self.publishes) + self.disconnects
            if count != last:
                last, since = count, time.time()
            elif time.time() - since >= quiet:
                return
            time.sleep(0.02)

    def wait_for(self, count, timeout=3.0):
        import time
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if len(self.publishes) >= count:
                    return
            time.sleep(0.02)

    def retained(self):
        """topic -> last payload, the way a broker keeps retained messages."""
        out = {}
        with self.lock:
            for topic, payload, retain, _ in self.publishes:
                if retain:
                    out[topic] = payload
        return out


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        self.addCleanup(self.broker.close)

    def test_connect_login_will_and_publish(self):
        client = MqttClient(client_id="ks-test")
        client.username_pw_set("house", "s3cret-pw")
        client.will_set("keepsake/availability", "offline", retain=True)
        client.connect("127.0.0.1", self.broker.port)
        client.publish("a/b", "hello", retain=True)
        client.publish("a/c", "world", qos=1)
        client.disconnect()
        self.broker.wait_for(2)
        seen = self.broker.connects[0]
        self.assertEqual((seen["client"], seen["user"], seen["password"]),
                         ("ks-test", "house", "s3cret-pw"))
        self.assertEqual(seen["will"], ("keepsake/availability", "offline", True))
        self.assertIn(("a/b", "hello", True, 0), self.broker.publishes)
        self.assertIn(("a/c", "world", False, 1), self.broker.publishes)

    def test_a_long_payload_uses_a_multi_byte_length(self):
        client = MqttClient()
        client.connect("127.0.0.1", self.broker.port)
        client.publish("big", "x" * 20000, retain=True)
        client.disconnect()
        self.broker.wait_for(1)
        self.assertEqual(len(self.broker.retained()["big"]), 20000)

    def test_a_refused_login_is_a_plain_sentence_without_the_password(self):
        refusing = FakeBroker(refuse=4)
        self.addCleanup(refusing.close)
        client = MqttClient()
        client.username_pw_set("house", "s3cret-pw")
        with self.assertRaises(MqttError) as caught:
            client.connect("127.0.0.1", refusing.port)
        self.assertIn("user name or password", str(caught.exception))
        self.assertNotIn("s3cret-pw", str(caught.exception))

    def test_an_unreachable_broker_is_a_sentence(self):
        with self.assertRaises(MqttError) as caught:
            MqttClient(timeout=1).connect("127.0.0.1", unused_port())
        self.assertIn("could not reach the broker", str(caught.exception))

    def test_publishing_when_not_connected_is_refused(self):
        with self.assertRaises(MqttError):
            MqttClient().publish("a", "b")


class NotifierThroughTheRealClientTests(unittest.TestCase):
    def test_an_event_reaches_the_broker_with_the_login(self):
        broker = FakeBroker()
        self.addCleanup(broker.close)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = replace(make_cfg(Path(tmp)), mqtt_host="127.0.0.1", mqtt_port=broker.port,
                          mqtt_user="house", mqtt_password="pw-1")
            self.assertTrue(MqttNotifier(cfg).send({"event": "failed", "job": "docs"}))
        broker.wait_for(2)
        self.assertIn("keepsake/event", [p[0] for p in broker.publishes])
        self.assertEqual(broker.retained()["keepsake/job/docs"],
                         json.dumps({"event": "failed", "job": "docs"}))
        self.assertEqual(broker.connects[0]["user"], "house")


class NamesTests(unittest.TestCase):
    def test_camel_case_and_hyphens_make_valid_unique_ids(self):
        self.assertEqual(slug("NasSharedDrive"), "nasshareddrive")
        self.assertEqual(slug("Photos-2"), "photos_2")
        self.assertEqual(slug("---"), "folder")
        self.assertEqual(slug("Docs & Notes"), "docs_notes")

    def test_titles_are_readable(self):
        self.assertEqual(title("NasSharedDrive"), "Nas Shared Drive")
        self.assertEqual(title("photos-2"), "Photos 2")


class StatusPublisherTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        self.addCleanup(self.broker.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = replace(make_cfg(self.root), mqtt_host="127.0.0.1",
                           mqtt_port=self.broker.port, mqtt_user="house",
                           mqtt_password="pw-2", mqtt_discovery_prefix="hubdisc")
        self.svc = Service(self.cfg)
        self.svc.store.save([make_job(name="PhotoLibrary", dest="PhotoLibrary")])

    def tick(self):
        self.svc.status_publisher.tick()
        self.broker.settle()

    def test_a_service_with_a_broker_builds_a_publisher_and_without_one_does_not(self):
        self.assertIsNotNone(self.svc.status_publisher)
        plain = Service(make_cfg(self.root / "other") if (self.root / "other").mkdir() is None
                        else None)
        self.assertIsNone(plain.status_publisher)
        off = Service(replace(self.cfg, mqtt_discovery_prefix=""))
        self.assertIsNone(off.status_publisher)

    def test_first_tick_announces_the_device_and_every_entity_retained(self):
        self.tick()
        retained = self.broker.retained()
        configs = {t: json.loads(p) for t, p in retained.items() if t.endswith("/config")}
        ids = sorted(c["unique_id"] for c in configs.values())
        self.assertEqual(ids, sorted([
            "keepsake_problem", "keepsake_attention", "keepsake_icloud_confirmed",
            *[f"keepsake_photolibrary_{f}" for f in
              ("status", "last_copy", "next_run", "files_copied", "bytes_copied", "icloud_size",
               "check", "evicted", "problem")]]))
        for topic, body in configs.items():
            self.assertTrue(topic.startswith("hubdisc/"))
            self.assertEqual(body["device"]["identifiers"], ["keepsake_icloud"])
            self.assertEqual(body["availability_topic"], "keepsake/availability")
            self.assertEqual(body["default_entity_id"].split(".", 1)[1], body["unique_id"])
            self.assertRegex(body["unique_id"], r"^[a-z0-9_]+$")
        self.assertEqual(retained["keepsake/availability"], "online")
        self.assertEqual(self.broker.connects[0]["will"],
                         ("keepsake/availability", "offline", True))
        self.assertEqual(self.broker.connects[0]["user"], "house")

    def test_the_status_entity_carries_every_field_as_attributes(self):
        self.tick()
        body = json.loads(self.broker.retained()[
            "hubdisc/sensor/keepsake_photolibrary_status/config"])
        self.assertEqual(body["json_attributes_topic"], "keepsake/status/photolibrary")

    def test_the_install_entities_are_not_named_keepsake_twice(self):
        # The device is called "Keepsake for iCloud", so the entity name must not repeat it.
        self.tick()
        names = {json.loads(p)["unique_id"]: json.loads(p)["name"]
                 for t, p in self.broker.retained().items()
                 if t.endswith("/config") and "keepsake_photolibrary" not in t}
        self.assertEqual(names, {"keepsake_problem": "Problem", "keepsake_attention": "Attention",
                                 "keepsake_icloud_confirmed": "iCloud Folder Confirmed"})

    def test_the_entity_names_read_like_titles(self):
        self.tick()
        body = json.loads(self.broker.retained()[
            "hubdisc/sensor/keepsake_photolibrary_status/config"])
        self.assertEqual(body["name"], "Photo Library Status")

    def test_state_is_published_only_when_it_changes(self):
        self.tick()
        first = len(self.broker.publishes)
        self.tick()
        again = self.broker.publishes[first:]
        self.assertEqual([p[0] for p in again], ["keepsake/availability"])   # the keep-alive only
        self.svc.state["PhotoLibrary"] = {"result": "ok", "last_run": "2026-11-10T10:00:00+00:00",
                                          "last_success": "2026-11-10T10:00:00+00:00",
                                          "files_copied": 12, "bytes_copied": 2048}
        self.tick()
        state = json.loads(self.broker.retained()["keepsake/status/photolibrary"])
        self.assertEqual((state["status"], state["files_copied"], state["bytes_copied"],
                          state["problem"]), ("ok", 12, 2048, False))

    def test_a_failed_folder_turns_the_problem_entities_on_and_names_why(self):
        self.svc.state["PhotoLibrary"] = {"result": "failed", "error": "the share is not mounted",
                                          "last_run": "2026-11-09T01:00:00+00:00",
                                          "last_success": "2026-11-07T01:00:00+00:00",
                                          "created": "2026-11-01T00:00:00+00:00"}
        self.tick()
        retained = self.broker.retained()
        job = json.loads(retained["keepsake/status/photolibrary"])
        summary = json.loads(retained["keepsake/status/summary"])
        self.assertEqual((job["status"], job["problem"]), ("failed", True))
        self.assertTrue(summary["problem"])
        self.assertIn("Photo Library", summary["text"])
        self.assertEqual(summary["folders"][0]["folder"], "PhotoLibrary")

    def test_all_quiet_says_so(self):
        self.tick()
        summary = json.loads(self.broker.retained()["keepsake/status/summary"])
        self.assertEqual((summary["problem"], summary["text"]),
                         (False, "Nothing needs attention"))
        self.assertTrue(summary["confirmed"])           # make_cfg writes the confirmation marker

    def test_a_removed_folder_is_retired_with_empty_retained_configs(self):
        self.tick()
        self.svc.store.save([])
        self.tick()
        retained = self.broker.retained()
        self.assertEqual(retained["hubdisc/sensor/keepsake_photolibrary_status/config"], "")
        self.assertEqual(retained["keepsake/status/photolibrary"], "")
        self.assertEqual(json.loads((self.cfg.data_dir / "mqtt-known.json").read_text()), [])

    def test_a_removed_folder_is_retired_even_after_a_restart(self):
        self.tick()
        self.svc.store.save([])
        again = Service(self.cfg)
        again.status_publisher.tick()
        self.broker.settle()
        self.assertEqual(self.broker.retained()[
            "hubdisc/sensor/keepsake_photolibrary_check/config"], "")

    def test_a_broker_that_is_down_never_raises_and_recovers(self):
        publisher = StatusPublisher(replace(self.cfg, mqtt_port=unused_port()), self.svc)
        publisher.tick()                       # must not raise
        publisher.tick()
        self.assertIsNone(publisher._client)

    def test_close_says_offline_and_disconnects(self):
        self.tick()
        self.svc.status_publisher.close()
        self.broker.settle()
        self.assertEqual(self.broker.retained()["keepsake/availability"], "offline")
        self.assertEqual(self.broker.disconnects, 1)

    def test_without_a_discovery_prefix_alerts_still_work(self):
        cfg = replace(self.cfg, mqtt_discovery_prefix="")
        self.assertIsNone(Service(cfg).status_publisher)
        self.assertEqual(len(Service(cfg).notifiers), 1)     # the alert channel is still there


if __name__ == "__main__":
    unittest.main()
