"""Live status for a home automation hub over MQTT ("MQTT discovery").

With an MQTT broker and a discovery prefix configured, Keepsake keeps one
connection open and tells the hub what it has, so the entities appear without
any configuration there:

  * one device, "Keepsake for iCloud", with a "Problem" and an "Attention" entity
    for the whole install (the one to alert on: it is on while any folder needs a
    person, and it names which and why) and whether the iCloud folder is confirmed, and
  * for each folder a status, the last good copy, the next run, files and bytes of the last
    run, the size in iCloud, the last copy check, the count of files macOS moved
    to iCloud only, and a "Problem" entity.

Everything is retained, so the hub has the picture the moment it starts.
A last-will message marks the device offline when Keepsake stops or loses the
network; ``availability`` is on every entity.

Nothing here can stop a sync. ``tick`` is called by the worker loop about every
30 seconds and after every run: it publishes only what changed, reconnects when
the connection is gone, and swallows every error (the first one of an outage is
logged as a plain sentence, with no address or password in it).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from keepsake.config import Config
from keepsake.mqttlite import MqttClient, MqttError

logger = logging.getLogger("keepsake")

DEVICE_ID = "keepsake_icloud"
DEVICE_NAME = "Keepsake for iCloud"
ATTENTION_MAX = 250          # an entity state is 255 characters at most


def slug(name: str) -> str:
    """A folder name as an entity id part: lower case, a to z, 0 to 9 and _.
    ``NasPhotos`` and ``Nas-Photos`` both become something valid; two folders
    never differ only by case, so ``nasphotos`` is unique."""
    out = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return out or "folder"


def title(name: str) -> str:
    """``NasSharedDrive`` -> ``Nas Shared Drive``; ``photos-2`` -> ``Photos 2``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return re.sub(r"[-_]+", " ", spaced).strip().title() or name


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class StatusPublisher:
    def __init__(self, cfg: Config, service: Any,
                 client_factory: Optional[Callable[[], Any]] = None) -> None:
        self.cfg = cfg
        self.service = service
        self._factory = client_factory
        self._client: Any = None
        self._sent: Dict[str, str] = {}      # topic -> last payload, until a reconnect
        self._known: List[str] = self._load_known()
        self._failing = False

    # -- topics ---------------------------------------------------------------
    @property
    def availability_topic(self) -> str:
        return f"{self.cfg.mqtt_prefix}/availability"

    def state_topic(self, job_slug: str) -> str:
        return f"{self.cfg.mqtt_prefix}/status/{job_slug}"

    @property
    def summary_topic(self) -> str:
        return f"{self.cfg.mqtt_prefix}/status/summary"

    def config_topic(self, component: str, unique_id: str) -> str:
        return f"{self.cfg.mqtt_discovery_prefix}/{component}/{unique_id}/config"

    # -- what the hub is told -----------------------------------------
    def _device(self) -> Dict[str, Any]:
        return {"identifiers": [DEVICE_ID], "name": DEVICE_NAME,
                "manufacturer": "Keepsake for iCloud", "model": "Folder copy to iCloud Drive"}

    def _entity(self, component: str, unique_id: str, name: str, state_topic: str,
                template: str, **extra: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "name": name, "unique_id": unique_id,
            "default_entity_id": f"{component}.{unique_id}",
            "state_topic": state_topic, "value_template": template,
            "availability_topic": self.availability_topic,
            "payload_available": "online", "payload_not_available": "offline",
            "device": self._device(),
        }
        body.update(extra)
        return body

    def discovery(self, job_slug: str, job_name: str) -> Dict[str, Dict[str, Any]]:
        """{config topic: config} for one folder. Names read as 'Photos Status'."""
        label = title(job_name)
        topic = self.state_topic(job_slug)
        uid = f"keepsake_{job_slug}"
        none_or = "{{ value_json.%s if value_json.%s is not none else none }}"
        out = {
            self.config_topic("sensor", f"{uid}_status"): self._entity(
                "sensor", f"{uid}_status", f"{label} Status", topic,
                "{{ value_json.status }}", icon="mdi:cloud-sync",
                json_attributes_topic=topic),        # every field below, in one entity
            self.config_topic("sensor", f"{uid}_last_copy"): self._entity(
                "sensor", f"{uid}_last_copy", f"{label} Last Copy", topic,
                none_or % ("last_success", "last_success"), device_class="timestamp"),
            self.config_topic("sensor", f"{uid}_next_run"): self._entity(
                "sensor", f"{uid}_next_run", f"{label} Next Run", topic,
                none_or % ("next_run", "next_run"), device_class="timestamp"),
            self.config_topic("sensor", f"{uid}_files_copied"): self._entity(
                "sensor", f"{uid}_files_copied", f"{label} Files Copied", topic,
                "{{ value_json.files_copied }}", state_class="measurement",
                icon="mdi:file-multiple"),
            self.config_topic("sensor", f"{uid}_bytes_copied"): self._entity(
                "sensor", f"{uid}_bytes_copied", f"{label} Data Copied", topic,
                "{{ value_json.bytes_copied }}", device_class="data_size",
                unit_of_measurement="B", suggested_display_precision=1,
                state_class="measurement"),
            self.config_topic("sensor", f"{uid}_icloud_size"): self._entity(
                "sensor", f"{uid}_icloud_size", f"{label} Size In iCloud", topic,
                "{{ value_json.icloud_bytes }}", device_class="data_size",
                unit_of_measurement="B", suggested_display_precision=1,
                state_class="measurement"),
            self.config_topic("sensor", f"{uid}_check"): self._entity(
                "sensor", f"{uid}_check", f"{label} Last Check", topic,
                "{{ value_json.check }}", icon="mdi:check-decagram"),
            self.config_topic("sensor", f"{uid}_evicted"): self._entity(
                "sensor", f"{uid}_evicted", f"{label} Files On iCloud Only", topic,
                "{{ value_json.evicted }}", state_class="measurement",
                icon="mdi:cloud-outline"),
            self.config_topic("binary_sensor", f"{uid}_problem"): self._entity(
                "binary_sensor", f"{uid}_problem", f"{label} Problem", topic,
                "{{ 'ON' if value_json.problem else 'OFF' }}", device_class="problem"),
        }
        return out

    def discovery_global(self) -> Dict[str, Dict[str, Any]]:
        topic = self.summary_topic
        return {
            self.config_topic("binary_sensor", "keepsake_problem"): self._entity(
                "binary_sensor", "keepsake_problem", "Problem", topic,
                "{{ 'ON' if value_json.problem else 'OFF' }}", device_class="problem"),
            self.config_topic("binary_sensor", "keepsake_icloud_confirmed"): self._entity(
                "binary_sensor", "keepsake_icloud_confirmed", "iCloud Folder Confirmed",
                topic, "{{ 'ON' if value_json.confirmed else 'OFF' }}"),
            self.config_topic("sensor", "keepsake_attention"): self._entity(
                "sensor", "keepsake_attention", "Attention", topic,
                "{{ value_json.text }}", json_attributes_topic=topic,
                json_attributes_template="{{ {'folders': value_json.folders} | tojson }}",
                icon="mdi:alert-circle-outline"),
        }

    def job_state(self, view: Dict[str, Any], attention: Dict[str, str]) -> Dict[str, Any]:
        status = str(view.get("status") or "never")
        problem = status == "failed" or view["name"] in attention
        if problem and status not in ("failed", "running", "queued", "paused"):
            status = "needs attention"
        check = (view.get("verify") or {}).get("state") or "never"
        evicted = (view.get("icloud") or {}).get("evicted") or 0
        return {"status": status, "problem": bool(problem),
                "last_success": view.get("last_success"),
                "last_run": view.get("last_run"),
                "next_run": self._future(view.get("next_run")),
                "files_copied": int(view.get("files_copied") or 0),
                "bytes_copied": int(view.get("bytes_copied") or 0),
                "icloud_bytes": int(view.get("icloud_bytes") or 0),
                "check": check, "evicted": int(evicted),
                "reason": attention.get(view["name"], "")}

    def _future(self, iso: Any) -> Optional[str]:
        """A next run only while it is still ahead. A folder that is due now has its
        time set to "now", which changes on every look: it would be republished for
        ever, so a due folder reports none."""
        try:
            at = datetime.fromisoformat(str(iso))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return str(iso) if at > self.service.clock() else None
        except (TypeError, ValueError):
            return None

    def summary(self, attention: Dict[str, str], confirmed: bool = True) -> Dict[str, Any]:
        text = "; ".join(f"{title(name)}: {why}" for name, why in sorted(attention.items()))
        return {"problem": bool(attention), "confirmed": bool(confirmed),
                "text": (text or "Nothing needs attention")[:ATTENTION_MAX],
                "folders": [{"folder": name, "reason": why}
                            for name, why in sorted(attention.items())]}

    # -- the loop --------------------------------------------------------------
    def _connect(self) -> Any:
        client = self._factory() if self._factory else MqttClient(client_id="keepsake-status")
        if self.cfg.mqtt_user:
            client.username_pw_set(self.cfg.mqtt_user, self.cfg.mqtt_password)
        client.will_set(self.availability_topic, "offline", retain=True)
        client.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, 120)
        self._sent.clear()          # a new connection resends everything (it is retained)
        return client

    def _put(self, topic: str, payload: str, retain: bool = True) -> None:
        if self._sent.get(topic) == payload:
            return
        self._client.publish(topic, payload, retain=retain)
        self._sent[topic] = payload

    def tick(self) -> None:
        """Publish what changed. Never raises."""
        try:
            self._tick()
            if self._failing:
                logger.info("the MQTT status connection is back")
            self._failing = False
        except Exception as exc:  # noqa: BLE001 - status must never stop a sync
            self._drop()
            if not self._failing:
                what = str(exc) if isinstance(exc, MqttError) else type(exc).__name__
                logger.warning("MQTT status is not being published: %s", what)
            self._failing = True

    def _tick(self) -> None:
        if self._client is None:
            self._client = self._connect()
        status = self.service.status()
        views = status["jobs"]
        attention = {a["job"]: str(a.get("reason") or "needs attention")
                     for a in status["attention"]}
        confirmed = bool(status.get("anchor_ok"))
        current = {slug(v["name"]): v for v in views}
        for job_slug, view in current.items():
            for topic, body in self.discovery(job_slug, view["name"]).items():
                self._put(topic, _json(body))
            self._put(self.state_topic(job_slug), _json(self.job_state(view, attention)))
        for topic, body in self.discovery_global().items():
            self._put(topic, _json(body))
        self._put(self.summary_topic, _json(self.summary(attention, confirmed)))
        for gone in [s for s in self._known if s not in current]:
            self._retire(gone)
        if sorted(self._known) != sorted(current):
            self._known = sorted(current)
            self._save_known()
        # Every tick, even with nothing changed: it keeps the connection alive, and
        # it is what a restarted hub needs to see the device as online.
        self._client.publish(self.availability_topic, "online", retain=True)

    def _retire(self, job_slug: str) -> None:
        """A folder that was removed: an empty retained config removes its entities."""
        for topic in self.discovery(job_slug, job_slug):
            self._client.publish(topic, "", retain=True)
        self._client.publish(self.state_topic(job_slug), "", retain=True)
        for topic in [t for t in self._sent if f"keepsake_{job_slug}_" in t
                      or t == self.state_topic(job_slug)]:
            self._sent.pop(topic, None)

    def close(self) -> None:
        """A clean stop: say so, instead of leaving the last-will to say it."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.publish(self.availability_topic, "offline", retain=True)
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def _drop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # -- the folders announced earlier (so a removed one can be retired) ---------
    def _known_file(self):
        return self.cfg.data_dir / "mqtt-known.json"

    def _load_known(self) -> List[str]:
        try:
            data = json.loads(self._known_file().read_text())
            return [str(s) for s in data] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_known(self) -> None:
        try:
            self._known_file().write_text(json.dumps(self._known))
        except OSError:
            logger.warning("could not remember the announced folders")
