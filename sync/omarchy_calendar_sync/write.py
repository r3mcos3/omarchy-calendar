"""Create, update, and delete Google Calendar events, then refresh the widget's file.

Invoked from the panel over a subprocess's stdin: one JSON object describing
the action, one JSON object printed back describing the result. Kept separate
from `cli.run` (the read-only sync loop) because the two are triggered
differently -- one on a timer, one on a click -- but this calls back into it
so a newly created event shows up without waiting for the next scheduled run.
"""

import json
from datetime import datetime, timezone

from . import cli as cli_module
from . import config as config_module
from .gws import Gws, GwsError

ACTIONS = ("create", "update", "delete")


class WriteError(Exception):
    """A problem with the payload itself, caught before any Google Calendar call."""


def _event_body(payload):
    """Build a Calendar API event resource from the panel's form fields."""
    title = str(payload.get("title", "")).strip()
    if not title:
        raise WriteError("title is required")

    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()
    if not start:
        raise WriteError("start is required")
    if not end:
        raise WriteError("end is required")

    body = {"summary": title}

    location = str(payload.get("location", "")).strip()
    if location:
        body["location"] = location

    description = str(payload.get("description", "")).strip()
    if description:
        body["description"] = description

    if payload.get("allDay"):
        body["start"] = {"date": start[:10]}
        body["end"] = {"date": end[:10]}
    else:
        tz_name = str(cli_module.resolve_local_timezone())
        body["start"] = {"dateTime": start, "timeZone": tz_name}
        body["end"] = {"dateTime": end, "timeZone": tz_name}

    return body


def handle(payload, cfg=None, client=None, resync=None, out_path=None):
    """Perform one write action and resync. Returns a JSON-safe result dict.

    `client` and `resync` are injectable so tests never shell out to gws or
    touch the real contract file; production calls leave both to build the
    real ones from `cfg`. `out_path` only matters when `resync` is left as
    None, since it is passed straight through to the real `_resync`.
    """
    if not isinstance(payload, dict):
        return {"status": "error", "message": "payload must be a JSON object"}

    action = str(payload.get("action", "")).strip()
    if action not in ACTIONS:
        return {"status": "error", "message": f"unknown action: {action!r}"}

    calendar_id = str(payload.get("calendarId", "")).strip()
    if not calendar_id:
        return {"status": "error", "message": "calendarId is required"}

    cfg = cfg if cfg is not None else config_module.load()
    client = client if client is not None else Gws(cfg["profile"], binary=cfg["gwsPath"])

    try:
        if action == "delete":
            event_id = str(payload.get("eventId", "")).strip()
            if not event_id:
                return {"status": "error", "message": "eventId is required"}
            client.delete_event(calendar_id, event_id)
        elif action == "update":
            event_id = str(payload.get("eventId", "")).strip()
            if not event_id:
                return {"status": "error", "message": "eventId is required"}
            client.patch_event(calendar_id, event_id, _event_body(payload))
        else:
            client.insert_event(calendar_id, _event_body(payload))
    except WriteError as error:
        return {"status": "error", "message": str(error)}
    except GwsError as error:
        return {"status": "error", "message": str(error)}

    if resync is not None:
        resync()
    else:
        _resync(client, cfg, out_path)

    return {"status": "success"}


def _resync(client, cfg, out_path=None):
    """Best-effort immediate refresh, so the change shows without a 5 minute wait.

    A failure here is not the write's failure: the event already landed on (or
    left) Google Calendar, and the next scheduled sync picks it up regardless.
    """
    out_path = out_path if out_path is not None else cli_module.contract.CONTRACT_PATH
    try:
        now = datetime.now(timezone.utc)
        local_tz = cli_module.resolve_local_timezone()
        # quiet=True: --write-event's stdout is the JSON result and nothing
        # else. run()'s own "wrote N rows..." line would otherwise share
        # stdout with it, and the panel's JSON.parse has no way to tell the
        # two apart.
        cli_module.run(client, cfg, now, out_path, local_tz, quiet=True)
    except Exception:
        pass
