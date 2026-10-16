# Smart-PI: Documentation Technique et Scientifique

## 1. Introduction

**Smart-PI** est un régulateur proportionnel-intégral discret auto-adaptatif, disponible sous forme d'intégration autonome pour *Versatile Thermostat*. Il vise à remplacer un TPI à coefficients fixes par une boucle qui apprend en ligne le comportement thermique réel de la pièce.

Le code actuel repose sur trois idées directrices :

1. identifier un modèle thermique simple du premier ordre avec temps mort,
2. adapter la commande PI à partir de ce modèle,
3. accepter ou rejeter chaque fenêtre d'apprentissage selon le régime physique
   qui l'a produite.

Ce document décrit le comportement réellement implémenté dans le code SmartPI actuel.

---

## 2. Modèle thermique et loi de commande

### 2.1 Modèle thermique utilisé

Le modèle thermique appris est :

$$ \frac{dT_{int}}{dt} = a \cdot u(t) - b \cdot (T_{int}(t) - T_{ext}(t)) $$

Avec :

- $T_{int}$ : température intérieure,
- $T_{ext}$ : température extérieure,
- $u(t) \in [0,1]$ : commande normalisée de l'actionneur actif,
- $a$ : gain thermique signé de l'actionneur, positif en HEAT et négatif en COOL,
- $b > 0$ : coefficient d'échange passif avec l'extérieur.

Un VTherm SmartPI est mono-mode : il apprend soit un modèle HEAT, soit un
modèle COOL. L'équation reste identique dans les deux cas ; le signe de $a$
porte le sens de l'action active.

La constante de temps associée est :

$$ \tau = \frac{1}{b} $$

Le modèle est volontairement simple. Le temps mort est appris séparément et injecté dans les heuristiques de réglage et dans les protections.
Pour les procédés physiques d'ordre supérieur, le `a` appris est donc un gain 1R1C
effectif et local au régime d'exploitation représenté par les fenêtres acceptées,
et non une constante physique unique de toute l'installation. Il doit être validé
sur des conditions d'exploitation représentatives, et non réglé à partir d'un seul
historique.

### 2.2 Chaîne de commande

La commande courante suit la structure :

$$ u = u_{FF} + u_{PI} $$

avec :

- `u_ff1` : feed-forward structurel dérivé de $b/a$,
- `u_ff2` : biais lent `FFTrim`,
- `u_ff3` : correction prédictive bornée de court horizon, activée par défaut et nulle hors contexte éligible,
- `u_pi` : terme PI discret.

La sortie réellement injectée dans le calcul est `u_ff_eff + u_pi`, puis elle passe par :

- limitation de vitesse,
- anti-windup,
- contraintes de cycle et protections.

---

## 3. Identification en ligne

### 3.1 Ordre de bootstrap

En phase `HYSTERESIS`, l'apprentissage suit un ordre strict :

1. le temps mort de la réponse active doit être fiable avant toute collecte
   utile pour `a` (`heat` en HEAT, `cool` en COOL),
2. le temps mort de la réponse passive doit être fiable avant toute collecte
   utile pour `b` (`cool` en HEAT, `heat` en COOL),
3. `b` doit progresser avant `a` via un soft gate.

Règles effectivement codées :

- `AB_MIN_SAMPLES_B = 8`,
- `AB_MIN_SAMPLES_A = 6`,
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
- rejette ou réinitialise une fenêtre ON/`a` sur l'observation exacte du changement
  de consigne utilisateur, afin qu'une fenêtre ne puisse pas couvrir les deux
  régimes,
- conserve l'éligibilité de la montée active initiale suivant cette observation
  pour `a`, sous les garde-fous existants de temps mort, stabilité de puissance,
  pente robuste, FF3 et gouvernance,
- conserve l'apprentissage passif OFF/`b` sous ses garde-fous physiques existants ;
  la calibration conserve son contrat dédié et les réponses dues uniquement à une
  perturbation ne sont pas bloquées par ce gate de consigne,
- bloque la collecte en bootstrap tant que les temps morts requis ne sont pas fiables,
- applique une pause après reprise (`LEARNING_PAUSE_RESUME_MIN = 20`),
- ancre le début de fenêtre après la fin du temps mort quand c'est nécessaire,
- surveille la stabilité de puissance via le coefficient de variation (`U_CV_MAX`),
  évalué dès que la puissance moyenne de la fenêtre atteint `U_CV_MIN_MEAN = 0.05`
  (en dessous, une puissance qui revient dans une fenêtre OFF n'est pas détectée par ce contrôle),
- prolonge la fenêtre tant que la pente n'est pas jugée robuste,
- abandonne après `DT_MAX_MIN = 240` minutes si aucun signal robuste n'émerge.

En pratique, la décision de soumettre ou d'étendre la fenêtre dépend de la robustesse de la pente, pas d'une durée minimale fixe.

### 3.3 Estimation robuste de pente

`ABEstimator.robust_dTdt_per_min()` applique des garde-fous sur la pente :

- amplitude minimale : `DT_DERIVATIVE_MIN_ABS = 0.05°C`,
- nombre minimal de changements de niveau : `OLS_MIN_JUMPS = 3`,
- significativité statistique : `OLS_T_MIN = 2.5`.

Les pentes sont calculées sur la température intérieure brute mesurée, et non sur les signaux filtrés, car le garde-fou de robustesse compte les sauts de valeur distincts de la mesure. L'historique brut (et l'historique filtré utilisé par la détection du temps mort) garde les échantillons des `TIN_HISTORY_RETENTION_MIN = 300` dernières minutes (`DT_MAX_MIN` + 60) quelle que soit la cadence du capteur, de sorte que la pente d'une fenêtre utilise les échantillons depuis le début de la fenêtre ; `TIN_HISTORY_MAX_SAMPLES = 1800` borne seulement la mémoire (environ un échantillon toutes les 10 s sur cette durée). Les pentes validées alimentent ensuite `ABEstimator.learn()` qui :

