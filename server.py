"""
RFP (Remote File Protocol) Server
TCP port 2077 - file transfer and control
"""

import asyncio
import json
import logging
import socket
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# --------------
# Configuration
# --------------

TCP_HOST = "0.0.0.0"
TCP_PORT = 2077
STORAGE_DIR = Path("./server_files")
STORAGE_DIR.mkdir(exist_ok=True)

# Simple credential store (username : password)
USERS = {
        "dev": "dev123",
        "user": "user123",
        "guest": "guest123",
        "admin": "admin123"
        }

# --------
# Helpers
# --------

def send_msg(writer: asyncio.StreamWriter, msg_type: str, payload: dict) -> None:
    """Encode and send a JSON message terminated with \r\n."""
    line = json.dumps({"type": msg_type, **payload}) + "\r\n"
    writer.write(line.encode())


def get_connect_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # This lets us discover the IP to connect the client to
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and ip != "0.0.0.0":
                return ip
    except OSError:
        pass
    return "127.0.0.1"


async def receive_msg(reader: asyncio.StreamReader) -> dict | None:
    """Read one \r\n-terminated JSON message. Returns None on disconnect."""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=30.0)
    except asyncio.TimeoutError:
        return None
    if not line:
        return None
    try:
        return json.loads(line.decode().strip())
    except json.JSONDecodeError:
        return {"type": "MALFORMED"}


# -------------------------
# TCP - per-client handler
# -------------------------

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    peer_str = f"{peer[0]}:{peer[1]}"
    log.info("TCP connection from %s", peer_str)

    username = None

    try:
        # AUTH phase
        msg = await receive_msg(reader)
        if not msg or msg.get("type") != "AUTH":
            send_msg(writer, "ERR", {"code": "AUTH_REQUIRED", "detail": "First message must be AUTH"})
            await writer.drain()
            return

        uname = msg.get("username", "")
        passwd = msg.get("password", "")
        if USERS.get(uname) != passwd:
            send_msg(writer, "AUTH_ERR", {"detail": "Bad credentials"})
            await writer.drain()
            log.warning("Failed auth attempt for user '%s' from %s", uname, peer_str)
            return

        username = uname
        send_msg(writer, "AUTH_OK", {"username": username})
        await writer.drain()
        log.info("User '%s' authenticated from %s", username, peer_str)

        # Command loop
        while True:
            msg = await receive_msg(reader)
            if msg is None:
                log.info("Client '%s' (%s) disconnected", username, peer_str)
                break

            mtype = msg.get("type", "")

            if mtype == "MALFORMED":
                send_msg(writer, "ERR", {"code": "MALFORMED", "detail": "Could not parse message"})
                await writer.drain()
                continue

            elif mtype == "LIST":
                files = []
                for f in STORAGE_DIR.iterdir():
                    if f.is_file():
                        files.append({"name": f.name, "size": f.stat().st_size})
                send_msg(writer, "LIST_RESP", {"files": files})
                await writer.drain()
                log.info("'%s' requested directory listing (%d files)", username, len(files))

            elif mtype == "UPLOAD":
                filename = msg.get("filename", "")
                filesize = msg.get("size", 0)

                if not filename or "/" in filename or "\\" in filename:
                    send_msg(writer, "ERR", {"code": "INVALID_FILENAME"})
                    await writer.drain()
                    continue
                if filesize <= 0 or filesize > 100 * 1024 * 1024:  # 100 MB cap
                    send_msg(writer, "ERR", {"code": "INVALID_SIZE"})
                    await writer.drain()
                    continue

                dest = STORAGE_DIR / filename
                if dest.is_file():
                    send_msg(writer, "ERR", {"code": "FILE_EXISTS", "detail": f"'{filename}' already exists on the server (delete it or choose another name)"})
                    await writer.drain()
                    log.info("'%s' upload rejected: file exists '%s'", username, filename)
                    continue

                send_msg(writer, "READY", {})
                await writer.drain()

                received = 0
                with open(dest, "wb") as fh:
                    # File bytes come right after READY, so keep reading fixed-size chunks until we reach the announced file size.
                    while received < filesize:
                        chunk = await asyncio.wait_for(
                            reader.read(min(4096, filesize - received)), timeout=30.0
                        )
                        if not chunk:
                            break
                        fh.write(chunk)
                        received += len(chunk)

                if received == filesize:
                    send_msg(writer, "OK", {"detail": f"Uploaded {filename} ({received} bytes)"})
                    await writer.drain()
                    log.info("'%s' uploaded '%s' (%d bytes)", username, filename, received)
                else:
                    dest.unlink(missing_ok=True)
                    send_msg(writer, "ERR", {"code": "INCOMPLETE", "detail": "Transfer incomplete"})
                    await writer.drain()

            elif mtype == "DOWNLOAD":
                filename = msg.get("filename", "")
                src = STORAGE_DIR / filename

                if not filename or "/" in filename or not src.is_file():
                    send_msg(writer, "ERR", {"code": "NOT_FOUND", "detail": f"'{filename}' not found"})
                    await writer.drain()
                    continue

                filesize = src.stat().st_size
                # First send metadata (name + size), then stream raw bytes so the client knows exactly how many bytes to expect.
                send_msg(writer, "FILE_DATA", {"filename": filename, "size": filesize})
                await writer.drain()

                with open(src, "rb") as fh:
                    while chunk := fh.read(4096):
                        writer.write(chunk)
                        await writer.drain()

                log.info("'%s' downloaded '%s' (%d bytes)", username, filename, filesize)

            elif mtype == "DELETE":
                filename = msg.get("filename", "")
                target = STORAGE_DIR / filename
                if not filename or not target.is_file():
                    send_msg(writer, "ERR", {"code": "NOT_FOUND"})
                else:
                    target.unlink()
                    send_msg(writer, "OK", {"detail": f"Deleted {filename}"})
                    log.info("'%s' deleted '%s'", username, filename)
                await writer.drain()

            elif mtype == "QUIT":
                send_msg(writer, "OK", {"detail": "Goodbye"})
                await writer.drain()
                log.info("'%s' sent QUIT", username)
                break

            else:
                send_msg(writer, "ERR", {"code": "UNKNOWN_CMD", "detail": f"Unknown command: {mtype}"})
                await writer.drain()

    except ConnectionResetError:
        log.warning("Connection reset by '%s' (%s)", username or "unauthenticated", peer_str)
    except asyncio.TimeoutError:
        log.warning("Timeout on connection from %s", peer_str)
    except Exception as e:
        log.error("Unexpected error for '%s': %s", username or peer_str, e, exc_info=True)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ------------
# Entry point
# ------------

async def main():
    server = await asyncio.start_server(handle_client, TCP_HOST, TCP_PORT)
    log.info("RFP server started")
    connect_ip = get_connect_ip()
    log.info("Connect from same PC: 127.0.0.1")
    log.info("Connect from another device: %s", connect_ip)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server shut down.")
