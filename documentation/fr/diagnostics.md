# Diagnostics SmartPI

SmartPI expose un capteur de diagnostic Home Assistant dédié. Son état indique
la phase de régulation courante et ses attributs suivent le schéma version 2.

Tant que le premier modèle n'est pas publié, l'état, `live.control.phase`,
`live.learning.stage` et `live.model.confidence` nomment le démarrage de l'apprentissage :

| Démarrage | État | `control.phase` | `learning.stage` | `model.confidence` |
|---|---|---|---|---|
| Amorçage | `bootstrap_hysteresis` | `Hysteresis` | `bootstrap` | `ab_bootstrap` |
| Démarrage direct | `learning_pulses` | `Learning` | `measurement pulses` | `ab_learning` |

Ensuite les deux démarrages publient les mêmes valeurs (`stable` ou `calibration`,
`Stable` ou `Calibration`, `learning`, `monitoring` ou `degraded`, `ab_ok`,
`ab_degraded` ou `ab_bad`).

## Enveloppe des attributs

```yaml
schema_version: 2
live:     # Diagnostics live complets
history:  # Contrat stable des séries temporelles
```

`live` est la source des dashboards, des cartes Markdown et de l'analyse
interactive. Son contenu est identique en mode normal et en mode debug.
`history` contient uniquement les valeurs destinées aux graphiques historiques.

Il n'existe pas de bloc d'attribut `debug` séparé.

## Profils Recorder

Le mode normal exclut l'attribut `live` complet du Recorder Home Assistant.
Seuls `schema_version`, l'état du capteur et `history` sont conservés.

Le mode debug enregistre `live` et `history`. Le contenu n'est pas dupliqué
dans `history` : le contrat historique reste identique dans les deux modes. Le
mode debug change donc la conservation, pas la publication.

Le bloc `history` contient ces dix-neuf séries :

