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
| `episode_median_s` | Durée médiane des épisodes actifs récents en secondes, `null` avec moins de 3 épisodes |
| `last_reason` | Issue du dernier épisode actif (estimation retenue, ou raison de son non-usage) |

`valve` vaut `null` sauf si le thermostat pilote une vanne. En mode vanne, il contient :

| Champ | Signification |
|---|---|
| `vt_dpercent` | Seuil de régulation de Versatile Thermostat en `%` (`null` si indisponible) |
| `vt_period_min` | Période minimale de régulation de Versatile Thermostat en minutes (`null` si indisponible) |
| `opening_remap_active` | `true` lorsque les réglages d'ouverture de Versatile Thermostat modifient l'ouverture envoyée à la vanne |
| `quantization_step_linear_percent` | Demande linéaire, en pourcentage, déplacée par un pas de vanne de `1%` à la position de vanne engagée ; `1.0` sans linéarisation, `null` lorsque la vanne est fermée |
| `issues` | Codes de filtrage, de retard de commande ou de transformation d'ouverture en conflit avec la linéarisation SmartPI : `dpercent_filter`, `period_filter`, `opening_remap`. Les mêmes avertissements apparaissent dans les Réparations de Home Assistant ; un filtrage ou un retard ne prouve pas, à lui seul, un échec de l'apprentissage |

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
