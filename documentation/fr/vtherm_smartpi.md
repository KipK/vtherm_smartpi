# L'algorithme SmartPI

- [L'algorithme SmartPI](#lalgorithme-smartpi)
  - [Principe de fonctionnement](#principe-de-fonctionnement)
  - [Phases de fonctionnement](#phases-de-fonctionnement)
  - [Fonctionnalités avancées](#fonctionnalités-avancées)
  - [Configuration](#configuration)
  - [Métriques de diagnostic](#métriques-de-diagnostic)
  - [Services](#services)

## Principe de fonctionnement

SmartPI est une alternative au TPI classique. Son but est simple : apprendre progressivement le comportement thermique réel de la pièce, puis ajuster automatiquement la régulation.

Concrètement, SmartPI apprend en continu trois éléments principaux :

- **a** : la capacité de chauffe du système,
- **b** : la vitesse de perte thermique de la pièce,
- **les temps morts** : le délai entre un changement de chauffe et la réaction réellement visible sur la température.

À partir de ces informations, SmartPI calcule une commande de chauffe plus précise qu'un simple TPI fixe :

- une part **PI** pour corriger l'écart à la consigne,
- une part **feed-forward** pour anticiper le besoin de maintien,
- des protections supplémentaires quand la température est proche de la consigne.

L'algorithme est réévalué régulièrement et continue d'apprendre tant que les conditions sont jugées fiables.

## Phases de fonctionnement

### Phase 1 : Hystérésis et bootstrap

Au démarrage, SmartPI commence par une phase d'apprentissage en mode hystérésis.

Par défaut :

- la chauffe démarre sous `Consigne - 0.3°C`,
- la chauffe s'arrête au-dessus de `Consigne + 0.5°C`.

Ces seuils sont configurables.

Pendant cette phase, l'apprentissage suit un ordre strict visible dans `specific_states.smart_pi.ab_learning.bootstrap_status` :

1. **Mesure des temps morts** : SmartPI attend d'avoir détecté les délais de réaction en chauffe et en refroidissement.
2. **Collecte initiale** : il collecte les premiers échantillons fiables.
3. **Consolidation du modèle** : il complète son historique jusqu'à obtenir un modèle suffisamment robuste.

Quelques repères utiles :

- `b` doit disposer d'au moins **11** échantillons validés,
- `a` démarre plus tôt, avec un minimum de **7** échantillons, mais reste partiellement bloqué tant que `b` n'a pas assez progressé,
- la cible complète d'historique est de **31** mesures validées par paramètre.

Tant que cette phase n'est pas suffisamment fiable, SmartPI reste en hystérésis.

### Phase 2 : Régulation SmartPI stable

Quand le modèle thermique devient fiable, SmartPI passe en phase stable.

Dans cette phase, la commande combine :

- un **correcteur PI**,
- un **feed-forward** de maintien basé sur la consigne et la température extérieure,
- une gestion spécifique des zones proches de la consigne.

Près de la consigne, SmartPI utilise :

- une **deadband** pour éviter les micro-corrections permanentes,
- une **near-band** pour adapter le comportement autour de la cible,
- un mode de **maintien** qui fait converger la commande vers la puissance réellement utile pour rester stable.

Si l'option **FF3** est activée, SmartPI peut aussi appliquer une petite correction prédictive de court horizon. Cette fonction ne s'active qu'en chauffage, près de la consigne, hors trajectoire de consigne, et seulement lorsqu'un contexte crédible de perturbation externe est détecté à partir d'un écart persistant entre la prédiction du jumeau thermique et la réponse observée, combiné à une dynamique compatible.

### Phase 3 : Auto-calibration

SmartPI surveille aussi la qualité de son apprentissage dans le temps.

Le fonctionnement général est le suivant :

1. Quand le modèle devient fiable, SmartPI prend un **snapshot** de référence.
2. Ensuite, une supervision horaire vérifie si l'apprentissage continue à progresser.
3. Si le modèle stagne, une calibration peut être déclenchée automatiquement.

Le cycle de calibration force alors une séquence simple :

1. refroidissement,
2. chauffe forcée,
3. refroidissement final.

Cette séquence sert surtout à revalider les temps morts et à relancer un apprentissage plus propre.

Deux points utiles à connaître :

- un snapshot roulant est repris tous les **5 jours** environ,
- si le temps mort de refroidissement reste indisponible pendant **7 jours**, SmartPI peut continuer avec un snapshot partiel,
- après **3** échecs de calibration, le thermostat continue de fonctionner mais signale un modèle dégradé dans les diagnostics.

## Fonctionnalités avancées

### 1. Estimation automatique des temps morts

SmartPI mesure automatiquement le délai entre un changement de chauffe et la réaction de la pièce. C'est particulièrement utile sur les systèmes lents ou inertiels.

### 2. Gestion des zones proches de la consigne

Quand la température approche de la cible, SmartPI ne se comporte pas comme loin de la consigne :

- il utilise une **near-band** asymétrique en chauffage,
- il applique des protections pour limiter les dépassements,
- il peut interrompre ou relancer un cycle plus tôt si la situation l'exige.

### 3. Gestion des changements de consigne

Lorsqu'un écart thermique significatif apparaît et que le modèle est jugé fiable, SmartPI active une trajectoire analytique sur la branche P pour lisser la référence de proportionnel.

Cette trajectoire :

- laisse la branche I continuer à utiliser la consigne brute,
- publie une référence filtrée `filtered_setpoint` pour la branche P,
- conserve la consigne brute sur la branche P tant que la pièce est encore loin de la cible,
- utilise le modèle 1R1C appris, `deadtime_cool`, la latence restante du cycle, la puissance déjà engagée sur le cycle courant et la puissance attendue au cycle suivant pour détecter le moment où le freinage tardif doit commencer,
- abaisse la référence proportionnelle en douceur uniquement près de la cible tout en conservant une petite demande P positive pour éviter le sur-freinage,
- remonte progressivement la référence filtrée vers la consigne brute lorsque le freinage n'est plus nécessaire,
- une fois la phase de release engagée pour une trajectoire issue d'un changement de consigne, elle reste dans cette phase jusqu'à la fin de la trajectoire,
- garde un transfert final sans à-coup en attendant à la fois que l'écart de commande proportionnelle soit assez faible et que la température mesurée soit suffisamment proche de la cible avant de couper la trajectoire,
- signale son état via `trajectory_active`,
- s'arrête lorsque ce transfert final reste sans à-coup et que la température mesurée est suffisamment proche de la cible, ou lorsque les conditions de fiabilité ne sont plus réunies.

L'intégrale n'est pas utilisée de la même manière pendant les phases de rattrapage :

- après un changement de consigne significatif,
- après une reprise suivant une ouverture détectée,
- après une reprise suivant un power shedding,
- pendant une trajectoire de rattrapage de perturbation.

Dans ces cas, SmartPI bloque la croissance positive de l'intégrale tant que le système est encore en phase de rattrapage. L'intégrale peut en revanche toujours se décharger si le signal va dans le sens opposé.

La libération ne dépend pas uniquement de la near-band :

- elle attend une vraie phase de stabilisation, détectée par une pente de rapprochement devenue faible de manière persistante,
- cette pente utilise à la fois un critère relatif à la pente de rattrapage observée et un petit plancher absolu, pour éviter d'être trop permissif ou trop strict selon le système,
- pendant une trajectoire de consigne active, elle s'appuie sur l'erreur de la référence effectivement suivie par la branche P (`error_p`, donc la consigne filtrée),
- hors trajectoire, elle continue à s'appuyer sur l'erreur brute de consigne (`error_i`),
- en cas de dépassement signé réel, la libération reste immédiate à partir de l'erreur brute.

Une fois le guard relâché, l'atténuation spécifique de croissance intégrale liée à la trajectoire n'est plus appliquée : l'intégrale peut reprendre une correction normale du résidu statique.

Les reprises après ouverture détectée et après power shedding suivent aussi une règle plus stricte en chauffage :

- SmartPI commence par un `I:HOLD` explicite tant que la chauffe est encore dans la fenêtre utile de `deadtime_heat`,
- à la fin de cette phase, il réévalue l'écart résiduel,
- il n'arme le guard positif que si cet écart reste suffisamment significatif,
- sinon il revient directement au fonctionnement intégral normal.

Cela évite de faire apprendre à l'intégrale un simple rattrapage transitoire alors que la réaction de chauffe n'est pas encore pleinement observable.

Les états transitoires de rattrapage ne sont pas conservés après redémarrage :

- une trajectoire analytique active,
- un `I:HOLD` temporaire de reprise,
- un guard de rattrapage déjà armé.

Après reboot, SmartPI repart donc sans réinjecter ces états servo transitoires dans la session suivante.

### 4. Maintien dans la deadband

Dans la deadband, SmartPI ne se contente pas de "ne rien faire" :

- il entre en maintien sans saut brutal de commande,
- il continue à viser une puissance de maintien cohérente,
- il ajuste lentement son biais feed-forward si la pièce dérive de façon répétée.

SmartPI distingue aussi deux notions :

- l'état de deadband avec hystérésis, utilisé pour stabiliser la machine d'état,
- la deadband réelle de configuration, utilisée pour savoir si P et I doivent effectivement être gelés.

Cela évite qu'un petit écart résiduel reste figé uniquement parce que l'hystérésis maintient encore l'état `in_deadband`.

### 5. Protections complémentaires

SmartPI embarque aussi plusieurs protections utiles au quotidien :

- anti-windup de l'intégrale,
- blocage temporaire de la croissance positive de l'intégrale pendant les reprises et les rattrapages,
- garde thermique lors d'une baisse de consigne,
- protections près de la consigne pour couper plus vite un dépassement ou relancer plus tôt si la pièce retombe.

## Configuration

Les réglages par défaut conviennent dans la plupart des cas.

| Paramètre | Rôle | Valeur par défaut |
|-----------|------|-------------------|
| **Deadband** | Zone de tolérance autour de la consigne. | `0.05°C` |
| **Filtre de consigne** | Active la trajectoire de freinage tardif sur la branche P. | `désactivé` |
| **FF3** | Correction prédictive de court horizon réservée à la récupération de perturbation près de la consigne. | `activé` |
| **Seuil bas d'hystérésis** | Redémarrage en phase bootstrap. | `0.3°C` |
| **Seuil haut d'hystérésis** | Arrêt en phase bootstrap. | `0.5°C` |
| **Mode debug SmartPI** | Ajoute les diagnostics détaillés. | `désactivé` |

> Si la température oscille trop autour de la consigne, le premier réglage à tester est souvent la **deadband**.

## Métriques de diagnostic

Les diagnostics SmartPI sont publiés dans `specific_states.smart_pi`.

- en mode normal, ce bloc contient un résumé structuré du comportement courant,
- en mode debug, ce même bloc conserve ce résumé et ajoute `specific_states.smart_pi.debug`.

### Structure publiée en mode normal

| Bloc | Contenu |
|------|---------|
| `control` | phase courante, mode, état hystérésis, `kp`, `ki`, raison de redémarrage |
| `power` | pourcentage du cycle en cours et du prochain cycle, contribution PI, feed-forward et maintien |
| `temperature` | température mesurée, erreur, intégrale, mode courant de l'intégrale, source du guard intégral |
| `model` | état du modèle thermique : `a`, `b`, niveau de confiance, temps morts |
| `ab_learning` | suivi de l'apprentissage : étape, progression bootstrap, compteurs d'échantillons, dernière raison d'acceptation ou de rejet |
| `governance` | régime courant et décision de mise à jour thermique |
| `feedforward` | état de FF3, exploitabilité du jumeau thermique, source de puissance en deadband |
| `setpoint` | `filtered_setpoint`, `trajectory_active`, source de trajectoire |
| `autocalib` | état de la supervision automatique |
| `calibration` | état du cycle de calibration forcée |

### Focus sur `ab_learning`

Le bloc `ab_learning` est le plus utile pour suivre l'apprentissage sans activer le debug :

| Champ | Description |
|-------|-------------|
| `stage` | Vue synthétique : `bootstrap`, `learning`, `monitoring` ou `degraded` |
| `bootstrap_progress_percent` | avancement du bootstrap |
| `bootstrap_status` | étape bootstrap en cours |
| `accepted_samples_a` | nombre d'échantillons validés pour `a` |
| `accepted_samples_b` | nombre d'échantillons validés pour `b` |
| `target_samples` | taille cible de l'historique |
| `last_reason` | dernière raison produite par la logique d'apprentissage |
| `a_drift_state`, `b_drift_state` | état de la surveillance de dérive |

### Mode debug

Quand le mode debug SmartPI est activé, `specific_states.smart_pi.debug` ajoute notamment :

- les détails de l'apprentissage (`tau_min`, compteurs, motifs de rejet),
- la chaîne complète de commande (`u_cmd`, `u_limited`, `u_applied`, `aw_du`),
- la chaîne feed-forward complète (`u_ff1`, `u_ff2`, `u_ff_final`, `u_ff3`, `u_ff_eff`),
- le contexte d'activation de FF3 (`ff3_disturbance_active`, `ff3_disturbance_reason`, `ff3_disturbance_kind`, `ff3_residual_persistent`, `ff3_dynamic_coherent`),
- l'état des bandes, protections et temps morts, y compris l'état détaillé du guard intégral et la `core deadband`,
- les détails de la trajectoire de consigne (`trajectory_start_sp`, `trajectory_target_sp`, `trajectory_tau_ref`, `trajectory_elapsed_s`, `trajectory_phase`, `trajectory_pending_target_change_braking`, `trajectory_braking_needed`, `trajectory_model_ready`, `trajectory_remaining_cycle_min`, `trajectory_next_cycle_u_ref`, `trajectory_bumpless_u_delta`, `trajectory_bumpless_ready`),
- les détails d'auto-calibration,
- les diagnostics avancés du jumeau thermique quand ils sont disponibles.

## Services

### `reset_smart_pi_learning`

À utiliser si le comportement thermique du logement a changé de façon importante, par exemple après un changement d'émetteur ou de travaux d'isolation.

Ce service remet à zéro l'apprentissage SmartPI et force un retour en phase bootstrap.

### `force_smart_pi_calibration`

Demande une calibration SmartPI pour relancer la mesure des temps morts et permettre aussi d'ajuster `a` et `b`.

Ce service est utile si :

- les temps morts affichés semblent incohérents,
- la régulation réagit moins bien qu'avant,
- vous voulez relancer une séquence de recalibration sans attendre le déclenchement automatique.

Si le thermostat est encore en phase bootstrap/hystérésis, la demande est ignorée.

### `reset_smartpi_integral`

Remet à zéro l'accumulateur intégral du contrôleur SmartPI et libère tout hold intégral actif.

Ce service est utile si :

- l'intégrale a accumulé une valeur inadaptée suite à un événement exceptionnel (coupure de chauffage prolongée, fenêtre ouverte longtemps, etc.),
- vous souhaitez repartir d'un état intégral neutre sans réinitialiser l'ensemble de l'apprentissage SmartPI.
