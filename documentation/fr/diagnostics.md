# Diagnostics SmartPI

SmartPI expose un capteur de diagnostic Home Assistant dédié. Son état indique
la phase de régulation courante et ses attributs suivent le schéma version 2.

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

Le bloc `history` contient ces dix-huit séries :

| Chemin sous `history` | Signification |
|---|---|
| `temperature.indoor` | Température intérieure |
| `temperature.integral_error` | État de l'intégrale (°C·min) ; `ki × integral_error` est sa part de la commande (0 à 1) |
| `control.kp` | Gain proportionnel |
| `control.ki` | Gain intégral |
| `setpoint.filtered_setpoint` | Consigne filtrée utilisée par la régulation |
| `power.applied_percent` | Commande appliquée après les contraintes de sortie |
| `power.command_percent` | Commande demandée avant les effets de l'actionneur |
| `power.pi_percent` | Contribution PI |
| `power.ff_percent` | Contribution feed-forward |
| `model.a` | Gain de chauffe appris |
| `model.b` | Coefficient de déperdition appris |
| `feedforward.source` | Source de la puissance de maintien en vigueur, comme dans `live.feedforward.source` |
| `feedforward.wall_power_percent` | Part des murs du feed-forward appliquée, en % de la pleine puissance (négative lorsque les murs restituent de la chaleur) ; `null` lorsqu'aucune part des murs n'est appliquée |
| `room_model.a` | Réponse de chauffe du modèle de pièce, dans les unités de `model.a` ; `null` avant une publication |
| `room_model.b_eq` | Déperdition statique du modèle de pièce, dans les unités de `model.b` ; `null` avant une publication |
| `room_model.forecast_error_1h_c` | Racine de l'erreur quadratique moyenne des prévisions à 1 h sur les 7 derniers jours (°C) |
| `room_model.forecast_error_12h_c` | Idem pour les prévisions à 12 h (°C) |
| `deadtime.applied_s` | Temps mort de chauffe utilisé par la régulation, en secondes (valeur du bootstrap ou valeur apprise) |

Les séries feed-forward, modèle de pièce et temps mort sont toujours présentes
et valent `null` lorsque le bloc correspondant n'est pas calculé. La commande
appliquée ne mesure pas la chaleur délivrée.

## Diagnostics live

Le bloc `live` est organisé par responsabilité :

| Bloc | Contenu |
|---|---|
| `control` | Phase, mode, gains (`kp`, `ki`), temps intégral effectif `ti_min` (minutes), saturation, bandes de régulation et dernier redémarrage (`last_restart`) |
| `power` | Puissances du cycle courant et suivant, PI/FF, limites et puissance appliquée |
| `temperature` | Températures intérieure/extérieure, erreurs et état de l'intégrale |
| `model` | Modèle A/B, confiance, constante de temps et temps morts |
| `learning` | Étape d'apprentissage, bootstrap, mises à jour acceptées et dérive |
| `governance` | Régime thermique et décision de mise à jour du modèle |
| `feedforward` | Source de la puissance de maintien, état FF3, source de bande morte et diagnostics FFTrim canoniques |
| `setpoint` | Consigne filtrée, trajectoire et résumé du boost |
| `autocalib` | État du superviseur de calibration automatique |
| `calibration` | État de la calibration, compteur d'essais AutoCalib (`autocalib.retry_count`, jusqu'à `AUTOCALIB_MAX_RETRIES`) et date de la dernière calibration |
| `analysis` | Champs avancés utilisés par les cartes de diagnostic fournies |
| `valve` | Réglages de vanne Versatile Thermostat vus par SmartPI, `null` hors mode vanne |
| `tar_learning` | Apprentissage du retard de l'émetteur |
| `room_model` | Modèle de pièce |

`control.last_restart` décrit le dernier redémarrage de Home Assistant ou de
l'intégration. Il vaut `null` avant le premier calcul de la régulation après le
redémarrage, et pour un thermostat qui démarre sans données sauvegardées.

