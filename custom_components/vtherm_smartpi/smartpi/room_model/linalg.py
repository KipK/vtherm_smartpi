"""Small dense matrices in pure Python (lists of rows).

The observer works on three states: plain lists keep numpy out of the event
loop. Only the matrix exponential of the discretisation is not trivial; it is
computed once per parameter set.
"""
from __future__ import annotations

Matrix = list[list[float]]


def zeros(n: int, m: int | None = None) -> Matrix:
    return [[0.0] * (n if m is None else m) for _ in range(n)]


def eye(n: int) -> Matrix:
    out = zeros(n)
    for i in range(n):
        out[i][i] = 1.0
    return out


def matmul(x: Matrix, y: Matrix) -> Matrix:
    yt = list(zip(*y))
    return [[sum(a * b for a, b in zip(row, col)) for col in yt] for row in x]


def matvec(x: Matrix, v: list[float]) -> list[float]:
    return [sum(a * b for a, b in zip(row, v)) for row in x]


def transpose(x: Matrix) -> Matrix:
    return [list(r) for r in zip(*x)]


def add(x: Matrix, y: Matrix) -> Matrix:
    return [[a + b for a, b in zip(rx, ry)] for rx, ry in zip(x, y)]


def scale(x: Matrix, s: float) -> Matrix:
    return [[a * s for a in r] for r in x]


def symmetrize(x: Matrix) -> Matrix:
    n = len(x)
    return [[0.5 * (x[i][j] + x[j][i]) for j in range(n)] for i in range(n)]


def norm_inf(x: Matrix) -> float:
    return max(sum(abs(a) for a in r) for r in x) if x else 0.0


def expm(x: Matrix) -> Matrix:
    """Matrix exponential by scaling and squaring of a Taylor series."""
    n = len(x)
    nrm = norm_inf(x)
    s = 0
    while nrm > 0.25:
        nrm /= 2.0
        s += 1
    xs = scale(x, 1.0 / (2 ** s))
    out = eye(n)
    term = eye(n)
    for k in range(1, 16):
        term = scale(matmul(term, xs), 1.0 / k)
        out = add(out, term)
        if norm_inf(term) < 1e-18:
            break
    for _ in range(s):
        out = matmul(out, out)
    return out
