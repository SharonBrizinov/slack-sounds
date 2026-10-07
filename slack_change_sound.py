import binascii
import re
import struct
import subprocess
import shutil
import sys
import os
import time



def crc(d):
	return struct.pack("<I", binascii.crc32(d))

def x(d):
	return binascii.unhexlify(d.replace(" ",""))

def is_slack_running():
	if sys.platform == "darwin":
		return subprocess.run(["pgrep", "-x", "Slack"], stdout=subprocess.DEVNULL).returncode == 0
	return subprocess.run(["pgrep", "-x", "slack"], stdout=subprocess.DEVNULL).returncode == 0

def quit_slack():
	# Editing the cache file while Slack is running risks it treating the
	# change as corruption and silently re-fetching the original from
	# Slack's servers, undoing the edit.
	if not is_slack_running():
		return
	print("[-] Quitting Slack")
	if sys.platform == "darwin":
		subprocess.run(["osascript", "-e", 'quit app "Slack"'])
	else:
		subprocess.run(["pkill", "-x", "slack"])
	for _ in range(20):
		if not is_slack_running():
			return
		time.sleep(0.5)
	print("ERROR: Slack did not quit in time, close it manually and re-run")
	sys.exit(1)

def relaunch_slack():
	print("[-] Relaunching Slack")
	if sys.platform == "darwin":
		subprocess.Popen(["open", "-a", "Slack"])
		return
	slack_bin = shutil.which("slack")
	if slack_bin:
		subprocess.Popen([slack_bin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
	else:
		print("[-] Could not find 'slack' on PATH, please relaunch it manually")

# On macOS 12+ Slack pins notificationPlayback to "system" on every launch, so the
# OS plays the sound by name out of the app bundle and never reads the cache entry
# below - that one only feeds the Preferences preview. Linux and macOS 11 and older
# stay on "web" playback, where the cache entry is the whole story.
BUNDLE_SOUND_PATH = "/Applications/Slack.app/Contents/Resources/hummus.mp3"

def patch_native_sound(sound_data):
	# macOS 14+ App Management refuses this write unless the calling terminal was
	# granted it. ~/Library/Sounds is not a way around it - the bundle copy wins.
	try:
		with open(BUNDLE_SOUND_PATH, "wb") as f:
			f.write(sound_data)
	except OSError as e:
		print("[-] Could not write '{}' ({})".format(BUNDLE_SOUND_PATH, e))
		return e
	print("[-] Replaced the sound macOS plays: '{}'".format(BUNDLE_SOUND_PATH))
	return None

if len(sys.argv) != 2:
	print("ERROR: python3 {} NEW_SOUND_FILE.mp3".format(sys.argv[0]))
	sys.exit(1)

if sys.platform == "win32":
	from slack_windows import main
	try:
		sys.exit(main(sys.argv[1]))
	except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
		print("ERROR: {}".format(error))
		sys.exit(1)

quit_slack()

print("[-] Searching for Slack dir")
dir_slack = None
home = os.path.expanduser('~')
candidate_dirs = [
	home + "/Library/Application Support/Slack/Cache/Cache_Data",                                # macOS
	home + "/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack/Cache/Cache_Data",  # macOS (sandboxed)
	home + "/.config/Slack/Cache/Cache_Data",                                                     # Linux (native rpm/deb install)
	home + "/snap/slack/current/.config/Slack/Cache/Cache_Data",                                  # Linux (snap)
	home + "/.var/app/com.slack.Slack/config/Slack/Cache/Cache_Data",                              # Linux (flatpak)
]
for d in candidate_dirs:
	if os.path.exists(d):
		dir_slack = d
		break

if dir_slack is None:
	print("ERROR: NO ACTIVE SLACK DIR!")
	sys.exit(1)

print("[-] Slack dir found at '{}'".format(dir_slack))
print("[-] Searching for hummus sound cache file")

# Slack changes the bundle version and file hash on every release, so we
# extract the current resource path from the cache instead of hardcoding it.
candidates = []
for filename in os.listdir(dir_slack):
	if not filename.endswith("_s"):
		continue
	filepath = os.path.join(dir_slack, filename)
	try:
		with open(filepath, "rb") as f:
			data = f.read()
	except OSError:
		continue
	if b"hummus" not in data.lower():
		continue
	match = re.search(rb"1/0/https://[^\x00]*?hummus[^\x00]*?\.mp3", data, re.IGNORECASE)
	if not match:
		continue
	candidates.append((os.path.getmtime(filepath), filepath, match.group(0)))

if not candidates:
	print("ERROR: No Hummus sound cache file! Go to Slack-->Preferences-->Notifications-->Select Hummus, then close Slack and try again")
	sys.exit(1)

# Pick the most recently modified match in case stale entries linger.
candidates.sort(key=lambda c: c[0], reverse=True)
_, hummus_sound_cache_filepath, req_resource_bytes = candidates[0]
req_resource = req_resource_bytes.decode()
print("[-] Found hummus sound cache file '{}'".format(hummus_sound_cache_filepath))
print("[-] Using resource path '{}'".format(req_resource))

new_sound_file = sys.argv[1]
new_file_data = open(new_sound_file, "rb").read()
print("[-] Overwriting cache file '{}' with '{}'".format(hummus_sound_cache_filepath, new_sound_file))


new_cache_data = b""
new_cache_data += x("30 5C 72 A7 1B 6D FB FC 09 00 00 00") 	# magic
new_cache_data += struct.pack("<I", len(req_resource)) 		# resource path len
new_cache_data += crc(b"") + b"\x00\x00\x00\x00"			# ?
new_cache_data += req_resource.encode()						# requested resource
new_cache_data += x("6B 67 53 65 01 BF 97 EB 00 00 00 00 00 00 00 00") # magic ?
new_cache_data += struct.pack("<I", len(new_file_data)) + b"\x00\x00\x00\x00" 		# resource len
new_cache_data += crc(new_file_data) + b"\x00\x00\x00\x00" 	# resource len
new_cache_data += new_file_data


with open(hummus_sound_cache_filepath, "wb") as f:
	f.write(new_cache_data)

bundle_error = None
if sys.platform == "darwin":
	bundle_error = patch_native_sound(new_file_data)

relaunch_slack()

print("[-] DONE! If this is the first time editing this sound, go to Slack-->Preferences-->Notifications-->Select Hummus once so this cache entry gets used.")
if sys.platform == "darwin" and bundle_error is None:
	print("[-] NOTE: this breaks the bundle's code-signature seal (Slack still launches) and a Slack update restores the original, so re-run after updates.")
elif sys.platform == "darwin":
	print("[-] WARNING: macOS plays notification sounds from the app bundle, so only the Preferences preview changed.")
	print("             Grant App Management to this terminal (it is per app), then re-run:")
	print("             open 'x-apple.systempreferences:com.apple.preference.security?Privacy_AppBundles'")
