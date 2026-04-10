# Cookbook: First Cell

The smallest end-to-end test — confirm the dispatcher is reachable and a cell actually runs on the remote machine.

---

## Goal

Run a cell that prints the hostname of the remote machine, so you can verify with certainty that execution happened there and not locally.

---

## Setup cell

```python
import os
os.environ["CAAS_HOST"]          = "http://192.168.1.50:8000"
os.environ["DISPATCHER_API_KEY"] = "your-secret-key"
os.environ["CAAS_DEFAULT_IMAGE"] = "python:3.12-slim"

from caas import register_magic
register_magic()
```

Run this cell first. It only needs to run once per kernel session.

---

## Health check (optional but recommended)

Before sending a cell, confirm the dispatcher is up:

```python
from caas import CaasClient
import os

with CaasClient(
    host=os.environ["CAAS_HOST"],
    api_key=os.environ.get("DISPATCHER_API_KEY"),
) as client:
    print(client.health())
```

Expected output:

```
{'status': 'ok'}
```

If this raises `CaasError` or a connection error, check that:

1. The dispatcher container is running (`docker compose ps` on the remote machine)
2. The host and port in `CAAS_HOST` are reachable from your machine
3. The API key matches `DISPATCHER_API_KEY` in `dispatcher/.env`

---

## The cell

```python
%%dispatch
import platform, socket

print("hostname :", platform.node())
print("fqdn     :", socket.getfqdn())
print("python   :", platform.python_version())
```

Expected output (example):

```
hostname : compute-node-1
fqdn     : compute-node-1.local
python   : 3.12.3
```

If the hostname matches the remote machine and not your local machine — everything is working.

---

## Common first-run problems

### `CAAS_HOST is not configured`

You forgot to call `register_magic()`, or you ran the setup cell after the `%%dispatch` cell.
Restart the kernel, run the setup cell first.

### `Invalid API Key` (HTTP 401)

The `DISPATCHER_API_KEY` in your notebook doesn't match the one in `dispatcher/.env` on the
remote machine. Double-check both. If you recently changed the `.env` file, restart the
dispatcher container:

```bash
docker compose restart
```

### `Connection refused` / `ConnectError`

The dispatcher isn't running. On the remote machine:

```bash
cd compute-service/dispatcher
docker compose up -d
```

### Kernel already has a stale API key cached

If you previously set `DISPATCHER_API_KEY` in a notebook cell and it is wrong, `os.environ`
caches it for the entire kernel session. Clear it:

```python
import os
os.environ.pop("DISPATCHER_API_KEY", None)
# then re-set it with the correct value
os.environ["DISPATCHER_API_KEY"] = "correct-key"
```

---

## Patterns to build on

Once the first cell works, a useful pattern is a **setup cell** followed by **work cells**:

```python
# Cell 1 — setup (run once)
import os
os.environ["CAAS_HOST"]          = "http://192.168.1.50:8000"
os.environ["DISPATCHER_API_KEY"] = "your-key"
os.environ["CAAS_DEFAULT_IMAGE"] = "python:3.12-slim"
from caas import register_magic
register_magic()
print("ready")
```

```python
%%dispatch
# Cell 2 — any work cell
print("hello from the remote machine")
```

Each `%%dispatch` cell is independent — no shared state. If cells need to share data,
use a volume mount or write to a shared path. See the [Custom Images](custom-image.md) cookbook
for how to bake data into an image.
