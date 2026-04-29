# Smart-PI: Documentation Technique et Scientifique

## 1. Introduction

**Smart-PI** est un régulateur proportionnel-intégral discret auto-adaptatif, disponible sous forme d'intégration autonome pour *Versatile Thermostat*. Il vise à remplacer un TPI à coefficients fixes par une boucle qui apprend en ligne le comportement thermique réel de la pièce.

Le code actuel repose sur trois idées directrices :

1. identifier un modèle thermique simple du premier ordre avec temps mort,
2. adapter la commande PI à partir de ce modèle,
3. geler ou limiter certaines adaptations quand le régime physique n'est pas jugé fiable.

Ce document décrit le comportement réellement implémenté dans le code SmartPI actuel.

---

## 2. Modèle thermique et loi de commande

### 2.1 Modèle thermique utilisé

Le modèle thermique appris est :

$$ \frac{dT_{int}}{dt} = a \cdot u(t) - b \cdot (T_{int}(t) - T_{ext}(t)) $$

Avec :

- $T_{int}$ : température intérieure,
- $T_{ext}$ : température extérieure,
- $u(t) \in [0,1]$ : commande de chauffe normalisée,
- $a$ : gain thermique de la chauffe,
- $b$ : coefficient de pertes.

La constante de temps associée est :

$$ \tau = \frac{1}{b} $$

Le modèle est volontairement simple. Le temps mort est appris séparément et injecté dans les heuristiques de réglage et dans les protections.

### 2.2 Chaîne de commande

La commande courante suit la structure :

$$ u = u_{FF} + u_{PI} $$

avec :

- `u_ff1` : feed-forward structurel dérivé de $b/a$,
- `u_ff2` : biais lent `FFTrim`,
- `u_ff3` : correction prédictive optionnelle de court horizon,
- `u_pi` : terme PI discret.

La sortie réellement injectée dans le calcul est `u_ff_eff + u_pi`, puis elle passe par :

- limitation de vitesse,
- anti-windup,
- contraintes de cycle et protections.

---

## 3. Identification en ligne

### 3.1 Ordre de bootstrap

En phase `HYSTERESIS`, l'apprentissage suit un ordre strict :

1. `deadtime_heat` doit être fiable avant toute collecte utile pour `a`,
2. `deadtime_cool` doit être fiable avant toute collecte utile pour `b`,
3. `b` doit progresser avant `a` via un soft gate.

Règles effectivement codées :

- `AB_MIN_SAMPLES_B = 8`,
- `AB_MIN_SAMPLES_A = 6`,
- `AB_MIN_SAMPLES_A_CONVERGED = 6`,
- `AB_A_SOFT_GATE_MIN_B = 8`.

SmartPI quitte `HYSTERESIS` lorsque `b` dispose de 8 mesures et `a` de
6 mesures. Les deux buffers continuent ensuite à se remplir jusqu'à
`AB_HISTORY_SIZE = 31` pendant la régulation SmartPI normale.

La convergence de `b` utilisée pour l'apprentissage `a` avec détection de dérive repose sur `b_converged_for_a()` :

1. `learn_ok_count_b >= 11`,
2. `len(_b_hat_hist) >= 5`,
3. `MAD(b_hat)/Med(b_hat) <= 0.30`,
4. `range(last_5_b_hat)/Med(b_hat) <= 0.10`.

### 3.2 Fenêtre d'apprentissage réelle

Le code n'utilise plus de constante `WINDOW_MIN_MINUTES`. L'apprentissage repose aujourd'hui sur `LearningWindowManager` et sur une fenêtre glissante qui :

- exige une température extérieure disponible,
- respecte la gouvernance thermique sauf en calibration,
- bloque la collecte en bootstrap tant que les temps morts requis ne sont pas fiables,
- applique une pause après reprise (`LEARNING_PAUSE_RESUME_MIN = 20`),
- ancre le début de fenêtre après la fin du temps mort quand c'est nécessaire,
- surveille la stabilité de puissance via le coefficient de variation (`U_CV_MAX`),
- prolonge la fenêtre tant que la pente n'est pas jugée robuste,
- abandonne après `DT_MAX_MIN = 240` minutes si aucun signal robuste n'émerge.

