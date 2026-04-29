# CS306 Remote File Protocol (RFP)

This project is a simple client-server file transfer system built with Python `asyncio`.

- `server.py` hosts files and handles commands over TCP.
- `client.py` connects to the server and gives an interactive command-line interface.
- This version is TCP-only (no UDP discovery or notifications).

## How the Code Works

### 1) Transport and ports

- **TCP (port `2077`)**: control channel and file data transfer.

### 2) Message format

Control messages are JSON objects terminated by `\r\n` (one message per line), for example:

- `AUTH`, `LIST`, `UPLOAD`, `DOWNLOAD`, `DELETE`, `QUIT`
- responses like `AUTH_OK`, `LIST_RESP`, `READY`, `FILE_DATA`, `OK`, `ERR`

Raw file bytes are streamed after the relevant control message (`READY` for upload, `FILE_DATA` for download).

### 3) Server flow (`server.py`)

1. Starts TCP server and waits for clients.
2. For each TCP client:
   - requires `AUTH` first,
   - processes commands in a loop,
   - reads/writes files in `./server_files`.

Important validation in the server:

- blocks invalid file names (path separators),
- rejects duplicate upload names,
- enforces a max upload size (100 MB),
- deletes partial files if upload is incomplete.

### 4) Client flow (`client.py`)

1. Connects to server with retry/backoff.
2. Sends `AUTH`.
3. Enters interactive shell:
   - `list`
   - `upload <local_path>`
   - `download <filename>`
   - `delete <filename>`
   - `quit`
4. Saves downloaded files to `./downloads`.

## Error Handling

### Client-side handling (`client.py`)

- **Connection failures at startup**
  - Retries up to 3 times with exponential backoff (`2s`, `4s`, ...).
  - Handles refused connections, timeouts, and generic socket errors.
  - Exits cleanly if all retries fail.

- **Control-message safety**
  - `receive_msg()` uses a read timeout (`IO_TIMEOUT`).
  - If timeout happens or no data arrives, it returns `None` (treated as failure by command handlers).
  - If JSON is malformed, client logs an error and treats response as invalid.

- **Command-level checks**
  - `upload`: verifies local file exists before contacting server.
  - `download`: validates first response type must be `FILE_DATA`; otherwise prints server error/unexpected response.
  - `delete`/`list`: prints server-provided error payloads when response is not success.

- **Data-transfer failure cases**
  - Upload fails if server does not send `READY`, sends `ERR`, or becomes silent.
  - Download fails on timeout during byte stream or if connection closes before expected file size.

- **Session-level exceptions**
  - Handles `ConnectionResetError`, `asyncio.TimeoutError`, and generic exceptions in the main loop.
  - Always attempts to close writer socket in `finally`.

### Server-side handling (`server.py`)

- **Protocol and auth enforcement**
  - First TCP message must be `AUTH`; otherwise returns `ERR` with `AUTH_REQUIRED`.
  - Invalid credentials return `AUTH_ERR` and terminate that session.
  - Malformed JSON is converted to internal `MALFORMED` and returned as `ERR` with `MALFORMED`.

- **Input validation**
  - Rejects unsafe filenames (path separators) with `INVALID_FILENAME`.
  - Rejects invalid file sizes (`<= 0` or `> 100MB`) with `INVALID_SIZE`.
  - Rejects upload when target already exists with `FILE_EXISTS`.
  - Rejects missing/nonexistent files in download/delete with `NOT_FOUND`.
  - Unknown commands return `UNKNOWN_CMD`.

- **Transfer integrity**
  - For uploads, server reads exactly announced byte count.
  - If fewer bytes arrive than expected, server deletes partial file and returns `INCOMPLETE`.

- **Timeout/disconnect robustness**
  - Per-message reads are timeout-protected.
  - Handles `ConnectionResetError`, timeout, and unexpected exceptions with logs.
  - Always removes client from `connected_clients` and closes writer in `finally`.

### Typical error codes you will see

- `AUTH_REQUIRED`: client did not authenticate first.
- `AUTH_ERR`: bad username/password.
- `MALFORMED`: invalid JSON control message.
- `INVALID_FILENAME`: unsafe or invalid filename.
- `INVALID_SIZE`: upload size out of allowed range.
- `FILE_EXISTS`: upload filename already present on server.
- `NOT_FOUND`: requested file does not exist.
- `INCOMPLETE`: file upload disconnected/ended early.
- `UNKNOWN_CMD`: unsupported command type.

## Quick Start

## 1) Requirements

- Python 3.10+ (recommended)

## 2) Start server

From the project folder:

```bash
python server.py
```

Server storage directory is created automatically at `./server_files`.

## 3) Start client

In another terminal:

```bash
python client.py <server_ip> -u guest -p guest123
```

Built-in users in `server.py`:

- `dev / dev123`
- `user / user123`
- `guest / guest123`