- rejette les outliers physiques (`max_abs_dT_per_min = 0.35`),
- apprend `b` si `u < U_OFF_MAX`,
- apprend `a` si `u > U_ON_MIN`,
- exige `a > 0` en HEAT et `a < 0` en COOL,
- publie les valeurs avec une agrégation robuste (median ou weighted median selon la configuration).

### 3.4 Estimation de `a` et `b`

Le calcul suivi par `learn()` est :

- phase OFF :

$$ b = -\frac{dT/dt}{T_{int} - T_{ext}} $$

- phase ON :

$$ a = \frac{dT/dt + b \cdot (T_{int} - T_{ext})}{u} $$

Les historiques sont filtrés par median/MAD. La magnitude publiée de `a` est
bornée tout en conservant le signe du mode ; `b` reste strictement positif.

### 3.5 Estimation du temps mort

`DeadTimeEstimator` implémente une FSM indépendante. Les canaux décrivent la
réponse physique de la pièce, pas le sens de la transition de puissance :

- `deadtime_heat` : délai avant une montée de température,
- `deadtime_cool` : délai avant une baisse de température.

Ainsi, OFF -> ON apprend `heat` en mode HEAT mais `cool` en mode COOL.
La transition ON -> OFF apprend la réponse passive opposée.

La détection suit la température intérieure filtrée. Une détection en cours n'est pas reprise après un redémarrage, et la durée minimale OFF doit être observée dans la session en cours avant qu'une détection puisse être armée.

La fiabilité devient vraie dès qu'au moins une mesure valide a été capturée sur le canal correspondant. Chaque canal garde ses 6 dernières détections ; la valeur publiée est leur médiane, de sorte qu'une détection aberrante isolée ne la déplace pas (avec une ou deux détections, la médiane vaut leur moyenne). Au redémarrage, la valeur publiée est recalculée à partir des détections restaurées.

---

## 4. Contrôle Smart-PI

### 4.1 Gains PI

`GainScheduler.calculate()` applique la logique suivante :

1. si `tau`, le signe de `a` ou le temps mort actif ne sont pas fiables,
   repli sur `KP_SAFE = 0.55` et `KI_SAFE = 0.010`,
2. sinon calcul IMC/SIMC avec la magnitude du gain actif :

$$ K_p = \frac{1}{|a| \cdot (\lambda + L)} $$

3. calcul de $K_i = K_p / \tau$,
4. bornage des gains ($K_p \in [0.10, 5.0]$, $K_i \in [0.001, 0.050]$) puis application des gels de gouvernance.

Avec une dynamique de pièce typique ($\tau$ de plusieurs heures), $K_p / \tau$
passe sous `KI_MIN = 0.001` : le $K_i$ appliqué vaut donc généralement le
plancher `KI_MIN`, et l'atténuation de $K_i$ près de la consigne est bornée par
ce même plancher.

Le temps intégral effectif $T_i = K_p / K_i$ des gains appliqués est publié en
minutes dans `control.ti_min` (`None` quand $K_i \le 0$). Avec $K_i$ sur le
plancher, $T_i$ suit tout de même l'inertie de la pièce par $K_p$
($K_p = 1 / (|a| (\lambda + L))$ croît avec $\tau$) : les valeurs de banc sont
d'environ 146 min pour $\tau \approx 3$ h, 247 min pour $\tau \approx 5$ h et
390 min pour $\tau \approx 8$ h. Dans une pièce rapide ($\tau \approx 1$ h),
$K_p / \tau$ est au-dessus du plancher et $K_i$ suit le modèle.

`GainScheduler.generation` compte les changements des gains du modèle non
atténués (IMC/SIMC ou gains de sécurité). Elle n'avance que tant que les gains
ne sont pas gelés et n'est pas avancée par l'atténuation en bande proche. La
fenêtre périodique du trim FF rejette une fenêtre qui couvre plusieurs
générations (`periodic_gains_changed`).

Le temps mort actif est `deadtime_heat` en HEAT et `deadtime_cool` en COOL.

Important :

- l'amortissement near-band est appliqué continûment entre le bord de near-band configuré et la consigne,
- `NEAR_BAND_TAPER_KI_MIN` réduit le gain intégral plus fortement que `NEAR_BAND_TAPER_KP_MIN` près de la consigne.

### 4.2 Feed-forward

Le feed-forward structurel est calculé à partir de :

$$ k_{ff} = \frac{b}{a} $$

et :

$$ u_{ff1} = clamp(k_{ff} \cdot (SP - T_{ext}), 0, 1) \cdot warmup\_scale $$