En pratique, la décision de soumettre ou d'étendre la fenêtre dépend de la robustesse de la pente, pas d'une durée minimale fixe.

### 3.3 Estimation robuste de pente

`ABEstimator.robust_dTdt_per_min()` applique des garde-fous sur la pente :

- amplitude minimale : `DT_DERIVATIVE_MIN_ABS = 0.05°C`,
- nombre minimal de changements de niveau : `OLS_MIN_JUMPS = 3`,
- significativité statistique : `OLS_T_MIN = 2.5`.

Les pentes validées alimentent ensuite `ABEstimator.learn()` qui :

- rejette les outliers physiques (`max_abs_dT_per_min = 0.35`),
- apprend `b` si `u < U_OFF_MAX`,
- apprend `a` si `u > U_ON_MIN`,
- publie les valeurs avec une agrégation robuste (median ou weighted median selon la configuration).

### 3.4 Estimation de `a` et `b`

Le calcul suivi par `learn()` est :

- phase OFF :

$$ b = -\frac{dT/dt}{T_{int} - T_{ext}} $$

- phase ON :

$$ a = \frac{dT/dt + b \cdot (T_{int} - T_{ext})}{u} $$

Les historiques sont filtrés par median/MAD et les valeurs publiées sont bornées.

### 3.5 Estimation du temps mort

`DeadTimeEstimator` implémente une FSM indépendante :

- transition OFF -> ON : attente d'une réponse de chauffe,
- transition ON -> OFF : attente d'une réponse de refroidissement.

La fiabilité devient vraie dès qu'au moins une mesure valide a été capturée sur le canal correspondant. Les valeurs publiées sont les moyennes des historiques `heat` et `cool`.

---

## 4. Contrôle Smart-PI

### 4.1 Gains PI

`GainScheduler.calculate()` applique la logique suivante :

1. si `tau` n'est pas fiable, repli sur `KP_SAFE = 0.55` et `KI_SAFE = 0.010`,
2. sinon calcul heuristique :

$$ K_{p,heu} = 0.35 + 0.9 \cdot \sqrt{\frac{\tau}{200}} $$

3. si `deadtime_heat` est fiable et `a > 0`, calcul IMC :

$$ K_{p,IMC} = \frac{1}{2 \cdot a \cdot (L/60)} $$

4. choix de `min(Kp_IMC, Kp_heu)`,
5. calcul de :

$$ K_i = \frac{K_p}{\max(\tau, 10)} $$

6. application des gels de gouvernance.

Important :

- les bornes effectivement appliquées dans `GainScheduler` sont actuellement larges (`Kp` borné dans `[0.05, 10.0]`, `Ki` dans `[0.0001, 1.0]`),
- les facteurs near-band par défaut sont `DEFAULT_KP_NEAR_FACTOR = 1.0` et `DEFAULT_KI_NEAR_FACTOR = 1.0`.

### 4.2 Feed-forward

Le feed-forward structurel est calculé à partir de :

$$ k_{ff} = \frac{b}{a} $$

et :

$$ u_{ff1} = clamp(k_{ff} \cdot (SP - T_{ext}), 0, 1) \cdot warmup\_scale $$

Le `warmup_scale` n'est pas un simple interrupteur. Il dépend :

- du nombre d'apprentissages valides,
- du nombre de cycles depuis reset,
- de la fiabilité de `tau`.

À cela s'ajoutent :

- `u_ff2` : correction lente `FFTrim`,
- `ABConfidence` : politique de confiance sur `a/b`,
- repli en mode `AB_BAD` vers un feed-forward nul après `AB_BAD_PERSIST_CYCLES = 3`.

### 4.3 FF3

`ff3.py` ajoute une correction prédictive optionnelle :

- horizon dérivé de `FF3_PREDICTION_HORIZON_MIN = 30`,
- autorité max `FF3_MAX_AUTHORITY = 0.20`,
- pas d'exploration `FF3_DELTA_U = 0.05`.

