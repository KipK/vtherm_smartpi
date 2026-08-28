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
| `power.applied_percent` | Puissance physiquement appliquée |
| `power.command_percent` | Commande demandée avant les effets de l'actionneur |
| `power.pi_percent` | Contribution PI |
| `power.ff_percent` | Contribution feed-forward |
| `model.a` | Gain de chauffe appris |
| `model.b` | Coefficient de déperdition appris |

## Diagnostics live

Le bloc `live` est organisé par responsabilité :

| Bloc | Contenu |
|---|---|
| `control` | Phase, mode, gains, saturation et bandes de régulation |
| `power` | Puissances du cycle courant et suivant, PI/FF, limites et puissance appliquée |
| `temperature` | Températures intérieure/extérieure, erreurs et état de l'intégrale |
| `model` | Modèle A/B, confiance, constante de temps et temps morts |
| `learning` | Étape d'apprentissage, bootstrap, mises à jour acceptées et dérive |
| `governance` | Régime thermique et décision de mise à jour du modèle |
| `feedforward` | État FF3, source de bande morte et diagnostics FFTrim canoniques |
| `setpoint` | Consigne filtrée, trajectoire, boost et résumé d'atterrissage |
| `autocalib` | État du superviseur de calibration automatique |
| `calibration` | État, nombre d'essais et date de la dernière calibration |
| `analysis` | Champs avancés utilisés par les cartes de diagnostic fournies |

`analysis` regroupe les valeurs live avancées dans `control`, `learning`,
`trajectory`, `landing`, `deadtime`, `governance`, `feedforward` et, lorsqu'il
est disponible, `twin`.

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
