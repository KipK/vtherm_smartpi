# SmartPI

- [SmartPI](#smartpi)
  - [Ce que fait SmartPI](#ce-que-fait-smartpi)
  - [Avant de commencer](#avant-de-commencer)
  - [Phases de fonctionnement](#phases-de-fonctionnement)
    - [Phase d'apprentissage](#phase-dapprentissage)
    - [Phase stable](#phase-stable)
    - [Recalibration automatique](#recalibration-automatique)
  - [Réglages conseillés](#réglages-conseillés)
  - [Configuration](#configuration)
  - [Diagnostics et carte Markdown](#diagnostics-et-carte-markdown)
  - [Services](#services)

## Ce que fait SmartPI

SmartPI est une alternative au TPI classique pour Versatile Thermostat.

Son objectif est simple : au lieu d'utiliser un comportement fixe, il apprend comment votre pièce chauffe et refroidit réellement, puis adapte automatiquement la régulation.

Concrètement, SmartPI apprend :

- à quelle vitesse votre chauffage réchauffe la pièce,
- à quelle vitesse la pièce perd sa chaleur,
- combien de temps la pièce met à réagir quand la chauffe démarre ou s'arrête.

À partir de là, SmartPI construit une commande de chauffe généralement plus précise qu'un TPI fixe :

- il corrige l'écart actuel à la consigne,
- il estime la puissance nécessaire pour tenir la consigne,
- il applique des protections près de la consigne pour limiter les dépassements et les oscillations inutiles.

Il n'est pas nécessaire de connaître la théorie des boucles PI pour utiliser SmartPI. L'idée importante est surtout qu'il a besoin d'une première période d'apprentissage avant de réguler dans son mode normal.

## Avant de commencer

Pour que SmartPI apprenne correctement, le thermostat doit disposer :

- d'une température intérieure fiable,
- d'une température extérieure,
- d'assez de temps pour observer des phases normales de chauffe et de refroidissement.

Pendant la première phase d'apprentissage, essayez d'éviter :

- les fenêtres ouvertes longtemps,
- les gros changements de planning,
- les apports de chaleur inhabituels comme un fort ensoleillement, une cheminée ou beaucoup de présence,
- les modifications répétées des réglages SmartPI alors que l'apprentissage est encore en cours.

Deux conseils pratiques aident beaucoup :

- laissez SmartPI fonctionner sans interruption pendant le premier ou les deux premiers jours,
- utilisez une consigne suffisamment au-dessus de la température extérieure pour que la réponse de chauffe soit bien visible.

En pratique, il faut souvent compter environ 24 à 48 heures avant que SmartPI puisse passer en régulation stable. Sur les systèmes lents ou très inertiels, cela peut prendre davantage de temps.

## Phases de fonctionnement

### Phase d'apprentissage

SmartPI démarre dans une phase bootstrap basée sur l'hystérésis.

Par défaut :

- la chauffe démarre sous `consigne - 0.3°C`,
- la chauffe s'arrête au-dessus de `consigne + 0.5°C`.

Pendant cette phase, SmartPI mesure d'abord les délais de réaction, puis collecte des observations de chauffe et de refroidissement, puis consolide son modèle thermique.

Tant que le modèle n'est pas jugé assez fiable, SmartPI reste dans ce mode d'apprentissage.

Ce qu'il faut retenir :

- la régulation est volontairement simple à ce stade,
- les diagnostics sont particulièrement utiles pendant cette phase,
- la progression dépend de la qualité des observations réelles, pas seulement du temps écoulé.

### Phase stable

Quand le modèle thermique devient fiable, SmartPI bascule dans son mode normal de régulation.

À ce moment-là, SmartPI :

- calcule automatiquement ses gains PI à partir du modèle appris,
- ajoute une part anticipative de maintien basée sur la pièce et la température extérieure,
- adapte son comportement près de la consigne avec une deadband et des protections supplémentaires.

Près de la température cible, SmartPI cherche à éviter les micro-corrections permanentes. Le résultat attendu est une régulation plus stable, avec moins de corrections inutiles qu'un TPI fixe.

Si l'option `FF3` est activée, SmartPI peut aussi appliquer une petite correction prédictive près de la consigne lorsqu'il détecte un contexte crédible de perturbation externe.

### Recalibration automatique

SmartPI continue de surveiller la qualité de son modèle dans le temps.

Si l'apprentissage n'évolue plus suffisamment, il peut déclencher automatiquement une séquence de recalibration pour rafraîchir le modèle et les temps morts.

Points utiles à connaître :

- un snapshot de référence est mémorisé quand le modèle devient fiable,
- un snapshot roulant est rafraîchi ensuite,
- si le temps mort de refroidissement ne peut pas être appris pendant longtemps, SmartPI peut continuer avec un snapshot partiel,
- après plusieurs échecs de recalibration, SmartPI continue de fonctionner et signale un modèle dégradé dans les diagnostics.

## Réglages conseillés

Les réglages par défaut conviennent dans la plupart des installations.

Pour démarrer simplement :

- gardez les seuils d'hystérésis par défaut,
- laissez `FF3` activé sauf raison précise de le désactiver,
- laissez le filtre de consigne activé par défaut,
- ajustez d'abord la deadband si la température oscille trop autour de la cible.

Évitez de modifier plusieurs paramètres à la fois pendant la première phase d'apprentissage. Il vaut mieux laisser SmartPI terminer un cycle d'apprentissage propre, puis ne changer que ce qui est réellement nécessaire.

## Configuration

| Paramètre | Rôle | Valeur par défaut |
| --- | --- | --- |
| **Deadband** | Zone de tolérance autour de la consigne. | `0.05°C` |
| **Filtre de consigne** | Active le lissage de consigne proportionnel près de la cible. | `activé` |
| **FF3** | Active une petite correction prédictive près de la consigne dans certaines situations de perturbation. | `activé` |
| **Seuil bas d'hystérésis** | Seuil de redémarrage pendant le bootstrap. | `0.3°C` |
| **Seuil haut d'hystérésis** | Seuil d'arrêt pendant le bootstrap. | `0.5°C` |
| **Mode debug SmartPI** | Publie des diagnostics plus détaillés. | `désactivé` |

## Diagnostics et carte Markdown

SmartPI publie ses diagnostics directement à la racine des attributs de l'entité capteur de diagnostic SmartPI.

C'est l'endroit principal à consulter pour savoir :

- si SmartPI est encore en apprentissage ou déjà stable,
- si le modèle est considéré comme fiable,
- si une recalibration ou un mode dégradé a été signalé.

Le bloc le plus utile pendant l'apprentissage est `ab_learning`.

Champs importants :

- `stage` : état global comme `bootstrap`, `learning`, `monitoring` ou `degraded`,
- `bootstrap_progress_percent` : progression du bootstrap,
- `bootstrap_status` : étape bootstrap en cours,
- `accepted_samples_a` : échantillons de chauffe validés,
- `accepted_samples_b` : échantillons de refroidissement validés,
- `target_samples` : taille cible de l'historique,
- `last_reason` : dernière raison d'acceptation ou de rejet d'apprentissage.

Autres blocs utiles en mode normal :

- `control` : phase et mode de régulation courants,
- `power` : informations de commande du cycle courant et du suivant,
- `temperature` : température mesurée, erreur, état de l'intégrale,
- `model` : `a`, `b`, niveau de confiance et temps morts appris,
- `feedforward` : état du feed-forward et de FF3,
- `setpoint` : informations de consigne filtrée,
- `autocalib` : état de la supervision automatique,
- `calibration` : état d'une calibration forcée.

Si le mode debug SmartPI est activé, le bloc `debug` ajoute des informations internes plus détaillées.

Une carte Markdown Home Assistant est aussi disponible pour afficher plus simplement les diagnostics SmartPI dans le tableau de bord.

## Services

SmartPI expose trois services dans le domaine `vtherm_smartpi`.

### `reset_smartpi_learning`

À utiliser quand le comportement thermique du logement a changé de manière importante, par exemple après des travaux d'isolation ou un changement d'émetteurs.

Ce service efface l'apprentissage SmartPI et force un retour en mode bootstrap.

### `force_smartpi_calibration`

À utiliser si vous souhaitez lancer un cycle de calibration sans attendre le déclenchement automatique.

Ce service est utile si :

- les temps morts affichés semblent incohérents,
- la régulation fonctionne moins bien qu'avant,
- vous souhaitez rafraîchir l'apprentissage après un changement important des conditions réelles.

Si SmartPI est encore en phase bootstrap, la demande est ignorée.

### `reset_smartpi_integral`

À utiliser si l'intégrale a conservé une valeur inadaptée après un événement exceptionnel.

Exemples typiques :

- une longue coupure de chauffage,
- une fenêtre restée ouverte longtemps,
- toute situation où vous voulez garder le modèle appris mais repartir d'un état intégral neutre.