| Champ | Signification |
|---|---|
| `bridged` | `true` lorsque le redémarrage a été ponté : les mesures en cours ont repris et rien n'a été exclu des données d'apprentissage. `false` lorsque SmartPI a repris avec prudence |
| `downtime_s` | Durée de l'arrêt, en secondes ; `null` lorsqu'elle ne peut pas être mesurée (saut d'horloge, pas de données sauvegardées) |
| `reason` | Raison pour laquelle le redémarrage n'a pas été ponté, `null` lorsqu'il l'a été : `stop too long`, `heater state not saved`, `not heating or cooling before the stop`, `not heating or cooling after the restart`, `HVAC mode changed across the stop`, `cycle settings changed`, `indoor temperature unknown`, `temperature jump across the stop`, `clock jump`, `no saved state of the last tick`, ou une exclusion avant l'arrêt ou après le redémarrage (par exemple `window open before the stop`, `safety mode after the restart`) |

`analysis` regroupe les valeurs live avancées dans `control`, `learning`,
`trajectory`, `deadtime`, `governance`, `feedforward` et, lorsqu'il
est disponible, `twin`.

`analysis.deadtime` suit la détection du temps mort :

| Champ | Signification |
|---|---|
| `state` | État du détecteur (`WAITING_HEAT_RESPONSE`, `WAITING_COOL_RESPONSE`, `HEATING`, `COOLING`, `OFF`) |
| `last_power` | Dernière puissance vue par le détecteur |
| `waiting_elapsed_s` | Âge de la réponse attendue en secondes, `null` quand aucune détection n'est armée |
| `heat_last_outcome` / `cool_last_outcome` | Issue de la dernière attente d'une réponse montante / descendante : `detected`, `timeout`, `aborted` (transition de puissance avant détection), `rejected` (la température évoluait déjà dans le sens de la réponse avant le démarrage de la puissance, trop fortement ou trop irrégulièrement pour la mesurer), `ignored` (une mesure plus courte que le temps mort publié, écartée parce que la température évoluait déjà dans le sens de la réponse avant le démarrage ou que la fenêtre avant le démarrage était trop courte pour juger), `null` avant la première |

`analysis.learning.response_lag` décrit le retard de réponse utilisé par l'apprentissage de `a` :

| Champ | Signification |
|---|---|
| `lag_s` | Retard de réponse publié en secondes, `null` tant qu'il n'existe pas un ensemble cohérent d'estimations |
| `estimates` | Nombre d'estimations conservées (7 au plus) |
| `episodes` | Nombre d'épisodes actifs terminés depuis le début de l'apprentissage ; il continue après la publication du retard et peut dépasser `episodes_target` |
| `episodes_target` | Nombre d'épisodes après lequel le modèle de puissance délivrée est résolu sans retard publié (`7`) |
| `episode_median_s` | Durée médiane des épisodes actifs récents en secondes, `null` avec moins de 3 épisodes |
| `last_reason` | Issue du dernier épisode actif (estimation retenue, ou raison de son non-usage). `episode interrupted by restart` : épisode en cours lors d'un redémarrage non ponté, non repris, compté sans estimation. Peut être vide juste après un redémarrage |

Pendant le bootstrap, `learning.bootstrap_status` décrit l'étape en cours :

| Valeur | Signification |
|---|---|
| `step1 - deadtime: heat:… cool:…` | Attente des temps morts actif et passif |
| `step2 - collecting emeas: A:n/5 B:n/5` | Collecte des mesures de `a` et `b` |
| `step2 - waiting response lag: estimates N, episodes K/7` | Collectes complètes ; la publication de `a` attend le modèle de puissance délivrée (retard publié, ou `K` atteint 7) |

`valve` vaut `null` sauf si le thermostat pilote une vanne. En mode vanne, il contient :

| Champ | Signification |
|---|---|
| `vt_dpercent` | Seuil de régulation de Versatile Thermostat en `%` (`null` si indisponible) |
| `vt_period_min` | Période minimale de régulation de Versatile Thermostat en minutes (`null` si indisponible) |
| `opening_remap_active` | `true` lorsque les réglages d'ouverture de Versatile Thermostat modifient l'ouverture envoyée à la vanne |
| `quantization_step_linear_percent` | Demande linéaire, en pourcentage, déplacée par un pas de vanne de `1%` à la position de vanne engagée ; `1.0` sans linéarisation, `null` lorsque la vanne est fermée |
| `issues` | Codes de filtrage, de retard de commande ou de transformation d'ouverture en conflit avec la linéarisation SmartPI : `dpercent_filter`, `period_filter`, `opening_remap`. Les mêmes avertissements apparaissent dans les Réparations de Home Assistant ; un filtrage ou un retard ne prouve pas, à lui seul, un échec de l'apprentissage |

### Apprentissage du retard de l'émetteur

`tar_learning` décrit l'apprentissage du retard de l'émetteur (voir [Apprentissage du retard de l'émetteur](./vtherm_smartpi.md#apprentissage-du-retard-de-lémetteur)) :

| Champ | Signification |
|---|---|
| `status` | `active`, `checking_dependencies` (jusqu'au démarrage du thermostat), `dependencies_unavailable` (numpy/scipy ne peuvent pas être chargés : valeur du bootstrap utilisée) ou `valve_mode_not_supported` |
| `applied_s` | Temps mort HEAT actif utilisé par la régulation (bande proche, maintien de l'intégrale, FF3, jumeau thermique), en secondes : la valeur apprise lorsque les gains l'utilisent, sinon `bootstrap_s` |
| `source` | Origine de `applied_s` et de `gains_s` : `bootstrap`, `fast`, `refined` ou `lower_bound` (une borne inférieure issue de montées de confort coupées par le lever) |
| `gains_s` | Temps mort utilisé par les gains, en secondes : la moitié de `applied_s` pour une valeur apprise ; sinon `reference_s` lorsque la valeur apprise est plus courte, deux fois `reference_s` lorsqu'elle est plus longue et non confirmée, `bootstrap_s` sans valeur apprise |
| `bootstrap_s` | Temps mort du bootstrap/calibration (`model.deadtime_heat_s`), jamais modifié |
| `reference_s` | Référence des gains, en secondes : le plus grand entre le premier `bootstrap_s` mesuré depuis le début de l'apprentissage (conservé) et le `bootstrap_s` actuel ; `null` tant qu'aucune valeur du bootstrap n'est mesurée |
| `in_force_s` / `in_force_source` | Valeur apprise en vigueur (affinée, sinon rapide) et son origine, `null` avant toute publication |
| `out_of_bound` | `true` lorsqu'une valeur apprise existe et que les gains ne l'utilisent pas telle quelle (voir `limit_reason`) |
| `limit_reason` | Raison pour laquelle les gains n'utilisent pas la valeur apprise telle quelle (`learned delay shorter than the reference: the gains keep the reference dead time`, ou `learned delay above twice the reference and not confirmed by a second method: the gains use twice the reference dead time`), `null` sinon |
| `fast.value_s` | Valeur rapide publiée (montées de confort confirmées), `null` avant 3 montées confirmées |
| `fast.confirmed_mornings` | Nombre de montées de confort acceptées et confirmées, une par période calme au plus |
| `fast.lower_bound_s` | Borne inférieure en vigueur : médiane des 5 dernières montées de confort confirmées coupées par le lever trop tôt pour donner une valeur, `null` sans aucune |
| `fast.lower_bound_mornings` | Nombre de montées de confort confirmées coupées par le lever qui ont donné une borne inférieure, une par période calme au plus |
| `fast.last_value_s` | Valeur de la dernière montée de confort acceptée |
| `fast.last_status` | `confirmed`, `unconfirmed` ou `none` pour la dernière montée de confort acceptée |
| `fast.last_unconfirmed_reason` | Raison pour laquelle la dernière montée de confort acceptée n'est pas confirmée (par exemple `the room did not reach its low setpoint before the comfort step`, ou au moins 1 heure sans chauffe alors que la pièce était régulée à sa consigne basse), en anglais |
| `fast.last_morning` | Dernière montée de confort évaluée, à toute heure : `day` (date locale, AAAA-MM-JJ), `accepted`, `reason` (`accepted`, ou la raison, en anglais, pour laquelle la montée n'a pas donné de valeur, par exemple `temperature rise too small`, `comfort step in daylight` ou `sunrise too soon after the comfort step`, `comfort step cut by sunrise before its response could be measured` ou `comfort step cut by sunrise: gives only a lower bound of the delay, not a value`), `room_class`, `confirmed` (`true` ou `false` pour une montée qui a donné une valeur ou une borne basse, `null` sinon) et `unconfirmed_reason` (la raison, en anglais, pour laquelle cette montée n'est pas confirmée, comme `last_unconfirmed_reason`) |
| `refined.tau_e_s` | Valeur affinée publiée `τe`, `null` avant une publication |
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

Tant qu'une source du modèle de pièce est en vigueur, `analysis.feedforward.ff2_frozen` vaut
`true` avec `ff2_freeze_reason` `room_model`, et `u_ff2` vaut `0`.

FFTrim utilise des blocs canoniques imbriqués :

```yaml
live:
  feedforward:
    fftrim:
      stationary: {}
      periodic: {}
      transfer: {}
      command_ownership: {}
      observation_mode: ...
      last_reject_reason: ...
      last_update_reason: ...
      last_result: ...
      last_transaction: ...
      windows_since_update: ...
```

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