En COOL, $a < 0$ et donc $k_{ff} < 0$. Lorsque l'extérieur est plus chaud que
la consigne, les deux différences négatives produisent bien une puissance de
refroidissement positive.

Le `warmup_scale` n'est pas un simple interrupteur. Il est le produit :

- du nombre d'apprentissages valides,
- du nombre de cycles depuis reset.

Le trim FF2 reste gelé (`ff_warmup`) tant que `warmup_scale` est inférieur à 1.

$k_{ff}$ est nul, et donc $u_{ff1}$ l'est aussi, lorsque le modèle n'est pas prêt (pas assez de mesures de `a` et de `b`), que `tau` n'est pas fiable, ou que le signe de `a` ne correspond pas au mode actif.

$T_{ext}$ est la dernière température extérieure valide reçue par SmartPI. Lorsque la sonde extérieure ne fournit aucune valeur, FF1, le modèle de consigne et la near-band continuent d'utiliser cette dernière valeur, sans limite d'âge. Tant qu'aucune température extérieure n'a été reçue, $u_{ff1}$ et $u_{ff2}$ sont tous deux nuls (`ff_reason` `ff_no_ext_temp`).

À cela s'ajoutent :

- `u_ff2` : correction lente `FFTrim`, gelée tant que `ABConfidence` vaut `AB_DEGRADED` ou `AB_BAD`, ou tant que `warmup_scale` est inférieur à 1 (`ff2_freeze_reason` `ff_warmup`), et nulle tant qu'aucune température extérieure n'a été reçue.

### 4.3 FF3

`ff3.py` ajoute une correction prédictive activée par défaut et désactivable par configuration :

- horizon calculé par `ff3_predictor.compute_ff3_horizon()` : `deadtime_cycles + FF3_RESPONSE_LOOKAHEAD_CYCLES`, borné entre `FF3_MIN_HORIZON_CYCLES` et `FF3_MAX_HORIZON_CYCLES`,
- prédiction ouverte locale avec le modèle discret exact 1R1C ZOH, sans appel à `ThermalTwin1R1C.step()` et sans mise à jour de l'observateur du jumeau,
- autorité max `FF3_MAX_AUTHORITY = 0.20`, multipliée par `authority_factor`,
- pas d'exploration `FF3_DELTA_U = 0.05`,
- scoring local quadratique avec coût de suivi, coût terminal, coût d'overshoot et coût de mouvement.

FF3 est désactivé si l'une des conditions suivantes n'est pas satisfaite :

- configuration activée,
- mode chauffage,
- température extérieure disponible,
- `tau` fiable,
- jumeau thermique initialisé et prédiction utilisable,
- pas de warm-up du jumeau,
- état stationnaire du jumeau valide,
- pas de calibration,
- pas de power shedding,
- pas de changement récent de consigne,
- pas en deadband,
- présence en near-band,
- régime de gouvernance compatible,
- pas de trajectoire active de source `setpoint`,
- présence d'un contexte crédible de perturbation externe.

Le contexte de perturbation accepte un modèle structurellement utilisable même si `model_reliable = False`.
Dans ce cas, `prediction_quality = "degraded"` et l'autorité est réduite via `FF3_UNRELIABLE_MODEL_AUTHORITY_FACTOR`.
`warming_up = True` et `T_steady_valid = False` restent des blocages absolus.

Le contexte de perturbation retenu ne repose pas sur `T_steady` comme faux négatif RMSE.
Il repose sur :

- un écart persistant entre la prédiction du jumeau et le comportement observé, interprété comme un résidu crédible (`bias_warning` ou `external_gain_detected` ou `external_loss_detected`),
- puis une cohérence dynamique via `perturbation_dTdt` et, si nécessaire, la pente thermique mesurée.

Dans ce contrat, FF3 n'est plus un optimiseur générique near-band. Il est réservé à la récupération de perturbation, et il ne provoque plus de restart de cycle dédié à l'entrée en deadband.

### 4.4 Gestion de consigne

`SmartPISetpointManager` pilote désormais une trajectoire analytique pour la branche P.
Cette trajectoire s’active uniquement lorsqu’un écart thermique significatif apparaît, que le modèle est jugé fiable et que la consigne évolue dans un sens nécessitant un lissage de la référence P.

Le principe est le suivant :

- la branche I continue d’utiliser la consigne brute,
- la branche P reçoit `filtered_setpoint`,
- la branche P conserve la consigne brute tant que la zone de freinage prédite n'est pas atteinte,
- un changement de consigne significatif arme une seule fois le freinage tardif ; HEAT et COOL utilisent ensuite les mêmes équations signées de fenêtre de freinage,
- un déclenchement dû à une perturbation continue d'exiger le seuil d'écart significatif,
- le modèle 1R1C appris, le temps mort de réponse passive du mode (`deadtime_cool` en HEAT, `deadtime_heat` en COOL), la latence restante du cycle et la puissance engagée sur le cycle servent à détecter cette zone de freinage,
- une trajectoire de freinage tardif douce façonne alors la référence P vers une cible de freinage située en deçà de la consigne brute dans le sens de la demande,
- lorsque le freinage n'est plus nécessaire, la référence filtrée remonte progressivement vers la consigne brute avant l'arrêt de la trajectoire,
- pour une trajectoire issue d'un changement de consigne, l'entrée en phase `release` verrouille ensuite cette phase jusqu'à la fin de la trajectoire, sans retour vers `tracking`,
- pendant `release`, la référence P reste du côté de la demande par rapport à la température mesurée de la pièce, sans franchir la consigne brute : elle ne dépasse jamais la cible en HEAT et ne descend jamais sous celle-ci en COOL,
- la trajectoire se termine une fois en `release`, lorsque la référence P et la pièce sont toutes deux à moins de `0,05 °C` de la cible et que le retour de P à la consigne brute modifie la commande proportionnelle d'au plus `0,03`,
- la trajectoire se termine aussi dès que la pièce atteint ou franchit la cible dans le sens de la demande, ou lors de tout changement réel de consigne ; la branche P utilise alors de nouveau la consigne brute,
- `trajectory_active` indique si la trajectoire analytique est en cours.

