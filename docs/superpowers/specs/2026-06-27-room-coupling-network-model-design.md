# Room-Coupling Network Model — Design Spec

**Date:** 2026-06-27
**Branch:** `feat/room-coupling-model`
**Status:** Approved design, literature-benchmarked (§13), pending implementation plan
**Component:** `custom_components/vtherm_smartpi` (SmartPI adaptive PI controller for Versatile Thermostat)

---

## 1. Purpose

Generalize SmartPI's room-coupling model so it can **comprehensively model a room network** — not just doors between two SmartPI-controlled rooms, but the full graph of apertures a real room has:

- **Scenario 1** — a room with a **window to the outside**, where the outside temperature is known (`T_ext`) and the window has an open/closed sensor.
- **Scenario 2** — a room with **many doors and windows**: all have open/closed sensors; some doors lead to other SmartPI-controlled rooms, some to rooms *without* SmartPI control, some directly outside; all windows lead outside and all have open/closed sensors.

The model must learn (tailor itself to) the thermal coupling of each aperture and fold it into the existing control law without changing the per-room control behaviour when everything is closed.

## 2. Background — the existing implementation

Base per-room thermal model (1R1C), learned online by `ABEstimator`:

```
dT_i/dt = a_i·u_i − b_i·(T_i − T_ext)
```

`a` = heating efficacy (°C/min per unit duty), `b` = loss coefficient (min⁻¹), `T_ext` = outdoor temperature.

Current room coupling (`smartpi/room_coupling.py`, `smartpi/coupling_estimator.py`):

- Rooms are connected by **doors with open/closed sensors**. When the door between rooms *i* and *j* is open, heat exchanges at a learned rate `k_ij` (min⁻¹): an extra loss term `−k_ij·(T_i − T_j)`.
- `k_ij` is learned from the **base-model residual** `r = m − p`, where `m = (T_i − T_i_prev)/dt` and `p = a·u − b·(T_i − T_ext)`. For one open edge, `k_ij = −r / (T_i − T_j)`.
- **Identifiability is single-aperture only**: learning is held whenever more than one door is open (one equation, many unknowns).
- `k_ij` is treated as a **structural** property of the doorway: learned only while open and the base model is reliable, otherwise **held** (never decayed). The door-state gate turns the contribution on/off.
- The **effective-parameter fold** (`compute_effective_params`) collapses any number of simultaneously-open edges exactly into a single equivalent 1R1C reservoir so the rest of the control law is unchanged:

```
b_eff = b + Σ_j k_j·open_j
T_eff = (b·T_ext + Σ_j k_j·T_j·open_j) / b_eff
```

- `RoomCouplingCoordinator` is a hass-level singleton holding live topology (bidirectional edges + door sensors), per-room snapshots (one SmartPI recalc-interval stale), a `RoomView` facade per room, and BFS connected-component power aggregation.
- When `any_door_open`, `SmartPIGovernance` returns the `COUPLED` regime, which **freezes base `a/b` learning and holds gains** while the coupling estimator keeps learning `k` on its separate path.

### Limitations this spec removes

1. **Single-aperture-only identification.** With many apertures, the "exactly one open" moment may essentially never occur, so most edges never become reliable.
2. **Edges can only target other SmartPI rooms.** There is no node type for *outside* (window / exterior door → `T_ext`) or for *a room with no SmartPI control* (a plain temperature sensor).
3. **Windows are not modelled.** An open window only trips VTherm's `HVAC_OFF_REASON_WINDOW_DETECTION` → thermostat OFF; there is no "model the loss and keep controlling" path.

## 3. Design decisions (the forks)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Open-window behaviour | **Configurable per aperture** (`model` = keep controlling and fold; `trip_off` = force off) |
| 2 | Unsensed neighbour | **Require a temperature sensor** — every node is observed; no hidden state / latent estimation |
| 3 | Identification with many open apertures | **Multi-edge constrained robust RLS** |
| 4 | Architecture | **A — generalize the existing edge abstraction** (keep coordinator + fold + control law; swap learner; type the nodes) |
| 5 | Shared controlled-room edge | **Consensus** — per-room RLS keeps its room-local `k`, softly regularized toward a reliability-weighted cross-endpoint value; coordinator owns the canonical graph edge |

## 4. The model — a typed RC network where every node is observed

### 4.1 Nodes (three kinds, all with a known temperature)

