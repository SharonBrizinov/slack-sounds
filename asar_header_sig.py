import sys
import hashlib
import struct
import io

asar = io.FileIO(sys.argv[1])
a,b,c,header_size = struct.unpack("<IIII", asar.read(16))
print(hashlib.sha256(asar.read(header_size)).hexdigest())