Le jumeau thermique, FF3 et la prédiction d'entrée en freinage partagent une
fonction pure de propagation 1R1C à puissance constante. Chaque consommateur
conserve ses horizons, sa politique de retard et ses validations.

La propagation utilise la forme discrète du modèle 1R1C dans l'espace de commande interne linéaire :

$$
\alpha = e^{-b \cdot h}
$$

$$
T_{pred} = T_{ext} + (T - T_{ext}) \cdot \alpha + \frac{a}{b}(1-\alpha) \cdot u
$$

L'entrée en freinage prédit la variation signée dans le sens de la demande active (`+1` en HEAT, `-1` en COOL) sur le reste du cycle, le temps mort passif et l'éventuelle puissance déjà prévue pour le cycle suivant. L'écart de freinage vaut cette variation prédite multipliée par `TRAJECTORY_BRAKE_GAIN = 1.20`.

Le bloc canonique `live.setpoint` publie le résumé :

- `filtered_setpoint`,
- `trajectory_active`,
- `trajectory_source`,
- `boost_active`.

Le même payload dans les deux modes expose les détails de trajectoire dans
`live.analysis.trajectory` :

- `start_setpoint`, `target_setpoint`, `tau_ref_min`, `elapsed_s` et `phase`,
- `pending_target_change_braking`, `braking_needed` et `model_ready`,
- `remaining_cycle_min`, `next_cycle_reference`, `bumpless_delta` et `bumpless_ready`.

Le façonnage de référence est limité à la branche P, ce qui laisse la consigne brute intacte côté intégrale. Aucun plafond de commande n'est appliqué après le calcul PI : la commande PI passe directement par les contraintes douces (limiteur de pente, `max_on_percent`) puis par les contraintes de temporisation.

Le code actuel applique aussi une garde explicite sur la croissance positive de l'intégrale pendant les phases de rattrapage :

- après un changement de consigne significatif,
- après une reprise suivant `hvac_off`,
- après une reprise suivant détection d'ouverture,
- après une reprise suivant power shedding,
- pendant une trajectoire de récupération de perturbation.

Cette garde n'empêche pas la décharge de l'intégrale. Si l'intégrale mémorisée
s'oppose déjà au rattrapage demandé, la garde autorise
la résorption de cette mémoire jusqu'à zéro. Elle continue d'interdire toute
nouvelle accumulation au-delà de zéro dans la direction protégée. Une intégrale
négative héritée de l'ancienne consigne ne peut donc plus maintenir la pièce
sous une nouvelle consigne plus élevée.

La garde est relâchée seulement quand deux conditions sont réunies :

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

Le redémarrage repart donc d'un état PI persistant utile, mais sans restaurer des états servo transitoires qui ne sont plus physiquement valides hors de leur session d'origine. Le timestamp mural de la dernière régulation active valide est persisté et n'est pas rafraîchi lorsque le mode HVAC est OFF. Si l'inactivité dépasse 96 heures, la mémoire PI du contrôleur est purgée à la reprise tandis que le modèle A/B appris reste disponible. Si le mode HVAC après un redémarrage diffère du mode du modèle persisté (HEAT versus COOL), l'intégrale est effacée. Lorsqu'une itération de régulation est ignorée parce que le capteur de température est absent ou que `guard_cut` est actif, le suivi du temps repart comme après un passage en OFF, afin que l'erreur ne soit pas intégrée sur la période ignorée.

### 4.5 Deadband, near-band et protections

`DeadbandManager` gère :

- une deadband symétrique avec hystérésis absolue (`DEADBAND_HYSTERESIS = 0.025`),
- une near-band asymétrique en chauffage ; en refroidissement, la near-band utilise la valeur configurée `near_band_deg` des deux côtés de la consigne,
- un calcul automatique de near-band en chauffage uniquement, si `deadtime_heat` et le modèle sont exploitables,
- un repli sur les seuils configurés sinon.

Les protections complémentaires actuellement présentes sont :

