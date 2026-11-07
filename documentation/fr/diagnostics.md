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

Le bloc `history` contient ces huit séries :

| Chemin sous `history` | Signification |
|---|---|
| `temperature.indoor` | Température intérieure |
| `setpoint.filtered_setpoint` | Consigne filtrée utilisée par la régulation |
| `power.applied_percent` | Commande appliquée après les contraintes de sortie |
| `power.command_percent` | Commande demandée avant les effets de l'actionneur |
| `power.pi_percent` | Contribution PI |
| `power.ff_percent` | Contribution feed-forward |
| `model.a` | Gain de chauffe appris |
| `model.b` | Coefficient de déperdition appris |

La commande appliquée ne mesure pas la chaleur délivrée.

## Diagnostics live

Le bloc `live` est organisé par responsabilité :

| Bloc | Contenu |
|---|---|
| `control` | Phase, mode, gains (`kp`, `ki`), temps intégral effectif `ti_min` (minutes), saturation et bandes de régulation |
| `power` | Puissances du cycle courant et suivant, PI/FF, limites et puissance appliquée |
| `temperature` | Températures intérieure/extérieure, erreurs et état de l'intégrale |
| `model` | Modèle A/B, confiance, constante de temps et temps morts |
| `learning` | Étape d'apprentissage, bootstrap, mises à jour acceptées et dérive |
| `governance` | Régime thermique et décision de mise à jour du modèle |
| `feedforward` | État FF3, source de bande morte et diagnostics FFTrim canoniques |
| `setpoint` | Consigne filtrée, trajectoire et résumé du boost |
| `autocalib` | État du superviseur de calibration automatique |
| `calibration` | État de la calibration, compteur d'essais AutoCalib (`autocalib.retry_count`, jusqu'à `AUTOCALIB_MAX_RETRIES`) et date de la dernière calibration |
| `analysis` | Champs avancés utilisés par les cartes de diagnostic fournies |
| `valve` | Réglages de vanne Versatile Thermostat vus par SmartPI, `null` hors mode vanne |
| `tar_learning` | Apprentissage du retard de l'émetteur |
| `room_model` | Modèle de pièce, en observation seulement |

`analysis` regroupe les valeurs live avancées dans `control`, `learning`,
`trajectory`, `deadtime`, `governance`, `feedforward` et, lorsqu'il
est disponible, `twin`.

`analysis.deadtime` suit la détection du temps mort :

| Champ | Signification |
|---|---|
| `state` | État du détecteur (`WAITING_HEAT_RESPONSE`, `WAITING_COOL_RESPONSE`, `HEATING`, `COOLING`, `OFF`) |
| `last_power` | Dernière puissance vue par le détecteur |
| `waiting_elapsed_s` | Âge de la réponse attendue en secondes, `null` quand aucune détection n'est armée |
| `heat_last_outcome` / `cool_last_outcome` | Issue de la dernière attente d'une réponse montante / descendante : `detected`, `timeout`, `aborted` (transition de puissance avant détection), `null` avant la première |

`analysis.learning.response_lag` décrit le retard de réponse utilisé par l'apprentissage de `a` :

