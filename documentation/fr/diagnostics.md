# Documentation des Diagnostics de SmartPI

**SmartPI** fournit des attributs de diagnostic détaillés pour surveiller l'identification en ligne du modèle thermique, la gouvernance de sécurité (Safety-First), les trajectoires de consigne et les boucles de feed-forward (part anticipative).

Ces diagnostics sont publiés directement à la racine des attributs de l'entité capteur de diagnostic SmartPI dans Home Assistant.

---

## 1. Modes de Diagnostic

SmartPI propose deux modes de diagnostic configurables via le paramètre **Mode debug SmartPI** dans la configuration :

- **Mode Normal (par défaut)** : Publie uniquement les **Clés Essentielles** (`ESSENTIAL_KEYS`) afin de garder les données propres et de limiter la taille de la base de données.
- **Mode Debug** : Publie le dictionnaire complet de variables internes brutes, y compris un bloc avancé nommé **`debug`** et le sous-bloc prédictif **`pred`** lorsque le jumeau thermique est actif.

---

## 2. Diagnostics en Mode Normal (Clés Essentielles)

Ces attributs sont toujours publiés par l'intégration SmartPI, quel que soit le réglage du mode debug.

| Attribut | Type | Catégorie | Description |
| :--- | :--- | :--- | :--- |
| `phase` | `string` / `Enum` | Général | Phase de fonctionnement actuelle de SmartPI (`HYSTERESIS` pendant le bootstrap, `SMARTPI` en régulation active). |
| `regulation_mode` | `string` | Général | Correspondance du mode de régulation actuel (`hysteresis` ou `smartpi`). |
| `hysteresis_state` | `string` | Général | État actuel du contrôleur d'hystérésis (par exemple, état de demande de chauffe ON/OFF). |
| `on_percent` | `float` | Commande | Rapport cyclique / pourcentage de commande de sortie calculé (0.0 à 1.0) pour le cycle suivant. |
| `error` | `float` | Température | Écart de température actuel (`Consigne - Température intérieure`) en °C. |
| `a` | `float` | Modèle | Gain thermique de chauffage estimé ($a$ dans le modèle physique de la pièce). |
| `b` | `float` | Modèle | Coefficient de déperdition thermique de la pièce estimé ($b$ dans le modèle physique de la pièce). |
| `u_pi` | `float` | Commande | Contribution de la branche proportionnelle-intégrale à la commande finale (0.0 à 1.0). |
| `u_ff` | `float` | Commande | Contribution totale du feed-forward (part anticipative) à la commande finale (0.0 à 1.0). |
| `u_hold` | `float` | Commande | Valeur de commande gelée ou maintenue lorsque la régulation est bloquée. |
| `Kp` | `float` | Gains PI | Gain proportionnel calculated ($K_p$) actuellement appliqué. |
| `Ki` | `float` | Gains PI | Gain intégral calculé ($K_i$) actuellement appliqué. |
| `integral_error` | `float` | État PI | Erreur intégrale accumulée (état interne de la branche I). |
| `governance_regime` | `string` | Sécurité | Régime de gouvernance de sécurité actif (ex. `WARMUP`, `EXCITED_STABLE`, `NEAR_BAND`, `DEAD_BAND`, `SATURATED`, `HOLD`, `PERTURBED`, `DEGRADED`). |
| `last_decision_thermal` | `string` | Sécurité | Décision de gouvernance appliquée aux mises à jour du modèle thermique (ex. `ADAPT_ON`, `FREEZE`, `HARD_FREEZE`). |
| `bootstrap_progress` | `float` | Bootstrap | Pourcentage de progression de la phase d'apprentissage bootstrap (présent uniquement en phase `HYSTERESIS`). |
| `bootstrap_state` | `string` | Bootstrap | Sous-état actuel du processus d'apprentissage bootstrap. |
| `a_drift_state` | `string` | Dérive | État du détecteur de dérive pour le paramètre $a$. |
| `b_drift_state` | `string` | Dérive | État du détecteur de dérive pour le paramètre $b$. |
| `a_drift_buffer_count` | `int` | Dérive | Nombre d'éléments dans le buffer de détection de dérive pour le paramètre $a$. |
| `b_drift_buffer_count` | `int` | Dérive | Nombre d'éléments dans le buffer de détection de dérive pour le paramètre $b$. |
| `a_drift_last_reason` | `string` | Dérive | Dernière raison enregistrée pour le changement d'état ou la mise à jour de dérive pour $a$. |
| `b_drift_last_reason` | `string` | Dérive | Dernière raison enregistrée pour le changement d'état ou la mise à jour de dérive pour $b$. |
| `deadtime_heat_s` | `float` | Modèle | Temps de réaction (temps mort) estimé en chauffage (en secondes). |
| `deadtime_cool_s` | `float` | Modèle | Temps de réaction (temps mort) estimé en refroidissement (en secondes). |
| `autocalib_last_trigger_ts` | `string` | AutoCalib | Horodatage ISO du dernier déclenchement d'une calibration automatique. |
| `autocalib_next_check_ts` | `string` | AutoCalib | Horodatage ISO de la prochaine vérification planifiée par le superviseur d'autocalibration. |
| `autocalib_snapshot_age_h` | `float` | AutoCalib | Âge en heures du snapshot du modèle de référence utilisé par le superviseur. |
| `sensor_temperature` | `float` | Température | Température intérieure brute mesurée et utilisée au dernier cycle de calcul. |
| `ext_sensor_temperature` | `float` | Température | Température extérieure brute mesurée et utilisée au dernier cycle de calcul. |
| `t_int_clean` | `float` | Température | Température intérieure propre et filtrée (après filtrage passe-bas adaptatif et rejet des anomalies). |
| `u_ff1` | `float` | Commande | Contribution du feed-forward structurel dérivé de $b/a$. |
| `u_ff2` | `float` | Commande | Composante de biais lent `FFTrim` de la commande de feed-forward. |
| `u_ff_final` | `float` | Commande | Valeur totale calculée du feed-forward avant limites de sécurité. |
| `u_ff3` | `float` | Commande | Composante de correction prédictive à court horizon (FF3) bornée. |
| `u_db_nominal` | `float` | Commande | Commande nominale estimée pour maintenir l'équilibre thermique à l'intérieur de la zone morte. |
| `u_ff_eff` | `float` | Commande | Feed-forward total effectif appliqué au contrôleur. |
| `ff3_enabled` | `boolean` | FF3 | Indique si la correction prédictive à court horizon (FF3) est active. |
| `ff3_reason_disabled` | `string` | FF3 | Raison claire expliquant pourquoi FF3 est désactivé (`none` s'il est actif). |
| `ff3_raw_reason_disabled` | `string` | FF3 | Code d'état interne expliquant la désactivation de FF3. |
| `ff3_horizon_cycles` | `int` | FF3 | Horizon de prédiction utilisé par FF3 (en cycles de contrôle). |
| `ff3_deadtime_cycles` | `int` | FF3 | Temps mort estimé (en cycles) utilisé pour décaler l'horizon dans FF3. |
| `ff3_horizon_capped` | `boolean` | FF3 | Indique si l'horizon calculé pour FF3 a été limité par les bornes maximales. |
| `ff3_action_sensitivity` | `float` | FF3 | Variation de pente de température estimée par unité de commande ($\Delta u$). |
| `ff3_prediction_quality` | `string` | FF3 | Qualité évaluée du modèle pour FF3 (ex. `robust` ou `degraded`). |
| `ff3_authority_factor` | `float` | FF3 | Facteur de sécurité limitant l'autorité maximale autorisée pour les corrections FF3. |
| `twin_status` | `string` | Jumeau | Statut du jumeau thermique 1R1C (`ok` ou `unavailable`). |
| `ff3_twin_usable` | `boolean` | Jumeau | Indique si le jumeau thermique est stabilisé et exploitable pour la correction prédictive. |
| `ab_confidence_state` | `string` | Modèle | État de confiance global de l'identification de $a$ et $b$ (ex. `AB_OK`, `AB_BAD`). |
| `deadband_power_source` | `string` | Deadband | Source de puissance utilisée pendant l'état de deadband (zone morte). |
| `deadband_p_mode` | `string` | Deadband | Mode de calcul de la branche proportionnelle appliqué dans le deadband. |
| `ff2_trim_delta` | `float` | Commande | Correction de trim feed-forward lent (`FFTrim`). |
| `fftrim_last_reject_reason` | `string` | Commande | Raison du rejet de la dernière mise à jour de trim lent. |
| `fftrim_last_update_reason` | `string` | Commande | Raison de l'acceptation de la dernière mise à jour de trim lent. |
| `fftrim_cycles_since_update` | `int` | Commande | Nombre de cycles écoulés depuis la dernière mise à jour de `FFTrim`. |
| `integral_hold_active` | `boolean` | État PI | Indique si l'accumulateur de la branche intégrale est actuellement gelé. |
| `integral_hold_mode` | `string` | État PI | Mode actif ou raison du gel de l'intégrale (ex. `window_hold`, `deadband_hold`). |
| `restart_reason` | `string` | Général | Raison du dernier redémarrage de l'algorithme ou de l'intégration. |
| `filtered_setpoint` | `float` | Consigne | Température de consigne filtrée dynamiquement suivie par la branche proportionnelle (branche P). |
| `setpoint_trajectory_active` | `boolean` | Consigne | Indique si la trajectoire analytique de consigne proportionnelle est active. |

---

## 3. Diagnostics en Mode Debug (Clés Supplémentaires Complètes)

Lorsque le **Mode debug SmartPI** est activé, un bloc imbriqué nommé **`debug`** est ajouté. Il contient l'intégralité des variables internes brutes de l'algorithme.

### 3.1 Paramètres Avancés de Modèle et d'Apprentissage

| Attribut | Type | Description |
| :--- | :--- | :--- |
| `tau_min` | `float` | Constante de temps minimale autorisée ($\tau_{min}$) en minutes. |
| `tau_reliable` | `boolean` | Indique si la constante de temps estimée ($\tau = 1/b$) est considérée comme fiable. |
| `learn_ok_count` | `int` | Nombre total de mesures d'apprentissage réussies dans la session courante. |
| `learn_ok_count_a` | `int` | Nombre de mesures de chauffage validées dans l'historique du paramètre $a$. |
| `learn_ok_count_b` | `int` | Nombre de mesures de refroidissement validées dans l'historique du paramètre $b$. |
| `learn_skip_count` | `int` | Nombre de cycles d'apprentissage ignorés dans la session courante. |
| `learn_last_reason` | `string` | Raison textuelle détaillée associée au dernier essai de mise à jour des paramètres. |
| `learn_b_converged` | `boolean` | Indique si le coefficient de perte $b$ a statistiquement convergé (bloquant l'apprentissage de $a$). |
| `learn_a_blocked_by_b` | `boolean` | Indique si l'apprentissage de $a$ est gelé car $b$ n'a pas encore convergé. |
| `diag_dTdt_method` | `string` | Méthode numérique appliquée pour calculer la dérivée de température (ex. `OLS`). |
| `diag_b_mad_over_med` | `float` | Déviation absolue médiane relative (MAD/Med) de l'historique de $b$. |
| `diag_a_mad_over_med` | `float` | Déviation absolue médiane relative (MAD/Med) de l'historique de $a$. |
| `diag_ab_bootstrap` | `boolean` | Indique si l'identification bootstrap initiale de $a$ et $b$ est active. |
| `diag_ab_points` | `int` | Nombre d'échantillons d'apprentissage actuellement enregistrés. |
| `diag_ab_mode_effective` | `string` | Mode d'agrégation mathématique effectif (ex. `median`). |
| `learning_start_dt` | `string` | Horodatage ISO du début de la fenêtre d'apprentissage active. |
| `learn_u_avg` | `float` | Commande moyenne appliquée dans la fenêtre d'apprentissage active. |
| `learn_u_cv` | `float` | Coefficient de variation de la commande ($u_{cv}$) dans la fenêtre d'apprentissage. |
| `learn_u_std` | `float` | Écart-type de la commande ($u_{std}$) dans la fenêtre d'apprentissage active. |

### 3.2 Paramètres Avancés de Contrôle, PI et d'Écarts

| Attribut | Type | Description |
| :--- | :--- | :--- |
| `i_mode` | `string` | Mode actif d'évaluation de l'erreur pour la branche intégrale. |
| `integral_guard_active` | `boolean` | Indique si la protection anti-emballement (guard) sur la croissance de l'intégrale est active. |
| `integral_guard_source` | `string` | État ou condition ayant déclenché la protection anti-emballement de l'intégrale. |
| `integral_guard_mode` | `string` | Régime actif de la protection de l'intégrale. |
| `sat` | `string` | Drapeau de saturation de la commande de sortie (`SAT_HIGH`, `SAT_LOW`, ou `NONE`). |
| `error_p` | `float` | Écart proportionnel (`Consigne Filtrée - Température Intérieure`) en °C. |
| `error_filtered` | `float` | Écart de température filtré passe-bas en °C. |
| `temperature_slope_h` | `float` | Pente horaire estimée de la température de la pièce en °C/h. |
| `near_band_deg` | `float` | Distance définissant la zone d'approche (near-band) en °C. |
| `kp_near_factor` | `float` | Facteur multiplicatif appliqué à $K_p$ dans la zone d'approche. |
| `ki_near_factor` | `float` | Facteur multiplicatif appliqué à $K_i$ dans la zone d'approche. |
| `sign_flip_leak` | `float` | Coefficient de fuite appliqué pour amortir l'état intégral lors d'un changement de consigne. |
| `sign_flip_active` | `boolean` | Indique si la logique d'amortissement de l'intégrale est active. |

### 3.3 Paramètres Avancés de Sorties et Feed-Forward

| Attribut | Type | Description |
| :--- | :--- | :--- |
| `ff_raw` | `float` | Commande de feed-forward brute avant application des limites. |
| `ff_reason` | `string` | Contexte d'identification utilisé pour pondérer le feed-forward structurel. |
| `ff_warmup_ok_count` | `int` | Nombre de cycles réussis pendant la phase de préchauffage du feed-forward. |
| `ff_warmup_cycles` | `int` | Nombre total de cycles de préchauffage exécutés par le planificateur. |
| `ff_scale_unreliable_max` | `float` | Facteur d'échelle maximal autorisé pour le feed-forward en cas de modèle peu fiable. |
| `ff2_authority` | `float` | Limite de correction maximale autorisée pour le trim feed-forward lent (`FFTrim`). |
| `ff2_frozen` | `boolean` | Indique si les ajustements du trim lent sont gelés. |
| `ff2_freeze_reason` | `string` | Raison du gel de la boucle d'adaptation du trim lent. |
| `fftrim_cycle_admissible` | `boolean` | Indique si le cycle actuel remplit les critères de stabilité pour mettre à jour `FFTrim`. |
| `u_ff_ab` | `float` | Composante de feed-forward issue strictement du modèle appris $a$ et $b$. |
| `u_ff_trim` | `float` | Composante de biais calculée par la boucle de trim lent. |
| `u_ff_base` | `float` | Commande de feed-forward de base avant application du trim. |

### 3.4 Paramètres Avancés Prédictifs FF3

| Attribut | Type | Description |
| :--- | :--- | :--- |
| `ff3_candidate_scores` | `list` | Scores quadratiques calculés pour chaque candidat de variation ($\Delta u$) de FF3. |
| `ff3_selected_candidate` | `float` | Variation de commande ($\Delta u$) optimale sélectionnée par le résolveur prédictif. |
| `ff3_disturbance_active` | `boolean` | Indique si une perturbation externe est détectée par le prédicteur. |
| `ff3_disturbance_reason` | `string` | Raison de diagnostic détaillant la détection de la perturbation. |
| `ff3_disturbance_kind` | `string` | Classification de la perturbation (ex. gain de chaleur `gain` ou déperdition `loss`). |
| `ff3_residual_persistent` | `boolean` | Indique si un écart de prédiction persistant a été détecté par le jumeau. |
| `ff3_dynamic_coherent` | `boolean` | Indique si la tendance de température mesurée est cohérente avec l'hypothèse de perturbation. |
| `integral_hold_reason` | `string` | Doublon/alias de `integral_hold_mode`. |
| `signed_error_mode` | `string` | Convention de signe reliant l'erreur à la demande (`positive_means_hvac_demand`). |
| `trim_freeze_reason` | `string` | Doublon/alias de `ff2_freeze_reason`. |
| `regime_prev` | `string` | Régime de gouvernance actif lors du cycle de calcul précédent. |
| `sat_persistent_cycles` | `int` | Nombre de cycles consécutifs pendant lesquels la sortie a été saturée. |
| `cycles_since_reset` | `int` | Nombre de cycles de contrôle écoulés depuis le démarrage ou le reset de l'algorithme. |
| `calculated_on_percent` | `float` | Commande de sortie brute calculée avant saturation ou limites de taux. |
| `committed_on_percent` | `float` | Commande finale enregistrée et envoyée au système pour le cycle actif. |
| `linear_on_percent` | `float` | Commande calculée en espace linéaire (avant linéarisation de vanne). |
| `linear_committed_on_percent` | `float` | Commande finale linéaire appliquée (avant linéarisation de vanne). |
| `valve_linearization_enabled` | `boolean` | Indique si la linéarisation de la courbe de vanne est active. |
| `cycle_min` | `float` | Durée configurée d'un cycle de contrôle en minutes. |

### 3.5 Paramètres Avancés de Trajectoire de Consigne Proportionnelle et d'Atterrissage

| Attribut | Type | Description |
| :--- | :--- | :--- |
| `trajectory_start_sp` | `float` | Température de consigne au démarrage de la trajectoire active. |
| `trajectory_target_sp` | `float` | Température de consigne cible finale de la trajectoire active. |
| `trajectory_tau_ref` | `float` | Constante de temps de référence servant de guide pour la vitesse de la trajectoire. |
| `trajectory_elapsed_s` | `float` | Durée écoulée en secondes depuis le début de la trajectoire active. |
| `trajectory_phase` | `string` | Sous-phase active de la trajectoire analytique (ex. `braking` pour freinage, `release` pour relâchement). |
| `trajectory_pending_target_change_braking` | `boolean` | Indique si un nouveau changement de consigne est intervenu pendant une trajectoire. |
| `trajectory_braking_needed` | `boolean` | Indique si un ralentissement (freinage) est requis par le modèle pour éviter le dépassement. |
| `trajectory_model_ready` | `boolean` | Indique si les paramètres identifiés permettent de calculer la trajectoire analytique. |
| `trajectory_remaining_cycle_min` | `float` | Minutes estimées restantes avant de compléter la phase de trajectoire active. |
| `trajectory_next_cycle_u_ref` | `float` | Commande proportionnelle de référence calculée pour le cycle suivant. |
| `trajectory_bumpless_u_delta` | `float` | Décalage (offset) de commande appliqué pour assurer une transition sans à-coups (bumpless). |
| `trajectory_bumpless_ready` | `boolean` | Indique si le calcul de transition sans à-coups est prêt et valide. |
| `landing_active` | `boolean` | Indique si le plafonnement d'atterrissage limite actuellement la commande. |
| `landing_reason` | `string` | Raison de diagnostic expliquant pourquoi le cap d'atterrissage est actif ou ignoré. |
| `landing_u_cap` | `float` | Plafond de commande linéaire calculé pour maintenir la température sous la cible d'atterrissage. |
| `landing_sp_for_p_cap` | `float` | Limite de consigne utilisée pour plafonner la branche proportionnelle. |
| `landing_predicted_temperature` | `float` | Température finale projetée à la fin de l'horizon d'atterrissage si le chauffage s'arrête. |
| `landing_predicted_rise` | `float` | Hausse de température totale estimée pendant la phase d'atterrissage en °C. |
| `landing_target_margin` | `float` | Marge par rapport à la consigne cible utilisée pour le calcul d'atterrissage sécurisé. |
| `landing_release_allowed` | `boolean` | Indique si le superviseur d'atterrissage autorise à libérer la contrainte de cap. |
| `landing_coast_required` | `boolean` | Indique si le chauffage doit être mis en roue libre (commande minimale) pour atterrir sur la cible. |
| `landing_non_constraining_count` | `int` | Nombre de cycles consécutifs où la contrainte d'atterrissage a été inactive. |
| `landing_time_to_target_min` | `float` | Estimation du temps restant en minutes pour atteindre la consigne cible. |
| `landing_release_blocked_by_slope` | `boolean` | Indique si la sortie de l'atterrissage est bloquée à cause d'une pente thermique trop forte. |
| `landing_u_cmd_before_cap` | `float` | Commande calculée avant application du cap d'atterrissage. |
| `landing_u_cmd_after_cap` | `float` | Commande finale appliquée après application du cap d'atterrissage. |

### 3.6 Paramètres Avancés de Protections et Timers

| Attribut | Type | Description |
| :--- | :--- | :--- |
| `learning_resume_ts` | `int` | Timestamp de fin de la pause d'apprentissage après reprise ou reset. |
| `u_cmd` | `float` | Commande brute calculée par la boucle de contrôle. |
| `u_limited` | `float` | Commande après application des limites de variation maximale. |
| `u_applied` | `float` | Commande réellement envoyée aux actionneurs de chauffage. |
| `aw_du` | `float` | Écart de commande utilisé par le mécanisme anti-windup (anti-emballement). |
| `forced_by_timing` | `boolean` | Indique si le cycle a été forcé par l'échéance temporelle (timeout). |
| `in_deadband` | `boolean` | Indique si la température intérieure est dans la deadband configurée (zone morte). |
| `in_core_deadband` | `boolean` | Indique si la température est dans la zone morte centrale étroite (core deadband). |
| `in_near_band` | `boolean` | Indique si la température est dans la zone d'approche (near-band). |
| `setpoint_boost_active` | `boolean` | Indique si le surcroît de puissance au démarrage (boost) est actif. |
| `hysteresis_thermal_guard` | `boolean` | Indique si la protection thermique d'hystérésis est active suite à une baisse de consigne. |
| `deadtime_heat_reliable` | `boolean` | Indique si le temps mort mesuré au chauffage est considéré comme fiable. |
| `deadtime_cool_reliable` | `boolean` | Indique si le temps mort mesuré au refroidissement est considéré comme fiable. |
| `in_deadtime_window` | `boolean` | Indique si le système est dans une fenêtre de neutralisation (blanking) après une modification. |
| `kp_source` | `string` | Origine heuristique utilisée pour planifier $K_p$ (`heuristic`, `imc`, ou `safe`). |
| `deadtime_skip_count_a` | `int` | Nombre d'échantillons de $a$ ignorés car ils se trouvaient dans le temps mort de chauffe. |
| `deadtime_skip_count_b` | `int` | Nombre d'échantillons de $b$ ignorés car ils se trouvaient dans le temps mort de refroidissement. |
| `deadtime_state` | `string` | État interne de la machine à états de l'estimateur de temps morts (`DeadTimeEstimator`). |
| `deadtime_last_power` | `float` | Commande enregistrée au début du test de temps mort actif. |
| `deadtime_heat_start_time` | `float` | Timestamp du début du calcul du temps mort de chauffe actif. |
| `deadtime_cool_start_time` | `float` | Timestamp du début du calcul du temps mort de refroidissement actif. |
| `near_band_below_deg` | `float` | Distance de la bande d'approche inférieure dynamique en °C. |
| `near_band_above_deg` | `float` | Distance de la bande d'approche supérieure dynamique en °C. |
| `near_band_source` | `string` | Origine du calcul de la zone d'approche (`auto` ou `config`). |
| `guard_cut_active` | `boolean` | Indique si la protection `guard_cut` limite actuellement la commande. |
| `guard_cut_count` | `int` | Nombre total de déclenchements de la protection `guard_cut` dans la session courante. |
| `guard_kick_active` | `boolean` | Indique si la protection `guard_kick` est actuellement active. |
| `guard_kick_count` | `int` | Nombre total de déclenchements de la protection `guard_kick` dans la session courante. |
| `calibration_state` | `string` | État de la calibration forcée (`idle`, `cool_down`, `heat_up`, `final_cool_down`). |
| `last_calibration_time` | `string` | Horodatage ISO du dernier cycle de calibration forcée terminé. |
| `calibration_retry_count` | `int` | Nombre de tentatives effectuées pour la calibration forcée. |
| `autocalib_state` | `string` | État actuel du superviseur d'autocalibration. |
| `autocalib_waiting_reason` | `string` | Raison d'attente expliquant pourquoi l'autocalibration n'a pas encore démarré. |
| `autocalib_model_degraded` | `boolean` | Indique si les performances du modèle se sont dégradées au point d'imposer une autocalibration. |
| `autocalib_triggered_params` | `list` | Liste des paramètres ayant dépassé les limites et initié la calibration. |
| `autocalib_retry_count` | `int` | Nombre d'essais réalisés par le superviseur d'autocalibration. |
| `autocalib_dt_cool_unavailable` | `boolean` | Indique si l'autocalibration s'est lancée par défaut en l'absence de temps mort de refroidissement. |
| `governance_cycle_regimes` | `list` | Régimes de gouvernance observés au cours du cycle de calcul courant. |
| `last_freeze_reason_thermal` | `string` | Raison de gouvernance à l'origine du gel de l'apprentissage du modèle thermique. |
| `last_freeze_reason_gains` | `string` | Raison de gouvernance à l'origine du gel des gains PI. |
| `last_decision_gains` | `string` | Décision de gouvernance appliquée au planificateur de gains. |
| `boost_active` | `boolean` | Doublon/alias de `setpoint_boost_active`. |
| `t_int_raw` | `float` | Température intérieure brute lue sur le capteur avant filtrage. |
| `t_int_lp` | `float` | Température intérieure filtrée par filtre passe-bas exponentiel. |
| `sigma_t_int` | `float` | Écart-type calculé (mesure de bruit) de la température intérieure. |
| `adaptive_tint_update` | `boolean` | Indique si le filtre adaptatif a publié une mise à jour au cours du cycle courant. |
| `adaptive_tint_hold_duration_s`| `float` | Durée en secondes pendant laquelle le filtre a maintenu une valeur constante. |

---

## 4. Diagnostics Prédictifs du Jumeau Thermique (Bloc `pred`)

Lorsque le **Jumeau Thermique 1R1C** est actif et jugé exploitable, le bloc imbriqué **`pred`** s'ajoute aux diagnostics en mode debug.

| Attribut | Type | Description |
| :--- | :--- | :--- |
| `twin_status` | `string` | Statut du modèle de jumeau thermique (`ok` ou `unavailable`). |
| `twin_T_hat` | `float` | Prédiction de température à l'horizon +1 cycle en °C. |
| `twin_T_pred` | `float` | Projection de température à long horizon en °C. |
| `twin_innovation` | `float` | Écart entre la température prédite par le jumeau et celle observée (innovation de Kalman). |
| `twin_rmse_30` | `float` | Erreur quadratique moyenne (RMSE) calculée sur une fenêtre glissante de 30 cycles. |
| `twin_model_reliable` | `boolean` | Indique si les paramètres du jumeau respectent les critères de cohérence physique. |
| `twin_perturbation_dTdt` | `float` | Perturbation de pente représentant des influences thermiques non modélisées. |
| `twin_cusum_pos` | `float` | Somme cumulée positive (détection d'apports thermiques externes non modélisés). |
| `twin_cusum_neg` | `float` | Somme cumulée négative (détection de pertes thermiques externes non modélisées). |
| `twin_external_gain` | `boolean` | Indique si un apport thermique externe non modélisé a été détecté. |
| `twin_external_loss` | `boolean` | Indique si une déperdition thermique externe non modélisée a été détectée. |
| `twin_T_steady` | `float` | Température stabilisée (steady-state) calculée sous les conditions actuelles. |
| `twin_T_steady_reliable`| `boolean` | Indique si la prédiction de la température stabilisée est fiable. |
| `twin_T_steady_max` | `float` | Température stabilisée maximale atteignable à pleine puissance de commande. |
| `twin_T_steady_immediate`| `float` | Température stabilisée immédiate estimée sans temps de retard. |
| `twin_T_steady_passive` | `float` | Température stabilisée passive (équilibre thermique sans chauffage). |
| `twin_setpoint_reachable`| `boolean` | Indique si la consigne active est atteignable sous les conditions en cours. |
| `twin_setpoint_reachable_max`| `boolean` | Indique si la consigne est atteignable à puissance de chauffage maximale. |
| `twin_emitter_saturated`| `boolean` | Indique si l'émetteur de chauffage est saturé. |
| `twin_cooling_model_available`| `boolean` | Indique si les paramètres du modèle de refroidissement sont disponibles. |
| `twin_d_hat_fresh` | `boolean` | Indique si l'estimation de la perturbation non modélisée est récente. |
| `twin_warming_up` | `boolean` | Indique si l'observateur du jumeau thermique est en phase de convergence/stabilisation. |
| `twin_u_eff` | `float` | Commande effective (rapport cyclique) injectée dans les équations du jumeau. |
| `twin_deadtime_s` | `float` | Temps mort thermique utilisé par le jumeau en secondes. |
| `twin_dead_steps` | `int` | Nombre de pas de retard discrets utilisés par l'observateur du jumeau. |
| `twin_T_hat_error` | `float` | Écart de suivi de l'observateur du jumeau. |
| `twin_rmse_pure` | `float` | Erreur RMSE pure de prédiction, sans recalage par filtre de Kalman. |
| `twin_innovation_bias`| `float` | Biais de prédiction moyen de l'innovation. |
| `twin_bias_warning` | `boolean` | Indique si une alerte de biais persistant est active. |
| `twin_auto_reset_triggered`| `boolean` | Indique si l'observateur a été réinitialisé suite à une divergence trop forte. |
| `twin_reset_count` | `int` | Nombre total de réinitialisations de l'observateur effectuées. |
| `eta_s` | `float` | Temps restant estimé (en secondes) pour atteindre la consigne cible. |
| `eta_u` | `float` | Commande de chauffage moyenne attendue pendant la durée de l'ETA. |
| `eta_reason` | `string` | Raison de diagnostic / code de retour pour le calcul de l'ETA. |
| `twin_d_hat` | `float` | Moyenne mobile exponentielle (EMA) de la perturbation thermique externe estimée. |

---

## 5. Structure de Résumé Publiée dans Home Assistant

Home Assistant utilise un résumé structuré sous l'attribut **`specific_states.smart_pi`** de l'entité thermostat. Cette structure est directement dérivée des diagnostics pour alimenter les cartes Lovelace (comme le tableau de bord Equinox).

```json
{
  "control": {
    "phase": "smartpi",
    "mode": "smartpi",
    "hysteresis_state": "idle",
    "kp": 0.85,
    "ki": 0.005,
    "restart_reason": "power_on",
    "saturation_state": "none",
    "in_deadband": false,
    "in_near_band": true,
    "in_deadtime_window": false
  },
  "power": {
    "current_cycle_percent": 35.0,
    "next_cycle_percent": 40.0,
    "linear_current_cycle_percent": 35.0,
    "linear_next_cycle_percent": 40.0,
    "valve_linearization_enabled": false,
    "pi_percent": 25.0,
    "ff_percent": 10.0,
    "hold_percent": 0.0,
    "command_percent": 35.0,
    "limited_percent": 35.0,
    "applied_percent": 35.0
  },
  "temperature": {
    "sensor": 19.5,
    "ext_sensor": 10.2,
    "error": 0.5,
    "integral_error": 50.0,
    "integral_mode": "normal",
    "integral_hold_mode": "none",
    "integral_guard_source": "none"
  },
  "model": {
    "a": 0.0125,
    "b": 0.00035,
    "confidence": "AB_OK",
    "tau_reliable": true,
    "tau_min": 15.0,
    "deadtime_heat_s": 240,
    "deadtime_cool_s": 180,
    "deadtime_heat_reliable": true,
    "deadtime_cool_reliable": true,
    "a_stability_ratio": 0.05,
    "b_stability_ratio": 0.08
  },
  "ab_learning": {
    "stage": "monitoring",
    "bootstrap_progress_percent": 100,
    "bootstrap_status": "done",
    "emea_samples_a": 15,
    "emea_samples_b": 18,
    "bootstrap_target_a": 6,
    "bootstrap_target_b": 8,
    "history_target": 31,
    "accepted_updates_a": 15,
    "accepted_updates_b": 18,
    "learn_b_converged": true,
    "accepted_samples_a": 15,
    "accepted_samples_b": 18,
    "target_samples": 31,
    "last_reason": "ols_robust_success",
    "a_drift_state": "stable",
    "b_drift_state": "stable"
  },
  "governance": {
    "regime": "excited_stable",
    "thermal_update_decision": "adapt_on",
    "thermal_update_reason": "excited_stable"
  },
  "feedforward": {
    "ff3_status": "active",
    "ff3_twin_usable": true,
    "twin_status": "ok",
    "deadband_power_source": "none",
    "deadband_p_mode": "none"
  },
  "setpoint": {
    "filtered_setpoint": 20.0,
    "trajectory_active": false,
    "trajectory_source": "none",
    "landing_active": false,
    "landing_reason": "none",
    "landing_u_cap": null,
    "landing_coast_required": false
  },
  "autocalib": {
    "state": "idle",
    "model_degraded": false,
    "last_trigger_ts": "2026-05-18T12:00:00Z",
    "next_check_ts": "2026-05-23T12:00:00Z",
    "snapshot_age_h": 24.0
  },
  "calibration": {
    "state": "idle",
    "retry_count": 0,
    "last_time": "2026-05-15T08:00:00Z"
  }
}
```