- `SmartPIGuards` (chauffage uniquement) : `guard_cut` force la sortie à 0 lorsque la température mesurée dépasse `setpoint + near_band_above` pendant que le chauffage est actif, jusqu'à ce qu'elle redescende à ce seuil diminué de `NEAR_BAND_HYSTERESIS_C = 0.05°C` ; `guard_kick` relance le cycle lorsque, dans la near-band, la température descend sous `setpoint - near_band_below` alors que la commande est inférieure à 99 %. En refroidissement, aucune des deux gardes n'agit et la near-band n'est pas calculée automatiquement,
- anti-windup à chaque tick sur la limite haute applicable (voir ci-dessous),
- garde intégrale signée pendant les reprises, rattrapages et changements de consigne,
- `I:HOLD` explicite pendant la phase post-reprise liée à `deadtime_heat` pour `window_resume` et `power_shedding_resume` en chauffage,
- `I:HOLD` avec source `vt_safety` pendant que le mode sécurité de Versatile Thermostat applique sa puissance par défaut à la place de la commande SmartPI,
- `I:HOLD` avec source `deadband_hysteresis_shell` tant que l'état de deadband hystérétique est actif et que l'erreur est hors de la deadband centrale,
- `I:HOLD` avec source `governance_sensor_invalid` tant qu'aucune température extérieure n'a été reçue (régime `DEGRADED`),
- gel de P et I dans la deadband centrale.

L'anti-windup agit à chaque calcul, dans l'espace de demande linéaire, sur la limite haute applicable : `1`, ou l'image linéaire de `max_on_percent` lorsqu'elle est plus basse. Il comporte deux parties :

- intégration conditionnelle : l'intégrale cesse de croître lorsque la demande non contrainte dépasse la limite haute applicable ;
- rétro-calculation : à chaque tick dont le mode intégral est `I:RUN`, `I:SKIP`, `I:GUARD` ou `I:CLAMP`, l'excès de la sortie PI (`u_ff + u_pi`, bornée à `[0, 1]`) au-delà de l'image linéaire de `max_on_percent` corrige l'intégrale de `beta * excès / Ki`, avec `beta = dt / max(Kp/Ki, dt)` (constante de temps de poursuite égale à `Ti = Kp/Ki`). La correction ne déplace l'intégrale que vers zéro et s'arrête à zéro : un excès porté par le seul terme proportionnel n'entraîne jamais l'intégrale dans l'autre sens. L'intégrale reste bornée à `±2/Ki`. Seule l'intégrale change : `u_pi` conserve la décomposition qui a produit la commande.

Ne sont pas traités comme une saturation : la quantification de Versatile Thermostat et la rétention de vanne (`dpercent`, période), les durées minimales on/off et le limiteur de pente. Le comportement est symétrique en HEAT et COOL, n'utilise pas les accusés de commande de Versatile Thermostat et est identique avec les anciennes versions de Versatile Thermostat. Le diagnostic `aw_du` est la correction (excès, `<= 0`) appliquée au dernier tick.

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
- la stagnation de `b` n'est évaluée que pendant que `b` peut être appris : le gradient intérieur-extérieur est au moins `AB_B_MIN_PASSIVE_GRADIENT_C` et la température extérieure est à au moins `AB_B_OUTDOOR_TARGET_MARGIN_C` de la consigne du côté passif (en dessous en HEAT, au-dessus en COOL), bornes de la fenêtre d'apprentissage ; sans température extérieure ou intérieure, `b` n'est pas évalué,
- la sortie positive est évaluée sur les paramètres qui ont déclenché la calibration (`triggered_params`) : un `a` ou un `b` déclencheur demande au moins `AUTOCALIB_EXIT_NEW_OBS_MIN = 1` nouvelle observation, un temps mort déclencheur doit être fiable ; une demande manuelle (`manual_request`) est jugée sur `deadtime_heat` et `deadtime_cool`,
- jusqu'à `AUTOCALIB_MAX_RETRIES = 3` essais,
- un essai échoué planifie le retry `AUTOCALIB_RETRY_DELAY_H = 6` heures plus tard : jusqu'à cette échéance, la garde de cooldown est remplacée par ce délai et le retry n'attend pas la période du snapshot. L'échéance est publiée sous `autocalib.next_check_ts` et effacée au démarrage du retry, lorsqu'un snapshot est pris ou lorsque la stagnation qui l'exigeait a disparu (le compteur de retry est alors réinitialisé).

