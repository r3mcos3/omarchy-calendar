import contextlib
import io
import unittest

from omarchy_calendar_sync import write
from omarchy_calendar_sync.gws import GwsAuthError, GwsError


class FakeClient:
    """Records every call instead of shelling out to gws."""

    def __init__(self, raise_error=None):
        self.calls = []
        self.raise_error = raise_error

    def insert_event(self, calendar_id, body):
        self.calls.append(("insert", calendar_id, None, body))
        if self.raise_error:
            raise self.raise_error
        return {"id": "new-evt", **body}

    def patch_event(self, calendar_id, event_id, body):
        self.calls.append(("patch", calendar_id, event_id, body))
        if self.raise_error:
            raise self.raise_error
        return {"id": event_id, **body}

    def delete_event(self, calendar_id, event_id):
        self.calls.append(("delete", calendar_id, event_id, None))
        if self.raise_error:
            raise self.raise_error
        return {}

    # The methods below let this double stand in for cli.run()'s real
    # resync too, not just the write itself -- needed to test that the
    # resync it triggers stays quiet on stdout.
    def check(self):
        return None

    def version(self):
        return (0, 22, 5)

    def calendars(self):
        return [{"id": "a@example.com", "name": "Personal", "color": "#f83a22"}]

    def events(self, calendar_id, time_min, time_max):
        return []


def resynced_flag():
    """Returns (resync_callable, was_it_called_list) for asserting a resync happened."""
    calls = []
    return (lambda: calls.append(True)), calls


class TestEventBody(unittest.TestCase):
    def test_requires_title(self):
        with self.assertRaises(write.WriteError):
            write._event_body({"start": "2026-08-10T09:00:00", "end": "2026-08-10T10:00:00"})

    def test_requires_start(self):
        with self.assertRaises(write.WriteError):
            write._event_body({"title": "Lunch", "end": "2026-08-10T10:00:00"})

    def test_requires_end(self):
        with self.assertRaises(write.WriteError):
            write._event_body({"title": "Lunch", "start": "2026-08-10T09:00:00"})

    def test_timed_event_gets_local_timezone(self):
        body = write._event_body({
            "title": "Lunch",
            "start": "2026-08-10T09:00:00",
            "end": "2026-08-10T10:00:00",
        })
        self.assertEqual(body["summary"], "Lunch")
        self.assertEqual(body["start"]["dateTime"], "2026-08-10T09:00:00")
        self.assertIn("timeZone", body["start"])
        self.assertIn("timeZone", body["end"])

    def test_all_day_event_uses_date_not_datetime(self):
        body = write._event_body({
            "title": "Vacation",
            "start": "2026-08-10",
            "end": "2026-08-11",
            "allDay": True,
        })
        self.assertEqual(body["start"], {"date": "2026-08-10"})
        self.assertEqual(body["end"], {"date": "2026-08-11"})

    def test_optional_fields_omitted_when_blank(self):
        body = write._event_body({
            "title": "Lunch",
            "start": "2026-08-10T09:00:00",
            "end": "2026-08-10T10:00:00",
            "location": "  ",
            "description": "",
        })
        self.assertNotIn("location", body)
        self.assertNotIn("description", body)

    def test_optional_fields_included_when_present(self):
        body = write._event_body({
            "title": "Lunch",
            "start": "2026-08-10T09:00:00",
            "end": "2026-08-10T10:00:00",
            "location": "Cafe",
            "description": "Bring the report",
        })
        self.assertEqual(body["location"], "Cafe")
        self.assertEqual(body["description"], "Bring the report")


class TestHandleValidation(unittest.TestCase):
    def test_rejects_non_dict_payload(self):
        result = write.handle("not a dict")
        self.assertEqual(result["status"], "error")

    def test_rejects_unknown_action(self):
        result = write.handle({"action": "archive", "calendarId": "a"})
        self.assertEqual(result["status"], "error")
        self.assertIn("archive", result["message"])

    def test_requires_calendar_id(self):
        result = write.handle({"action": "create"})
        self.assertEqual(result["status"], "error")
        self.assertIn("calendarId", result["message"])

    def test_update_requires_event_id(self):
        client = FakeClient()
        result = write.handle({"action": "update", "calendarId": "a"}, client=client)
        self.assertEqual(result["status"], "error")
        self.assertIn("eventId", result["message"])
        self.assertEqual(client.calls, [])

    def test_delete_requires_event_id(self):
        client = FakeClient()
        result = write.handle({"action": "delete", "calendarId": "a"}, client=client)
        self.assertEqual(result["status"], "error")
        self.assertIn("eventId", result["message"])