| Champ | Signification |
|---|---|
| `lag_s` | Retard de réponse publié en secondes, `null` tant qu'il n'existe pas un ensemble cohérent d'estimations |
| `estimates` | Nombre d'estimations conservées (7 au plus) |
| `episodes` | Nombre d'épisodes actifs terminés depuis le début de l'apprentissage ; il continue après la publication du retard et peut dépasser `episodes_target` |
| `episodes_target` | Nombre d'épisodes après lequel le modèle de puissance délivrée est résolu sans retard publié (`7`) |
| `episode_median_s` | Durée médiane des épisodes actifs récents en secondes, `null` avec moins de 3 épisodes |
| `last_reason` | Issue du dernier épisode actif (estimation retenue, ou raison de son non-usage). `episode interrupted by restart` : épisode en cours lors d'un redémarrage, non repris, compté sans estimation. Peut être vide juste après un redémarrage |

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
| `applied_s` | Temps mort HEAT actif utilisé par la régulation, en secondes |
| `source` | Origine de `applied_s` : `bootstrap`, `fast` ou `refined` |
| `gains_s` | Temps mort utilisé par les gains, en secondes : la moitié de `applied_s` pour une valeur apprise, `applied_s` lui-même pour la valeur du bootstrap |
| `bootstrap_s` | Temps mort du bootstrap/calibration (`model.deadtime_heat_s`), jamais modifié |
| `in_force_s` / `in_force_source` | Valeur apprise en vigueur (affinée, sinon rapide) et son origine, `null` avant toute publication |
| `out_of_bound` | `true` lorsque la valeur en vigueur est hors de l'intervalle de 0,5 à 2 fois `bootstrap_s` et n'est donc pas appliquée |
| `fast.value_s` | Valeur rapide publiée (matins confirmés), `null` avant 3 matins confirmés |
| `fast.confirmed_mornings` | Nombre de matins acceptés et confirmés |
| `fast.last_value_s` | Valeur du dernier matin accepté |
| `fast.last_status` | `confirmed`, `unconfirmed` ou `none` pour le dernier matin accepté |
| `fast.last_unconfirmed_reason` | Raison pour laquelle le dernier matin accepté n'est pas confirmé (par exemple une nuit restée au moins 1 heure sans chauffe entre 23 h et la montée en confort), en anglais |
| `fast.last_morning` | Dernier matin évalué : `day`, `accepted`, `reason` (`accepted`, ou la raison, en anglais, pour laquelle le matin n'a pas donné de valeur, par exemple `temperature rise too small`), `room_class` |
| `refined.tau_e_s` | Valeur affinée publiée `τe`, `null` avant une publication |
| `refined.class` | Type de pièce en vigueur issu de l'identification : `thermal_mass` (pièce à masse thermique), `no_thermal_mass` ou `undetermined` |
| `refined.k_per_s`, `refined.tau_w_h` | Couplage air-murs et constante de temps des murs en vigueur (pièce `thermal_mass`) |
| `refined.last_evaluation` | Dernière évaluation quotidienne : `day`, `regulated_nights` (les six nuits sont retenues), `class` et `tau_e_s` avant la règle des nuits régulées, ainsi que les `a` et `b` identifiés, dans les unités de `model.a` et `model.b` (pour information, jamais appliqués ; pour une pièce `thermal_mass`, `b` est la perte de l'air vers l'extérieur, les murs étant modélisés à part, il diffère donc du `model.b` effectif) |
| `night_room_class` | Type de pièce d'après les nuits sans chauffage : `thermal_mass`, `no_thermal_mass` ou `undetermined` |
| `pending_steps` | Étapes du matin en attente de leur réponse de 2 h |
| `data_invalid_reason` | Raison pour laquelle les données actuelles sont exclues (par exemple `window open`, `temperature jump`, `hvac not heating`), `null` lorsqu'elles sont valides |
| `history_h` | Heures de signaux conservées |
| `last_error` | Dernière erreur de calcul, `null` lorsqu'il n'y en a pas |

### Modèle de pièce

`room_model` décrit le modèle de pièce (voir [Modèle de pièce](./vtherm_smartpi.md#modèle-de-pièce)). Il est calculé en observation seulement : la régulation ne l'utilise pas.

| Champ | Signification |
|---|---|
| `status` | `learning`, `identified` (modèle avec murs), `simple model` (modèle sans murs), ou `not identified: ` suivi de la raison, en anglais (par exemple `not identified: too little heating during the nights`) |
| `tracking` | État du suivi de la pièce : `ok`, `waiting for an identified model`, `waiting for measurements`, `paused (SmartPI not heating or not in control)`, `settling after a restart`, `measurements drift away from the model`, `measurements inconsistent with the model`, `temperature estimate uncertain`, `large unexplained heat gains or losses` ou `wall state implausible` |
| `walls` | `identified`, ou la raison pour laquelle les murs ne sont pas dans le modèle : `not identified: temperature sensor too coarse`, `not identified: no thermal mass detected`, `not identified: not confirmed yet`, `not identified: no better than the simple model` ; `null` avant tout résultat |
| `a` | Réponse de chauffe à pleine puissance, dans les unités de `model.a` (°C par minute) |
| `b_eq` | Déperdition statique, dans les unités de `model.b` (par minute) : la perte qui fixe la puissance de maintien |
| `b_air` | Taux de perte totale de l'air, vers l'extérieur et vers les murs (par minute) ; `null` pour le modèle simple |
| `b_wall` | Part en régime établi de cette perte qui passe par les murs (par minute), avec `b_eq` = `b_air` − `b_wall` ; `null` pour le modèle simple |
| `forecast_c` | Température prévue de la pièce à `1h`, `2h`, `6h` et `12h` si la puissance de chauffe actuelle était maintenue (°C) ; `null` tant que le suivi ne tourne pas |
| `forecast_error_c` | Racine de l'erreur quadratique moyenne des prévisions passées, par horizon, sur les 7 derniers jours, par rapport à la température mesurée (°C) ; les prévisions sont rejouées avec la puissance réellement appliquée et la température extérieure mesurée |
| `forecast_checks` | Nombre de prévisions passées vérifiées par horizon |
| `unchanged_temperature_error_c` | Même erreur pour une prévision qui garde la température inchangée, comme référence |
| `wall_extra_power_percent` | Puissance supplémentaire que les murs absorbent actuellement au-delà de leur part en régime établi, en % de la pleine puissance (négative lorsqu'ils restituent de la chaleur) ; `null` sans murs identifiés, sans sonde précise ou tant que le suivi n'est pas `ok` |
| `identified_day` | Jour des valeurs en usage, `null` avant une publication |
| `last_identification_day` | Jour de la dernière identification quotidienne |
| `last_refusal_reason` | Raison pour laquelle la dernière identification quotidienne n'a rien publié (par exemple `waiting for a second series of nights to confirm`), `null` lorsqu'elle a publié |
| `sensor_resolution_c` | Plus petit pas de la sonde de température observé dans l'historique enregistré (°C) |
| `last_error` | Dernière erreur de calcul, `null` lorsqu'il n'y en a pas |

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
