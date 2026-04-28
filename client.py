"""
RFP (Remote File Protocol) Client
Interactive command-line client for the RFP file transfer server.
"""

import asyncio
import json
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CLIENT] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TCP_PORT = 2077
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
    if not resp:
        print("  Upload failed: no response from server.")
        return
    if resp.get("type") == "ERR":
        print(f"  Error: {resp.get('detail', resp.get('code'))}")
        return
    if resp.get("type") != "READY":
        print(f"  Server rejected upload: {repr(resp)}")
        return

    print(f"  Uploading {src.name} ({filesize} bytes)...")
    sent = 0
    with open(src, "rb") as fh:
        while chunk := fh.read(4096):
            writer.write(chunk)
            await writer.drain()
            sent += len(chunk)

    resp = await receive_msg(reader)
    if not resp:
        print("  Upload failed: no response from server.")
        return
    if resp.get("type") == "ERR":
        print(f"  Error: {resp.get('detail', resp.get('code'))}")
        return
    if resp.get("type") == "OK":
        print(f"  Upload complete: {resp.get('detail')}")
    else:
        print(f"  Upload failed: {repr(resp)}")


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

    # Authenticate
    send_msg(writer, "AUTH", {"username": username, "password": password})
    await writer.drain()
    resp = await receive_msg(reader)

    if not resp or resp.get("type") != "AUTH_OK":
        detail = resp.get("detail", "unknown") if resp else "no response"
        log.error("Authentication failed: %s", detail)
        writer.close()
        await writer.wait_closed()
        return

    print(f"\nAuthenticated as '{username}'. Type 'help' for commands.\n")

    try:
        while True:
            try:
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
    parser.add_argument("host", help="Server IP or hostname")
    parser.add_argument("-u", required=True, help="Username")
    parser.add_argument("-p", required=True, help="Password")
    args = parser.parse_args()

    async def main():
        await run_client(args.host, args.u, args.p)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
