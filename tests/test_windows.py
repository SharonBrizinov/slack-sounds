import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import slack_windows


KEY = b"1/0/https://a.slack-edge.com/bv1-13-br/hummus-test.mp3"
PARENT = 0xa0010000
CHILD = 0xa0010001


def block_file(size, blocks=32):
    data = bytearray(8192 + size * blocks)
    struct.pack_into("<IIhhiii", data, 0, 0xc104cac3, 0x20000, 0, 0, size, 0, blocks)
    struct.pack_into("<i", data, 36, blocks // 4)
    return data


def allocate(data, block, count):
    data[80 + block // 8] |= ((1 << count) - 1) << (block % 8)
    struct.pack_into("<i", data, 16, struct.unpack_from("<i", data, 16)[0] + 1)


def entry(key, sizes, addresses, flags=0):
    data = bytearray(256)
    struct.pack_into("<I", data, 0, slack_windows.persistent_hash(key))
    struct.pack_into("<Q", data, 24, 123)
    struct.pack_into("<I", data, 32, len(key))
    struct.pack_into("<4I", data, 40, *sizes)
    struct.pack_into("<4I", data, 56, *addresses)
    struct.pack_into("<I", data, 72, flags)
    data[96:96+len(key)] = key
    struct.pack_into("<I", data, 92, slack_windows.persistent_hash(data[:92]))
    return data


def fixture(root, sparse=True, sound_size=13270):
    cache = root / "Cache" / "Cache_Data"
    cache.mkdir(parents=True)
    sound = b"ID3" + b"o" * (sound_size-3)
    body_address = 0xc3030000 if sound_size <= 16384 else 0x80000001
    headers = b"HTTP/1.1 " + (b"206" if sparse else b"200") + b"\0content-type:audio/mpeg\0Content-Length: " + str(sound_size).encode() + b"\0"
    if sparse:
        headers += "content-range:bytes 0-{}/{}\0".format(sound_size-1,sound_size).encode()
    headers += b"\0"
    http = bytearray(36)
    struct.pack_into("<I", http, 32, len(headers))
    http += headers + b"\0" * (-len(headers) % 4) + b"unrelated-metadata"
    struct.pack_into("<I", http, 0, len(http)-4)
    one = block_file(256)
    three = block_file(4096)
    one[8192:8448] = entry(KEY, (len(http), 0 if sparse else len(sound), 192 if sparse else 0, 0),
                           (0xc0030004, 0 if sparse else body_address, 0xa0010003 if sparse else 0, 0), int(sparse))
    allocate(one, 0, 1)
    if sparse:
        child_key = b"Range_" + KEY + b":7b:0"
        one[8448:8704] = entry(child_key, (0, len(sound), 192, 0), (0,body_address,0xa0010002,0), 2)
        control = bytearray(192)
        full, partial = divmod(sound_size,1024)
        struct.pack_into("<QIIii", control, 0, 123, 0xc103cac3, len(KEY),full if partial else -1,partial)
        control[64:] = ((1 << full)-1).to_bytes(128,"little")
        one[8704:8896] = control
        parent_control = bytearray(control)
        struct.pack_into("<II",parent_control,16,0,0)
        parent_control[64:] = b"\x01" + b"\0" * 127
        one[8960:9152] = parent_control
        for block in (1,2,3):allocate(one,block,1)
    if sound_size <= 16384:
        three[8192:8192+len(sound)] = sound
        allocate(three,0,4)
    else:
        (cache/"f_000001").write_bytes(sound)
    three[8192+4*4096:8192+4*4096+len(http)] = http
    allocate(three,4,1)
    index = bytearray(368+8*4)
    struct.pack_into("<II",index,0,0xc103cac3,0x30000)
    struct.pack_into("<I",index,28,8)
    struct.pack_into("<q",index,48,len(sound)+len(http)+384 if sparse else len(sound)+len(http))
    struct.pack_into("<I",index,368,PARENT)
    if sparse:struct.pack_into("<I",index,372,CHILD)
    for name,data in (("index",index),("data_1",one),("data_3",three)):(cache/name).write_bytes(data)
    (root/"storage").mkdir()
    (root/"storage"/"root-state.json").write_text(json.dumps({"settings":{"notificationPlayback":"system","userChoices":{"notificationPlayback":"system","locale":"en-US"}},"unrelated":{"keep":42}}))
    (root/"local-settings.json").write_text(json.dumps({"didSetSystemNotificationPlayback":True,"keep":True}))
    return cache


class WindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_sparse_growth_updates_audio_headers_bitmap_and_frees_blocks(self):
        cache = fixture(self.root)
        sound = b"ID3" + b"n" * 17233
        slack_windows.patch_profile(self.root,sound)
        data = (cache/"data_1").read_bytes()
        size, = struct.unpack_from("<I",data,8448+44)
        address, = struct.unpack_from("<I",data,8448+60)
        self.assertEqual(size,len(sound))
        self.assertEqual((cache/f"f_{address & 0xfffffff:06x}").read_bytes(),sound)
        self.assertEqual(struct.unpack_from("<II",data,8704+16),(16,852))
        self.assertEqual(struct.unpack_from("<I",data,8704+64)[0],65535)
        self.assertEqual(slack_windows.persistent_hash(data[8448:8448+92]),struct.unpack_from("<I",data,8448+92)[0])
        blocks = (cache/"data_3").read_bytes()
        self.assertEqual(blocks[80]&15,0)
        self.assertIn(b"Content-Length: 17236",blocks)
        self.assertIn(b"content-range:bytes 0-17235/17236",blocks)
        self.assertIn(b"unrelated-metadata",blocks)
        settings=json.loads((self.root/"storage"/"root-state.json").read_text())
        self.assertEqual(settings["settings"]["notificationPlayback"],"web")
        self.assertEqual(settings["settings"]["userChoices"]["notificationPlayback"],"web")
        self.assertEqual(settings["unrelated"],{"keep":42})
        self.assertTrue(list(self.root.glob("slack-sounds-backup-*")))

    def test_second_run_can_shrink_external_audio(self):
        cache=fixture(self.root)
        slack_windows.patch_profile(self.root,b"ID3"+b"x"*20000)
        slack_windows.patch_profile(self.root,b"ID3"+b"y"*97)
        data=(cache/"data_1").read_bytes()
        addr=struct.unpack_from("<I",data,8448+60)[0]
        self.assertEqual((cache/f"f_{addr&0xfffffff:06x}").read_bytes(),b"ID3"+b"y"*97)
        self.assertEqual(struct.unpack_from("<II",data,8704+16),(0,100))
        self.assertEqual(struct.unpack_from("<I",data,8704+64)[0],0)

    def test_regular_response_uses_stream_one(self):
        cache=fixture(self.root,sparse=False)
        slack_windows.patch_profile(self.root,b"ID3"+b"n"*20000)
        data=(cache/"data_1").read_bytes()
        address=struct.unpack_from("<I",data,8192+60)[0]
        self.assertEqual((cache/f"f_{address&0xfffffff:06x}").read_bytes(),b"ID3"+b"n"*20000)

    def test_unknown_cache_version_changes_nothing(self):
        cache=fixture(self.root)
        p=cache/"index"; d=bytearray(p.read_bytes());struct.pack_into("<I",d,4,0xffffffff);p.write_bytes(d)
        before={p.name:p.read_bytes() for p in cache.iterdir()}
        with self.assertRaises(ValueError):slack_windows.patch_profile(self.root,b"ID3new")
        self.assertEqual(before,{p.name:p.read_bytes() for p in cache.iterdir()})
        self.assertEqual(json.loads((self.root/"storage"/"root-state.json").read_text())["settings"]["notificationPlayback"],"system")

    def test_windows_profile_discovery_includes_store_and_standard(self):
        roaming=self.root/"Roaming"; local=self.root/"Local"
        standard=roaming/"Slack"; store=local/"Packages"/"com.tinyspeck.slackdesktop_test"/"LocalCache"/"Roaming"/"Slack"
        fixture(standard); fixture(store)
        self.assertEqual(set(slack_windows.find_profiles(roaming,local)),{standard,store})

    def test_http_headers_with_64_bit_flags_and_changing_digit_count(self):
        header = b"HTTP/1.1 206\0Content-Length: 13270\0content-range:bytes 0-13269/13270\0\0"
        data = bytearray(40)
        struct.pack_into("<Q", data, 4, 0x68a476503)
        struct.pack_into("<I", data, 36, len(header))
        data += header + b"\0" * (-len(header)%4) + b"certificate-metadata"
        struct.pack_into("<I",data,0,len(data)-4)
        result = slack_windows.update_headers(data,100)
        self.assertEqual(struct.unpack_from("<I",result)[0],len(result)-4)
        self.assertEqual(result[4:36],data[4:36])
        self.assertIn(b"Content-Length: 100\0",result)
        self.assertIn(b"content-range:bytes 0-99/100\0",result)
        self.assertTrue(result.endswith(b"certificate-metadata"))

    def test_failed_write_restores_cache_and_settings(self):
        cache = fixture(self.root)
        paths = list(cache.iterdir()) + [self.root/"storage"/"root-state.json", self.root/"local-settings.json"]
        before = {p:p.read_bytes() for p in paths}
        original_write = Path.write_bytes
        def fail_settings_write(path, data):
            if path == self.root/"storage"/"root-state.json":
                raise OSError("simulated disk failure")
            return original_write(path, data)
        with patch.object(Path, "write_bytes", fail_settings_write):
            with self.assertRaisesRegex(OSError,"simulated disk failure"):
                slack_windows.patch_profile(self.root,b"ID3"+b"n"*20000)
        self.assertEqual(before,{p:p.read_bytes() for p in paths})
        self.assertEqual({p.name for p in cache.iterdir()}, {"index","data_1","data_3"})

    def test_invalid_entry_checksum_changes_nothing(self):
        cache = fixture(self.root)
        p = cache/"data_1"
        data = bytearray(p.read_bytes())
        data[8192+40] ^= 1
        p.write_bytes(data)
        before = {p.name:p.read_bytes() for p in cache.iterdir()}
        with self.assertRaisesRegex(ValueError,"checksum"):
            slack_windows.patch_profile(self.root,b"ID3new")
        self.assertEqual(before,{p.name:p.read_bytes() for p in cache.iterdir()})


    def test_aligned_sparse_audio_uses_no_partial_block_sentinel(self):
        cache = fixture(self.root, sound_size=8192)
        path = cache / "data_1"
        data = bytearray(path.read_bytes())
        # Chromium may retain an unused length when there is no partial block.
        struct.pack_into("<i", data, 8704+20, 982)
        path.write_bytes(data)
        slack_windows.patch_profile(self.root, b"ID3" + b"x" * 4093)
        data = path.read_bytes()
        self.assertEqual(struct.unpack_from("<ii", data, 8704+16), (-1, 0))
        self.assertEqual(data[8704+64:8704+192], (15).to_bytes(128, "little"))
        slack_windows.patch_profile(self.root, b"ID3" + b"y" * 8189)

    def test_full_sparse_child_can_be_replaced_repeatedly(self):
        fixture(self.root, sound_size=1024*1024)
        slack_windows.patch_profile(self.root, b"ID3" + b"x" * (1024*1024-3))
        slack_windows.patch_profile(self.root, b"ID3new")

    def test_system_playback_initialization_is_marked_complete(self):
        fixture(self.root)
        path = self.root / "local-settings.json"
        path.write_text(json.dumps({"keep": True}))
        slack_windows.patch_profile(self.root, b"ID3new")
        self.assertEqual(json.loads(path.read_text()), {"keep": True, "didSetSystemNotificationPlayback": True})

    def test_shutdown_confirmation_error_still_relaunches_slack(self):
        fixture(self.root)
        source = self.root / "sound.mp3"
        source.write_bytes(b"ID3new")
        with patch.object(slack_windows, "find_profiles", return_value=[self.root]), \
             patch.object(slack_windows, "slack_executable", return_value="Slack.exe"), \
             patch.object(slack_windows, "running_slack", side_effect=[True, slack_windows.subprocess.CalledProcessError(1, "tasklist")]), \
             patch.object(slack_windows.subprocess, "run"), \
             patch.object(slack_windows.subprocess, "Popen") as launch, \
             patch("builtins.print"):
            with self.assertRaises(slack_windows.subprocess.CalledProcessError):
                slack_windows.main(source)
            launch.assert_called_once()

    def test_failed_restoration_keeps_slack_closed_and_retains_backup(self):
        fixture(self.root)
        source = self.root / "sound.mp3"
        source.write_bytes(b"ID3" + b"n" * 20000)
        original_write = Path.write_bytes
        original_copy = slack_windows.shutil.copy2
        def fail_write(path, data):
            if path == self.root / "storage" / "root-state.json":
                raise OSError("simulated write failure")
            return original_write(path, data)
        def fail_restore(src, dst, *args, **kwargs):
            if "slack-sounds-backup-" in str(src):
                raise OSError("simulated persistent restoration failure")
            return original_copy(src, dst, *args, **kwargs)
        with patch.object(slack_windows, "find_profiles", return_value=[self.root]), \
             patch.object(slack_windows, "slack_executable", return_value="Slack.exe"), \
             patch.object(slack_windows, "running_slack", return_value=False), \
             patch.object(slack_windows.subprocess, "Popen") as launch, \
             patch.object(Path, "write_bytes", fail_write), \
             patch.object(slack_windows.shutil, "copy2", fail_restore):
            with self.assertRaises(RuntimeError):
                slack_windows.main(source)
            launch.assert_not_called()
        self.assertTrue(list(self.root.glob("slack-sounds-backup-*/Cache/Cache_Data/index")))

    def test_executable_discovery_ignores_null_process_paths(self):
        executable = self.root / "Slack.exe"
        executable.write_bytes(b"test")
        result = slack_windows.subprocess.CompletedProcess([], 0, json.dumps([None, str(executable)]))
        with patch.object(slack_windows.subprocess, "run", return_value=result), \
             patch.dict(slack_windows.os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(slack_windows.slack_executable(), str(executable))


if __name__ == "__main__":unittest.main()