FF3 est désactivé si l'une des conditions suivantes n'est pas satisfaite :

- configuration activée,
- mode chauffage,
- température extérieure disponible,
- `tau` fiable,
- jumeau thermique initialisé et fiable,
- pas de warm-up du jumeau,
- pas de calibration,
- pas de power shedding,
- pas de changement récent de consigne,
- pas en deadband,
- présence en near-band,
- régime de gouvernance compatible,
- pas de trajectoire active de source `setpoint`,
- présence d'un contexte crédible de perturbation externe.

Le contexte de perturbation retenu ne repose pas sur `T_steady`.
Il repose sur :

- un écart persistant entre la prédiction du jumeau et le comportement observé, interprété comme un résidu crédible lorsque le modèle est fiable (`bias_warning` ou `external_gain_detected` ou `external_loss_detected`),
- puis une cohérence dynamique via `perturbation_dTdt` et, si nécessaire, la pente thermique mesurée.

Dans ce contrat, FF3 n'est plus un optimiseur générique near-band. Il est réservé à la récupération de perturbation, et il ne provoque plus de restart de cycle dédié à l'entrée en deadband.

### 4.4 Gestion de consigne

`SmartPISetpointManager` pilote désormais une trajectoire analytique pour la branche P.
Cette trajectoire s’active uniquement lorsqu’un écart thermique significatif apparaît, que le modèle est jugé fiable et que la consigne évolue dans un sens nécessitant un lissage de la référence P.

Le principe est le suivant :

- la branche I continue d’utiliser la consigne brute,
- la branche P reçoit `filtered_setpoint`,
- la branche P conserve la consigne brute tant que la zone de freinage prédite n'est pas atteinte,
- le modèle 1R1C appris, `deadtime_cool`, la latence restante du cycle et la puissance engagée sur le cycle servent à détecter cette zone de freinage,
- une trajectoire de freinage tardif douce est ensuite appliquée près de la cible tout en conservant une demande proportionnelle minimale positive,
- pour les trajectoires de consigne en chauffage, un cap d'atterrissage peut contraindre la commande interne après le calcul PI lorsque le modèle prédit que la chaleur stockée suffit à atteindre la cible,
- lorsque le freinage n'est plus nécessaire, la référence filtrée remonte progressivement vers la consigne brute avant l'arrêt de la trajectoire,
- pour une trajectoire issue d'un changement de consigne, l'entrée en phase `release` verrouille ensuite cette phase jusqu'à la fin de la trajectoire, sans retour vers `tracking`,
- `trajectory_active` indique si la trajectoire analytique est en cours,
- la trajectoire se termine seulement lorsque le handoff reste bumpless, que la température mesurée est assez proche de la cible et que l'état d'atterrissage autorise le relâchement, ou lorsque les conditions de fiabilité ne sont plus réunies.

Le cap d'atterrissage utilise la forme discrète du modèle 1R1C dans l'espace de commande interne linéaire :

$$
\alpha = e^{-b \cdot h}
$$

$$
T_{pred} = T_{ext} + (T - T_{ext}) \cdot \alpha + \frac{a}{b}(1-\alpha) \cdot u
$$

Le cap résout la commande maximale qui garde la température prédite sous `target - LANDING_SAFETY_MARGIN_C`. Il est appliqué après le calcul PI normal et avant les contraintes douces, de sorte que `u_pi` reste le diagnostic PI brut tandis que `landing_u_cap` explique la réduction finale de commande.

En mode normal, le bloc de diagnostic `setpoint` publie seulement :

- `filtered_setpoint`,
- `trajectory_active`,
- `trajectory_source`,
- `landing_active`,
- `landing_reason`,
- `landing_u_cap`,
- `landing_coast_required`.

En mode debug, il ajoute les détails de trajectoire :