`AutoCalibTrigger` publie ses transitions sur le bus d'événements Home Assistant via le handler :
`smartpi_autocalib_snapshot_taken`, `smartpi_autocalib_triggered`, `smartpi_autocalib_success`,
`smartpi_autocalib_retry`, `smartpi_autocalib_degraded` et `smartpi_autocalib_unreachable`. Chaque payload d'événement contient `entity_id`
(l'entité climate, par exemple `climate.salon`) et `name` (le nom du thermostat, par exemple `Salon`),
suivis des champs propres à chaque événement (`smartpi_autocalib_unreachable` : `triggered_params` et `retry_count`).

Le cycle forcé est géré par `CalibrationManager` :

- `COOL_DOWN` jusqu'à `sp - 0.3°C`,
- `HEAT_UP`,
- `COOL_DOWN_FINAL` jusqu'au retour à `sp`.

Avant et pendant chaque phase, le manager vérifie, avec le jumeau thermique, que le seuil de la phase peut être atteint. Une phase pilotée à pleine puissance converge vers `T_steady_max` et une phase passive vers `T_steady_passive` ; le seuil est atteignable lorsque cet équilibre se situe au-delà de lui d'une marge `CALIBRATION_REACHABILITY_MARGIN_C = 0.5°C` (HEAT : `COOL_DOWN` et `COOL_DOWN_FINAL` passifs, `HEAT_UP` à pleine puissance ; COOL : `COOL_DOWN` et `COOL_DOWN_FINAL` à pleine puissance, `HEAT_UP` passif). La vérification ne s'applique que lorsque le jumeau est utilisable (`status` ok, `model_reliable`, passé sa phase de chauffe, équilibre valide, et `cooling_model_available` en COOL). Lorsque le seuil n'est pas atteignable, le cycle se termine immédiatement avec le résultat `Unreachable`, distinct de `Cancelled` (HVAC OFF, timeout de `CALIBRATION_TIMEOUT_MIN = 600` minutes, reset) : `last_calibration_time` n'est pas défini, `AutoCalibTrigger` ne consomme aucun retry et publie `smartpi_autocalib_unreachable`, et la tentative suivante attend `AUTOCALIB_COOLDOWN_H` heures (`autocalib.next_check_ts`). L'équilibre à pleine puissance ne tient pas compte de `max_on_percent`. Le résultat n'est pas un diagnostic publié : `calibration.state` ne porte que la phase.

`CalibrationManager.take_completed_result()` renvoie le résultat d'une exécution terminée (`Success`, `Cancelled`, `Unreachable`), afin que le handler signale chaque exécution à `AutoCalibTrigger`, y compris une exécution qui se termine dans le tick qui l'a démarrée.

---

## 5. Architecture logicielle

### 5.1 Orchestrateurs

| Fichier                   | Classe           | Rôle                                                                     |
| ------------------------- | ---------------- | ------------------------------------------------------------------------ |
| `algo.py`                 | `SmartPI`        | façade algorithmique, orchestration complète                             |
| `handler.py`              | `SmartPIHandler` | pont Home Assistant : timer périodique, services, attributs              |
| `state_store.py`          | `SmartPIStateStore` | persistance Home Assistant (`Store`) de l'état de l'algorithme        |

### 5.2 Modules `smartpi/` réellement utilisés

| Module                 | Rôle principal                                           |
| ---------------------- | -------------------------------------------------------- |
| `const.py`             | constantes, enums, matrice de gouvernance                |
| `ab_estimator.py`      | estimation de pente et `ABEstimator`                     |
| `deadtime_estimator.py` | `DeadTimeEstimator`                                     |
| `ab_aggregator.py`     | agrégation médiane ou médiane pondérée des mesures `a/b` |
| `ab_drift.py`          | détection et recentrage de dérive persistante sur `a/b`, avec une séquence de candidats par canal |
| `learning_window.py`   | gestion des fenêtres d'apprentissage et de leurs gardes  |
| `gains.py`             | calcul et gel des gains                                  |
| `controller.py`        | PI discret, anti-windup, maintien, hystérésis            |
| `deadband.py`          | deadband et near-band                                    |
| `setpoint.py`          | trajectoire analytique de la référence P                    |
| `integral_guard.py`    | garde de croissance positive de l'intégrale              |
| `feedforward.py`       | orchestration `u_ff1/u_ff2/u_ff3`                        |
| `ff_trim.py`           | biais causal et transfert borné trim/intégrale           |
| `ff_trim_runtime.py`   | `FFTrimCoordinator` : décomposition de commande figée des requêtes du scheduler, routage des résultats des observateurs de trim, transactions bumpless trim/intégrale à la frontière de cycle, état de transfert |
| `command_ownership.py` | identifiants de requête, snapshots figés et rattachement des reçus de commande appliquée du scheduler |
| `ff_trim_periodic.py`  | sélection de fenêtres d’équilibre périodique par phase   |
| `ff_ab_confidence.py`  | politique de confiance sur `a/b`                         |
| `ff3.py`               | correction prédictive bornée FF3 et son état d’exécution par cycle (`FF3RuntimeState`) |
| `governance.py`        | décisions de gel                                         |
| `calibration.py`       | FSM de calibration forcée                                |
| `autocalib.py`         | supervision et déclenchement automatique                 |
| `guards.py`            | `guard_cut` et `guard_kick`                              |
| `thermal_twin_1r1c.py` | jumeau thermique et diagnostics prédictifs               |
| `diagnostics.py`       | payload canonique des diagnostics live                  |
| `diagnostic_history.py` | enveloppe versionnée et séries Recorder stables         |
| `tint_filter.py`       | filtrage adaptatif de la température intérieure          |
| `timestamp_utils.py`   | conversion monotonic / wall-clock                        |

### 5.3 Composition interne de `SmartPI`

Les composants instanciés par `SmartPI.__init__()` sont notamment :

```python
self.gov = SmartPIGovernance(name)
self.sp_mgr = SmartPISetpointManager(name, enabled=use_setpoint_filter, release_tau_factor=release_tau_factor)
self.ctl = SmartPIController(name)
self.est = ABEstimator()
self.learn_win = LearningWindowManager(name)
self.deadband_mgr = DeadbandManager(name, near_band_deg)
self.calibration_mgr = CalibrationManager(name)
self.gain_scheduler = GainScheduler(name)
self.tint_filter = AdaptiveTintFilter(name, enabled=ENABLE_ADAPTIVE_TINT_FILTER)
self.integral_guard = SmartPIIntegralGuard(name)
self.dt_est = DeadTimeEstimator()
self.twin = ThermalTwin1R1C(dt_s=SMARTPI_RECALC_INTERVAL_SEC, gamma=0.1)
self.guards = SmartPIGuards()
self.autocalib = AutoCalibTrigger(name)
self.ff3_state = FF3RuntimeState()
self._ff_trim = FFTrim()
self._fftrim_observer = CausalFFTrimObserver(cycle_min, self._causal_power_trace)
self._fftrim_periodic_observer = PeriodicFFTrimObserver(cycle_min)
self._command_ownership = CommandOwnershipTracker()
self.fftrim_coord = FFTrimCoordinator(self)
self._ab_confidence = ABConfidence()
```

### 5.4 Persistance actuelle

L'observateur causal aligne une seconde timeline, pondérée dans le temps, de
la propriété des termes de commande avec celle de la puissance physique. Une
fenêtre stricte en deadband peut ainsi séparer le biais intégral stable
$J=mean(K_i integral)$ du déficit physique
$D=H-mean(u_{applied})$. Après trois fenêtres indépendantes cohérentes, une
transaction bornée applique
`delta_trim = alpha * lambda * (J + D)` et
`delta_u_i = -alpha * lambda * J`, donc
`delta_u_cmd = alpha * lambda * D`. Le même facteur `alpha` respecte l'autorité du
trim, les clamps FF, la capacité intégrale et la marge de commande. La
transaction s'exécute uniquement à la frontière de cycle,
dans le même espace de puissance signé en HEAT et COOL.
Les transferts inférieurs au seuil générique de précision de l'actionneur
restent ignorés, sauf lorsqu'ils réduisent strictement des contributions trim
et intégrale opposées. Ce compactage de propriété conserve les mêmes bornes
atomiques et le même invariant de commande.

Les observateurs stationnaire et périodique réutilisent une seule timeline de
puissance physique et de propriété, mais conservent des mesures thermiques, un
washout et des buffers de persistance séparés. La voie périodique accepte la
saturation basse et le maintien de l’intégrale comme parties d’un cycle créé
par la régulation. Elle ne ferme la fenêtre qu’après un retour à la même phase
thermique et au moins `max(6 cycles VT, 2 × temps mort)` ; elle n’a donc pas de
plancher fixe de trente minutes. Son estimation causale pondérée dans le temps
n’effectue jamais de transfert de propriété de l’intégrale ; les corrections
persistantes restent soumises au même taux d’apprentissage et à la même limite
d’autorité du trim.

Un gel du trim vide le buffer stationnaire. Un gel de raison `saturated`
conserve les mesures et les propositions en attente de la voie périodique, et
la fenêtre périodique reste valide ; le trim lui-même n'est pas modifié tant
qu'il est gelé. Toute autre raison de gel vide le buffer périodique et
invalide la fenêtre.

Le payload actuel de `SmartPI.save_state()` contient notamment :

```python
{
    "version": 2,
    "learning_enabled": ...,
    "last_active_wall_ts": ...,
    "deadtime_skip_count_a": ...,
    "deadtime_skip_count_b": ...,
    "est_state": {...},
    "dt_est_state": {...},
    "gov_state": {...},
    "ctl_state": {...},
    "sp_mgr_state": {...},
    "lw_state": {...},
    "db_state": {...},
    "cal_state": {...},
    "gs_state": {...},
    "twin_state": {...},
    "guards_state": {...},
    "ac_state": {...},
    "ff_v2_trim": {...},
    "ff_trim_delta": ...,
    "tint_filter_state": {...},
}
```

La boucle de régulation planifie au plus une écriture différée de ce payload par fenêtre de cinq minutes ; Home Assistant vide une écriture en attente à l'arrêt. Les services et la suppression de l'entité écrivent immédiatement un snapshot.

Les timestamps d'événements passés (historiques de température, début de calibration) sont persistés en temps mural et reconvertis en horloge monotonic au chargement, afin de conserver leur position dans le passé après un redémarrage ; en particulier, le délai d'expiration de la calibration continue de s'écouler après un redémarrage.

Seul le trim appris reste persistant. Les fenêtres de propriété, propositions
en attente et transferts d'intégrale sont transitoires ; après redémarrage,
l'observateur est reconstruit et le gel existant de trois cycles est conservé.

La propriété des commandes vient des reçus de commande appliquée du scheduler
de cycles de Versatile Thermostat (`AppliedCommandReceipt` de `vtherm_api`).
Juste avant de soumettre une requête, SmartPI fige sa décomposition de
commande et passe un `request_id` strictement croissant à `start_cycle`. Le
scheduler renvoie cet identifiant dans un reçu pour chaque commande qu’il
applique réellement : un reçu `cycle_start` à chaque début de cycle maître, y
compris son redémarrage automatique, et un reçu `update` pour chaque position
de vanne appliquée en cours de cycle. Aucun reçu n’est émis pour une requête
switch en attente du cycle suivant. Le reçu engage la puissance actionneur
appliquée, convertie dans l’espace linéaire du modèle, et rattache le snapshot
figé de sa requête. Un reçu n’est possédé que si sa requête est connue, garde
son mode HVAC, ne porte aucune altération `min_on_off` ou `valve_retained` et
n’a pas été contrainte par le timing ; une requête rejetée reste rejetée pour
ses reçus suivants. Les commandes non taguées ne sont jamais possédées : les
substitutions de Versatile Thermostat comme la puissance par défaut de
sécurité, et les mises à jour de vanne différées de Versatile Thermostat. Les
32 dernières requêtes sont conservées, de sorte qu’un scheduler qui applique
une requête plus ancienne l’attribue encore à cette requête. Un reçu absent ou
rejeté ne reconstruit jamais la propriété depuis l’état courant du contrôleur.
Un transfert bumpless est engagé dès qu’un reçu possédé porte une requête
préparée après le transfert.

Sans prise en charge des reçus par Versatile Thermostat, SmartPI soumet des
requêtes non taguées et engage la puissance indiquée par le callback de début
de cycle, ou l’ouverture de vanne publiée en mode vanne, sans propriété. Le
trim stationnaire utilise alors le `u_ff1` échantillonné, le trim périodique et
le transfert bumpless restent inactifs, et la régulation PI est inchangée. Une
mise à jour de vanne sans ouverture publiée n’engage rien et marque une
discontinuité de trace.

### 5.5 Diagnostics publiés

`diagnostics.py` expose les vues algorithmiques et live canoniques :

- `SmartPI.get_published_diagnostics()` : renvoie le payload `live` canonique du capteur de diagnostic dédié,
- `build_published_diagnostics()` : la fonction qui construit ce payload.

`diagnostic_history.py` enveloppe ce payload dans `schema_version`, `live` et le
bloc stable de onze séries `history`. Les entités normales excluent `live` du
Recorder ; la sous-classe debug l'enregistre directement. Le mode debug ne fait que sélectionner la classe de capteur enregistrée ; il n'ajoute pas de second bloc de diagnostic.

`analysis.learning.learn_ok_count` et `learn_skip_count` sont des compteurs
d'exécution relatifs au processus Home Assistant courant. Les compteurs
persistés du modèle restent internes et servent aux décisions de qualité du
modèle et de warm-up. Quand le jumeau thermique est exploitable, ses champs
publics sont exposés sous `analysis.twin`.

`feedforward.fftrim.command_ownership` indique si le scheduler émet des reçus
et expose le dernier état de rattachement, son motif de rejet, l’identifiant de
requête (`request_sequence`), le type de reçu, les puissances demandée et
appliquée et leur delta, les durées ON/OFF appliquées, les altérations du reçu,
l’indicateur de contrainte temporelle et les compteurs de rejet de la session
Home Assistant courante. Ces valeurs sont des observations : elles expliquent
une acceptation ou un rejet causal sans modifier la décision d’apprentissage.

---

## 6. Constantes importantes

### 6.1 Apprentissage

| Constante                      | Valeur |
| ------------------------------ | ------ |
| `AB_HISTORY_SIZE`              | `31`   |
| `AB_MIN_SAMPLES_B`             | `8`    |
| `AB_MIN_SAMPLES_A`             | `6`    |
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
| `SMARTPI_RECALC_INTERVAL_SEC` | `60`    |
| `LEARNING_PAUSE_RESUME_MIN`   | `20`    |

### 6.3 Bandes et consigne

| Constante                  | Valeur  |
| -------------------------- | ------- |
| `DEFAULT_DEADBAND_C`       | `0.05`  |
| `DEADBAND_HYSTERESIS`      | `0.025` |
| `DEFAULT_NEAR_BAND_DEG`    | `0.40`  |
| `NEAR_BAND_TAPER_KP_MIN`   | `0.75`  |
| `NEAR_BAND_TAPER_KI_MIN`   | `0.45`  |
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
| `FF3_DELTA_U`                     | `0.05` |
| `FF3_MAX_AUTHORITY`               | `0.20` |
| `FF3_NEARBAND_GAIN`               | `0.50` |
| `FF3_MIN_HORIZON_CYCLES`          | `2`    |
| `FF3_RESPONSE_LOOKAHEAD_CYCLES`   | `2`    |
| `FF3_MAX_HORIZON_CYCLES`          | `8`    |
| `FF3_ACTION_SENSITIVITY_EPS_C`    | `1e-4` |
| `FF3_SCORE_EPS_COST`              | `1e-4` |
| `FF3_UNRELIABLE_MODEL_AUTHORITY_FACTOR` | `0.5` |

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

La matrice pilote uniquement la mise à jour des gains et l'apprentissage thermique. Une transition de régime (`REGIME_TRANSITION`) gèle les gains et l'apprentissage mais ne maintient pas l'intégrale. L'intégrale n'est maintenue que par les sources listées dans les protections de la section 4 (deadtime, phases de reprise, `vt_safety`) et par deux sources liées à la gouvernance : `deadband_hysteresis_shell` (état de deadband hystérétique actif, erreur hors de la deadband centrale) et `governance_sensor_invalid` (régime `DEGRADED` avant la réception d'une première température extérieure). Lorsque la sonde extérieure cesse de fournir des valeurs après une première valeur, le régime est `DEGRADED` : les gains, l'apprentissage thermique et le trim FF restent gelés, l'intégrale continue d'intégrer, et la chaîne de commande utilise la dernière température extérieure valide.

---

## 8. Références

1. **Astrom K.J. et Hagglund T.**, travaux sur la régulation PID et l'usage de modèles FOPDT.
2. **Sundaresan K.R. et Krishnaswamy P.R.**, estimation des paramètres de délai et de constante de temps.
3. Régression **OLS** avec test t de Student pour la validation de pente.

Ce document sert de référence alignée sur l'implémentation SmartPI actuelle.