class TestHandleCreate(unittest.TestCase):
    def test_creates_event_and_resyncs(self):
        client = FakeClient()
        resync, resynced = resynced_flag()
        result = write.handle(
            {
                "action": "create",
                "calendarId": "a@example.com",
                "title": "Lunch",
                "start": "2026-08-10T09:00:00",
                "end": "2026-08-10T10:00:00",
            },
            client=client,
            resync=resync,
        )
        self.assertEqual(result, {"status": "success"})
        self.assertEqual(client.calls[0][0], "insert")
        self.assertEqual(client.calls[0][1], "a@example.com")
        self.assertEqual(resynced, [True])

    def test_real_resync_prints_nothing_to_stdout(self):
        # --write-event's stdout is the JSON result, read by the panel with
        # JSON.parse. run()'s own "wrote N rows..." line landing on the same
        # stream (this test leaves `resync` as None, so the real _resync
        # runs) would break that parse even though the write itself succeeded.
        import tempfile
        from pathlib import Path

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "calendar-events.json"
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                result = write.handle(
                    {
                        "action": "create",
                        "calendarId": "a@example.com",
                        "title": "Lunch",
                        "start": "2026-08-10T09:00:00",
                        "end": "2026-08-10T10:00:00",
                    },
                    cfg={"profile": "/tmp/profile", "gwsPath": "gws", "calendars": {"include": [], "exclude": []}, "window": {"pastDays": 7, "futureDays": 60}},
                    client=client,
                    out_path=out_path,
                )

            self.assertEqual(result, {"status": "success"})
            self.assertEqual(captured.getvalue(), "")
            self.assertTrue(out_path.exists())

    def test_bad_form_fields_never_reach_the_client(self):
        client = FakeClient()
        result = write.handle({"action": "create", "calendarId": "a"}, client=client)
        self.assertEqual(result["status"], "error")
        self.assertEqual(client.calls, [])

    def test_gws_error_is_reported_not_raised(self):
        client = FakeClient(raise_error=GwsAuthError("403: insufficient scopes"))
        result = write.handle(
            {
                "action": "create",
                "calendarId": "a",
                "title": "Lunch",
                "start": "2026-08-10T09:00:00",
                "end": "2026-08-10T10:00:00",
            },
            client=client,
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("insufficient scopes", result["message"])


class TestHandleUpdate(unittest.TestCase):
    def test_patches_event_and_resyncs(self):
        client = FakeClient()
        resync, resynced = resynced_flag()
        result = write.handle(
            {
                "action": "update",
                "calendarId": "a",
                "eventId": "evt1",
                "title": "Lunch (moved)",
                "start": "2026-08-10T09:30:00",
                "end": "2026-08-10T10:30:00",
            },
            client=client,
            resync=resync,
        )
        self.assertEqual(result, {"status": "success"})
        self.assertEqual(client.calls[0], ("patch", "a", "evt1", {
            "summary": "Lunch (moved)",
            "start": {"dateTime": "2026-08-10T09:30:00", "timeZone": client.calls[0][3]["start"]["timeZone"]},
            "end": {"dateTime": "2026-08-10T10:30:00", "timeZone": client.calls[0][3]["end"]["timeZone"]},
        }))
        self.assertEqual(resynced, [True])


class TestHandleDelete(unittest.TestCase):
    def test_deletes_and_resyncs_without_needing_form_fields(self):
        client = FakeClient()
        resync, resynced = resynced_flag()
        result = write.handle(
            {"action": "delete", "calendarId": "a", "eventId": "evt1"},
            client=client,
            resync=resync,
        )
        self.assertEqual(result, {"status": "success"})
        self.assertEqual(client.calls, [("delete", "a", "evt1", None)])
        self.assertEqual(resynced, [True])

    def test_gws_error_on_delete_is_reported_not_raised(self):
        client = FakeClient(raise_error=GwsError("500: boom"))
        result = write.handle(
            {"action": "delete", "calendarId": "a", "eventId": "evt1"},
            client=client,
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("boom", result["message"])


if __name__ == "__main__":
    unittest.main()