- `trajectory_start_sp`,
- `trajectory_target_sp`,
- `trajectory_tau_ref`,
- `trajectory_elapsed_s`,
- `trajectory_phase`,
- `trajectory_pending_target_change_braking`,
- `trajectory_braking_needed`,
- `trajectory_model_ready`,
- `trajectory_remaining_cycle_min`,
- `trajectory_next_cycle_u_ref`,
- `trajectory_bumpless_u_delta`,
- `trajectory_bumpless_ready`,
- `landing_sp_for_p_cap`,
- `landing_predicted_temperature`,
- `landing_predicted_rise`,
- `landing_target_margin`,
- `landing_release_allowed`,
- `landing_u_cmd_before_cap`,
- `landing_u_cmd_after_cap`.

Le façonnage de référence reste limité à la branche P afin de préserver la lisibilité de la consigne brute côté intégrale et d’éviter de perturber l’apprentissage. Le cap d'atterrissage est un gouverneur de commande post-PI séparé pour les trajectoires de consigne en chauffage ; il ne réécrit pas l'intégrale et ne change pas la courbe de linéarisation de vanne.

Le code actuel applique aussi une garde explicite sur la croissance positive de l'intégrale pendant les phases de rattrapage :

- après un changement de consigne significatif,
- après une reprise suivant `hvac_off`,
- après une reprise suivant détection d'ouverture,
- après une reprise suivant power shedding,
- pendant une trajectoire de récupération de perturbation.

Cette garde n'empêche pas la décharge de l'intégrale. Elle est relâchée seulement quand deux conditions sont réunies :

- l'erreur de release est revenue proche de l'échelle de la deadband configurée,
- la pente signée de rapprochement est devenue faible de manière persistante.

Le test de pente combine deux critères :

- un seuil relatif à la pente maximale observée pendant le rattrapage,
- un plancher absolu en `°C/h` pour éviter qu'un pic très faible ne rende la release trop permissive.

Le signal utilisé pour cette erreur de release dépend du contexte servo :

- pendant une trajectoire de consigne active, la release se base sur `error_p`, donc l'erreur de la consigne filtrée réellement suivie par la branche proportionnelle,
- hors trajectoire, la release se base sur `error_i`, donc l'erreur brute de consigne.

Cette séparation évite de garder la garde active alors que la trajectoire a déjà convergé vers sa référence filtrée et que le système est physiquement stabilisé sous la consigne brute.

L'erreur brute signée reste néanmoins la référence de sécurité : si `error_i <= 0`, la garde est relâchée immédiatement.

Cette pente est traitée symétriquement :

- en chauffage, une pente positive correspond à un rapprochement vers la consigne,
- en refroidissement, la pente est inversée logiquement pour conserver la même lecture physique.

Tant que la garde reste active, l'atténuation `TRAJECTORY_I_RUN_SCALE` peut limiter la croissance positive de l'intégrale pendant la trajectoire. Dès que la garde est relâchée, cette atténuation n'est plus appliquée : la branche I retrouve sa dynamique normale pour corriger le résidu statique.

Les reprises suivant détection d'ouverture et power shedding ajoutent une étape supplémentaire en chauffage :

- à la reprise, SmartPI arme d'abord un `I:HOLD` explicite,
- ce hold reste actif tant que la réaction de chauffe est encore dans la fenêtre exploitable de `deadtime_heat`,
- à la sortie de cette phase, SmartPI réévalue l'erreur résiduelle signée,
- il n'arme ensuite la garde positive que si cette erreur reste assez grande pour caractériser un vrai rattrapage,
- sinon la branche I revient directement à son fonctionnement normal.

Cette séquence évite de faire apprendre à l'intégrale une dynamique de reprise encore dominée par le temps mort de chauffe.

Les états transitoires de rattrapage ne sont pas restaurés après reboot :

- la trajectoire analytique active est purgée,
- la garde intégrale est remise à zéro,
- tout `integral_hold_mode` temporaire est effacé.

Le redémarrage repart donc d'un état PI persistant utile, mais sans restaurer des états servo transitoires qui ne sont plus physiquement valides hors de leur session d'origine.

### 4.5 Deadband, near-band et protections

`DeadbandManager` gère :

- une deadband symétrique avec hystérésis absolue (`DEADBAND_HYSTERESIS = 0.025`),
- une near-band asymétrique en chauffage,
- un calcul automatique de near-band si `deadtime_heat` et le modèle sont exploitables,
- un repli sur les seuils configurés sinon.

