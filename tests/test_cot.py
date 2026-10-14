import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from broadcastify_tak_bridge.cot import CotSender, build_geochat, build_marker


class CotXmlTests(unittest.TestCase):
    def test_geochat_escapes_external_text(self):
        xml = build_geochat(
            "sender&one",
            'DISPATCH "ONE"',
            "Room <A>",
            'Smoke & fire at <Main> "Street"',
        )
        root = ET.fromstring(xml)
        chat = root.find("./detail/__chat")
        remarks = root.find("./detail/remarks")
        self.assertEqual(chat.attrib["chatroom"], "Room <A>")
        self.assertEqual(remarks.text, 'Smoke & fire at <Main> "Street"')

    def test_marker_escapes_external_text(self):
        xml = build_marker(
            "incident&1",
            "a-u-G",
            40.0,
            -100.0,
            'FIRE "A&B"',
            "Alarm at Smith & Sons <warehouse>",
        )
        root = ET.fromstring(xml)
        contact = root.find("./detail/contact")
        remarks = root.find("./detail/remarks")
        self.assertEqual(contact.attrib["callsign"], 'FIRE "A&B"')
        self.assertEqual(remarks.text, "Alarm at Smith & Sons <warehouse>")


class CertificatePathTests(unittest.TestCase):
    def test_pkcs12_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be a PEM"):
            CotSender.required_pem("client certificate", "client.p12")

    def test_existing_pem_path_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.pem"
            path.write_text("test", encoding="utf-8")
            self.assertEqual(CotSender.required_pem("client certificate", str(path)), path)

    def test_missing_pem_path_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            CotSender.required_pem("client certificate", "/missing/client.pem")


if __name__ == "__main__":
    unittest.main()
