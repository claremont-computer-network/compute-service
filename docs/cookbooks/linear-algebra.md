# Cookbook: Linear Algebra

Run matrix operations on a remote machine, starting with pure Python and graduating to NumPy and SciPy.

---

## Pure Python matmul

No dependencies — works with the stock `python:3.12-slim` image.

```python
%%dispatch
def matmul(A, B):
    n, m, p = len(A), len(B), len(B[0])
    return [[sum(A[i][k] * B[k][j] for k in range(m)) for j in range(p)] for i in range(n)]

A = [[1, 2], [3, 4]]
B = [[5, 6], [7, 8]]
C = matmul(A, B)
for row in C:
    print(row)
```

Output:

```
[19, 22]
[43, 50]
```

---

## NumPy matmul — pip-install inside the cell

For quick experiments, install the package at the top of the cell. The install happens inside
the container and is thrown away when the cell finishes, so there's no state left behind.

```python
%%dispatch
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy", "-q"])

import numpy as np

A = np.random.randn(512, 512)
B = np.random.randn(512, 512)
C = A @ B
print(f"shape : {C.shape}")
print(f"mean  : {C.mean():.4f}")
print(f"std   : {C.std():.4f}")
```

!!! tip "Slow on repeated runs"
    Every time this cell runs, pip re-downloads NumPy. For packages you use frequently,
    bake them into a custom image instead. See the [Custom Images](custom-image.md) cookbook.

---

## NumPy matmul — custom image (faster)

If you've built and pushed a NumPy image (see [Custom Images](custom-image.md)), substitute
your actual image name. The examples below show the pattern — replace
`ghcr.io/yourorg/caas-numpy:latest` with whatever you pushed:

```python
# Replace this with your actual image before running
NUMPY_IMAGE = "ghcr.io/yourorg/caas-numpy:latest"
```

```python
%%dispatch --image ghcr.io/yourorg/caas-numpy:latest
import numpy as np

rng = np.random.default_rng(42)
A = rng.standard_normal((1024, 1024))
B = rng.standard_normal((1024, 1024))
C = A @ B
print(f"C[0, :4] = {C[0, :4]}")
```

No pip install — the container starts and runs immediately.

!!! warning "Placeholder image name"
    `ghcr.io/yourorg/caas-numpy:latest` is not a real image. Copy the Dockerfile from the
    [Custom Images](custom-image.md) cookbook, build it, push it, then substitute your image
    name in the `--image` flag. If you don't have an image yet, use the pip-install approach
    in the section above — it works out of the box.

---

## SVD decomposition

Works immediately with the pip-install approach:

```python
%%dispatch
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy", "-q"])

import numpy as np

rng = np.random.default_rng(0)
M = rng.standard_normal((200, 50))

U, S, Vh = np.linalg.svd(M, full_matrices=False)
print(f"U  : {U.shape}")
print(f"S  : {S.shape},  S[0] = {S[0]:.4f}")
print(f"Vh : {Vh.shape}")

# Reconstruct and check error
M_hat = U @ np.diag(S) @ Vh
print(f"reconstruction error: {np.linalg.norm(M - M_hat):.2e}")
```

---

## SciPy sparse solve

Install SciPy alongside NumPy if you need sparse solvers:

```python
%%dispatch
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy", "scipy", "-q"])

import numpy as np
from scipy.sparse import diags
from scipy.sparse.linalg import spsolve

n = 500
diagonals = [[-1] * (n - 1), [2] * n, [-1] * (n - 1)]
A = diags(diagonals, [-1, 0, 1], format="csr")
b = np.ones(n)

x = spsolve(A, b)
print(f"x[0]   = {x[0]:.6f}")
print(f"x[n//2] = {x[n//2]:.6f}")
print(f"x[-1]  = {x[-1]:.6f}")
```

---

## Benchmarking tip

To measure wall time on the remote machine (not your network round-trip), use `time` inside the cell:

```python
%%dispatch
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy", "-q"])

import numpy as np, time

sizes = [256, 512, 1024, 2048]
for n in sizes:
    A = np.random.randn(n, n)
    B = np.random.randn(n, n)
    t0 = time.perf_counter()
    _ = A @ B
    elapsed = time.perf_counter() - t0
    print(f"n={n:4d}  {elapsed*1000:.1f} ms")
```