Les protections complémentaires actuellement présentes sont :

- `SmartPIGuards` : `guard_cut` et `guard_kick`,
- anti-windup par tracking,
- garde de croissance positive de l'intégrale pendant les reprises et rattrapages,
- `I:HOLD` explicite pendant la phase post-reprise liée à `deadtime_heat` pour `window_resume` et `power_shedding_resume` en chauffage,
- garde thermique sur baisse de consigne,
- logique de maintien dans la deadband.

Le calcul distingue aussi deux zones autour de la consigne :

- la deadband hystérétique, utilisée pour stabiliser l'état `in_deadband`,
- la deadband centrale réelle, définie par `abs(error_i) < deadband_c`, utilisée pour geler effectivement P et I.

Cette séparation évite de garder P et I gelés uniquement parce que l'hystérésis maintient encore l'état de deadband alors que l'erreur a déjà quitté la deadband configurée.

### 4.6 Auto-calibration

`AutoCalibTrigger` supervise l'algorithme hors loi de commande.

Comportement implémenté :

- snapshot initial quand `tau`, `deadtime_heat` et `deadtime_cool` sont fiables,
- fallback snapshot après `AUTOCALIB_DT_COOL_FALLBACK_DAYS = 7` jours sans `deadtime_cool` fiable,
- snapshot roulant tous les `AUTOCALIB_SNAPSHOT_PERIOD_H = 120` heures,
- vérification horaire,
- garde de cooldown `AUTOCALIB_COOLDOWN_H = 24` heures,
- seuils de stagnation `AUTOCALIB_A_MAD_THRESHOLD = 0.25` et `AUTOCALIB_B_MAD_THRESHOLD = 0.30`,
- sortie positive après au moins `AUTOCALIB_EXIT_NEW_OBS_MIN = 1` nouvelle observation sur `a` et `b` plus des temps morts cohérents,
- jusqu'à `AUTOCALIB_MAX_RETRIES = 3` essais,
- délai de retry planifié `AUTOCALIB_RETRY_DELAY_H = 6` heures.

Le cycle forcé est géré par `CalibrationManager` :

- `COOL_DOWN` jusqu'à `sp - 0.3°C`,
- `HEAT_UP`,
- `COOL_DOWN_FINAL` jusqu'au retour à `sp`.

---

## 5. Architecture logicielle

### 5.1 Orchestrateurs

| Fichier                   | Classe           | Rôle                                                                     |
| ------------------------- | ---------------- | ------------------------------------------------------------------------ |
| `prop_algo_smartpi.py`    | `SmartPI`        | façade algorithmique, orchestration complète                             |
| `prop_handler_smartpi.py` | `SmartPIHandler` | pont Home Assistant : persistance, timer périodique, services, attributs |

### 5.2 Modules `smartpi/` réellement utilisés

| Module                 | Rôle principal                                           |
| ---------------------- | -------------------------------------------------------- |
| `const.py`             | constantes, enums, matrice de gouvernance                |
| `learning.py`          | estimation de pente, `ABEstimator`, `DeadTimeEstimator`  |
| `ab_aggregator.py`     | agrégation médiane ou médiane pondérée des mesures `a/b` |
| `ab_drift.py`          | détection et recentrage de dérive persistante sur `a/b`  |
| `learning_window.py`   | gestion des fenêtres d'apprentissage et de leurs gardes  |
| `gains.py`             | calcul et gel des gains                                  |
| `controller.py`        | PI discret, anti-windup, maintien, hystérésis            |
| `deadband.py`          | deadband et near-band                                    |
| `setpoint.py`          | trajectoire analytique de consigne et cap d'atterrissage |
| `integral_guard.py`    | garde de croissance positive de l'intégrale              |
| `feedforward.py`       | orchestration `u_ff1/u_ff2/u_ff3`                        |
| `ff_trim.py`           | biais lent feed-forward                                  |
| `ff_ab_confidence.py`  | politique de confiance sur `a/b`                         |
| `ff3.py`               | correction prédictive optionnelle                        |
| `governance.py`        | décisions de gel                                         |
| `calibration.py`       | FSM de calibration forcée                                |
| `autocalib.py`         | supervision et déclenchement automatique                 |
| `guards.py`            | `guard_cut` et `guard_kick`                              |
| `thermal_twin_1r1c.py` | jumeau thermique et diagnostics prédictifs               |
| `diagnostics.py`       | payload publié et payload debug                          |
| `tint_filter.py`       | filtrage adaptatif de la température intérieure          |
| `timestamp_utils.py`   | conversion monotonic / wall-clock                        |

