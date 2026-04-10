# Installation

The Python client lives in `clients/python/` and can be installed directly from GitHub with `pip`.

---

## Standard install

```bash
pip install "git+https://github.com/claremont-computer-network/compute-service.git#subdirectory=clients/python"
```

This installs `CaasClient` and `CaasError`. No IPython dependency is pulled in.

## Notebook install

To also get `register_magic()` and the `%%dispatch` magic, install with the `notebook` extra:

```bash
pip install "git+https://github.com/claremont-computer-network/compute-service.git#subdirectory=clients/python[notebook]"
```

IPython is listed as an optional dependency. In JupyterHub and JupyterLab environments it is already present, so the extra is only needed when installing into a plain Python environment.

---

## Editable install (development)

If you have the repo cloned locally:

```bash
uv pip install -e clients/python
# or
pip install -e clients/python
```

Install dev dependencies and run the client tests:

```bash
uv pip install -e "clients/python[dev]"
uv run pytest clients/python/tests/ -v
```

---

## Verify

```python
from caas import CaasClient, CaasError
print("ok")
```

For notebook usage:

```python
from caas import register_magic
print("ok")
```
