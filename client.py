"""
RFP (Remote File Protocol) Client
Interactive command-line client for the RFP file transfer server.
"""

import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CLIENT] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TCP_PORT = 2077
UDP_PORT = 2078
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_RETRIES = 3
CONNECT_TIMEOUT = 5.0
IO_TIMEOUT = 30.0


# --------
# Helpers
# --------

def send_msg(writer: asyncio.StreamWriter, msg_type: str, payload: dict = None):
    # Match server framing: one JSON control message per line.
    line = json.dumps({"type": msg_type, **(payload or {})}) + "\r\n"
    writer.write(line.encode())


async def receive_msg(reader: asyncio.StreamReader) -> dict | None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=IO_TIMEOUT)
    except asyncio.TimeoutError:
        log.error("Timed out waiting for server response.")
        return None
    if not line:
        return None
    try:
        return json.loads(line.decode().strip())
    except json.JSONDecodeError:
        log.error("Received malformed response from server.")
        return None


# -------------
# UDP listener
# -------------

class UDPNotifyProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr):
        try:
            msg = json.loads(data.decode())
            if msg.get("type") == "NOTIFY":
                event = msg.get("event", "?")
                filename = msg.get("filename", "?")
                print(f"\n*** SERVER NOTIFICATION: {event} → {filename} ***\n> ", end="", flush=True)
        except Exception:
            pass

    def error_received(self, exc):
        log.debug("UDP error: %s", exc)


async def start_udp_listener(local_udp_port: int):
    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(
            UDPNotifyProtocol, local_addr=("0.0.0.0", local_udp_port)
        )
        log.info("UDP notification listener on port %d", local_udp_port)
    except OSError as e:
        log.warning("Could not bind UDP port %d: %s (notifications disabled)", local_udp_port, e)


# --------------
# UDP discovery
# --------------

async def discover_server(broadcast_addr: str = "255.255.255.255") -> str | None:
    """Send a UDP DISCOVER and wait for DISCOVER_ACK. Returns server IP or None."""
    msg = json.dumps({"type": "DISCOVER"}).encode()
    loop = asyncio.get_running_loop()
    result = asyncio.Future()

    class DiscoverProtocol(asyncio.DatagramProtocol):
        def __init__(self, transport_holder):
            self._t = transport_holder

        def connection_made(self, transport):
            self._t.append(transport)
            transport.sendto(msg, (broadcast_addr, UDP_PORT))

        def datagram_received(self, data, addr):
            try:
                resp = json.loads(data.decode())
                if resp.get("type") == "DISCOVER_ACK" and not result.done():
                    result.set_result(addr[0])
            except Exception:
                pass

    transport_holder = []
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoverProtocol(transport_holder),
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    try:
        return await asyncio.wait_for(result, timeout=3.0)
    except asyncio.TimeoutError:
        return None
    finally:
        transport.close()


# ------------------------
# Command implementations
# ------------------------

async def cmd_list(reader, writer):
    send_msg(writer, "LIST")
    await writer.drain()
    resp = await receive_msg(reader)
    if resp and resp.get("type") == "LIST_RESP":
        files = resp.get("files", [])
        if not files:
            print("  (no files on server)")
        else:
            print(f"  {'Name':<40} {'Size':>10}")
            print(f"  {'-'*40} {'-'*10}")
            for f in files:
                print(f"  {f['name']:<40} {f['size']:>10} bytes")
    else:
        print(f"  Error: {resp}")


async def cmd_upload(reader, writer, local_path: str):
    src = Path(local_path)
    if not src.is_file():
        print(f"  File not found: {local_path}")
        return

    filesize = src.stat().st_size
    send_msg(writer, "UPLOAD", {"filename": src.name, "size": filesize})
    await writer.drain()

    resp = await receive_msg(reader)
    if not resp or resp.get("type") != "READY":
        print(f"  Server rejected upload: {resp}")
        return

    print(f"  Uploading {src.name} ({filesize} bytes)...")
    sent = 0
    with open(src, "rb") as fh:
        while chunk := fh.read(4096):
            writer.write(chunk)
            await writer.drain()
            sent += len(chunk)

    resp = await receive_msg(reader)
    if resp and resp.get("type") == "OK":
        print(f"  Upload complete: {resp.get('detail')}")
    else:
        print(f"  Upload failed: {resp}")


async def cmd_download(reader, writer, filename: str):
    send_msg(writer, "DOWNLOAD", {"filename": filename})
    await writer.drain()

    resp = await receive_msg(reader)
    if not resp:
        print("  No response from server.")
        return
    if resp.get("type") == "ERR":
        print(f"  Error: {resp.get('detail', resp.get('code'))}")
        return
    if resp.get("type") != "FILE_DATA":
        print(f"  Unexpected response: {resp}")
        return

    filesize = resp["size"]
    dest = DOWNLOAD_DIR / filename
    print(f"  Downloading {filename} ({filesize} bytes)...")

    received = 0
    with open(dest, "wb") as fh:
        while received < filesize:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(min(4096, filesize - received)), timeout=IO_TIMEOUT
                )
            except asyncio.TimeoutError:
                print("  Download timed out!")
                return
            if not chunk:
                print("  Connection closed during download.")
                return
            fh.write(chunk)
            received += len(chunk)

    print(f"  Saved to {dest.resolve()}")