### 5.3 Composition interne de `SmartPI`

Les composants instanciés par `SmartPI.__init__()` sont notamment :

```python
self.gov = SmartPIGovernance(name)
self.sp_mgr = SmartPISetpointManager(name, enabled=use_setpoint_filter)
self.ctl = SmartPIController(name)
self.est = ABEstimator(mode=aggregation_mode)
self.learn_win = LearningWindowManager(name)
self.deadband_mgr = DeadbandManager(name, near_band_deg)
self.calibration_mgr = CalibrationManager(name)
self.gain_scheduler = GainScheduler(name)
self.tint_filter = AdaptiveTintFilter(name, enabled=ENABLE_ADAPTIVE_TINT_FILTER)
self.integral_guard = SmartPIIntegralGuard(name)
self.twin = ThermalTwin1R1C(dt_s=SMARTPI_RECALC_INTERVAL_SEC, gamma=0.1)
self.guards = SmartPIGuards()
self.autocalib = AutoCalibTrigger(name)
self._ff_trim = FFTrim()
self._ab_confidence = ABConfidence()
```

### 5.4 Persistance actuelle

Le payload actuel de `SmartPI.save_state()` contient notamment :

```python
{
    "est_state": {...},
    "dt_est_state": {...},
    "gov_state": {...},
    "ctl_state": {...},
    "sp_mgr_state": {...},
    "lw_state": {...},
    "db_state": {...},
    "cal_state": {...},
    "gs_state": {...},
    "integral_guard_state": {...},
    "twin_state": {...},
    "guards_state": {...},
    "ac_state": {...},
    "ff_v2_trim": {...},
    "tint_filter_state": {...},
}
```

### 5.5 Diagnostics publiés

`diagnostics.py` expose trois niveaux :

- `build_diagnostics()` : version compacte ou complète selon `debug_mode`,
- `build_published_diagnostics()` : résumé structuré destiné à `specific_states.smart_pi`,
- `build_debug_diagnostics()` : résumé publié + sous-bloc `debug`.

Quand le jumeau thermique est exploitable, un sous-bloc `pred` est ajouté au debug.

---

## 6. Constantes importantes

### 6.1 Apprentissage

| Constante                      | Valeur |
| ------------------------------ | ------ |
| `AB_HISTORY_SIZE`              | `31`   |
| `AB_MIN_SAMPLES_B`             | `8`    |
| `AB_MIN_SAMPLES_A`             | `6`    |
| `AB_MIN_SAMPLES_A_CONVERGED`   | `6`    |
| `AB_A_SOFT_GATE_MIN_B`         | `8`    |
| `AB_CONFIDENCE_MIN_SAMPLES_A`  | `11`   |
| `AB_CONFIDENCE_MIN_SAMPLES_B`  | `11`   |
| `AB_B_CONVERGENCE_MIN_SAMPLES` | `11`   |
| `AB_B_CONVERGENCE_MIN_BHIST`   | `5`    |
| `AB_B_CONVERGENCE_MAD_RATIO`   | `0.30` |
| `AB_B_CONVERGENCE_RANGE_RATIO` | `0.10` |
| `DT_DERIVATIVE_MIN_ABS`        | `0.05` |
| `OLS_MIN_JUMPS`                | `3`    |
| `OLS_T_MIN`                    | `2.5`  |
| `DT_MAX_MIN`                   | `240`  |
| `U_OFF_MAX`                    | `0.05` |
| `U_ON_MIN`                     | `0.20` |

