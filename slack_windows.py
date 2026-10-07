"""Windows Slack sound replacement (standard and Microsoft Store installs).

Chromium blockfile layout: net/disk_cache/blockfile/disk_format{,_base}.h.
Only the cached Hummus response and Slack's playback preference are changed.
"""
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
from datetime import datetime


INDEX_MAGIC = 0xc103cac3
BLOCK_MAGIC = 0xc104cac3
BLOCK_SIZES = {1: 36, 2: 256, 3: 1024, 4: 4096}
HUMMUS = re.compile(rb"1/0/https://[^\x00]*?/hummus[^/\x00]*\.mp3", re.I)


def persistent_hash(data):
    """Chromium's persistent SuperFastHash, with uint32 overflow."""
    mask = 0xffffffff
    value = len(data)
    for i in range(0, len(data) // 4 * 4, 4):
        value = (value + int.from_bytes(data[i:i+2], "little")) & mask
        tmp = (int.from_bytes(data[i+2:i+4], "little") << 11) ^ value
        value = ((value << 16) ^ tmp) & mask
        value = (value + (value >> 11)) & mask
    tail = data[len(data) // 4 * 4:]
    if len(tail) == 3:
        value = (value + int.from_bytes(tail[:2], "little")) & mask
        value = (value ^ (value << 16)) & mask
        value = (value ^ ((tail[2] if tail[2] < 128 else tail[2]-256) << 18)) & mask
        value = (value + (value >> 11)) & mask
    elif len(tail) == 2:
        value = (value + int.from_bytes(tail, "little")) & mask
        value = (value ^ (value << 11)) & mask
        value = (value + (value >> 17)) & mask
    elif len(tail) == 1:
        value = (value + (tail[0] if tail[0] < 128 else tail[0]-256)) & mask
        value = (value ^ (value << 10)) & mask
        value = (value + (value >> 1)) & mask
    value = (value ^ (value << 3)) & mask
    value = (value + (value >> 5)) & mask
    value = (value ^ (value << 4)) & mask
    value = (value + (value >> 17)) & mask
    value = (value ^ (value << 25)) & mask
    return (value + (value >> 6)) & mask


class RestorationError(RuntimeError):
    """A failed rollback requires manual recovery before Slack can restart."""


class BlockCache:
    def __init__(self, path):
        self.path = Path(path)
        self.files = {}
        self.changed = set()
        self.deleted = set()
        self.index = self.load("index")
        if len(self.index) < 368 or struct.unpack_from("<II", self.index) != (INDEX_MAGIC, 0x30000):
            raise ValueError("Unsupported Windows cache format (expected Chromium blockfile v3)")
        self.delta = 0

    def load(self, name):
        if name not in self.files:
            self.files[name] = bytearray((self.path / name).read_bytes())
        return self.files[name]

    def location(self, address, size):
        if not address & 0x80000000:
            raise ValueError("Uninitialized cache address")
        kind = (address >> 28) & 7
        if not kind:
            name = "f_{:06x}".format(address & 0xfffffff)
            data = self.load(name)
            offset, capacity = 0, len(data)
        else:
            if kind not in BLOCK_SIZES or address & 0x0c000000:
                raise ValueError("Unsupported cache block address")
            name = "data_{}".format((address >> 16) & 255)
            data = self.load(name)
            block_size = BLOCK_SIZES[kind]
            if len(data) < 8192 or struct.unpack_from("<II", data) != (BLOCK_MAGIC, 0x20000) or struct.unpack_from("<I", data, 12)[0] != block_size:
                raise ValueError("Invalid cache block header")
            offset = 8192 + (address & 65535) * block_size
            capacity = (((address >> 24) & 3) + 1) * block_size
        if size > capacity or offset + capacity > len(data):
            raise ValueError("Cache stream extends beyond its allocation")
        return name, data, offset, capacity

    def read(self, address, size):
        _, data, offset, _ = self.location(address, size)
        return bytes(data[offset:offset+size])

    def entries(self):
        count = struct.unpack_from("<I", self.index, 28)[0] or 65536
        if 368 + 4 * count > len(self.index):
            raise ValueError("Truncated cache index")
        seen = set()
        for (address,) in struct.iter_unpack("<I", self.index[368:368+4*count]):
            while address and address not in seen:
                seen.add(address)
                if (address >> 28) & 7 != 2:
                    raise ValueError("Unsupported cache entry layout")
                entry = self.read(address, 256)
                stored_hash = struct.unpack_from("<I", entry, 92)[0]
                if stored_hash and stored_hash != persistent_hash(entry[:92]):
                    raise ValueError("Invalid cache entry checksum")
                key_len, long_key = struct.unpack_from("<II", entry, 32)
                if long_key:
                    key = self.read(long_key, key_len)
                else:
                    key = self.read(address, 96 + key_len)[96:]
                if struct.unpack_from("<I", entry)[0] != persistent_hash(key):
                    raise ValueError("Invalid cache key checksum")
                if struct.unpack_from("<I", entry, 20)[0] == 0:
                    yield address, entry, key
                address = struct.unpack_from("<I", entry, 4)[0]

    def release(self, address):
        if not (address >> 28) & 7:
            self.deleted.add("f_{:06x}".format(address & 0xfffffff))
            return
        name, data, _, _ = self.location(address, 0)
        start, count = address & 65535, ((address >> 24) & 3) + 1
        if start % 4 + count > 4:
            raise ValueError("Invalid block allocation")
        byte = 80 + start // 8
        bits = ((1 << count) - 1) << (start % 8)
        if data[byte] & bits != bits:
            raise ValueError("Cache block is not allocated")
        nibble = (data[byte] >> (4 if start % 8 >= 4 else 0)) & 15
        trailing = 4 - count - start % 4
        if not nibble & ((15 << (4-trailing)) & 15):
            types = (4, 3, 2, 2, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0)
            new_type = types[nibble & ~(((1 << count)-1) << (start % 4))]
            if trailing:
                off = 24 + (trailing-1)*4
                struct.pack_into("<i", data, off, struct.unpack_from("<i", data, off)[0]-1)
            off = 24 + (new_type-1)*4
            struct.pack_into("<i", data, off, struct.unpack_from("<i", data, off)[0]+1)
        data[byte] &= ~bits
        struct.pack_into("<i", data, 16, struct.unpack_from("<i", data, 16)[0]-1)
        self.changed.add(name)

    def write_stream(self, address, entry, stream, payload):
        old_size = struct.unpack_from("<I", entry, 40+4*stream)[0]
        old_addr = struct.unpack_from("<I", entry, 56+4*stream)[0]
        name, data, offset, capacity = self.location(old_addr, old_size)
        if len(payload) <= capacity:
            if (old_addr >> 28) & 7:
                data[offset:offset+len(payload)] = payload
                data[offset+len(payload):offset+old_size] = b"\0" * max(0,old_size-len(payload))
            else:
                data[:] = payload
            self.changed.add(name)
        else:
            number = struct.unpack_from("<I", self.index, 16)[0] + 1
            while (self.path / "f_{:06x}".format(number)).exists() or "f_{:06x}".format(number) in self.files:
                number += 1
            if number > 0xfffffff:
                raise ValueError("Cache external file numbers exhausted")
            name = "f_{:06x}".format(number)
            self.files[name] = bytearray(payload)
            self.changed.add(name)
            self.release(old_addr)
            struct.pack_into("<I", self.index, 16, number)
            struct.pack_into("<I", entry, 56+4*stream, 0x80000000 | number)
        self.delta += len(payload)-old_size
        struct.pack_into("<I", entry, 40+4*stream, len(payload))
        struct.pack_into("<I", entry, 92, persistent_hash(entry[:92]))
        name, data, offset, _ = self.location(address, 256)
        data[offset:offset+256] = entry
        self.changed.add(name)
        self.changed.add("index")


def update_headers(data, size):
    # HttpResponseInfo uses either 32-bit or 64-bit flags before its three
    # timestamps. Both layouts store a Pickle string of HTTP headers next.
    if len(data) < 36 or struct.unpack_from("<I", data)[0] != len(data)-4:
        raise ValueError("Unsupported cached HTTP response")
    start = next((off for off in (36, 40) if data[off:off+5] == b"HTTP/"), None)
    if start is None:
        raise ValueError("Unsupported HTTP header layout")
    length = struct.unpack_from("<I", data, start-4)[0]
    end = start + length
    if end > len(data):
        raise ValueError("Truncated cached HTTP headers")
    headers = data[start:end]
    headers, count = re.subn(rb"(?i)(content-length:\s*)\d+", lambda m: m[1]+str(size).encode(), headers)
    if count != 1:
        raise ValueError("Expected one cached Content-Length header")
    headers = re.sub(rb"(?i)(content-range:\s*bytes )0-\d+/\d+", lambda m: m[1]+b"0-"+str(size-1).encode()+b"/"+str(size).encode(), headers)
    result = bytearray(data[:start-4]) + struct.pack("<I",len(headers))
    result += headers + b"\0" * (-len(headers) % 4) + data[(end+3)//4*4:]
    struct.pack_into("<I", result, 0, len(result)-4)
    return result


def patch_profile(profile, sound):
    profile = Path(profile)
    if not sound or len(sound) > 1024*1024:
        raise ValueError("Use a non-empty notification clip of at most 1 MiB")
    cache = BlockCache(profile / "Cache" / "Cache_Data")
    entries = [(a, e, k) for a, e, k in cache.entries()
               if HUMMUS.fullmatch(k) or (k.startswith(b"Range_") and b"/hummus" in k.lower())]
    parents = [(a,e,k) for a,e,k in entries if HUMMUS.fullmatch(k)]
    if not parents:
        raise ValueError("No cached Hummus sound. Select Hummus and play its preview in Slack first")
    address, parent, key = max(parents, key=lambda row: struct.unpack_from("<Q",row[1],24)[0])
    parent = bytearray(parent)
    flags = struct.unpack_from("<I",parent,72)[0]
    if flags & 1:
        control_addr = struct.unpack_from("<I",parent,64)[0]
        control = cache.read(control_addr,192)
        signature = struct.unpack_from("<Q",control)[0]
        child_key = b"Range_" + key + (":{:x}:0".format(signature)).encode()
        children = [(a,e) for a,e,k in entries if k == child_key]
        if len(children) != 1 or control[64:] != b"\x01"+b"\0"*127:
            raise ValueError("Unsupported multi-range Hummus cache entry; reselect its preview")
        child_address, child = children[0]
        child = bytearray(child)
        sparse_addr = struct.unpack_from("<I",child,64)[0]
        sparse = bytearray(cache.read(sparse_addr,192))
        old_size = struct.unpack_from("<I",child,44)[0]
        if old_size > 1024*1024:
            raise ValueError("Unsupported multi-range Hummus audio size")
        full, partial = divmod(old_size,1024)
        last_block, last_length = struct.unpack_from("<ii",sparse,16)
        expected_bitmap = ((1 << full)-1).to_bytes(128,"little")
        if (struct.unpack_from("<QII",sparse) != (signature,INDEX_MAGIC,len(key)) or
                (last_block != full or last_length != partial if partial else last_block != -1) or sparse[64:] != expected_bitmap):
            raise ValueError("Hummus audio is not fully cached from offset zero")
        full, partial = divmod(len(sound),1024)
        struct.pack_into("<ii",sparse,16,full if partial else -1,partial)
        sparse[64:] = ((1 << full)-1).to_bytes(128,"little")
        cache.write_stream(child_address,child,1,sound)
        cache.write_stream(child_address,child,2,sparse)
    else:
        cache.write_stream(address,parent,1,sound)
    header_addr, = struct.unpack_from("<I",parent,56)
    header_size, = struct.unpack_from("<I",parent,40)
    cache.write_stream(address,parent,0,update_headers(cache.read(header_addr,header_size),len(sound)))
    struct.pack_into("<q",cache.index,48,struct.unpack_from("<q",cache.index,48)[0]+cache.delta)

    settings_path = profile / "storage" / "root-state.json"
    settings = json.loads(settings_path.read_text(encoding="utf8"))
    settings["settings"]["notificationPlayback"] = "web"
    settings["settings"].setdefault("userChoices",{})["notificationPlayback"] = "web"
    local_path = profile / "local-settings.json"
    local = json.loads(local_path.read_text(encoding="utf8")) if local_path.exists() else {}
    # Store Slack otherwise switches to system playback on its first launch.
    local["didSetSystemNotificationPlayback"] = True
    writes = {cache.path/name:bytes(cache.files[name]) for name in cache.changed}
    writes[settings_path] = json.dumps(settings,ensure_ascii=False,separators=(",",":")).encode("utf8")
    writes[local_path] = json.dumps(local,ensure_ascii=False,separators=(",",":")).encode("utf8")
    deleted = {cache.path/name for name in cache.deleted}
    backup = profile / ("slack-sounds-backup-"+datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir()
    originals = set()
    for path in set(writes) | deleted:
        if path.exists():
            destination = backup / path.relative_to(profile)
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,destination)
            originals.add(path)
    attempted = []
    try:
        for path,data in writes.items():
            attempted.append(path)
            path.write_bytes(data)
        for path in deleted:
            attempted.append(path)
            path.unlink()
    except BaseException:
        failures = []
        for path in attempted:
            try:
                if path in originals:
                    shutil.copy2(backup / path.relative_to(profile),path)
                elif path.exists():
                    path.unlink()
            except OSError as error:
                failures.append("{}: {}".format(path,error))
        if failures:
            raise RestorationError("Rollback incomplete; Slack remains closed. Restore the backup at {}.\n{}".format(backup,"\n".join(failures)))
        raise
    return backup


def find_profiles(roaming=None, local=None):
    roaming = Path(roaming or os.environ["APPDATA"])
    local = Path(local or os.environ["LOCALAPPDATA"])
    candidates = [roaming / "Slack"]
    candidates += list((local / "Packages").glob("com.tinyspeck.slackdesktop_*/LocalCache/Roaming/Slack"))
    return [p for p in candidates if (p/"Cache"/"Cache_Data"/"index").exists()]


def running_slack():
    result = subprocess.run(["tasklist.exe","/FI","IMAGENAME eq slack.exe","/FO","CSV","/NH"],capture_output=True,text=True,check=True)
    return any(row and row[0].lower() == "slack.exe" for row in csv.reader(io.StringIO(result.stdout)))


def slack_executable():
    result = subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",
        "@(Get-Process -Name slack -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path -Unique) | ConvertTo-Json -Compress"],capture_output=True,text=True,check=True)
    paths = json.loads(result.stdout) if result.stdout.strip() else []
    if isinstance(paths,str):
        paths = [paths]
    paths = [p for p in (paths or []) if isinstance(p,str) and p]
    paths += [str(Path(os.environ["LOCALAPPDATA"])/"slack"/"slack.exe")]
    paths += [str(p) for p in (Path(os.environ["LOCALAPPDATA"])/"slack").glob("app-*/slack.exe")]
    return next((p for p in paths if Path(p).is_file()),None)


def main(sound_path):
    with open(sound_path, "rb") as source:
        sound = source.read(1024*1024 + 1)
    if not sound or len(sound) > 1024*1024:
        print("ERROR: Use a non-empty notification clip of at most 1 MiB")
        return 1
    profiles = find_profiles()
    if not profiles:
        print("ERROR: No Windows Slack cache found")
        return 1
    executable = slack_executable()
    safe_to_relaunch = True
    try:
        if running_slack():
            print("[-] Quitting Slack")
            subprocess.run(["taskkill.exe","/IM","slack.exe","/T","/F"],capture_output=True,check=True)
            for _ in range(20):
                if not running_slack():
                    break
                time.sleep(.5)
            else:
                raise RuntimeError("Slack did not quit; close it manually and retry")
        errors = []
        changed = 0
        for profile in profiles:
            try:
                backup = patch_profile(profile,sound)
            except ValueError as error:
                errors.append("{}: {}".format(profile,error))
                continue
            print("[-] Replaced Hummus and enabled web playback. Backup: {}".format(backup))
            changed += 1
        if not changed:
            print("ERROR: " + "\n".join(errors))
            return 1
        for error in errors:
            print("[-] Skipped profile: " + error)
        print("[-] DONE! Keep Hummus selected and test a real notification.")
        return 0
    except RestorationError:
        safe_to_relaunch = False
        raise
    finally:
        if safe_to_relaunch and executable:
            subprocess.Popen([executable],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        elif safe_to_relaunch:
            os.startfile("slack://")
