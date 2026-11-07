"""Room model: parameters and exact discretisation (pure Python).

State x = (Ta, w, d), per second, with w = k (Tw - Text) the wall state scaled
to the air (C/s) and d the unmeasured disturbance (C/s):

    dTa/dt = a u_e - alpha (Ta - Text) + w + d
    dw/dt  = gamma (Ta - Text) - beta w - k dText/dt
    dd/dt  = 0 (a bounded random walk in the observer)

u_e is the applied heat-equivalent power (0..1) through the emitter (delay L,
then first order tau_e). Only combinations of the physical parameters are
identifiable: alpha (total air loss rate), beta (wall rate), c = gamma / beta
(the steady share of the air loss that goes through the walls) and
b_eq = alpha - c (the static loss). The coupling k of the outdoor-derivative
term is not identifiable; it lies in [c, alpha] and the midpoint is used.

The reduced model (room without thermal mass, or wall not identified) is
dTa/dt = a u_e - b_eq (Ta - Text) + d, with w held at zero.

At steady state w = c (Ta - Text); the wall extra power
delta_wall = c (Ta - Text) - w is the heat the walls absorb beyond their
steady share (positive while they warm up), and delta_wall / a is that power
as a fraction of full power.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from . import linalg as la
from .const import (
    H,
    RM_A_RANGE,
    RM_ALPHA_MAX,
    RM_BEQ_RANGE,
    RM_BETA_RANGE,
    RM_D_MAX_REL,
    RM_P0_AIR_C,
    RM_P0_DIST_REL,
    RM_P0_WALL_C,
    RM_Q_AIR_C,
    RM_Q_DIST_REL,
    RM_Q_WALL_C,
    STRUCT_FULL,
    STRUCT_REDUCED,
)

# Wall rate used by the reduced model (the wall state stays at zero)
_REDUCED_BETA = 1.0 / H


@dataclass(frozen=True)
class RoomParams:
    """Published room model parameters (per second; tau_e and L in seconds)."""

    struct: str
    a: float
    alpha: float
    beta: float
    c: float
    tau_e: float
    L: float

    @property
    def full(self) -> bool:
        return self.struct == STRUCT_FULL

    @property
    def b_eq(self) -> float:
        return self.alpha - self.c if self.full else self.alpha

    @property
    def gamma(self) -> float:
        return self.c * self.beta if self.full else 0.0

    @property
    def k_te(self) -> float:
        """Coupling of the outdoor-derivative term: midpoint of its admissible interval [c, alpha]."""
        return 0.5 * (self.c + self.alpha) if self.full else 0.0

    @property
    def d_max(self) -> float:
        return RM_D_MAX_REL * self.a

    def to_dict(self) -> dict[str, Any]:
        return {"struct": self.struct, "a": self.a, "alpha": self.alpha, "beta": self.beta, "c": self.c,
                "tau_e": self.tau_e, "L": self.L}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RoomParams | None":
        if not isinstance(data, dict):
            return None
        try:
            p = cls(str(data["struct"]), float(data["a"]), float(data["alpha"]), float(data["beta"]),
                    float(data["c"]), float(data["tau_e"]), float(data["L"]))
        except (KeyError, TypeError, ValueError):
            return None
        return p if p.plausible() else None

    @classmethod
    def reduced(cls, a: float, b_eq: float, tau_e: float, L: float) -> "RoomParams":
        return cls(STRUCT_REDUCED, a, b_eq, _REDUCED_BETA, 0.0, tau_e, L)

    def plausible(self) -> bool:
        """Every parameter finite and inside its plausible range."""
        vals = (self.a, self.alpha, self.beta, self.c, self.tau_e, self.L)
        if not all(math.isfinite(v) for v in vals) or self.struct not in (STRUCT_FULL, STRUCT_REDUCED):
            return False
        if not RM_A_RANGE[0] <= self.a <= RM_A_RANGE[1]:
            return False
        if not RM_BEQ_RANGE[0] <= self.b_eq <= RM_BEQ_RANGE[1] or self.alpha > RM_ALPHA_MAX:
            return False
        if self.tau_e < 0.0 or self.L < 0.0:
            return False
        if self.full:
            return self.c > 0.0 and RM_BETA_RANGE[0] <= self.beta <= RM_BETA_RANGE[1]
        return True


def continuous(p: RoomParams) -> tuple[la.Matrix, la.Matrix]:
    """State matrix A (3 x 3) and input matrix B (3 x 3, inputs u_e, Text, dText/dt)."""
    if p.full:
        A = [[-p.alpha, 1.0, 1.0], [p.gamma, -p.beta, 0.0], [0.0, 0.0, 0.0]]
        B = [[p.a, p.alpha, 0.0], [0.0, -p.gamma, -p.k_te], [0.0, 0.0, 0.0]]
    else:
        A = [[-p.alpha, 0.0, 1.0], [0.0, -p.beta, 0.0], [0.0, 0.0, 0.0]]
        B = [[p.a, p.alpha, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    return A, B


def process_noise(p: RoomParams) -> la.Matrix:
    """Continuous process noise per second (diagonal)."""
    qa = RM_Q_AIR_C ** 2 / H
    qw = (RM_Q_WALL_C * p.k_te) ** 2 / H if p.full else 0.0
    qd = (RM_Q_DIST_REL * p.b_eq) ** 2 / H
    return [[qa, 0.0, 0.0], [0.0, qw, 0.0], [0.0, 0.0, qd]]


def initial_covariance(p: RoomParams) -> la.Matrix:
    pa = RM_P0_AIR_C ** 2
    pw = (RM_P0_WALL_C * p.k_te) ** 2 if p.full else 0.0
    pd = (RM_P0_DIST_REL * p.b_eq) ** 2
    return [[pa, 0.0, 0.0], [0.0, pw, 0.0], [0.0, 0.0, pd]]


@dataclass(frozen=True)
class Discrete:
    """Exact zero-order-hold discretisation over one step h."""

    phi: la.Matrix
    gam: la.Matrix
    qd: la.Matrix


def discretize(p: RoomParams, h: float) -> Discrete:
    """Phi, Gamma (inputs held over the step) and the process covariance (Van Loan)."""
    A, B = continuous(p)
    M = la.zeros(6)
    for i in range(3):
        for j in range(3):
            M[i][j] = A[i][j] * h
            M[i][3 + j] = B[i][j] * h
    E = la.expm(M)
    phi = [row[:3] for row in E[:3]]
    gam = [row[3:] for row in E[:3]]
    Qc = process_noise(p)
    V = la.zeros(6)
    for i in range(3):
        for j in range(3):
            V[i][j] = -A[i][j] * h
            V[i][3 + j] = Qc[i][j] * h
            V[3 + i][3 + j] = A[j][i] * h
    F = la.expm(V)
    f12 = [row[3:] for row in F[:3]]
    f22t = la.transpose([row[3:] for row in F[3:]])
    qd = la.symmetrize(la.matmul(f22t, f12))
    return Discrete(phi, gam, qd)


def equilibrium_wall(p: RoomParams, ta: float, text: float) -> float:
    """Wall state at equilibrium with the air."""
    return p.c * (ta - text) if p.full else 0.0


def wall_extra_power(p: RoomParams, ta: float, w: float, text: float) -> float | None:
    """Heat the walls absorb beyond their steady share, as a fraction of full power."""
    if not p.full or p.a <= 0.0:
        return None
    return (p.c * (ta - text) - w) / p.a
