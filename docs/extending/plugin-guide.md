# Extending the Dispatcher with Plugins

The dispatcher's behaviour is driven by a small set of **built-in plugins**, but
the same mechanism is fully open to community authors.  You can add new
behaviour — custom volume policies, audit logging, cost tracking, Slack
notifications — without touching the core codebase.

---

## How the plugin system works

Every plugin is a subclass of `CaasPlugin` registered in a global
`PluginRegistry` singleton.  When a request arrives the registry calls each
plugin's hooks **in priority order** (ascending — lower runs first).

```
client request
      │
      ▼
  pre_create(req, create_kwargs)   ← mutate Docker kwargs, raise 400s
      │
  containers.create() / .run()
      │
  on_register(record)              ← background threads, fire-and-forget
      │
      ▼
  container runs…
      │
  post_run(record, result)         ← mutate HTTP response, persist data
      │
      ▼
  HTTP response returned to client
```

### The three hooks

| Hook | When it fires | Exception handling |
|---|---|---|
| `pre_create(req, create_kwargs)` | After request validation, **before** `docker create/run` | Propagates — raise `HTTPException` to reject the request |
| `on_register(record)` | Immediately after the job is stored | Fault-isolated — exceptions are logged and skipped |
| `post_run(record, result)` | After the container exits and logs are captured | Fault-isolated — exceptions are logged and skipped |

Because `pre_create` propagates exceptions, it is the right place for
**validation and policy enforcement** (e.g. blocking disallowed images,
enforcing resource caps).  A bug in `on_register` or `post_run` will never
crash the request — it is logged as `ERROR` by the `caas.dispatcher` logger.

### Built-in plugins and their priorities

| Priority | Name | Hook(s) used | What it does |
|---|---|---|---|
| 10 | `nvidia-entrypoint` | `pre_create` | Clears the NGC banner entrypoint when `suppress_entrypoint=True` |
| 20 | `shm-ipc-policy` | `pre_create` | Parses `shm_size`, enforces `MAX_SHM_SIZE_MB`, sets IPC mode |
| 30 | `volume-policy` | `pre_create` | Validates host paths against `ALLOWED_HOST_DIRS`, deduplicates |
| 50 | `resource-sampler` | `on_register`, `post_run` | Samples CPU/memory for cell jobs and injects `resource_history` |
| 60 | `log-retention` | `post_run` | Persists logs into the job record before the container is removed |

**Reserve priorities 100 and above for your own plugins.**  Built-ins will
never use a priority above 99, so your code will always run after all built-in
hooks within the same hook phase.

---

## Writing your first plugin

### Minimal skeleton

```python
# my_caas_plugins/audit.py
from __future__ import annotations

import logging
import typing as t

from app.core.plugin import CaasPlugin

if t.TYPE_CHECKING:
    from app.jobs import JobRecord

logger = logging.getLogger("caas.dispatcher")


class AuditLogPlugin(CaasPlugin):
    """Append a one-line audit record to a file after every job completes."""

    name = "audit-log"
    priority = 110

    def __init__(self, log_path: str = "/var/log/caas_audit.log") -> None:
        self.log_path = log_path

    def post_run(self, record: "JobRecord", result: dict) -> None:
        line = (
            f"{record.submitted_at.isoformat()} "
            f"job={record.job_id} image={record.image} "
            f"exit={result.get('exit_code', '?')}\n"
        )
        with open(self.log_path, "a") as fh:
            fh.write(line)
```

### Registering your plugin

Call `registry.register()` **after** `app.main` has been imported — the
simplest place is a small startup script that wraps `uvicorn`:

```python
# run.py
import uvicorn
import app.main  # noqa: F401  — triggers register_default_plugins()

from app.core.plugin import registry
from my_caas_plugins.audit import AuditLogPlugin

registry.register(AuditLogPlugin(log_path="/data/audit.log"))

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
```

Then in your `Dockerfile` replace the default `CMD` with this script:

```dockerfile
CMD ["python", "run.py"]
```

!!! tip "Verify registration"
    After starting the service, `GET /health` returns the list of active
    plugins in priority order.  Your plugin's `name` should appear in the list.

---

## Pre-create example — image allow-list

`pre_create` receives the raw request object and the `create_kwargs` dict that
will be forwarded to the Docker API.  Raise `HTTPException(400, …)` to reject
the request.

