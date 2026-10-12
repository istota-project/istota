#!/usr/bin/env python3
"""local-vm-install Stage 1 (c): one line of reachability facts.

    netprobe.py <label> <gateway> <blocked-port> <allowed-port> <dns-server>...

TCP connects to the gateway's two ports, a UDP DNS query for debian.org to
each DNS server given, and a lookup through the container's own resolv.conf.
"""
import random
import socket
import struct
import sys


def connect(host, port):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        s.sendall(b"GET / HTTP/1.0\r\n\r\n")
        return "ok"
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()


def dns(server):
    query = struct.pack(">HHHHHH", random.randrange(65536), 0x0100, 1, 0, 0, 0)
    for part in "debian.org".split("."):
        query += bytes([len(part)]) + part.encode()
    query += b"\0" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(query, (server, 53))
        reply = s.recv(512)
        answers = struct.unpack(">H", reply[6:8])[0]
        return f"ok/{answers}"
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()


def resolv():
    try:
        socket.gethostbyname("debian.org")
        return "ok"
    except OSError as e:
        return type(e).__name__


label, gateway, blocked, allowed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
parts = [f"blocked_port={connect(gateway, blocked)}", f"allowed_port={connect(gateway, allowed)}"]
for server in sys.argv[5:]:
    parts.append(f"dns@{server}={dns(server)}")
parts.append(f"resolv.conf={resolv()}")
print(f"{label}: " + " ".join(parts))