| Node kind | Temperature source | Power | Notes |
|-----------|-------------------|-------|-------|
| `CONTROLLED` | SmartPI snapshot `t_int` | yes (`power_w`) | existing SmartPI room |
| `SENSED` | coordinator reads a temperature entity from `hass.states` | no | room with no SmartPI control |
| `OUTSIDE` | the **querying room's own** `T_ext` | no | one logical node, shared by all rooms |

Passive nodes are **shared**: one logical node per temperature-sensor entity, and a single `OUTSIDE` node, each referenceable by many rooms (a hallway sensor shared by three rooms is one node, not three).

### 4.2 Edges (apertures)

Each declared aperture carries:

- `target` — a room uid, a sensor entity-id, or `OUTSIDE`
- `aperture_sensor` — binary open/closed sensor (today's "door sensor")
- `aperture_type` — `door` | `window` (defaults/labels)
- `open_policy` — `model` | `trip_off`
- learned conductance `k ≥ 0` (min⁻¹), **held when closed**

### 4.3 Loss decomposition and the fold

For room *i*:

```
total_loss(i) = b·(T_i − T_ext)               envelope (closed apertures baked into b)
              + Σ_j k_j·(T_i − T_j)·open_j      each OPEN modelled aperture
```

This folds **exactly** (existing `compute_effective_params`, untouched) into:

```
b_eff = b + Σ_j k_j·open_j
T_eff = (b·T_ext + Σ_j k_j·T_j·open_j) / b_eff
```

- An **OUTSIDE** edge has `T_j = T_ext`, so an open window adds `k_window` to `b_eff` and leaves `T_eff = T_ext` — instantaneously exact ("more conductance to outside, same reference"). **Note (benchmark revision §13):** open-aperture exchange is buoyancy-driven, so `k_window` is *not* constant — it grows ~`√|ΔT|`. Outside/window edges therefore use a `√|ΔT|` conductance law (see §5.1); the fold consumes the *instantaneous* `k`, so this changes only the learned parameter and the regressor, not the fold.
- A **room/sensor** edge pulls `T_eff` toward `T_j`. Interior-door edges keep the constant-`k` (linear-in-ΔT) form — inter-room ΔT is small, so the linearization is fine.
- `b` remains the **closed-envelope** loss. Base-`b` learning is frozen whenever **any modelled aperture is open** (existing `COUPLED` regime, extended from doors → all modelled apertures), so open-aperture loss is attributed to `k`, never absorbed into `b`.

### 4.4 Whole-network behaviour

Because **every node is observed**, the per-room folds compose into a consistent whole-network model with no missing state. Room A reads B's current temperature; B's temperature already reflects B's coupling to C; multi-hop heat flow propagates through the coordinator's snapshot mesh — a distributed Gauss–Seidel/Jacobi solve of the network ODE with **one-cycle lag per hop** (harmless: `k` is small and slow).

## 5. Identification — constrained robust multi-edge RLS

### 5.1 The linear-in-k regression

Each reliable HEAT cycle (`dt > 0`, base model reliable, not calibrating, `T_ext` available) produces one linear equation in the conductances of the currently-open edges:

```
m   = (T_i − T_i_prev) / dt                  measured slope (°C/min)
p   = a·u − b·(T_i − T_ext)                  base prediction (no coupling)
r   = m − p = Σ_j k_j·x_j + noise
x_j = −(T_i − T_j)·open_j                     (0 for closed edges)
```

**Per-node-kind regressor (benchmark revision §13).** For interior **room/sensor** edges the conductance is constant: `x_j = −(T_i − T_j)·open_j`, learned parameter `k_j`. For **outside/window** edges the physical conductance grows with buoyancy as `√|ΔT|`, so we parameterize by an orifice-like coefficient `κ_j` and use the regressor `x_j = −sign(ΔT)·|T_i − T_ext|^{1.5}·open_j` (still **linear in `κ_j`**, so the same RLS applies). The instantaneous conductance handed to the fold is then `k_j = κ_j·√|T_i − T_ext|`. (Second-order discharge-coefficient drift `K ≈ 0.40 + 0.0045·ΔT` and wind terms are left as future refinements.)

`r = xᵀθ` is **linear in the coefficient vector θ** (`k_j` for room/sensor edges, `κ_j` for outside/window edges) → solve with **recursive least squares** (covariance `P`, forgetting since the coefficients are structural and slow):

```
e = r − xᵀk                       innovation
g = P x / (λ + xᵀ P x)            gain
k = project_≥0( k + g·ψ(e) )      ψ = Huber influence (robustness)
P = (P − g xᵀ P) / λ              covariance update (diagonal capped)
```

- **Non-negativity:** `k_j ≥ 0` (physical) by projection after update.
- **Hold:** no update when no aperture is open.
- **Clamp:** `k_j ∈ [COUPLING_K_MIN, COUPLING_K_MAX]`.

### 5.2 Identifiability safeguards (the crux of "many apertures")

- **Per-edge excitation gate:** only adapt edge *j* when `|T_i − T_j| ≥ COUPLING_DT_MIN_C` while open (generalizes today's gradient gate). Cap the `P` diagonal so unexcited directions don't wind up.
- **Per-edge / directional forgetting (benchmark revision §13):** a *single* global `λ` is a known weak point — it cannot track edges that drift at different rates and is prone to covariance windup under low excitation. Use **per-edge forgetting** (each edge keeps its own effective memory) with `λ` near unity (`≈0.99–0.999`), and apply forgetting only in *excited* directions (variable-direction-forgetting style), or equivalently **reset/inflate an edge's covariance** when it has been closed/stale for a long time rather than letting exponential forgetting blow it up. This makes the excitation gate a *belt-and-braces* guard, not the sole defence.
- **Conditioning gate:** when the open-set's regressors are collinear (apertures *always* open together with near-equal ΔT), the split is unidentifiable — detect from the regressor **condition number / Fisher information** (not just `|ΔT|`), mark those edges `reliable=False`, and **expose "sum identifiable, split unidentifiable"** to the consumer rather than silently splitting (only the aggregate conductance is learnable). A solo-open cycle is a rank-1 update that pins that edge exactly, so **RLS reduces to today's single-aperture estimator** where solo opens happen — no regression, full coverage where they don't.
- **Per-edge reliability:** from the covariance diagonal (estimate variance) plus a minimum excited-sample count, mirroring the AB median/MAD gate.

RLS is chosen over batch least-squares: O(E²) per cycle (E = edges, small), no growing buffers, recency-weighted, and graceful degradation to the exact single-edge solution. A Kalman/UKF with per-parameter process noise is the recognized heavier alternative that handles the multi-rate/forgetting problem natively; RLS-with-per-edge-forgetting is the lighter, defensible choice for this footprint (see §13).

### 5.3 Consensus for shared controlled↔controlled edges

A doorway between two SmartPI rooms A and B is one physical conductance `G_AB`, but room *i*'s coefficient is `k_ij = G_ij / C_i` (conductance over **that room's** heat capacity). Hence `k_AB ≠ k_BA` in general — they differ by the capacity ratio `C_B/C_A`. **Hard equality would be physically wrong.**

Instead, **soft, reliability-weighted shrinkage**:

- The coordinator owns the **canonical graph edge** (topology/identity) and exposes each endpoint's current `k` and reliability to the other.
- Each room's RLS adds a regularization pull toward the cross-endpoint value with strength ∝ neighbour reliability and ∝ 1/own-excitation. It is a **shared prior (shrinkage), not a constraint**: a well-excited endpoint keeps its room-local value; an under-excited endpoint is rescued by the neighbour's evidence.
- Edges to `SENSED`/`OUTSIDE` nodes have a single controlled observer → no reconciliation.

**Robustness note (benchmark §13):** in the distributed-estimation literature *diffusion* strategies are shown to converge faster and more stably than *consensus* averaging (consensus networks can even go mean-square unstable when nodes are individually stable). Our reconciliation is a deliberately weak, reliability-weighted *shrinkage prior* (not iterated averaging), so the instability regime doesn't apply; but if it ever misbehaves, a diffusion-style update (combine then adapt) is the recognized stronger form.

**Future path (out of scope here):** separate physical conductance `G` from room capacity `C` so the coordinator can own a true single physical-edge estimate; this design deliberately keeps room-local coefficients and per-room persistence.

## 6. The network model object (coordinator)

The `RoomCouplingCoordinator` becomes the single source of truth for topology:

- **Typed nodes** (`CONTROLLED` / `SENSED` / `OUTSIDE`) and **typed edges** (by target kind).
- **Shared passive nodes** — one node per sensor entity, one `OUTSIDE` node — referenced by many edges.
- **`T_j` resolution by node kind:** controlled → snapshot `t_int`; sensed → read entity from `hass.states`; outside → the querying room's own `T_ext` (filled room-side). `ResolvedEdge` gains `target_kind` and resolved `neighbor_temp`.
- **Consensus exposure:** for each shared controlled↔controlled edge, expose the other endpoint's `k` + reliability.
- **Network queries:** connected components (BFS over open *controlled* edges for power-sharing; full typed topology for diagnostics), shared-node fan-out, and flagging of under-determined sub-structures.

## 7. Control-side & per-aperture policy

- `compute_effective_params` **unchanged**; the fold sums over all open modelled edges with their resolved `T_j`.
- *Any modelled aperture open* → existing `COUPLED` regime freezes base `a/b` learning and holds gains. (`any_door_open` generalized to "any modelled aperture open".)
- **MODEL apertures** (interior doors always; windows/exterior doors opted in): folded into `(b_eff, T_eff)`, controller keeps running, RLS learns `k`. **Contract:** a MODEL aperture must *not* also be wired to VTherm's native window→OFF detection.
- **TRIP_OFF apertures:** while open, SmartPI itself forces command → 0, enters `COUPLED` (freezes base learning), and arms the existing resume-guard on close — self-contained, mirroring window detection but driven by the aperture sensor. Not folded, not learned (thermostat off → no controlled heat to identify).

## 8. Config surface + persistence / migration

Each connection entry extends from `{neighbor_vtherm_entity, connection_door_sensor}` to:

```
target_kind:             room | sensor | outside
neighbor_vtherm_entity:  <uid>       (when target_kind = room)
neighbor_temp_sensor:    <entity>    (when target_kind = sensor)
aperture_sensor:         <binary_sensor>   (was connection_door_sensor)
aperture_type:           door | window
open_policy:             model | trip_off
```

- **Edge key** becomes a stable string edge-id: room targets keep `neighbor_uid` (back-compat); sensor/outside targets key by `aperture_sensor` entity-id (stable, unique per aperture). `CouplingEstimator` and `prune()` generalize from "neighbour uid" to "edge id".
- **Backward compatibility:** an existing `{neighbor_vtherm, door_sensor}` entry maps to `room / door / model`; existing persisted `coupling_state` keyed by `neighbor_uid` still loads unchanged.
- **config_flow:** group the per-aperture fields; defaults — `aperture_type=door` → `open_policy=model`; `aperture_type=window` → `open_policy` user-selectable.

## 9. Diagnostics

- **Per-edge:** `target_kind`, resolved `T_j`, open, `k`, `reliable`, excitation count, conditioning/identifiability status, neighbour-`k`/consensus value, contribution to `b_eff`.
- **Network-level:** node/edge inventory, connected components, shared nodes, flagged under-determined edges.
- Extends the existing `edges_diag` / `_last_coupling_diag` and the `diagnostics.py` builders.

## 10. Test plan

- **Fold:** identity when all closed (byte-identical to uncoupled); correct multi-edge `b_eff`/`T_eff`; outside edge leaves `T_eff = T_ext`.
- **RLS:** recovers the exact single-edge solution in a solo-open cycle (matches today); separates two edges given time-varying open-patterns; conditioning gate holds collinear always-together edges (`reliable=False`); non-negativity; reliability gating; persistence/migration round-trip; pruning by edge-id.
- **Consensus:** two endpoints converge under shrinkage; a reliable endpoint dominates an unreliable one; capacity-ratio difference is *not* forced to zero.
- **Coordinator/network:** typed `T_j` resolution per node kind; one shared passive node fanned out to many rooms; component-power traverses only controlled edges; multi-hop snapshot propagation across ≥3 rooms.
- **Policy/governance:** any modelled aperture open → `COUPLED`; TRIP_OFF aperture open → forced off + freeze + resume-guard on close.
- **Integration:** Scenario 1 (room + one outside window) and Scenario 2 (many mixed doors/windows) as end-to-end sims.

Existing `tests/test_coupling_*.py`, `test_effective_params.py`, `test_room_coupling_coordinator.py` extend rather than get replaced.

## 11. Out of scope / future work

- Latent (unsensed) node estimation — explicitly excluded (decision #2 requires a sensor).
- A coordinator-owned **physical-edge** estimator separating `G` from `C` (replacing per-room coefficients + consensus).
- Whole-house EKF / joint state-space observer.
- Cooling-mode coupling identification (current learning gated to HEAT).

## 12. Symbol glossary

| Symbol | Meaning | Units |
|--------|---------|-------|
| `T_i`, `T_j` | indoor temperature of room *i* / neighbour node *j* | °C |
| `T_ext` | outdoor temperature (per-room reading) | °C |
| `a`, `b` | base 1R1C heating efficacy / loss coefficient | °C·min⁻¹·duty⁻¹, min⁻¹ |
| `u` | duty-cycle command | 0..1 |
| `k_ij` | room-*i* coupling coefficient of aperture to *j* (`= G_ij/C_i`) | min⁻¹ |
| `G_ij` | physical aperture conductance | W·K⁻¹ |
| `C_i` | room *i* thermal capacity | J·K⁻¹ |
| `b_eff`, `T_eff` | folded effective loss / reference | min⁻¹, °C |
| `open_j` | aperture *j* open indicator | {0,1} |
| `λ` | RLS forgetting factor | — |
| `κ_j` | orifice-like coefficient for an outside/window edge (`k_j = κ_j·√|ΔT|`) | min⁻¹·°C⁻⁰·⁵ |
| `P` | RLS covariance | — |

---

## 13. Benchmark findings & revisions (2026-06-27)

A deep, adversarially-verified literature benchmark (run `wf_5730c22f-8b6`; 24 primary sources, 25 claims verified 3-0/2-1, 0 refuted) assessed every design choice. Verdict per choice and the revisions adopted above:

| Design choice | Verdict | Source anchor |
|---------------|---------|---------------|
| Per-zone RC grey-box + learned inter-zone conductances | **Sound / standard** — mainstream multi-zone approach. (1R1C base is low-order vs ISO 13790's 5R1C / 1–4 states/zone — a deliberate identifiability-over-fidelity trade.) | arXiv:1810.07400; LBL Modelica ISO13790 Zone5R1C; Arroyo & Spiessens 2020 |
| Effective-parameter fold | **Sound** — it is the thermal analogue of **Kron / star-mesh (Schur-complement)** reduction, exact at the instantaneous loss level. Folding *observed* neighbour temps sidesteps the dynamic-node-elimination error; residual error is only one-cycle staleness. | Dörfler & Bullo (arXiv:1102.2950); Time-domain Kron generalization |
| Identifiability gates (excitation, non-negativity, hold-collinear) | **Sound / well-motivated** — matches the documented collinearity & persistent-excitation failure modes; topology is reconstructable from temperature-only data. **Revised:** gate on condition number / Fisher info, expose "sum-identifiable, split-unidentifiable." | Agbi/Song/Krogh CDC 2012; Vahidi 2004; E&B 2022 (S037877882200617X) |
| Single global forgetting factor | **Weak** → **revised** to per-edge / directional forgetting + covariance reset; excitation gate demoted to belt-and-braces. Kalman/UKF noted as heavier native alternative. | VDF (Wan/JAS 2021); Vahidi 2004; PMC4962952 / PMC11798724 |
| Constant-`k` open window (loss linear in ΔT) | **Weakest assumption** → **revised** to `k_window = κ·√|ΔT|` (loss ∝ `ΔT·√|ΔT|`); buoyancy counterflow ∝ `√(g'H)` and discharge coeff `K ≈ 0.40+0.0045·ΔT`. | AIVC airbase_4535 (Brown–Solvason); ASHRAE/EN 16798 single-sided ventilation |
| Distributed snapshot-mesh + consensus reconciliation | **Partially supported** — decentralized *identifiability* and neighbour-local distributed RLS (diffusion RLS) are recognized and can match/beat centralized; but snapshot-mesh *dynamics* vs joint EKF were **not** head-to-head benchmarked. Diffusion ≥ consensus noted. | Diffusion RLS (IEEE TSP 2008); Tu & Sayed (arXiv:1205.3993); distributed MPC (arXiv:1902.10259) |
| Prior art in real thermostats | **No prior art found** for learned inter-room coupling / open-window-loss modelling — Versatile Thermostat does window→OFF only. The room-network model is genuinely novel; novelty cuts both ways (no reference implementation to lean on). | github.com/jmcollin78/versatile_thermostat; better_thermostat |

**Residual risks to carry into implementation / validation:**
1. **Snapshot-mesh vs joint-EKF dynamics** are unbenchmarked here. Guardrail: keep the SmartPI recalc interval short relative to room thermal time constants (bounds the one-cycle-per-hop staleness error), and validate multi-hop convergence in the Scenario-2 integration sim. Literature also advises **decoupling weakly-interacting zones** — our open/closed gate does this naturally (closed = decoupled).
2. **`√|ΔT|` window law** should be validated against the constant-`k` baseline on real open-window data before being made the default for outside edges; keep it behind the same reliability gating so a poor fit degrades to "unreliable, no fold" rather than mis-controlling.
3. **Consensus** is intentionally a weak shrinkage prior; if it ever destabilizes, switch to a diffusion-style combine-then-adapt update.
