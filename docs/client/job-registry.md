# Job Registry

The dispatcher maintains an in-memory registry of all jobs submitted with `detach=True`. The registry lets you inspect running jobs, check their live resource usage, and stop them — without needing the original container ID.

---

## How the registry works

- Every detached job is assigned a `job_id` (the container's short ID) when it is submitted.
- On startup the dispatcher re-discovers any containers that are still running (filtered by the `caas.managed=true` label).
- Restarting the dispatcher clears the in-memory store, but running containers are re-discovered automatically.
- Synchronous jobs (`detach=False`) are **not** tracked — they run, return their logs, and are removed.

---

## Listing jobs

=== "CaasClient"

    ```python
    from caas import CaasClient

    with CaasClient(host="http://192.168.1.50:8000", api_key="secret") as client:
        jobs = client.jobs()

    for j in jobs:
        r = j.get("resources") or {}
        print(
            j["job_id"], j["status"],
            f"cpu={r.get('cpu_percent', '-')}%",
            f"mem={r.get('mem_usage_mib', '-')} MiB",
        )
    ```

=== "curl"

    ```bash
    curl http://192.168.1.50:8000/v1/jobs \
      -H "X-API-Key: your-key"
    ```

**Response shape:**

```json
{
  "jobs": [
    {
      "job_id": "a3f8d0e12b9c",
      "container_id": "a3f8d0e12b9cdeadbeef",
      "image": "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime",
      "cmd": ["python", "train.py"],
      "status": "running",
      "submitted_at": "2026-04-12T10:00:00",
      "exit_code": null,
      "resources": {
        "cpu_percent": 312.4,
        "mem_usage_mib": 8192.0,
        "mem_limit_mib": 32768.0,
        "mem_percent": 25.0
      }
    }
  ]
}
```

---

## Inspecting a single job

=== "CaasClient"

    ```python
    j = client.job("a3f8d0e12b9c")
    print(j["status"])      # "running" or "stopped"
    print(j["exit_code"])   # None while running, integer once stopped
    ```

=== "curl"

    ```bash
    curl http://192.168.1.50:8000/v1/jobs/a3f8d0e12b9c \
      -H "X-API-Key: your-key"
    ```

---

## Polling a job until it finishes

```python
import time
from caas import CaasClient

with CaasClient(host="http://192.168.1.50:8000", api_key="secret") as client:
    job = client.execute(
        image="pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime",
        cmd=["python", "train.py"],
        gpu={"device_ids": "all"},
        detach=True,
    )
    job_id = job["job_id"]
    print(f"Submitted {job_id}")

    while True:
        j = client.job(job_id)
        r = j.get("resources") or {}
        if r:
            print(f"cpu={r['cpu_percent']:.1f}%  mem={r['mem_usage_mib']:.0f} MiB")
        if j["status"] == "stopped":
            print(f"Finished — exit code {j['exit_code']}")
            break
        time.sleep(10)
```

---

## Stopping a job

=== "CaasClient"

    ```python
    client.stop("a3f8d0e12b9c")
    ```

=== "curl"

    ```bash
    curl -X DELETE http://192.168.1.50:8000/v1/jobs/a3f8d0e12b9c \
      -H "X-API-Key: your-key"
    ```

**Response:**

```json
{"job_id": "a3f8d0e12b9c", "status": "stopped"}
```

!!! note "Already stopped"
    If the job has already exited naturally, `DELETE /v1/jobs/{job_id}` returns HTTP 409.
    You can check `j["status"]` before calling `stop()` to avoid this.

---

## Resource fields

The `resources` object is included for jobs with `status: running` only. For stopped jobs it is `null`.

| Field | Description |
|-------|-------------|
| `cpu_percent` | CPU usage as a percentage of one core. Can exceed 100 on multi-core workloads. |
| `mem_usage_mib` | Current RSS in MiB. |
| `mem_limit_mib` | Container memory limit in MiB (set by Docker). |
| `mem_percent` | `mem_usage_mib / mem_limit_mib × 100`. |