async def cmd_delete(reader, writer, filename: str):
    send_msg(writer, "DELETE", {"filename": filename})
    await writer.drain()
    resp = await receive_msg(reader)
    if resp and resp.get("type") == "OK":
        print(f"  {resp.get('detail')}")
    else:
        print(f"  Error: {resp}")


# ----------------------
# Main interactive loop
# ----------------------

async def run_client(host: str, username: str, password: str):
    local_tcp_port = None  # will be set after connect

    # Try connecting with retry + backoff
    reader, writer = None, None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info("Connecting to %s:%d (attempt %d/%d)...", host, TCP_PORT, attempt, MAX_RETRIES)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, TCP_PORT), timeout=CONNECT_TIMEOUT
            )
            local_tcp_port = writer.get_extra_info("sockname")[1]
            log.info("Connected from local port %d", local_tcp_port)
            break
        except ConnectionRefusedError:
            log.error("Connection refused — is the server running on %s:%d?", host, TCP_PORT)
        except asyncio.TimeoutError:
            log.error("Connection timed out.")
        except OSError as e:
            log.error("Connection error: %s", e)

        if attempt < MAX_RETRIES:
            wait = 2 ** attempt
            log.info("Retrying in %d seconds...", wait)
            await asyncio.sleep(wait)

    if not writer:
        log.error("Could not connect after %d attempts. Exiting.", MAX_RETRIES)
        return

    # Start UDP notification listener (client UDP port = TCP port + 1 by convention)
    # The server uses this deterministic port rule when broadcasting NOTIFY events.
    udp_listen_port = local_tcp_port + 1
    await start_udp_listener(udp_listen_port)

    # Authenticate
    send_msg(writer, "AUTH", {"username": username, "password": password})
    await writer.drain()
    resp = await receive_msg(reader)

    if not resp or resp.get("type") != "AUTH_OK":
        detail = resp.get("detail", "unknown") if resp else "no response"
        log.error("Authentication failed: %s", detail)
        writer.close()
        return

    print(f"\nAuthenticated as '{username}'. Type 'help' for commands.\n")

    try:
        while True:
            try:
                # `input()` blocks, so run it in a thread to keep the event loop responsive
                # for network I/O and incoming UDP notifications.
                line = await asyncio.get_event_loop().run_in_executor(None, lambda: input("> "))
            except EOFError:
                break

            parts = line.strip().split(None, 1)
            if not parts:
                continue
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "help":
                print("  list                  — list files on server")
                print("  upload <local_path>   — upload a file")
                print("  download <filename>   — download a file")
                print("  delete <filename>     — delete a file from server")
                print("  discover              — broadcast UDP discovery")
                print("  quit                  — disconnect and exit")

            elif cmd == "list":
                await cmd_list(reader, writer)

            elif cmd == "upload":
                if not arg:
                    print("  Usage: upload <local_path>")
                else:
                    await cmd_upload(reader, writer, arg)

            elif cmd == "download":
                if not arg:
                    print("  Usage: download <filename>")
                else:
                    await cmd_download(reader, writer, arg)

            elif cmd == "delete":
                if not arg:
                    print("  Usage: delete <filename>")
                else:
                    await cmd_delete(reader, writer, arg)

            elif cmd == "discover":
                print("  Broadcasting UDP discovery...")
                ip = await discover_server()
                if ip:
                    print(f"  Found RFP server at {ip}")
                else:
                    print("  No server responded.")

            elif cmd == "quit":
                send_msg(writer, "QUIT")
                await writer.drain()
                await receive_msg(reader)
                break

            else:
                print(f"  Unknown command: {cmd}. Type 'help'.")

    except ConnectionResetError:
        log.error("Server closed the connection unexpectedly.")
    except asyncio.TimeoutError:
        log.error("Operation timed out.")
    except Exception as e:
        log.error("Unexpected error: %s", e, exc_info=True)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        log.info("Disconnected.")


# ------------
# Entry point
# ------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RFP Client")
    parser.add_argument("host", nargs="?", default=None, help="Server IP (omit to discover via UDP)")
    parser.add_argument("-u", "--username", default="guest")
    parser.add_argument("-p", "--password", default="guest")
    args = parser.parse_args()

    async def main():
        host = args.host
        if not host:
            print("No host specified — broadcasting UDP discovery...")
            host = await discover_server()
            if not host:
                print("No server found. Specify host manually: python client.py <host>")
                return
            print(f"Found server at {host}")
        await run_client(host, args.username, args.password)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
