import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'feeds'))

from cot_sender import COTSender
from broadcastify_feed import build_cot_marker, build_geochat


class CotXmlTests(unittest.TestCase):
    def test_geochat_escapes_external_text(self):
        xml = build_geochat(
            'sender&one',
            'DISPATCH "ONE"',
            'Room <A>',
            'Smoke & fire at <Main> "Street"',
        )

        root = ET.fromstring(xml)
        chat = root.find('./detail/__chat')
        remarks = root.find('./detail/remarks')

        self.assertEqual(chat.attrib['chatroom'], 'Room <A>')
        self.assertEqual(remarks.text, 'Smoke & fire at <Main> "Street"')

    def test_marker_escapes_external_text(self):
        xml = build_cot_marker(
            'incident&1',
            'a-u-G',
            46.5,
            -87.5,
            'FIRE "A&B"',
            'Alarm at Smith & Sons <warehouse>',
        )

        root = ET.fromstring(xml)
        contact = root.find('./detail/contact')
        remarks = root.find('./detail/remarks')

        self.assertEqual(contact.attrib['callsign'], 'FIRE "A&B"')
        self.assertEqual(remarks.text, 'Alarm at Smith & Sons <warehouse>')


class CertificatePathTests(unittest.TestCase):
    def test_pkcs12_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must be PEM'):
            COTSender._required_pem('client certificate', 'client.p12')

    def test_missing_pem_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            COTSender._required_pem('client certificate', '/missing/client.pem')

    def test_existing_pem_path_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'client.pem'
            path.write_text('test')
            self.assertEqual(
                COTSender._required_pem('client certificate', str(path)),
                path,
            )


if __name__ == '__main__':
    unittest.main()
