import unittest

from usbshare.identity import entry_key, entry_selector, new_entry, reconcile
from usbshare.names import serial_of
from usbshare.protocol import DeviceInfo
from usbshare.server import matches


def dev(busid, vid, pid, serial, name=""):
    return DeviceInfo(busid=busid, busnum=1, devnum=5, speed=2, vendor=vid, product=pid,
                      bcd_device=0, device_class=0, device_subclass=0, device_protocol=0,
                      configuration_value=1, num_configurations=1, description=name, serial=serial)


MIC = (0x352F, 0x0414)
STICK = (0x0781, 0x5567)
AUDIO = (0x18C3, 0x1008)


def entries_for(*infos_and_aliases):
    out = {}
    for info, alias in infos_and_aliases:
        entry = new_entry(info)
        entry.update(share=True, alias=alias)
        out[entry_key(entry)] = entry
    return out


class SerialDevices(unittest.TestCase):
    def setUp(self):
        self.entries = entries_for((dev("1-4", *MIC, "MIC0123456789", "Maono Mic"), "mic"))
        self.key = "352f:0414:MIC0123456789"

    def test_follows_device_to_another_port(self):
        entries, matched, _ = reconcile(self.entries, [dev("1-9", *MIC, "MIC0123456789")])
        self.assertEqual(matched, {"1-9": self.key})
        self.assertEqual(entries[self.key]["busid"], "1-9")
        self.assertEqual(entry_selector(entries[self.key]), "352f:0414:MIC0123456789=mic")

    def test_other_device_in_the_old_port_is_not_shared(self):
        _, matched, _ = reconcile(self.entries, [dev("1-4", *STICK, "AA11")])
        self.assertEqual(matched, {})

    def test_same_model_with_another_serial_is_not_shared(self):
        _, matched, _ = reconcile(self.entries, [dev("1-4", *MIC, "OTHER999")])
        self.assertEqual(matched, {})

    def test_unknown_serial_waits(self):
        _, matched, _ = reconcile(self.entries, [dev("1-4", *MIC, None)])
        self.assertEqual(matched, {})


class SerialLessDevices(unittest.TestCase):
    def setUp(self):
        self.entries = entries_for((dev("1-3", *AUDIO, "", "USB Audio"), "speaker"))

    def test_same_model_same_port_matches(self):
        _, matched, _ = reconcile(self.entries, [dev("1-3", *AUDIO, "")])
        self.assertEqual(matched, {"1-3": "18c3:1008@1-3"})

    def test_other_model_in_that_port_is_not_shared(self):
        _, matched, _ = reconcile(self.entries, [dev("1-3", *STICK, "")])
        self.assertEqual(matched, {})

    def test_moved_alone_is_followed(self):
        entries, matched, messages = reconcile(self.entries, [dev("1-7", *AUDIO, "")])
        self.assertEqual(matched, {"1-7": "18c3:1008@1-7"})
        self.assertEqual(list(entries), ["18c3:1008@1-7"])
        self.assertEqual(entries["18c3:1008@1-7"]["alias"], "speaker")
        self.assertIn("moved from port 1-3 to 1-7", messages[0])

    def test_two_identical_candidates_are_not_guessed(self):
        _, matched, _ = reconcile(self.entries, [dev("1-7", *AUDIO, ""), dev("1-8", *AUDIO, "")])
        self.assertEqual(matched, {})


class LegacyEntries(unittest.TestCase):
    def test_converted_on_first_sight_keeping_share_and_alias(self):
        legacy = {"1-6": {"share": True, "alias": "camera", "name": "UGREEN Camera 2K"}}
        self.assertIsNone(entry_selector(legacy["1-6"]))  # not shared blindly by port
        entries, matched, messages = reconcile(legacy, [dev("1-6", 0x0C45, 0x636F, "SN0001", "UGREEN Camera 2K")])
        self.assertEqual(matched, {"1-6": "0c45:636f:SN0001"})
        entry = entries["0c45:636f:SN0001"]
        self.assertEqual((entry["share"], entry["alias"]), (True, "camera"))
        self.assertEqual(entry_selector(entry), "0c45:636f:SN0001=camera")
        self.assertTrue(messages)

    def test_absent_legacy_entry_is_kept(self):
        legacy = {"1-6": {"share": True, "alias": "camera", "name": "UGREEN Camera 2K"}}
        entries, matched, _ = reconcile(legacy, [])
        self.assertEqual((entries, matched), (legacy, {}))


class Selectors(unittest.TestCase):
    def test_server_selector_forms(self):
        mic = dev("1-4", *MIC, "MIC0123456789")
        self.assertTrue(matches(mic, "352f:0414:mic0123456789=mic"))
        self.assertFalse(matches(dev("1-4", *MIC, None), "352f:0414:MIC0123456789"))
        self.assertTrue(matches(mic, "352f:0414@1-4"))
        self.assertFalse(matches(mic, "352f:0414@1-5"))
        self.assertTrue(matches(mic, "1-4") and matches(mic, "352f:0414") and matches(mic, "all"))

    def test_serial_from_windows_instance_id(self):
        self.assertEqual(serial_of({"id": r"USB\VID_352F&PID_0414\MIC0123456789"}), "MIC0123456789")
        self.assertEqual(serial_of({"id": r"USB\VID_18C3&PID_1008\5&376ABA2D&0&3"}), "")
        self.assertIsNone(serial_of({}))


if __name__ == "__main__":
    unittest.main()
