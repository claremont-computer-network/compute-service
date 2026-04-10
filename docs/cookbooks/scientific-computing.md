# Cookbook: Scientific Computing & Optimisation

This cookbook uses two custom ARM64 images built for the GB10:

| Image | Contents | Build script |
|-------|----------|--------------|
| `caas-scientific:latest` | PyTorch, JAX, optax, diffrax, SciPy, CVXPY, Optuna, PyMC | `docker/scientific/build.sh` |
| `caas-sagemath:latest` | SageMath (symbolic maths, number theory) | `docker/sagemath/build.sh` |

Build them on the remote machine once before running any cells:

```bash
HOST=user@example.com REPO_DIR=~/path/to/repo ./docker/scientific/build.sh
HOST=user@example.com REPO_DIR=~/path/to/repo ./docker/sagemath/build.sh
```

---

## Verify the stack

```python
%%dispatch --image caas-scientific:latest --gpu all
import torch, numpy, scipy, jax, cvxpy, optuna

print("torch  :", torch.__version__, "| CUDA:", torch.cuda.is_available())
print("numpy  :", numpy.__version__)
print("scipy  :", scipy.__version__)
print("jax    :", jax.__version__, "| devices:", jax.devices())
print("cvxpy  :", cvxpy.__version__)
print("optuna :", optuna.__version__)

# Quick GPU tensor round-trip
x = torch.randn(1000, 1000, device="cuda")
print("GPU matmul:", (x @ x.T).shape)
```

---

## JAX — JIT-compiled gradient descent

```python
%%dispatch --image caas-scientific:latest --gpu all
import jax
import jax.numpy as jnp
import optax

# Minimise f(x) = (x - 3)^2 with Adam
def loss(x):
    return jnp.sum((x - 3.0) ** 2)

grad_fn   = jax.jit(jax.grad(loss))
optimizer = optax.adam(learning_rate=0.1)
x         = jnp.array([0.0])
opt_state = optimizer.init(x)

for step in range(60):
    grads          = grad_fn(x)
    updates, opt_state = optimizer.update(grads, opt_state)
    x              = optax.apply_updates(x, updates)
    if step % 10 == 0:
        print(f"step {step:3d}  x={float(x[0]):.6f}  loss={float(loss(x)):.8f}")

print(f"\nconverged → x={float(x[0]):.6f}  (target 3.0)")
```

---

## JAX — differentiable ODE solver (diffrax)

```python
%%dispatch --image caas-scientific:latest --gpu all
import jax.numpy as jnp
import diffrax

# Lotka-Volterra predator-prey system
def lotka_volterra(t, y, args):
    alpha, beta, gamma, delta = args
    prey, predator = y
    return jnp.array([
        alpha * prey     - beta  * prey * predator,
        delta * prey * predator - gamma * predator,
    ])

sol = diffrax.diffeqsolve(
    diffrax.ODETerm(lotka_volterra),
    diffrax.Tsit5(),
    t0=0.0, t1=20.0, dt0=0.05,
    y0=jnp.array([10.0, 5.0]),
    args=(1.5, 1.0, 3.0, 1.0),
    saveat=diffrax.SaveAt(ts=jnp.linspace(0, 20, 200)),
)

print("prey    range:", round(float(sol.ys[:, 0].min()), 2), "→", round(float(sol.ys[:, 0].max()), 2))
print("predator range:", round(float(sol.ys[:, 1].min()), 2), "→", round(float(sol.ys[:, 1].max()), 2))
```

---

## SciPy — constrained optimisation

