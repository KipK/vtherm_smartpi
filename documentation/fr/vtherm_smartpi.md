# SmartPI

- [SmartPI](#smartpi)
  - [Ce que fait SmartPI](#ce-que-fait-smartpi)
  - [Avant de commencer](#avant-de-commencer)
  - [Installation et mise en place](#installation-et-mise-en-place)
    - [Sélectionner SmartPI dans Versatile Thermostat](#sélectionner-smartpi-dans-versatile-thermostat)
    - [Configurer SmartPI](#configurer-smartpi)
      - [Configuration par thermostat](#configuration-par-thermostat)
  - [Vannes de radiateur et linéarisation](#vannes-de-radiateur-et-linéarisation)
    - [Pourquoi une vanne peut être difficile à réguler](#pourquoi-une-vanne-peut-être-difficile-à-réguler)
    - [Ce que fait la linéarisation](#ce-que-fait-la-linéarisation)
    - [Quand l'activer](#quand-lactiver)
    - [Choisir les valeurs](#choisir-les-valeurs)
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

En pratique, l'apprentissage peut prendre de quelques heures à 48 heures sur les systèmes lents ou très inertiels.

## Installation et mise en place

Installez l'intégration via HACS (ou manuellement) comme décrit dans le [README](../../README.fr.md), puis redémarrez Home Assistant.

Deux étapes sont nécessaires après le redémarrage : activer SmartPI dans Versatile Thermostat, puis ajouter l'intégration SmartPI dans Home Assistant.

### Sélectionner SmartPI dans Versatile Thermostat

Ouvrez la configuration de l'appareil Versatile Thermostat que vous souhaitez piloter avec SmartPI. Dans l'étape **Underlyings**, repérez le sélecteur d'algorithme et choisissez **SmartPI**.

![VT underlyings — sélection de l'algorithme SmartPI](../../assets/screens/config_algo.png)

Répétez cette étape pour chaque thermostat que vous souhaitez faire fonctionner avec SmartPI.

### Configurer SmartPI

Une fois SmartPI sélectionné comme algorithme dans au moins un thermostat, ajoutez l'intégration **SmartPI** dans Home Assistant : allez dans **Paramètres → Intégrations → Ajouter une intégration**, puis recherchez *SmartPI*.

Lors de la première installation, SmartPI crée automatiquement une entrée de configuration par défaut avec des valeurs adaptées. Vous pouvez modifier ces valeurs par défaut globales ensuite depuis **Paramètres → Intégrations → SmartPI → Configurer**.

#### Configuration par thermostat

Pour ajouter SmartPI à un thermostat supplémentaire, ouvrez la configuration de l'intégration SmartPI et ajoutez une nouvelle entrée de thermostat. Sélectionnez le thermostat cible dans la liste, puis ajustez les paramètres selon vos besoins.

![SmartPI — configuration par thermostat](../../assets/screens/config_smartpi.png)

Les paramètres disponibles sont identiques à ceux des valeurs par défaut globales. Tout paramètre défini ici remplace la valeur globale correspondante pour ce thermostat uniquement.

## Vannes de radiateur et linéarisation

Cette section concerne uniquement les thermostats qui pilotent directement une vanne, par exemple une tête thermostatique de radiateur.

### Pourquoi une vanne peut être difficile à réguler

Une vanne de radiateur ne se comporte pas toujours comme un chauffage électrique.

Avec un chauffage électrique, demander `40%` produit généralement environ deux fois plus de chaleur que demander `20%`. Avec beaucoup de vannes de radiateur, ce n'est pas aussi régulier : les premiers pourcents peuvent ne presque rien faire, puis une petite ouverture supplémentaire peut laisser passer beaucoup d'eau chaude.

Sur certaines installations, une vanne peut déjà laisser passer la majorité du débit alors qu'elle n'est ouverte qu'à environ `20%`. Ensuite, ouvrir davantage change beaucoup moins le débit.

En pratique, cela peut donner ce type de sensation :

| Commande envoyée à la vanne | Effet possible sur le radiateur |
| --- | --- |
| `0%` à quelques pourcents | pas de chaleur visible |
| petite ouverture | le radiateur commence seulement à chauffer |
| environ `15%` à `25%` | grande partie du débit déjà présente |
| au-delà | changement plus faible, parfois surtout plus de bruit hydraulique |

Les valeurs exactes dépendent de la vanne, du corps de vanne, de l'équilibrage du radiateur et de l'installation hydraulique. Il faut donc éviter de chercher une valeur parfaite au premier essai.

### Ce que fait la linéarisation

La linéarisation de courbe de vanne sert à traduire la demande SmartPI en position de vanne.

SmartPI continue de raisonner en demande de chauffe simple :

- `0%` signifie pas de chauffe,
- `50%` signifie une demande moyenne,
- `100%` signifie une demande maximale.

La linéarisation transforme ensuite cette demande en ouverture de vanne plus adaptée au comportement réel du radiateur. L'objectif est d'utiliser plus finement la petite zone où la vanne change réellement le débit, au lieu d'envoyer directement la demande brute à la vanne.

Exemple simplifié :

| Demande SmartPI | Ouverture envoyée à la vanne |
| --- | --- |
| `0%` | `0%` |
| faible demande | autour de l'ouverture minimale utile |
| `80%` | autour de l'ouverture au coude |
| `100%` | ouverture maximale autorisée |

Cette correction ne remplace pas l'apprentissage SmartPI. Elle aide seulement SmartPI à parler plus naturellement à une vanne non linéaire.

### Quand l'activer

Activez cette option si :

- votre thermostat VTherm est de type vanne,
- SmartPI pilote directement une ouverture de vanne,
- le radiateur semble passer rapidement de froid à très chaud avec seulement quelques pourcents d'ouverture,
- les petites variations de commande donnent des réactions trop fortes ou trop irrégulières.

L'option est proposée uniquement pour les thermostats qui exposent une commande de vanne.

### Choisir les valeurs

Les valeurs par défaut donnent un point de départ raisonnable. Ajustez-les seulement si vous avez observé le comportement de votre radiateur.

| Paramètre | À quoi il sert | Valeur de départ |
| --- | --- | --- |
| **Ouverture minimale de vanne** | Première ouverture où le radiateur commence réellement à chauffer. | `7%` |
| **Demande au coude** | Demande SmartPI à partir de laquelle on considère que la vanne arrive dans sa zone de débit élevé. | `80%` |
| **Ouverture de vanne au coude** | Position physique de la vanne à cette demande. | `15%` |
| **Ouverture maximale de vanne** | Ouverture maximale autorisée. | `100%` |

Pour trouver l'ouverture minimale, le plus simple est d'observer le radiateur :

1. Laissez le radiateur refroidir.
2. Lancez une demande de chauffe.
3. Augmentez doucement l'ouverture de la vanne, par petits pas.
4. Attendez au moins une minute entre deux essais.
5. Notez la première valeur où le tuyau ou le radiateur commence vraiment à chauffer.

Si vous n'avez pas envie de tester finement, gardez les valeurs par défaut. Si votre vanne semble très rapide, une ouverture au coude autour de `20%` à `25%` peut être un essai raisonnable. Si le radiateur devient bruyant à pleine ouverture, baissez l'ouverture maximale.

Après un changement, laissez SmartPI fonctionner plusieurs cycles avant de juger le résultat. Une seule chauffe ne suffit pas toujours à conclure.

## Phases de fonctionnement

### Phase d'apprentissage

SmartPI démarre dans une phase de bootstrap. Pendant cette phase, il utilise une stratégie de chauffe simple pour observer le comportement de la pièce, puis en extrait les paramètres physiques dont il a besoin pour réguler correctement.

#### Comment le bootstrap chauffe

Pendant le bootstrap, SmartPI ne cherche pas à maintenir la température avec précision. Il alterne entre chauffe complète et arrêt complet pour observer des réponses thermiques nettes :

- la chauffe démarre quand la température descend sous `consigne - 0.3°C`,
- la chauffe s'arrête quand la température monte au-dessus de `consigne + 0.5°C`.

Cela produit des oscillations de température visibles, qui sont attendues et volontaires à ce stade.

#### Étape 1 — Mesure des temps morts

La première chose que SmartPI doit apprendre est **le temps de réaction de la pièce** quand la chauffe démarre ou s'arrête. Ces délais s'appellent les *temps morts* :

- **temps mort de chauffe** : le délai entre le moment où SmartPI envoie une commande de chauffe et le moment où la température intérieure commence à monter,
- **temps mort de refroidissement** : le délai entre l'arrêt de la chauffe et le moment où la température commence à baisser.

Les temps morts dépendent du type d'émetteur, de la taille de la pièce et du placement du capteur. Ils doivent être mesurés avant que SmartPI puisse interpréter correctement les observations de chauffe et de refroidissement.

#### Étape 2 — Apprentissage des déperditions (`b`)

Une fois les temps morts considérés comme fiables, SmartPI commence à collecter des **observations de refroidissement** : il mesure à quelle vitesse la pièce perd sa chaleur quand le chauffage est éteint.

À partir de ces observations, il calcule `b`, le coefficient de déperdition thermique. Ce paramètre représente la vitesse à laquelle la pièce se refroidit en fonction de la différence entre température intérieure et extérieure.

SmartPI a besoin d'au moins 8 observations de refroidissement valides avant que `b` soit considéré comme utilisable.

#### Étape 3 — Apprentissage du gain de chauffe (`a`)

Une fois que `b` dispose de suffisamment d'observations pour être considéré comme fiable, SmartPI commence à collecter des **observations de chauffe** : il mesure à quelle vitesse la pièce se réchauffe pour une commande de chauffe donnée.

À partir de ces observations, il calcule `a`, le gain de chauffe. Ce paramètre représente l'efficacité avec laquelle votre système de chauffage fait monter la température intérieure.

SmartPI a besoin d'au moins 6 observations de chauffe valides pour `a`.

#### Sortie du bootstrap

SmartPI quitte le bootstrap et bascule en régulation normale une fois que `a` et `b` disposent de suffisamment d'observations pour publier un premier modèle thermique.

Les buffers d'observation continuent ensuite à se remplir jusqu'à 31 échantillons pendant la régulation normale, ce qui permet au modèle de continuer à se consolider après le bootstrap. La confiance complète reste plus stricte que la sortie de bootstrap — tant que le nombre d'observations n'est pas suffisant pour cette confiance complète, SmartPI régule avec le modèle publié tout en gardant certaines corrections internes gelées.

Ce qu'il faut retenir pendant le bootstrap :

- les oscillations de température sont normales et attendues,
- la régulation est volontairement simple à ce stade,
- les diagnostics sont particulièrement utiles pour suivre la progression,
- la vitesse dépend de la qualité des observations réelles, pas seulement du temps écoulé.

### Phase stable

Quand le modèle thermique devient fiable, SmartPI bascule dans son mode normal de régulation.

À ce moment-là, SmartPI :

- calcule automatiquement ses gains PI à partir du modèle appris,
- ajoute une part anticipative de maintien basée sur la pièce et la température extérieure,
- adapte son comportement près de la consigne avec une deadband et des protections supplémentaires.

Près de la température cible, SmartPI cherche à éviter les micro-corrections permanentes. Le résultat attendu est une régulation plus stable, avec moins de corrections inutiles qu'un TPI fixe.

Lors d'une hausse de consigne en chauffage, le filtre de consigne utilise aussi le modèle appris pour gérer l'approche finale de la cible. La branche proportionnelle suit une référence filtrée, tandis que la consigne brute reste disponible pour la branche intégrale. Près de la cible, SmartPI peut plafonner la demande de chauffe interne quand le modèle prédit que la chaleur déjà injectée suffit à atteindre la consigne. Cet atterrissage aide la pièce à ralentir avant la cible au lieu de continuer à chauffer uniquement parce que le feed-forward ou l'état PI gelé reste positif.

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
| **Délai minimal d'activation** | Durée minimale pendant laquelle le chauffage reste allumé une fois activé. | `0 s` |
| **Délai minimal de désactivation** | Durée minimale pendant laquelle le chauffage reste éteint une fois désactivé. | `0 s` |
| **Deadband** | Zone de tolérance autour de la consigne. | `0.05°C` |
| **Filtre de consigne** | Active le lissage de consigne proportionnel et l'atterrissage de chauffe près de la cible. | `activé` |
| **FF3** | Active une petite correction prédictive près de la consigne dans certaines situations de perturbation. | `désactivé` |
| **Autoriser P dans la deadband** | Permet à la branche proportionnelle de rester active à l'intérieur de la deadband. | `désactivé` |
| **Facteur release tau** | Échelle du délai de relâchement intégral par rapport à la constante de temps apprise. | `0.5` |
| **Seuil bas d'hystérésis** | Seuil de redémarrage pendant le bootstrap. | `0.3°C` |
| **Seuil haut d'hystérésis** | Seuil d'arrêt pendant le bootstrap. | `0.5°C` |
| **Mode debug SmartPI** | Publie des diagnostics plus détaillés. | `désactivé` |
| **Linéarisation de courbe de vanne** | Adapte la demande SmartPI aux vannes de radiateur non linéaires. | `désactivé` |
| **Ouverture minimale de vanne** | Première ouverture utile lorsque la linéarisation est activée. | `7%` |
| **Demande au coude** | Demande SmartPI correspondant au changement de pente de la vanne. | `80%` |
| **Ouverture de vanne au coude** | Ouverture physique de la vanne au changement de pente. | `15%` |
| **Ouverture maximale de vanne** | Ouverture maximale autorisée lorsque la linéarisation est activée. | `100%` |

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
- `target_samples` : taille cible des buffers A/B complets,
- `last_reason` : dernière raison d'acceptation ou de rejet d'apprentissage.

Autres blocs utiles en mode normal :

- `control` : phase et mode de régulation courants,
- `power` : informations de commande du cycle courant et du suivant,
- `temperature` : température mesurée, erreur, état de l'intégrale,
- `model` : `a`, `b`, niveau de confiance et temps morts appris,
- `feedforward` : état du feed-forward et de FF3,
- `setpoint` : informations de consigne filtrée et d'atterrissage,
- `autocalib` : état de la supervision automatique,
- `calibration` : état d'une calibration forcée.

En mode normal, le bloc `setpoint` peut contenir :

- `filtered_setpoint` : référence suivie par la branche proportionnelle,
- `trajectory_active` : indique si une trajectoire de consigne est active,
- `trajectory_source` : indique pourquoi la trajectoire est active,
- `landing_active` : indique si l'atterrissage de chauffe est actif,
- `landing_reason` : raison de l'état d'atterrissage,
- `landing_u_cap` : cap de demande de chauffe interne appliqué pendant l'atterrissage,
- `landing_coast_required` : indique si SmartPI laisse la pièce en roue libre parce que le modèle prédit assez de chaleur stockée.

Si le mode debug SmartPI est activé, le bloc `debug` ajoute des informations internes plus détaillées, notamment la prédiction d'atterrissage, la marge cible, la décision de relâchement et la commande avant/après le cap d'atterrissage.

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