| Chemin sous `history` | Signification |
|---|---|
| `temperature.indoor` | Température intérieure |
| `temperature.anticipated` | Température intérieure plus la correction d'anticipation : la température lue par les erreurs proportionnelle et intégrale |
| `temperature.integral_error` | État de l'intégrale (°C·min) ; `ki × integral_error` est sa part de la commande (0 à 1) |
| `control.kp` | Gain proportionnel |
| `control.ki` | Gain intégral |
| `power.applied_percent` | Commande appliquée après les contraintes de sortie |
| `power.command_percent` | Commande demandée avant les effets de l'actionneur |
| `power.pi_percent` | Contribution PI |
| `power.ff_percent` | Contribution feed-forward |
| `model.a` | Gain de chauffe appris |
| `model.b` | Coefficient de déperdition appris |
| `model.effective_dead_time_s` | Temps mort effectif du modèle de réponse, en secondes (`model.dead_time_s` plus la moitié de `model.response_time_constant_s`) ; publié seulement, non utilisé par la régulation |
| `feedforward.source` | Source de la puissance de maintien en vigueur, comme dans `live.feedforward.source` |
| `feedforward.wall_power_percent` | Part des murs du feed-forward appliquée, en % de la pleine puissance (négative lorsque les murs restituent de la chaleur) ; `null` lorsqu'aucune part des murs n'est appliquée |
| `room_model.a` | Réponse de chauffe du modèle de pièce, dans les unités de `model.a` ; `null` avant une publication |
| `room_model.b_eq` | Déperdition statique du modèle de pièce, dans les unités de `model.b` ; `null` avant une publication |
| `room_model.forecast_error_1h_c` | Racine de l'erreur quadratique moyenne des prévisions à 1 h sur les 7 derniers jours (°C) |
| `room_model.forecast_error_12h_c` | Idem pour les prévisions à 12 h (°C) |
| `deadtime.applied_s` | Temps mort de chauffe en vigueur pour la régulation, en secondes (le temps mort du bootstrap, ou la valeur dérivée du retard de l'émetteur appris) |

Les séries feed-forward, modèle de pièce, temps mort et temps mort effectif sont
toujours présentes et valent `null` lorsque le bloc correspondant n'est pas calculé.
La commande appliquée ne mesure pas la chaleur délivrée.

## Diagnostics live

Le bloc `live` est organisé par responsabilité :

| Bloc | Contenu |
|---|---|
| `control` | Phase, mode, gains (`kp`, `ki`), temps intégral effectif `ti_min` (minutes), saturation, bandes de régulation et dernier redémarrage (`last_restart`) |
| `power` | Puissances du cycle courant et suivant, PI/FF, limites et puissance appliquée |
| `temperature` | Températures intérieure/extérieure, température anticipée (`anticipated` : température intérieure plus la correction d'anticipation), erreurs et état de l'intégrale |
| `model` | Modèle A/B, confiance, constante de temps, temps morts et modèle de réponse |
| `learning` | Étape d'apprentissage, bootstrap, mises à jour acceptées, dérive et démarrage de l'apprentissage (`start`) |
| `governance` | Régime thermique et décision de mise à jour du modèle |
| `feedforward` | Source de la puissance de maintien, part des murs de la puissance de maintien et source de bande morte |
| `setpoint` | État du boost de consigne |
| `calibration` | La calibration en cours ou la dernière : état, impulsions, résultat, dernière demande, temps morts mesurés, lecture, valeurs en usage et corrections |
| `analysis` | Champs avancés utilisés par les cartes de diagnostic fournies |
| `valve` | Réglages de vanne Versatile Thermostat vus par SmartPI, `null` hors mode vanne |
| `tar_learning` | Apprentissage du retard de l'émetteur |
| `room_model` | Modèle de pièce |

`control.mode` vaut `smartpi` lorsque le PI régule, `hysteresis` pendant le
bootstrap, et `heating cut above the setpoint` pendant une coupure de la garde
(la pièce a dépassé la consigne de la near-band au-dessus : le chauffage est
forcé à l'arrêt). Le PI n'est pas calculé pendant une coupure :
`temperature.error` et `analysis.control.error_p` sont alors la consigne moins la
température du dernier calcul, `analysis.control.anticipation_c` vaut `0` et
`temperature.anticipated` est la température intérieure.

`temperature.integral_hold_source` vaut `calibration_recovery` tant que
l'intégrale est figée après une impulsion de calibration, jusqu'à ce que la
pièce atteigne la consigne.

`control.last_restart` décrit le dernier redémarrage de Home Assistant ou de
l'intégration. Il vaut `null` avant le premier calcul de la régulation après le
redémarrage, et pour un thermostat qui démarre sans données sauvegardées.

| Champ | Signification |
|---|---|
| `bridged` | `true` lorsque le redémarrage a été ponté : les mesures en cours ont repris et rien n'a été exclu des données d'apprentissage. `false` lorsque SmartPI a repris avec prudence |
| `downtime_s` | Durée de l'arrêt, en secondes ; `null` lorsqu'elle ne peut pas être mesurée (saut d'horloge, pas de données sauvegardées) |
| `reason` | Raison pour laquelle le redémarrage n'a pas été ponté, `null` lorsqu'il l'a été : `stop too long`, `heater state not saved`, `not heating or cooling before the stop`, `not heating or cooling after the restart`, `HVAC mode changed across the stop`, `cycle settings changed`, `indoor temperature unknown` (aussi lorsque la commande est passée à 0 pendant que la température était attendue), `temperature jump across the stop`, `clock jump`, `no saved state of the last tick`, ou une exclusion avant l'arrêt ou après le redémarrage (par exemple `window open before the stop`, `safety mode after the restart`) |

`learning.start` résume comment l'apprentissage a démarré et ce que les
impulsions de mesure du démarrage direct ont donné (voir l'option **Démarrage de
l'apprentissage** dans la [documentation SmartPI](vtherm_smartpi.md)). Les valeurs
sont celles du mode HVAC dans lequel se trouve le modèle ; le démarrage par
amorçage affiche `0` impulsion et aucune impulsion en cours.

| Champ | Signification |
|---|---|
| `mode` | Démarrage de l'apprentissage en cours : `bootstrap` ou `direct`. Un thermostat qui a déjà appris garde le démarrage avec lequel son apprentissage a commencé, quelle que soit l'option |
| `pulses_done` | Impulsions de mesure démarrées depuis le début de l'apprentissage, dans le mode courant (trois au plus) ; les impulsions d'une calibration ne sont pas comptées |
| `pulse` | L'impulsion en cours : `none`, `rest` (sans chauffage, en attendant que la pièce descende), `full power` ou `stop` (attente du pic de température) ; une impulsion de calibration affiche `none` ici |
| `values_kept` | `true` lorsque deux lectures concordantes ont donné des valeurs pour le mode courant |
| `values_kept_at` | Heure locale à laquelle les valeurs ont été conservées (ISO 8601), `null` si aucune n'est conservée ou si l'heure est inconnue |
| `kept_a` | Vitesse de chauffe conservée en °C/min à pleine puissance (5 décimales), `null` si aucune n'est conservée |
| `kept_b` | Coefficient de pertes conservé en 1/min (6 décimales), `null` si aucun n'est conservé |
| `kept_lag_s` | Retard de l'émetteur conservé en secondes, `null` si aucun n'est conservé |

`calibration` résume la calibration demandée avec le service
`force_smartpi_calibration` (voir **Calibration** dans la
[documentation SmartPI](vtherm_smartpi.md)) : la calibration en cours, ou la
dernière jusqu'à la demande suivante.

| Champ | Signification |
|---|---|
| `state` | `idle` (aucune calibration en cours), `waiting` (entre deux impulsions, la régulation fonctionne), ou l'étape de son impulsion en cours : `rest`, `full power`, `stop` |
| `pulses_done` | Impulsions démarrées par la calibration (trois au plus ; une impulsion interrompue compte) |
| `result` | `null` avant toute calibration, `in progress`, `readings agree` (deux impulsions concordent), `pulses done` (trois impulsions lues sans concordance), `time limit` (48 heures) ou `cancelled` (passage entre chauffage et refroidissement, retour de la phase d'apprentissage, réinitialisation de l'apprentissage, apprentissage mis en pause) |
| `last_request` | Réponse à la dernière demande, `null` avant toute demande : `started`, `refused: learning stage` (premier modèle en apprentissage), `refused: learning paused` (apprentissage en pause), `refused: thermostat off` (thermostat éteint), `refused: too soon` (moins de 24 heures après la fin de la dernière calibration), `ignored: in progress` (calibration en cours) |
| `last_time` | Heure locale de fin de la dernière calibration non annulée (ISO 8601), `null` avant |
| `deadtime_start_s` | Dernier temps mort après un démarrage mesuré pendant une impulsion de la calibration, en secondes, `null` sans mesure |
| `deadtime_stop_s` | Dernier temps mort après un arrêt mesuré pendant une impulsion de la calibration, en secondes, `null` sans mesure |
| `reading_a` | Vitesse de chauffe des deux impulsions concordantes en °C/min à pleine puissance (5 décimales), `null` tant que deux impulsions ne concordent pas |
| `reading_b` | Coefficient de pertes des deux impulsions concordantes en 1/min (6 décimales), `null` tant que deux impulsions ne concordent pas ; publié seulement, jamais corrigé |
| `reading_lag_s` | Retard de l'émetteur des deux impulsions concordantes en secondes, `null` tant que deux impulsions ne concordent pas |
| `in_force_a` | Vitesse de chauffe en usage lorsque les impulsions ont concordé, en °C/min à pleine puissance (5 décimales), comparée à `reading_a` ; `null` tant que deux impulsions ne concordent pas |
| `in_force_b` | Coefficient de pertes en usage lorsque les impulsions ont concordé, en 1/min (6 décimales) ; `null` tant que deux impulsions ne concordent pas |
| `in_force_lag_s` | Horizon de l'anticipation en usage lorsque les impulsions ont concordé, en secondes, comparé à `reading_lag_s` ; `null` tant que deux impulsions ne concordent pas |
| `corrected` | Ce que la calibration a corrigé : une liste parmi `heating rate` (vitesse de chauffe), `emitter lag` (retard de l'émetteur) ; vide si rien n'a été corrigé |

Les temps morts mesurés par les impulsions rejoignent les temps morts appris
comme toute autre mesure. Une grandeur de la lecture n'est corrigée que si elle
s'écarte de plus de 30 % de la valeur en usage (voir **Calibration** dans la
[documentation SmartPI](vtherm_smartpi.md)) ; la lecture ne fixe jamais
l'horizon de l'anticipation.

`analysis` regroupe les valeurs live avancées dans `control`, `learning`,
`deadtime`, `governance` et `feedforward`.

`analysis.control` publie, parmi les termes d'erreur et la géométrie de bande,
l'anticipation de la température, en chauffage et en refroidissement :

| Champ | Signification |
|---|---|
| `anticipation_c` | Correction en °C ajoutée à la température mesurée pour les erreurs proportionnelle et intégrale au dernier tick, `0` lorsqu'elle est inactive (hors phase stable, sans horizon, sans pente montante ou descendante, ou pendant une coupure de la garde) |
| `anticipation_horizon_s` | Horizon de l'anticipation en secondes (le temps entre une commande et son effet), `0` lorsqu'il n'y en a pas |
| `anticipation_horizon_source` | Origine de l'horizon : `measurement pulses` (retard de l'émetteur conservé des impulsions de mesure du démarrage direct, en chauffage et en refroidissement), `heating balance` (médiane des derniers retards de l'émetteur mesurés), `dead times` (le plus long entre le temps mort actif du mode et la moitié de son temps mort passif) ou `none` ; le bilan de chauffe n'est utilisé qu'en chauffage |
| `effective_dead_time_s` | Temps mort effectif du modèle de réponse, en secondes (temps mort plus la moitié de la constante de temps de réponse), `null` sans modèle ; publié seulement |

`analysis.deadtime` suit la détection du temps mort :

| Champ | Signification |
|---|---|
| `state` | État du détecteur (`WAITING_HEAT_RESPONSE`, `WAITING_COOL_RESPONSE`, `HEATING`, `COOLING`, `OFF`) |
| `last_power` | Dernière puissance vue par le détecteur |
| `waiting_elapsed_s` | Âge de la réponse attendue en secondes, `null` quand aucune détection n'est armée |
| `heat_last_outcome` / `cool_last_outcome` | Issue de la dernière attente d'une réponse montante / descendante : `detected`, `timeout`, `aborted` (transition de puissance avant détection), `rejected` (la température évoluait déjà dans le sens de la réponse avant le démarrage de la puissance, trop fortement ou trop irrégulièrement pour la mesurer), `ignored` (une mesure plus courte que le temps mort publié, écartée parce que la température évoluait déjà dans le sens de la réponse avant le démarrage ou que la fenêtre avant le démarrage était trop courte pour juger), `null` avant la première |

`analysis.learning.heating_balance` décrit la vitesse de chauffe et le retard de l'émetteur mesurés sur les phases de chauffe. La régulation ne lit que `kept_lag_s` :

| Champ | Signification |
|---|---|
| `state` | `resting`, `heating` ou `waiting for the peak` (après la coupure) |
| `measurements` | Nombre de mesures conservées (7 au plus) |
| `a` | Vitesse de chauffe médiane en °C/min à pleine puissance (5 décimales), `null` sans mesure |
| `lag_s` | Retard médian de l'émetteur en secondes, `null` sans mesure qui le résout |
| `kept_lag_s` | Médiane des 7 derniers retards de l'émetteur mesurés, en secondes, `null` tant qu'aucun n'a été mesuré ; une mesure sans retard de l'émetteur n'en retire aucun. C'est l'horizon de l'anticipation lorsqu'il est positif |
| `kept_lags` | Nombre de retards de l'émetteur retenus (7 au plus) |
| `last_reason` | Issue de la dernière phase terminée : `measured`, ou la raison pour laquelle elle n'a rien donné (`rest before heating too short`, `baseline samples insufficient`, `heating too short`, `temperature rise too small`, `still rising at the next heating`, `no peak within the wait`, `observation gap`, `heating mode left`, `interrupted by restart`). Vide avant la première phase |
| `last` | Dernière mesure, `null` sans mesure : `heating_min` (durée de chauffe à pleine puissance en minutes), `rise_c` (hausse de température au pic au-dessus de la trajectoire passive), `peak_delay_min` (minutes entre la coupure et le pic), `a`, `lag_s` (`null` lorsque la puissance n'était pas constante ou que le retard de l'émetteur n'est pas résolu), `constant_power`, `context` (`bootstrap`, `calibration` ou `regulation`, au début de la phase) |

Pendant le bootstrap, `learning.bootstrap_status` décrit l'étape en cours :

| Valeur | Signification |
|---|---|
| `step1 - waiting response model` | Aucune fenêtre d'apprentissage avant la publication du modèle de réponse du mode (la première phase de chauffe ou impulsion lue en entier le donne) |
| `step2 - collecting emeas: A:n/5 B:n/5` | Collecte des mesures de `a` et `b` |

`valve` vaut `null` sauf si le thermostat pilote une vanne. En mode vanne, il contient :

| Champ | Signification |
|---|---|
| `vt_dpercent` | Seuil de régulation de Versatile Thermostat en `%` (`null` si indisponible) |
| `vt_period_min` | Période minimale de régulation de Versatile Thermostat en minutes (`null` si indisponible) |
| `opening_remap_active` | `true` lorsque les réglages d'ouverture de Versatile Thermostat modifient l'ouverture envoyée à la vanne |
| `quantization_step_linear_percent` | Demande linéaire, en pourcentage, déplacée par un pas de vanne de `1%` à la position de vanne engagée ; `1.0` sans linéarisation, `null` lorsque la vanne est fermée |
| `issues` | Codes de filtrage, de retard de commande ou de transformation d'ouverture en conflit avec la linéarisation SmartPI : `dpercent_filter`, `period_filter`, `opening_remap`. Les mêmes avertissements apparaissent dans les Réparations de Home Assistant ; un filtrage ou un retard ne prouve pas, à lui seul, un échec de l'apprentissage |

### Modèle de réponse

Le modèle de réponse décrit, pour le mode HVAC dans lequel se trouve le modèle, la façon
dont la pièce répond à la puissance : un temps mort (sonde, transport, actionneur) suivi
d'une constante de temps de réponse (l'émetteur, l'air et la masse proche ensemble). Il est ajusté sur des événements de mesure
entiers : les impulsions, les phases du bootstrap, les impulsions de calibration et les
montées de confort de la régulation après un repos (celles-ci seulement la nuit, la position
du domicile étant connue). L'apprentissage de `a` et `b` le lit (aucune fenêtre d'apprentissage de la phase
d'apprentissage avant lui, puissance délivrée, délai d'une fenêtre d'apprentissage après un
démarrage ou un arrêt) ; le régulateur lui-même (gains, maintiens, anticipation, bande proche)
ne le lit pas.

| Chemin | Signification |
|---|---|
| `model.dead_time_s` | Temps mort en secondes, `null` sans modèle |
| `model.response_time_constant_s` | Constante de temps de réponse en secondes, `null` sans modèle |
| `model.response_source` | Origine du dernier événement accepté : `measurement pulses`, `bootstrap`, `calibration`, `regulation`, ou `previous version` (valeurs reprises du temps mort et du retard de l'émetteur appris avant l'existence du modèle, jusqu'au premier événement accepté) ; `null` sans modèle |
| `model.response_confidence` | `measured` (deux événements concordent sur le temps mort plus la constante de temps à 20 % près), `provisional` (un événement, ou valeurs de la version précédente), `dead time assumed` (aucun événement ne distingue le temps mort : la moitié du total est supposée) ; `null` sans modèle |

`analysis.learning.response_model` :

| Champ | Signification |
|---|---|
| `events` | Nombre d'événements acceptés conservés pour le mode (au plus 7) |
| `last` | Dernier événement ajusté, `null` avant le premier : `source`, `dead_time_s`, `response_time_constant_s`, `rms_c` (racine de la moyenne des carrés des résidus aux changements de température, °C), `outcome` (`accepted`, `accepted, response time constant at its lower limit` (60 s, un émetteur rapide), `used in daylight until an event in darkness`, `not used in daylight`, ou la raison du rejet : `too few temperature changes`, `temperature rise too small`, `no heating seen`, `dead time at its limit`, `response time constant at its limit`, `fit too far from the room temperature`), `daytime` (`true` pour un événement de jour : sa chauffe démarre hors de la nuit, ou la nuit couvre moins de trois retards d'émetteur après elle ; utilisé seulement tant que le mode n'a pas d'autre événement), `prediction_rms_c` (pour un événement donné au modèle : racine de la moyenne des carrés des résidus, °C, du modèle en vigueur avant lui sur sa fenêtre, son temps mort et sa constante de temps de réponse fixés ; `null` sans modèle en vigueur) |
| `last_reason` | Issue du dernier événement examiné : l'issue de son ajustement, ou la raison pour laquelle il n'a pas été lu (par exemple `rest before heating too short`, `heating too short`, `temperature rise too small`, `SmartPI not in control`, `thermostat stopped`, `home position unknown`, `no record`, `24 hours`, `eight fits`) ; `null` avant le premier |
| `prediction_rms_c` | Médiane des 7 dernières valeurs `last.prediction_rms_c` du mode (°C) : la qualité de prédiction des nouveaux événements par le modèle ; montrée seulement, `null` avant la première |
| `peak_samples` | Constantes de temps de réponse en secondes lues aux derniers pics de température confirmés après une coupure (au plus 7), à partir du temps mort, de la pente de la pièce avant la coupure et de ses pertes ; montrées seulement |

### Apprentissage du retard de l'émetteur

`tar_learning` décrit l'apprentissage du retard de l'émetteur (voir [Apprentissage du retard de l'émetteur](./vtherm_smartpi.md#apprentissage-du-retard-de-lémetteur)) :

| Champ | Signification |
|---|---|
| `status` | `active`, `checking_dependencies` (jusqu'au démarrage du thermostat), `dependencies_unavailable` (numpy/scipy ne peuvent pas être chargés : valeur du bootstrap utilisée) ou `valve_mode_not_supported` |
| `applied_s` | Temps mort HEAT actif en vigueur pour la régulation (bande proche, maintien de l'intégrale), en secondes : le retard de l'émetteur appris lorsque les gains l'utilisent, sinon `bootstrap_s` |
| `source` | Origine de `applied_s` et de `gains_s` : `bootstrap`, `fast`, `refined` ou `lower_bound` (une borne inférieure issue de montées de confort coupées par le lever) |
| `gains_s` | Temps mort utilisé par les gains, en secondes : la moitié de `applied_s` pour un retard de l'émetteur appris ; sinon `reference_s` lorsqu'il est plus court, deux fois `reference_s` lorsqu'il est plus long et non confirmé, `bootstrap_s` sans retard de l'émetteur appris |
| `bootstrap_s` | Temps mort de chauffe du bootstrap/calibration (`model.deadtime_heat_s`), jamais modifié |
| `reference_s` | Référence des gains, en secondes : le plus grand entre le premier `bootstrap_s` mesuré depuis le début de l'apprentissage (conservé) et le `bootstrap_s` actuel ; `null` tant qu'aucune valeur du bootstrap n'est mesurée |
| `in_force_s` / `in_force_source` | Retard de l'émetteur appris en vigueur (affinée, sinon rapide) et son origine, `null` avant toute publication |
| `out_of_bound` | `true` lorsqu'un retard de l'émetteur appris existe et que les gains ne l'utilisent pas tel quel (voir `limit_reason`) |
| `limit_reason` | Raison pour laquelle les gains n'utilisent pas le retard de l'émetteur appris tel quel (`learned delay shorter than the reference: the gains keep the reference dead time`, ou `learned delay above twice the reference and not confirmed by a second method: the gains use twice the reference dead time`), `null` sinon |
| `fast.value_s` | Retard de l'émetteur rapide publié (montées de confort confirmées), `null` avant 3 montées confirmées |
| `fast.confirmed_mornings` | Nombre de montées de confort acceptées et confirmées, une par période calme au plus |
| `fast.lower_bound_s` | Borne inférieure du retard de l'émetteur en vigueur : médiane des 5 dernières montées de confort confirmées coupées par le lever trop tôt pour donner une valeur, `null` sans aucune |
| `fast.lower_bound_mornings` | Nombre de montées de confort confirmées coupées par le lever qui ont donné une borne inférieure, une par période calme au plus |
| `fast.last_value_s` | Retard de l'émetteur de la dernière montée de confort acceptée |
| `fast.last_status` | `confirmed`, `unconfirmed` ou `none` pour la dernière montée de confort acceptée |
| `fast.last_unconfirmed_reason` | Raison pour laquelle la dernière montée de confort acceptée n'est pas confirmée (par exemple `the room did not reach its low setpoint before the comfort step`, ou au moins 1 heure sans chauffe alors que la pièce était régulée à sa consigne basse), en anglais |
| `fast.last_morning` | Dernière montée de confort évaluée, à toute heure : `day` (date locale, AAAA-MM-JJ), `accepted`, `reason` (`accepted`, ou la raison, en anglais, pour laquelle la montée n'a pas donné de valeur, par exemple `temperature rise too small`, `comfort step in daylight` ou `sunrise too soon after the comfort step`, `comfort step cut by sunrise before its response could be measured` ou `comfort step cut by sunrise: gives only a lower bound of the delay, not a value`), `room_class`, `confirmed` (`true` ou `false` pour une montée qui a donné une valeur ou une borne basse, `null` sinon) et `unconfirmed_reason` (la raison, en anglais, pour laquelle cette montée n'est pas confirmée, comme `last_unconfirmed_reason`) |
| `refined.tau_e_s` | Retard de l'émetteur affiné publié `τe`, `null` avant une publication |
| `refined.class` | Type de pièce en vigueur issu de l'identification : `thermal_mass` (pièce à masse thermique), `no_thermal_mass` ou `undetermined` |
| `refined.k_per_s`, `refined.tau_w_h` | Couplage air-murs et constante de temps des murs en vigueur (pièce `thermal_mass`) |
| `refined.last_evaluation` | Dernière évaluation, faite à la fin d'une période calme : `day` (date locale de cette fin, AAAA-MM-JJ), `regulated_nights` (les six périodes de ses deux ajustements sont régulées), `class` et `tau_e_s` avant la règle des nuits régulées, ainsi que les `a` et `b` identifiés, dans les unités de `model.a` et `model.b` (pour information, jamais appliqués ; pour une pièce `thermal_mass`, `b` est la perte de l'air vers l'extérieur, les murs étant modélisés à part, il diffère donc du `model.b` effectif) |
| `night_room_class` | Type de pièce d'après les périodes calmes sans chauffage : `thermal_mass`, `no_thermal_mass` ou `undetermined` |
| `quiet_periods.reason` | Raison pour laquelle aucune période calme ne peut être trouvée (`home position unknown: the absence of sun cannot be checked`), `null` sinon |
| `quiet_periods.count` | Nombre de périodes calmes conservées comme preuves (une par nuit au plus) |
| `quiet_periods.last` | Dernière période calme fermée : `start` et `end` (heure locale), `regulated_from` (début de sa partie régulée, `null` lorsque la pièce n'a jamais régulé à sa consigne basse) |
| `pending_steps` | Montées de confort en attente de leur réponse de 2 h |
| `data_invalid_reason` | Raison pour laquelle les données actuelles sont exclues (par exemple `window open`, `temperature jump`, `hvac not heating`), `null` lorsqu'elles sont valides |
| `history_h` | Heures de signaux conservées |
| `last_error` | Raison de l'échec du dernier calcul (`the calculation failed, see the log` ; le détail est dans le journal), `null` lorsqu'il n'y en a pas |

### Modèle de pièce

`room_model` décrit le modèle de pièce (voir [Modèle de pièce](./vtherm_smartpi.md#modèle-de-pièce)). Une fois publié, et une fois un bilan thermique en vigueur, il donne la puissance de maintien du feed-forward en chauffage, affichée sous `feedforward`.

| Champ | Signification |
|---|---|
| `status` | `learning`, `identified` (modèle avec murs), `simple model` (modèle sans murs), ou `not identified: ` suivi de la raison, en anglais (par exemple `not identified: too little heating during the quiet periods`) |
| `tracking` | État du suivi de la pièce : `ok`, `waiting for an identified model`, `waiting for measurements`, `paused (SmartPI not heating or not in control)`, `settling after a restart`, `measurements drift away from the model`, `measurements inconsistent with the model`, `temperature estimate uncertain`, `large unexplained heat gains or losses` ou `wall state implausible` |
| `walls` | `identified`, ou la raison pour laquelle les murs ne sont pas dans le modèle : `not identified: temperature sensor too coarse`, `not identified: no thermal mass detected`, `not identified: not confirmed yet`, `not identified: no better than the simple model` ; `null` avant tout résultat |
| `a` | Réponse de chauffe à pleine puissance, dans les unités de `model.a` (°C par minute) |
| `b_eq` | Déperdition statique du modèle, dans les unités de `model.b` (par minute). La puissance de maintien ne l'utilise pas : sa partie statique vient du bilan thermique (`energy_balance_loss_per_c` et `energy_balance_free_gain_percent`), et `b_eq / a` doit correspondre à `energy_balance_loss_per_c` à 5 % près pour qu'il y ait une publication (rien n'est publié avant un premier bilan thermique) |
| `b_air` | Taux de perte totale de l'air, vers l'extérieur et vers les murs (par minute) ; `null` pour le modèle simple |
| `b_wall` | Part en régime établi de cette perte qui passe par les murs (par minute), avec `b_eq` = `b_air` − `b_wall` ; `null` pour le modèle simple |
| `forecast_c` | Température prévue de la pièce à `1h`, `2h`, `6h` et `12h` si la puissance de chauffe actuelle était maintenue (°C) ; `null` tant que le suivi ne tourne pas |
| `forecast_error_c` | Racine de l'erreur quadratique moyenne des prévisions passées, par horizon, sur les 7 derniers jours, par rapport à la température mesurée (°C) ; les prévisions sont rejouées avec la puissance réellement appliquée et la température extérieure mesurée |
| `forecast_checks` | Nombre de prévisions passées vérifiées par horizon |
| `unchanged_temperature_error_c` | Même erreur pour une prévision qui garde la température inchangée, comme référence |
| `wall_extra_power_percent` | Puissance supplémentaire que les murs absorbent actuellement au-delà de leur part en régime établi, en % de la pleine puissance (négative lorsqu'ils restituent de la chaleur) ; `null` sans murs identifiés, sans sonde précise ou tant que le suivi n'est pas `ok` |
| `identified_day` | Jour des valeurs en usage (le jour où leur dernière période calme s'est terminée), `null` avant une publication |
| `last_identification_day` | Jour de la dernière identification (le jour où sa dernière période calme s'est terminée) |
| `last_refusal_reason` | Raison pour laquelle la dernière identification n'a rien publié (par exemple `waiting for a second series of quiet periods to confirm`, `the quiet periods do not determine the model precisely enough yet`, `the heating response delay does not match the measured temperature`, `the heating response differs too much from the one measured by the regulation`, `the model's heat loss disagrees with the measured heat balance` ou `waiting for a qualified heat balance to check the model's heat loss`), `null` lorsqu'elle a publié |
| `energy_balance_loss_per_c` | Déperdition du bilan thermique en vigueur : la puissance nécessaire par °C d'écart intérieur-extérieur, dans les unités de `model.b / model.a`. C'est la pente d'une droite passant par les bilans thermiques quotidiens des journées entières conservées (jusqu'à 7), calculée une fois par jour. Une fois le modèle publié, la partie statique de la puissance de maintien est `energy_balance_loss_per_c` × (consigne − température extérieure) − l'apport gratuit. Un jour dont le bilan thermique est refusé garde les valeurs précédentes ; `null` avant une première valeur acceptée |
| `energy_balance_free_gain_percent` | Apport gratuit constant du bilan thermique en vigueur, en % de la pleine puissance : chaleur d'une pièce plus chaude voisine ou d'occupants présents de façon stable, négatif pour une perte vers une pièce plus froide. `0` tant que l'écart intérieur-extérieur n'a pas assez varié entre les jours pour distinguer un apport des pertes (et que les jours n'en montrent aucun) ; `null` avant une première valeur acceptée |
| `energy_balance_days` | Nombre de jours utilisés par le bilan thermique en vigueur, `null` avant une première valeur acceptée |
| `energy_balance_day` | Jour où les valeurs en vigueur ont été calculées, `null` avant une première valeur acceptée |
| `energy_balance_refusal_reason` | Raison pour laquelle le dernier bilan thermique quotidien n'a donné aucune nouvelle valeur (`not enough recorded days yet`, `heating response not known yet`, `too few usable days (missing data or no heating)`, `indoor-outdoor temperature difference too small`, `too little heating`, `free heat gains detected; the outdoor temperature has not varied enough yet to separate them from the losses`, `the heating need varies too much from one day to the next`, `the heating need is still drifting from day to day (walls storing or releasing heat)`, `value out of the plausible range` ou `computation failed`), `null` lorsqu'il en a donné une |
| `sensor_resolution_c` | Plus petit pas de la sonde de température observé dans l'historique enregistré (°C) |
| `last_error` | Raison de l'échec du dernier calcul (`the calculation failed, see the log` ; le détail est dans le journal), `null` lorsqu'il n'y en a pas |

### Feed-forward

`feedforward` commence par la source de la puissance de maintien (le
feed-forward structurel) :

| Champ | Signification |
|---|---|
| `source` | `learned model` (le modèle A/B, `model.b / model.a`), `room model (air and walls)` (modèle publié avec murs, suivi `ok` : partie statique plus la puissance supplémentaire des murs), `room model (static part)` (modèle publié avec murs alors que le suivi n'est pas `ok` : partie statique seule) ou `room model (simple)` (modèle publié sans murs). La partie statique d'une source de modèle de pièce vaut `room_model.energy_balance_loss_per_c` × (consigne − température extérieure) − `room_model.energy_balance_free_gain_percent` / 100 ; tant qu'aucun bilan thermique n'est en vigueur, ou tant que son `energy_balance_loss_per_c` s'écarte de plus de 5 % du `b_eq / a` publié, la source reste `learned model` |
| `wall_power_percent` | Puissance supplémentaire des murs appliquée dans la puissance de maintien, en % de la pleine puissance (négative lorsque les murs restituent de la chaleur). Lorsque le terme des murs démarre, s'arrête ou change avec une nouvelle publication, la valeur appliquée passe en 30 minutes de sa dernière valeur à la nouvelle ; elle peut donc différer de `room_model.wall_extra_power_percent` et rester non nulle pendant 30 minutes après que `source` a quitté `room model (air and walls)` ; `null` lorsqu'aucune part des murs n'est appliquée |

`analysis.feedforward` publie `u_ff1`, la puissance de maintien injectée dans la commande, et sa `reason` ; `u_ff_final` et `u_ff_effective` portent la même valeur.

## Fréquence de publication

Le capteur de diagnostic est rafraîchi lors des entrées de contrôle
significatives, des limites de cycle, des calculs explicitement forcés et des
services de diagnostic. Le timer interne de recalcul de 60 secondes ne publie
pas lorsque ses entrées et la puissance engagée sont inchangées. Le capteur
supprime également une écriture lorsque son état et l'enveloppe complète de ses
attributs sont identiques à la publication précédente.

Cela limite les écritures dans la machine d'état et le Recorder sans modifier
la temporisation de contrôle de SmartPI.

## Chemins pour les consommateurs

Les consommateurs live doivent lire `attributes.live`. Les graphiques
historiques doivent lire `attributes.history`. Les consommateurs doivent
vérifier `attributes.schema_version == 2` avant d'interpréter la structure.
