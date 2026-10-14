import unittest
import xml.etree.ElementTree as ET

from broadcastify_tak_bridge.locations import ResolvedLocation
from broadcastify_tak_bridge.pipeline import (
    BroadcastifyTakPipeline,
    classify_incident,
    clean_transcript,
    should_send,
)


class FakeSender:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)
        return True


class FakeResolver:
    unit_map = {}

    def resolve(self, transcript):
        return ResolvedLocation(40.0, -100.0, "Example Station", "approximate")


class TranscriptTests(unittest.TestCase):
    def test_short_acknowledgement_is_filtered(self):
        self.assertFalse(should_send("Copy"))

    def test_useful_transcript_is_kept(self):
        self.assertTrue(should_send("Engine one responding to a structure fire"))

    def test_repetition_cascade_is_filtered(self):
        self.assertEqual(clean_transcript("alpha bravo alpha bravo alpha bravo"), "")

    def test_incident_classification(self):
        label, color = classify_incident("Report of a structure fire")
        self.assertEqual(label, "FIRE")
        self.assertEqual(color, "-23296")

    def test_processed_transcript_creates_chat_and_marker(self):
        sender = FakeSender()
        statuses = []
        pipeline = object.__new__(BroadcastifyTakPipeline)
        pipeline.sender = sender
        pipeline.resolver = FakeResolver()
        pipeline.status_callback = lambda **values: statuses.append(values)
        pipeline.log_transcripts = False
        pipeline.min_confidence = 0.35
        pipeline.output = {
            "sender_uid": "test-bridge",
            "sender_callsign": "DISPATCH",
            "chat_room": "TEST",
            "send_markers": True,
            "marker_stale_seconds": 1800,
        }

        pipeline._process(
            "Engine one responding to a structure fire",
            0.9,
            "DISPATCH",
        )

        self.assertEqual(len(sender.events), 2)
        self.assertEqual(ET.fromstring(sender.events[0]).attrib["type"], "b-t-f")
        self.assertEqual(ET.fromstring(sender.events[1]).attrib["type"], "a-u-G")
        self.assertEqual(statuses, [{"success": True}])


if __name__ == "__main__":
    unittest.main()
