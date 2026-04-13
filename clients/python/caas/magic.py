"""
caas.magic
──────────
IPython cell magic %%dispatch — sends a cell's source to a remote
compute-service node and prints the output inline.

Usage
-----
Once per notebook session:
    from caas import register_magic
    register_magic()           # reads CAAS_HOST, DISPATCHER_API_KEY, CAAS_DEFAULT_IMAGE

Then per cell:
    %%dispatch
    import torch
    print(torch.cuda.get_device_name(0))

With overrides:
    %%dispatch --image pytorch/pytorch:latest --gpu all
    import torch; print(torch.cuda.is_available())
"""
from __future__ import annotations

import os
import argparse
import shlex
import typing as t
from caas.client import CaasClient, CaasError, CaasTimeoutError, DEFAULT_TIMEOUT


class CaasMagicError(Exception):
    """Raised when the magic is misconfigured."""


# Module-level config dict — mutated by register_magic() and tests.
_config: dict = {
    "host": None,
    "api_key": None,
    "default_image": None,
    "default_gpu": None,
}

# Args consumed by the magic itself; the rest are forwarded verbatim to execute_cell().
# Adding a new --flag: add to _parse_line() with dest= matching the execute_cell()
# kwarg name. Only add to this set if the arg is NOT a container option.
_MAGIC_META_ARGS: frozenset[str] = frozenset({"image", "gpu", "timeout", "volumes", "verbose"})


def _get_ipython():
    """Thin wrapper so tests can patch this without importing IPython."""
    try:
        from IPython import get_ipython as _gip
        return _gip()
    except ImportError:
        return None


def _make_client(timeout: float = DEFAULT_TIMEOUT) -> CaasClient:
    return CaasClient(host=_config["host"], api_key=_config["api_key"], timeout=timeout)


def _parse_line(line: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="%%dispatch", add_help=False)
    parser.add_argument("--image", default=None)
    parser.add_argument("--gpu",   default=None,
                        help="'all' or comma-separated device IDs e.g. 0,1")
    parser.add_argument("--timeout", type=float, default=None,
                        help=f"Read timeout in seconds (default: {DEFAULT_TIMEOUT}s). "
                             "Increase for long-running jobs.")
    parser.add_argument("--shm-size", dest="shm_size", default=None,
                        help="Shared memory size, e.g. '1g'. Recommended for PyTorch DataLoader.")
    parser.add_argument("--ipc", dest="ipc_mode", default=None,
                        help="IPC mode, e.g. 'host'. Gives unlimited shared memory to PyTorch workers.")
    parser.add_argument("--volume", dest="volumes", action="append", default=None,
                        metavar="HOST:CONTAINER[:MODE]",
                        help="Bind-mount a host path, e.g. /home/erik/nas_data:/outputs. "
                             "May be repeated for multiple mounts.")
    parser.add_argument("--verbose", action="store_true", default=False,
                        help="Include stderr (container banner, pip warnings) in output. "
                             "Always enabled when the job exits non-zero.")
    # unknown args are silently ignored so custom flags don't break the magic
    ns, _ = parser.parse_known_args(shlex.split(line))
    return ns


def _build_gpu(gpu_arg: t.Optional[str]) -> t.Optional[dict]:
    if gpu_arg is None:
        return None
    if gpu_arg == "all":
        return {"device_ids": "all", "capabilities": ["gpu"]}
    ids = [d.strip() for d in gpu_arg.split(",") if d.strip()]
    if not ids:
        raise CaasMagicError(
            f"Invalid --gpu value {gpu_arg!r}. Use 'all' or a comma-separated "
            "list of device IDs, e.g. --gpu 0,1"
        )
    return {"device_ids": ids, "capabilities": ["gpu"]}


def _build_volumes(volume_args: t.Optional[t.List[str]]) -> t.Optional[t.List[dict]]:
    """Parse --volume HOST:CONTAINER[:MODE] strings into VolumeSpec dicts."""
    if not volume_args:
        return None
    result = []
    for v in volume_args:
        parts = v.split(":")
        if len(parts) < 2:
            raise CaasMagicError(
                f"Invalid --volume value {v!r}. Expected HOST:CONTAINER or HOST:CONTAINER:MODE."
            )
        result.append({
            "host_path": parts[0],
            "container_path": parts[1],
            "mode": parts[2] if len(parts) > 2 else "rw",
        })
    return result


def _dispatch_magic(line: str, cell: str) -> None:
    """Core logic — separated from IPython registration so tests can call it."""
    if not _config.get("host"):
        raise CaasMagicError(
            "CAAS_HOST is not configured. Call register_magic() or set the "
            "CAAS_HOST environment variable before using %%dispatch."
        )

    args = _parse_line(line)
    image = args.image or _config.get("default_image")

    if not image:
        raise CaasMagicError(
            "No image specified. Pass --image <image> or set CAAS_DEFAULT_IMAGE."
        )

    gpu = _build_gpu(args.gpu) if args.gpu else _config.get("default_gpu")
    volumes = _build_volumes(args.volumes)
    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT

    # Collect container options that map 1:1 to execute_cell() kwargs.
    # Adding a new --flag: add to _parse_line() with a dest= matching the
    # execute_cell() kwarg name — nothing else changes.
    opts = {k: v for k, v in vars(args).items()
            if k not in _MAGIC_META_ARGS and v is not None}

    client = _make_client(timeout=timeout)
    try:
        logs = client.execute_cell(code=cell, image=image, gpu=gpu, volumes=volumes,
                                   verbose=args.verbose, **opts)
    except CaasTimeoutError as exc:
        raise CaasMagicError(str(exc)) from exc
    except CaasError as exc:
        raise CaasMagicError(str(exc)) from exc
    print(logs, end="")


def register_magic(ip=None) -> None:
    """
    Register %%dispatch with the current IPython kernel and load config
    from environment variables.

    Call once at the top of a notebook:
        from caas import register_magic
        register_magic()
    """
    _config["host"]          = os.environ.get("CAAS_HOST")
    _config["api_key"]       = os.environ.get("DISPATCHER_API_KEY")
    _config["default_image"] = os.environ.get("CAAS_DEFAULT_IMAGE")
    _config["default_gpu"]   = None  # override via --gpu flag per cell

    shell = ip or _get_ipython()
    if shell is not None:
        shell.register_magic_function(_dispatch_magic, magic_kind="cell", magic_name="dispatch")


def load_ipython_extension(ipython) -> None:
    """Called by %load_ext caas.magic — delegates to register_magic()."""
    register_magic(ip=ipython)