```python
%%dispatch --image caas-scientific:latest
from scipy.optimize import minimize
import numpy as np

# Minimise the Rosenbrock function subject to x0 + x1 >= 1
def rosenbrock(x):
    return sum(100 * (x[i+1] - x[i]**2)**2 + (1 - x[i])**2 for i in range(len(x) - 1))

result = minimize(
    rosenbrock,
    x0=np.array([-1.0, 1.0]),
    method="SLSQP",
    constraints={"type": "ineq", "fun": lambda x: x[0] + x[1] - 1},
    options={"ftol": 1e-9, "maxiter": 1000},
)
print("success :", result.success)
print("x       :", result.x)
print("f(x)    :", result.fun)
print("message :", result.message)
```

---

## CVXPY — convex optimisation (Lasso)

```python
%%dispatch --image caas-scientific:latest
import cvxpy as cp
import numpy as np

np.random.seed(0)
n, m = 20, 10          # variables, constraints

A   = np.random.randn(m, n)
b   = np.random.randn(m)
lam = 0.1

x          = cp.Variable(n)
objective  = cp.Minimize(cp.sum_squares(A @ x - b) + lam * cp.norm1(x))
problem    = cp.Problem(objective)
problem.solve(solver=cp.CLARABEL)

print("status :", problem.status)
print("obj    :", round(problem.value, 6))
print("nnz(x) :", int(np.sum(np.abs(x.value) > 1e-4)), "of", n, "non-zero")
```

---

## Optuna — hyperparameter search

```python
%%dispatch --image caas-scientific:latest --gpu all
import optuna, torch, torch.nn as nn

optuna.logging.set_verbosity(optuna.logging.WARNING)

def objective(trial):
    lr    = trial.suggest_float("lr", 1e-4, 1e-1, log=True)
    units = trial.suggest_int("units", 16, 128, step=16)

    model = nn.Sequential(nn.Linear(10, units), nn.ReLU(), nn.Linear(units, 1)).cuda()
    opt   = torch.optim.Adam(model.parameters(), lr=lr)
    x     = torch.randn(256, 10, device="cuda")
    y     = torch.randn(256, 1,  device="cuda")

    for _ in range(100):
        opt.zero_grad()
        loss = nn.MSELoss()(model(x), y)
        loss.backward()
        opt.step()
    return loss.item()

study = optuna.create_study(direction="minimize")
study.optimize(objective, n_trials=30)

print("best trial  :", study.best_trial.number)
print("best params :", study.best_params)
print("best value  :", round(study.best_value, 6))
```

---

## PyMC — Bayesian inference

```python
%%dispatch --image caas-scientific:latest
import pymc as pm
import numpy as np
import arviz as az

np.random.seed(42)
true_mu, true_sigma = 3.5, 1.2
data = np.random.normal(true_mu, true_sigma, size=100)

with pm.Model():
    mu    = pm.Normal("mu",    mu=0, sigma=10)
    sigma = pm.HalfNormal("sigma", sigma=5)
    _     = pm.Normal("obs",   mu=mu, sigma=sigma, observed=data)
    idata = pm.sample(1000, tune=500, progressbar=False, cores=1)

summary = az.summary(idata, var_names=["mu", "sigma"])
print(summary[["mean", "sd", "hdi_3%", "hdi_97%"]])
print(f"\ntrue mu={true_mu}, true sigma={true_sigma}")
```

---

## SageMath — symbolic maths

!!! note "Uses the `caas-sagemath` image"
    SageMath is not included in `caas-scientific` because the apt package
    conflicts with the NGC Python environment. Build `caas-sagemath` separately.

Use `**` for exponentiation in `--python` / `-c` mode — the `.sage` preprocessor
that rewrites `^` to `**` is not active for inline strings.

```python
%%dispatch --image caas-sagemath:latest
from sage.all import *

# Symbolic differentiation
x = var('x')
f = sin(x**2) * exp(-x)
print("f        =", f)
print("f'       =", diff(f, x).simplify_full())

# Factor a large number
n = 2**128 + 1
print("factors  =", factor(n))

# Solve a system
y = var('y')
sol = solve([x**2 + y**2 == 1, x + y == 1], x, y)
print("solution =", sol)
```