```python
from fastapi import HTTPException
from app.core.plugin import CaasPlugin


class ImageAllowListPlugin(CaasPlugin):
    """Reject requests for images not in an explicit allow-list."""

    name = "image-allow-list"
    priority = 105

    def __init__(self, allowed: list[str]) -> None:
        self._allowed = set(allowed)

    def pre_create(self, req, create_kwargs: dict) -> None:
        if req.image not in self._allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Image {req.image!r} is not in the allow-list.",
            )
```

---

## On-register example — start a background watcher

`on_register` fires right after the job is stored.  Use it to kick off
background work that needs the container ID (e.g. streaming logs to an
external system).

```python
import threading
from app.core.plugin import CaasPlugin


class LiveLogShipperPlugin(CaasPlugin):
    """Stream container logs to stdout in a background thread."""

    name = "live-log-shipper"
    priority = 120

    def on_register(self, record) -> None:
        t = threading.Thread(
            target=self._ship,
            args=(record.container_id,),
            name=f"log-ship-{record.job_id[:12]}",
            daemon=True,
        )
        t.start()

    def _ship(self, container_id: str) -> None:
        import app.main as _main  # import at call-time so test patches apply

        container = _main.client.containers.get(container_id)
        for chunk in container.logs(stream=True, follow=True):
            print(chunk.decode(errors="replace"), end="")
```

---

## Post-run example — push metrics to Prometheus Pushgateway

`post_run` receives the finished `JobRecord` and the HTTP response dict.
Mutate `result` in-place to add fields, or perform side-effects like pushing
metrics.

```python
from app.core.plugin import CaasPlugin


class PrometheusPushPlugin(CaasPlugin):
    """Push job duration and exit code to a Prometheus Pushgateway."""

    name = "prometheus-push"
    priority = 150

    def __init__(self, gateway_url: str) -> None:
        self.gateway_url = gateway_url

    def post_run(self, record, result: dict) -> None:
        from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

        reg = CollectorRegistry()
        g = Gauge("caas_job_exit_code", "Exit code of last job", registry=reg)
        g.set(result.get("exit_code", -1))
        push_to_gateway(self.gateway_url, job=record.job_id, registry=reg)
```

---

## Accessing dispatcher config inside a plugin

Plugins should **not** read environment variables directly.  The dispatcher
exposes its config as module-level variables in `app.main`.  Import the module
at **call-time** (inside the hook method, not at class definition) so that test
monkey-patches apply correctly:

```python
def pre_create(self, req, create_kwargs: dict) -> None:
    import app.main as _main  # ← call-time import

    cap = getattr(_main, "MY_CUSTOM_CAP_MB", 512)
    ...
```

!!! warning "Don't import `app.main` at module level in a plugin"
    A top-level `import app.main` creates a circular dependency because
    `app.main` itself imports and registers all built-in plugins on load.
    Always import inside the hook method body.

---

## Testing your plugin

Write a `pytest` fixture that registers your plugin **after** the built-ins and
de-registers it in teardown:

```python
# tests/test_audit_plugin.py
import pytest
from starlette.testclient import TestClient
from app.main import app
from app.core.plugin import registry
from my_caas_plugins.audit import AuditLogPlugin


@pytest.fixture()
def audit_plugin(tmp_path):
    plugin = AuditLogPlugin(log_path=str(tmp_path / "audit.log"))
    registry.register(plugin)
    yield plugin
    # teardown — remove just this plugin so built-ins stay intact
    registry._plugins.remove(plugin)


def test_audit_entry_written(audit_plugin, tmp_path, client):
    resp = client.post("/v1/execute", json={...})
    assert resp.status_code == 200
    lines = (tmp_path / "audit.log").read_text().splitlines()
    assert len(lines) == 1
    assert "exit=0" in lines[0]
```

---

## Plugin checklist

Before shipping a plugin:

- [ ] `name` is unique (lowercase, hyphen-separated)
- [ ] `priority` is 100 or above
- [ ] `pre_create` raises `HTTPException` on invalid input — never returns silently
- [ ] `on_register` and `post_run` do **not** re-raise exceptions (let the
      registry's fault isolation protect the caller)
- [ ] Config is read from `app.main` at call-time, not at import time
- [ ] The plugin is covered by at least one `pytest` test