### 6.2 Régulation

| Constante                     | Valeur  |
| ----------------------------- | ------- |
| `KP_SAFE`                     | `0.55`  |
| `KI_SAFE`                     | `0.010` |
| `MAX_STEP_PER_MINUTE`         | `0.25`  |
| `SETPOINT_BOOST_RATE`         | `0.50`  |
| `AW_TRACK_TAU_S`              | `120.0` |
| `SMARTPI_RECALC_INTERVAL_SEC` | `60`    |
| `LEARNING_PAUSE_RESUME_MIN`   | `20`    |

### 6.3 Bandes et consigne

| Constante                  | Valeur  |
| -------------------------- | ------- |
| `DEFAULT_DEADBAND_C`       | `0.05`  |
| `DEADBAND_HYSTERESIS`      | `0.025` |
| `DEFAULT_NEAR_BAND_DEG`    | `0.40`  |
| `DEFAULT_KP_NEAR_FACTOR`   | `1.0`   |
| `DEFAULT_KI_NEAR_FACTOR`   | `1.0`   |
| `SETPOINT_BOOST_THRESHOLD` | `0.3`   |
| `SETPOINT_BOOST_ERROR_MIN` | `0.3`   |

### 6.4 Auto-calibration et FF

| Constante                         | Valeur |
| --------------------------------- | ------ |
| `AUTOCALIB_SNAPSHOT_PERIOD_H`     | `120`  |
| `AUTOCALIB_DT_COOL_FALLBACK_DAYS` | `7`    |
| `AUTOCALIB_COOLDOWN_H`            | `24`   |
| `AUTOCALIB_A_MAD_THRESHOLD`       | `0.25` |
| `AUTOCALIB_B_MAD_THRESHOLD`       | `0.30` |
| `AUTOCALIB_MAX_RETRIES`           | `3`    |
| `AUTOCALIB_RETRY_DELAY_H`         | `6`    |
| `AUTOCALIB_EXIT_NEW_OBS_MIN`      | `1`    |
| `FF_TRIM_RHO`                     | `0.15` |
| `FF_TRIM_LAMBDA`                  | `0.05` |
| `AB_BAD_PERSIST_CYCLES`           | `3`    |
| `FF3_DELTA_U`                     | `0.05` |
| `FF3_MAX_AUTHORITY`               | `0.20` |
| `FF3_PREDICTION_HORIZON_MIN`      | `30.0` |

---

## 7. Gouvernance Safety-First

La matrice réellement utilisée dans `const.py` est :

| Régime           | Thermique (`a/b`) | Gains              |
| ---------------- | ----------------- | ------------------ |
| `WARMUP`         | `ADAPT_ON`        | `FREEZE`           |
| `EXCITED_STABLE` | `ADAPT_ON`        | `ADAPT_ON`         |
| `NEAR_BAND`      | `ADAPT_ON`        | `ADAPT_ON`         |
| `DEAD_BAND`      | `HARD_FREEZE`     | `HARD_FREEZE`      |
| `SATURATED`      | `ADAPT_ON`        | `FREEZE`           |
| `HOLD`           | `HARD_FREEZE`     | `SOFT_FREEZE_DOWN` |
| `PERTURBED`      | `HARD_FREEZE`     | `HARD_FREEZE`      |
| `DEGRADED`       | `HARD_FREEZE`     | `HARD_FREEZE`      |

Cette gouvernance est centrale :

- elle empêche d'apprendre sur des données jugées polluées,
- elle évite de faire bouger les gains dans des régimes peu informatifs,
- elle laisse malgré tout l'apprentissage thermique actif dans certains régimes contraints comme `SATURATED`.

---

## 8. Références

1. **Astrom K.J. et Hagglund T.**, travaux sur la régulation PID et l'usage de modèles FOPDT.
2. **Sundaresan K.R. et Krishnaswamy P.R.**, estimation des paramètres de délai et de constante de temps.
3. Régression **OLS** avec test t de Student pour la validation de pente.

Ce document sert de référence alignée sur l'implémentation SmartPI actuelle.
